# STEMBox database

- **Engine:** SQLite 3, in WAL mode, with foreign keys enforced (`PRAGMA foreign_keys = ON`).
- **Location:** `storage/stembox.db` by default (`STEMBOX_DATABASE` to change).
- **Source of truth:** `app/schema.sql` (migration 1). Later changes are appended to `MIGRATIONS` in `app/db.py`, and the applied version is kept in `PRAGMA user_version`. Migrations run automatically when the server starts.

| Version | Change |
|---|---|
| 1 | Base schema (all tables below). |
| 2 | Removes the obsolete `allow_registration` setting row. Public self-registration was removed; no table or column changed, and no data is lost. |
- **Timestamps** are ISO-8601 UTC text, for example `2026-11-11T09:30:00+00:00`.
- **UUIDs:** every table that can be exported or synced has a `uuid` column, so the same item is recognised on another STEMBox.
- **Sample data:** rows created by `flask seed-dev` have `is_sample = 1`, and `flask unseed-dev` removes them.

## Entity relationships

```
users ─┬─< class_members >─ classes
       │                       │
       │            (optional) │ class_id
subjects ─< courses ──────────┘
              ├─< topics
              ├─< lessons ─< resources          (files on disk)
              │      └── topic_id → topics
              └─< quizzes ─< questions ─< choices
                     └── lesson_id → lessons (optional)

users ─< attempts >─ quizzes        attempts ─< attempt_answers
users ─< progress >─ lessons
publish_requests → lessons | quizzes (target_type + target_id)
settings, audit_log, content_packages (stand-alone)
```

## Tables

### users
| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| uuid | TEXT UNIQUE | |
| name | TEXT | full name shown in the UI |
| username | TEXT UNIQUE, NOCASE | `[a-z0-9._-]{3,40}` |
| password_hash | TEXT | Werkzeug scrypt (never plain text) |
| role | TEXT | `admin`, `teacher`, `student` |
| active | INTEGER | 0 = switched off (cannot log in) |
| must_change_password | INTEGER | 1 = must choose a new password at next login |
| is_sample | INTEGER | seed data flag |
| created_at, last_login_at | TEXT | |

### classes / class_members
- `classes(id, uuid, name, level, is_sample, created_at)`
- `class_members(class_id → classes ON DELETE CASCADE, user_id → users ON DELETE CASCADE, role_in_class ∈ {teacher, student})`, primary key `(class_id, user_id)`. Admins are never members.

### subjects
`(id, uuid, name, description, created_by → users, is_sample, created_at)`

### courses
| Column | Notes |
|---|---|
| subject_id → subjects | CASCADE |
| class_id → classes | **NULL = whole subject (every student); set = only that class.** SET NULL if the class is deleted. |
| title, level, description | `level` is free text (for example "Grade 7"), used for filtering |
| author_id → users | creator; the creator may manage the course |
| is_sample, created_at, updated_at | |

### topics
`(id, uuid, course_id → courses CASCADE, title, position)`. Groups lessons within a course.

### lessons
| Column | Notes |
|---|---|
| course_id → courses | CASCADE |
| topic_id → topics | optional; SET NULL when a topic is removed |
| title, body | `body` is written text in the STEMBox markup (escaped when shown) |
| status | `draft`, `pending` (waiting for approval), `published` |
| author_id, position, published_at, created_at, updated_at, is_sample | |

### resources
| Column | Notes |
|---|---|
| lesson_id → lessons | CASCADE (the file on disk is removed by the application) |
| type | `pdf`, `video`, `image`, `audio` |
| title | shown to students |
| file_path | **generated** path relative to `storage/files`, for example `3f/3f9c…e1.mp4` |
| original_name | the uploader's file name (label only, never used as a path) |
| mime_type, size, checksum | checksum = SHA-256 of the file |
| position, created_at, updated_at | |

### publish_requests
`(id, target_type ∈ {lesson, quiz}, target_id, requested_by, status ∈ {pending, approved, rejected, cancelled}, note, created_at, reviewed_by, reviewed_at)`. At most one `pending` request exists per item. `note` holds the admin's reason when sending an item back.

### settings
Key/value text. The keys:
| Key | Default | Meaning |
|---|---|---|
| `publish_mode` | `approval` | `direct`, `approval` or `mixed` |
| `allow_remote_pull` | `0` | other STEMBoxes may pull content over Wi-Fi |
| `site_name` | `STEMBox` | name in the page header |

### quizzes / questions / choices
- `quizzes(id, uuid, course_id → courses CASCADE, lesson_id → lessons SET NULL, title, description, pass_mark 0–100, status, author_id, is_sample, published_at, created_at, updated_at)`
- `questions(id, uuid, quiz_id → quizzes CASCADE, text, position)`
- `choices(id, question_id → questions CASCADE, text, is_correct, position)`. Exactly one correct choice per question; the application enforces this before publishing.

### attempts / attempt_answers
- `attempts(id, uuid, quiz_id, user_id, score, total, started_at, finished_at)`. `finished_at` NULL means in progress. Every attempt is kept.
- `attempt_answers(attempt_id → attempts CASCADE, question_id → questions SET NULL, choice_id → choices SET NULL, question_text, choice_text, correct_text, is_correct)`. The text copies keep old results readable after a quiz is edited.

### progress
`(user_id, lesson_id)` primary key, plus:
- `completed_at`: NULL = opened but not finished
- `last_position`: JSON `{resource_uuid: seconds}` for video and audio
- `last_viewed_at`

### audit_log
`(id, user_id, action, target, details, created_at)`. Records administrative actions, for example `user.create`, `user.deactivate`, `user.reset_password`, `user.import`, `class.*`, `settings.change`, `approval.approve` / `reject`, `course.delete`, `package.export` / `import` / `reject` / `remote_export`.

### content_packages
`(id, uuid, checksum, imported_at, source, summary)`. One row per imported package. `summary` is JSON with the added, updated, skipped and rejected counts.

## Backups

Stop the server, or use `sqlite3 storage/stembox.db ".backup backup.db"` while it runs, then copy the whole `storage/` folder. Restoring means putting the folder back.
