"""Phase 6: works with no internet; no external assets; binds to all interfaces."""
import re
import socket
from pathlib import Path

import pytest

from tests.conftest import db_exec, db_one, login
from tests.helpers import published_course

APP_DIR = Path(__file__).resolve().parent.parent / "app"
EXTERNAL = re.compile(r"""(?:src|href|action|data)\s*=\s*["'](?:https?:)?//|url\(\s*["']?(?:https?:)?//|@import""", re.I)


def test_no_external_urls_in_templates_or_static():
    offenders = []
    for path in list((APP_DIR / "templates").rglob("*.html")) + list((APP_DIR / "static").rglob("*")):
        if path.is_dir():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if EXTERNAL.search(text):
            offenders.append(str(path))
        # The only absolute URL allowed is the SVG namespace identifier (not a network request).
        for url in re.findall(r"https?://[^\s\"'<>)]+", text):
            if url != "http://www.w3.org/2000/svg":
                offenders.append(f"{path}: {url}")
    assert offenders == []


def _all_get_pages(app, client):
    """Every parameter-free GET page plus the main detail pages, for the logged-in user."""
    urls = []
    for rule in app.url_map.iter_rules():
        if "GET" in rule.methods and not rule.arguments and not rule.rule.startswith(("/static", "/api")) \
                and not rule.rule.endswith((".csv",)):
            urls.append(rule.rule)
    return urls


@pytest.fixture
def seeded(app):
    app.test_cli_runner().invoke(args=["seed-dev"])
    db_exec(app, "UPDATE users SET must_change_password = 0")
    return app


@pytest.mark.parametrize("who, password", [("admin", "admin123"), ("teacher1", "teacher123"), ("student1", "student123")])
def test_rendered_pages_load_nothing_external(seeded, client, who, password):
    login(client, who, password)
    ids = {k: db_one(seeded, f"SELECT MIN(id) FROM {t}")[0] for k, t in
           (("course", "courses"), ("lesson", "lessons"), ("quiz", "quizzes"), ("resource", "resources"),
            ("subject", "subjects"))}
    urls = _all_get_pages(seeded, client) + [
        f"/learn/course/{ids['course']}", f"/learn/lesson/{ids['lesson']}", f"/learn/resource/{ids['resource']}",
        f"/learn/subject/{ids['subject']}", f"/quiz/{ids['quiz']}"]
    if who != "student":
        urls += [f"/content/courses/{ids['course']}", f"/content/lessons/{ids['lesson']}", f"/quizzes/{ids['quiz']}"]
    checked = 0
    for url in urls:
        response = client.get(url)
        if response.status_code != 200 or response.mimetype != "text/html":
            continue
        html = response.data.decode()
        assert not EXTERNAL.search(html), url
        assert "default-src 'self'" in response.headers["Content-Security-Policy"]
        assert '<meta name="viewport"' in html
        checked += 1
    assert checked >= 8


def test_full_flow_with_network_disconnected(app, client, users, monkeypatch):
    """Block every non-local network connection, then use STEMBox end to end."""
    content = published_course(app, client, users, mode="direct")
    real_connect = socket.socket.connect
    blocked = []

    def guarded_connect(sock, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):
            blocked.append(host)
            raise OSError("Network is unreachable (test: internet disconnected)")
        return real_connect(sock, address)

    def no_dns(*args, **kwargs):
        raise socket.gaierror("no DNS (test: internet disconnected)")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    monkeypatch.setattr(socket, "gethostbyname_ex", no_dns)

    login(client, "admin")
    page = client.get("/dashboard").data.decode()
    assert "not connected to a network" in page  # handled gracefully, no crash
    client.post("/logout")

    login(client, "student")
    assert client.get(f"/learn/lesson/{content['lesson']}").status_code == 200
    video = db_one(app, "SELECT uuid FROM resources WHERE type = 'video'")[0]
    assert client.get(f"/media/{video}", headers={"Range": "bytes=0-99"}).status_code == 206
    client.post(f"/learn/lesson/{content['lesson']}/complete", data={"done": "1"})
    response = client.post(f"/quiz/{content['quiz']}/start")
    attempt_id = int(response.headers["Location"].split("/")[-1])
    client.post(f"/attempt/{attempt_id}/submit", data={})
    assert db_one(app, "SELECT finished_at FROM attempts")[0] is not None
    assert db_one(app, "SELECT completed_at FROM progress")[0] is not None


def test_default_binding_is_all_interfaces(app):
    assert app.config["HOST"] == "0.0.0.0"
    assert app.config["PORT"] == 5000


def test_lan_addresses_are_private_ipv4():
    import ipaddress
    from app.network import lan_addresses
    for ip in lan_addresses():
        addr = ipaddress.ip_address(ip)
        assert addr.version == 4 and not addr.is_loopback
