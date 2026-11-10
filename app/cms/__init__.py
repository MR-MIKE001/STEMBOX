"""Content management: subjects, courses, topics, lessons, files, search,
publishing and the administrator approval queue."""
from pathlib import Path

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import publishing, storage
from ..auth import roles_required
from ..db import audit, execute, get_db, get_setting, new_uuid, now, query
from ..permissions import (can_manage_course, can_publish_directly, class_ids, manageable_courses_clause)
from ..utils import to_int

bp = Blueprint("cms", __name__, url_prefix="/content")

STAFF = ("admin", "teacher")


# ---------------------------------------------------------------- loaders

def get_course_or_404(course_id, manage=True):
    course = query("SELECT c.*, s.name AS subject_name, cl.name AS class_name FROM courses c "
                   "JOIN subjects s ON s.id = c.subject_id LEFT JOIN classes cl ON cl.id = c.class_id "
                   "WHERE c.id = ?", (course_id,), one=True)
    if course is None:
        abort(404)
    if manage and not can_manage_course(g.user, course):
        abort(403)
    return course


def get_lesson_or_404(lesson_id):
    lesson = query("SELECT * FROM lessons WHERE id = ?", (lesson_id,), one=True)
    if lesson is None:
        abort(404)
    course = get_course_or_404(lesson["course_id"])
    return lesson, course


def get_resource_or_404(resource_id):
    resource = query("SELECT * FROM resources WHERE id = ?", (resource_id,), one=True)
    if resource is None:
        abort(404)
    lesson, course = get_lesson_or_404(resource["lesson_id"])
    return resource, lesson, course


def _touch(table, row_id):
    execute(f"UPDATE {table} SET updated_at = ? WHERE id = ?", (now(), row_id))


def _after_change(lesson, course):
    """Re-submit a published lesson for approval if the editor may not publish directly."""
    fresh = query("SELECT * FROM lessons WHERE id = ?", (lesson["id"],), one=True)
    if publishing.content_changed("lesson", fresh, course, g.user):
        flash("Because this lesson was already published, your changes now wait for an administrator's "
              "approval. Students will not see the lesson until it is approved.", "warning")


def _class_choices():
    if g.user["role"] == "admin":
        return query("SELECT * FROM classes ORDER BY name")
    ids = class_ids(g.user["id"], "teacher")
    if not ids:
        return []
    return query(f"SELECT * FROM classes WHERE id IN ({','.join('?' * len(ids))}) ORDER BY name", list(ids))


# ---------------------------------------------------------------- content home & search

@bp.route("/")
@roles_required(*STAFF)
def content_home():
    f = {
        "q": request.args.get("q", "").strip(),
        "subject": to_int(request.args.get("subject")),
        "class": request.args.get("class", ""),
        "level": request.args.get("level", ""),
        "topic": request.args.get("topic", "").strip(),
        "status": request.args.get("status", ""),
    }
    where, args = manageable_courses_clause(g.user)
    course_sql = ("SELECT c.*, s.name AS subject_name, cl.name AS class_name, "
                  "(SELECT COUNT(*) FROM lessons l WHERE l.course_id = c.id) AS lesson_count, "
                  "(SELECT COUNT(*) FROM lessons l WHERE l.course_id = c.id AND l.status = 'published') AS published_count, "
                  "(SELECT COUNT(*) FROM quizzes z WHERE z.course_id = c.id) AS quiz_count "
                  "FROM courses c JOIN subjects s ON s.id = c.subject_id LEFT JOIN classes cl ON cl.id = c.class_id "
                  f"WHERE {where}")
    course_args = list(args)
    filters_sql = ""
    filter_args = []
    if f["subject"]:
        filters_sql += " AND c.subject_id = ?"
        filter_args.append(f["subject"])
    if f["class"] == "none":
        filters_sql += " AND c.class_id IS NULL"
    elif to_int(f["class"]):
        filters_sql += " AND c.class_id = ?"
        filter_args.append(to_int(f["class"]))
    if f["level"]:
        filters_sql += " AND c.level = ?"
        filter_args.append(f["level"])

    courses = query(course_sql + filters_sql + (" AND (c.title LIKE ? OR c.description LIKE ?)" if f["q"] else "")
                    + " ORDER BY s.name, c.level, c.title",
                    course_args + filter_args + ([f"%{f['q']}%"] * 2 if f["q"] else []))

    lessons = []
    searching = bool(f["q"] or f["topic"] or f["status"])
    if searching:
        lesson_sql = ("SELECT l.*, c.title AS course_title, s.name AS subject_name, t.title AS topic_title "
                      "FROM lessons l JOIN courses c ON c.id = l.course_id JOIN subjects s ON s.id = c.subject_id "
                      f"LEFT JOIN topics t ON t.id = l.topic_id WHERE {where}" + filters_sql)
        lesson_args = list(args) + filter_args
        if f["q"]:
            lesson_sql += (" AND (l.title LIKE ? OR l.body LIKE ? OR EXISTS (SELECT 1 FROM resources r "
                           "WHERE r.lesson_id = l.id AND r.title LIKE ?))")
            lesson_args += [f"%{f['q']}%"] * 3
        if f["topic"]:
            lesson_sql += " AND t.title LIKE ?"
            lesson_args.append(f"%{f['topic']}%")
        if f["status"] in ("draft", "pending", "published"):
            lesson_sql += " AND l.status = ?"
            lesson_args.append(f["status"])
        lessons = query(lesson_sql + " ORDER BY l.updated_at DESC LIMIT 200", lesson_args)

    subjects = query("SELECT * FROM subjects ORDER BY name")
    levels = [r["level"] for r in query("SELECT DISTINCT level FROM courses WHERE level != '' ORDER BY level")]
    classes = query("SELECT * FROM classes ORDER BY name")
    return render_template("cms/home.html", courses=courses, lessons=lessons, searching=searching, f=f,
                           subjects=subjects, levels=levels, classes=classes,
                           publish_mode=get_setting("publish_mode"))


# ---------------------------------------------------------------- subjects

def _can_edit_subject(subject):
    return g.user["role"] == "admin" or subject["created_by"] == g.user["id"]


@bp.route("/subjects")
@roles_required(*STAFF)
def subject_list():
    subjects = query("SELECT s.*, (SELECT COUNT(*) FROM courses c WHERE c.subject_id = s.id) AS course_count "
                     "FROM subjects s ORDER BY s.name")
    return render_template("cms/subject_list.html", subjects=subjects, can_edit=_can_edit_subject)


@bp.route("/subjects/new", methods=["GET", "POST"])
@roles_required(*STAFF)
def subject_new():
    return _subject_form(None)


@bp.route("/subjects/<int:subject_id>/edit", methods=["GET", "POST"])
@roles_required(*STAFF)
def subject_edit(subject_id):
    subject = query("SELECT * FROM subjects WHERE id = ?", (subject_id,), one=True)
    if subject is None:
        abort(404)
    if not _can_edit_subject(subject):
        abort(403)
    return _subject_form(subject)


def _subject_form(subject):
    form = dict(subject) if subject else {"name": "", "description": ""}
    errors = []
    if request.method == "POST":
        form = {"name": request.form.get("name", "").strip(), "description": request.form.get("description", "").strip()}
        if not form["name"]:
            errors.append("Please type the subject name, for example “Mathematics”.")
        dup = query("SELECT id FROM subjects WHERE name = ? COLLATE NOCASE", (form["name"],), one=True)
        if dup and (subject is None or dup["id"] != subject["id"]):
            errors.append("A subject with this name already exists.")
        if not errors:
            if subject:
                execute("UPDATE subjects SET name = ?, description = ? WHERE id = ?",
                        (form["name"], form["description"], subject["id"]))
                flash("Subject saved.", "success")
            else:
                execute("INSERT INTO subjects (uuid, name, description, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                        (new_uuid(), form["name"], form["description"], g.user["id"], now()))
                flash(f"Subject “{form['name']}” created. Now add a course to it.", "success")
            nxt = request.args.get("next")
            return redirect(url_for("cms.course_new") if nxt == "course" else url_for("cms.subject_list"))
    return render_template("cms/subject_form.html", form=form, errors=errors, subject=subject)


@bp.route("/subjects/<int:subject_id>/delete", methods=["POST"])
@roles_required(*STAFF)
def subject_delete(subject_id):
    subject = query("SELECT * FROM subjects WHERE id = ?", (subject_id,), one=True)
    if subject is None:
        abort(404)
    if not _can_edit_subject(subject):
        abort(403)
    if query("SELECT 1 FROM courses WHERE subject_id = ?", (subject_id,), one=True):
        flash("This subject still has courses. Delete or move its courses first.", "error")
    else:
        execute("DELETE FROM subjects WHERE id = ?", (subject_id,))
        audit("subject.delete", f"subject:{subject_id}", subject["name"])
        flash("Subject deleted.", "success")
    return redirect(url_for("cms.subject_list"))


# ---------------------------------------------------------------- courses

def _read_course_form():
    form = {
        "subject_id": to_int(request.form.get("subject_id")),
        "title": request.form.get("title", "").strip(),
        "level": request.form.get("level", "").strip(),
        "description": request.form.get("description", "").strip(),
        "class_id": to_int(request.form.get("class_id")),
    }
    errors = []
    if not form["title"]:
        errors.append("Please type the course title.")
    if not form["subject_id"] or not query("SELECT 1 FROM subjects WHERE id = ?", (form["subject_id"],), one=True):
        errors.append("Please choose a subject.")
    allowed = {c["id"] for c in _class_choices()}
    if form["class_id"] is not None and form["class_id"] not in allowed:
        errors.append("You can only choose a class that you teach.")
    return form, errors


@bp.route("/courses/new", methods=["GET", "POST"])
@roles_required(*STAFF)
def course_new():
    subjects = query("SELECT * FROM subjects ORDER BY name")
    form = {"subject_id": to_int(request.args.get("subject")), "title": "", "level": "", "description": "", "class_id": None}
    errors = []
    if request.method == "POST":
        form, errors = _read_course_form()
        if not errors:
            stamp = now()
            course_id = execute(
                "INSERT INTO courses (uuid, subject_id, class_id, title, level, description, author_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (new_uuid(), form["subject_id"], form["class_id"], form["title"], form["level"], form["description"],
                 g.user["id"], stamp, stamp))
            flash("Course created. Now add its first lesson.", "success")
            return redirect(url_for("cms.course_detail", course_id=course_id))
    return render_template("cms/course_form.html", form=form, errors=errors, course=None, subjects=subjects,
                           classes=_class_choices(), levels=_levels())


def _levels():
    return [r["level"] for r in query("SELECT DISTINCT level FROM courses WHERE level != '' "
                                      "UNION SELECT DISTINCT level FROM classes WHERE level != '' ORDER BY 1")]


@bp.route("/courses/<int:course_id>/edit", methods=["GET", "POST"])
@roles_required(*STAFF)
def course_edit(course_id):
    course = get_course_or_404(course_id)
    subjects = query("SELECT * FROM subjects ORDER BY name")
    form = dict(course)
    errors = []
    classes = _class_choices()
    if course["class_id"] and course["class_id"] not in {c["id"] for c in classes}:
        classes = list(classes) + [query("SELECT * FROM classes WHERE id = ?", (course["class_id"],), one=True)]
    if request.method == "POST":
        form, errors = _read_course_form()
        if form["class_id"] == course["class_id"] and "You can only choose a class that you teach." in errors:
            errors.remove("You can only choose a class that you teach.")  # unchanged class is fine
        if not errors:
            execute("UPDATE courses SET subject_id = ?, class_id = ?, title = ?, level = ?, description = ?, updated_at = ? "
                    "WHERE id = ?", (form["subject_id"], form["class_id"], form["title"], form["level"],
                                     form["description"], now(), course_id))
            if form["class_id"] != course["class_id"]:
                # Moving content to a wider audience may need approval again (mixed mode).
                new_course = get_course_or_404(course_id, manage=False)
                resubmitted = 0
                for kind, table in (("lesson", "lessons"), ("quiz", "quizzes")):
                    for row in query(f"SELECT * FROM {table} WHERE course_id = ? AND status = 'published'", (course_id,)):
                        resubmitted += publishing.content_changed(kind, row, new_course, g.user)
                if resubmitted:
                    flash(f"{resubmitted} published item(s) now need an administrator's approval for the new audience.",
                          "warning")
            flash("Course saved.", "success")
            return redirect(url_for("cms.course_detail", course_id=course_id))
    return render_template("cms/course_form.html", form=form, errors=errors, course=course, subjects=subjects,
                           classes=classes, levels=_levels())


@bp.route("/courses/<int:course_id>")
@roles_required(*STAFF)
def course_detail(course_id):
    course = get_course_or_404(course_id)
    topics = query("SELECT * FROM topics WHERE course_id = ? ORDER BY position, id", (course_id,))
    lessons = query("SELECT l.*, (SELECT COUNT(*) FROM resources r WHERE r.lesson_id = l.id) AS resource_count "
                    "FROM lessons l WHERE l.course_id = ? ORDER BY l.position, l.id", (course_id,))
    quizzes = query("SELECT z.*, (SELECT COUNT(*) FROM questions q WHERE q.quiz_id = z.id) AS question_count "
                    "FROM quizzes z WHERE z.course_id = ? ORDER BY z.id", (course_id,))
    by_topic = {t["id"]: [] for t in topics}
    loose = []
    for lesson in lessons:
        (by_topic[lesson["topic_id"]] if lesson["topic_id"] in by_topic else loose).append(lesson)
    return render_template("cms/course_detail.html", course=course, topics=topics, by_topic=by_topic, loose=loose,
                           quizzes=quizzes, direct=can_publish_directly(g.user, course))


@bp.route("/courses/<int:course_id>/delete", methods=["POST"])
@roles_required(*STAFF)
def course_delete(course_id):
    course = get_course_or_404(course_id)
    paths = [r["file_path"] for r in query("SELECT r.file_path FROM resources r JOIN lessons l ON l.id = r.lesson_id "
                                           "WHERE l.course_id = ?", (course_id,))]
    lesson_ids = [r["id"] for r in query("SELECT id FROM lessons WHERE course_id = ?", (course_id,))]
    quiz_ids = [r["id"] for r in query("SELECT id FROM quizzes WHERE course_id = ?", (course_id,))]
    execute("DELETE FROM courses WHERE id = ?", (course_id,))
    for lid in lesson_ids:
        publishing.delete_requests("lesson", lid)
    for qid in quiz_ids:
        publishing.delete_requests("quiz", qid)
    for path in paths:
        storage.delete_file(path)
    audit("course.delete", f"course:{course_id}", course["title"])
    flash(f"Course “{course['title']}” and everything in it was deleted.", "success")
    return redirect(url_for("cms.content_home"))


# ---------------------------------------------------------------- topics

@bp.route("/courses/<int:course_id>/topics", methods=["POST"])
@roles_required(*STAFF)
def topic_add(course_id):
    get_course_or_404(course_id)
    title = request.form.get("title", "").strip()
    if not title:
        flash("Please type a topic name.", "error")
    else:
        pos = query("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM topics WHERE course_id = ?", (course_id,), one=True)["p"]
        execute("INSERT INTO topics (uuid, course_id, title, position) VALUES (?, ?, ?, ?)",
                (new_uuid(), course_id, title[:200], pos))
        flash(f"Topic “{title}” added.", "success")
    return redirect(url_for("cms.course_detail", course_id=course_id))


def _get_topic(topic_id):
    topic = query("SELECT * FROM topics WHERE id = ?", (topic_id,), one=True)
    if topic is None:
        abort(404)
    get_course_or_404(topic["course_id"])
    return topic


@bp.route("/topics/<int:topic_id>/rename", methods=["POST"])
@roles_required(*STAFF)
def topic_rename(topic_id):
    topic = _get_topic(topic_id)
    title = request.form.get("title", "").strip()
    if title:
        execute("UPDATE topics SET title = ? WHERE id = ?", (title[:200], topic_id))
        flash("Topic renamed.", "success")
    return redirect(url_for("cms.course_detail", course_id=topic["course_id"]))


@bp.route("/topics/<int:topic_id>/move/<direction>", methods=["POST"])
@roles_required(*STAFF)
def topic_move(topic_id, direction):
    topic = _get_topic(topic_id)
    _move("topics", topic, "course_id = ?", (topic["course_id"],), direction)
    return redirect(url_for("cms.course_detail", course_id=topic["course_id"]))


@bp.route("/topics/<int:topic_id>/delete", methods=["POST"])
@roles_required(*STAFF)
def topic_delete(topic_id):
    topic = _get_topic(topic_id)
    execute("DELETE FROM topics WHERE id = ?", (topic_id,))
    flash("Topic removed. Its lessons were kept and are now listed under “Other lessons”.", "success")
    return redirect(url_for("cms.course_detail", course_id=topic["course_id"]))


def _move(table, row, scope_sql, scope_args, direction):
    """Swap an item with its neighbour (by position) inside the same scope."""
    siblings = query(f"SELECT id, position FROM {table} WHERE {scope_sql} ORDER BY position, id", scope_args)
    ids = [s["id"] for s in siblings]
    if row["id"] not in ids:
        return
    i = ids.index(row["id"])
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
    db = get_db()
    for pos, item_id in enumerate(ids):
        db.execute(f"UPDATE {table} SET position = ? WHERE id = ?", (pos, item_id))
    db.commit()


# ---------------------------------------------------------------- lessons

def _topic_choices(course_id):
    return query("SELECT * FROM topics WHERE course_id = ? ORDER BY position, id", (course_id,))


def _read_lesson_form(course_id):
    form = {
        "title": request.form.get("title", "").strip(),
        "body": request.form.get("body", "").replace("\r\n", "\n"),
        "topic_id": to_int(request.form.get("topic_id")),
        "new_topic": request.form.get("new_topic", "").strip(),
    }
    errors = []
    if not form["title"]:
        errors.append("Please type the lesson title.")
    if len(form["body"]) > 200_000:
        errors.append("The lesson text is too long.")
    if form["topic_id"] and not query("SELECT 1 FROM topics WHERE id = ? AND course_id = ?",
                                      (form["topic_id"], course_id), one=True):
        errors.append("Please choose a topic from the list.")
    return form, errors


def _resolve_topic(course_id, form):
    if form["new_topic"]:
        pos = query("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM topics WHERE course_id = ?", (course_id,), one=True)["p"]
        return execute("INSERT INTO topics (uuid, course_id, title, position) VALUES (?, ?, ?, ?)",
                       (new_uuid(), course_id, form["new_topic"][:200], pos))
    return form["topic_id"]


@bp.route("/courses/<int:course_id>/lessons/new", methods=["GET", "POST"])
@roles_required(*STAFF)
def lesson_new(course_id):
    course = get_course_or_404(course_id)
    form = {"title": "", "body": "", "topic_id": to_int(request.args.get("topic")), "new_topic": ""}
    errors = []
    if request.method == "POST":
        form, errors = _read_lesson_form(course_id)
        upload = request.files.get("file")
        rtype = None
        if upload and upload.filename:
            rtype = storage.guess_type_from_name(upload.filename)
            if rtype is None:
                errors.append("The attached file is not a PDF, video, picture or audio file STEMBox can show.")
        if not errors:
            info = None
            if rtype:
                try:
                    info = storage.store_upload(upload, rtype)
                except storage.UploadError as exc:
                    errors.append(str(exc))
        if not errors:
            topic_id = _resolve_topic(course_id, form)
            pos = query("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM lessons WHERE course_id = ?",
                        (course_id,), one=True)["p"]
            stamp = now()
            lesson_id = execute(
                "INSERT INTO lessons (uuid, course_id, topic_id, title, body, status, author_id, position, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?)",
                (new_uuid(), course_id, topic_id, form["title"], form["body"], g.user["id"], pos, stamp, stamp))
            if info:
                _insert_resource(lesson_id, rtype, Path(info["original_name"]).stem, info)
            flash("Lesson saved as a draft. Add files if you need them, then press Publish.", "success")
            return redirect(url_for("cms.lesson_detail", lesson_id=lesson_id))
    return render_template("cms/lesson_form.html", form=form, errors=errors, course=course, lesson=None,
                           topics=_topic_choices(course_id), accept=storage.accept_attr(None))


@bp.route("/lessons/<int:lesson_id>")
@roles_required(*STAFF)
def lesson_detail(lesson_id):
    lesson, course = get_lesson_or_404(lesson_id)
    resources = query("SELECT * FROM resources WHERE lesson_id = ? ORDER BY position, id", (lesson_id,))
    topic = query("SELECT * FROM topics WHERE id = ?", (lesson["topic_id"],), one=True) if lesson["topic_id"] else None
    return render_template("cms/lesson_detail.html", lesson=lesson, course=course, resources=resources, topic=topic,
                           direct=can_publish_directly(g.user, course),
                           review=publishing.latest_review("lesson", lesson_id),
                           types=storage.RESOURCE_TYPES, accept=storage.accept_attr(None),
                           limits=_limits_mb())


def _limits_mb():
    from flask import current_app
    return {k: v // (1024 * 1024) for k, v in current_app.config["UPLOAD_LIMITS"].items()}


@bp.route("/lessons/<int:lesson_id>/edit", methods=["GET", "POST"])
@roles_required(*STAFF)
def lesson_edit(lesson_id):
    lesson, course = get_lesson_or_404(lesson_id)
    form = {**dict(lesson), "new_topic": ""}
    errors = []
    if request.method == "POST":
        form, errors = _read_lesson_form(course["id"])
        if not errors:
            topic_id = _resolve_topic(course["id"], form)
            execute("UPDATE lessons SET title = ?, body = ?, topic_id = ?, updated_at = ? WHERE id = ?",
                    (form["title"], form["body"], topic_id, now(), lesson_id))
            flash("Lesson saved.", "success")
            _after_change(lesson, course)
            return redirect(url_for("cms.lesson_detail", lesson_id=lesson_id))
    return render_template("cms/lesson_form.html", form=form, errors=errors, course=course, lesson=lesson,
                           topics=_topic_choices(course["id"]), accept=None)


@bp.route("/lessons/<int:lesson_id>/publish", methods=["POST"])
@roles_required(*STAFF)
def lesson_publish(lesson_id):
    lesson, course = get_lesson_or_404(lesson_id)
    if not lesson["body"].strip() and not query("SELECT 1 FROM resources WHERE lesson_id = ?", (lesson_id,), one=True):
        flash("This lesson is empty. Write some text or add a file before publishing.", "error")
        return redirect(url_for("cms.lesson_detail", lesson_id=lesson_id))
    result = publishing.publish("lesson", lesson, course, g.user)
    if result == "published":
        flash("Published. Students can now see this lesson.", "success")
    else:
        flash("Sent to the administrator for approval. Students will see it once it is approved.", "info")
    return redirect(url_for("cms.lesson_detail", lesson_id=lesson_id))


@bp.route("/lessons/<int:lesson_id>/unpublish", methods=["POST"])
@roles_required(*STAFF)
def lesson_unpublish(lesson_id):
    lesson, _course = get_lesson_or_404(lesson_id)
    publishing.unpublish("lesson", lesson, g.user)
    flash("The lesson is now a draft and hidden from students.", "success")
    return redirect(url_for("cms.lesson_detail", lesson_id=lesson_id))


@bp.route("/lessons/<int:lesson_id>/move/<direction>", methods=["POST"])
@roles_required(*STAFF)
def lesson_move(lesson_id, direction):
    lesson, course = get_lesson_or_404(lesson_id)
    _move("lessons", lesson, "course_id = ? AND topic_id IS ?", (course["id"], lesson["topic_id"]), direction)
    return redirect(url_for("cms.course_detail", course_id=course["id"]))


@bp.route("/lessons/<int:lesson_id>/delete", methods=["POST"])
@roles_required(*STAFF)
def lesson_delete(lesson_id):
    lesson, course = get_lesson_or_404(lesson_id)
    paths = [r["file_path"] for r in query("SELECT file_path FROM resources WHERE lesson_id = ?", (lesson_id,))]
    execute("DELETE FROM lessons WHERE id = ?", (lesson_id,))
    publishing.delete_requests("lesson", lesson_id)
    for path in paths:
        storage.delete_file(path)
    audit("lesson.delete", f"lesson:{lesson_id}", lesson["title"])
    flash(f"Lesson “{lesson['title']}” deleted.", "success")
    return redirect(url_for("cms.course_detail", course_id=course["id"]))


# ---------------------------------------------------------------- resources (files)

def _insert_resource(lesson_id, rtype, title, info):
    pos = query("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM resources WHERE lesson_id = ?", (lesson_id,), one=True)["p"]
    stamp = now()
    return execute(
        "INSERT INTO resources (uuid, lesson_id, type, title, file_path, original_name, mime_type, size, checksum, "
        "position, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (new_uuid(), lesson_id, rtype, (title or "File")[:200], info["file_path"], info["original_name"],
         info["mime_type"], info["size"], info["checksum"], pos, stamp, stamp))


@bp.route("/lessons/<int:lesson_id>/resources", methods=["POST"])
@roles_required(*STAFF)
def resource_upload(lesson_id):
    lesson, course = get_lesson_or_404(lesson_id)
    upload = request.files.get("file")
    rtype = request.form.get("type", "auto")
    if rtype == "auto":
        rtype = storage.guess_type_from_name(upload.filename if upload else "")
        if rtype is None:
            flash("STEMBox cannot show this kind of file. Use PDF, MP4/WebM video, "
                  "PNG/JPG/GIF/WebP pictures or MP3/OGG/WAV/M4A audio.", "error")
            return redirect(url_for("cms.lesson_detail", lesson_id=lesson_id))
    try:
        info = storage.store_upload(upload, rtype)
    except storage.UploadError as exc:
        flash(str(exc), "error")
        return redirect(url_for("cms.lesson_detail", lesson_id=lesson_id))
    title = request.form.get("title", "").strip() or Path(info["original_name"]).stem
    _insert_resource(lesson_id, rtype, title, info)
    _touch("lessons", lesson_id)
    flash(f"“{title}” uploaded.", "success")
    _after_change(lesson, course)
    return redirect(url_for("cms.lesson_detail", lesson_id=lesson_id))


@bp.route("/resources/<int:resource_id>/edit", methods=["GET", "POST"])
@roles_required(*STAFF)
def resource_edit(resource_id):
    resource, lesson, course = get_resource_or_404(resource_id)
    errors = []
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        if not title:
            errors.append("Please type a title.")
        upload = request.files.get("file")
        info = None
        if not errors and upload and upload.filename:
            try:
                info = storage.store_upload(upload, resource["type"])
            except storage.UploadError as exc:
                errors.append(str(exc))
        if not errors:
            if info:
                execute("UPDATE resources SET title = ?, file_path = ?, original_name = ?, mime_type = ?, size = ?, "
                        "checksum = ?, updated_at = ? WHERE id = ?",
                        (title, info["file_path"], info["original_name"], info["mime_type"], info["size"],
                         info["checksum"], now(), resource_id))
                storage.delete_file(resource["file_path"])
                flash("File replaced.", "success")
            else:
                execute("UPDATE resources SET title = ?, updated_at = ? WHERE id = ?", (title, now(), resource_id))
                flash("Saved.", "success")
            _touch("lessons", lesson["id"])
            _after_change(lesson, course)
            return redirect(url_for("cms.lesson_detail", lesson_id=lesson["id"]))
    return render_template("cms/resource_form.html", resource=resource, lesson=lesson, course=course, errors=errors,
                           accept=storage.accept_attr(resource["type"]), limits=_limits_mb())


@bp.route("/resources/<int:resource_id>/move/<direction>", methods=["POST"])
@roles_required(*STAFF)
def resource_move(resource_id, direction):
    resource, lesson, _course = get_resource_or_404(resource_id)
    _move("resources", resource, "lesson_id = ?", (lesson["id"],), direction)
    return redirect(url_for("cms.lesson_detail", lesson_id=lesson["id"]))


@bp.route("/resources/<int:resource_id>/delete", methods=["POST"])
@roles_required(*STAFF)
def resource_delete(resource_id):
    resource, lesson, course = get_resource_or_404(resource_id)
    execute("DELETE FROM resources WHERE id = ?", (resource_id,))
    storage.delete_file(resource["file_path"])
    _touch("lessons", lesson["id"])
    flash(f"“{resource['title']}” removed.", "success")
    _after_change(lesson, course)
    return redirect(url_for("cms.lesson_detail", lesson_id=lesson["id"]))


# ---------------------------------------------------------------- approvals (admin)

def _request_rows(status_sql, limit):
    rows = query(
        "SELECT r.*, u.name AS requester, rv.name AS reviewer, "
        "CASE r.target_type WHEN 'lesson' THEN l.title ELSE z.title END AS title, "
        "c.title AS course_title, c.id AS course_id, cl.name AS class_name "
        "FROM publish_requests r LEFT JOIN users u ON u.id = r.requested_by "
        "LEFT JOIN users rv ON rv.id = r.reviewed_by "
        "LEFT JOIN lessons l ON r.target_type = 'lesson' AND l.id = r.target_id "
        "LEFT JOIN quizzes z ON r.target_type = 'quiz' AND z.id = r.target_id "
        "LEFT JOIN courses c ON c.id = COALESCE(l.course_id, z.course_id) "
        "LEFT JOIN classes cl ON cl.id = c.class_id "
        f"WHERE {status_sql} ORDER BY r.id {'ASC' if 'pending' in status_sql else 'DESC'} LIMIT {int(limit)}")
    return [r for r in rows if r["title"] is not None]


@bp.route("/approvals")
@roles_required("admin")
def approvals():
    return render_template("cms/approvals.html", pending=_request_rows("r.status = 'pending'", 500),
                           history=_request_rows("r.status IN ('approved', 'rejected')", 20),
                           publish_mode=get_setting("publish_mode"))


def _get_request(request_id):
    row = query("SELECT * FROM publish_requests WHERE id = ?", (request_id,), one=True)
    if row is None:
        abort(404)
    if row["status"] != "pending":
        flash("This request was already handled.", "info")
        return None
    return row


@bp.route("/approvals/<int:request_id>/approve", methods=["POST"])
@roles_required("admin")
def approval_approve(request_id):
    row = _get_request(request_id)
    if row:
        publishing.approve(row, g.user)
        flash("Approved and published.", "success")
    return redirect(url_for("cms.approvals"))


@bp.route("/approvals/<int:request_id>/reject", methods=["POST"])
@roles_required("admin")
def approval_reject(request_id):
    row = _get_request(request_id)
    if row:
        publishing.reject(row, g.user, request.form.get("note", "").strip()[:500])
        flash("Sent back to the teacher as a draft.", "success")
    return redirect(url_for("cms.approvals"))
