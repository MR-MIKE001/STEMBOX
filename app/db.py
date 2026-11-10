"""SQLite access, schema creation and migrations."""
import sqlite3
import uuid as uuid_lib
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app, g

SCHEMA_FILE = Path(__file__).with_name("schema.sql")

# Migrations run in order; the index + 1 is stored in PRAGMA user_version.
# Migration 1 is the base schema. Add new SQL strings to the end of this list
# to change the schema of existing installations without losing data.
MIGRATIONS = [
    SCHEMA_FILE.read_text(encoding="utf-8"),
]

DEFAULT_SETTINGS = {
    "publish_mode": "approval",      # direct | approval | mixed
    "allow_registration": "0",       # students may create their own account
    "allow_remote_pull": "0",        # other STEMBoxes may pull content over Wi-Fi
    "site_name": "STEMBox",
}


def now():
    """Current UTC time as an ISO-8601 string (seconds precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_uuid():
    return str(uuid_lib.uuid4())


def connect(path):
    conn = sqlite3.connect(path, timeout=30, detect_types=0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    cur.close()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql, args=()):
    """Run a write statement and commit. Returns the last row id."""
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid


def migrate(conn):
    """Apply any migrations the database has not seen yet. Returns how many ran."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    applied = 0
    for index, sql in enumerate(MIGRATIONS[version:], start=version):
        conn.executescript("BEGIN;\n" + sql + f"\nPRAGMA user_version = {index + 1};\nCOMMIT;")
        applied += 1
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    return applied


def init_db(app, reset=False):
    path = Path(app.config["DATABASE"])
    if reset and path.exists():
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(path) + suffix)
            if p.exists():
                p.unlink()
    conn = connect(str(path))
    try:
        return migrate(conn)
    finally:
        conn.close()


def get_setting(key, default=None):
    row = query("SELECT value FROM settings WHERE key = ?", (key,), one=True)
    if row is None:
        return DEFAULT_SETTINGS.get(key, default)
    return row["value"]


def set_setting(key, value):
    execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def audit(action, target="", details="", user_id=None):
    """Record an administrative action."""
    if user_id is None:
        user = g.get("user")
        user_id = user["id"] if user else None
    execute(
        "INSERT INTO audit_log (user_id, action, target, details, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, action, str(target), str(details), now()),
    )
