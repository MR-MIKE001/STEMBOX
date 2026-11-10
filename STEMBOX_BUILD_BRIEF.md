# STEMBox: Build Brief for Claude Code

## How to use this file

Put this file in the root of an empty project folder and start Claude Code there. Then send:

> Read STEMBOX_BUILD_BRIEF.md. Build STEMBox phase by phase, starting with Phase 0. After each phase, run the tests, fix failures, and report which acceptance tests pass before moving on.

---

## 1. Role and goal

Build the complete STEMBox software: the database, backend, frontend, UI/UX, dashboards, and testing. Claude Code writes the code, but every change must be reviewed, integrated, and tested before the phase is considered done.

The objective is a functional STEMBox platform, ready to receive educational content, that can demonstrate its core capabilities to competition judges.

## 2. Working rules

- **Working software over looks.** Every button, form, and workflow must actually function. A screen that looks right but does nothing does not count as done.
- **Modular and extensible.** Separate the app into blueprints or modules (auth, users, classes, cms, learning, quizzes, progress, sync). New subjects, courses, and lessons must be addable without code changes.
- **No hardcoded courses or videos.** Real content will arrive later. Sample or placeholder content is for development and testing only, and must be clearly marked as such. It should be loadable and removable through a seed command.
- **Works offline.** Do not load fonts, CSS frameworks, scripts, or icons from a CDN. Vendor everything locally.
- **Lightweight.** Target low-power devices such as a Raspberry Pi. Keep dependencies minimal.
- **Simple UI.** Some teachers and students have little digital experience. Use large buttons, plain language, clear labels, and few steps per task.
- **Respond to the user at phase boundaries.** Do not ask about every small decision. Choose sensible defaults, record them in `docs/DECISIONS.md`, and keep going.

## 3. Recommended stack

- Python 3.11+ with Flask
- SQLite for the database (use WAL mode)
- Local server file storage under `storage/`, outside the code folder
- HTML, CSS, and vanilla JavaScript for the frontend (no build step required)
- Passwords hashed with Werkzeug or argon2
- Flask-WTF or an equivalent CSRF token on every form
- Configuration through environment variables or a `config.py` file, with a `.env.example`
- Tests with pytest

## 4. Requirements (from the assignment)

### 4.1 Platform
- Responsive, browser-based. Works on smartphones, tablets, and computers.

### 4.2 User management (A)
- Three roles: administrator, teacher, student.
- Login and logout.
- Role-based permissions enforced on the server, not just hidden in the UI.
- User management: create, edit, deactivate, reset password, import users from CSV.
- Class management: create classes, assign teachers and students to classes.

### 4.3 Teacher dashboard (B)
- Create and manage courses and lessons.
- Upload educational materials.
- Organise content by subject, class, level, topic, and course.
- Create quizzes and assessments.
- Monitor student activity and performance.

### 4.4 Student dashboard (C)
- Browse available subjects and courses.
- Access lessons and learning materials.
- Watch videos and read PDFs.
- Take quizzes and assessments.
- View learning progress and previous results.

### 4.5 Content management system (D)
- Teachers and administrators can create, edit, organise, and publish learning materials.
- Supported types: PDF, video, image, audio, and written lessons.
- Upload, preview, update, and remove content.
- Search and categorisation.
- Configurable publishing permission, set by the administrator:
  - Mode 1: teachers publish directly.
  - Mode 2: publishing requires administrator approval.
  - Mode 3: mixed. For example, a teacher publishes directly in their own class but needs approval for subject-wide content.
- Store the chosen mode in settings and enforce it in the server.

### 4.6 Assessment and progress (E)
- Multiple-choice quizzes.
- Automatic marking of objective questions.
- Store scores, attempts, and completed lessons.
- Class performance summaries for teachers.

### 4.7 Offline functionality (F)
- Host on a local server. Several devices connect over local Wi-Fi.
- Works with no internet access.
- Lessons, videos, PDFs, quizzes, and saved progress remain available locally.
- Interface is lightweight and responsive.

### 4.8 Content transfer (G)
- USB export and import of content packages.
- Transfer between STEMBox installations over local Wi-Fi.
- Validate imported packages. Reject invalid packages. Skip or merge duplicates.

### 4.9 Interface
Required screens:
- Login and registration
- Administrator dashboard
- Teacher dashboard
- Student dashboard
- Course and lesson pages
- Video and PDF viewing pages
- Quiz and assessment pages
- Content upload and management pages
- User management and settings

## 5. Suggested project structure

```
stembox/
  app/
    __init__.py          # app factory
    config.py
    db.py                # SQLite connection, schema init, migrations
    auth/                # login, logout, decorators for role checks
    users/               # users, classes, CSV import
    cms/                 # subjects, courses, lessons, resources, publishing
    learning/            # student views, lesson and resource viewing
    quizzes/             # quiz builder, attempts, auto-marking
    progress/            # progress tracking, teacher summaries
    sync/                # USB export/import, Wi-Fi transfer, package validation
    templates/
    static/              # local CSS and JS only
  storage/               # uploaded files (created at runtime)
  packages/              # exported packages
  seed/                  # placeholder development content, clearly labelled
  tests/
  docs/
    ARCHITECTURE.md
    DATABASE.md
    INSTALL.md
    TESTING.md
    DECISIONS.md
  run.py
  requirements.txt
  .env.example
  README.md
```

## 6. Suggested data model

- `users` (id, uuid, name, username, password_hash, role, active, created_at)
- `classes` (id, uuid, name, level)
- `class_members` (class_id, user_id, role_in_class)
- `subjects` (id, uuid, name, description)
- `courses` (id, uuid, subject_id, title, level, description, published)
- `topics` (id, course_id, title, position)
- `lessons` (id, uuid, course_id, topic_id, title, body_html, status, author_id, published_at, position)
- `resources` (id, uuid, lesson_id, type, title, file_path, mime_type, size, checksum, created_at)
- `publish_requests` (id, lesson_id, requested_by, status, reviewed_by, reviewed_at)
- `settings` (key, value), including `publish_mode`
- `quizzes` (id, uuid, lesson_id or course_id, title, pass_mark)
- `questions` (id, quiz_id, text, position)
- `choices` (id, question_id, text, is_correct)
- `attempts` (id, quiz_id, user_id, score, total, started_at, finished_at)
- `attempt_answers` (attempt_id, question_id, choice_id)
- `progress` (user_id, lesson_id, completed_at, last_position)
- `audit_log` (id, user_id, action, target, created_at)
- `content_packages` (id, uuid, checksum, imported_at, source)

Use UUIDs for anything that is exported or synced, so that duplicates can be detected across installations.

## 7. Phases

Build and test each phase before starting the next. At the end of each phase, report what works, what doesn't, and what is deferred.

**Phase 0: Scaffold.** Project layout, app factory, config, database init, a `flask init-db` command, a `flask seed-dev` command, and a pytest setup with one passing smoke test.

**Phase 1: Auth and users.** Login, logout, password hashing, role decorators, admin user management (create, edit, deactivate, reset password, CSV import), classes, and class membership. Test that a student cannot reach teacher or admin routes.

**Phase 2: CMS.** Subjects, courses, topics, and lessons. Resource upload for PDF, video, image, and audio, with type and size validation and safe filenames. Preview, edit, replace, and delete. Search and filters by subject, class, level, and topic. Publishing workflow with the three modes, enforced server-side. Administrator approval queue.

**Phase 3: Student learning.** Subject and course browsing, lesson pages, an in-browser PDF viewer, a video player with range-request support, an audio player, and image display. Mark lessons as complete.

**Phase 4: Quizzes.** Quiz builder for teachers. Multiple-choice questions and choices. Student attempts. Automatic marking. Store every attempt. Show previous results to the student.

**Phase 5: Monitoring.** Teacher view of class performance: completion rates, average scores, per-student detail, per-quiz breakdown. Simple CSV export of the summary.

**Phase 6: Offline and local network.** Bind to `0.0.0.0` by default. Show the server's LAN address on the admin page so other devices can connect. Confirm there are no external asset requests. Test with the network disconnected.

**Phase 7: Content transfer.**
- USB export: a zip package containing a `manifest.json` (package UUID, version, created_at, item list) and a `checksums.sha256` file. Include the database records and files for the selected subjects or courses.
- USB import: validate the manifest and checksums before writing anything. Reject packages with bad checksums, unknown versions, or path traversal. Skip items whose UUID and checksum already exist. Report what was added, updated, skipped, and rejected.
- Wi-Fi transfer: an administrator on one installation can pull a package from another installation on the same network. Require admin credentials on the source. Use the same validation as USB import.

**Phase 8: UI polish.** Consistent layout, large touch targets, plain-language labels, mobile navigation, and tested layouts at phone, tablet, and desktop widths.

**Phase 9: Testing, docs, and delivery.** Run the full acceptance checklist below and write `docs/TESTING.md` with the results. Complete the architecture, database, install, and decisions documents.

## 8. Security requirements

- Enforce role checks on every server route.
- CSRF protection on all POST forms.
- Validate upload type, size, and filename. Store files under generated names. Never trust the client filename.
- Prevent path traversal in both upload and import.
- Set a maximum upload size in config.
- Log admin actions to `audit_log`.
- Never store plain-text passwords. Seed accounts must have changed passwords or a forced-reset flag.

## 9. Acceptance tests

Each item must have an automated test where possible, or a documented manual test in `docs/TESTING.md`.

1. Users can log in according to their assigned roles, and are refused access outside them.
2. Teachers can create and publish lessons, subject to the configured publishing mode.
3. Teachers can upload PDFs and videos.
4. Students can access and view learning materials.
5. Students can complete quizzes, and their results are saved.
6. Teachers can monitor student progress.
7. The platform works without an internet connection.
8. Multiple devices can access it over local Wi-Fi.
9. Content can be exported and imported through USB.
10. Data remains saved after the server restarts.
11. The platform works properly on phones and computers.

## 10. Expected deliverables

1. Complete working STEMBox software.
2. Functional administrator, teacher, and student dashboards.
3. Fully implemented content management system.
4. Offline local-network functionality.
5. Assessment and progress-tracking system.
6. USB content import and export.
7. Responsive, professional user interface.
8. Database and system architecture documentation (`docs/ARCHITECTURE.md`, `docs/DATABASE.md`).
9. Installation and setup instructions (`docs/INSTALL.md`, `README.md`).
10. Testing report demonstrating that the essential features work (`docs/TESTING.md`).

## 11. Decisions to make and record

Choose defaults and record them in `docs/DECISIONS.md`:

- Default publishing mode (suggest Mode 2, administrator approval, as the safest default).
- Maximum upload sizes per resource type.
- Whether students can self-register or only administrators create accounts (suggest admin-created accounts, with an optional registration toggle).
- Wi-Fi transfer authentication method.
- Port and hostname defaults.
