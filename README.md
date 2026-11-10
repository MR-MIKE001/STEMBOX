# STEMBox

**An offline learning platform for schools.**

STEMBox runs on a small, low-power computer such as a Raspberry Pi. Students and teachers connect from any phone, tablet or computer on the local Wi-Fi using an ordinary web browser. It needs **no internet connection** and **no app to install**.

```
 Phone   Tablet   Laptop          any web browser
   \       |       /
    local Wi-Fi  ──►  STEMBox (Raspberry Pi)  ◄──►  another STEMBox
                      lessons · videos · PDFs       (USB stick or Wi-Fi)
                      quizzes · progress
```

---

## Features

### For students
- Browse subjects and courses, and read lessons with pictures
- Watch videos (with seeking; the video resumes where you stopped), listen to audio, read PDFs
- Take multiple-choice quizzes, get marked straight away, and review the right answers
- See progress per course, finished lessons, and every past quiz result

### For teachers
- Create courses, organised by **subject, class, level, topic and course**
- Write lessons in simple formatting, and upload **PDF, video, picture and audio** files
- Build quizzes (2–6 answers per question, a pass mark you set, linked to a lesson)
- Publish directly or ask for approval, depending on the school's setting
- Monitor each class: completion rates, average scores, per-student detail, a per-question breakdown, and CSV export for Excel

### For administrators
- Manage accounts: create, edit, switch off, reset passwords, **import from CSV**
- Create classes and assign teachers and students
- Choose the **publishing mode**: teachers publish directly, administrator approval, or mixed
- An approval queue with notes back to the teacher
- The dashboard shows the Wi-Fi address devices should use
- **Copy content between STEMBoxes** by USB stick or over Wi-Fi. Every package is checked before anything is saved, and duplicates are skipped.
- An activity log of administrative actions

### Built for schools with limited resources
- **Fully offline.** No CDN fonts, scripts or icons. A strict Content-Security-Policy blocks external requests.
- **Lightweight.** Python, Flask and SQLite, with four small runtime dependencies and no build step. Runs on a Raspberry Pi.
- **Simple, mobile-first UI.** Large buttons and plain language. Every page is checked at phone, tablet and desktop sizes.
- **Secure by default.** Hashed passwords, CSRF protection, role checks on the server for every page, validated uploads stored under generated names, and a failed-login lockout.

---

## Quick start

Requires **Python 3.11 or newer**.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip install -r requirements.txt
.venv/bin/python run.py                        # Windows: .venv\Scripts\python run.py
```

The console prints the addresses to use:

```
 On this computer:   http://127.0.0.1:5000
 On the local Wi-Fi: http://192.168.1.20:5000
```

Open the address in a browser. On the first visit you create the **administrator** account.

### Try it with sample content

```bash
.venv/bin/flask seed-dev      # loads [SAMPLE] subjects, courses, lessons, quizzes and accounts
.venv/bin/flask unseed-dev    # removes every [SAMPLE] item again; real content is untouched
```

| Sample account | Password | Role |
|---|---|---|
| `admin` | `admin123` | Administrator |
| `teacher1` | `teacher123` | Teacher |
| `student1` | `student123` | Student |

Each sample account must choose a new password at its first login. The sample content is for development and testing only.

For a Raspberry Pi, including start-at-boot (systemd), a classroom hotspot and offline installation, see **[docs/INSTALL.md](docs/INSTALL.md)**.

---

## Configuration

Copy `.env.example` to `.env` and change what you need. Everything is optional.

| Variable | Default | Meaning |
|---|---|---|
| `STEMBOX_HOST` | `0.0.0.0` | listen on all network interfaces (needed for Wi-Fi access) |
| `STEMBOX_PORT` | `5000` | |
| `STEMBOX_STORAGE_DIR` | `./storage` | database, uploaded files and secret key: **back this folder up** |
| `STEMBOX_MAX_VIDEO_MB` | `500` | upload limits; PDF 50, image 10 and audio 100 MB by default |

The publishing mode, student self-registration, Wi-Fi sharing and the site name are set in the browser, under **Settings**.

## Command-line tools

| Command | Purpose |
|---|---|
| `flask init-db` | create or upgrade the database (this also runs automatically on start) |
| `flask create-admin` | create an administrator account |
| `flask seed-dev` / `flask unseed-dev` | load or remove the sample content |
| `flask export-package --all --out /media/usb/stembox.zip` | export content straight to a USB stick on the server |
| `flask import-package /media/usb/stembox.zip` | validate and import a package from a USB stick |

---

## Testing

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest             # 106 automated tests
.venv/bin/python tools/ui_check.py     # layout check at phone/tablet/desktop widths (needs Chrome or Chromium)
```

The tests cover all 11 acceptance criteria. Among them:
- a real server reached over the Wi-Fi address by several devices at once
- the whole student flow with outside network access blocked
- importing a package into a second installation
- restarting the server and checking that data persists

See **[docs/TESTING.md](docs/TESTING.md)** for the full report and the manual test scripts.

---

## Project layout

```
app/                    the application (one Flask blueprint per module)
  auth/                 login, logout, registration, role decorators
  main/                 first-run setup, role dashboards
  users/                users, CSV import, classes, settings, activity log
  cms/                  subjects, courses, topics, lessons, files, search, approvals
  learning/             student pages, file viewers, media streaming
  quizzes/              quiz builder, attempts, automatic marking
  progress/             class reports, CSV export
  sync/                 USB/Wi-Fi content packages
  permissions.py        who may see, edit and publish what
  publishing.py         draft → waiting for approval → published
  storage.py            safe file storage
  templates/ static/    local HTML, CSS and JS only
seed/                   placeholder development content ([SAMPLE])
tests/                  automated tests
tools/                  developer tools (layout checker)
docs/                   documentation
run.py                  starts the server
```

## Documentation

| Document | Contents |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | installation, Raspberry Pi setup, Wi-Fi hotspot, configuration, updating |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | system design, request flow, key workflows, how to extend STEMBox |
| [docs/DATABASE.md](docs/DATABASE.md) | every table and column, relationships, backups |
| [docs/DECISIONS.md](docs/DECISIONS.md) | defaults chosen and why |
| [docs/TESTING.md](docs/TESTING.md) | acceptance checklist with results, manual tests |

## Tech stack

Python 3.11+ · Flask · SQLite (WAL) · Waitress · Flask-WTF (CSRF) · HTML, CSS and vanilla JavaScript · pytest
