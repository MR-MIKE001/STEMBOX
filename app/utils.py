"""Small shared helpers: formatting, the lesson text renderer and safe redirects."""
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from markupsafe import Markup, escape

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\w*])_(.+?)_(?![\w*])")


def _inline(text):
    html = str(escape(text))
    html = _BOLD.sub(r"<strong>\1</strong>", html)
    html = _ITALIC.sub(r"<em>\1</em>", html)
    return html


def render_lesson_text(text):
    """Render the simple lesson markup to safe HTML.

    Supported (kept deliberately small so teachers can learn it in a minute):
      # Heading          -> large heading
      ## Smaller heading -> small heading
      - item / * item    -> bullet list
      1. item            -> numbered list
      **bold**, _italic_
      A blank line starts a new paragraph.
    All text is HTML-escaped first, so lesson text can never inject markup.
    """
    if not text:
        return Markup("")
    out = []
    paragraph = []
    list_type = None

    def flush_paragraph():
        if paragraph:
            out.append("<p>" + "<br>".join(_inline(line) for line in paragraph) + "</p>")
            paragraph.clear()

    def close_list():
        nonlocal list_type
        if list_type:
            out.append(f"</{list_type}>")
            list_type = None

    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            close_list()
            continue
        if stripped.startswith("## "):
            flush_paragraph(); close_list()
            out.append(f"<h3>{_inline(stripped[3:])}</h3>")
        elif stripped.startswith("# "):
            flush_paragraph(); close_list()
            out.append(f"<h2>{_inline(stripped[2:])}</h2>")
        elif stripped[:2] in ("- ", "* "):
            flush_paragraph()
            if list_type != "ul":
                close_list(); out.append("<ul>"); list_type = "ul"
            out.append(f"<li>{_inline(stripped[2:])}</li>")
        elif re.match(r"^\d+[.)] ", stripped):
            flush_paragraph()
            if list_type != "ol":
                close_list(); out.append("<ol>"); list_type = "ol"
            out.append(f"<li>{_inline(stripped.split(' ', 1)[1])}</li>")
        else:
            close_list()
            paragraph.append(stripped)
    flush_paragraph()
    close_list()
    return Markup("\n".join(out))


def fmt_dt(value, with_time=True):
    """Show an ISO timestamp in a short, readable local form."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone()
    return dt.strftime("%d %b %Y %H:%M" if with_time else "%d %b %Y")


def filesize(num):
    num = float(num or 0)
    for unit in ("bytes", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "bytes" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def percent(part, whole):
    if not whole:
        return 0
    return round(100 * part / whole)


def is_safe_next(target):
    """Only allow redirects to paths on this server."""
    if not target:
        return False
    parsed = urlparse(target)
    return not parsed.scheme and not parsed.netloc and target.startswith("/") and not target.startswith("//")


def to_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


ROLE_LABELS = {"admin": "Administrator", "teacher": "Teacher", "student": "Student"}
STATUS_LABELS = {"draft": "Draft", "pending": "Waiting for approval", "published": "Published"}
TYPE_LABELS = {"pdf": "PDF document", "video": "Video", "image": "Picture", "audio": "Audio"}


def register_template_helpers(app):
    app.jinja_env.filters["lesson_text"] = render_lesson_text
    app.jinja_env.filters["dt"] = fmt_dt
    app.jinja_env.filters["date"] = lambda v: fmt_dt(v, with_time=False)
    app.jinja_env.filters["filesize"] = filesize
    app.jinja_env.globals["percent"] = percent
    app.jinja_env.globals["ROLE_LABELS"] = ROLE_LABELS
    app.jinja_env.globals["STATUS_LABELS"] = STATUS_LABELS
    app.jinja_env.globals["TYPE_LABELS"] = TYPE_LABELS
