"""Jinja filters shared by the main app and the Zwift module (each has its own Environment)."""
from __future__ import annotations

import json

from jinja2 import Environment


def interval_label(minutes: int) -> str:
    """30 -> "30 min", 60 -> "1 hour", 120 -> "2 hours"."""
    minutes = int(minutes)
    if minutes == 10080:
        return "1 week"
    if minutes > 1440 and minutes % 1440 == 0:
        return f"{minutes // 1440} days"
    if minutes < 60 or minutes % 60:
        return f"{minutes} min"
    hours = minutes // 60
    return "1 hour" if hours == 1 else f"{hours} hours"


def render_autosync_line(env: Environment, enabled: bool, last, next_, manual_hint: str) -> str:
    """Render ui.autosync_line outside a page (for the HTMX reply of an auto-sync switch)."""
    template = env.from_string(
        '{% import "partials/ui.html" as ui %}{{ ui.autosync_line(enabled, last, next, hint) }}')
    return template.render(enabled=enabled, last=last, next=next_, hint=manual_hint)


def filesize(size: int | float | None) -> str:
    """1536 -> "1.5 KB"."""
    size = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def notice_headers(message: str, kind: str = "success") -> dict[str, str]:
    """HTMX response headers that show a floating notice (notify() in base.html) and swap nothing."""
    return {"HX-Trigger": json.dumps({"notify": {"message": message, "kind": kind}}), "HX-Reswap": "none"}


def fromiso(value):
    """ISO 8601 text (as saved in JSON state files) -> aware datetime, for local_time()."""
    from datetime import datetime, timezone

    if not value or not isinstance(value, str):
        return value
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def register(env: Environment) -> Environment:
    env.filters["interval_label"] = interval_label
    env.filters["filesize"] = filesize
    env.filters["fromiso"] = fromiso
    return env
