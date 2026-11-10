"""Role dashboards."""
from flask import current_app, g, render_template, request

from ..db import get_setting, query
from ..learning import completed_lesson_ids, visible_courses_sql
from ..network import lan_addresses
from ..permissions import class_ids, manageable_courses_clause


def admin_dashboard():
    counts = {r["role"]: r["n"] for r in query("SELECT role, COUNT(*) AS n FROM users WHERE active = 1 GROUP BY role")}
    stats = {
        "students": counts.get("student", 0),
        "teachers": counts.get("teacher", 0),
        "admins": counts.get("admin", 0),
        "classes": query("SELECT COUNT(*) AS n FROM classes", one=True)["n"],
        "courses": query("SELECT COUNT(*) AS n FROM courses", one=True)["n"],
        "lessons": query("SELECT COUNT(*) AS n FROM lessons WHERE status = 'published'", one=True)["n"],
        "quizzes": query("SELECT COUNT(*) AS n FROM quizzes WHERE status = 'published'", one=True)["n"],
        "pending": query("SELECT COUNT(*) AS n FROM publish_requests WHERE status = 'pending'", one=True)["n"],
        "attempts": query("SELECT COUNT(*) AS n FROM attempts WHERE finished_at IS NOT NULL", one=True)["n"],
        "sample": query("SELECT COUNT(*) AS n FROM subjects WHERE is_sample = 1", one=True)["n"],
    }
    used = query("SELECT COALESCE(SUM(size), 0) AS n FROM resources", one=True)["n"]
    free = _free_space()
    port = request.host.rsplit(":", 1)[1] if ":" in request.host else str(current_app.config["PORT"])
    addresses = [f"http://{ip}:{port}" for ip in lan_addresses()]
    recent = query("SELECT a.*, u.name AS user_name FROM audit_log a LEFT JOIN users u ON u.id = a.user_id "
                   "ORDER BY a.id DESC LIMIT 8")
    return render_template("main/admin_dashboard.html", stats=stats, used=used, free=free, addresses=addresses,
                           current_url=request.host_url.rstrip("/"), recent=recent,
                           publish_mode=get_setting("publish_mode"))


def _free_space():
    import shutil
    try:
        return shutil.disk_usage(current_app.config["STORAGE_DIR"]).free
    except OSError:
        return 0


def teacher_dashboard():
    from ..progress import class_summary
    ids = class_ids(g.user["id"], "teacher")
    classes = []
    if ids:
        for c in query(f"SELECT * FROM classes WHERE id IN ({','.join('?' * len(ids))}) ORDER BY name", list(ids)):
            classes.append((c, class_summary(c["id"])))
    where, args = manageable_courses_clause(g.user)
    courses = query(
        "SELECT c.*, s.name AS subject_name, "
        "(SELECT COUNT(*) FROM lessons l WHERE l.course_id = c.id) AS lesson_count, "
        "(SELECT COUNT(*) FROM lessons l WHERE l.course_id = c.id AND l.status = 'draft') AS drafts, "
        "(SELECT COUNT(*) FROM lessons l WHERE l.course_id = c.id AND l.status = 'pending') "
        " + (SELECT COUNT(*) FROM quizzes z WHERE z.course_id = c.id AND z.status = 'pending') AS pending "
        f"FROM courses c JOIN subjects s ON s.id = c.subject_id WHERE {where} ORDER BY c.updated_at DESC LIMIT 12", args)
    recent = query(
        "SELECT a.*, u.name AS student_name, z.title AS quiz_title, z.pass_mark FROM attempts a "
        "JOIN users u ON u.id = a.user_id JOIN quizzes z ON z.id = a.quiz_id JOIN courses c ON c.id = z.course_id "
        f"WHERE a.finished_at IS NOT NULL AND {where} ORDER BY a.finished_at DESC LIMIT 10", args)
    returned = query(
        "SELECT r.*, l.title AS lesson_title, l.id AS lesson_id FROM publish_requests r JOIN lessons l "
        "ON r.target_type = 'lesson' AND l.id = r.target_id WHERE r.requested_by = ? AND r.status = 'rejected' "
        "AND l.status = 'draft' ORDER BY r.reviewed_at DESC LIMIT 5", (g.user["id"],))
    return render_template("main/teacher_dashboard.html", classes=classes, courses=courses, recent=recent,
                           returned=returned, publish_mode=get_setting("publish_mode"))


def student_dashboard():
    sql, args = visible_courses_sql(g.user)
    courses = query(sql + " ORDER BY s.name, c.title", args)
    done_all = completed_lesson_ids(g.user["id"])
    course_rows = []
    for c in courses:
        published = {r["id"] for r in query("SELECT id FROM lessons WHERE course_id = ? AND status = 'published'", (c["id"],))}
        course_rows.append({"course": c, "done": len(published & done_all), "total": len(published)})
    visible_ids = [c["id"] for c in courses]
    continue_lesson = None
    new_lessons = []
    if visible_ids:
        marks = ",".join("?" * len(visible_ids))
        continue_lesson = query(
            "SELECT l.*, c.title AS course_title FROM progress p JOIN lessons l ON l.id = p.lesson_id "
            f"JOIN courses c ON c.id = l.course_id WHERE p.user_id = ? AND p.completed_at IS NULL "
            f"AND l.status = 'published' AND l.course_id IN ({marks}) ORDER BY p.last_viewed_at DESC LIMIT 1",
            [g.user["id"]] + visible_ids, one=True)
        new_lessons = query(
            "SELECT l.id, l.title, l.published_at, c.title AS course_title FROM lessons l JOIN courses c ON c.id = l.course_id "
            f"WHERE l.status = 'published' AND l.course_id IN ({marks}) AND l.id NOT IN "
            "(SELECT lesson_id FROM progress WHERE user_id = ?) ORDER BY l.published_at DESC LIMIT 5",
            visible_ids + [g.user["id"]])
    results = query(
        "SELECT a.*, z.title AS quiz_title, z.pass_mark FROM attempts a JOIN quizzes z ON z.id = a.quiz_id "
        "WHERE a.user_id = ? AND a.finished_at IS NOT NULL ORDER BY a.finished_at DESC LIMIT 5", (g.user["id"],))
    return render_template("main/student_dashboard.html", course_rows=course_rows, continue_lesson=continue_lesson,
                           new_lessons=new_lessons, results=results, done_count=len(done_all))
