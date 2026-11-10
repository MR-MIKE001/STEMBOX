"""Phase 3: students browse, read lessons, view files (with Range support) and mark lessons complete."""
import pytest

from tests.conftest import MP4_BYTES, PDF_BYTES, db_one, login, make_user
from tests.helpers import create_course, create_lesson, create_subject, make_class, published_course, set_mode


@pytest.fixture
def content(app, client, users):
    return published_course(app, client, users, mode="direct")


def _resource(app, rtype):
    return db_one(app, "SELECT * FROM resources WHERE type = ?", (rtype,))


def test_student_browses_to_lesson(app, client, users, content):
    login(client, "student")
    assert b"Maths" in client.get("/learn").data
    assert b"Fractions" in client.get(f"/learn/subject/{content['subject']}").data
    page = client.get(f"/learn/course/{content['course']}").data.decode()
    assert "Lesson 1" in page and "Check" in page
    page = client.get(f"/learn/lesson/{content['lesson']}").data.decode()
    assert "<strong>text</strong>" in page
    assert "<video" in page and "Read the PDF" in page


def test_student_cannot_see_drafts(app, client, users, content):
    login(client, "teacher")
    draft = create_lesson(client, app, content["course"], "Secret draft")
    client.post("/logout")
    login(client, "student")
    assert client.get(f"/learn/lesson/{draft}").status_code == 404
    assert b"Secret draft" not in client.get(f"/learn/course/{content['course']}").data


def test_class_course_only_for_that_class(app, client, users):
    set_mode(app, "direct")
    outsider = make_user(app, "outsider", "student")
    class_id = make_class(app, "7A", teacher_ids=[users["teacher"]], student_ids=[users["student"]])
    login(client, "teacher")
    course = create_course(client, app, create_subject(client, app), "Class only", class_id=class_id)
    lesson = create_lesson(client, app, course)
    client.post(f"/content/lessons/{lesson}/publish")
    client.post("/logout")

    login(client, "student")
    assert client.get(f"/learn/lesson/{lesson}").status_code == 200
    client.post("/logout")
    login(client, "outsider")
    assert client.get(f"/learn/course/{course}").status_code == 403
    assert client.get(f"/learn/lesson/{lesson}").status_code == 403
    assert b"Class only" not in client.get(f"/learn/subject/{db_one(app, 'SELECT id FROM subjects')[0]}").data


def test_media_full_and_range_requests(app, client, users, content):
    login(client, "student")
    video = _resource(app, "video")
    response = client.get(f"/media/{video['uuid']}")
    assert response.status_code == 200
    assert response.data == MP4_BYTES
    assert response.headers["Content-Type"] == "video/mp4"
    assert response.headers["Accept-Ranges"] == "bytes"

    response = client.get(f"/media/{video['uuid']}", headers={"Range": "bytes=100-199"})
    assert response.status_code == 206
    assert response.data == MP4_BYTES[100:200]
    assert response.headers["Content-Range"] == f"bytes 100-199/{len(MP4_BYTES)}"

    pdf = _resource(app, "pdf")
    response = client.get(f"/media/{pdf['uuid']}?download=1")
    assert response.data == PDF_BYTES
    assert "attachment" in response.headers["Content-Disposition"]


def test_media_protected(app, client, users, content):
    video = _resource(app, "video")
    assert client.get(f"/media/{video['uuid']}").status_code == 302  # not logged in
    login(client, "teacher")
    client.post(f"/content/lessons/{content['lesson']}/unpublish")
    client.post("/logout")
    login(client, "student")
    assert client.get(f"/media/{video['uuid']}").status_code == 404
    assert client.get("/media/not-a-real-uuid").status_code == 404


def test_viewer_pages(app, client, users, content):
    login(client, "student")
    page = client.get(f"/learn/resource/{_resource(app, 'pdf')['id']}").data.decode()
    assert 'type="application/pdf"' in page and "Download" in page
    page = client.get(f"/learn/resource/{_resource(app, 'video')['id']}").data.decode()
    assert "<video" in page and "data-save-position" in page


def test_mark_complete_and_undo(app, client, users, content):
    login(client, "student")
    client.get(f"/learn/lesson/{content['lesson']}")
    row = db_one(app, "SELECT * FROM progress WHERE user_id = ?", (users["student"],))
    assert row["completed_at"] is None and row["last_viewed_at"]
    client.post(f"/learn/lesson/{content['lesson']}/complete", data={"done": "1"})
    assert db_one(app, "SELECT completed_at FROM progress")[0] is not None
    page = client.get(f"/learn/course/{content['course']}").data.decode()
    assert "finished 1 of 1" in page
    client.post(f"/learn/lesson/{content['lesson']}/complete", data={"done": "0"})
    assert db_one(app, "SELECT completed_at FROM progress")[0] is None


def test_staff_progress_not_recorded(app, client, users, content):
    login(client, "teacher")
    client.get(f"/learn/lesson/{content['lesson']}")
    client.post(f"/learn/lesson/{content['lesson']}/complete")
    assert db_one(app, "SELECT COUNT(*) FROM progress")[0] == 0


def test_video_position_saved_and_restored(app, client, users, content):
    login(client, "student")
    video = _resource(app, "video")
    response = client.post(f"/learn/resource/{video['id']}/position", data={"position": "42"})
    assert response.get_json() == {"ok": True, "saved": True}
    page = client.get(f"/learn/resource/{video['id']}").data.decode()
    assert 'data-start="42"' in page


def test_student_search(app, client, users, content):
    login(client, "student")
    assert b"Lesson 1" in client.get("/learn/search?q=text").data
    assert b"Lesson 1" not in client.get("/learn/search?q=zebra").data
