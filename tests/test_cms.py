"""Phase 2: content management, uploads, search and the publishing workflow."""
import pytest

from tests.conftest import MP3_BYTES, MP4_BYTES, PDF_BYTES, PNG_BYTES, db_all, db_one, login, make_user
from tests.helpers import create_course, create_lesson, create_subject, make_class, set_mode, upload_file


@pytest.fixture
def course(app, as_teacher):
    subject_id = create_subject(as_teacher, app)
    return create_course(as_teacher, app, subject_id)


def _status(app, lesson_id):
    return db_one(app, "SELECT status FROM lessons WHERE id = ?", (lesson_id,))[0]


# ---------------------------------------------------------------- structure

def test_teacher_creates_subject_course_topic_lesson(app, as_teacher, course):
    as_teacher.post(f"/content/courses/{course}/topics", data={"title": "Basics"})
    topic_id = db_one(app, "SELECT id FROM topics WHERE course_id = ?", (course,))[0]
    response = as_teacher.post(f"/content/courses/{course}/lessons/new",
                               data={"title": "Halves", "body": "# Hi", "topic_id": topic_id},
                               content_type="multipart/form-data")
    assert response.status_code == 302
    lesson = db_one(app, "SELECT * FROM lessons WHERE title = 'Halves'")
    assert lesson["status"] == "draft" and lesson["topic_id"] == topic_id
    page = as_teacher.get(f"/content/courses/{course}").data.decode()
    assert "Basics" in page and "Halves" in page


def test_lesson_with_new_topic_and_attached_file(app, as_teacher, course):
    from tests.conftest import upload
    as_teacher.post(f"/content/courses/{course}/lessons/new",
                    data={"title": "With file", "body": "", "new_topic": "Fresh topic",
                          "file": upload(PDF_BYTES, "sheet.pdf")}, content_type="multipart/form-data")
    lesson = db_one(app, "SELECT * FROM lessons WHERE title = 'With file'")
    assert db_one(app, "SELECT title FROM topics WHERE id = ?", (lesson["topic_id"],))[0] == "Fresh topic"
    assert db_one(app, "SELECT type FROM resources WHERE lesson_id = ?", (lesson["id"],))[0] == "pdf"


def test_lesson_text_is_escaped(app, as_teacher, course):
    lesson_id = create_lesson(as_teacher, app, course, body="<script>alert(1)</script> **bold**")
    page = as_teacher.get(f"/content/lessons/{lesson_id}").data.decode()
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page and "<strong>bold</strong>" in page


# ---------------------------------------------------------------- uploads

def test_upload_pdf_video_image_audio(app, as_teacher, course):
    lesson_id = create_lesson(as_teacher, app, course)
    for data, name, rtype in [(PDF_BYTES, "a.pdf", "pdf"), (MP4_BYTES, "b.mp4", "video"),
                              (PNG_BYTES, "c.png", "image"), (MP3_BYTES, "d.mp3", "audio")]:
        assert upload_file(as_teacher, lesson_id, data, name).status_code == 302
        row = db_one(app, "SELECT * FROM resources WHERE original_name = ?", (name,))
        assert row["type"] == rtype
        assert row["size"] == len(data) and len(row["checksum"]) == 64
        stored = app.config["STORAGE_DIR"] + "/files/" + row["file_path"]
        with open(stored, "rb") as fh:
            assert fh.read() == data


def test_upload_rejects_bad_type_and_fake_content(app, as_teacher, course):
    lesson_id = create_lesson(as_teacher, app, course)
    response = upload_file(as_teacher, lesson_id, b"MZ\x90\x00", "virus.exe")
    assert db_one(app, "SELECT COUNT(*) FROM resources")[0] == 0
    response = upload_file(as_teacher, lesson_id, PNG_BYTES, "renamed.pdf")  # PNG bytes with a .pdf name
    assert db_one(app, "SELECT COUNT(*) FROM resources")[0] == 0
    response = upload_file(as_teacher, lesson_id, PDF_BYTES, "doc.pdf", rtype="video")  # wrong declared type
    assert db_one(app, "SELECT COUNT(*) FROM resources")[0] == 0
    page = as_teacher.get(f"/content/lessons/{lesson_id}").data.decode()
    assert "not allowed" in page or "do not match" in page


def test_upload_size_limit(app, as_teacher, course):
    app.config["UPLOAD_LIMITS"] = {**app.config["UPLOAD_LIMITS"], "pdf": 100}
    lesson_id = create_lesson(as_teacher, app, course)
    upload_file(as_teacher, lesson_id, PDF_BYTES + b"x" * 200, "big.pdf")
    assert db_one(app, "SELECT COUNT(*) FROM resources")[0] == 0
    import os
    assert os.listdir(os.path.join(app.config["STORAGE_DIR"], "tmp")) == []  # partial file cleaned up


def test_client_filename_never_used_for_storage(app, as_teacher, course):
    lesson_id = create_lesson(as_teacher, app, course)
    upload_file(as_teacher, lesson_id, PDF_BYTES, "../../../evil.pdf")
    row = db_one(app, "SELECT * FROM resources")
    assert ".." not in row["file_path"] and "evil" not in row["file_path"]
    assert row["original_name"] == "evil.pdf"


def test_replace_and_delete_resource(app, as_teacher, course):
    import os
    from tests.conftest import upload
    lesson_id = create_lesson(as_teacher, app, course)
    upload_file(as_teacher, lesson_id, PDF_BYTES, "v1.pdf")
    row = db_one(app, "SELECT * FROM resources")
    old_path = os.path.join(app.config["STORAGE_DIR"], "files", row["file_path"])
    as_teacher.post(f"/content/resources/{row['id']}/edit",
                    data={"title": "Version 2", "file": upload(PDF_BYTES + b"%v2", "v2.pdf")},
                    content_type="multipart/form-data")
    new = db_one(app, "SELECT * FROM resources")
    assert new["title"] == "Version 2" and new["checksum"] != row["checksum"]
    assert not os.path.exists(old_path)
    # Replacing with another type is refused.
    as_teacher.post(f"/content/resources/{row['id']}/edit",
                    data={"title": "x", "file": upload(MP4_BYTES, "x.mp4")}, content_type="multipart/form-data")
    assert db_one(app, "SELECT type FROM resources")[0] == "pdf"
    new_path = os.path.join(app.config["STORAGE_DIR"], "files", new["file_path"])
    as_teacher.post(f"/content/resources/{row['id']}/delete")
    assert db_one(app, "SELECT COUNT(*) FROM resources")[0] == 0
    assert not os.path.exists(new_path)


def test_delete_course_removes_files(app, as_teacher, course):
    import os
    lesson_id = create_lesson(as_teacher, app, course)
    upload_file(as_teacher, lesson_id, PDF_BYTES, "a.pdf")
    path = os.path.join(app.config["STORAGE_DIR"], "files", db_one(app, "SELECT file_path FROM resources")[0])
    as_teacher.post(f"/content/courses/{course}/delete")
    assert db_one(app, "SELECT COUNT(*) FROM lessons")[0] == 0
    assert not os.path.exists(path)


# ---------------------------------------------------------------- publishing modes

def test_mode_approval_requires_admin(app, client, users):
    set_mode(app, "approval")
    login(client, "teacher")
    course = create_course(client, app, create_subject(client, app))
    lesson_id = create_lesson(client, app, course)
    client.post(f"/content/lessons/{lesson_id}/publish")
    assert _status(app, lesson_id) == "pending"
    # Teacher cannot reach the approval queue.
    assert client.get("/content/approvals").status_code == 403
    client.post("/logout")

    login(client, "admin")
    assert b"Lesson 1" in client.get("/content/approvals").data
    req = db_one(app, "SELECT id FROM publish_requests WHERE status = 'pending'")[0]
    client.post(f"/content/approvals/{req}/approve")
    assert _status(app, lesson_id) == "published"
    assert db_one(app, "SELECT COUNT(*) FROM audit_log WHERE action = 'approval.approve'")[0] == 1


def test_reject_sends_back_with_note(app, client, users):
    set_mode(app, "approval")
    login(client, "teacher")
    course = create_course(client, app, create_subject(client, app))
    lesson_id = create_lesson(client, app, course)
    client.post(f"/content/lessons/{lesson_id}/publish")
    client.post("/logout")
    login(client, "admin")
    req = db_one(app, "SELECT id FROM publish_requests")[0]
    client.post(f"/content/approvals/{req}/reject", data={"note": "Add a picture"})
    assert _status(app, lesson_id) == "draft"
    client.post("/logout")
    login(client, "teacher")
    assert b"Add a picture" in client.get(f"/content/lessons/{lesson_id}").data


def test_mode_direct(app, client, users):
    set_mode(app, "direct")
    login(client, "teacher")
    course = create_course(client, app, create_subject(client, app))
    lesson_id = create_lesson(client, app, course)
    client.post(f"/content/lessons/{lesson_id}/publish")
    assert _status(app, lesson_id) == "published"


def test_mode_mixed(app, client, users):
    set_mode(app, "mixed")
    class_id = make_class(app, "7A", teacher_ids=[users["teacher"]])
    login(client, "teacher")
    subject_id = create_subject(client, app)
    class_course = create_course(client, app, subject_id, "Class course", class_id=class_id)
    wide_course = create_course(client, app, subject_id, "Subject course")
    a = create_lesson(client, app, class_course, "A")
    b = create_lesson(client, app, wide_course, "B")
    client.post(f"/content/lessons/{a}/publish")
    client.post(f"/content/lessons/{b}/publish")
    assert _status(app, a) == "published"
    assert _status(app, b) == "pending"
    # Moving the class course to the whole subject sends published content back for approval.
    client.post(f"/content/courses/{class_course}/edit", data={"subject_id": subject_id, "title": "Class course",
                                                                "level": "", "description": ""})
    assert _status(app, a) == "pending"


def test_teacher_cannot_choose_class_they_do_not_teach(app, client, users):
    other = make_class(app, "Not mine")
    login(client, "teacher")
    subject_id = create_subject(client, app)
    response = client.post("/content/courses/new", data={"subject_id": subject_id, "title": "Sneaky", "class_id": other})
    assert response.status_code == 200 and b"only choose a class that you teach" in response.data
    assert db_one(app, "SELECT COUNT(*) FROM courses")[0] == 0


def test_editing_published_lesson_needs_reapproval(app, client, users):
    set_mode(app, "approval")
    login(client, "teacher")
    course = create_course(client, app, create_subject(client, app))
    lesson_id = create_lesson(client, app, course)
    client.post("/logout")
    login(client, "admin")
    client.post(f"/content/lessons/{lesson_id}/publish")  # admin publishes directly
    assert _status(app, lesson_id) == "published"
    client.post("/logout")
    login(client, "teacher")
    client.post(f"/content/lessons/{lesson_id}/edit", data={"title": "Lesson 1", "body": "changed"})
    assert _status(app, lesson_id) == "pending"
    upload_file(client, lesson_id)  # still pending, still one open request
    assert db_one(app, "SELECT COUNT(*) FROM publish_requests WHERE status = 'pending'")[0] == 1


def test_empty_lesson_cannot_be_published(app, as_teacher, course):
    set_mode(app, "direct")
    lesson_id = create_lesson(as_teacher, app, course, body="")
    as_teacher.post(f"/content/lessons/{lesson_id}/publish")
    assert _status(app, lesson_id) == "draft"


# ---------------------------------------------------------------- permissions

def test_teacher_cannot_edit_other_teachers_course(app, client, users):
    make_user(app, "teacher2", "teacher")
    login(client, "teacher")
    course = create_course(client, app, create_subject(client, app))
    lesson_id = create_lesson(client, app, course)
    client.post("/logout")
    login(client, "teacher2")
    assert client.get(f"/content/courses/{course}").status_code == 403
    assert client.post(f"/content/lessons/{lesson_id}/edit", data={"title": "hacked"}).status_code == 403
    assert client.post(f"/content/lessons/{lesson_id}/publish").status_code == 403
    assert client.post(f"/content/courses/{course}/delete").status_code == 403
    assert db_one(app, "SELECT title FROM lessons")[0] == "Lesson 1"


def test_class_teacher_can_manage_class_course(app, client, users):
    t2 = make_user(app, "teacher2", "teacher")
    class_id = make_class(app, "7A", teacher_ids=[users["teacher"], t2])
    login(client, "teacher")
    course = create_course(client, app, create_subject(client, app), class_id=class_id)
    client.post("/logout")
    login(client, "teacher2")
    assert client.get(f"/content/courses/{course}").status_code == 200


def test_student_refused_on_every_cms_route(app, client, users):
    login(client, "student")
    checked = 0
    for rule in app.url_map.iter_rules():
        if not rule.endpoint.startswith(("cms.", "progress.")) and not (
                rule.endpoint.startswith("quizzes.") and rule.rule.startswith(("/quizzes", "/content"))):
            continue
        url = rule.rule
        for arg in rule.arguments:
            url = url.replace(f"<int:{arg}>", "1").replace(f"<{arg}>", "up")
        for method in rule.methods & {"GET", "POST"}:
            assert client.open(url, method=method).status_code == 403, f"{method} {url}"
            checked += 1
    assert checked > 30


# ---------------------------------------------------------------- search

def test_search_and_filters(app, client, users):
    class_id = make_class(app, "7A", teacher_ids=[users["teacher"]])
    login(client, "teacher")
    maths = create_subject(client, app, "Maths")
    science = create_subject(client, app, "Science")
    c1 = create_course(client, app, maths, "Fractions", level="Grade 7")
    c2 = create_course(client, app, science, "Plants", class_id=class_id, level="Grade 8")
    client.post(f"/content/courses/{c1}/topics", data={"title": "Halves"})
    topic = db_one(app, "SELECT id FROM topics")[0]
    client.post(f"/content/courses/{c1}/lessons/new", data={"title": "Cutting pizza", "body": "numerator", "topic_id": topic},
                content_type="multipart/form-data")
    create_lesson(client, app, c2, "Photosynthesis", "sunlight")

    page = client.get("/content/?q=numerator").data.decode()
    assert "Cutting pizza" in page and "Photosynthesis" not in page
    page = client.get(f"/content/?subject={science}").data.decode()
    assert "Plants" in page and "Fractions" not in page
    page = client.get(f"/content/?class={class_id}").data.decode()
    assert "Plants" in page and "Fractions" not in page
    page = client.get("/content/?level=Grade+7").data.decode()
    assert "Fractions" in page and "Plants" not in page
    page = client.get("/content/?topic=Halv").data.decode()
    assert "Cutting pizza" in page and "Photosynthesis" not in page
    page = client.get("/content/?status=draft").data.decode()
    assert "Cutting pizza" in page and "Photosynthesis" in page
