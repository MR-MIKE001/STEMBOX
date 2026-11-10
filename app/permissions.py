"""Who may see, edit and publish what. Every route uses these checks on the server."""
from .db import get_setting, query


def class_ids(user_id, role_in_class=None):
    sql = "SELECT class_id FROM class_members WHERE user_id = ?"
    args = [user_id]
    if role_in_class:
        sql += " AND role_in_class = ?"
        args.append(role_in_class)
    return {r["class_id"] for r in query(sql, args)}


def can_manage_course(user, course):
    """Admins manage everything. Teachers manage courses they created and
    courses that belong to a class they teach."""
    if user is None or course is None:
        return False
    if user["role"] == "admin":
        return True
    if user["role"] != "teacher":
        return False
    if course["author_id"] == user["id"]:
        return True
    return course["class_id"] is not None and course["class_id"] in class_ids(user["id"], "teacher")


def manageable_courses_clause(user, alias="c"):
    """SQL WHERE fragment + args selecting courses the user may manage."""
    if user["role"] == "admin":
        return "1 = 1", []
    return (f"({alias}.author_id = ? OR {alias}.class_id IN "
            f"(SELECT class_id FROM class_members WHERE user_id = ? AND role_in_class = 'teacher'))",
            [user["id"], user["id"]])


def can_publish_directly(user, course):
    """Apply the administrator's publishing mode.

    direct   -> teachers publish straight away
    approval -> teachers always need an administrator's approval
    mixed    -> teachers publish directly to a class they teach; subject-wide
                content (course without a class) needs approval
    """
    if user["role"] == "admin":
        return True
    if user["role"] != "teacher" or not can_manage_course(user, course):
        return False
    mode = get_setting("publish_mode")
    if mode == "direct":
        return True
    if mode == "mixed":
        return course["class_id"] is not None and course["class_id"] in class_ids(user["id"], "teacher")
    return False


def can_view_course(user, course):
    """Can this user see the course in the learning area?"""
    if user is None or course is None:
        return False
    if user["role"] in ("admin", "teacher"):
        return True
    return course["class_id"] is None or course["class_id"] in class_ids(user["id"])


def student_courses_clause(user, alias="c"):
    """SQL fragment limiting courses to those a student may see."""
    return (f"({alias}.class_id IS NULL OR {alias}.class_id IN "
            f"(SELECT class_id FROM class_members WHERE user_id = ?))", [user["id"]])


def can_view_lesson(user, lesson, course):
    if user is None:
        return False
    if user["role"] == "admin":
        return True
    if user["role"] == "teacher":
        return lesson["status"] == "published" or can_manage_course(user, course)
    return lesson["status"] == "published" and can_view_course(user, course)


def can_view_quiz(user, quiz, course):
    return can_view_lesson(user, quiz, course)
