"""Command-line tools: `flask init-db`, `flask seed-dev`, `flask unseed-dev`,
`flask create-admin`, `flask export-package`, `flask import-package`."""
import click
from flask import current_app

from . import db


def register_cli(app):

    @app.cli.command("init-db")
    @click.option("--reset", is_flag=True, help="Delete ALL data and start with an empty database.")
    def init_db_command(reset):
        """Create the database tables (or upgrade them)."""
        if reset:
            click.confirm("This deletes every user, lesson and result. Continue?", abort=True)
        db.init_db(current_app, reset=reset)
        version = db.query("PRAGMA user_version", one=True)[0]
        click.echo(f"Database ready at {current_app.config['DATABASE']} (schema version {version}).")

    @app.cli.command("seed-dev")
    def seed_dev_command():
        """Load the [SAMPLE] placeholder content and accounts (for development/testing only)."""
        from .auth import hash_password
        from .seeding import load_sample
        with app.app_context():
            summary = load_sample(current_app, hash_password)
        click.echo("Sample content loaded: " + ", ".join(f"{k}={v}" for k, v in summary.items()))
        click.echo("Sample accounts (each must choose a new password at first login):")
        click.echo("  admin / admin123   teacher1 / teacher123   student1 / student123")

    @app.cli.command("unseed-dev")
    def unseed_dev_command():
        """Remove all [SAMPLE] content, accounts and files."""
        from .seeding import remove_sample
        summary = remove_sample(current_app)
        click.echo("Sample content removed: " + ", ".join(f"{k}={v}" for k, v in summary.items()))

    @app.cli.command("create-admin")
    @click.option("--username", prompt=True)
    @click.option("--name", prompt="Full name")
    @click.password_option()
    def create_admin_command(username, name, password):
        """Create an administrator account."""
        from .auth import hash_password, password_problem, username_problem
        username = username.strip().lower()
        problem = username_problem(username) or password_problem(password)
        if problem:
            raise click.ClickException(problem)
        db.execute(
            "INSERT INTO users (uuid, name, username, password_hash, role, active, created_at) "
            "VALUES (?, ?, ?, ?, 'admin', 1, ?)",
            (db.new_uuid(), name.strip(), username, hash_password(password), db.now()))
        click.echo(f"Administrator '{username}' created.")

    @app.cli.command("export-package")
    @click.option("--course", "courses", multiple=True, help="Course UUID (repeatable).")
    @click.option("--subject", "subjects", multiple=True, help="Subject UUID (repeatable).")
    @click.option("--all", "export_all", is_flag=True, help="Export every subject.")
    @click.option("--drafts", is_flag=True, help="Include unpublished lessons and quizzes.")
    @click.option("--out", "out_path", required=True, type=click.Path(dir_okay=False),
                  help="Where to write the .zip, e.g. a USB stick: /media/usb/maths.zip")
    def export_package_command(courses, subjects, export_all, drafts, out_path):
        """Export content to a package file (for USB transfer)."""
        import shutil
        from .sync.package import build_package
        if export_all:
            subjects = [r["uuid"] for r in db.query("SELECT uuid FROM subjects")]
        course_ids = [r["id"] for r in db.query(
            f"SELECT id FROM courses WHERE uuid IN ({','.join('?' * len(courses))})", courses)] if courses else []
        subject_ids = [r["id"] for r in db.query(
            f"SELECT id FROM subjects WHERE uuid IN ({','.join('?' * len(subjects))})", subjects)] if subjects else []
        if not course_ids and not subject_ids:
            raise click.ClickException("Nothing selected. Use --course, --subject or --all.")
        path, manifest = build_package(subject_ids, course_ids, include_drafts=drafts, source="command line")
        shutil.copyfile(path, out_path)
        click.echo(f"Package {manifest['package_uuid']} written to {out_path} ({len(manifest['items'])} items).")

    @app.cli.command("import-package")
    @click.argument("path", type=click.Path(exists=True, dir_okay=False))
    def import_package_command(path):
        """Validate and import a package file (for USB transfer)."""
        from .sync.package import PackageError, import_package
        try:
            report = import_package(path, source=f"file {path}", user_id=None)
        except PackageError as exc:
            raise click.ClickException("Package rejected: " + "; ".join(exc.problems))
        for key in ("added", "updated", "skipped", "rejected"):
            click.echo(f"{key}: {len(report[key])}")
