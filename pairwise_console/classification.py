import re
from typing import Any


PROJECT_CATEGORIES = ("纯后端", "纯前端", "全栈")


def normalize_stack(value: Any = "") -> str:
    """Return a compact, comma-separated list of technology names.

    The submission field is not a project description. Generated and legacy
    rows sometimes append architecture decisions or Chinese explanations;
    those are removed while language, framework, test and container labels
    are retained. Docker Compose is represented once as Docker.
    """
    raw = str(value or "").strip()
    parts = re.split(r"[,，、;；\n|]+|\s+\+\s+", raw)
    names = []
    seen = set()
    for part in parts:
        token = part.strip().strip(".。:：-—•· ")
        if not token or re.search(r"[\u3400-\u9fff]", token):
            continue
        # A slash commonly joins two library names in generated metadata.
        candidates = re.split(r"\s*/\s*", token) if re.fullmatch(
            r"[A-Za-z0-9_.+#() -]+\s*/\s*[A-Za-z0-9_.+#() -]+", token
        ) else [token]
        for candidate in candidates:
            candidate = re.sub(r"\s+", " ", candidate).strip()
            if not candidate or len(candidate) > 48 or ":" in candidate:
                continue
            if candidate.casefold() in ("docker compose", "docker-compose"):
                candidate = "Docker"
            key = candidate.casefold()
            if key not in seen:
                seen.add(key)
                names.append(candidate)
    return ", ".join(names)[:255]


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
