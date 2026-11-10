# Installing STEMBox

STEMBox needs **Python 3.11 or newer**. Nothing is downloaded while it runs. You need internet only once, to install the Python packages (or copy them in from another machine; see "Offline installation" below).

## 1. Raspberry Pi (Raspberry Pi OS / Debian / Ubuntu)

```bash
sudo apt update && sudo apt install -y python3 python3-venv
cd /home/pi
# copy the STEMBox folder here (git clone, USB stick, ...)
cd stembox
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py
```

The console prints the addresses to use, for example:

```
 On this computer:   http://127.0.0.1:5000
 On the local Wi-Fi: http://192.168.1.20:5000
```

Open that address in a browser. The first visit shows **Welcome to STEMBox**, where you create the administrator account.

### Start automatically at boot (systemd)

Create `/etc/systemd/system/stembox.service`:

```ini
[Unit]
Description=STEMBox
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi/stembox
ExecStart=/home/pi/stembox/.venv/bin/python run.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now stembox
sudo systemctl status stembox
```

### Wi-Fi for the classroom

Devices only need to be on the **same network** as the STEMBox. Either:
- connect the Pi to the school's Wi-Fi router (no internet needed on the router), or
- make the Pi its own hotspot. On Raspberry Pi OS Bookworm: `sudo nmcli device wifi hotspot ssid STEMBox password <8+ characters>`, then `sudo nmcli connection modify Hotspot connection.autoconnect yes`. Devices join the "STEMBox" network and open `http://10.42.0.1:5000`.

If a firewall is active, allow the port: `sudo ufw allow 5000/tcp`.

## 2. Windows or macOS (for testing or a teacher's laptop)

```bash
python -m venv .venv
# Windows:  .venv\Scripts\pip install -r requirements.txt   then   .venv\Scripts\python run.py
# macOS:    .venv/bin/pip install -r requirements.txt       then   .venv/bin/python run.py
```

On Windows, allow Python through the firewall when asked, so other devices can connect.

## 3. Configuration

Copy `.env.example` to `.env` and change what you need. Every setting is optional.

| Variable | Default | Meaning |
|---|---|---|
| `STEMBOX_HOST` | `0.0.0.0` | `0.0.0.0` = reachable from the Wi-Fi; `127.0.0.1` = this computer only |
| `STEMBOX_PORT` | `5000` | |
| `STEMBOX_STORAGE_DIR` | `./storage` | database, uploaded files and secret key: **back this up** |
| `STEMBOX_PACKAGES_DIR` | `./packages` | exported content packages |
| `STEMBOX_MAX_*_MB` | 50 / 10 / 100 / 500 / 2048 | upload limits for PDF, image, audio, video and packages |
| `STEMBOX_SECRET_KEY` | auto-generated | only set it if you run several servers that must share logins |

Publishing mode, self-registration, Wi-Fi sharing and the site name are changed in the browser, under **Settings** (administrator).

## 4. Command-line tools

Run these from the STEMBox folder, using the virtual environment's `flask` (`.venv/bin/flask` or `.venv\Scripts\flask`):

| Command | What it does |
|---|---|
| `flask init-db` | Creates or upgrades the database. This also happens automatically on start. `--reset` wipes everything. |
| `flask create-admin` | Creates an administrator. Useful if every admin password is lost. |
| `flask seed-dev` | Loads the **[SAMPLE]** placeholder content and accounts (`admin/admin123`, `teacher1/teacher123`, `student1/student123`). Each must choose a new password at first login. **For testing only.** |
| `flask unseed-dev` | Removes all sample content, accounts and files. Real content is not touched. |
| `flask export-package --all --out /media/usb/stembox.zip` | Writes a content package straight to a USB stick plugged into the server. Use `--course <uuid>` or `--subject <uuid>` to choose content, and `--drafts` to include unpublished items. |
| `flask import-package /media/usb/stembox.zip` | Validates and imports a package from a USB stick on the server. |

## 5. Offline installation (no internet at the school)

On any computer with internet and the same Python version:

```bash
pip download -r requirements.txt -d wheels/
```

Copy the STEMBox folder and `wheels/` to the Pi, then:

```bash
python3 -m venv .venv
.venv/bin/pip install --no-index --find-links wheels/ -r requirements.txt
```

## 6. Updating

Stop the server, back up `storage/`, replace the code files (but **not** `storage/`), install the requirements again, and start the server. Database migrations run automatically.

## 7. Running the tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest              # 106 automated tests
.venv/bin/python tools/ui_check.py      # layout check at phone, tablet and desktop widths (needs Chrome/Chromium)
```
