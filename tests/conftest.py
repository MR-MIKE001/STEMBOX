import io
import re

import pytest

from app import create_app
from app.db import connect, new_uuid, now

PASSWORD = "secret123"


@pytest.fixture
def app(tmp_path):
    from app import auth
    auth._failures.clear()  # the login lockout counter is process-wide
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "STORAGE_DIR": str(tmp_path / "storage"),
        "PACKAGES_DIR": str(tmp_path / "packages"),
        "DATABASE": str(tmp_path / "storage" / "test.db"),
        "WTF_CSRF_ENABLED": False,
        "PASSWORD_METHOD": "pbkdf2:sha256:1000",  # fast hashing in tests
    })
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


def make_user(app, username, role="student", password=PASSWORD, name=None, active=1, must_change=0):
    from app.auth import hash_password
    with app.app_context():
        conn = connect(app.config["DATABASE"])
        cur = conn.execute(
            "INSERT INTO users (uuid, name, username, password_hash, role, active, must_change_password, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (new_uuid(), name or username.title(), username, hash_password(password), role, active, must_change, now()))
        conn.commit()
        conn.close()
        return cur.lastrowid


def login(client, username, password=PASSWORD):
    return client.post("/login", data={"username": username, "password": password})


def db_one(app, sql, args=()):
    conn = connect(app.config["DATABASE"])
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


def db_all(app, sql, args=()):
    conn = connect(app.config["DATABASE"])
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def db_exec(app, sql, args=()):
    conn = connect(app.config["DATABASE"])
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


@pytest.fixture
def users(app):
    """One user of each role: admin, teacher, student (already active, no forced reset)."""
    return {
        "admin": make_user(app, "admin", "admin"),
        "teacher": make_user(app, "teacher", "teacher"),
        "student": make_user(app, "student", "student"),
    }


@pytest.fixture
def as_admin(client, users):
    login(client, "admin")
    return client


@pytest.fixture
def as_teacher(client, users):
    login(client, "teacher")
    return client


@pytest.fixture
def as_student(client, users):
    login(client, "student")
    return client


# Minimal valid file contents for upload tests.
PDF_BYTES = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 2000
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
MP3_BYTES = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 100


def upload(data, name):
    return (io.BytesIO(data), name)


def extract_csrf(html):
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return match.group(1) if match else None
