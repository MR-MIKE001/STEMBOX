"""Teacher monitoring: class performance, per-student detail, per-quiz breakdown, CSV export."""
import csv
import io

from flask import Blueprint, Response, abort, g, render_template, request

from ..auth import roles_required
from ..db import query
from ..permissions import can_manage_course, class_ids
from ..utils import percent, to_int

bp = Blueprint("progress", __name__, url_prefix="/progress")

STAFF = ("admin", "teacher")


def _visible_classes():
    if g.user["role"] == "admin":
        return query("SELECT * FROM classes ORDER BY level, name")
    ids = class_ids(g.user["id"], "teacher")
    if not ids:
        return []
    return query(f"SELECT * FROM classes WHERE id IN ({','.join('?' * len(ids))}) ORDER BY level, name", list(ids))


def _class_or_403(class_id):
    klass = query("SELECT * FROM classes WHERE id = ?", (class_id,), one=True)
    if klass is None:
        abort(404)
    if g.user["role"] != "admin" and class_id not in class_ids(g.user["id"], "teacher"):
        abort(403)
    return klass


def class_courses(class_id):
    """Courses whose published content this class's students can see."""
    return query("SELECT c.*, s.name AS subject_name FROM courses c JOIN subjects s ON s.id = c.subject_id "
                 "WHERE (c.class_id = ? OR c.class_id IS NULL) AND (EXISTS (SELECT 1 FROM lessons l WHERE "
                 "l.course_id = c.id AND l.status = 'published') OR EXISTS (SELECT 1 FROM quizzes z WHERE "
                 "z.course_id = c.id AND z.status = 'published')) ORDER BY s.name, c.title", (class_id,))


def class_summary(class_id, course_id=None):
    """Everything the class report, dashboards and CSV export need."""
    courses = class_courses(class_id)
    if course_id:
        courses = [c for c in courses if c["id"] == course_id]
    course_ids = [c["id"] for c in courses]
    students = query("SELECT u.* FROM class_members m JOIN users u ON u.id = m.user_id "
                     "WHERE m.class_id = ? AND m.role_in_class = 'student' ORDER BY u.name", (class_id,))
    student_ids = [s["id"] for s in students]
    lessons, quizzes = [], []
    if course_ids:
        marks = ",".join("?" * len(course_ids))
        lessons = query(f"SELECT id, title, course_id FROM lessons WHERE status = 'published' AND course_id IN ({marks})",
                        course_ids)
        quizzes = query(f"SELECT z.*, c.title AS course_title FROM quizzes z JOIN courses c ON c.id = z.course_id "
                        f"WHERE z.status = 'published' AND z.course_id IN ({marks}) ORDER BY c.title, z.id", course_ids)
    lesson_ids = {lesson["id"] for lesson in lessons}
    quiz_ids = {q["id"] for q in quizzes}

    completed = {sid: 0 for sid in student_ids}
    last_active = {sid: None for sid in student_ids}
    best = {}  # (student, quiz) -> best fraction
    attempts_count = {sid: 0 for sid in student_ids}
    if student_ids:
        smarks = ",".join("?" * len(student_ids))
        for row in query(f"SELECT user_id, lesson_id, completed_at, last_viewed_at FROM progress WHERE user_id IN ({smarks})",
                         student_ids):
            if row["lesson_id"] in lesson_ids:
                if row["completed_at"]:
                    completed[row["user_id"]] += 1
                if row["last_viewed_at"] and (last_active[row["user_id"]] or "") < row["last_viewed_at"]:
                    last_active[row["user_id"]] = row["last_viewed_at"]
        for row in query(f"SELECT user_id, quiz_id, score, total, finished_at FROM attempts "
                         f"WHERE finished_at IS NOT NULL AND user_id IN ({smarks})", student_ids):
            if row["quiz_id"] not in quiz_ids:
                continue
            attempts_count[row["user_id"]] += 1
            frac = row["score"] / row["total"] if row["total"] else 0
            key = (row["user_id"], row["quiz_id"])
            best[key] = max(best.get(key, 0), frac)
            if (last_active[row["user_id"]] or "") < row["finished_at"]:
                last_active[row["user_id"]] = row["finished_at"]

    rows = []
    for s in students:
        scores = [best[(s["id"], q["id"])] for q in quizzes if (s["id"], q["id"]) in best]
        passed = sum(1 for q in quizzes if (s["id"], q["id"]) in best and 100 * best[(s["id"], q["id"])] >= q["pass_mark"])
        rows.append({
            "id": s["id"], "name": s["name"], "username": s["username"], "active": s["active"],
            "completed": completed[s["id"]], "lessons": len(lessons),
            "completion": percent(completed[s["id"]], len(lessons)),
            "quizzes_taken": len(scores), "quizzes": len(quizzes), "quizzes_passed": passed,
            "attempts": attempts_count[s["id"]],
            "avg_score": round(100 * sum(scores) / len(scores)) if scores else None,
            "last_active": last_active[s["id"]],
        })

    quiz_rows = []
    for q in quizzes:
        scores = [best[(sid, q["id"])] for sid in student_ids if (sid, q["id"]) in best]
        quiz_rows.append({
            "id": q["id"], "title": q["title"], "course_title": q["course_title"], "pass_mark": q["pass_mark"],
            "taken": len(scores), "students": len(student_ids),
            "avg_score": round(100 * sum(scores) / len(scores)) if scores else None,
            "pass_rate": percent(sum(1 for f in scores if 100 * f >= q["pass_mark"]), len(scores)) if scores else None,
        })

    total_cells = len(students) * len(lessons)
    all_scores = [r["avg_score"] for r in rows if r["avg_score"] is not None]
    return {
        "courses": courses, "students": rows, "quizzes": quiz_rows,
        "lesson_count": len(lessons), "quiz_count": len(quizzes), "student_count": len(students),
        "completion_rate": percent(sum(completed.values()), total_cells) if total_cells else 0,
        "avg_score": round(sum(all_scores) / len(all_scores)) if all_scores else None,
        "active_students": sum(1 for r in rows if r["last_active"]),
    }


@bp.route("/")
@roles_required(*STAFF)
def overview():
    classes = _visible_classes()
    summaries = [(c, class_summary(c["id"])) for c in classes]
    return render_template("progress/overview.html", summaries=summaries)


@bp.route("/class/<int:class_id>")
@roles_required(*STAFF)
def class_report(class_id):
    klass = _class_or_403(class_id)
    course_id = to_int(request.args.get("course"))
    summary = class_summary(class_id, course_id)
    return render_template("progress/class_report.html", klass=klass, s=summary, course_id=course_id,
                           all_courses=class_courses(class_id))


@bp.route("/class/<int:class_id>/export.csv")
@roles_required(*STAFF)
def class_export(class_id):
    klass = _class_or_403(class_id)
    course_id = to_int(request.args.get("course"))
    s = class_summary(class_id, course_id)
    out = io.StringIO()
    writer = csv.writer(out)
    scope = s["courses"][0]["title"] if course_id and s["courses"] else "All courses"
    writer.writerow(["Class", klass["name"], "Courses", scope])
    writer.writerow([])
    writer.writerow(["Student", "Username", "Lessons finished", "Lessons available", "Completion %",
                     "Quizzes taken", "Quizzes available", "Quizzes passed", "Average best score %", "Attempts",
                     "Last active"])
    for r in s["students"]:
        writer.writerow([r["name"], r["username"], r["completed"], r["lessons"], r["completion"], r["quizzes_taken"],
                         r["quizzes"], r["quizzes_passed"], "" if r["avg_score"] is None else r["avg_score"],
                         r["attempts"], r["last_active"] or ""])
    writer.writerow([])
    writer.writerow(["Quiz", "Course", "Students who took it", "Students in class", "Average best score %",
                     "Pass rate %", "Pass mark %"])
    for q in s["quizzes"]:
        writer.writerow([q["title"], q["course_title"], q["taken"], q["students"],
                         "" if q["avg_score"] is None else q["avg_score"],
                         "" if q["pass_rate"] is None else q["pass_rate"], q["pass_mark"]])
    safe = "".join(ch if ch.isalnum() else "-" for ch in klass["name"]).strip("-") or "class"
    # Excel opens UTF-8 CSV correctly when it starts with a byte-order mark.
    return Response("﻿" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=stembox-{safe}-progress.csv"})


@bp.route("/student/<int:user_id>")
@roles_required(*STAFF)
def student_detail(user_id):
    student = query("SELECT * FROM users WHERE id = ? AND role = 'student'", (user_id,), one=True)
    if student is None:
        abort(404)
    student_classes = class_ids(user_id)
    if g.user["role"] != "admin" and not (student_classes & class_ids(g.user["id"], "teacher")):
        abort(403)
    classes = query(f"SELECT * FROM classes WHERE id IN ({','.join('?' * len(student_classes))})",
                    list(student_classes)) if student_classes else []
    lessons = query(
        "SELECT l.id, l.title, c.title AS course_title, p.completed_at, p.last_viewed_at FROM progress p "
        "JOIN lessons l ON l.id = p.lesson_id JOIN courses c ON c.id = l.course_id WHERE p.user_id = ? "
        "ORDER BY p.last_viewed_at DESC", (user_id,))
    attempts = query(
        "SELECT a.*, z.title AS quiz_title, z.pass_mark, c.title AS course_title FROM attempts a "
        "JOIN quizzes z ON z.id = a.quiz_id JOIN courses c ON c.id = z.course_id "
        "WHERE a.user_id = ? AND a.finished_at IS NOT NULL ORDER BY a.finished_at DESC", (user_id,))
    return render_template("progress/student.html", student=student, classes=classes, lessons=lessons,
                           attempts=attempts)


@bp.route("/quiz/<int:quiz_id>")
@roles_required(*STAFF)
def quiz_report(quiz_id):
    quiz = query("SELECT * FROM quizzes WHERE id = ?", (quiz_id,), one=True)
    if quiz is None:
        abort(404)
    course = query("SELECT * FROM courses WHERE id = ?", (quiz["course_id"],), one=True)
    if not can_manage_course(g.user, course):
        abort(403)
    attempts = query("SELECT a.*, u.name AS student_name FROM attempts a JOIN users u ON u.id = a.user_id "
                     "WHERE a.quiz_id = ? AND a.finished_at IS NOT NULL ORDER BY a.finished_at DESC", (quiz_id,))
    questions = query(
        "SELECT q.id, q.text, COUNT(aa.attempt_id) AS answered, COALESCE(SUM(aa.is_correct), 0) AS correct "
        "FROM questions q LEFT JOIN attempt_answers aa ON aa.question_id = q.id "
        "WHERE q.quiz_id = ? GROUP BY q.id ORDER BY q.position, q.id", (quiz_id,))
    scores = [percent(a["score"], a["total"]) for a in attempts]
    stats = {
        "attempts": len(attempts),
        "students": len({a["user_id"] for a in attempts}),
        "avg": round(sum(scores) / len(scores)) if scores else None,
        "pass_rate": percent(sum(1 for s in scores if s >= quiz["pass_mark"]), len(scores)) if scores else None,
    }
    return render_template("progress/quiz_report.html", quiz=quiz, course=course, attempts=attempts,
                           questions=questions, stats=stats)
