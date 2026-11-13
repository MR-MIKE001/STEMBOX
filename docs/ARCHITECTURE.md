# STEMBox architecture

STEMBox is a single Python web application that runs on a small computer, such as a Raspberry Pi, in a school. Phones, tablets and computers connect to it over the local Wi-Fi with an ordinary web browser. Nothing is loaded from the internet.

```
 ┌────────────┐  ┌────────────┐  ┌────────────┐
 │  Phone     │  │  Tablet    │  │  Laptop    │     any browser, no app to install
 └─────┬──────┘  └─────┬──────┘  └─────┬──────┘
       └───────── local Wi-Fi (http://192.168.x.x:5000) ─────────┐
                                                                  ▼
 ┌──────────────────────────── STEMBox server ────────────────────────────┐
 │ run.py ─ Waitress (8 threads, 0.0.0.0:5000)                            │
 │   └─ Flask app (app/__init__.py: create_app)                           │
 │        auth · main · users · cms · learning · quizzes · progress · sync│
 │        permissions.py · publishing.py · storage.py · db.py             │
 │   ├─ storage/stembox.db      SQLite (WAL mode)                         │
 │   ├─ storage/files/…         uploaded PDFs, videos, pictures, audio    │
 │   └─ packages/…              exported content packages                 │
 └──────────────────────────────▲─────────────────────────────────────────┘
                                │ Wi-Fi pull (HTTP Basic, admin account) or USB stick (.zip)
                       ┌────────┴────────┐
                       │ another STEMBox │
                       └─────────────────┘
```

## Code layout

| Path | Responsibility |
|---|---|
| `run.py` | Starts the server (Waitress if installed) and prints the addresses devices should use. |
| `app/__init__.py` | App factory: config, secret key, CSRF, blueprints, error pages, security headers (CSP). |
| `app/config.py` | All settings, overridable by environment variables or `.env`. |
| `app/db.py` | SQLite connection per request, migrations (`PRAGMA user_version`), settings table, `audit()` helper. |
| `app/schema.sql` | Migration 1: the full schema (see `DATABASE.md`). |
| `app/auth/` | Login, logout, password change (role-based rules), idle-session timeout, lockout, `login_required` / `roles_required` decorators. |
| `app/main/` | Home redirect, first-run `/setup`, the three role dashboards. |
| `app/users/` | Admin: users, CSV import, classes and membership, settings, activity log. URL prefix `/admin`. |
| `app/cms/` | Subjects, courses, topics, lessons, file upload/replace/delete, search and filters, publish actions, approval queue. URL prefix `/content`. |
| `app/learning/` | Student area: browse, lesson pages, file viewer pages, `/media/<uuid>` streaming with HTTP Range, completion, saved video position, search. |
| `app/quizzes/` | Quiz builder, publishing, taking quizzes, automatic marking, results. |
| `app/progress/` | Class reports, per-student and per-quiz detail, CSV export. URL prefix `/progress`. |
| `app/sync/` | Transfer pages (`/transfer`), the package format (`package.py`), and the API other STEMBoxes call (`/api/sync/*`). |
| `app/permissions.py` | **The single place that decides who may see, manage and publish what.** Every route calls it. |
| `app/publishing.py` | The draft → pending → published workflow shared by lessons and quizzes. |
| `app/storage.py` | Safe file storage: type and magic-byte checks, size limits, generated names, path containment. |
| `app/seeding.py`, `seed/` | `[SAMPLE]` placeholder content, loaded and removed with `flask seed-dev` / `flask unseed-dev`. |
| `app/templates/`, `app/static/` | Jinja templates, one stylesheet, one small script, one icon. All local. |
| `tools/ui_check.py` | Dev tool: headless Chrome layout check at phone, tablet and desktop widths. |

## Request flow

1. `auth.load_logged_in_user` runs before every request. It loads the user from the signed session cookie and drops the session if the account was switched off or its password was reset. Users who must change their password are sent to that page.
2. A route decorator (`login_required` or `roles_required(...)`) rejects the wrong roles with **403**.
3. The view loads the object and calls `permissions.*` to check access to *that* object: `can_manage_course`, `can_view_lesson`, and so on. Objects a student may not see return **404**, so their existence is not revealed.
4. Every POST must carry a CSRF token (Flask-WTF). The `/api/*` endpoints are the exception: they use HTTP Basic auth, not cookies.
5. Static links carry `?v=<file modification time>`, so browsers can cache CSS, JS, fonts and images for a week (`SEND_FILE_MAX_AGE_DEFAULT`) and still fetch a changed file straight away.
6. Responses get `X-Content-Type-Options`, `X-Frame-Options` and `Referrer-Policy` headers. HTML pages also get a strict Content-Security-Policy.

## Key workflows

**Publishing.** `publishing.publish()` asks `permissions.can_publish_directly()`, which reads the `publish_mode` setting:
- If the user may publish directly, the item becomes `published`.
- Otherwise the item becomes `pending` and a row goes into `publish_requests`. The admin then approves (published) or rejects with a note (draft) in **Approvals**.

When someone without direct rights edits a published item, `publishing.content_changed()` puts it back to `pending`.

**Files.** Upload, then `storage.store_stream()`:
1. Check the extension for the declared type.
2. Read the first 16 bytes and compare the magic-byte family.
3. Stream the file to `storage/tmp` while hashing (SHA-256) and enforcing the size limit.
4. Move it to `storage/files/ab/<uuid>.<ext>`.

`/media/<uuid>` checks permissions, then uses `send_file(conditional=True)`, which supports `Range` requests (`206 Partial Content`) so videos can seek.

**Quizzes.** Start creates an `attempts` row. Submit marks each question against `choices.is_correct`, stores `attempt_answers` (with a copy of the text), and closes the attempt with `score/total`. Each attempt can be submitted once.

**Monitoring.** `progress.class_summary(class_id, course_id=None)` computes everything with a handful of queries:
- the students in the class, and the published lessons and quizzes in courses visible to it
- each student's completion, best score per quiz, attempts and last activity
- per-quiz averages and pass rates

The class page, dashboards and CSV export all reuse it.

**Transfer.** `sync/package.py`: `build_package()` writes the zip. `validate_package()` checks everything and raises `PackageError` with plain-language problems. `import_package()` runs inside one `BEGIN IMMEDIATE` transaction. Wi-Fi pull: the admin types the address and the source's admin credentials. The target calls `GET /api/sync/catalog`, the admin picks courses, and the target calls `POST /api/sync/package`. The downloaded zip goes through exactly the same `import_package()` as a USB upload.

## Extending STEMBox

- **New subjects, courses, lessons and quizzes** are data, created in the UI or imported as packages. No code changes are needed.
- **A new file type:** add it to `storage.ALLOWED` (extension, MIME type, magic family), then teach `_resource.html` how to show it.
- **A schema change:** append a SQL string to `MIGRATIONS` in `app/db.py`. Existing installations upgrade on their next start.
- **A new module:** create a blueprint package under `app/`, register it in `create_app()`, and add menu entries in `app/nav.py`. Use `roles_required` and `permissions` for access.
- **Package format changes:** bump `VERSION` in `sync/package.py` and add the old version to `SUPPORTED_VERSIONS` while it can still be read.

## Performance notes (Raspberry Pi)

- SQLite in WAL mode lets readers and a writer work at the same time. With Waitress's 8 threads, a classroom of 30–40 devices browsing at once is fine.
- Media is streamed straight from disk in chunks, so it is never fully loaded into memory.
- Pages are server-rendered HTML with one ~15 KB stylesheet and one ~4 KB script, and no client framework.
- Password hashing (scrypt) costs about 0.1–0.3 s per login on a Pi. CSV-importing hundreds of students takes a few minutes for this reason.
