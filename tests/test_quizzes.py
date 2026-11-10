"""Phase 4: quiz builder, attempts, automatic marking, saved results."""
import pytest

from tests.conftest import db_all, db_one, login, make_user
from tests.helpers import published_course, set_mode


@pytest.fixture
def content(app, client, users):
    return published_course(app, client, users, mode="direct")


def _choices(app, quiz_id):
    """{question_text: {choice_text: choice_id}}"""
    out = {}
    for row in db_all(app, "SELECT q.text AS q, c.text AS c, c.id FROM choices c JOIN questions q ON q.id = c.question_id "
                           "WHERE q.quiz_id = ?", (quiz_id,)):
        out.setdefault(row["q"], {})[row["c"]] = row["id"]
    return out


def _qid(app, text):
    return db_one(app, "SELECT id FROM questions WHERE text = ?", (text,))[0]


def _take(client, app, quiz_id, answers):
    response = client.post(f"/quiz/{quiz_id}/start")
    attempt_id = int(response.headers["Location"].rstrip("/").split("/")[-1])
    page = client.get(f"/attempt/{attempt_id}").data.decode()
    assert "Hand in my answers" in page
    choices = _choices(app, quiz_id)
    data = {f"q{_qid(app, q)}": choices[q][a] for q, a in answers.items()}
    client.post(f"/attempt/{attempt_id}/submit", data=data)
    return attempt_id


def test_quiz_builder_validation(app, client, users, content):
    login(client, "teacher")
    client.post(f"/content/courses/{content['course']}/quizzes/new", data={"title": "Empty", "pass_mark": "50"})
    quiz_id = db_one(app, "SELECT id FROM quizzes WHERE title = 'Empty'")[0]
    # No correct answer chosen -> refused.
    response = client.post(f"/quizzes/{quiz_id}/questions", data={"text": "Q?", "choice_0": "a", "choice_1": "b"})
    assert response.status_code == 400 and b"Choose which answer is correct" in response.data
    # Only one answer -> refused.
    response = client.post(f"/quizzes/{quiz_id}/questions", data={"text": "Q?", "choice_0": "a", "correct": "0"})
    assert response.status_code == 400
    # Correct mark on an empty answer -> refused.
    response = client.post(f"/quizzes/{quiz_id}/questions", data={"text": "Q?", "choice_0": "a", "choice_1": "b", "correct": "3"})
    assert response.status_code == 400
    # Publishing an empty quiz is blocked.
    client.post(f"/quizzes/{quiz_id}/publish")
    assert db_one(app, "SELECT status FROM quizzes WHERE id = ?", (quiz_id,))[0] == "draft"
    # Bad pass mark.
    response = client.post(f"/quizzes/{quiz_id}/settings", data={"title": "Empty", "pass_mark": "150"})
    assert b"from 0 to 100" in response.data


def test_question_edit_and_delete(app, client, users, content):
    login(client, "teacher")
    qid = _qid(app, "2+2?")
    client.post(f"/quizzes/questions/{qid}/edit", data={"text": "2+2 = ?", "choice_0": "4", "choice_1": "5",
                                                       "choice_2": "22", "correct": "0"})
    rows = db_all(app, "SELECT text, is_correct FROM choices WHERE question_id = ? ORDER BY position", (qid,))
    assert [(r[0], r[1]) for r in rows] == [("4", 1), ("5", 0), ("22", 0)]
    client.post(f"/quizzes/questions/{qid}/delete")
    assert db_one(app, "SELECT COUNT(*) FROM questions WHERE quiz_id = ?", (content["quiz"],))[0] == 1


def test_student_takes_quiz_and_is_marked(app, client, users, content):
    login(client, "student")
    attempt_id = _take(client, app, content["quiz"], {"2+2?": "4", "3+3?": "7"})
    attempt = db_one(app, "SELECT * FROM attempts WHERE id = ?", (attempt_id,))
    assert (attempt["score"], attempt["total"]) == (1, 2)
    assert attempt["finished_at"] is not None
    answers = db_all(app, "SELECT question_text, choice_text, correct_text, is_correct FROM attempt_answers "
                          "WHERE attempt_id = ?", (attempt_id,))
    assert {(a[0], a[1], a[2], a[3]) for a in answers} == {("2+2?", "4", "4", 1), ("3+3?", "7", "6", 0)}
    page = client.get(f"/attempt/{attempt_id}").data.decode()
    assert "1 / 2" in page and "50%" in page and "Passed" in page


def test_unanswered_questions_count_as_wrong(app, client, users, content):
    login(client, "student")
    attempt_id = _take(client, app, content["quiz"], {})
    assert db_one(app, "SELECT score, total FROM attempts WHERE id = ?", (attempt_id,))[:] == (0, 2)


def test_every_attempt_stored_and_shown(app, client, users, content):
    login(client, "student")
    _take(client, app, content["quiz"], {"2+2?": "3", "3+3?": "7"})
    _take(client, app, content["quiz"], {"2+2?": "4", "3+3?": "6"})
    assert db_one(app, "SELECT COUNT(*) FROM attempts WHERE finished_at IS NOT NULL")[0] == 2
    page = client.get("/my-results").data.decode()
    assert page.count("Check") >= 2 and "2/2" in page and "0/2" in page
    page = client.get(f"/quiz/{content['quiz']}").data.decode()
    assert "Your earlier results" in page and "Try again" in page


def test_cannot_resubmit_or_tamper(app, client, users, content):
    other = make_user(app, "other", "student")
    login(client, "student")
    attempt_id = _take(client, app, content["quiz"], {"2+2?": "3"})
    # Re-submitting does not change the score.
    choices = _choices(app, content["quiz"])
    client.post(f"/attempt/{attempt_id}/submit", data={f"q{_qid(app, '2+2?')}": choices["2+2?"]["4"]})
    assert db_one(app, "SELECT score FROM attempts WHERE id = ?", (attempt_id,))[0] == 0
    client.post("/logout")
    login(client, "other")
    assert client.get(f"/attempt/{attempt_id}").status_code == 404
    assert client.post(f"/attempt/{attempt_id}/submit").status_code == 404
    # A choice from another question is ignored.
    response = client.post(f"/quiz/{content['quiz']}/start")
    new_id = int(response.headers["Location"].split("/")[-1])
    client.post(f"/attempt/{new_id}/submit", data={f"q{_qid(app, '2+2?')}": choices["3+3?"]["6"]})
    assert db_one(app, "SELECT score FROM attempts WHERE id = ?", (new_id,))[0] == 0


def test_draft_quiz_hidden_from_students(app, client, users, content):
    login(client, "teacher")
    client.post(f"/quizzes/{content['quiz']}/unpublish")
    client.post("/logout")
    login(client, "student")
    assert client.get(f"/quiz/{content['quiz']}").status_code == 404
    assert client.post(f"/quiz/{content['quiz']}/start").status_code == 404


def test_teacher_preview_cannot_start_attempt(app, client, users, content):
    login(client, "teacher")
    page = client.get(f"/quiz/{content['quiz']}").data.decode()
    assert "Teacher preview" in page and "Correct" in page
    assert client.post(f"/quiz/{content['quiz']}/start").status_code == 403


def test_quiz_needs_approval_in_approval_mode(app, client, users):
    content = published_course(app, client, users, mode="approval")
    assert db_one(app, "SELECT status FROM quizzes WHERE id = ?", (content["quiz"],))[0] == "pending"
    assert db_one(app, "SELECT COUNT(*) FROM publish_requests WHERE target_type = 'quiz'")[0] == 1


def test_results_survive_quiz_edits(app, client, users, content):
    login(client, "student")
    attempt_id = _take(client, app, content["quiz"], {"2+2?": "4", "3+3?": "6"})
    client.post("/logout")
    login(client, "teacher")
    client.post(f"/quizzes/questions/{_qid(app, '2+2?')}/delete")
    client.post("/logout")
    login(client, "student")
    page = client.get(f"/attempt/{attempt_id}").data.decode()
    assert "2+2?" in page and "2 / 2" in page
