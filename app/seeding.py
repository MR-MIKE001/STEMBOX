"""Load and remove the placeholder development content in seed/sample_content.json.

Every row created here has is_sample = 1 so `flask unseed-dev` can remove it
without touching real content. Sample files (PDF, PNG, WAV) are generated in
code so no binary placeholder files live in the repository.
"""
import io
import json
import math
import struct
import wave
import zlib
from pathlib import Path

from .db import connect, new_uuid, now
from . import storage

SEED_FILE = Path(__file__).resolve().parent.parent / "seed" / "sample_content.json"


# --- generated placeholder files ---------------------------------------------

def make_pdf(title, lines):
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    text = ["BT", "/F1 22 Tf", "72 770 Td", f"({esc(title)}) Tj", "/F1 13 Tf"]
    for line in lines:
        text += ["0 -24 Td", f"({esc(line)}) Tj"]
    text.append("ET")
    stream = "\n".join(text).encode("latin-1", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def make_png(width=320, height=240):
    """A pie chart showing 3/4 (a fraction picture), drawn pixel by pixel."""
    cx, cy, r = width / 2, height / 2, min(width, height) * 0.4
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            dx, dy = x - cx, y - cy
            if dx * dx + dy * dy <= r * r:
                angle = (math.degrees(math.atan2(dx, -dy)) + 360) % 360
                rgb = (37, 99, 235) if angle < 270 else (226, 232, 240)
                if abs(dx) < 1.2 or abs(dy) < 1.2:
                    rgb = (255, 255, 255)
            else:
                rgb = (255, 255, 255)
            row += bytes(rgb)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def make_wav(seconds=2.0, freq=440.0, rate=8000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(seconds * rate)):
            frames += struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / rate)))
        w.writeframes(bytes(frames))
    return buf.getvalue()


def _generate(kind, title):
    if kind == "pdf":
        return make_pdf("[SAMPLE] " + title, [
            "This is placeholder content for testing STEMBox.",
            "Replace it with real learning material.",
            "",
            "1. Read the lesson.",
            "2. Try the questions.",
            "3. Take the quiz.",
        ]), ".pdf"
    if kind == "png":
        return make_png(), ".png"
    if kind == "wav":
        return make_wav(), ".wav"
    raise ValueError(f"unknown generator {kind}")


# --- loader ------------------------------------------------------------------

def load_sample(app, password_hasher):
    """Insert the sample content. Returns a summary dict. Safe to run twice."""
    data = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    conn = connect(app.config["DATABASE"])
    summary = {"users": 0, "classes": 0, "subjects": 0, "courses": 0, "lessons": 0,
               "resources": 0, "quizzes": 0, "skipped": 0}
    stamp = now()
    try:
        users = {}
        for u in data["users"]:
            row = conn.execute("SELECT id FROM users WHERE username = ?", (u["username"],)).fetchone()
            if row:
                users[u["username"]] = row["id"]
                summary["skipped"] += 1
                continue
            cur = conn.execute(
                "INSERT INTO users (uuid, name, username, password_hash, role, active, "
                "must_change_password, is_sample, created_at) VALUES (?, ?, ?, ?, ?, 1, 1, 1, ?)",
                (new_uuid(), u["name"], u["username"], password_hasher(u["password"]), u["role"], stamp))
            users[u["username"]] = cur.lastrowid
            summary["users"] += 1

        classes = {}
        for c in data["classes"]:
            row = conn.execute("SELECT id FROM classes WHERE name = ? AND is_sample = 1", (c["name"],)).fetchone()
            if row:
                classes[c["name"]] = row["id"]
                summary["skipped"] += 1
                continue
            cur = conn.execute("INSERT INTO classes (uuid, name, level, is_sample, created_at) VALUES (?, ?, ?, 1, ?)",
                               (new_uuid(), c["name"], c["level"], stamp))
            classes[c["name"]] = cur.lastrowid
            for role_key, role in (("teachers", "teacher"), ("students", "student")):
                for username in c.get(role_key, []):
                    conn.execute("INSERT OR IGNORE INTO class_members VALUES (?, ?, ?)",
                                 (cur.lastrowid, users[username], role))
            summary["classes"] += 1

        for s in data["subjects"]:
            if conn.execute("SELECT 1 FROM subjects WHERE name = ? AND is_sample = 1", (s["name"],)).fetchone():
                summary["skipped"] += 1
                continue
            subject_id = conn.execute(
                "INSERT INTO subjects (uuid, name, description, is_sample, created_at) VALUES (?, ?, ?, 1, ?)",
                (new_uuid(), s["name"], s["description"], stamp)).lastrowid
            summary["subjects"] += 1
            for c in s["courses"]:
                author = users.get(c.get("author"))
                course_id = conn.execute(
                    "INSERT INTO courses (uuid, subject_id, class_id, title, level, description, author_id, "
                    "is_sample, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (new_uuid(), subject_id, classes.get(c.get("class")), c["title"], c["level"],
                     c["description"], author, stamp, stamp)).lastrowid
                summary["courses"] += 1
                lessons = {}
                position = 0
                for t_pos, t in enumerate(c["topics"]):
                    topic_id = conn.execute(
                        "INSERT INTO topics (uuid, course_id, title, position) VALUES (?, ?, ?, ?)",
                        (new_uuid(), course_id, t["title"], t_pos)).lastrowid
                    for lesson in t["lessons"]:
                        position += 1
                        lesson_id = conn.execute(
                            "INSERT INTO lessons (uuid, course_id, topic_id, title, body, status, author_id, "
                            "position, is_sample, published_at, created_at, updated_at) "
                            "VALUES (?, ?, ?, ?, ?, 'published', ?, ?, 1, ?, ?, ?)",
                            (new_uuid(), course_id, topic_id, lesson["title"], lesson["body"], author,
                             position, stamp, stamp, stamp)).lastrowid
                        lessons[lesson["title"]] = lesson_id
                        summary["lessons"] += 1
                        for r_pos, r in enumerate(lesson.get("resources", [])):
                            content, ext = _generate(r["generate"], r["title"])
                            with app.app_context():
                                info = storage.store_stream(io.BytesIO(content), "sample" + ext, r["type"])
                            conn.execute(
                                "INSERT INTO resources (uuid, lesson_id, type, title, file_path, original_name, "
                                "mime_type, size, checksum, position, created_at, updated_at) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (new_uuid(), lesson_id, r["type"], r["title"], info["file_path"],
                                 info["original_name"], info["mime_type"], info["size"], info["checksum"],
                                 r_pos, stamp, stamp))
                            summary["resources"] += 1
                for q in c.get("quizzes", []):
                    quiz_id = conn.execute(
                        "INSERT INTO quizzes (uuid, course_id, lesson_id, title, description, pass_mark, status, "
                        "author_id, is_sample, published_at, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'published', ?, 1, ?, ?, ?)",
                        (new_uuid(), course_id, lessons.get(q.get("lesson")), q["title"], q["description"],
                         q["pass_mark"], author, stamp, stamp, stamp)).lastrowid
                    summary["quizzes"] += 1
                    for q_pos, question in enumerate(q["questions"]):
                        question_id = conn.execute(
                            "INSERT INTO questions (uuid, quiz_id, text, position) VALUES (?, ?, ?, ?)",
                            (new_uuid(), quiz_id, question["text"], q_pos)).lastrowid
                        for c_pos, choice in enumerate(question["choices"]):
                            conn.execute(
                                "INSERT INTO choices (question_id, text, is_correct, position) VALUES (?, ?, ?, ?)",
                                (question_id, choice, 1 if c_pos == question["correct"] else 0, c_pos))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return summary


def remove_sample(app):
    """Delete everything flagged is_sample, including stored files. Returns a summary."""
    conn = connect(app.config["DATABASE"])
    try:
        files = [r["file_path"] for r in conn.execute(
            "SELECT r.file_path FROM resources r JOIN lessons l ON l.id = r.lesson_id "
            "JOIN courses c ON c.id = l.course_id JOIN subjects s ON s.id = c.subject_id "
            "WHERE l.is_sample = 1 OR c.is_sample = 1 OR s.is_sample = 1")]
        summary = {}
        for table in ("subjects", "courses", "lessons", "quizzes", "classes", "users"):
            summary[table] = conn.execute(f"DELETE FROM {table} WHERE is_sample = 1").rowcount
        conn.execute("DELETE FROM publish_requests WHERE (target_type = 'lesson' AND target_id NOT IN "
                     "(SELECT id FROM lessons)) OR (target_type = 'quiz' AND target_id NOT IN (SELECT id FROM quizzes))")
        conn.commit()
    finally:
        conn.close()
    with app.app_context():
        for path in files:
            storage.delete_file(path)
    summary["files"] = len(files)
    return summary
