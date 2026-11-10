"""Publishing workflow shared by lessons and quizzes.

States: draft -> (pending) -> published. Whether a teacher's "Publish" goes
straight to published or into the administrator's approval queue is decided by
permissions.can_publish_directly(), which applies the configured publish mode.
"""
from .db import audit, execute, now, query
from .permissions import can_publish_directly

TABLES = {"lesson": "lessons", "quiz": "quizzes"}


def _set_status(target_type, target_id, status):
    table = TABLES[target_type]
    if status == "published":
        execute(f"UPDATE {table} SET status = 'published', published_at = ?, updated_at = ? WHERE id = ?",
                (now(), now(), target_id))
    else:
        execute(f"UPDATE {table} SET status = ?, updated_at = ? WHERE id = ?", (status, now(), target_id))


def _close_requests(target_type, target_id, status, reviewer_id=None, note=""):
    execute("UPDATE publish_requests SET status = ?, reviewed_by = ?, reviewed_at = ?, "
            "note = CASE WHEN ? != '' THEN ? ELSE note END "
            "WHERE target_type = ? AND target_id = ? AND status = 'pending'",
            (status, reviewer_id, now(), note, note, target_type, target_id))


def pending_request(target_type, target_id):
    return query("SELECT * FROM publish_requests WHERE target_type = ? AND target_id = ? AND status = 'pending'",
                 (target_type, target_id), one=True)


def latest_review(target_type, target_id):
    return query("SELECT r.*, u.name AS reviewer_name FROM publish_requests r LEFT JOIN users u ON u.id = r.reviewed_by "
                 "WHERE r.target_type = ? AND r.target_id = ? ORDER BY r.id DESC LIMIT 1",
                 (target_type, target_id), one=True)


def _open_request(target_type, target_id, user, note=""):
    if pending_request(target_type, target_id) is None:
        execute("INSERT INTO publish_requests (target_type, target_id, requested_by, status, note, created_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?)", (target_type, target_id, user["id"], note, now()))


def publish(target_type, target, course, user):
    """Publish now, or ask for approval. Returns 'published' or 'pending'."""
    if can_publish_directly(user, course):
        _set_status(target_type, target["id"], "published")
        _close_requests(target_type, target["id"], "approved", user["id"])
        if user["role"] == "admin":
            audit(f"{target_type}.publish", f"{target_type}:{target['id']}", target["title"])
        return "published"
    _set_status(target_type, target["id"], "pending")
    _open_request(target_type, target["id"], user)
    return "pending"


def unpublish(target_type, target, user):
    """Back to draft (also withdraws a waiting approval request)."""
    _set_status(target_type, target["id"], "draft")
    _close_requests(target_type, target["id"], "cancelled", user["id"])


def content_changed(target_type, target, course, user):
    """Call after a published item is edited. If the editor could not have
    published it directly, the item goes back to the approval queue so that
    unapproved changes never reach students. Returns True if that happened."""
    if target["status"] == "published" and not can_publish_directly(user, course):
        _set_status(target_type, target["id"], "pending")
        _open_request(target_type, target["id"], user, note="Changed after it was published")
        return True
    return False


def approve(request_row, admin):
    _set_status(request_row["target_type"], request_row["target_id"], "published")
    _close_requests(request_row["target_type"], request_row["target_id"], "approved", admin["id"])
    audit("approval.approve", f"{request_row['target_type']}:{request_row['target_id']}")


def reject(request_row, admin, note):
    _set_status(request_row["target_type"], request_row["target_id"], "draft")
    _close_requests(request_row["target_type"], request_row["target_id"], "rejected", admin["id"], note or "")
    audit("approval.reject", f"{request_row['target_type']}:{request_row['target_id']}", note or "")


def delete_requests(target_type, target_id):
    execute("DELETE FROM publish_requests WHERE target_type = ? AND target_id = ?", (target_type, target_id))
