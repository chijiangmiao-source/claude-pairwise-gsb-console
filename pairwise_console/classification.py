from typing import Any


PROJECT_CATEGORIES = ("纯后端", "纯前端", "全栈")


def normalize_project_category(value: Any = "", *context: Any) -> str:
    """Return one of the three project categories used by the console.

    Explicit metadata wins.  The fallback is deliberately conservative and is
    only used for historical rows created before project_category was stored.
    """
    candidate = str(value or "").strip()
    if candidate in PROJECT_CATEGORIES:
        return candidate

    text = " ".join(str(item or "") for item in context).casefold()
    for label in PROJECT_CATEGORIES:
        if label in text:
            return label

    frontend_markers = ("react", "vue", "vite", "svelte", "angular", "纯前端", "浏览器内")
    backend_markers = (
        "fastapi", "django", "flask", "spring boot", "gin", "gorm", "fiber", "sqlalchemy",
        "alembic", "postgresql", "mysql", "redis", "纯后端", "后端服务", "rest api",
    )
    has_frontend = any(marker in text for marker in frontend_markers)
    has_backend = any(marker in text for marker in backend_markers)
    if has_frontend and has_backend:
        return "全栈"
    if has_frontend:
        return "纯前端"
    return "纯后端"
