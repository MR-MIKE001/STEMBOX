"""Integration: the real server (run.py + Waitress) on 0.0.0.0, several devices at once,
CSRF on real forms, and data kept after a restart (acceptance tests 8 and 10)."""
import http.cookiejar
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from app import create_app
from app.network import lan_addresses
from tests.conftest import db_exec, db_one

ROOT = Path(__file__).resolve().parent.parent


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(storage, port):
    env = {**os.environ, "STEMBOX_STORAGE_DIR": str(storage), "STEMBOX_PACKAGES_DIR": str(storage / "packages"),
           "STEMBOX_PORT": str(port), "STEMBOX_HOST": "0.0.0.0", "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen([sys.executable, "run.py"], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
                if r.status == 200:
                    return proc
        except OSError:
            time.sleep(0.3)
    proc.kill()
    raise RuntimeError("server did not start: " + proc.stdout.read().decode(errors="ignore"))


def _stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


class Device:
    """A browser on a phone/tablet/computer: its own cookies, real CSRF tokens."""

    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar),
                                                  urllib.request.ProxyHandler({}))

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        with self.opener.open(req, timeout=20) as r:
            return r.status, r.read(), dict(r.headers)

    def post_form(self, path, data, token_from):
        _s, html, _h = self.get(token_from)
        token = re.search(rb'name="csrf_token" value="([^"]+)"', html).group(1).decode()
        body = urllib.parse.urlencode({**data, "csrf_token": token}).encode()
        with self.opener.open(urllib.request.Request(self.base + path, data=body), timeout=20) as r:
            return r.status, r.read()

    def login(self, username, password):
        _status, html = self.post_form("/login", {"username": username, "password": password}, "/login")
        assert b"Wrong username" not in html, username


@pytest.fixture
def storage(tmp_path):
    storage = tmp_path / "storage"
    app = create_app({"STORAGE_DIR": str(storage), "PACKAGES_DIR": str(storage / "packages")})
    result = app.test_cli_runner().invoke(args=["seed-dev"])
    assert result.exit_code == 0, result.output
    db_exec(app, "UPDATE users SET must_change_password = 0")
    return storage


def test_several_devices_over_lan_and_restart(storage):
    port = _free_port()
    proc = _start(storage, port)
    try:
        hosts = lan_addresses() or ["127.0.0.1"]
        base = f"http://{hosts[0]}:{port}"
        try:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(base + "/api/ping", timeout=5)
        except OSError:  # LAN interface blocked by a firewall on this machine: fall back to loopback
            base = f"http://127.0.0.1:{port}"
        print(f"\nSTEMBox server under test reachable at {base}")

        app = create_app({"STORAGE_DIR": str(storage), "PACKAGES_DIR": str(storage / "packages")})
        lesson_id = db_one(app, "SELECT id FROM lessons WHERE title = 'What is a fraction?'")[0]
        pdf_uuid = db_one(app, "SELECT uuid FROM resources WHERE type = 'pdf' AND lesson_id = ?", (lesson_id,))[0]

        errors = []

        def student_session(username):
            try:
                d = Device(base)
                d.login(username, "student123")
                status, html, _ = d.get(f"/learn/lesson/{lesson_id}")
                assert status == 200 and b"What is a fraction?" in html
                status, data, headers = d.get(f"/media/{pdf_uuid}", headers={"Range": "bytes=0-4"})
                assert status == 206 and data == b"%PDF-"
                d.post_form(f"/learn/lesson/{lesson_id}/complete", {"done": "1"}, f"/learn/lesson/{lesson_id}")
            except Exception as exc:  # noqa: BLE001 - report all failures from threads
                errors.append(f"{username}: {exc!r}")

        threads = [threading.Thread(target=student_session, args=(u,)) for u in ("student1", "student2", "student1")]
        admin = Device(base)
        admin.login("admin", "admin123")
        for t in threads:
            t.start()
        status, html, _ = admin.get("/dashboard")
        assert status == 200 and b"Connect other devices" in html
        for t in threads:
            t.join(30)
        assert errors == []

        # A form without its CSRF token is refused by the real server.
        with pytest.raises(urllib.error.HTTPError) as exc:
            admin.opener.open(urllib.request.Request(base + "/admin/settings",
                                                     data=b"publish_mode=direct"), timeout=10)
        assert exc.value.code == 400
    finally:
        _stop(proc)

    # Restart: everything is still there and the old login session still works.
    proc = _start(storage, port)
    try:
        app = create_app({"STORAGE_DIR": str(storage), "PACKAGES_DIR": str(storage / "packages")})
        assert db_one(app, "SELECT COUNT(*) FROM progress WHERE completed_at IS NOT NULL")[0] == 2
        # The admin's session cookie from before the restart is still valid (secret key is kept on disk).
        status, html, _ = admin.get("/dashboard")
        assert status == 200 and b"Administrator dashboard" in html
        d = Device(base)
        d.login("student1", "student123")
        status, html, _ = d.get("/my-results")
        assert b"What is a fraction?" in html
    finally:
        _stop(proc)
