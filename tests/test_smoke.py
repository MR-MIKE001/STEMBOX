"""Phase 0: the application starts, the database is created and the seed commands work."""
from tests.conftest import db_one


def test_app_starts_and_redirects_to_setup(client):
    # A brand-new installation has no users, so the home page goes to first-run setup.
    response = client.get("/")
    assert response.status_code == 302
    assert "/setup" in response.headers["Location"]
    assert client.get("/setup").status_code == 200


def test_database_has_schema_and_wal(app):
    row = db_one(app, "PRAGMA journal_mode")
    assert row[0].lower() == "wal"
    assert db_one(app, "SELECT value FROM settings WHERE key = 'publish_mode'")[0] == "approval"
    assert db_one(app, "PRAGMA user_version")[0] >= 1


def test_static_assets_served_locally(client):
    assert client.get("/static/css/stembox.css").status_code == 200
    assert client.get("/static/js/stembox.js").status_code == 200


def test_init_db_command(app):
    result = app.test_cli_runner().invoke(args=["init-db"])
    assert result.exit_code == 0, result.output
    assert "Database ready" in result.output


def test_seed_and_unseed_commands(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["seed-dev"])
    assert result.exit_code == 0, result.output
    assert db_one(app, "SELECT COUNT(*) FROM lessons WHERE is_sample = 1")[0] >= 3
    assert db_one(app, "SELECT COUNT(*) FROM resources")[0] >= 3
    # Seed accounts must be forced to change their password.
    assert db_one(app, "SELECT MIN(must_change_password) FROM users WHERE is_sample = 1")[0] == 1
    # Running it twice does not duplicate anything.
    runner.invoke(args=["seed-dev"])
    assert db_one(app, "SELECT COUNT(*) FROM subjects")[0] == 2

    result = runner.invoke(args=["unseed-dev"])
    assert result.exit_code == 0, result.output
    for table in ("users", "subjects", "courses", "lessons", "resources", "quizzes", "classes"):
        assert db_one(app, f"SELECT COUNT(*) FROM {table}")[0] == 0, table
