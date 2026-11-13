"""Quizzes: teacher quiz builder, student attempts and automatic marking."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import publishing
from ..auth import login_required, roles_required
from ..cms import get_course_or_404
from ..db import audit, execute, get_db, new_uuid, now, query
from ..permissions import can_manage_course, can_publish_directly, can_view_quiz, manageable_courses_clause
from ..utils import length_errors, percent, to_int

bp = Blueprint("quizzes", __name__)

STAFF = ("admin", "teacher")
MAX_CHOICES = 6


# ---------------------------------------------------------------- helpers

def _quiz_or_404(quiz_id, manage=True):
    quiz = query("SELECT * FROM quizzes WHERE id = ?", (quiz_id,), one=True)
    if quiz is None:
        abort(404)
    course = get_course_or_404(quiz["course_id"], manage=manage)
    return quiz, course


def load_questions(quiz_id):
    questions = [dict(q) for q in query("SELECT * FROM questions WHERE quiz_id = ? ORDER BY position, id", (quiz_id,))]
    by_id = {q["id"]: q for q in questions}
    for q in questions:
        q["choices"] = []
    if questions:
        marks = ",".join("?" * len(questions))
        for c in query(f"SELECT * FROM choices WHERE question_id IN ({marks}) ORDER BY position, id", list(by_id)):
            by_id[c["question_id"]]["choices"].append(dict(c))
    return questions


def quiz_problems(questions):
    """Reasons a quiz is not ready to be published."""
    problems = []
    if not questions:
        problems.append("Add at least one question.")
    for n, q in enumerate(questions, start=1):
        if len(q["choices"]) < 2:
            problems.append(f"Question {n} needs at least two answers.")
        if sum(1 for c in q["choices"] if c["is_correct"]) != 1:
            problems.append(f"Question {n} needs exactly one correct answer.")
    return problems


def _after_change(quiz, course):
    fresh = query("SELECT * FROM quizzes WHERE id = ?", (quiz["id"],), one=True)
    if publishing.content_changed("quiz", fresh, course, g.user):
        flash("Because this quiz was already published, your changes now wait for an administrator's approval. "
              "Students cannot take it until it is approved.", "warning")
    execute("UPDATE quizzes SET updated_at = ? WHERE id = ?", (now(), quiz["id"]))


# ---------------------------------------------------------------- teacher: quiz builder

@bp.route("/quizzes")
@roles_required(*STAFF)
def quiz_list():
    where, args = manageable_courses_clause(g.user)
    quizzes = query(
        "SELECT z.*, c.title AS course_title, s.name AS subject_name, "
        "(SELECT COUNT(*) FROM questions q WHERE q.quiz_id = z.id) AS question_count, "
        "(SELECT COUNT(*) FROM attempts a WHERE a.quiz_id = z.id AND a.finished_at IS NOT NULL) AS attempt_count "
        "FROM quizzes z JOIN courses c ON c.id = z.course_id JOIN subjects s ON s.id = c.subject_id "
        f"WHERE {where} ORDER BY s.name, c.title, z.title", args)
    courses = query(f"SELECT c.id, c.title, s.name AS subject_name FROM courses c JOIN subjects s ON s.id = c.subject_id "
                    f"WHERE {where} ORDER BY s.name, c.title", args)
    return render_template("quizzes/list.html", quizzes=quizzes, courses=courses)


@bp.route("/quizzes/new")
@roles_required(*STAFF)
def quiz_new_pick():
    course = get_course_or_404(to_int(request.args.get("course"), 0))
    return redirect(url_for("quizzes.quiz_new", course_id=course["id"]))


def _lesson_choices(course_id):
    return query("SELECT id, title FROM lessons WHERE course_id = ? ORDER BY position, id", (course_id,))


def _read_quiz_form(course_id):
    form = {
        "title": request.form.get("title", "").strip(),
        "description": request.form.get("description", "").strip(),
        "pass_mark": to_int(request.form.get("pass_mark"), -1),
        "lesson_id": to_int(request.form.get("lesson_id")),
    }
    errors = []
    if not form["title"]:
        errors.append("Please type the quiz title.")
    errors += length_errors([("The quiz title", form["title"], "title"),
                             ("The instructions", form["description"], "description")])
    if not 0 <= form["pass_mark"] <= 100:
        errors.append("The pass mark must be a number from 0 to 100.")
    if form["lesson_id"] and not query("SELECT 1 FROM lessons WHERE id = ? AND course_id = ?",
                                       (form["lesson_id"], course_id), one=True):
        errors.append("Please choose a lesson from this course.")
    return form, errors


@bp.route("/content/courses/<int:course_id>/quizzes/new", methods=["GET", "POST"])
@roles_required(*STAFF)
def quiz_new(course_id):
    course = get_course_or_404(course_id)
    form = {"title": "", "description": "", "pass_mark": 50, "lesson_id": to_int(request.args.get("lesson"))}
    errors = []
    if request.method == "POST":
        form, errors = _read_quiz_form(course_id)
        if not errors:
            stamp = now()
            quiz_id = execute(
                "INSERT INTO quizzes (uuid, course_id, lesson_id, title, description, pass_mark, status, author_id, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?)",
                (new_uuid(), course_id, form["lesson_id"], form["title"], form["description"], form["pass_mark"],
                 g.user["id"], stamp, stamp))
            flash("Quiz created. Now add the questions.", "success")
            return redirect(url_for("quizzes.quiz_edit", quiz_id=quiz_id))
    return render_template("quizzes/quiz_form.html", form=form, errors=errors, course=course, quiz=None,
                           lessons=_lesson_choices(course_id))


@bp.route("/quizzes/<int:quiz_id>/settings", methods=["GET", "POST"])
@roles_required(*STAFF)
def quiz_settings(quiz_id):
    quiz, course = _quiz_or_404(quiz_id)
    form = dict(quiz)
    errors = []
    if request.method == "POST":
        form, errors = _read_quiz_form(course["id"])
        if not errors:
            execute("UPDATE quizzes SET title = ?, description = ?, pass_mark = ?, lesson_id = ? WHERE id = ?",
                    (form["title"], form["description"], form["pass_mark"], form["lesson_id"], quiz_id))
            flash("Quiz saved.", "success")
            _after_change(quiz, course)
            return redirect(url_for("quizzes.quiz_edit", quiz_id=quiz_id))
    return render_template("quizzes/quiz_form.html", form=form, errors=errors, course=course, quiz=quiz,
                           lessons=_lesson_choices(course["id"]))


@bp.route("/quizzes/<int:quiz_id>")
@roles_required(*STAFF)
def quiz_edit(quiz_id):
    quiz, course = _quiz_or_404(quiz_id)
    questions = load_questions(quiz_id)
    attempts = query("SELECT COUNT(*) AS n FROM attempts WHERE quiz_id = ? AND finished_at IS NOT NULL",
                     (quiz_id,), one=True)["n"]
    lesson = query("SELECT * FROM lessons WHERE id = ?", (quiz["lesson_id"],), one=True) if quiz["lesson_id"] else None
    return render_template("quizzes/quiz_edit.html", quiz=quiz, course=course, questions=questions,
                           problems=quiz_problems(questions), direct=can_publish_directly(g.user, course),
                           review=publishing.latest_review("quiz", quiz_id), attempts=attempts, lesson=lesson,
                           blank={"text": "", "choices": [], "correct": None}, max_choices=MAX_CHOICES)


def _read_question_form():
    text = request.form.get("text", "").strip()
    raw = [request.form.get(f"choice_{i}", "").strip() for i in range(MAX_CHOICES)]
    correct = to_int(request.form.get("correct"))
    errors = []
    if not text:
        errors.append("Please type the question.")
    errors += length_errors([("The question", text, "question")] +
                            [(f"Answer {i + 1}", t, "choice") for i, t in enumerate(raw)])
    choices = [(i, t) for i, t in enumerate(raw) if t]
    if len(choices) < 2:
        errors.append("Type at least two possible answers.")
    if correct is None or not (0 <= correct < MAX_CHOICES) or not raw[correct]:
        errors.append("Choose which answer is correct (the round button next to it).")
    return {"text": text, "raw": raw, "correct": correct}, choices, errors


def _save_choices(question_id, choices, correct):
    db = get_db()
    db.execute("DELETE FROM choices WHERE question_id = ?", (question_id,))
    for pos, (index, text) in enumerate(choices):
        db.execute("INSERT INTO choices (question_id, text, is_correct, position) VALUES (?, ?, ?, ?)",
                   (question_id, text[:500], 1 if index == correct else 0, pos))
    db.commit()


@bp.route("/quizzes/<int:quiz_id>/questions", methods=["POST"])
@roles_required(*STAFF)
def question_add(quiz_id):
    quiz, course = _quiz_or_404(quiz_id)
    form, choices, errors = _read_question_form()
    if errors:
        questions = load_questions(quiz_id)
        return render_template("quizzes/quiz_edit.html", quiz=quiz, course=course, questions=questions,
                               problems=quiz_problems(questions), direct=can_publish_directly(g.user, course),
                               review=publishing.latest_review("quiz", quiz_id), attempts=0, lesson=None,
                               blank={"text": form["text"], "raw": form["raw"], "correct": form["correct"]},
                               max_choices=MAX_CHOICES, form_errors=errors), 400
    pos = query("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM questions WHERE quiz_id = ?", (quiz_id,), one=True)["p"]
    question_id = execute("INSERT INTO questions (uuid, quiz_id, text, position) VALUES (?, ?, ?, ?)",
                          (new_uuid(), quiz_id, form["text"][:2000], pos))
    _save_choices(question_id, choices, form["correct"])
    flash("Question added.", "success")
    _after_change(quiz, course)
    return redirect(url_for("quizzes.quiz_edit", quiz_id=quiz_id) + "#add-question")


def _question_or_404(question_id):
    question = query("SELECT * FROM questions WHERE id = ?", (question_id,), one=True)
    if question is None:
        abort(404)
    quiz, course = _quiz_or_404(question["quiz_id"])
    return question, quiz, course


@bp.route("/quizzes/questions/<int:question_id>/edit", methods=["GET", "POST"])
@roles_required(*STAFF)
def question_edit(question_id):
    question, quiz, course = _question_or_404(question_id)
    current = [dict(c) for c in query("SELECT * FROM choices WHERE question_id = ? ORDER BY position, id", (question_id,))]
    form = {"text": question["text"], "raw": [c["text"] for c in current] + [""] * (MAX_CHOICES - len(current)),
            "correct": next((i for i, c in enumerate(current) if c["is_correct"]), None)}
    errors = []
    if request.method == "POST":
        form, choices, errors = _read_question_form()
        if not errors:
            execute("UPDATE questions SET text = ? WHERE id = ?", (form["text"][:2000], question_id))
            _save_choices(question_id, choices, form["correct"])
            flash("Question saved.", "success")
            _after_change(quiz, course)
            return redirect(url_for("quizzes.quiz_edit", quiz_id=quiz["id"]))
    return render_template("quizzes/question_form.html", quiz=quiz, course=course, question=question, form=form,
                           errors=errors, max_choices=MAX_CHOICES)


@bp.route("/quizzes/questions/<int:question_id>/move/<direction>", methods=["POST"])
@roles_required(*STAFF)
def question_move(question_id, direction):
    question, quiz, _course = _question_or_404(question_id)
    from ..cms import _move
    _move("questions", question, "quiz_id = ?", (quiz["id"],), direction)
    return redirect(url_for("quizzes.quiz_edit", quiz_id=quiz["id"]))


@bp.route("/quizzes/questions/<int:question_id>/delete", methods=["POST"])
@roles_required(*STAFF)
def question_delete(question_id):
    question, quiz, course = _question_or_404(question_id)
    execute("DELETE FROM questions WHERE id = ?", (question_id,))
    flash("Question removed.", "success")
    _after_change(quiz, course)
    return redirect(url_for("quizzes.quiz_edit", quiz_id=quiz["id"]))


@bp.route("/quizzes/<int:quiz_id>/publish", methods=["POST"])
@roles_required(*STAFF)
def quiz_publish(quiz_id):
    quiz, course = _quiz_or_404(quiz_id)
    problems = quiz_problems(load_questions(quiz_id))
    if problems:
        flash("The quiz is not ready yet: " + " ".join(problems), "error")
    elif publishing.publish("quiz", quiz, course, g.user) == "published":
        flash("Published. Students can now take this quiz.", "success")
    else:
        flash("Sent to the administrator for approval.", "info")
    return redirect(url_for("quizzes.quiz_edit", quiz_id=quiz_id))


@bp.route("/quizzes/<int:quiz_id>/unpublish", methods=["POST"])
@roles_required(*STAFF)
def quiz_unpublish(quiz_id):
    quiz, _course = _quiz_or_404(quiz_id)
    publishing.unpublish("quiz", quiz, g.user)
    flash("The quiz is now a draft and hidden from students.", "success")
    return redirect(url_for("quizzes.quiz_edit", quiz_id=quiz_id))


@bp.route("/quizzes/<int:quiz_id>/delete", methods=["POST"])
@roles_required(*STAFF)
def quiz_delete(quiz_id):
    quiz, course = _quiz_or_404(quiz_id)
    execute("DELETE FROM quizzes WHERE id = ?", (quiz_id,))
    publishing.delete_requests("quiz", quiz_id)
    audit("quiz.delete", f"quiz:{quiz_id}", quiz["title"])
    flash(f"Quiz “{quiz['title']}” and its results were deleted.", "success")
    return redirect(url_for("cms.course_detail", course_id=course["id"]))


# ---------------------------------------------------------------- students: taking quizzes

def _visible_quiz_or_404(quiz_id):
    quiz = query("SELECT * FROM quizzes WHERE id = ?", (quiz_id,), one=True)
    if quiz is None:
        abort(404)
    course = query("SELECT c.*, s.name AS subject_name FROM courses c JOIN subjects s ON s.id = c.subject_id "
                   "WHERE c.id = ?", (quiz["course_id"],), one=True)
    if not can_view_quiz(g.user, quiz, course):
        abort(404 if g.user["role"] == "student" else 403)
    return quiz, course


@bp.route("/quiz/<int:quiz_id>")
@login_required
def quiz_start_page(quiz_id):
    quiz, course = _visible_quiz_or_404(quiz_id)
    questions = load_questions(quiz_id)
    attempts = query("SELECT * FROM attempts WHERE quiz_id = ? AND user_id = ? ORDER BY id DESC",
                     (quiz_id, g.user["id"]))
    unfinished = next((a for a in attempts if a["finished_at"] is None), None)
    finished = [a for a in attempts if a["finished_at"]]
    return render_template("quizzes/start.html", quiz=quiz, course=course, questions=questions,
                           unfinished=unfinished, finished=finished,
                           staff_preview=g.user["role"] != "student",
                           can_manage=can_manage_course(g.user, course))


@bp.route("/quiz/<int:quiz_id>/start", methods=["POST"])
@roles_required("student")
def quiz_start(quiz_id):
    quiz, _course = _visible_quiz_or_404(quiz_id)
    if quiz["status"] != "published":
        abort(404)
    attempt = query("SELECT * FROM attempts WHERE quiz_id = ? AND user_id = ? AND finished_at IS NULL "
                    "ORDER BY id DESC LIMIT 1", (quiz_id, g.user["id"]), one=True)
    if attempt is None:
        attempt_id = execute("INSERT INTO attempts (uuid, quiz_id, user_id, started_at) VALUES (?, ?, ?, ?)",
                             (new_uuid(), quiz_id, g.user["id"], now()))
    else:
        attempt_id = attempt["id"]
    return redirect(url_for("quizzes.attempt", attempt_id=attempt_id))


def _attempt_or_404(attempt_id):
    attempt = query("SELECT * FROM attempts WHERE id = ?", (attempt_id,), one=True)
    if attempt is None:
        abort(404)
    # Students only see their own attempts; staff who manage the course may look at any.
    if attempt["user_id"] != g.user["id"]:
        if g.user["role"] == "student":
            abort(404)
        quiz = query("SELECT * FROM quizzes WHERE id = ?", (attempt["quiz_id"],), one=True)
        course = query("SELECT * FROM courses WHERE id = ?", (quiz["course_id"],), one=True)
        if not can_manage_course(g.user, course):
            abort(403)
    return attempt


@bp.route("/attempt/<int:attempt_id>")
@login_required
def attempt(attempt_id):
    attempt = _attempt_or_404(attempt_id)
    quiz = query("SELECT * FROM quizzes WHERE id = ?", (attempt["quiz_id"],), one=True)
    course = query("SELECT * FROM courses WHERE id = ?", (quiz["course_id"],), one=True)
    if attempt["finished_at"] is None:
        if attempt["user_id"] != g.user["id"]:
            abort(404)
        return render_template("quizzes/take.html", attempt=attempt, quiz=quiz, course=course,
                               questions=load_questions(quiz["id"]))
    answers = query("SELECT * FROM attempt_answers WHERE attempt_id = ? ORDER BY rowid", (attempt_id,))
    student = query("SELECT name FROM users WHERE id = ?", (attempt["user_id"],), one=True)
    pct = percent(attempt["score"], attempt["total"])
    return render_template("quizzes/result.html", attempt=attempt, quiz=quiz, course=course, answers=answers,
                           pct=pct, passed=pct >= quiz["pass_mark"], student=student)


def mark_attempt(attempt, quiz, form):
    """Mark the submitted answers, store them and close the attempt. Returns (score, total)."""
    questions = load_questions(quiz["id"])
    db = get_db()
    score = 0
    for q in questions:
        chosen_id = to_int(form.get(f"q{q['id']}"))
        chosen = next((c for c in q["choices"] if c["id"] == chosen_id), None)
        correct = next((c for c in q["choices"] if c["is_correct"]), None)
        is_correct = bool(chosen and chosen["is_correct"])
        score += is_correct
        db.execute("INSERT INTO attempt_answers (attempt_id, question_id, choice_id, question_text, choice_text, "
                   "correct_text, is_correct) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (attempt["id"], q["id"], chosen["id"] if chosen else None, q["text"],
                    chosen["text"] if chosen else "", correct["text"] if correct else "", int(is_correct)))
    db.execute("UPDATE attempts SET score = ?, total = ?, finished_at = ? WHERE id = ?",
               (score, len(questions), now(), attempt["id"]))
    db.commit()
    return score, len(questions)


@bp.route("/attempt/<int:attempt_id>/submit", methods=["POST"])
@roles_required("student")
def attempt_submit(attempt_id):
    attempt = _attempt_or_404(attempt_id)
    if attempt["user_id"] != g.user["id"]:
        abort(404)
    if attempt["finished_at"] is not None:
        flash("This attempt was already handed in.", "info")
        return redirect(url_for("quizzes.attempt", attempt_id=attempt_id))
    quiz, _course = _visible_quiz_or_404(attempt["quiz_id"])
    mark_attempt(attempt, quiz, request.form)
    return redirect(url_for("quizzes.attempt", attempt_id=attempt_id))


@bp.route("/my-results")
@roles_required("student")
def my_results():
    rows = query(
        "SELECT a.*, z.title AS quiz_title, z.pass_mark, c.title AS course_title FROM attempts a "
        "JOIN quizzes z ON z.id = a.quiz_id JOIN courses c ON c.id = z.course_id "
        "WHERE a.user_id = ? AND a.finished_at IS NOT NULL ORDER BY a.finished_at DESC", (g.user["id"],))
    completed = query(
        "SELECT p.completed_at, l.id, l.title, c.title AS course_title FROM progress p JOIN lessons l ON l.id = p.lesson_id "
        "JOIN courses c ON c.id = l.course_id WHERE p.user_id = ? AND p.completed_at IS NOT NULL "
        "ORDER BY p.completed_at DESC", (g.user["id"],))
    return render_template("quizzes/my_results.html", rows=rows, completed=completed)
