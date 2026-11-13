"""Home page, role dashboards and first-run setup."""
from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from ..auth import hash_password, login_required, password_problem, start_session, username_problem
from ..db import execute, new_uuid, now, query

bp = Blueprint("main", __name__)


def _no_users():
    return query("SELECT COUNT(*) AS n FROM users", one=True)["n"] == 0


@bp.route("/")
def index():
    if _no_users():
        return redirect(url_for("main.setup"))
    if g.get("user"):
        return redirect(url_for("main.dashboard"))
    return redirect(url_for("auth.login"))


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """First-run page: create the first administrator. Only works while there are no users."""
    if not _no_users():
        return redirect(url_for("auth.login"))
    errors = []
    form = {"name": "", "username": ""}
    if request.method == "POST":
        form["name"] = request.form.get("name", "").strip()
        form["username"] = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        if not form["name"]:
            errors.append("Please type your full name.")
        for problem in (username_problem(form["username"]), password_problem(password, "admin")):
            if problem:
                errors.append(problem)
        if password != request.form.get("password2", ""):
            errors.append("The two passwords are not the same.")
        if not errors:
            user_id = execute(
                "INSERT INTO users (uuid, name, username, password_hash, role, active, created_at) "
                "VALUES (?, ?, ?, ?, 'admin', 1, ?)",
                (new_uuid(), form["name"], form["username"], hash_password(password), now()))
            start_session(query("SELECT * FROM users WHERE id = ?", (user_id,), one=True))
            flash("STEMBox is ready. You are logged in as the administrator.", "success")
            return redirect(url_for("main.dashboard"))
    return render_template("main/setup.html", errors=errors, form=form)


@bp.route("/dashboard")
@login_required
def dashboard():
    role = g.user["role"]
    if role == "admin":
        from .dashboards import admin_dashboard
        return admin_dashboard()
    if role == "teacher":
        from .dashboards import teacher_dashboard
        return teacher_dashboard()
    from .dashboards import student_dashboard
    return student_dashboard()
