"""Correction round: admin-only accounts, password rules, sessions, student dashboard redesign,
asset optimisation, input validation and protection of student records."""
import io
import re
import sqlite3
import time
from pathlib import Path

import pytest

from app import create_app
from tests.conftest import PASSWORD, db_one, login, make_user
from tests.helpers import make_class, published_course

APP_DIR = Path(__file__).resolve().parent.parent / "app"


# ---------------------------------------------------------------- database migration

def test_migration_upgrades_existing_database(tmp_path):
    """A database from the previous version (schema 1, with the old registration setting)
    is upgraded in place without losing data."""
    storage = tmp_path / "storage"
    storage.mkdir()
    db_path = storage / "stembox.db"
    conn = sqlite3.connect(db_path)
    conn.executescript((APP_DIR / "schema.sql").read_text(encoding="utf-8"))
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO settings VALUES ('allow_registration', '1'), ('publish_mode', 'direct')")
    conn.execute("INSERT INTO users (uuid, name, username, password_hash, role, created_at) "
                 "VALUES ('u1', 'Old User', 'old', 'x', 'student', '2026')")
    conn.commit()
    conn.close()

    create_app({"STORAGE_DIR": str(storage), "PACKAGES_DIR": str(tmp_path / "p"), "SECRET_KEY": "x"})
    conn = sqlite3.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM settings WHERE key = 'allow_registration'").fetchone()[0] == 0
    assert conn.execute("SELECT value FROM settings WHERE key = 'publish_mode'").fetchone()[0] == "direct"
    assert conn.execute("SELECT name FROM users").fetchone()[0] == "Old User"
    conn.close()


# ---------------------------------------------------------------- accounts & passwords

def test_staff_need_longer_passwords(app, as_admin, users):
    # Teacher with a 7-character password: refused. Student with 6: accepted.
    response = as_admin.post("/admin/users/new", data={"name": "T", "username": "shortt", "role": "teacher",
                                                        "password": "seven77"})
    assert b"at least 8 characters" in response.data
    assert db_one(app, "SELECT COUNT(*) FROM users WHERE username = 'shortt'")[0] == 0
    as_admin.post("/admin/users/new", data={"name": "S", "username": "shorts", "role": "student", "password": "six666"})
    assert db_one(app, "SELECT COUNT(*) FROM users WHERE username = 'shorts'")[0] == 1
    # Reset a teacher's password to something too short: refused.
    response = as_admin.post(f"/admin/users/{users['teacher']}/reset-password", data={"password": "seven77"})
    assert b"at least 8 characters" in response.data
    # CSV import applies the same rule per role.
    response = as_admin.post("/admin/users/import", data={"text": "name,username,role,password\nT2,tt2,teacher,short1\n"})
    assert b"Line 2" in response.data and b"at least 8" in response.data


def test_teacher_change_password_rule(app, client, users):
    login(client, "teacher")
    response = client.post("/account/password", data={"current_password": PASSWORD, "new_password": "seven77",
                                                      "new_password2": "seven77"})
    assert b"at least 8 characters" in response.data
    response = client.post("/account/password", data={"current_password": PASSWORD, "new_password": "eightchr",
                                                      "new_password2": "eightchr"})
    assert response.status_code == 302


def test_generated_passwords_meet_every_rule(app):
    from app.auth import generate_temp_password, password_problem
    for _ in range(50):
        pw = generate_temp_password()
        assert password_problem(pw, "admin") is None and password_problem(pw, "student") is None


@pytest.mark.parametrize("role, marker", [("admin", "Administrator dashboard"), ("teacher", "My classes"),
                                          ("student", "My courses")])
def test_login_goes_to_role_dashboard(app, client, users, role, marker):
    response = login(client, role)
    assert response.headers["Location"].endswith("/dashboard")
    assert marker in client.get("/dashboard").data.decode()


def test_admin_activates_and_deactivates(app, as_admin, users):
    as_admin.post(f"/admin/users/{users['student']}/active")
    assert db_one(app, "SELECT active FROM users WHERE id = ?", (users["student"],))[0] == 0
    as_admin.post(f"/admin/users/{users['student']}/active")
    assert db_one(app, "SELECT active FROM users WHERE id = ?", (users["student"],))[0] == 1


# ---------------------------------------------------------------- sessions

def test_session_cookie_flags(app, client, users):
    response = login(client, "student")
    cookie = response.headers["Set-Cookie"]
    assert cookie.startswith("stembox_session=")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


def test_idle_session_is_logged_out(app, client, users):
    app.config["SESSION_IDLE_MINUTES"] = 30
    login(client, "student")
    assert client.get("/dashboard").status_code == 200
    with client.session_transaction() as sess:
        sess["seen"] = int(time.time()) - 31 * 60
    response = client.get("/dashboard")
    assert response.status_code == 302 and "/login" in response.headers["Location"]
    assert b"not used for a while" in client.get("/login").data


def test_active_session_stays_logged_in(app, client, users):
    app.config["SESSION_IDLE_MINUTES"] = 30
    login(client, "student")
    with client.session_transaction() as sess:
        sess["seen"] = int(time.time()) - 29 * 60
    assert client.get("/dashboard").status_code == 200


# ---------------------------------------------------------------- student records

def test_student_records_protected(app, client, users):
    content = published_course(app, client, users, mode="direct")
    other_teacher = make_user(app, "teacher2", "teacher")
    classmate = make_user(app, "classmate", "student")
    make_class(app, "7A", teacher_ids=[users["teacher"]], student_ids=[users["student"], classmate])
    login(client, "student")
    attempt = client.post(f"/quiz/{content['quiz']}/start").headers["Location"].split("/")[-1]
    client.post(f"/attempt/{attempt}/submit", data={})
    client.post("/logout")
    # Another student cannot see the attempt or any progress/admin page.
    login(client, "classmate")
    assert client.get(f"/attempt/{attempt}").status_code == 404
    for url in (f"/progress/student/{users['student']}", "/progress/", f"/admin/users/{users['student']}/edit",
                "/admin/users"):
        assert client.get(url).status_code == 403, url
    client.post("/logout")
    # A teacher who does not teach the student's class cannot see the records.
    login(client, "teacher2")
    assert client.get(f"/progress/student/{users['student']}").status_code == 403
    assert client.get(f"/attempt/{attempt}").status_code == 403
    assert client.get(f"/admin/users/{users['student']}/edit").status_code == 403
    client.post("/logout")
    # The class teacher can.
    login(client, "teacher")
    assert client.get(f"/progress/student/{users['student']}").status_code == 200
    assert client.get(f"/attempt/{attempt}").status_code == 200


# ---------------------------------------------------------------- input validation

def test_overlong_input_rejected(app, client, users):
    login(client, "admin")
    response = client.post("/admin/users/new", data={"name": "x" * 101, "username": "longname", "role": "student"})
    assert b"too long" in response.data
    assert db_one(app, "SELECT COUNT(*) FROM users WHERE username = 'longname'")[0] == 0
    client.post("/admin/classes/new", data={"name": "c" * 101})
    assert db_one(app, "SELECT COUNT(*) FROM classes")[0] == 0
    client.post("/content/subjects/new", data={"name": "Maths"})
    subject = db_one(app, "SELECT id FROM subjects")[0]
    response = client.post("/content/courses/new", data={"subject_id": subject, "title": "t" * 201})
    assert b"too long" in response.data and db_one(app, "SELECT COUNT(*) FROM courses")[0] == 0
    client.post("/content/courses/new", data={"subject_id": subject, "title": "Ok"})
    course = db_one(app, "SELECT id FROM courses")[0]
    client.post(f"/content/courses/{course}/quizzes/new", data={"title": "Q", "pass_mark": "50"})
    quiz = db_one(app, "SELECT id FROM quizzes")[0]
    response = client.post(f"/quizzes/{quiz}/questions", data={"text": "q" * 2001, "choice_0": "a", "choice_1": "b",
                                                               "correct": "0"})
    assert response.status_code == 400 and b"too long" in response.data


# ---------------------------------------------------------------- student dashboard design

def test_student_dashboard_redesign(app, client, users):
    published_course(app, client, users, mode="direct")
    login(client, "student")
    page = client.get("/dashboard").data.decode()
    assert "Hi, Student!" in page                       # friendly greeting with first name
    assert "/static/img/3d/hero.svg" in page             # 3D hero illustration
    assert "/static/img/3d/math.svg" in page             # subject artwork for "Maths › Fractions"
    assert 'class="s-stats"' in page and "lessons finished" in page and "quizzes passed" in page
    assert 'icons.svg' in page and "#home" in page     # icons in the student menu
    for img in re.findall(r'<img[^>]+/img/3d/[^>]+>', page):
        assert 'width="' in img and 'height="' in img   # no layout jump while loading


def test_illustrations_only_on_student_dashboard(app, client, users):
    content = published_course(app, client, users, mode="direct")
    for role in ("admin", "teacher"):
        login(client, role)
        for url in ("/dashboard", "/content/", f"/content/courses/{content['course']}", "/progress/"):
            page = client.get(url).data.decode()
            assert "/img/3d/" not in page, (role, url)
            assert "icons.svg#" not in page, (role, url)   # teacher/admin menus stay plain
        client.post("/logout")
    login(client, "student")
    for url in ("/learn", f"/learn/course/{content['course']}", f"/learn/lesson/{content['lesson']}"):
        assert "/img/3d/" not in client.get(url).data.decode(), url


def test_illustrations_are_small_and_valid():
    import xml.etree.ElementTree as ET
    files = list((APP_DIR / "static" / "img" / "3d").glob("*.svg"))
    assert len(files) >= 8
    for path in files:
        assert path.stat().st_size < 6 * 1024, path.name
        root = ET.parse(path).getroot()                    # well-formed XML
        assert root.get("viewBox")
        text = path.read_text(encoding="utf-8")
        assert "<script" not in text and "href=\"http" not in text


# ---------------------------------------------------------------- performance

def test_static_assets_cached_and_versioned(app, client, users):
    login(client, "student")
    page = client.get("/dashboard").data.decode()
    css = re.search(r'href="(/static/css/stembox\.css\?v=\d+)"', page)
    assert css, "stylesheet link should carry a version"
    response = client.get(css.group(1))
    assert "max-age=604800" in response.headers["Cache-Control"]
    font = re.search(r'href="(/static/fonts/[^"]+\.woff2\?v=\d+)"', page)
    assert font
    response = client.get(font.group(1))
    assert response.status_code == 200 and response.data[:4] == b"wOF2"
    assert len(response.data) < 64 * 1024


def test_no_autoplay_anywhere():
    for path in (APP_DIR / "templates").rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert "autoplay" not in text, path
        for tag in re.findall(r"<video[^>]*>", text):
            assert 'preload="metadata"' in tag, path


def test_no_css_animations():
    css = (APP_DIR / "static" / "css" / "stembox.css").read_text(encoding="utf-8")
    assert "@keyframes" not in css and "animation:" not in css and "transition:" not in css
