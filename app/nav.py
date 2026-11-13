"""Navigation menu for each role."""
from flask import g, request, url_for
from werkzeug.routing import BuildError

# (label, endpoint, endpoints-that-highlight-this-item prefix)
MENUS = {
    "admin": [
        ("Home", "main.dashboard", "main."),
        ("Users", "users.user_list", "users.user"),
        ("Classes", "users.class_list", "users.class"),
        ("Content", "cms.content_home", "cms."),
        ("Quizzes", "quizzes.quiz_list", "quizzes.quiz"),
        ("Approvals", "cms.approvals", "cms.approval"),
        ("Progress", "progress.overview", "progress."),
        ("Transfer", "sync.home", "sync."),
        ("Settings", "users.settings", "users.settings"),
    ],
    "teacher": [
        ("Home", "main.dashboard", "main."),
        ("My content", "cms.content_home", "cms."),
        ("Quizzes", "quizzes.quiz_list", "quizzes.quiz"),
        ("Progress", "progress.overview", "progress."),
        ("Preview as student", "learning.subjects", "learning."),
    ],
    "student": [
        ("Home", "main.dashboard", "main.", "home"),
        ("Subjects", "learning.subjects", "learning.", "grid"),
        ("My results", "quizzes.my_results", "quizzes.my_results", "trophy"),
    ],
}


def nav_links():
    user = g.get("user")
    if not user:
        return []
    endpoint = request.endpoint or ""
    links = []
    for label, target, prefix, *icon in MENUS[user["role"]]:
        try:
            url = url_for(target)
        except BuildError:  # module not installed: hide its menu item
            continue
        links.append({
            "label": label,
            "url": url,
            "icon": icon[0] if icon else None,  # icons are used in the student menu only
            "active": endpoint == target or (prefix.endswith(".") and endpoint.startswith(prefix))
                      or (not prefix.endswith(".") and endpoint.startswith(prefix)),
        })
    # Only the most specific match stays highlighted.
    actives = [link for link in links if link["active"]]
    if len(actives) > 1:
        for link in actives[:-1]:
            link["active"] = False
    return links
