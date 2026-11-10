"""Builders that create content through the real HTTP routes."""
from tests.conftest import MP4_BYTES, PDF_BYTES, db_one, login, upload


def set_mode(app, mode):
    from tests.conftest import db_exec
    db_exec(app, "UPDATE settings SET value = ? WHERE key = 'publish_mode'", (mode,))


def create_subject(client, app, name="Maths"):
    client.post("/content/subjects/new", data={"name": name})
    return db_one(app, "SELECT id FROM subjects WHERE name = ?", (name,))[0]


def create_course(client, app, subject_id, title="Fractions", class_id=None, level="Grade 7"):
    data = {"subject_id": subject_id, "title": title, "level": level, "description": ""}
    if class_id:
        data["class_id"] = class_id
    response = client.post("/content/courses/new", data=data)
    assert response.status_code == 302, response.data[:500]
    return db_one(app, "SELECT id FROM courses WHERE title = ?", (title,))[0]


def create_lesson(client, app, course_id, title="Lesson 1", body="Some **text**."):
    response = client.post(f"/content/courses/{course_id}/lessons/new", data={"title": title, "body": body},
                           content_type="multipart/form-data")
    assert response.status_code == 302, response.data[:500]
    return db_one(app, "SELECT id FROM lessons WHERE title = ? AND course_id = ?", (title, course_id))[0]


def upload_file(client, lesson_id, data=PDF_BYTES, name="notes.pdf", rtype="auto", title=""):
    return client.post(f"/content/lessons/{lesson_id}/resources",
                       data={"file": upload(data, name), "type": rtype, "title": title},
                       content_type="multipart/form-data")


def make_class(app, name, teacher_ids=(), student_ids=()):
    from tests.conftest import db_exec
    class_id = db_exec(app, "INSERT INTO classes (uuid, name, level, created_at) VALUES (lower(hex(randomblob(16))), ?, '', '2026')",
                       (name,))
    for t in teacher_ids:
        db_exec(app, "INSERT INTO class_members VALUES (?, ?, 'teacher')", (class_id, t))
    for s in student_ids:
        db_exec(app, "INSERT INTO class_members VALUES (?, ?, 'student')", (class_id, s))
    return class_id


def published_course(app, client, users, mode="direct", with_quiz=True):
    """Teacher builds a course with one published lesson (PDF + video) and a published quiz."""
    set_mode(app, mode)
    login(client, "teacher")
    subject_id = create_subject(client, app)
    course_id = create_course(client, app, subject_id)
    lesson_id = create_lesson(client, app, course_id)
    upload_file(client, lesson_id, PDF_BYTES, "notes.pdf")
    upload_file(client, lesson_id, MP4_BYTES, "clip.mp4")
    client.post(f"/content/lessons/{lesson_id}/publish")
    quiz_id = None
    if with_quiz:
        client.post(f"/content/courses/{course_id}/quizzes/new", data={"title": "Check", "pass_mark": "50"})
        quiz_id = db_one(app, "SELECT id FROM quizzes WHERE title = 'Check'")[0]
        client.post(f"/quizzes/{quiz_id}/questions", data={"text": "2+2?", "choice_0": "3", "choice_1": "4", "correct": "1"})
        client.post(f"/quizzes/{quiz_id}/questions", data={"text": "3+3?", "choice_0": "6", "choice_1": "7", "correct": "0"})
        client.post(f"/quizzes/{quiz_id}/publish")
    client.post("/logout")
    return {"subject": subject_id, "course": course_id, "lesson": lesson_id, "quiz": quiz_id}
