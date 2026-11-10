"""Layout check at phone, tablet and desktop widths using headless Chrome/Edge.

    python tools/ui_check.py [--out screenshots_dir] [--browser path/to/chrome.exe]

Starts a throw-away STEMBox with the sample content, logs in as each role,
opens the main pages at 390px (phone), 820px (tablet) and 1280px (desktop)
wide, and reports:
  * pages that scroll sideways (content wider than the screen),
  * buttons/links/inputs smaller than 40px high (hard to tap),
and saves a screenshot of every page/width.

Development tool only. Needs: pip install websocket-client
"""
import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WIDTHS = {"phone": (390, 844, True), "tablet": (820, 1180, True), "desktop": (1280, 900, False)}
BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/chromium-browser", "/usr/bin/chromium", "/usr/bin/google-chrome",
]

CHECK_JS = r"""
(() => {
  const nav = document.querySelector('.site-nav');
  if (nav) nav.classList.add('open');                    // check the phone menu too
  const doc = document.documentElement;
  const overflow = doc.scrollWidth - window.innerWidth;
  const small = [];
  document.querySelectorAll('a.btn, button, input:not([type=hidden]):not([type=checkbox]):not([type=radio]), select, .site-nav a').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;           // not visible
    if (el.closest('.sr-only, details:not([open])')) return;
    if (r.height < 40) small.push((el.innerText || el.name || el.id || el.tagName).trim().slice(0, 30) + ' (' + Math.round(r.height) + 'px)');
  });
  const wide = [];
  if (overflow > 1) {
    document.querySelectorAll('body *').forEach(el => {
      const r = el.getBoundingClientRect();
      if (r.right > window.innerWidth + 1 && !el.closest('.table-wrap')) wide.push(el.tagName + '.' + el.className);
    });
  }
  if (nav) nav.classList.remove('open');
  return JSON.stringify({overflow, small: small.slice(0, 8), wide: wide.slice(0, 5)});
})()
"""


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        self.next_id = 0

    def call(self, method, **params):
        self.next_id += 1
        self.ws.send(json.dumps({"id": self.next_id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.next_id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def wait_event(self, name, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("method") == name:
                return msg
        raise TimeoutError(name)

    def goto(self, url):
        self.call("Page.navigate", url=url)
        self.wait_event("Page.loadEventFired")
        time.sleep(0.15)

    def js(self, expression):
        return self.call("Runtime.evaluate", expression=expression, returnByValue=True)["result"].get("value")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(f"{url} did not answer")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "tools" / "screenshots"))
    parser.add_argument("--browser", default=next((b for b in BROWSERS if os.path.exists(b)), None))
    args = parser.parse_args()
    if not args.browser:
        sys.exit("No Chrome/Edge/Chromium found; pass --browser")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    work = Path(tempfile.mkdtemp(prefix="stembox-ui-"))
    storage = work / "storage"
    from app import create_app
    from app.db import connect
    app = create_app({"STORAGE_DIR": str(storage), "PACKAGES_DIR": str(work / "packages")})
    app.test_cli_runner().invoke(args=["seed-dev"])
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE users SET must_change_password = 0")
    conn.commit()
    ids = {t: conn.execute(f"SELECT MIN(id) FROM {t}").fetchone()[0]
           for t in ("courses", "lessons", "quizzes", "resources", "subjects", "classes")}
    pdf_id = conn.execute("SELECT id FROM resources WHERE type = 'pdf' LIMIT 1").fetchone()[0]
    conn.close()

    port = free_port()
    env = {**os.environ, "STEMBOX_STORAGE_DIR": str(storage), "STEMBOX_PACKAGES_DIR": str(work / "packages"),
           "STEMBOX_PORT": str(port), "STEMBOX_HOST": "127.0.0.1"}
    server = subprocess.Popen([sys.executable, "run.py"], cwd=ROOT, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    debug_port = free_port()
    browser = subprocess.Popen([args.browser, "--headless=new", f"--remote-debugging-port={debug_port}",
                                f"--user-data-dir={work / 'profile'}", "--no-first-run", "--disable-gpu",
                                "--remote-allow-origins=*", "about:blank"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pages = {
        "public": ["/login"],
        "admin": ["/dashboard", "/admin/users", "/admin/users/new", "/admin/users/import", "/admin/classes",
                  f"/admin/classes/{ids['classes']}", "/admin/settings", "/content/", "/content/approvals",
                  f"/content/courses/{ids['courses']}", "/transfer/", f"/progress/class/{ids['classes']}"],
        "teacher": ["/dashboard", "/content/", f"/content/courses/{ids['courses']}",
                    f"/content/lessons/{ids['lessons']}", f"/content/courses/{ids['courses']}/lessons/new",
                    f"/quizzes/{ids['quizzes']}", "/quizzes", "/progress/", f"/progress/class/{ids['classes']}",
                    f"/progress/quiz/{ids['quizzes']}"],
        "student": ["/dashboard", "/learn", f"/learn/subject/{ids['subjects']}", f"/learn/course/{ids['courses']}",
                    f"/learn/lesson/{ids['lessons']}", f"/learn/resource/{pdf_id}", f"/quiz/{ids['quizzes']}",
                    "/my-results"],
    }
    accounts = {"admin": ("admin", "admin123"), "teacher": ("teacher1", "teacher123"),
                "student": ("student1", "student123")}
    problems = []
    shots = 0
    try:
        wait_http(base + "/api/ping")
        wait_http(f"http://127.0.0.1:{debug_port}/json/version")
        targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json").read())
        page_target = next(t for t in targets if t["type"] == "page")
        cdp = CDP(page_target["webSocketDebuggerUrl"])
        cdp.call("Page.enable")
        for role, urls in pages.items():
            cdp.call("Network.enable")
            cdp.call("Network.clearBrowserCookies")
            if role in accounts:
                user, password = accounts[role]
                cdp.goto(base + "/login")
                cdp.js(f"document.getElementById('username').value={json.dumps(user)};"
                       f"document.getElementById('password').value={json.dumps(password)};"
                       "document.querySelector('form.form').submit()")
                cdp.wait_event("Page.loadEventFired")
                if "/login" in cdp.js("location.pathname"):
                    problems.append(f"{role}: could not log in")
                    continue
            # Sample quiz attempt so the results pages have data.
            if role == "student":
                cdp.goto(base + f"/quiz/{ids['quizzes']}")
                cdp.js("document.querySelector('form[action$=\"/start\"]').submit()")
                cdp.wait_event("Page.loadEventFired")
                cdp.js("document.querySelectorAll('fieldset.question').forEach(f => f.querySelector('input').checked = true);"
                       "HTMLFormElement.prototype.submit.call(document.querySelector('form[action$=\"/submit\"]'))")
                cdp.wait_event("Page.loadEventFired")
                urls = urls + [cdp.js("location.pathname")]
            for url in urls:
                for label, (width, height, mobile) in WIDTHS.items():
                    cdp.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
                             deviceScaleFactor=1, mobile=mobile)
                    cdp.goto(base + url)
                    result = json.loads(cdp.js(CHECK_JS))
                    if result["overflow"] > 1:
                        problems.append(f"{role} {url} @{label}: scrolls sideways by {result['overflow']}px "
                                        f"({', '.join(result['wide'])})")
                    if result["small"]:
                        problems.append(f"{role} {url} @{label}: small tap targets: {', '.join(result['small'])}")
                    full_h = cdp.js("Math.min(document.documentElement.scrollHeight, 4000)")
                    cdp.call("Emulation.setDeviceMetricsOverride", width=width, height=int(full_h),
                             deviceScaleFactor=1, mobile=mobile)
                    shot = cdp.call("Page.captureScreenshot", format="png")
                    name = f"{role}{url.replace('/', '_').rstrip('_') or '_root'}-{label}.png"
                    (out / name).write_bytes(base64.b64decode(shot["data"]))
                    shots += 1
    finally:
        browser.terminate()
        server.terminate()
        time.sleep(0.5)
        shutil.rmtree(work, ignore_errors=True)
    print(f"Checked {shots} page/width combinations. Screenshots in {out}")
    if problems:
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print("  - " + p)
        sys.exit(1)
    print("No layout problems found.")


if __name__ == "__main__":
    main()
