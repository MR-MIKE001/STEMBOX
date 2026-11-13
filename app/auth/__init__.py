"""Login, logout, registration, password change and role decorators."""
import functools
import secrets
import string
import threading
import time

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from ..db import execute, now, query
from ..utils import is_safe_next

bp = Blueprint("auth", __name__)

MIN_PASSWORD_LENGTH = 6          # students (often young)
MIN_STAFF_PASSWORD_LENGTH = 8    # teachers and administrators
ROLES = ("admin", "teacher", "student")

# Endpoints a user who must change their password may still reach.
_PASSWORD_CHANGE_ALLOWED = {"auth.change_password", "auth.logout", "static"}

# --- password helpers -------------------------------------------------------

def hash_password(password):
    return generate_password_hash(password, method=current_app.config.get("PASSWORD_METHOD", "scrypt"))


def check_password(password_hash, password):
    return check_password_hash(password_hash, password)


def min_password_length(role):
    return MIN_PASSWORD_LENGTH if role == "student" else MIN_STAFF_PASSWORD_LENGTH


def password_problem(password, role="student"):
    """Return a plain-language problem with the password, or None if it is fine.
    Teachers and administrators need longer passwords than students."""
    minimum = min_password_length(role)
    if len(password or "") < minimum:
        who = "Students' passwords" if role == "student" else "Passwords for teachers and administrators"
        return f"{who} must be at least {minimum} characters long."
    if len(password) > 200:
        return "The password is too long."
    if password.strip() == "":
        return "The password cannot be only spaces."
    return None


def generate_temp_password(length=8):
    # No look-alike characters (0/O, 1/l/I) so it is easy to read out to a student.
    alphabet = "".join(c for c in string.ascii_lowercase + string.digits if c not in "0o1li")
    return "".join(secrets.choice(alphabet) for _ in range(length))


# --- failed-login lockout ---------------------------------------------------

_failures = {}
_failures_lock = threading.Lock()
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300


def _is_locked(key):
    with _failures_lock:
        count, first = _failures.get(key, (0, 0))
        if count >= MAX_FAILURES and time.time() - first < LOCKOUT_SECONDS:
            return True
        if time.time() - first >= LOCKOUT_SECONDS:
            _failures.pop(key, None)
        return False


def _record_failure(key):
    with _failures_lock:
        count, first = _failures.get(key, (0, time.time()))
        _failures[key] = (count + 1, first)


def _clear_failures(key):
    with _failures_lock:
        _failures.pop(key, None)


def authenticate(username, password):
    """Check credentials. Returns (user_row, error_message)."""
    key = (username or "").strip().lower()
    if _is_locked(key):
        return None, "Too many wrong passwords. Please wait 5 minutes and try again."
    user = query("SELECT * FROM users WHERE username = ?", (key,), one=True)
    if user is None or not check_password(user["password_hash"], password or ""):
        _record_failure(key)
        return None, "Wrong username or password."
    if not user["active"]:
        return None, "This account has been switched off. Please ask your administrator."
    _clear_failures(key)
    return user, None


# --- request hooks and decorators --------------------------------------------

def load_logged_in_user():
    g.user = None
    user_id = session.get("user_id")
    if user_id is not None:
        user = query("SELECT * FROM users WHERE id = ?", (user_id,), one=True)
        # Session is dropped if the account was deactivated or the password was
        # reset since login (the stored version no longer matches).
        idle_limit = current_app.config.get("SESSION_IDLE_MINUTES", 0) * 60
        seen = session.get("seen", 0)
        if user is None or not user["active"] or session.get("pw") != user["password_hash"][-12:]:
            session.clear()
        elif idle_limit and time.time() - seen > idle_limit:
            session.clear()
            flash("You were logged out because the page was not used for a while. Please log in again.", "info")
        else:
            g.user = user
            # Refresh the activity stamp at most once a minute (avoids rewriting the cookie on every file request).
            if time.time() - seen > 60:
                session["seen"] = int(time.time())
    if g.user is not None and g.user["must_change_password"] and request.endpoint not in _PASSWORD_CHANGE_ALLOWED:
        if request.endpoint and not request.endpoint.startswith("api."):
            return redirect(url_for("auth.change_password"))
    return None


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("user") is None:
            return redirect(url_for("auth.login", next=request.full_path if request.method == "GET" else None))
        return view(*args, **kwargs)
    return wrapped


def roles_required(*roles):
    """Allow the view only for logged-in users whose role is in `roles`."""
    for role in roles:
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")

    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if g.get("user") is None:
                return redirect(url_for("auth.login", next=request.full_path if request.method == "GET" else None))
            if g.user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def start_session(user):
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["pw"] = user["password_hash"][-12:]
    session["seen"] = int(time.time())
    execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now(), user["id"]))


# --- views -------------------------------------------------------------------

@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.get("user"):
        return redirect(url_for("main.dashboard"))
    error = None
    username = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        user, error = authenticate(username, request.form.get("password", ""))
        if user:
            start_session(user)
            nxt = request.args.get("next")
            if user["must_change_password"]:
                flash("Welcome! Please choose a new password before you continue.", "info")
                return redirect(url_for("auth.change_password"))
            return redirect(nxt if is_safe_next(nxt) else url_for("main.dashboard"))
    return render_template("auth/login.html", error=error, username=username), (401 if error else 200)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have logged out.", "success")
    return redirect(url_for("auth.login"))


def username_problem(username, ignore_user_id=None):
    import re
    if not username:
        return "Please choose a username."
    if not re.fullmatch(r"[a-z0-9._-]{3,40}", username):
        return "Usernames use 3 to 40 lowercase letters, numbers, dots, dashes or underscores (no spaces)."
    existing = query("SELECT id FROM users WHERE username = ?", (username,), one=True)
    if existing and existing["id"] != ignore_user_id:
        return f"The username “{username}” is already taken."
    return None


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    errors = []
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        if not check_password(g.user["password_hash"], current):
            errors.append("Your current password is not correct.")
        problem = password_problem(new, g.user["role"])
        if problem:
            errors.append(problem)
        if new != request.form.get("new_password2", ""):
            errors.append("The two new passwords are not the same.")
        if new and new == current:
            errors.append("The new password must be different from the old one.")
        if not errors:
            new_hash = hash_password(new)
            execute("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
                    (new_hash, g.user["id"]))
            session["pw"] = new_hash[-12:]
            flash("Your password has been changed.", "success")
            return redirect(url_for("main.dashboard"))
    return render_template("auth/change_password.html", errors=errors)
