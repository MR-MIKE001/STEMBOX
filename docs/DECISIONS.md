# Design decisions

Defaults chosen during the build, with the reason for each. Settings marked
*(setting)* can be changed by an administrator in **Settings** without touching code;
*(config)* means an environment variable or `.env` entry (see `.env.example`).

## Required decisions (brief §11)

| Topic | Decision | Why |
|---|---|---|
| Default publishing mode | **Mode 2: administrator approval** *(setting)* | Safest default: nothing reaches students unchecked. The admin can switch to *direct* or *mixed* in Settings. |
| Mixed mode rule | A teacher publishes directly to a **course that belongs to a class they teach**. A course with no class (whole subject, visible to every student) needs approval. | Matches the brief's example. It is enforced in `app/permissions.py::can_publish_directly`. |
| Max upload sizes | PDF **50 MB**, picture **10 MB**, audio **100 MB**, video **500 MB**, package **2 GB** *(config)* | Big enough for a typical lesson video on a Raspberry Pi SD card, small enough to stop accidents. `MAX_CONTENT_LENGTH` is set just above the largest limit. |
| Self-registration | **Off by default.** The admin creates accounts (one at a time or by CSV). An optional toggle *(setting)* lets students register themselves, as students only. | Schools usually control who has accounts. Registration can never create a teacher or admin. |
| Wi-Fi transfer authentication | **HTTP Basic auth with an administrator account of the *source* STEMBox**, and the source must have *“Allow another STEMBox on this Wi-Fi to copy content”* switched on *(setting, default off)*. Failed attempts count towards the normal login lockout. Each pull is written to the source's audit log. | Simple, needs no extra keys, and requires two things: a source admin's consent (the toggle) and a source admin's password. |
| Port and host | Host **0.0.0.0** (all interfaces), port **5000** *(config)* | 0.0.0.0 lets phones on the Wi-Fi connect. 5000 needs no root privileges on a Pi. |

## Other decisions

**Platform and dependencies**
- **Flask + SQLite (WAL) + vanilla JS/CSS.** Runtime dependencies: Flask, Flask-WTF (used only for CSRF), python-dotenv and Waitress. All are pure Python and install on Raspberry Pi OS. There is no build step.
- **Waitress** is the production server, started by `run.py` with 8 threads. If it isn't installed, `run.py` falls back to Flask's threaded server.
- **No external assets.** We use system fonts, a local SVG icon and local CSS/JS. A Content-Security-Policy (`default-src 'self'`) on every HTML page makes the browser refuse anything from elsewhere. A test checks this.
- **No inline styles or scripts**, so the CSP stays strict. Progress bars use the native `<progress>` element.

**Data and storage**
- **Mutable data lives in `storage/`**: the database, uploaded files, and the secret key. Exported packages go in `packages/`. Both are outside the code folder (`app/`), so one folder holds everything to back up.
- **The secret key is generated on first start** and saved in `storage/secret_key`, so logins survive a restart. You can override it with `STEMBOX_SECRET_KEY`.
- **Uploaded files are stored as `files/<2 hex>/<random uuid><ext>`.** The client's file name is kept only as a label (`original_name`). Types are checked by extension *and* by the file's first bytes ("magic bytes"). SVG and HTML are not accepted, because they could carry scripts.
- **Allowed types:** PDF; MP4/M4V/WebM/OGV video; PNG/JPG/GIF/WebP pictures; MP3/M4A/OGG/WAV/FLAC audio. These are the formats phone and desktop browsers can play without plug-ins.
- **Schema migrations** use `PRAGMA user_version` and a list in `app/db.py`. The database is created or upgraded automatically on start, so `flask init-db` is optional.

**Content model**
- **Written lessons use a tiny markup**: `#` headings, `-` lists, `**bold**`, `_italic_`. We chose this over stored HTML. All text is HTML-escaped before rendering, so lessons can't inject scripts, and no HTML-sanitiser dependency is needed. (The brief's `body_html` column is called `body` for this reason.)
- **Publishing is per lesson and per quiz** (draft → pending → published). A course appears to students once it has at least one published lesson or quiz. This replaces a separate `courses.published` flag, so there is one less workflow to understand.
- **Editing published content.** When a teacher who can't publish directly edits a published lesson or quiz (text, files, questions, settings), it goes back to *waiting for approval* and is hidden from students until approved. Unapproved changes can never reach students. Moving a course to a wider audience (for example from a class to the whole subject) re-checks its published items the same way.
- **Course audience:** each course is either for one class or for the whole subject (every student). Teachers can only choose classes they teach.
- **Who manages a course:** administrators manage every course. A teacher manages the courses they created, plus any course that belongs to a class they teach.
- **Subjects** can be created by teachers and admins. Only the creator or an admin can edit one, and a subject can only be deleted when it is empty.
- **Class management is admin-only.** Teachers see their classes in Progress.

**Quizzes and progress**
- **Multiple choice, one correct answer per question, 2–6 answers.** Unanswered questions count as wrong. Students can retake a quiz; every attempt is stored, and reports use each student's *best* score per quiz.
- **Attempt answers keep a copy of the question and answer text**, so old results stay readable after a teacher edits or deletes questions.
- **Staff don't create attempts or progress.** Teachers see a preview with the correct answers marked, so their visits don't skew reports.
- **Video and audio position** is saved every few seconds and restored when the student returns. It is stored per file in `progress.last_position` as JSON.

**PDF viewing**
- We use **the browser's built-in PDF viewer** (`<object>`), with a large "Open the PDF" / "Download" fallback. Desktop browsers show PDFs inline. Most phone browsers open them in their own viewer. We didn't bundle pdf.js (about 3 MB of vendored code) to stay lightweight. It can be added later under `app/static/` if inline PDF on phones becomes a requirement.

**Users and security**
- **Passwords:** Werkzeug `scrypt` hashes, minimum 6 characters (many students are young). Temporary passwords skip look-alike characters (0/O, 1/l/I).
- **Every account created by an admin, CSV import or the seed** must choose a new password at first login.
- **After 5 wrong passwords**, that username is locked for 5 minutes (in memory, per server process).
- **Resetting a password or switching an account off ends that user's existing sessions** at once (the session stores a fingerprint of the password hash).
- **The last active administrator** can't be switched off or demoted, and admins can't switch off their own account.
- **First run:** while there are no users at all, `/setup` lets you create the first administrator from the browser. `flask create-admin` does the same from the command line.
- **CSV import** accepts comma, semicolon or tab separators and UTF-8 or Latin-1. Valid rows are imported and invalid rows are reported line by line. Generated passwords are shown once, on the result page.

**Content transfer**
- **Package format v1:** a zip containing `manifest.json`, `data/content.json`, `files/` and `checksums.sha256`. Records keep their UUIDs. Each manifest item carries a SHA-256 of its *content* fields; parent links are compared separately.
- **On import:**
  - A new UUID is **added**.
  - A known UUID with the same checksum and parents is **skipped**.
  - A known UUID with different content is **updated** (the package version wins).
  - A file that fails the per-item checks is **rejected**.
  - A subject with a new UUID but the same name as an existing subject is **merged** into the existing one.
- **Validation happens before anything is written:** zip sanity, safe names only (no `..`, absolute paths, drive letters, backslashes or links), every file checksummed, known format and version, record schema, references, file types and magic bytes, and the manifest matching the contents. Files are stored first under new names. If the database transaction fails, they are deleted again.
- **Not transferred:** classes, users, student results, and the course-to-class link. These belong to each school. Imported content is owned by the importing admin and is visible to the whole subject.
- **Drafts** are exported only when you tick "Also copy drafts".
- **USB:** in the browser, the admin downloads the package and saves it to a USB stick, then uploads it on the other box. On the server itself, `flask export-package --out /media/usb/x.zip` and `flask import-package` work directly with a stick plugged into the Pi.
- **Wi-Fi pull is limited to local-network addresses:** private, loopback or link-local IPs, or names ending in `.local`, `.lan`, `.home` or `.internal`. This stops the feature being used to reach the internet. System proxies are ignored.
- **Kept packages:** the 10 most recent exports stay in `packages/`.

**UI**
- **Mobile first.** Buttons and inputs are at least 44–48px tall. The menu collapses behind a "Menu" button below 900px. On phones, tables become stacked cards (`table.stack`). Labels use plain language ("Switch off" rather than "Deactivate", "Ask for approval to publish").
- **Every action works without JavaScript.** JS only adds conveniences: the menu toggle, confirmation dialogs, double-submit protection, filter auto-submit, and saving the video position.
