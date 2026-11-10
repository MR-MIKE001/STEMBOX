"""Content transfer: USB export/import and pulling content from another STEMBox over Wi-Fi."""
import base64
import ipaddress
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from flask import (Blueprint, Response, abort, current_app, flash, g, jsonify, redirect, render_template, request,
                   send_file, session, url_for)

from .. import __version__
from ..auth import authenticate, roles_required
from ..db import audit, get_setting, new_uuid, query
from .package import PackageError, build_package, import_package

bp = Blueprint("sync", __name__, url_prefix="/transfer")
api_bp = Blueprint("api", __name__, url_prefix="/api")

KEEP_PACKAGES = 10

# Proxies are ignored: the other STEMBox is on the local network.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
urlopen = _opener.open  # replaced in tests


# ---------------------------------------------------------------- helpers

def _catalog():
    subjects = []
    for s in query("SELECT * FROM subjects ORDER BY name"):
        courses = query(
            "SELECT c.*, cl.name AS class_name, "
            "(SELECT COUNT(*) FROM lessons l WHERE l.course_id = c.id) AS lesson_count, "
            "(SELECT COUNT(*) FROM lessons l WHERE l.course_id = c.id AND l.status = 'published') AS published_count, "
            "(SELECT COUNT(*) FROM quizzes z WHERE z.course_id = c.id) AS quiz_count "
            "FROM courses c LEFT JOIN classes cl ON cl.id = c.class_id WHERE c.subject_id = ? ORDER BY c.title", (s["id"],))
        subjects.append({"id": s["id"], "uuid": s["uuid"], "name": s["name"], "courses": [
            {"id": c["id"], "uuid": c["uuid"], "title": c["title"], "level": c["level"], "class_name": c["class_name"],
             "lessons": c["lesson_count"], "published": c["published_count"], "quizzes": c["quiz_count"]}
            for c in courses]})
    return subjects


def _prune_packages():
    folder = Path(current_app.config["PACKAGES_DIR"])
    zips = sorted(folder.glob("stembox-package-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in zips[KEEP_PACKAGES:]:
        try:
            old.unlink()
        except OSError:
            pass


def _save_upload_to_tmp(file_storage):
    limit = current_app.config["MAX_PACKAGE_BYTES"]
    tmp = Path(current_app.config["STORAGE_DIR"]) / "tmp" / f"import-{new_uuid()}.zip"
    size = 0
    with open(tmp, "wb") as out:
        while True:
            chunk = file_storage.stream.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                out.close()
                tmp.unlink()
                raise PackageError("The package is larger than this STEMBox accepts.")
            out.write(chunk)
    return tmp


def _history():
    return query("SELECT * FROM content_packages ORDER BY id DESC LIMIT 20")


# ---------------------------------------------------------------- pages (admin)

@bp.route("/")
@roles_required("admin")
def home():
    folder = Path(current_app.config["PACKAGES_DIR"])
    exports = sorted(folder.glob("stembox-package-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    history = []
    for row in _history():
        try:
            summary = json.loads(row["summary"])
        except ValueError:
            summary = {}
        history.append({**dict(row), "counts": summary})
    return render_template("sync/home.html", catalog=_catalog(), exports=exports, history=history,
                           allow_remote_pull=get_setting("allow_remote_pull") == "1")


@bp.route("/export", methods=["POST"])
@roles_required("admin")
def export():
    subject_ids = [int(x) for x in request.form.getlist("subject_id") if x.isdigit()]
    course_ids = [int(x) for x in request.form.getlist("course_id") if x.isdigit()]
    if not subject_ids and not course_ids:
        flash("Tick at least one subject or course to copy.", "error")
        return redirect(url_for("sync.home"))
    path, manifest = build_package(subject_ids, course_ids, include_drafts=bool(request.form.get("include_drafts")))
    _prune_packages()
    audit("package.export", manifest["package_uuid"], json.dumps(manifest["counts"]))
    return send_file(path, mimetype="application/zip", as_attachment=True, download_name=path.name)


@bp.route("/packages/<name>")
@roles_required("admin")
def download_package(name):
    folder = Path(current_app.config["PACKAGES_DIR"]).resolve()
    path = (folder / name).resolve()
    if path.parent != folder or not path.name.startswith("stembox-package-") or not path.exists():
        abort(404)
    return send_file(path, mimetype="application/zip", as_attachment=True, download_name=path.name)


@bp.route("/import", methods=["POST"])
@roles_required("admin")
def import_upload():
    upload = request.files.get("package")
    if upload is None or not upload.filename:
        flash("Please choose the package file (.zip) from the USB stick.", "error")
        return redirect(url_for("sync.home"))
    try:
        tmp = _save_upload_to_tmp(upload)
    except PackageError as exc:
        return render_template("sync/report.html", report=None, problems=exc.problems, source=upload.filename), 400
    try:
        report = import_package(tmp, source=f"USB/file upload: {Path(upload.filename).name}", user_id=g.user["id"])
    except PackageError as exc:
        audit("package.reject", Path(upload.filename).name, "; ".join(exc.problems)[:500])
        return render_template("sync/report.html", report=None, problems=exc.problems, source=upload.filename), 400
    finally:
        if tmp.exists():
            tmp.unlink()
    return render_template("sync/report.html", report=report, problems=[], source=upload.filename)


# ---------------------------------------------------------------- Wi-Fi: pulling from another STEMBox

_remote = {}
_remote_lock = threading.Lock()
REMOTE_TTL = 15 * 60


class RemoteError(Exception):
    pass


def normalise_address(address):
    """Accept '192.168.1.20', '192.168.1.20:5000' or 'http://stembox.local:5000'.
    Only local-network hosts are allowed."""
    address = (address or "").strip().rstrip("/")
    if not address:
        raise RemoteError("Please type the address of the other STEMBox.")
    if "://" not in address:
        address = "http://" + address
    parsed = urlparse(address)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path not in ("", "/") \
            or parsed.username or parsed.query:
        raise RemoteError("That does not look like a STEMBox address. Example: http://192.168.1.20:5000")
    host = parsed.hostname
    try:
        ip = ipaddress.ip_address(host)
        if not (ip.is_private or ip.is_loopback or ip.is_link_local):
            raise RemoteError("Only STEMBoxes on the local network can be used.")
    except ValueError:
        if "." in host and not host.endswith((".local", ".lan", ".home", ".internal")):
            raise RemoteError("Use the other STEMBox's local address (numbers like 192.168.1.20, or a name ending in .local).")
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}"


def remote_call(base, path, username, password, payload=None, dest=None):
    """Call the other STEMBox's API. Returns parsed JSON, or writes the body to `dest`."""
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    headers = {"Authorization": f"Basic {token}", "User-Agent": f"STEMBox/{__version__}"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urlopen(req, timeout=current_app.config["SYNC_TIMEOUT"]) as resp:
            if dest is None:
                return json.loads(resp.read(10 * 1024 * 1024).decode("utf-8"))
            limit = current_app.config["MAX_PACKAGE_BYTES"]
            size = 0
            with open(dest, "wb") as out:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise RemoteError("The package from the other STEMBox is too large.")
                    out.write(chunk)
            return None
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise RemoteError("The other STEMBox did not accept that administrator username and password.")
        if exc.code == 403:
            raise RemoteError("Sharing is switched off on the other STEMBox. Turn on “Allow another STEMBox on this "
                              "Wi-Fi to copy content” in its Settings.")
        raise RemoteError(f"The other STEMBox answered with an error ({exc.code}).")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise RemoteError("Could not reach the other STEMBox. Check the address and that both are on the same Wi-Fi.")
    except ValueError:
        raise RemoteError("The other device did not answer like a STEMBox.")


def _store_remote(data):
    now_ts = time.time()
    with _remote_lock:
        for key in [k for k, v in _remote.items() if v["expires"] < now_ts]:
            _remote.pop(key, None)
        token = secrets.token_urlsafe(24)
        _remote[token] = {**data, "expires": now_ts + REMOTE_TTL, "owner": g.user["id"]}
    session["remote_token"] = token
    return token


def _get_remote(token):
    with _remote_lock:
        entry = _remote.get(token)
    if not entry or entry["expires"] < time.time() or entry["owner"] != g.user["id"] \
            or session.get("remote_token") != token:
        return None
    return entry


@bp.route("/wifi", methods=["POST"])
@roles_required("admin")
def wifi_connect():
    address = request.form.get("address", "")
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    try:
        base = normalise_address(address)
        if not username or not password:
            raise RemoteError("Type the administrator username and password of the other STEMBox.")
        catalog = remote_call(base, "/api/sync/catalog", username, password)
        if not isinstance(catalog, dict) or not isinstance(catalog.get("subjects"), list):
            raise RemoteError("The other device did not answer like a STEMBox.")
    except RemoteError as exc:
        flash(str(exc), "error")
        return redirect(url_for("sync.home") + "#wifi")
    token = _store_remote({"base": base, "username": username, "password": password})
    return render_template("sync/wifi_select.html", catalog=catalog, base=base, token=token)


@bp.route("/wifi/pull", methods=["POST"])
@roles_required("admin")
def wifi_pull():
    entry = _get_remote(request.form.get("token", ""))
    if entry is None:
        flash("The connection to the other STEMBox expired. Please connect again.", "error")
        return redirect(url_for("sync.home") + "#wifi")
    courses = [c for c in request.form.getlist("course_uuid") if len(c) <= 64]
    if not courses:
        flash("Tick at least one course to copy.", "error")
        return redirect(url_for("sync.home") + "#wifi")
    tmp = Path(current_app.config["STORAGE_DIR"]) / "tmp" / f"wifi-{new_uuid()}.zip"
    try:
        remote_call(entry["base"], "/api/sync/package", entry["username"], entry["password"],
                    payload={"courses": courses, "include_drafts": bool(request.form.get("include_drafts"))}, dest=tmp)
        report = import_package(tmp, source=f"Wi-Fi from {entry['base']}", user_id=g.user["id"])
    except RemoteError as exc:
        flash(str(exc), "error")
        return redirect(url_for("sync.home") + "#wifi")
    except PackageError as exc:
        audit("package.reject", entry["base"], "; ".join(exc.problems)[:500])
        return render_template("sync/report.html", report=None, problems=exc.problems, source=entry["base"]), 400
    finally:
        if tmp.exists():
            tmp.unlink()
    return render_template("sync/report.html", report=report, problems=[], source=entry["base"])


# ---------------------------------------------------------------- API used by other STEMBoxes

def _api_admin():
    """HTTP Basic auth with an administrator account of THIS STEMBox."""
    auth = request.authorization
    if auth is None or auth.type != "basic" or not auth.username:
        return None, _unauthorized()
    user, _error = authenticate(auth.username, auth.password or "")
    if user is None or user["role"] != "admin":
        return None, _unauthorized()
    if get_setting("allow_remote_pull") != "1":
        return None, (jsonify(error="Sharing with other STEMBoxes is switched off."), 403)
    return user, None


def _unauthorized():
    response = jsonify(error="Administrator username and password required.")
    response.status_code = 401
    response.headers["WWW-Authenticate"] = 'Basic realm="STEMBox"'
    return response


@api_bp.route("/sync/catalog")
def api_catalog():
    user, error = _api_admin()
    if error:
        return error
    subjects = [{"uuid": s["uuid"], "name": s["name"],
                 "courses": [{k: c[k] for k in ("uuid", "title", "level", "lessons", "published", "quizzes")}
                             for c in s["courses"]]}
                for s in _catalog() if s["courses"]]
    return jsonify(site_name=get_setting("site_name"), version=__version__, subjects=subjects)


@api_bp.route("/sync/package", methods=["POST"])
def api_package():
    user, error = _api_admin()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    uuids = [u for u in body.get("courses", []) if isinstance(u, str)][:1000]
    if not uuids:
        return jsonify(error="No courses requested."), 400
    course_ids = [r["id"] for r in query(f"SELECT id FROM courses WHERE uuid IN ({','.join('?' * len(uuids))})", uuids)]
    if not course_ids:
        return jsonify(error="None of the requested courses exist here."), 404
    path, manifest = build_package([], course_ids, include_drafts=bool(body.get("include_drafts")),
                                   source=get_setting("site_name"))
    _prune_packages()
    audit("package.remote_export", manifest["package_uuid"], f"pulled by {request.remote_addr}", user_id=user["id"])
    return send_file(path, mimetype="application/zip", as_attachment=True, download_name=path.name)


@api_bp.route("/ping")
def api_ping():
    """Lets another STEMBox (or a person) check that this is a STEMBox and it is running."""
    return jsonify(app="STEMBox", version=__version__)
