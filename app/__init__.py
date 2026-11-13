"""STEMBox application factory."""
import os
import secrets
from pathlib import Path

from flask import Flask, g, render_template, request
from flask_wtf.csrf import CSRFError, CSRFProtect

from . import db
from .config import Config

csrf = CSRFProtect()

__version__ = "1.0.0"


def _load_secret_key(app):
    if app.config.get("SECRET_KEY"):
        return
    key_file = Path(app.config["STORAGE_DIR"]) / "secret_key"
    if key_file.exists():
        app.config["SECRET_KEY"] = key_file.read_text(encoding="utf-8").strip()
    else:
        key = secrets.token_hex(32)
        key_file.write_text(key, encoding="utf-8")
        app.config["SECRET_KEY"] = key


def create_app(overrides=None):
    try:  # optional .env support
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except ImportError:
        pass

    app = Flask(__name__)
    app.config.from_object(Config)
    if overrides:
        app.config.update(overrides)

    storage = Path(app.config["STORAGE_DIR"])
    if not app.config.get("DATABASE"):
        app.config["DATABASE"] = str(storage / "stembox.db")
    for folder in (storage, storage / "files", storage / "tmp", Path(app.config["PACKAGES_DIR"])):
        folder.mkdir(parents=True, exist_ok=True)
    _load_secret_key(app)

    csrf.init_app(app)
    app.teardown_appcontext(db.close_db)

    # Create or upgrade the database automatically so a fresh install just works.
    db.init_db(app)

    from . import utils
    utils.register_template_helpers(app)

    from .auth import bp as auth_bp, load_logged_in_user
    from .main import bp as main_bp
    from .users import bp as users_bp
    from .cms import bp as cms_bp
    from .learning import bp as learning_bp
    from .quizzes import bp as quizzes_bp
    from .progress import bp as progress_bp
    from .sync import bp as sync_bp

    for blueprint in (auth_bp, main_bp, users_bp, cms_bp, learning_bp, quizzes_bp, progress_bp, sync_bp):
        app.register_blueprint(blueprint)

    # Machine-to-machine endpoints use HTTP Basic auth, not cookies, so they
    # do not need (and cannot send) a CSRF token.
    from .sync import api_bp
    csrf.exempt(api_bp)
    app.register_blueprint(api_bp)

    app.before_request(load_logged_in_user)

    from .cli import register_cli
    register_cli(app)

    @app.url_defaults
    def static_version(endpoint, values):
        """Add ?v=<file modification time> to static links so browsers can cache
        them for a week yet pick up a changed file immediately."""
        if endpoint == "static" and "filename" in values and "v" not in values:
            try:
                values["v"] = int(os.stat(os.path.join(app.static_folder, values["filename"])).st_mtime)
            except OSError:
                pass

    from .nav import nav_links

    @app.context_processor
    def inject_globals():
        return {
            "nav_links": nav_links,
            "current_user": g.get("user"),
            "site_name": _site_name(),
            "app_version": __version__,
        }

    def _site_name():
        try:
            return db.get_setting("site_name") or app.config["SITE_NAME"]
        except Exception:  # database not available (e.g. during an error page)
            return app.config["SITE_NAME"]

    @app.errorhandler(CSRFError)
    def csrf_error(e):
        return render_template("error.html", code=400, title="Form expired",
                               message="This form was open for too long or came from another page. "
                                       "Please go back, reload the page and try again."), 400

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("error.html", code=403, title="Not allowed",
                               message="Your account does not have permission to open this page."), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("error.html", code=404, title="Page not found",
                               message="We could not find that page. It may have been removed."), 404

    @app.errorhandler(413)
    def too_large(e):
        return render_template("error.html", code=413, title="File too large",
                               message="The file you sent is bigger than this STEMBox allows."), 413

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        # Everything is served locally: forbid loading anything from elsewhere.
        # (Only for HTML pages; media files are shown by the browser's own viewers.)
        if response.mimetype != "text/html":
            return response
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; media-src 'self'; "
            "object-src 'self'; frame-src 'self'; frame-ancestors 'self'; "
            "style-src 'self'; script-src 'self'; form-action 'self'; base-uri 'self'",
        )
        return response

    return app
