"""Safe storage of uploaded learning files.

Files are never stored under the name the browser sends. Each file gets a
generated name (`<2 hex>/<uuid><ext>`) under STORAGE_DIR/files, the type is
checked against both the extension and the file's first bytes, and the size is
limited per resource type.
"""
import hashlib
import os
import shutil
import uuid
from pathlib import Path

from flask import current_app

# type -> extension -> (mime type, magic-byte family)
ALLOWED = {
    "pdf": {".pdf": ("application/pdf", "pdf")},
    "image": {
        ".png": ("image/png", "png"),
        ".jpg": ("image/jpeg", "jpeg"),
        ".jpeg": ("image/jpeg", "jpeg"),
        ".gif": ("image/gif", "gif"),
        ".webp": ("image/webp", "webp"),
    },
    "video": {
        ".mp4": ("video/mp4", "isobmff"),
        ".m4v": ("video/mp4", "isobmff"),
        ".webm": ("video/webm", "ebml"),
        ".ogv": ("video/ogg", "ogg"),
    },
    "audio": {
        ".mp3": ("audio/mpeg", "mp3"),
        ".m4a": ("audio/mp4", "isobmff"),
        ".ogg": ("audio/ogg", "ogg"),
        ".oga": ("audio/ogg", "ogg"),
        ".wav": ("audio/wav", "wav"),
        ".flac": ("audio/flac", "flac"),
    },
}

RESOURCE_TYPES = tuple(ALLOWED)


class UploadError(ValueError):
    """Raised with a plain-language message when an upload is not accepted."""


def accept_attr(rtype):
    """Value for an <input type=file accept=...> attribute."""
    if rtype not in ALLOWED:
        return ",".join(ext for exts in ALLOWED.values() for ext in exts)
    return ",".join(ALLOWED[rtype])


def detect_family(head):
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[4:8] == b"ftyp":
        return "isobmff"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "ebml"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"fLaC"):
        return "flac"
    if head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "mp3"
    return None


def guess_type_from_name(filename):
    ext = Path(filename or "").suffix.lower()
    for rtype, exts in ALLOWED.items():
        if ext in exts:
            return rtype
    return None


def files_dir():
    return Path(current_app.config["STORAGE_DIR"]) / "files"


def tmp_dir():
    return Path(current_app.config["STORAGE_DIR"]) / "tmp"


def resolve(rel_path):
    """Absolute path for a stored file; refuses anything outside the files folder."""
    base = files_dir().resolve()
    target = (base / rel_path).resolve()
    if base not in target.parents:
        raise UploadError("Invalid file path.")
    return target


def validate_name_and_type(filename, rtype):
    """Check the extension. Returns (ext, mime, family)."""
    if rtype not in ALLOWED:
        raise UploadError("Unknown kind of file.")
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED[rtype]:
        allowed = ", ".join(sorted(ALLOWED[rtype]))
        raise UploadError(f"This file type is not allowed here. Allowed types: {allowed}.")
    mime, family = ALLOWED[rtype][ext]
    return ext, mime, family


def store_stream(stream, filename, rtype):
    """Validate and store a binary stream. Returns a dict describing the stored file."""
    ext, mime, family = validate_name_and_type(filename, rtype)
    limit = current_app.config["UPLOAD_LIMITS"][rtype]
    head = stream.read(16)
    if not head:
        raise UploadError("The file is empty.")
    if detect_family(head) != family:
        raise UploadError("The file's contents do not match its type. It may be damaged or renamed.")

    file_uuid = uuid.uuid4().hex
    rel_path = f"{file_uuid[:2]}/{file_uuid}{ext}"
    tmp_dir().mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir() / f"{file_uuid}.part"
    digest = hashlib.sha256()
    size = 0
    try:
        with open(tmp_path, "wb") as out:
            chunk = head
            while chunk:
                size += len(chunk)
                if size > limit:
                    raise UploadError(f"The file is too large. The limit for this type is {limit // (1024 * 1024)} MB.")
                digest.update(chunk)
                out.write(chunk)
                chunk = stream.read(1024 * 1024)
        final = resolve(rel_path)
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_path), str(final))
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return {
        "file_path": rel_path,
        "mime_type": mime,
        "size": size,
        "checksum": digest.hexdigest(),
        "original_name": Path(filename).name[:200],
    }


def store_upload(file_storage, rtype):
    """Store a Werkzeug FileStorage from a form upload."""
    if file_storage is None or not file_storage.filename:
        raise UploadError("Please choose a file to upload.")
    return store_stream(file_storage.stream, file_storage.filename, rtype)


def delete_file(rel_path):
    try:
        path = resolve(rel_path)
    except UploadError:
        return
    if path.exists():
        path.unlink()
    try:  # tidy up the empty shard folder
        path.parent.rmdir()
    except OSError:
        pass


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def disk_usage():
    """(used_by_files_bytes, free_bytes) for the admin dashboard."""
    used = 0
    for root, _dirs, files in os.walk(files_dir()):
        for name in files:
            try:
                used += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    try:
        free = shutil.disk_usage(files_dir()).free
    except OSError:
        free = 0
    return used, free
