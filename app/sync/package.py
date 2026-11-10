"""Content packages: build (export), validate and import.

A package is a zip file:

    manifest.json        format, version, package_uuid, created_at, source, items[]
    data/content.json    the database records (subjects, courses, topics, lessons,
                         resources, quizzes with questions and choices)
    files/<uuid>.<ext>   the learning files
    checksums.sha256     "<sha256>  <path>" for every other file in the zip

Records are matched across installations by UUID. Each item carries a checksum
of its content, so an import can skip items that are already identical, update
items that changed, and add new ones. Nothing is written until the whole
package has passed validation.
"""
import hashlib
import io
import json
import re
import shutil
import stat
import uuid as uuid_lib
import zipfile
from pathlib import Path

from flask import current_app

from .. import __version__, storage
from ..db import audit, connect, get_setting, new_uuid, now, query

FORMAT = "stembox-package"
VERSION = 1
SUPPORTED_VERSIONS = {1}
MAX_ENTRIES = 20000
MAX_JSON_BYTES = 50 * 1024 * 1024
FILE_NAME_RE = re.compile(r"^files/[A-Za-z0-9_-]{1,64}\.[a-z0-9]{1,5}$")
FIXED_NAMES = {"manifest.json", "checksums.sha256", "data/content.json"}


class PackageError(Exception):
    """The package was rejected. `problems` is a list of plain-language reasons."""

    def __init__(self, problems):
        self.problems = problems if isinstance(problems, list) else [problems]
        super().__init__("; ".join(self.problems))


# ---------------------------------------------------------------- portable records

def _checksum(data):
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# The fields that define each item's content (parent links are compared separately).
CONTENT_FIELDS = {
    "subject": ("name", "description"),
    "course": ("title", "level", "description"),
    "topic": ("title", "position"),
    "lesson": ("title", "body", "status", "position"),
    "resource": ("type", "title", "mime_type", "size", "checksum", "position"),
    "quiz": ("title", "description", "pass_mark", "status", "questions"),
}


def item_checksum(kind, record):
    return _checksum({field: record.get(field) for field in CONTENT_FIELDS[kind]})


def _quiz_questions(conn, quiz_id):
    questions = []
    for q in conn.execute("SELECT * FROM questions WHERE quiz_id = ? ORDER BY position, id", (quiz_id,)):
        choices = [{"text": c["text"], "is_correct": bool(c["is_correct"])}
                   for c in conn.execute("SELECT * FROM choices WHERE question_id = ? ORDER BY position, id", (q["id"],))]
        questions.append({"uuid": q["uuid"], "text": q["text"], "choices": choices})
    return questions


# ---------------------------------------------------------------- export

def collect(conn, subject_ids, course_ids, include_drafts=False):
    """Gather portable records for the chosen subjects/courses."""
    course_ids = set(course_ids or [])
    if subject_ids:
        marks = ",".join("?" * len(subject_ids))
        course_ids |= {r["id"] for r in conn.execute(f"SELECT id FROM courses WHERE subject_id IN ({marks})", list(subject_ids))}
    content = {k: [] for k in ("subjects", "courses", "topics", "lessons", "resources", "quizzes")}
    files = []  # (zip name, absolute path)
    if not course_ids:
        return content, files
    marks = ",".join("?" * len(course_ids))
    courses = conn.execute(f"SELECT c.*, s.uuid AS subject_uuid FROM courses c JOIN subjects s ON s.id = c.subject_id "
                           f"WHERE c.id IN ({marks}) ORDER BY c.id", list(course_ids)).fetchall()
    subject_seen = set()
    for c in courses:
        if c["subject_id"] not in subject_seen:
            subject_seen.add(c["subject_id"])
            s = conn.execute("SELECT * FROM subjects WHERE id = ?", (c["subject_id"],)).fetchone()
            content["subjects"].append({"uuid": s["uuid"], "name": s["name"], "description": s["description"]})
        content["courses"].append({"uuid": c["uuid"], "subject_uuid": c["subject_uuid"], "title": c["title"],
                                   "level": c["level"], "description": c["description"]})
        topic_uuid = {}
        for t in conn.execute("SELECT * FROM topics WHERE course_id = ? ORDER BY position, id", (c["id"],)):
            topic_uuid[t["id"]] = t["uuid"]
            content["topics"].append({"uuid": t["uuid"], "course_uuid": c["uuid"], "title": t["title"],
                                      "position": t["position"]})
        status_sql = "" if include_drafts else " AND status = 'published'"
        lesson_uuid = {}
        for lesson in conn.execute(f"SELECT * FROM lessons WHERE course_id = ?{status_sql} ORDER BY position, id", (c["id"],)):
            lesson_uuid[lesson["id"]] = lesson["uuid"]
            content["lessons"].append({
                "uuid": lesson["uuid"], "course_uuid": c["uuid"], "topic_uuid": topic_uuid.get(lesson["topic_id"]),
                "title": lesson["title"], "body": lesson["body"],
                "status": lesson["status"] if lesson["status"] == "published" else "draft",
                "position": lesson["position"]})
            for r in conn.execute("SELECT * FROM resources WHERE lesson_id = ? ORDER BY position, id", (lesson["id"],)):
                ext = Path(r["file_path"]).suffix.lower()
                zip_name = f"files/{r['uuid'].replace('-', '')}{ext}"
                files.append((zip_name, storage.resolve(r["file_path"])))
                content["resources"].append({
                    "uuid": r["uuid"], "lesson_uuid": lesson["uuid"], "type": r["type"], "title": r["title"],
                    "file": zip_name, "mime_type": r["mime_type"], "size": r["size"], "checksum": r["checksum"],
                    "position": r["position"]})
        for z in conn.execute(f"SELECT * FROM quizzes WHERE course_id = ?{status_sql} ORDER BY id", (c["id"],)):
            content["quizzes"].append({
                "uuid": z["uuid"], "course_uuid": c["uuid"], "lesson_uuid": lesson_uuid.get(z["lesson_id"]),
                "title": z["title"], "description": z["description"], "pass_mark": z["pass_mark"],
                "status": z["status"] if z["status"] == "published" else "draft",
                "questions": _quiz_questions(conn, z["id"])})
    return content, files


KIND_OF = {"subjects": "subject", "courses": "course", "topics": "topic", "lessons": "lesson",
           "resources": "resource", "quizzes": "quiz"}


def build_package(subject_ids, course_ids, include_drafts=False, source="", out_dir=None):
    """Write a package zip. Returns (path, manifest)."""
    conn = connect(current_app.config["DATABASE"])
    try:
        content, files = collect(conn, subject_ids, course_ids, include_drafts)
    finally:
        conn.close()
    items = []
    for key, kind in KIND_OF.items():
        for record in content[key]:
            title = record.get("title") or record.get("name") or ""
            items.append({"type": kind, "uuid": record["uuid"], "title": title,
                          "checksum": item_checksum(kind, record)})
    manifest = {
        "format": FORMAT, "version": VERSION, "package_uuid": new_uuid(), "created_at": now(),
        "source": source or get_setting("site_name") or "STEMBox", "app_version": __version__,
        "include_drafts": bool(include_drafts),
        "counts": {k: len(v) for k, v in content.items()}, "items": items,
    }
    out_dir = Path(out_dir or current_app.config["PACKAGES_DIR"])
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = manifest["created_at"][:10]
    path = out_dir / f"stembox-package-{stamp}-{manifest['package_uuid'][:8]}.zip"
    checksums = []
    manifest_bytes = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
    content_bytes = json.dumps(content, indent=1, ensure_ascii=False).encode("utf-8")
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in (("manifest.json", manifest_bytes), ("data/content.json", content_bytes)):
            zf.writestr(zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0)), data, compress_type=zipfile.ZIP_DEFLATED)
            checksums.append((hashlib.sha256(data).hexdigest(), name))
        for name, abs_path in files:
            zf.write(abs_path, name, compress_type=zipfile.ZIP_STORED)  # media is already compressed
            checksums.append((storage.sha256_file(abs_path), name))
        zf.writestr("checksums.sha256", "".join(f"{h}  {n}\n" for h, n in checksums))
    return path, manifest


# ---------------------------------------------------------------- validation

def _is_uuid(value):
    try:
        uuid_lib.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _safe_name(name):
    if not name or name.startswith(("/", "\\")) or "\\" in name or ":" in name or "\x00" in name:
        return False
    if any(part in ("", ".", "..") for part in name.split("/")):
        return False
    return name in FIXED_NAMES or bool(FILE_NAME_RE.match(name))


def _hash_member(zf, name):
    digest = hashlib.sha256()
    with zf.open(name) as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _req(record, field, types, problems, where, allow_none=False):
    value = record.get(field) if isinstance(record, dict) else None
    if value is None and allow_none:
        return None
    if not isinstance(value, types) or isinstance(value, bool) and types in (int, (int,)):
        problems.append(f"{where}: field “{field}” is missing or has the wrong type")
        return None
    return value


def validate_package(path):
    """Check everything. Returns (manifest, content). Raises PackageError."""
    path = Path(path)
    limit = current_app.config["MAX_PACKAGE_BYTES"]
    if not path.exists() or path.stat().st_size == 0:
        raise PackageError("The package file is empty.")
    if path.stat().st_size > limit:
        raise PackageError("The package is larger than this STEMBox accepts.")
    if not zipfile.is_zipfile(path):
        raise PackageError("This is not a STEMBox package (not a zip file).")
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise PackageError("The package is damaged and cannot be opened.")
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise PackageError("The package contains too many files.")
        problems = []
        names = []
        total = 0
        for info in infos:
            name = info.filename
            if info.is_dir():
                if name.rstrip("/") not in ("files", "data"):
                    problems.append(f"Unexpected folder in package: {name}")
                continue
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                problems.append(f"Links are not allowed in packages: {name}")
            if not _safe_name(name):
                problems.append(f"Unsafe or unexpected file name in package: {name!r}")
            if info.flag_bits & 0x1:
                problems.append(f"Encrypted files are not allowed: {name}")
            total += info.file_size
            names.append(name)
        if total > limit * 2:
            problems.append("The package would unpack to more data than this STEMBox accepts.")
        if len(set(names)) != len(names):
            problems.append("The package lists the same file twice.")
        if problems:
            raise PackageError(problems)
        for required in ("manifest.json", "checksums.sha256", "data/content.json"):
            if required not in names:
                raise PackageError(f"The package is missing {required}.")

        # Checksums first: nothing inside is trusted until it matches.
        listed = {}
        try:
            text = zf.read("checksums.sha256").decode("utf-8")
        except UnicodeDecodeError:
            raise PackageError("checksums.sha256 is not readable.")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            match = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line.strip())
            if not match:
                raise PackageError(f"checksums.sha256 line {line_no} is not valid.")
            listed[match.group(2)] = match.group(1)
        others = set(names) - {"checksums.sha256"}
        if set(listed) != others:
            missing = sorted(others - set(listed))
            extra = sorted(set(listed) - others)
            if missing:
                problems.append("Files without a checksum: " + ", ".join(missing[:5]))
            if extra:
                problems.append("Checksums for files that are not in the package: " + ", ".join(extra[:5]))
            raise PackageError(problems)
        for name, expected in listed.items():
            if _hash_member(zf, name) != expected:
                problems.append(f"Checksum does not match for {name}. The package is damaged or was changed.")
        if problems:
            raise PackageError(problems)

        # Manifest
        try:
            if zf.getinfo("manifest.json").file_size > MAX_JSON_BYTES:
                raise PackageError("manifest.json is too large.")
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise PackageError("manifest.json is not valid JSON.")
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise PackageError("This is not a STEMBox content package.")
        if manifest.get("version") not in SUPPORTED_VERSIONS:
            raise PackageError(f"Unknown package version {manifest.get('version')!r}. "
                               f"This STEMBox reads version {sorted(SUPPORTED_VERSIONS)}.")
        if not _is_uuid(manifest.get("package_uuid")):
            raise PackageError("The package has no valid package_uuid.")
        if not isinstance(manifest.get("items"), list):
            raise PackageError("The manifest has no item list.")

        # Content records
        try:
            if zf.getinfo("data/content.json").file_size > MAX_JSON_BYTES:
                raise PackageError("data/content.json is too large.")
            content = json.loads(zf.read("data/content.json").decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise PackageError("data/content.json is not valid JSON.")
        if not isinstance(content, dict):
            raise PackageError("data/content.json has the wrong layout.")
        for key in KIND_OF:
            if not isinstance(content.get(key, []), list):
                raise PackageError(f"data/content.json: “{key}” must be a list.")
            content.setdefault(key, [])

        _validate_records(content, set(names), zf, problems)
        if problems:
            raise PackageError(problems[:20])

        # Manifest items must describe exactly the records in the package.
        expected = {(KIND_OF[k], r["uuid"]): item_checksum(KIND_OF[k], r) for k in KIND_OF for r in content[k]}
        declared = {}
        for item in manifest["items"]:
            if not isinstance(item, dict):
                raise PackageError("The manifest item list is not valid.")
            declared[(item.get("type"), item.get("uuid"))] = item.get("checksum")
        if declared != expected:
            raise PackageError("The manifest item list does not match the package contents.")
    return manifest, content


def _validate_records(content, names, zf, problems):
    seen = set()

    def check_uuid(record, where):
        value = record.get("uuid") if isinstance(record, dict) else None
        if not _is_uuid(value):
            problems.append(f"{where}: invalid uuid")
            return None
        if value in seen:
            problems.append(f"{where}: the uuid {value} is used twice")
        seen.add(value)
        return value

    subjects = set()
    for i, s in enumerate(content["subjects"]):
        where = f"subject {i + 1}"
        if check_uuid(s, where):
            subjects.add(s["uuid"])
        _req(s, "name", str, problems, where)
        _req(s, "description", str, problems, where)
    courses = set()
    for i, c in enumerate(content["courses"]):
        where = f"course {i + 1}"
        if check_uuid(c, where):
            courses.add(c["uuid"])
        for field in ("title", "level", "description"):
            _req(c, field, str, problems, where)
        if c.get("subject_uuid") not in subjects:
            problems.append(f"{where}: its subject is not in the package")
    topics = set()
    for i, t in enumerate(content["topics"]):
        where = f"topic {i + 1}"
        if check_uuid(t, where):
            topics.add(t["uuid"])
        _req(t, "title", str, problems, where)
        _req(t, "position", int, problems, where)
        if t.get("course_uuid") not in courses:
            problems.append(f"{where}: its course is not in the package")
    lessons = set()
    for i, lesson in enumerate(content["lessons"]):
        where = f"lesson {i + 1}"
        if check_uuid(lesson, where):
            lessons.add(lesson["uuid"])
        for field in ("title", "body"):
            _req(lesson, field, str, problems, where)
        _req(lesson, "position", int, problems, where)
        if lesson.get("status") not in ("draft", "published"):
            problems.append(f"{where}: unknown status")
        if lesson.get("course_uuid") not in courses:
            problems.append(f"{where}: its course is not in the package")
        if lesson.get("topic_uuid") is not None and lesson.get("topic_uuid") not in topics:
            problems.append(f"{where}: its topic is not in the package")
    for i, r in enumerate(content["resources"]):
        where = f"file {i + 1}"
        check_uuid(r, where)
        for field in ("title", "mime_type", "checksum", "file"):
            _req(r, field, str, problems, where)
        _req(r, "size", int, problems, where)
        _req(r, "position", int, problems, where)
        if r.get("lesson_uuid") not in lessons:
            problems.append(f"{where}: its lesson is not in the package")
        rtype = r.get("type")
        if rtype not in storage.ALLOWED:
            problems.append(f"{where}: unknown file type {rtype!r}")
            continue
        name = r.get("file")
        if not isinstance(name, str) or name not in names or not FILE_NAME_RE.match(name):
            problems.append(f"{where}: its file is missing from the package")
            continue
        ext = Path(name).suffix.lower()
        if ext not in storage.ALLOWED[rtype]:
            problems.append(f"{where}: a {rtype} cannot have the extension {ext}")
            continue
        info = zf.getinfo(name)
        if info.file_size != r.get("size"):
            problems.append(f"{where}: file size does not match")
        if _hash_member(zf, name) != r.get("checksum"):
            problems.append(f"{where}: file checksum does not match")
        with zf.open(name) as fh:
            family = storage.detect_family(fh.read(16))
        if family != storage.ALLOWED[rtype][ext][1]:
            problems.append(f"{where}: the file's contents do not match its type")
    for i, z in enumerate(content["quizzes"]):
        where = f"quiz {i + 1}"
        check_uuid(z, where)
        for field in ("title", "description"):
            _req(z, field, str, problems, where)
        pass_mark = _req(z, "pass_mark", int, problems, where)
        if pass_mark is not None and not 0 <= pass_mark <= 100:
            problems.append(f"{where}: pass mark must be 0-100")
        if z.get("status") not in ("draft", "published"):
            problems.append(f"{where}: unknown status")
        if z.get("course_uuid") not in courses:
            problems.append(f"{where}: its course is not in the package")
        if z.get("lesson_uuid") is not None and z.get("lesson_uuid") not in lessons:
            problems.append(f"{where}: its lesson is not in the package")
        questions = z.get("questions")
        if not isinstance(questions, list):
            problems.append(f"{where}: questions must be a list")
            continue
        for j, q in enumerate(questions):
            qwhere = f"{where} question {j + 1}"
            check_uuid(q, qwhere)
            _req(q, "text", str, problems, qwhere)
            choices = q.get("choices") if isinstance(q, dict) else None
            if not isinstance(choices, list) or len(choices) < 2:
                problems.append(f"{qwhere}: needs at least two answers")
                continue
            if sum(1 for c in choices if isinstance(c, dict) and c.get("is_correct") is True) != 1:
                problems.append(f"{qwhere}: needs exactly one correct answer")
            for c in choices:
                if not isinstance(c, dict) or not isinstance(c.get("text"), str):
                    problems.append(f"{qwhere}: an answer is not valid")


# ---------------------------------------------------------------- import

def _local_record(conn, kind, row):
    """Build the portable form of a local row so it can be compared with a package item."""
    if kind == "subject":
        return {"name": row["name"], "description": row["description"]}
    if kind == "course":
        return {"title": row["title"], "level": row["level"], "description": row["description"]}
    if kind == "topic":
        return {"title": row["title"], "position": row["position"]}
    if kind == "lesson":
        return {"title": row["title"], "body": row["body"],
                "status": row["status"] if row["status"] == "published" else "draft", "position": row["position"]}
    if kind == "resource":
        return {k: row[k] for k in ("type", "title", "mime_type", "size", "checksum", "position")}
    if kind == "quiz":
        return {"title": row["title"], "description": row["description"], "pass_mark": row["pass_mark"],
                "status": row["status"] if row["status"] == "published" else "draft",
                "questions": _quiz_questions(conn, row["id"])}
    raise ValueError(kind)


def import_package(path, source="", user_id=None):
    """Validate, then import. Returns a report dict. Raises PackageError if the
    package is rejected (in which case nothing was written)."""
    manifest, content = validate_package(path)
    package_checksum = storage.sha256_file(path)
    report = {"package_uuid": manifest["package_uuid"], "source": manifest.get("source", ""),
              "added": [], "updated": [], "skipped": [], "rejected": []}
    new_files = []      # stored during this import (removed again on failure)
    old_files = []      # replaced by this import (removed after success)
    conn = connect(current_app.config["DATABASE"])
    stamp = now()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ids = {}  # uuid -> local id

        def find(table, uid):
            return conn.execute(f"SELECT * FROM {table} WHERE uuid = ?", (uid,)).fetchone()

        def note(kind, rec, outcome, why=""):
            label = rec.get("title") or rec.get("name") or rec.get("uuid")
            report[outcome].append({"type": kind, "uuid": rec["uuid"], "title": label, "why": why})

        def compare(kind, row, rec, parent_ok=True):
            same = item_checksum(kind, _local_record(conn, kind, row)) == item_checksum(kind, rec)
            return same and parent_ok

        # Subjects (merged by name if the uuid is new but the name already exists)
        for rec in content["subjects"]:
            row = find("subjects", rec["uuid"])
            if row is None:
                by_name = conn.execute("SELECT * FROM subjects WHERE name = ? COLLATE NOCASE", (rec["name"],)).fetchone()
                if by_name:
                    ids[rec["uuid"]] = by_name["id"]
                    note("subject", rec, "skipped", f"merged into the existing subject “{by_name['name']}”")
                    continue
                ids[rec["uuid"]] = conn.execute(
                    "INSERT INTO subjects (uuid, name, description, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                    (rec["uuid"], rec["name"], rec["description"], user_id, stamp)).lastrowid
                note("subject", rec, "added")
            else:
                ids[rec["uuid"]] = row["id"]
                if compare("subject", row, rec):
                    note("subject", rec, "skipped", "already up to date")
                else:
                    conn.execute("UPDATE subjects SET name = ?, description = ? WHERE id = ?",
                                 (rec["name"], rec["description"], row["id"]))
                    note("subject", rec, "updated")

        for rec in content["courses"]:
            subject_id = ids[rec["subject_uuid"]]
            row = find("courses", rec["uuid"])
            if row is None:
                ids[rec["uuid"]] = conn.execute(
                    "INSERT INTO courses (uuid, subject_id, title, level, description, author_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (rec["uuid"], subject_id, rec["title"], rec["level"], rec["description"], user_id, stamp, stamp)).lastrowid
                note("course", rec, "added")
            else:
                ids[rec["uuid"]] = row["id"]
                if compare("course", row, rec, row["subject_id"] == subject_id):
                    note("course", rec, "skipped", "already up to date")
                else:
                    conn.execute("UPDATE courses SET subject_id = ?, title = ?, level = ?, description = ?, updated_at = ? "
                                 "WHERE id = ?", (subject_id, rec["title"], rec["level"], rec["description"], stamp, row["id"]))
                    note("course", rec, "updated")

        for rec in content["topics"]:
            course_id = ids[rec["course_uuid"]]
            row = find("topics", rec["uuid"])
            if row is None:
                ids[rec["uuid"]] = conn.execute("INSERT INTO topics (uuid, course_id, title, position) VALUES (?, ?, ?, ?)",
                                                (rec["uuid"], course_id, rec["title"], rec["position"])).lastrowid
                note("topic", rec, "added")
            else:
                ids[rec["uuid"]] = row["id"]
                if compare("topic", row, rec, row["course_id"] == course_id):
                    note("topic", rec, "skipped", "already up to date")
                else:
                    conn.execute("UPDATE topics SET course_id = ?, title = ?, position = ? WHERE id = ?",
                                 (course_id, rec["title"], rec["position"], row["id"]))
                    note("topic", rec, "updated")

        for rec in content["lessons"]:
            course_id = ids[rec["course_uuid"]]
            topic_id = ids.get(rec["topic_uuid"]) if rec.get("topic_uuid") else None
            published_at = stamp if rec["status"] == "published" else None
            row = find("lessons", rec["uuid"])
            if row is None:
                ids[rec["uuid"]] = conn.execute(
                    "INSERT INTO lessons (uuid, course_id, topic_id, title, body, status, author_id, position, "
                    "published_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (rec["uuid"], course_id, topic_id, rec["title"], rec["body"], rec["status"], user_id,
                     rec["position"], published_at, stamp, stamp)).lastrowid
                note("lesson", rec, "added")
            else:
                ids[rec["uuid"]] = row["id"]
                if compare("lesson", row, rec, row["course_id"] == course_id and row["topic_id"] == topic_id):
                    note("lesson", rec, "skipped", "already up to date")
                else:
                    conn.execute("UPDATE lessons SET course_id = ?, topic_id = ?, title = ?, body = ?, status = ?, "
                                 "position = ?, published_at = COALESCE(published_at, ?), updated_at = ? WHERE id = ?",
                                 (course_id, topic_id, rec["title"], rec["body"], rec["status"], rec["position"],
                                  published_at, stamp, row["id"]))
                    conn.execute("UPDATE publish_requests SET status = 'cancelled' WHERE target_type = 'lesson' "
                                 "AND target_id = ? AND status = 'pending'", (row["id"],))
                    note("lesson", rec, "updated")

        with zipfile.ZipFile(path) as zf:
            for rec in content["resources"]:
                lesson_id = ids[rec["lesson_uuid"]]
                row = find("resources", rec["uuid"])
                if row is not None and compare("resource", row, rec, row["lesson_id"] == lesson_id):
                    note("resource", rec, "skipped", "already up to date")
                    continue
                info = None
                if row is None or row["checksum"] != rec["checksum"]:
                    try:
                        with zf.open(rec["file"]) as fh:
                            info = storage.store_stream(fh, rec["file"], rec["type"])
                    except storage.UploadError as exc:
                        note("resource", rec, "rejected", str(exc))
                        continue
                    new_files.append(info["file_path"])
                if row is None:
                    conn.execute(
                        "INSERT INTO resources (uuid, lesson_id, type, title, file_path, original_name, mime_type, size, "
                        "checksum, position, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (rec["uuid"], lesson_id, rec["type"], rec["title"], info["file_path"], rec["file"].split("/")[-1],
                         info["mime_type"], info["size"], info["checksum"], rec["position"], stamp, stamp))
                    note("resource", rec, "added")
                else:
                    if info:
                        old_files.append(row["file_path"])
                        conn.execute("UPDATE resources SET file_path = ?, mime_type = ?, size = ?, checksum = ? WHERE id = ?",
                                     (info["file_path"], info["mime_type"], info["size"], info["checksum"], row["id"]))
                    conn.execute("UPDATE resources SET lesson_id = ?, type = ?, title = ?, position = ?, updated_at = ? "
                                 "WHERE id = ?", (lesson_id, rec["type"], rec["title"], rec["position"], stamp, row["id"]))
                    note("resource", rec, "updated")

        for rec in content["quizzes"]:
            course_id = ids[rec["course_uuid"]]
            lesson_id = ids.get(rec["lesson_uuid"]) if rec.get("lesson_uuid") else None
            published_at = stamp if rec["status"] == "published" else None
            row = find("quizzes", rec["uuid"])
            if row is not None and compare("quiz", row, rec, row["course_id"] == course_id and row["lesson_id"] == lesson_id):
                note("quiz", rec, "skipped", "already up to date")
                continue
            if row is None:
                quiz_id = conn.execute(
                    "INSERT INTO quizzes (uuid, course_id, lesson_id, title, description, pass_mark, status, author_id, "
                    "published_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (rec["uuid"], course_id, lesson_id, rec["title"], rec["description"], rec["pass_mark"],
                     rec["status"], user_id, published_at, stamp, stamp)).lastrowid
                note("quiz", rec, "added")
            else:
                quiz_id = row["id"]
                conn.execute("UPDATE quizzes SET course_id = ?, lesson_id = ?, title = ?, description = ?, pass_mark = ?, "
                             "status = ?, published_at = COALESCE(published_at, ?), updated_at = ? WHERE id = ?",
                             (course_id, lesson_id, rec["title"], rec["description"], rec["pass_mark"], rec["status"],
                              published_at, stamp, quiz_id))
                conn.execute("UPDATE publish_requests SET status = 'cancelled' WHERE target_type = 'quiz' "
                             "AND target_id = ? AND status = 'pending'", (quiz_id,))
                note("quiz", rec, "updated")
            keep = []
            for pos, q in enumerate(rec["questions"]):
                existing = conn.execute("SELECT id, quiz_id FROM questions WHERE uuid = ?", (q["uuid"],)).fetchone()
                if existing and existing["quiz_id"] == quiz_id:
                    question_id = existing["id"]
                    conn.execute("UPDATE questions SET text = ?, position = ? WHERE id = ?", (q["text"], pos, question_id))
                else:
                    if existing:  # uuid belongs to another quiz locally: give the copy a fresh uuid
                        q = {**q, "uuid": new_uuid()}
                    question_id = conn.execute("INSERT INTO questions (uuid, quiz_id, text, position) VALUES (?, ?, ?, ?)",
                                               (q["uuid"], quiz_id, q["text"], pos)).lastrowid
                keep.append(question_id)
                conn.execute("DELETE FROM choices WHERE question_id = ?", (question_id,))
                for cpos, c in enumerate(q["choices"]):
                    conn.execute("INSERT INTO choices (question_id, text, is_correct, position) VALUES (?, ?, ?, ?)",
                                 (question_id, c["text"], 1 if c["is_correct"] else 0, cpos))
            marks = ",".join("?" * len(keep)) or "NULL"
            conn.execute(f"DELETE FROM questions WHERE quiz_id = ? AND id NOT IN ({marks})", [quiz_id] + keep)

        summary = {k: len(report[k]) for k in ("added", "updated", "skipped", "rejected")}
        conn.execute("INSERT INTO content_packages (uuid, checksum, imported_at, source, summary) VALUES (?, ?, ?, ?, ?)",
                     (manifest["package_uuid"], package_checksum, stamp, source[:200], json.dumps(summary)))
        conn.commit()
    except Exception:
        conn.rollback()
        for rel in new_files:
            storage.delete_file(rel)
        raise
    finally:
        conn.close()
    for rel in old_files:
        storage.delete_file(rel)
    audit("package.import", manifest["package_uuid"], f"{source}: " + json.dumps(summary), user_id=user_id)
    return report
