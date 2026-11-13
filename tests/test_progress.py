"""Phase 5: teacher monitoring, per-student detail, per-quiz breakdown, CSV export, dashboards."""
import pytest

from tests.conftest import db_all, db_one, login, make_user
from tests.helpers import make_class, published_course


@pytest.fixture
def scenario(app, client, users):
    content = published_course(app, client, users, mode="direct")
    s2 = make_user(app, "student2", "student", name="Second Student")
    class_id = make_class(app, "7A", teacher_ids=[users["teacher"]], student_ids=[users["student"], s2])
    # Student 1 finishes the lesson and scores 2/2 then 1/2; student 2 does nothing.
    login(client, "student")
    client.post(f"/learn/lesson/{content['lesson']}/complete", data={"done": "1"})
    for answers in (("4", "6"), ("4", "7")):
        response = client.post(f"/quiz/{content['quiz']}/start")
        attempt_id = int(response.headers["Location"].split("/")[-1])
        data = {}
        for qtext, answer in zip(("2+2?", "3+3?"), answers):
            qid = db_one(app, "SELECT id FROM questions WHERE text = ?", (qtext,))[0]
            cid = db_one(app, "SELECT id FROM choices WHERE question_id = ? AND text = ?", (qid, answer))[0]
            data[f"q{qid}"] = cid
        client.post(f"/attempt/{attempt_id}/submit", data=data)
    client.post("/logout")
    return {**content, "class": class_id, "student2": s2}


def test_class_summary_numbers(app, client, users, scenario):
    from app.progress import class_summary
    with app.test_request_context():
        s = class_summary(scenario["class"])
    assert s["student_count"] == 2 and s["lesson_count"] == 1 and s["quiz_count"] == 1
    assert s["completion_rate"] == 50
    first = next(r for r in s["students"] if r["username"] == "student")
    second = next(r for r in s["students"] if r["username"] == "student2")
    assert first["completed"] == 1 and first["avg_score"] == 100 and first["attempts"] == 2
    assert first["quizzes_passed"] == 1
    assert second["completed"] == 0 and second["avg_score"] is None and second["last_active"] is None
    quiz = s["quizzes"][0]
    assert quiz["taken"] == 1 and quiz["students"] == 2 and quiz["avg_score"] == 100 and quiz["pass_rate"] == 100
    assert s["avg_score"] == 100


def test_teacher_sees_class_report(app, client, users, scenario):
    login(client, "teacher")
    page = client.get(f"/progress/class/{scenario['class']}").data.decode()
    assert "Second Student" in page and "50%" in page and "Not started" in page
    page = client.get(f"/progress/class/{scenario['class']}?course={scenario['course']}").data.decode()
    assert "1 of 2 students" in page
    assert b"7A" in client.get("/progress/").data


def test_per_student_and_per_quiz_detail(app, client, users, scenario):
    login(client, "teacher")
    page = client.get(f"/progress/student/{users['student']}").data.decode()
    assert "Check" in page and "2/2" in page and "1/2" in page and "Finished" in page
    page = client.get(f"/progress/quiz/{scenario['quiz']}").data.decode()
    assert "2+2?" in page and "2 of 2" in page   # both attempts got 2+2 right
    assert "3+3?" in page and "1 of 2" in page
    # The teacher can open a student's attempt.
    attempt_id = db_one(app, "SELECT id FROM attempts LIMIT 1")[0]
    assert client.get(f"/attempt/{attempt_id}").status_code == 200


def test_csv_export(app, client, users, scenario):
    login(client, "teacher")
    response = client.get(f"/progress/class/{scenario['class']}/export.csv")
    assert response.status_code == 200 and response.mimetype == "text/csv"
    text = response.data.decode("utf-8-sig")
    assert "Student,Username,Lessons finished" in text
    assert "Second Student,student2,0,1,0" in text
    assert "Check,Fractions,1,2,100,100,50" in text


def test_monitoring_permissions(app, client, users, scenario):
    make_user(app, "teacher2", "teacher")
    login(client, "teacher2")
    assert client.get(f"/progress/class/{scenario['class']}").status_code == 403
    assert client.get(f"/progress/class/{scenario['class']}/export.csv").status_code == 403
    assert client.get(f"/progress/student/{users['student']}").status_code == 403
    assert client.get(f"/progress/quiz/{scenario['quiz']}").status_code == 403
    attempt_id = db_one(app, "SELECT id FROM attempts LIMIT 1")[0]
    assert client.get(f"/attempt/{attempt_id}").status_code == 403
    client.post("/logout")
    login(client, "student")
    assert client.get(f"/progress/class/{scenario['class']}").status_code == 403
    client.post("/logout")
    login(client, "admin")
    assert client.get(f"/progress/class/{scenario['class']}").status_code == 200


def test_dashboards_render(app, client, users, scenario):
    for who, expected in (("admin", "Connect other devices"), ("teacher", "My classes"), ("student", "My courses")):
        login(client, who)
        page = client.get("/dashboard").data.decode()
        assert expected in page, who
        client.post("/logout")
    login(client, "student")
    page = client.get("/dashboard").data.decode()
    assert "1/1" in page and "lessons finished" in page and "2/2" in page
    client.post("/logout")
    login(client, "teacher")
    assert "Student" in client.get("/dashboard").data.decode()


def test_dashboards_with_sample_content(app, client):
    from tests.conftest import db_exec
    app.test_cli_runner().invoke(args=["seed-dev"])
    db_exec(app, "UPDATE users SET must_change_password = 0")
    for username, password in (("admin", "admin123"), ("teacher1", "teacher123"), ("student1", "student123")):
        login(client, username, password)
        response = client.get("/dashboard")
        assert response.status_code == 200, username
        client.post("/logout")
