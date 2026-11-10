"""Student learning area: browse subjects and courses, read lessons, view files,
mark lessons complete. Teachers and administrators can use it to preview."""
import json
from pathlib import Path

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, send_file, url_for

from .. import storage
from ..auth import login_required
from ..db import execute, now, query
from ..permissions import can_manage_course, can_view_course, can_view_lesson, student_courses_clause
from ..utils import to_int

bp = Blueprint("learning", __name__)


# ---------------------------------------------------------------- visibility helpers

def visible_courses_sql(user):
    """SQL (and args) for the courses this user can open in the learning area,
    with counts of published lessons."""
    base = ("SELECT c.*, s.name AS subject_name, cl.name AS class_name, "
            "(SELECT COUNT(*) FROM lessons l WHERE l.course_id = c.id AND l.status = 'published') AS lesson_count, "
            "(SELECT COUNT(*) FROM quizzes z WHERE z.course_id = c.id AND z.status = 'published') AS quiz_count "
            "FROM courses c JOIN subjects s ON s.id = c.subject_id LEFT JOIN classes cl ON cl.id = c.class_id WHERE ")
    has_content = ("(EXISTS (SELECT 1 FROM lessons l WHERE l.course_id = c.id AND l.status = 'published') OR "
                   "EXISTS (SELECT 1 FROM quizzes z WHERE z.course_id = c.id AND z.status = 'published'))")
    if user["role"] == "student":
        clause, args = student_courses_clause(user)
        return base + clause + " AND " + has_content, args
    return base + has_content, []


def completed_lesson_ids(user_id, course_id=None):
    sql = ("SELECT p.lesson_id FROM progress p JOIN lessons l ON l.id = p.lesson_id "
           "WHERE p.user_id = ? AND p.completed_at IS NOT NULL")
    args = [user_id]
    if course_id:
        sql += " AND l.course_id = ?"
        args.append(course_id)
    return {r["lesson_id"] for r in query(sql, args)}


def _course_or_404(course_id):
    course = query("SELECT c.*, s.name AS subject_name, cl.name AS class_name FROM courses c "
                   "JOIN subjects s ON s.id = c.subject_id LEFT JOIN classes cl ON cl.id = c.class_id WHERE c.id = ?",
                   (course_id,), one=True)
    if course is None:
        abort(404)
    if not can_view_course(g.user, course):
        abort(403)
    return course


def _lesson_or_404(lesson_id):
    lesson = query("SELECT * FROM lessons WHERE id = ?", (lesson_id,), one=True)
    if lesson is None:
        abort(404)
    course = _course_or_404(lesson["course_id"])
    if not can_view_lesson(g.user, lesson, course):
        abort(404 if g.user["role"] == "student" else 403)
    return lesson, course


def _resource_or_404(resource_id=None, uuid=None):
    if uuid is not None:
        resource = query("SELECT * FROM resources WHERE uuid = ?", (uuid,), one=True)
    else:
        resource = query("SELECT * FROM resources WHERE id = ?", (resource_id,), one=True)
    if resource is None:
        abort(404)
    lesson, course = _lesson_or_404(resource["lesson_id"])
    return resource, lesson, course


def _lessons_in_order(course, include_unpublished=False):
    sql = ("SELECT l.*, t.title AS topic_title, t.position AS topic_position, "
           "(SELECT GROUP_CONCAT(DISTINCT r.type) FROM resources r WHERE r.lesson_id = l.id) AS kinds "
           "FROM lessons l LEFT JOIN topics t ON t.id = l.topic_id WHERE l.course_id = ?")
    if not include_unpublished:
        sql += " AND l.status = 'published'"
    sql += " ORDER BY t.position IS NULL, t.position, t.id, l.position, l.id"
    return query(sql, (course["id"],))


def _is_student():
    return g.user["role"] == "student"


# ---------------------------------------------------------------- browsing

@bp.route("/learn")
@login_required
def subjects():
    sql, args = visible_courses_sql(g.user)
    courses = query(sql + " ORDER BY s.name, c.level, c.title", args)
    subjects = {}
    for c in courses:
        subjects.setdefault(c["subject_id"], {"id": c["subject_id"], "name": c["subject_name"], "courses": 0})
        subjects[c["subject_id"]]["courses"] += 1
    return render_template("learning/subjects.html", subjects=list(subjects.values()))


@bp.route("/learn/subject/<int:subject_id>")
@login_required
def subject(subject_id):
    subject = query("SELECT * FROM subjects WHERE id = ?", (subject_id,), one=True)
    if subject is None:
        abort(404)
    sql, args = visible_courses_sql(g.user)
    courses = query(sql + " AND c.subject_id = ? ORDER BY c.level, c.title", args + [subject_id])
    done = {}
    if _is_student():
        for c in courses:
            done[c["id"]] = len(completed_lesson_ids(g.user["id"], c["id"]))
    return render_template("learning/subject.html", subject=subject, courses=courses, done=done)


@bp.route("/learn/course/<int:course_id>")
@login_required
def course(course_id):
    course = _course_or_404(course_id)
    staff_view = can_manage_course(g.user, course)
    lessons = _lessons_in_order(course, include_unpublished=staff_view)
    groups = []
    for lesson in lessons:
        title = lesson["topic_title"] or "Other lessons"
        if not groups or groups[-1]["title"] != title:
            groups.append({"title": title, "lessons": []})
        groups[-1]["lessons"].append(lesson)
    quiz_sql = ("SELECT z.*, (SELECT COUNT(*) FROM questions q WHERE q.quiz_id = z.id) AS question_count "
                "FROM quizzes z WHERE z.course_id = ?")
    if not staff_view:
        quiz_sql += " AND z.status = 'published'"
    quizzes = query(quiz_sql + " ORDER BY z.id", (course_id,))
    done = completed_lesson_ids(g.user["id"], course_id) if _is_student() else set()
    best = {}
    if _is_student():
        for row in query("SELECT quiz_id, MAX(CAST(score AS REAL) / NULLIF(total, 0)) AS best FROM attempts "
                         "WHERE user_id = ? AND finished_at IS NOT NULL GROUP BY quiz_id", (g.user["id"],)):
            best[row["quiz_id"]] = round(100 * (row["best"] or 0))
    published_total = sum(1 for lesson in lessons if lesson["status"] == "published")
    return render_template("learning/course.html", course=course, groups=groups, quizzes=quizzes, done=done,
                           best=best, total=published_total, staff_view=staff_view)


@bp.route("/learn/search")
@login_required
def search():
    q = request.args.get("q", "").strip()
    results = []
    if q:
        sql, args = visible_courses_sql(g.user)
        course_ids = [c["id"] for c in query(sql, args)]
        if course_ids:
            marks = ",".join("?" * len(course_ids))
            like = f"%{q}%"
            results = query(
                "SELECT l.id, l.title, c.title AS course_title, s.name AS subject_name FROM lessons l "
                "JOIN courses c ON c.id = l.course_id JOIN subjects s ON s.id = c.subject_id "
                f"WHERE l.status = 'published' AND l.course_id IN ({marks}) AND (l.title LIKE ? OR l.body LIKE ? "
                "OR c.title LIKE ? OR EXISTS (SELECT 1 FROM resources r WHERE r.lesson_id = l.id AND r.title LIKE ?)) "
                "ORDER BY s.name, c.title, l.position LIMIT 100", course_ids + [like] * 4)
    return render_template("learning/search.html", q=q, results=results)


# ---------------------------------------------------------------- lessons

@bp.route("/learn/lesson/<int:lesson_id>")
@login_required
def lesson(lesson_id):
    lesson, course = _lesson_or_404(lesson_id)
    resources = query("SELECT * FROM resources WHERE lesson_id = ? ORDER BY position, id", (lesson_id,))
    ordered = [row["id"] for row in _lessons_in_order(course)]
    prev_id = next_id = None
    if lesson_id in ordered:
        i = ordered.index(lesson_id)
        prev_id = ordered[i - 1] if i > 0 else None
        next_id = ordered[i + 1] if i + 1 < len(ordered) else None
    progress = None
    positions = {}
    if _is_student():
        execute("INSERT INTO progress (user_id, lesson_id, last_viewed_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, lesson_id) DO UPDATE SET last_viewed_at = excluded.last_viewed_at",
                (g.user["id"], lesson_id, now()))
        progress = query("SELECT * FROM progress WHERE user_id = ? AND lesson_id = ?", (g.user["id"], lesson_id), one=True)
        positions = _positions(progress)
    quizzes = query("SELECT * FROM quizzes WHERE lesson_id = ? AND status = 'published' ORDER BY id", (lesson_id,))
    return render_template("learning/lesson.html", lesson=lesson, course=course, resources=resources,
                           prev_id=prev_id, next_id=next_id, progress=progress, positions=positions, quizzes=quizzes)


def _positions(progress):
    if not progress or not progress["last_position"]:
        return {}
    try:
        data = json.loads(progress["last_position"])
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


@bp.route("/learn/lesson/<int:lesson_id>/complete", methods=["POST"])
@login_required
def complete(lesson_id):
    lesson, _course = _lesson_or_404(lesson_id)
    if not _is_student():
        flash("Only students' progress is recorded.", "info")
        return redirect(url_for("learning.lesson", lesson_id=lesson_id))
    done = request.form.get("done", "1") == "1"
    stamp = now()
    execute("INSERT INTO progress (user_id, lesson_id, completed_at, last_viewed_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, lesson_id) DO UPDATE SET completed_at = excluded.completed_at, "
            "last_viewed_at = excluded.last_viewed_at",
            (g.user["id"], lesson_id, stamp if done else None, stamp))
    if done:
        flash(f"Well done! “{lesson['title']}” is marked as finished.", "success")
        nxt = to_int(request.form.get("next_id"))
        if nxt:
            return redirect(url_for("learning.lesson", lesson_id=nxt))
    return redirect(url_for("learning.lesson", lesson_id=lesson_id))


# ---------------------------------------------------------------- files

@bp.route("/learn/resource/<int:resource_id>")
@login_required
def view_resource(resource_id):
    resource, lesson, course = _resource_or_404(resource_id)
    start = 0
    if _is_student():
        progress = query("SELECT * FROM progress WHERE user_id = ? AND lesson_id = ?", (g.user["id"], lesson["id"]), one=True)
        start = _positions(progress).get(resource["uuid"], 0)
    return render_template("learning/viewer.html", r=resource, lesson=lesson, course=course, start=start)


@bp.route("/learn/resource/<int:resource_id>/position", methods=["POST"])
@login_required
def save_position(resource_id):
    resource, lesson, _course = _resource_or_404(resource_id)
    if not _is_student():
        return jsonify(ok=True, saved=False)
    seconds = max(0, min(to_int(request.form.get("position"), 0), 60 * 60 * 24))
    progress = query("SELECT * FROM progress WHERE user_id = ? AND lesson_id = ?", (g.user["id"], lesson["id"]), one=True)
    positions = _positions(progress)
    positions[resource["uuid"]] = seconds
    execute("INSERT INTO progress (user_id, lesson_id, last_position, last_viewed_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, lesson_id) DO UPDATE SET last_position = excluded.last_position, "
            "last_viewed_at = excluded.last_viewed_at",
            (g.user["id"], lesson["id"], json.dumps(positions), now()))
    return jsonify(ok=True, saved=True)


@bp.route("/media/<uuid>")
@login_required
def media(uuid):
    """Stream a stored file. Supports HTTP Range requests so videos can seek."""
    resource, _lesson, _course = _resource_or_404(uuid=uuid)
    try:
        path = storage.resolve(resource["file_path"])
    except storage.UploadError:
        abort(404)
    if not path.exists():
        abort(404)
    download = request.args.get("download") == "1"
    ext = Path(resource["file_path"]).suffix
    safe_title = "".join(ch for ch in resource["title"] if ch.isalnum() or ch in " -_").strip() or "file"
    response = send_file(path, mimetype=resource["mime_type"], conditional=True, etag=resource["checksum"][:32],
                         as_attachment=download, download_name=safe_title + ext, max_age=3600)
    response.headers["Accept-Ranges"] = "bytes"
    return response
