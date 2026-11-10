# STEMBox testing report

**Run date:** 11 Nov 2026 · **Platform:** Windows 11, Python 3.14.3, Flask 3.1, Waitress 3 · **Browser for layout tests:** Chrome (headless)

| Suite | Command | Result |
|---|---|---|
| Automated tests | `python -m pytest` | **106 passed, 0 failed** |
| Layout check (3 roles + login, 32 pages × 3 widths) | `python tools/ui_check.py` | **96 page/width combinations, no problems** |

Every automated test drives the real application, either through Flask's test client or, in `test_server.py`, a real Waitress server reached over HTTP. Content, quizzes and transfers are created through the same routes the browser uses.

## Acceptance checklist (brief §9)

| # | Requirement | Result | Evidence |
|---|---|---|---|
| 1 | Users log in according to their roles, and are refused access outside them | **PASS** | `test_auth_users.py`: login/logout, wrong password, inactive account, lockout, forced password change, open-redirect block. `test_non_admin_refused_on_every_admin_route` tries **every** `/admin` URL and method as a student and as a teacher and expects 403. `test_student_refused_on_every_cms_route` does the same for every content, quiz-builder and progress route. Anonymous users are redirected to login. Switching an account off, or resetting its password, ends its sessions. |
| 2 | Teachers create and publish lessons, subject to the publishing mode | **PASS** | `test_cms.py`: `test_mode_approval_requires_admin`, `test_mode_direct`, `test_mode_mixed` (a class course publishes directly; a whole-subject course waits, and moving a course to a wider audience re-queues it), `test_reject_sends_back_with_note`, `test_editing_published_lesson_needs_reapproval`, `test_teacher_cannot_choose_class_they_do_not_teach`. `test_quizzes.py::test_quiz_needs_approval_in_approval_mode`. |
| 3 | Teachers upload PDFs and videos | **PASS** | `test_upload_pdf_video_image_audio` (bytes stored match, size and checksum recorded), `test_upload_rejects_bad_type_and_fake_content` (.exe, PNG renamed to .pdf, wrong declared type), `test_upload_size_limit` (and no partial file is left behind), `test_client_filename_never_used_for_storage` (`../../../evil.pdf`), `test_replace_and_delete_resource`, `test_delete_course_removes_files`. |
| 4 | Students access and view learning materials | **PASS** | `test_learning.py`: browsing subject → course → lesson; PDF viewer and video pages; `test_media_full_and_range_requests` (200 for the full file, **206 with a correct Content-Range** for `Range: bytes=100-199`); drafts and other classes' courses are hidden; media needs a login and permission; completion; saved video position; search. |
| 5 | Students complete quizzes and results are saved | **PASS** | `test_quizzes.py`: automatic marking (1/2 stored, per-question answers stored), unanswered questions count as wrong, **every attempt stored** and listed in "My results", no re-submission, no access to other students' attempts, answers from another question ignored, results stay readable after the quiz is edited. |
| 6 | Teachers monitor student progress | **PASS** | `test_progress.py`: exact numbers for completion rate, best score, attempts, pass rate and last activity; class, student and quiz pages; CSV export content; teachers who don't teach the class get 403; dashboards for all roles. |
| 7 | Works without an internet connection | **PASS** | `test_offline.py::test_full_flow_with_network_disconnected` blocks every non-local socket and DNS lookup, then runs the admin dashboard, lesson, video (206 range), completion and quiz end to end. `test_no_external_urls_in_templates_or_static` and `test_rendered_pages_load_nothing_external` check every page for each role: no external `src`, `href`, `url()` or `@import`, and a CSP of `default-src 'self'` on every page. See also manual test M1. |
| 8 | Multiple devices access it over local Wi-Fi | **PASS** | `test_server.py` starts the real server (`run.py`, Waitress, `0.0.0.0`) and connects through the machine's **Wi-Fi address (192.168.0.194 in this run)**. Three student "devices", each with its own cookies and real CSRF tokens, log in, open a lesson, stream a PDF range and mark it complete **at the same time** as an admin uses the dashboard. The admin dashboard shows the address to type. See also manual test M2. |
| 9 | Content exported and imported through USB | **PASS** | `test_sync.py`: package layout (manifest, checksums, content and files, all verified); import into a **second, separate installation**, where a student then plays the imported video; re-import → everything skipped; changed lesson → only that item updated; same-name subject merged. **10 kinds of bad package rejected with nothing written:** bad checksum, unknown version, `../` path traversal, absolute path, missing checksum line, manifest mismatch, fake media, not a zip, not a STEMBox package, broken reference. Also tested: Wi-Fi pull between two installations (wrong password, sharing off, internet address refused, token tied to the admin's session) and CLI export/import for a stick plugged into the server. |
| 10 | Data remains saved after the server restarts | **PASS** | `test_server.py` stops the real server process and starts it again: completed lessons and quiz results are still there, and the admin's **existing login still works** (the secret key is kept on disk). The database is SQLite in WAL mode on disk. |
| 11 | Works properly on phones and computers | **PASS** | `tools/ui_check.py` opens 32 pages as admin, teacher, student and logged out at **390 px (phone), 820 px (tablet) and 1280 px (desktop)**: no sideways scrolling and no tap target under 40 px, with the phone menu opened and checked too. Every page has the viewport meta tag (`test_offline.py`). Screenshots were reviewed by eye; see manual test M3. |

## Tests by phase

| Phase | File | Tests | What is covered |
|---|---|---|---|
| 0 Scaffold | `test_smoke.py` | 5 | App starts, first-run redirect, WAL mode and default settings, static files, `init-db`, `seed-dev` / `unseed-dev` (idempotent, forced password reset, complete removal) |
| 1 Auth & users | `test_auth_users.py` | 23 | See checklist item 1, plus user create/edit/deactivate, last-admin protection, password reset, CSV import (valid and invalid rows, classes, semicolons), classes and membership, settings, **CSRF enforced** |
| 2 CMS | `test_cms.py` | 20 | Structure, safe text rendering (no script injection), uploads, publishing modes, permissions, search and filters by subject, class, level, topic and status |
| 3 Learning | `test_learning.py` | 10 | See checklist item 4 |
| 4 Quizzes | `test_quizzes.py` | 10 | Builder validation, marking, attempts, tampering, drafts, teacher preview |
| 5 Monitoring | `test_progress.py` | 7 | See checklist item 6 |
| 6 Offline & LAN | `test_offline.py`, `test_server.py` | 7 + 1 | See checklist items 7, 8 and 10 |
| 7 Transfer | `test_sync.py` | 23 | See checklist item 9 |
| 8 UI | `tools/ui_check.py` | 96 combos | See checklist item 11 |

## Security checks (brief §8)

| Requirement | Evidence |
|---|---|
| Role checks on every route | The route-enumeration tests above. The object-level checks are in `permissions.py` (teacher A can't edit teacher B's course: `test_teacher_cannot_edit_other_teachers_course`). |
| CSRF on all POST forms | `test_csrf_required_on_post`, plus `test_server.py` (the real server refuses a form without a token with 400). |
| Upload type, size and filename validation; generated names | `test_upload_*` and `test_client_filename_never_used_for_storage` |
| Path traversal, upload and import | the filename test; package tests `traversal` and `absolute`; `test_download_package_blocks_traversal` |
| Max upload size in config | `UPLOAD_LIMITS`, `MAX_CONTENT_LENGTH` and `MAX_PACKAGE_BYTES`; `test_upload_size_limit` |
| Admin actions logged | Audit-log assertions in the user, approval, export and import tests |
| No plain-text passwords; seed accounts forced to reset | `test_passwords_are_hashed`, `test_seed_and_unseed_commands` |

## Manual tests

These need real hardware, so they are documented to be repeated on the competition device.

**M1. No internet**
1. Unplug the internet from the router, or use the Pi's own hotspot with no uplink.
2. Start STEMBox and open it from a phone.
3. Log in as a student: open a lesson, play a video and seek to the middle, open the PDF, take a quiz.
4. In a desktop browser, open DevTools → Network and reload a few pages. Every request should go to the STEMBox address.

*Expected:* everything works, and there are no failed or external requests.

**M2. Several devices on Wi-Fi**
1. On the admin dashboard, note the address under **Connect other devices**.
2. Join 3 or more devices (phone, tablet, laptop) to the same Wi-Fi and open that address.
3. Log in as different students on each device. All watch the same video at once.
4. On a teacher's device, open **Progress** while the students mark lessons as finished.

*Expected:* all devices load the pages and videos, and the teacher sees the progress after reloading.

**M3. Phones and computers**
1. Use STEMBox on an Android phone (Chrome), an iPhone (Safari), a tablet and a desktop.
2. Use the **Menu** button, the forms, the upload button, the video player and the quiz radio buttons.

*Expected:* no sideways scrolling, and buttons are easy to tap. PDFs open inline on desktop. On phones they open in the phone's PDF viewer, which is the expected behaviour (see DECISIONS.md).

**M4. USB stick**
1. On box A, go to **Copy content**, tick a course and choose **Make the package file**. Save the .zip to a USB stick.
2. On box B, go to **Load content from a USB stick** and choose the file.

*Expected:* a report showing what was added. Loading the same file again shows everything as "skipped".

**M5. Restart**
1. Make a change (for example, finish a lesson).
2. Unplug the Pi's power, plug it back in, and wait for STEMBox to start (systemd).

*Expected:* the change is still there.

**M6. First run**
1. Delete `storage/`, start STEMBox and open it.

*Expected:* the "Welcome to STEMBox" page creates the administrator.

## Known limitations

- PDFs aren't displayed *inside* the page on most phone browsers. They open in the device's viewer instead (see DECISIONS.md).
- The login lockout counter is kept in memory, so it resets when the server restarts.
- There is no sample video in the seed content, because it can't be generated without extra tools. Upload any MP4 to try the video player.
- `tools/ui_check.py` checks layout mechanically (overflow and tap-target size). Visual quality was judged from the screenshots by eye.
