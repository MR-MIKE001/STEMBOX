"""Administrator tools: users, CSV import, classes, settings and the audit log."""
import csv
import io

from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for

from ..auth import (ROLES, generate_temp_password, hash_password, password_problem, roles_required,
                    username_problem)
from ..db import audit, execute, get_db, get_setting, new_uuid, now, query, set_setting
from ..utils import to_int

bp = Blueprint("users", __name__, url_prefix="/admin")

ROLE_ALIASES = {"administrator": "admin", "admin": "admin", "teacher": "teacher", "student": "student",
                "learner": "student", "pupil": "student"}


def _get_user(user_id):
    user = query("SELECT * FROM users WHERE id = ?", (user_id,), one=True)
    if user is None:
        abort(404)
    return user


def _active_admin_count(exclude_id=None):
    row = query("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND active = 1 AND id != ?",
                (exclude_id or 0,), one=True)
    return row["n"]


# ---------------------------------------------------------------- users

@bp.route("/users")
@roles_required("admin")
def user_list():
    q = request.args.get("q", "").strip()
    role = request.args.get("role", "")
    status = request.args.get("status", "")
    sql = ("SELECT u.*, (SELECT GROUP_CONCAT(c.name, ', ') FROM class_members cm JOIN classes c ON c.id = cm.class_id "
           "WHERE cm.user_id = u.id) AS class_names FROM users u WHERE 1 = 1")
    args = []
    if q:
        sql += " AND (u.name LIKE ? OR u.username LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    if role in ROLES:
        sql += " AND u.role = ?"
        args.append(role)
    if status == "active":
        sql += " AND u.active = 1"
    elif status == "inactive":
        sql += " AND u.active = 0"
    sql += " ORDER BY u.role, u.name LIMIT 500"
    users = query(sql, args)
    return render_template("users/list.html", users=users, q=q, role=role, status=status)


def _read_user_form(user=None):
    form = {
        "name": request.form.get("name", "").strip(),
        "username": request.form.get("username", "").strip().lower(),
        "role": request.form.get("role", "student"),
        "active": 1 if request.form.get("active", "1" if user is None else "") else 0,
    }
    errors = []
    if not form["name"]:
        errors.append("Please type the person's full name.")
    problem = username_problem(form["username"], ignore_user_id=user["id"] if user else None)
    if problem:
        errors.append(problem)
    if form["role"] not in ROLES:
        errors.append("Please choose a role.")
    return form, errors


@bp.route("/users/new", methods=["GET", "POST"])
@roles_required("admin")
def user_new():
    form = {"name": "", "username": "", "role": request.args.get("role", "student"), "active": 1}
    errors = []
    classes = query("SELECT * FROM classes ORDER BY name")
    if request.method == "POST":
        form, errors = _read_user_form()
        password = request.form.get("password", "").strip()
        generated = False
        if not password:
            password = generate_temp_password()
            generated = True
        else:
            problem = password_problem(password)
            if problem:
                errors.append(problem)
        if not errors:
            user_id = execute(
                "INSERT INTO users (uuid, name, username, password_hash, role, active, must_change_password, created_at) "
                "VALUES (?, ?, ?, ?, ?, 1, 1, ?)",
                (new_uuid(), form["name"], form["username"], hash_password(password), form["role"], now()))
            class_id = to_int(request.form.get("class_id"))
            if class_id and form["role"] != "admin" and query("SELECT 1 FROM classes WHERE id = ?", (class_id,), one=True):
                execute("INSERT OR IGNORE INTO class_members VALUES (?, ?, ?)", (class_id, user_id, form["role"]))
            audit("user.create", f"user:{user_id}", f"{form['username']} ({form['role']})")
            msg = f"Account created for {form['name']}. Username: {form['username']}"
            if generated:
                msg += f" · Temporary password: {password}"
            flash(msg + " · They will choose their own password when they first log in.", "success")
            if request.form.get("again"):
                return redirect(url_for("users.user_new", role=form["role"]))
            return redirect(url_for("users.user_list"))
    return render_template("users/form.html", form=form, errors=errors, user=None, classes=classes)


@bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@roles_required("admin")
def user_edit(user_id):
    user = _get_user(user_id)
    form = dict(user)
    errors = []
    if request.method == "POST":
        form, errors = _read_user_form(user)
        is_self = user["id"] == g.user["id"]
        losing_admin = user["role"] == "admin" and (form["role"] != "admin" or not form["active"])
        if losing_admin and _active_admin_count(exclude_id=user["id"]) == 0:
            errors.append("This is the only active administrator. Create another administrator first.")
        if is_self and not form["active"]:
            errors.append("You cannot switch off your own account.")
        if not errors:
            execute("UPDATE users SET name = ?, username = ?, role = ?, active = ? WHERE id = ?",
                    (form["name"], form["username"], form["role"], form["active"], user_id))
            if form["role"] != user["role"]:
                # Class membership follows the role (admins are not class members).
                if form["role"] == "admin":
                    execute("DELETE FROM class_members WHERE user_id = ?", (user_id,))
                else:
                    execute("UPDATE class_members SET role_in_class = ? WHERE user_id = ?", (form["role"], user_id))
            audit("user.edit", f"user:{user_id}",
                  f"{form['username']} role={form['role']} active={form['active']}")
            flash("Changes saved.", "success")
            return redirect(url_for("users.user_list"))
    classes = query("SELECT c.* FROM classes c JOIN class_members cm ON cm.class_id = c.id WHERE cm.user_id = ? "
                    "ORDER BY c.name", (user_id,))
    return render_template("users/form.html", form=form, errors=errors, user=user, classes=[],
                           member_of=classes)


@bp.route("/users/<int:user_id>/active", methods=["POST"])
@roles_required("admin")
def user_toggle_active(user_id):
    user = _get_user(user_id)
    new_state = 0 if user["active"] else 1
    if new_state == 0:
        if user["id"] == g.user["id"]:
            flash("You cannot switch off your own account.", "error")
            return redirect(url_for("users.user_list"))
        if user["role"] == "admin" and _active_admin_count(exclude_id=user["id"]) == 0:
            flash("This is the only active administrator, so it cannot be switched off.", "error")
            return redirect(url_for("users.user_list"))
    execute("UPDATE users SET active = ? WHERE id = ?", (new_state, user_id))
    audit("user.activate" if new_state else "user.deactivate", f"user:{user_id}", user["username"])
    flash(f"{user['name']} can {'now' if new_state else 'no longer'} log in.", "success")
    return redirect(request.form.get("back") or url_for("users.user_list"))


@bp.route("/users/<int:user_id>/reset-password", methods=["GET", "POST"])
@roles_required("admin")
def user_reset_password(user_id):
    user = _get_user(user_id)
    errors = []
    new_password = None
    if request.method == "POST":
        password = request.form.get("password", "").strip()
        if not password:
            password = generate_temp_password()
        problem = password_problem(password)
        if problem:
            errors.append(problem)
        else:
            execute("UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?",
                    (hash_password(password), user_id))
            audit("user.reset_password", f"user:{user_id}", user["username"])
            new_password = password
    return render_template("users/reset_password.html", user=user, errors=errors, new_password=new_password)


# ---------------------------------------------------------------- CSV import

CSV_TEMPLATE = "name,username,role,password,class\nAmina Okafor,amina.o,student,,Grade 7A\nJohn Banda,j.banda,teacher,,\n"


@bp.route("/users/import/template.csv")
@roles_required("admin")
def user_import_template():
    return Response(CSV_TEMPLATE, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=stembox-users-template.csv"})


def parse_user_csv(text):
    """Parse and validate CSV text. Returns (valid_rows, problems).

    Required columns: name, username, role. Optional: password, class.
    """
    problems = []
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        return [], ["The file is empty."]
    headers = {h.strip().lower(): h for h in reader.fieldnames if h}
    missing = [c for c in ("name", "username", "role") if c not in headers]
    if missing:
        return [], [f"The first line must name the columns. Missing: {', '.join(missing)}. "
                    "Download the template to see the layout."]
    rows = []
    seen = set()
    classes = {r["name"].strip().lower(): r["id"] for r in query("SELECT id, name FROM classes")}
    for line_no, raw in enumerate(reader, start=2):
        row = {k: (raw.get(v) or "").strip() for k, v in headers.items()}
        if not any(row.values()):
            continue
        name, username = row.get("name", ""), row.get("username", "").lower()
        role = ROLE_ALIASES.get(row.get("role", "").lower())
        errs = []
        if not name:
            errs.append("name is empty")
        if role is None:
            errs.append(f"role “{row.get('role', '')}” is not admin, teacher or student")
        if username in seen:
            errs.append(f"username “{username}” appears twice in the file")
        else:
            problem = username_problem(username)
            if problem:
                errs.append(problem)
        password = row.get("password", "")
        if password and password_problem(password):
            errs.append(password_problem(password))
        class_name = row.get("class", "")
        class_id = None
        if class_name:
            class_id = classes.get(class_name.lower())
            if class_id is None:
                errs.append(f"class “{class_name}” does not exist (create it first)")
            elif role == "admin":
                errs.append("administrators cannot be added to a class")
        if errs:
            problems.append(f"Line {line_no} ({username or name or 'blank'}): " + "; ".join(errs))
            continue
        seen.add(username)
        rows.append({"name": name, "username": username, "role": role, "password": password,
                     "class_id": class_id, "class_name": class_name})
    return rows, problems


@bp.route("/users/import", methods=["GET", "POST"])
@roles_required("admin")
def user_import():
    if request.method == "GET":
        return render_template("users/import.html")
    upload = request.files.get("file")
    text = request.form.get("text", "")
    if upload and upload.filename:
        data = upload.read(current_app.config["MAX_CSV_BYTES"] + 1)
        if len(data) > current_app.config["MAX_CSV_BYTES"]:
            flash("The CSV file is too large (limit 2 MB).", "error")
            return redirect(url_for("users.user_import"))
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
    if not text.strip():
        flash("Please choose a CSV file or paste the list.", "error")
        return redirect(url_for("users.user_import"))
    rows, problems = parse_user_csv(text)
    created = []
    db = get_db()
    stamp = now()
    for row in rows:
        password = row["password"] or generate_temp_password()
        cur = db.execute(
            "INSERT INTO users (uuid, name, username, password_hash, role, active, must_change_password, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, 1, ?)",
            (new_uuid(), row["name"], row["username"], hash_password(password), row["role"], stamp))
        if row["class_id"]:
            db.execute("INSERT OR IGNORE INTO class_members VALUES (?, ?, ?)", (row["class_id"], cur.lastrowid, row["role"]))
        created.append({**row, "password": password, "generated": not row["password"]})
    db.commit()
    audit("user.import", "users", f"created={len(created)} problems={len(problems)}")
    return render_template("users/import_result.html", created=created, problems=problems)


# ---------------------------------------------------------------- classes

def _get_class(class_id):
    row = query("SELECT * FROM classes WHERE id = ?", (class_id,), one=True)
    if row is None:
        abort(404)
    return row


@bp.route("/classes")
@roles_required("admin")
def class_list():
    classes = query(
        "SELECT c.*, "
        "(SELECT COUNT(*) FROM class_members m WHERE m.class_id = c.id AND m.role_in_class = 'student') AS students, "
        "(SELECT GROUP_CONCAT(u.name, ', ') FROM class_members m JOIN users u ON u.id = m.user_id "
        " WHERE m.class_id = c.id AND m.role_in_class = 'teacher') AS teachers "
        "FROM classes c ORDER BY c.level, c.name")
    return render_template("users/class_list.html", classes=classes)


@bp.route("/classes/new", methods=["GET", "POST"])
@roles_required("admin")
def class_new():
    return _class_form(None)


@bp.route("/classes/<int:class_id>/edit", methods=["GET", "POST"])
@roles_required("admin")
def class_edit(class_id):
    return _class_form(_get_class(class_id))


def _class_form(klass):
    form = dict(klass) if klass else {"name": "", "level": ""}
    errors = []
    if request.method == "POST":
        form = {"name": request.form.get("name", "").strip(), "level": request.form.get("level", "").strip()}
        if not form["name"]:
            errors.append("Please type a class name, for example “Grade 7A”.")
        dup = query("SELECT id FROM classes WHERE name = ? COLLATE NOCASE", (form["name"],), one=True)
        if dup and (klass is None or dup["id"] != klass["id"]):
            errors.append("A class with this name already exists.")
        if not errors:
            if klass:
                execute("UPDATE classes SET name = ?, level = ? WHERE id = ?", (form["name"], form["level"], klass["id"]))
                audit("class.edit", f"class:{klass['id']}", form["name"])
                flash("Class saved.", "success")
                return redirect(url_for("users.class_detail", class_id=klass["id"]))
            class_id = execute("INSERT INTO classes (uuid, name, level, created_at) VALUES (?, ?, ?, ?)",
                               (new_uuid(), form["name"], form["level"], now()))
            audit("class.create", f"class:{class_id}", form["name"])
            flash("Class created. Now add its teachers and students.", "success")
            return redirect(url_for("users.class_detail", class_id=class_id))
    return render_template("users/class_form.html", form=form, errors=errors, klass=klass)


@bp.route("/classes/<int:class_id>")
@roles_required("admin")
def class_detail(class_id):
    klass = _get_class(class_id)
    members = query("SELECT u.*, m.role_in_class FROM class_members m JOIN users u ON u.id = m.user_id "
                    "WHERE m.class_id = ? ORDER BY m.role_in_class DESC, u.name", (class_id,))
    q = request.args.get("q", "").strip()
    role = request.args.get("role", "student")
    if role not in ("student", "teacher"):
        role = "student"
    sql = ("SELECT * FROM users WHERE role = ? AND active = 1 AND id NOT IN "
           "(SELECT user_id FROM class_members WHERE class_id = ?)")
    args = [role, class_id]
    if q:
        sql += " AND (name LIKE ? OR username LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    candidates = query(sql + " ORDER BY name LIMIT 300", args)
    return render_template("users/class_detail.html", klass=klass, members=members, candidates=candidates,
                           q=q, role=role)


@bp.route("/classes/<int:class_id>/members", methods=["POST"])
@roles_required("admin")
def class_add_members(class_id):
    klass = _get_class(class_id)
    added = 0
    for raw in request.form.getlist("user_id"):
        user = query("SELECT * FROM users WHERE id = ?", (to_int(raw, 0),), one=True)
        if user and user["role"] in ("teacher", "student"):
            execute("INSERT OR IGNORE INTO class_members VALUES (?, ?, ?)", (class_id, user["id"], user["role"]))
            added += 1
    if added:
        audit("class.add_members", f"class:{class_id}", f"{added} added to {klass['name']}")
        flash(f"{added} {'person' if added == 1 else 'people'} added to {klass['name']}.", "success")
    else:
        flash("Tick at least one person to add.", "error")
    return redirect(url_for("users.class_detail", class_id=class_id, role=request.form.get("role", "student")))


@bp.route("/classes/<int:class_id>/members/<int:user_id>/remove", methods=["POST"])
@roles_required("admin")
def class_remove_member(class_id, user_id):
    klass = _get_class(class_id)
    execute("DELETE FROM class_members WHERE class_id = ? AND user_id = ?", (class_id, user_id))
    audit("class.remove_member", f"class:{class_id}", f"user:{user_id} from {klass['name']}")
    flash("Removed from the class.", "success")
    return redirect(url_for("users.class_detail", class_id=class_id))


@bp.route("/classes/<int:class_id>/delete", methods=["POST"])
@roles_required("admin")
def class_delete(class_id):
    klass = _get_class(class_id)
    execute("DELETE FROM classes WHERE id = ?", (class_id,))
    audit("class.delete", f"class:{class_id}", klass["name"])
    flash(f"Class {klass['name']} deleted. Its students and teachers still have their accounts.", "success")
    return redirect(url_for("users.class_list"))


# ---------------------------------------------------------------- settings & audit

PUBLISH_MODES = {
    "direct": ("Teachers publish directly",
               "Lessons and quizzes appear to students as soon as a teacher presses Publish."),
    "approval": ("Administrator approval",
                 "A teacher's lesson or quiz waits until an administrator approves it."),
    "mixed": ("Mixed",
              "Teachers publish directly to a class they teach. Content for a whole subject (all classes) "
              "needs administrator approval."),
}


@bp.route("/settings", methods=["GET", "POST"])
@roles_required("admin")
def settings():
    if request.method == "POST":
        mode = request.form.get("publish_mode")
        if mode not in PUBLISH_MODES:
            flash("Please choose a publishing mode.", "error")
            return redirect(url_for("users.settings"))
        site_name = request.form.get("site_name", "").strip()[:60] or "STEMBox"
        changes = {
            "publish_mode": mode,
            "allow_registration": "1" if request.form.get("allow_registration") else "0",
            "allow_remote_pull": "1" if request.form.get("allow_remote_pull") else "0",
            "site_name": site_name,
        }
        for key, value in changes.items():
            if get_setting(key) != value:
                set_setting(key, value)
                audit("settings.change", key, value)
        flash("Settings saved.", "success")
        return redirect(url_for("users.settings"))
    current = {k: get_setting(k) for k in ("publish_mode", "allow_registration", "allow_remote_pull", "site_name")}
    return render_template("users/settings.html", current=current, modes=PUBLISH_MODES)


@bp.route("/audit")
@roles_required("admin")
def audit_log():
    rows = query("SELECT a.*, u.name AS user_name FROM audit_log a LEFT JOIN users u ON u.id = a.user_id "
                 "ORDER BY a.id DESC LIMIT 300")
    return render_template("users/audit.html", rows=rows)
