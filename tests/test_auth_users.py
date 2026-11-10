"""Phase 1: authentication, role permissions, user and class management."""
import io

import pytest

from tests.conftest import PASSWORD, db_one, extract_csrf, login, make_user


def _rules(app, blueprint):
    """Every URL in a blueprint, with numeric placeholders filled in."""
    for rule in app.url_map.iter_rules():
        if rule.endpoint.startswith(blueprint + "."):
            url = rule.rule
            for arg in rule.arguments:
                url = url.replace(f"<int:{arg}>", "1").replace(f"<{arg}>", "x").replace(f"<path:{arg}>", "x")
            yield rule, url


# ---------------------------------------------------------------- login

def test_login_and_logout(client, users):
    response = login(client, "teacher")
    assert response.status_code == 302
    assert client.get("/dashboard").status_code == 200
    client.post("/logout")
    response = client.get("/dashboard")
    assert response.status_code == 302 and "/login" in response.headers["Location"]


def test_wrong_password_refused(client, users):
    response = login(client, "teacher", "nope")
    assert response.status_code == 401
    assert b"Wrong username or password" in response.data


def test_passwords_are_hashed(app, users):
    row = db_one(app, "SELECT password_hash FROM users WHERE username = 'teacher'")
    assert PASSWORD not in row[0]
    assert row[0].startswith(("pbkdf2:", "scrypt:"))


def test_inactive_user_cannot_login(app, client):
    make_user(app, "gone", "student", active=0)
    response = login(client, "gone")
    assert response.status_code == 401
    assert b"switched off" in response.data


def test_lockout_after_repeated_failures(client, users):
    for _ in range(5):
        login(client, "student", "bad")
    response = login(client, "student")  # correct password, but locked
    assert b"Too many wrong passwords" in response.data


def test_forced_password_change(app, client):
    make_user(app, "newbie", "student", must_change=1)
    response = login(client, "newbie")
    assert "/account/password" in response.headers["Location"]
    # Any other page sends them back to the password form.
    assert "/account/password" in client.get("/dashboard").headers["Location"]
    response = client.post("/account/password", data={
        "current_password": PASSWORD, "new_password": "brandnew1", "new_password2": "brandnew1"})
    assert response.status_code == 302
    assert client.get("/dashboard").status_code == 200
    assert db_one(app, "SELECT must_change_password FROM users WHERE username = 'newbie'")[0] == 0


def test_open_redirect_blocked(client, users):
    response = client.post("/login?next=https://evil.example/", data={"username": "admin", "password": PASSWORD})
    assert response.headers["Location"].endswith("/dashboard")


def test_first_run_setup_creates_admin(app, client):
    response = client.post("/setup", data={"name": "Head", "username": "head", "password": "longpass1",
                                           "password2": "longpass1"})
    assert response.status_code == 302
    assert db_one(app, "SELECT role FROM users WHERE username = 'head'")[0] == "admin"
    # Setup is closed once a user exists.
    assert "/login" in client.get("/setup").headers["Location"]


def test_registration_toggle(app, client, users):
    assert client.get("/register").status_code == 403
    login(client, "admin")
    client.post("/admin/settings", data={"publish_mode": "approval", "allow_registration": "1", "site_name": "STEMBox"})
    client.post("/logout")
    response = client.post("/register", data={"name": "Self Made", "username": "selfie",
                                              "password": "abcdef1", "password2": "abcdef1"})
    assert response.status_code == 302
    assert db_one(app, "SELECT role FROM users WHERE username = 'selfie'")[0] == "student"


# ---------------------------------------------------------------- role enforcement

@pytest.mark.parametrize("who", ["student", "teacher"])
def test_non_admin_refused_on_every_admin_route(app, client, users, who):
    login(client, who)
    checked = 0
    for rule, url in _rules(app, "users"):
        for method in rule.methods & {"GET", "POST"}:
            response = client.open(url, method=method)
            assert response.status_code == 403, f"{who} reached {method} {url}"
            checked += 1
    assert checked >= 15


def test_anonymous_redirected_to_login(app, client):
    for rule, url in _rules(app, "users"):
        if "GET" in rule.methods:
            response = client.get(url)
            assert response.status_code == 302 and "/login" in response.headers["Location"], url


def test_deactivated_user_session_ends(app, client, users):
    login(client, "student")
    assert client.get("/dashboard").status_code == 200
    from tests.conftest import db_exec
    db_exec(app, "UPDATE users SET active = 0 WHERE username = 'student'")
    assert client.get("/dashboard").status_code == 302


# ---------------------------------------------------------------- user management

def test_admin_creates_edits_deactivates_user(app, as_admin):
    response = as_admin.post("/admin/users/new", data={"name": "Mary Teacher", "username": "mary",
                                                        "role": "teacher", "password": ""})
    assert response.status_code == 302
    user = db_one(app, "SELECT * FROM users WHERE username = 'mary'")
    assert user["role"] == "teacher" and user["must_change_password"] == 1

    as_admin.post(f"/admin/users/{user['id']}/edit", data={"name": "Mary T", "username": "mary",
                                                           "role": "teacher", "active": "1"})
    assert db_one(app, "SELECT name FROM users WHERE id = ?", (user["id"],))[0] == "Mary T"

    as_admin.post(f"/admin/users/{user['id']}/active")
    assert db_one(app, "SELECT active FROM users WHERE id = ?", (user["id"],))[0] == 0
    assert db_one(app, "SELECT COUNT(*) FROM audit_log WHERE action = 'user.deactivate'")[0] == 1


def test_duplicate_username_rejected(app, as_admin):
    response = as_admin.post("/admin/users/new", data={"name": "X", "username": "teacher", "role": "teacher"})
    assert response.status_code == 200
    assert b"already taken" in response.data


def test_admin_cannot_deactivate_self_or_last_admin(app, as_admin, users):
    as_admin.post(f"/admin/users/{users['admin']}/active")
    assert db_one(app, "SELECT active FROM users WHERE id = ?", (users["admin"],))[0] == 1
    response = as_admin.post(f"/admin/users/{users['admin']}/edit",
                             data={"name": "Admin", "username": "admin", "role": "teacher", "active": "1"})
    assert b"only active administrator" in response.data


def test_reset_password(app, client, users):
    login(client, "admin")
    response = client.post(f"/admin/users/{users['student']}/reset-password", data={"password": "temp999"})
    assert b"temp999" in response.data
    client.post("/logout")
    response = login(client, "student", "temp999")
    assert "/account/password" in response.headers["Location"]


def test_password_reset_ends_existing_sessions(app, users):
    student = app.test_client()
    login(student, "student")
    admin = app.test_client()
    login(admin, "admin")
    admin.post(f"/admin/users/{users['student']}/reset-password", data={"password": "temp999"})
    assert student.get("/dashboard").status_code == 302


def test_csv_import(app, as_admin):
    as_admin.post("/admin/classes/new", data={"name": "Grade 7A", "level": "Grade 7"})
    csv_text = ("name,username,role,password,class\n"
                "Ann One,ann,student,,Grade 7A\n"
                "Bob Two,bob,teacher,goodpass1,\n"
                "Bad Role,badrole,wizard,,\n"
                "Dup,teacher,student,,\n"
                "No Class,nocls,student,,Grade 99\n")
    response = as_admin.post("/admin/users/import",
                             data={"file": (io.BytesIO(csv_text.encode()), "people.csv")},
                             content_type="multipart/form-data")
    assert response.status_code == 200
    page = response.data.decode()
    assert "Line 4" in page and "Line 5" in page and "Line 6" in page
    assert db_one(app, "SELECT COUNT(*) FROM users WHERE username IN ('ann', 'bob')")[0] == 2
    assert db_one(app, "SELECT COUNT(*) FROM users WHERE username = 'badrole'")[0] == 0
    assert db_one(app, "SELECT COUNT(*) FROM class_members m JOIN users u ON u.id = m.user_id "
                       "WHERE u.username = 'ann'")[0] == 1
    # Bob was given a password in the file.
    as_admin.post("/logout")
    assert "/account/password" in login(as_admin, "bob", "goodpass1").headers["Location"]


def test_csv_semicolon_and_missing_columns(app, as_admin):
    response = as_admin.post("/admin/users/import", data={"text": "name;username;role\nZed;zed;student\n"})
    assert db_one(app, "SELECT COUNT(*) FROM users WHERE username = 'zed'")[0] == 1
    response = as_admin.post("/admin/users/import", data={"text": "fullname,login\nA,b\n"})
    assert b"Missing" in response.data


# ---------------------------------------------------------------- classes

def test_class_membership(app, as_admin, users):
    as_admin.post("/admin/classes/new", data={"name": "Grade 8B", "level": "Grade 8"})
    class_id = db_one(app, "SELECT id FROM classes WHERE name = 'Grade 8B'")[0]
    as_admin.post(f"/admin/classes/{class_id}/members", data={"user_id": [users["teacher"], users["student"]]})
    rows = {r[0]: r[1] for r in __import__("tests.conftest", fromlist=["db_all"]).db_all(
        app, "SELECT user_id, role_in_class FROM class_members WHERE class_id = ?", (class_id,))}
    assert rows == {users["teacher"]: "teacher", users["student"]: "student"}
    # Admins cannot be added to a class.
    as_admin.post(f"/admin/classes/{class_id}/members", data={"user_id": [users["admin"]]})
    assert db_one(app, "SELECT COUNT(*) FROM class_members WHERE class_id = ?", (class_id,))[0] == 2
    as_admin.post(f"/admin/classes/{class_id}/members/{users['student']}/remove")
    assert db_one(app, "SELECT COUNT(*) FROM class_members WHERE class_id = ?", (class_id,))[0] == 1
    page = as_admin.get(f"/admin/classes/{class_id}").data.decode()
    assert "Teacher" in page
    as_admin.post(f"/admin/classes/{class_id}/delete")
    assert db_one(app, "SELECT COUNT(*) FROM classes")[0] == 0
    assert db_one(app, "SELECT COUNT(*) FROM users")[0] == 3


def test_settings_publish_mode_saved(app, as_admin):
    as_admin.post("/admin/settings", data={"publish_mode": "mixed", "site_name": "School Box"})
    assert db_one(app, "SELECT value FROM settings WHERE key = 'publish_mode'")[0] == "mixed"
    assert b"School Box" in as_admin.get("/dashboard").data
    as_admin.post("/admin/settings", data={"publish_mode": "anarchy"})
    assert db_one(app, "SELECT value FROM settings WHERE key = 'publish_mode'")[0] == "mixed"


# ---------------------------------------------------------------- CSRF

def test_csrf_required_on_post(tmp_path):
    from app import create_app
    app = create_app({"TESTING": True, "SECRET_KEY": "x", "STORAGE_DIR": str(tmp_path / "s"),
                      "PACKAGES_DIR": str(tmp_path / "p"), "PASSWORD_METHOD": "pbkdf2:sha256:1000"})
    make_user(app, "admin", "admin")
    client = app.test_client()
    # Without a token the login form is refused.
    response = client.post("/login", data={"username": "admin", "password": PASSWORD})
    assert response.status_code == 400
    token = extract_csrf(client.get("/login").data.decode())
    response = client.post("/login", data={"username": "admin", "password": PASSWORD, "csrf_token": token})
    assert response.status_code == 302
