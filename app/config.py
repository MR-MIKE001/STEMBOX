"""STEMBox configuration.

Every value can be overridden with an environment variable (or a `.env` file in
the project root, see `.env.example`). Tests pass overrides to `create_app()`.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

MB = 1024 * 1024


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


class Config:
    # Where mutable data lives. Everything under STORAGE_DIR should be backed up.
    STORAGE_DIR = os.environ.get("STEMBOX_STORAGE_DIR", str(BASE_DIR / "storage"))
    DATABASE = os.environ.get("STEMBOX_DATABASE", "")  # default: STORAGE_DIR/stembox.db
    PACKAGES_DIR = os.environ.get("STEMBOX_PACKAGES_DIR", str(BASE_DIR / "packages"))

    # If SECRET_KEY is not set, a random key is generated once and kept in
    # STORAGE_DIR/secret_key so logins survive a restart.
    SECRET_KEY = os.environ.get("STEMBOX_SECRET_KEY", "")

    HOST = os.environ.get("STEMBOX_HOST", "0.0.0.0")
    PORT = _env_int("STEMBOX_PORT", 5000)

    SITE_NAME = os.environ.get("STEMBOX_SITE_NAME", "STEMBox")

    # Upload limits per resource type, in bytes.
    UPLOAD_LIMITS = {
        "pdf": _env_int("STEMBOX_MAX_PDF_MB", 50) * MB,
        "image": _env_int("STEMBOX_MAX_IMAGE_MB", 10) * MB,
        "audio": _env_int("STEMBOX_MAX_AUDIO_MB", 100) * MB,
        "video": _env_int("STEMBOX_MAX_VIDEO_MB", 500) * MB,
    }
    MAX_CSV_BYTES = 2 * MB
    MAX_PACKAGE_BYTES = _env_int("STEMBOX_MAX_PACKAGE_MB", 2048) * MB
    # Hard cap on any single request body (largest upload + form overhead).
    MAX_CONTENT_LENGTH = max(_env_int("STEMBOX_MAX_PACKAGE_MB", 2048) * MB,
                             _env_int("STEMBOX_MAX_VIDEO_MB", 500) * MB) + MB

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 12  # 12 hours: one school day

    WTF_CSRF_TIME_LIMIT = None  # token valid for the whole session
    WTF_CSRF_ENABLED = _env_bool("STEMBOX_CSRF", True)

    # Seconds a Wi-Fi transfer request may take before giving up.
    SYNC_TIMEOUT = _env_int("STEMBOX_SYNC_TIMEOUT", 600)
