"""Phase 7: USB export/import, package validation and Wi-Fi transfer between two installations."""
import hashlib
import io
import json
import urllib.error
import zipfile
from urllib.parse import urlparse

import pytest

from app import create_app
from tests.conftest import MP4_BYTES, PDF_BYTES, db_all, db_one, db_exec, login, make_user
from tests.helpers import published_course


def _second_app(tmp_path, name="box2"):
    from app import auth
    auth._failures.clear()
    return create_app({
        "TESTING": True, "SECRET_KEY": name, "WTF_CSRF_ENABLED": False,
        "STORAGE_DIR": str(tmp_path / name / "storage"), "PACKAGES_DIR": str(tmp_path / name / "packages"),
        "PASSWORD_METHOD": "pbkdf2:sha256:1000",
    })


@pytest.fixture
def source(app, client, users):
    content = published_course(app, client, users, mode="direct")
    login(client, "admin")
    return content


def _export(client, app, content, **extra):
    response = client.post("/transfer/export", data={"course_id": content["course"], **extra})
    assert response.status_code == 200 and response.mimetype == "application/zip"
    return response.data


def _import(client, data, name="pkg.zip"):
    return client.post("/transfer/import", data={"package": (io.BytesIO(data), name)}, content_type="multipart/form-data")


def _rewrite(data, change, fix_checksums=True):
    """Copy a package, letting `change(name, bytes)` return new bytes (or None to drop the entry)."""
    src = zipfile.ZipFile(io.BytesIO(data))
    entries = {}
    for info in src.infolist():
        if info.filename == "checksums.sha256":
            continue
        new = change(info.filename, src.read(info.filename))
        if new is not None:
            entries[info.filename] = new
    extra = change("__extra__", None)
    if extra:
        entries.update(extra)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
        if fix_checksums:
            sums = "".join(f"{hashlib.sha256(p).hexdigest()}  {n}\n" for n, p in entries.items())
        else:
            sums = src.read("checksums.sha256").decode()
        zf.writestr("checksums.sha256", sums)
    return out.getvalue()


# ---------------------------------------------------------------- export

def test_export_package_layout(app, client, source):
    data = _export(client, app, source)
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = set(zf.namelist())
    assert {"manifest.json", "checksums.sha256", "data/content.json"} <= names
    assert len([n for n in names if n.startswith("files/")]) == 2
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["format"] == "stembox-package" and manifest["version"] == 1
    assert manifest["package_uuid"] and manifest["created_at"]
    kinds = sorted(i["type"] for i in manifest["items"])
    assert kinds == ["course", "lesson", "quiz", "resource", "resource", "subject"]
    for line in zf.read("checksums.sha256").decode().splitlines():
        digest, name = line.split("  ", 1)
        assert hashlib.sha256(zf.read(name)).hexdigest() == digest
    assert db_one(app, "SELECT COUNT(*) FROM audit_log WHERE action = 'package.export'")[0] == 1


def test_drafts_only_exported_on_request(app, client, source):
    from tests.helpers import create_lesson
    create_lesson(client, app, source["course"], "Draft lesson")
    content = json.loads(zipfile.ZipFile(io.BytesIO(_export(client, app, source))).read("data/content.json"))
    assert [l["title"] for l in content["lessons"]] == ["Lesson 1"]
    content = json.loads(zipfile.ZipFile(io.BytesIO(_export(client, app, source, include_drafts="1"))).read("data/content.json"))
    assert sorted(l["title"] for l in content["lessons"]) == ["Draft lesson", "Lesson 1"]


# ---------------------------------------------------------------- import

def test_import_into_second_installation(app, client, source, tmp_path):
    data = _export(client, app, source)
    box2 = _second_app(tmp_path)
    make_user(box2, "admin2", "admin")
    make_user(box2, "pupil", "student")
    c2 = box2.test_client()
    login(c2, "admin2")
    response = _import(c2, data)
    assert response.status_code == 200
    page = response.data.decode()
    assert "Content loaded" in page
    assert db_one(box2, "SELECT COUNT(*) FROM lessons WHERE status = 'published'")[0] == 1
    assert db_one(box2, "SELECT COUNT(*) FROM resources")[0] == 2
    assert db_one(box2, "SELECT COUNT(*) FROM questions")[0] == 2
    # UUIDs are preserved so later imports can recognise the same items.
    assert db_one(box2, "SELECT uuid FROM lessons")[0] == db_one(app, "SELECT uuid FROM lessons")[0]
    # A student on the second box can watch the imported video.
    c2.post("/logout")
    login(c2, "pupil")
    video = db_one(box2, "SELECT uuid FROM resources WHERE type = 'video'")[0]
    assert c2.get(f"/media/{video}").data == MP4_BYTES
    assert db_one(box2, "SELECT COUNT(*) FROM content_packages")[0] == 1


def test_reimport_skips_and_changes_update(app, client, source, tmp_path):
    first = _export(client, app, source)
    box2 = _second_app(tmp_path)
    make_user(box2, "admin2", "admin")
    c2 = box2.test_client()
    login(c2, "admin2")
    _import(c2, first)
    response = _import(c2, first)
    assert "6</div><div class=\"stat-label\">skipped" in response.data.decode().replace("\n", "").replace("  ", "")
    assert db_one(box2, "SELECT COUNT(*) FROM lessons")[0] == 1

    # Change the lesson text on the source and send it again.
    client.post(f"/content/lessons/{source['lesson']}/edit", data={"title": "Lesson 1", "body": "New text"})
    client.post(f"/content/lessons/{source['lesson']}/publish")
    second = _export(client, app, source)
    from app.sync.package import import_package
    path = tmp_path / "second.zip"
    path.write_bytes(second)
    with box2.app_context():
        report = import_package(path, source="test", user_id=None)
    assert [i["type"] for i in report["updated"]] == ["lesson"]
    assert len(report["skipped"]) == 5 and not report["added"] and not report["rejected"]
    assert db_one(box2, "SELECT body FROM lessons")[0] == "New text"


def test_subject_with_same_name_is_merged(app, client, source, tmp_path):
    data = _export(client, app, source)
    box2 = _second_app(tmp_path)
    make_user(box2, "admin2", "admin")
    c2 = box2.test_client()
    login(c2, "admin2")
    c2.post("/content/subjects/new", data={"name": "maths"})
    _import(c2, data)
    assert db_one(box2, "SELECT COUNT(*) FROM subjects")[0] == 1
    assert db_one(box2, "SELECT s.name FROM courses c JOIN subjects s ON s.id = c.subject_id")[0] == "maths"


@pytest.mark.parametrize("case, expected", [
    ("bad_checksum", "Checksum does not match"),
    ("unknown_version", "Unknown package version"),
    ("traversal", "Unsafe or unexpected file name"),
    ("absolute", "Unsafe or unexpected file name"),
    ("missing_checksum_line", "Files without a checksum"),
    ("manifest_mismatch", "does not match the package contents"),
    ("fake_media", "contents do not match its type"),
    ("not_zip", "not a zip file"),
    ("not_stembox", "not a STEMBox content package"),
    ("bad_reference", "is not in the package"),
])
def test_invalid_packages_rejected_without_changes(app, client, source, tmp_path, case, expected):
    data = _export(client, app, source)
    if case == "bad_checksum":
        bad = _rewrite(data, lambda n, b: (b + b"x") if n.startswith("files/") and n.endswith(".mp4") else b,
                       fix_checksums=False)
    elif case == "unknown_version":
        def change(n, b):
            if n == "manifest.json":
                m = json.loads(b); m["version"] = 99; return json.dumps(m).encode()
            return b
        bad = _rewrite(data, change)
    elif case == "traversal":
        bad = _rewrite(data, lambda n, b: {"../../evil.txt": b"pwned"} if n == "__extra__" else b)
    elif case == "absolute":
        bad = _rewrite(data, lambda n, b: {"/etc/evil": b"pwned"} if n == "__extra__" else b)
    elif case == "missing_checksum_line":
        src = zipfile.ZipFile(io.BytesIO(data))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as zf:
            for info in src.infolist():
                payload = src.read(info.filename)
                if info.filename == "checksums.sha256":
                    payload = b"\n".join(l for l in payload.splitlines() if b"content.json" not in l) + b"\n"
                zf.writestr(info.filename, payload)
        bad = out.getvalue()
    elif case == "manifest_mismatch":
        def change(n, b):
            if n == "data/content.json":
                c = json.loads(b); c["lessons"][0]["title"] = "Swapped"; return json.dumps(c).encode()
            return b
        bad = _rewrite(data, change)
    elif case == "fake_media":
        def change(n, b):
            if n.endswith(".mp4"):
                return b"NOT A VIDEO" + b[11:]
            return b
        bad = _rewrite(data, change)
    elif case == "not_zip":
        bad = b"this is just text"
    elif case == "not_stembox":
        bad = _rewrite(data, lambda n, b: b'{"format": "something-else"}' if n == "manifest.json" else b)
    elif case == "bad_reference":
        def change(n, b):
            if n == "data/content.json":
                c = json.loads(b); c["courses"][0]["subject_uuid"] = "11111111-1111-1111-1111-111111111111"
                return json.dumps(c).encode()
            return b
        bad = _rewrite(data, change)

    box2 = _second_app(tmp_path)
    make_user(box2, "admin2", "admin")
    c2 = box2.test_client()
    login(c2, "admin2")
    response = _import(c2, bad)
    assert response.status_code == 400
    page = response.data.decode()
    assert "Package rejected" in page and "Nothing was changed" in page
    assert expected in page, page[page.find("<ul>"):page.find("</ul>")]
    for table in ("subjects", "courses", "lessons", "resources", "quizzes", "content_packages"):
        assert db_one(box2, f"SELECT COUNT(*) FROM {table}")[0] == 0, table
    import os
    files_dir = os.path.join(box2.config["STORAGE_DIR"], "files")
    assert not any(files for _r, _d, files in os.walk(files_dir))
    assert db_one(box2, "SELECT COUNT(*) FROM audit_log WHERE action = 'package.reject'")[0] == 1


def test_transfer_pages_admin_only(app, client, users):
    for who in ("teacher", "student"):
        login(client, who)
        assert client.get("/transfer/").status_code == 403
        assert client.post("/transfer/export").status_code == 403
        assert client.post("/transfer/import").status_code == 403
        assert client.post("/transfer/wifi").status_code == 403
        client.post("/logout")


def test_download_package_blocks_traversal(app, client, source):
    _export(client, app, source)
    assert client.get("/transfer/packages/..%2Fstorage%2Fstembox.db").status_code == 404
    assert client.get("/transfer/packages/secret_key").status_code == 404


# ---------------------------------------------------------------- Wi-Fi

class _FakeResponse:
    def __init__(self, data):
        self._buf = io.BytesIO(data)

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _route_to(source_client, seen):
    def fake_urlopen(req, timeout=None):
        url = urlparse(req.full_url)
        seen.append(url.netloc)
        response = source_client.open(url.path, method=req.get_method(), data=req.data,
                                       headers=dict(req.header_items()))
        if response.status_code >= 400:
            raise urllib.error.HTTPError(req.full_url, response.status_code, "error", {}, None)
        return _FakeResponse(response.data)
    return fake_urlopen


def test_api_requires_admin_credentials(app, client, source):
    api = app.test_client()
    db_exec(app, "UPDATE settings SET value = '1' WHERE key = 'allow_remote_pull'")
    assert api.get("/api/sync/catalog").status_code == 401
    import base64

    def basic(u, p):
        return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}
    assert api.get("/api/sync/catalog", headers=basic("admin", "wrong")).status_code == 401
    assert api.get("/api/sync/catalog", headers=basic("teacher", "secret123")).status_code == 401
    response = api.get("/api/sync/catalog", headers=basic("admin", "secret123"))
    assert response.status_code == 200 and response.get_json()["subjects"][0]["name"] == "Maths"
    db_exec(app, "UPDATE settings SET value = '0' WHERE key = 'allow_remote_pull'")
    assert api.get("/api/sync/catalog", headers=basic("admin", "secret123")).status_code == 403


def test_wifi_pull_between_installations(app, client, source, tmp_path, monkeypatch):
    import app.sync as sync_module
    db_exec(app, "UPDATE settings SET value = '1' WHERE key = 'allow_remote_pull'")
    box2 = _second_app(tmp_path)
    make_user(box2, "admin2", "admin")
    c2 = box2.test_client()
    login(c2, "admin2")
    seen = []
    monkeypatch.setattr(sync_module, "urlopen", _route_to(app.test_client(), seen))

    # Wrong password on the source.
    response = c2.post("/transfer/wifi", data={"address": "192.168.1.20:5000", "username": "admin", "password": "bad"},
                       follow_redirects=True)
    assert "did not accept that administrator" in response.data.decode()

    # Internet addresses are refused before any request is made.
    seen.clear()
    response = c2.post("/transfer/wifi", data={"address": "http://example.com", "username": "admin", "password": "x"},
                       follow_redirects=True)
    assert "local" in response.data.decode() and seen == []

    response = c2.post("/transfer/wifi", data={"address": "192.168.1.20:5000", "username": "admin",
                                               "password": "secret123"})
    page = response.data.decode()
    assert "Fractions" in page
    assert seen[-1] == "192.168.1.20:5000"
    token = page.split('name="token" value="')[1].split('"')[0]
    course_uuid = db_one(app, "SELECT uuid FROM courses")[0]
    response = c2.post("/transfer/wifi/pull", data={"token": token, "course_uuid": course_uuid})
    assert response.status_code == 200 and "Content loaded" in response.data.decode()
    assert db_one(box2, "SELECT COUNT(*) FROM lessons")[0] == 1
    assert db_one(box2, "SELECT COUNT(*) FROM resources")[0] == 2
    assert db_one(box2, "SELECT source FROM content_packages")[0].startswith("Wi-Fi from http://192.168.1.20:5000")
    # The source logged who pulled content.
    assert db_one(app, "SELECT COUNT(*) FROM audit_log WHERE action = 'package.remote_export'")[0] == 1


def test_wifi_sharing_switched_off(app, client, source, tmp_path, monkeypatch):
    import app.sync as sync_module
    box2 = _second_app(tmp_path)
    make_user(box2, "admin2", "admin")
    c2 = box2.test_client()
    login(c2, "admin2")
    monkeypatch.setattr(sync_module, "urlopen", _route_to(app.test_client(), []))
    response = c2.post("/transfer/wifi", data={"address": "10.0.0.5", "username": "admin", "password": "secret123"},
                       follow_redirects=True)
    assert "Sharing is switched off" in response.data.decode()


def test_wifi_token_belongs_to_session(app, client, source, tmp_path, monkeypatch):
    import app.sync as sync_module
    db_exec(app, "UPDATE settings SET value = '1' WHERE key = 'allow_remote_pull'")
    box2 = _second_app(tmp_path)
    make_user(box2, "admin2", "admin")
    make_user(box2, "admin3", "admin")
    c2 = box2.test_client()
    login(c2, "admin2")
    monkeypatch.setattr(sync_module, "urlopen", _route_to(app.test_client(), []))
    page = c2.post("/transfer/wifi", data={"address": "10.0.0.5", "username": "admin", "password": "secret123"}).data.decode()
    token = page.split('name="token" value="')[1].split('"')[0]
    other = box2.test_client()
    login(other, "admin3")
    response = other.post("/transfer/wifi/pull", data={"token": token, "course_uuid": "x"}, follow_redirects=True)
    assert "expired" in response.data.decode()


def test_normalise_address():
    from app.sync import RemoteError, normalise_address
    assert normalise_address("192.168.1.5") == "http://192.168.1.5"
    assert normalise_address("192.168.1.5:5000/") == "http://192.168.1.5:5000"
    assert normalise_address("http://stembox.local:8080") == "http://stembox.local:8080"
    for bad in ("", "8.8.8.8", "http://evil.com", "ftp://192.168.1.5", "http://192.168.1.5/admin", "http://u:p@10.0.0.1"):
        with pytest.raises(RemoteError):
            normalise_address(bad)


# ---------------------------------------------------------------- command line (server-side USB)

def test_cli_export_and_import(app, client, source, tmp_path):
    out = tmp_path / "usb" / "maths.zip"
    out.parent.mkdir()
    course_uuid = db_one(app, "SELECT uuid FROM courses")[0]
    result = app.test_cli_runner().invoke(args=["export-package", "--course", course_uuid, "--out", str(out)])
    assert result.exit_code == 0, result.output
    box2 = _second_app(tmp_path)
    result = box2.test_cli_runner().invoke(args=["import-package", str(out)])
    assert result.exit_code == 0, result.output
    assert "added: 6" in result.output
    assert db_one(box2, "SELECT COUNT(*) FROM lessons")[0] == 1
    out.write_bytes(b"garbage")
    result = box2.test_cli_runner().invoke(args=["import-package", str(out)])
    assert result.exit_code != 0 and "rejected" in result.output
