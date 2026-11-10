-- STEMBox schema, migration 1.
-- Every table that can be exported/synced carries a `uuid` so duplicates can be
-- detected across installations. Timestamps are ISO-8601 UTC strings.

CREATE TABLE users (
    id                   INTEGER PRIMARY KEY,
    uuid                 TEXT NOT NULL UNIQUE,
    name                 TEXT NOT NULL,
    username             TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash        TEXT NOT NULL,
    role                 TEXT NOT NULL CHECK (role IN ('admin', 'teacher', 'student')),
    active               INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    is_sample            INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    last_login_at        TEXT
);

CREATE TABLE classes (
    id         INTEGER PRIMARY KEY,
    uuid       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    level      TEXT NOT NULL DEFAULT '',
    is_sample  INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE class_members (
    class_id      INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_in_class TEXT NOT NULL CHECK (role_in_class IN ('teacher', 'student')),
    PRIMARY KEY (class_id, user_id)
);
CREATE INDEX idx_class_members_user ON class_members(user_id);

CREATE TABLE subjects (
    id          INTEGER PRIMARY KEY,
    uuid        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    is_sample   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

-- class_id NULL  = subject-wide course (visible to every student)
-- class_id set   = class course (visible only to members of that class)
CREATE TABLE courses (
    id          INTEGER PRIMARY KEY,
    uuid        TEXT NOT NULL UNIQUE,
    subject_id  INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    class_id    INTEGER REFERENCES classes(id) ON DELETE SET NULL,
    title       TEXT NOT NULL,
    level       TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    author_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    is_sample   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX idx_courses_subject ON courses(subject_id);

CREATE TABLE topics (
    id        INTEGER PRIMARY KEY,
    uuid      TEXT NOT NULL UNIQUE,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    title     TEXT NOT NULL,
    position  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_topics_course ON topics(course_id);

-- status: draft | pending | published
-- body: written lesson text in the simple STEMBox markup (see app/utils.py),
-- always HTML-escaped when rendered.
CREATE TABLE lessons (
    id           INTEGER PRIMARY KEY,
    uuid         TEXT NOT NULL UNIQUE,
    course_id    INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    topic_id     INTEGER REFERENCES topics(id) ON DELETE SET NULL,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'pending', 'published')),
    author_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    position     INTEGER NOT NULL DEFAULT 0,
    is_sample    INTEGER NOT NULL DEFAULT 0,
    published_at TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX idx_lessons_course ON lessons(course_id);

-- file_path is relative to STORAGE_DIR/files and is always a generated name.
CREATE TABLE resources (
    id            INTEGER PRIMARY KEY,
    uuid          TEXT NOT NULL UNIQUE,
    lesson_id     INTEGER NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    type          TEXT NOT NULL CHECK (type IN ('pdf', 'video', 'image', 'audio')),
    title         TEXT NOT NULL,
    file_path     TEXT NOT NULL,
    original_name TEXT NOT NULL DEFAULT '',
    mime_type     TEXT NOT NULL,
    size          INTEGER NOT NULL,
    checksum      TEXT NOT NULL,
    position      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX idx_resources_lesson ON resources(lesson_id);

-- target_type: lesson | quiz
CREATE TABLE publish_requests (
    id           INTEGER PRIMARY KEY,
    target_type  TEXT NOT NULL CHECK (target_type IN ('lesson', 'quiz')),
    target_id    INTEGER NOT NULL,
    requested_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled')),
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    reviewed_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    reviewed_at  TEXT
);
CREATE INDEX idx_publish_requests_target ON publish_requests(target_type, target_id);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE quizzes (
    id          INTEGER PRIMARY KEY,
    uuid        TEXT NOT NULL UNIQUE,
    course_id   INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    lesson_id   INTEGER REFERENCES lessons(id) ON DELETE SET NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    pass_mark   INTEGER NOT NULL DEFAULT 50,
    status      TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'pending', 'published')),
    author_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    is_sample   INTEGER NOT NULL DEFAULT 0,
    published_at TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX idx_quizzes_course ON quizzes(course_id);

CREATE TABLE questions (
    id       INTEGER PRIMARY KEY,
    uuid     TEXT NOT NULL UNIQUE,
    quiz_id  INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
    text     TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_questions_quiz ON questions(quiz_id);

CREATE TABLE choices (
    id          INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    text        TEXT NOT NULL,
    is_correct  INTEGER NOT NULL DEFAULT 0,
    position    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_choices_question ON choices(question_id);

-- finished_at NULL means the attempt is still in progress.
CREATE TABLE attempts (
    id          INTEGER PRIMARY KEY,
    uuid        TEXT NOT NULL UNIQUE,
    quiz_id     INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    score       INTEGER NOT NULL DEFAULT 0,
    total       INTEGER NOT NULL DEFAULT 0,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX idx_attempts_quiz_user ON attempts(quiz_id, user_id);

-- Question/choice text is copied in so results stay readable if the quiz is edited later.
CREATE TABLE attempt_answers (
    attempt_id    INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    question_id   INTEGER REFERENCES questions(id) ON DELETE SET NULL,
    choice_id     INTEGER REFERENCES choices(id) ON DELETE SET NULL,
    question_text TEXT NOT NULL,
    choice_text   TEXT NOT NULL DEFAULT '',
    correct_text  TEXT NOT NULL DEFAULT '',
    is_correct    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_attempt_answers_attempt ON attempt_answers(attempt_id);

CREATE TABLE progress (
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    lesson_id      INTEGER NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    completed_at   TEXT,
    last_position  TEXT NOT NULL DEFAULT '',
    last_viewed_at TEXT NOT NULL,
    PRIMARY KEY (user_id, lesson_id)
);
CREATE INDEX idx_progress_lesson ON progress(lesson_id);

CREATE TABLE audit_log (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action     TEXT NOT NULL,
    target     TEXT NOT NULL DEFAULT '',
    details    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE content_packages (
    id          INTEGER PRIMARY KEY,
    uuid        TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT '',
    summary     TEXT NOT NULL DEFAULT ''
);
