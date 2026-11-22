"""Jinja filters shared by the main app and the Zwift module (each has its own Environment)."""
from __future__ import annotations

from jinja2 import Environment


def interval_label(minutes: int) -> str:
    """30 -> "30 min", 60 -> "1 hour", 120 -> "2 hours"."""
    minutes = int(minutes)
    if minutes < 60 or minutes % 60:
        return f"{minutes} min"
    hours = minutes // 60
    return "1 hour" if hours == 1 else f"{hours} hours"


def render_autosync_line(env: Environment, enabled: bool, last, next_, manual_hint: str) -> str:
    """Render ui.autosync_line outside a page (for the HTMX reply of an auto-sync switch)."""
    template = env.from_string(
        '{% import "partials/ui.html" as ui %}{{ ui.autosync_line(enabled, last, next, hint) }}')
    return template.render(enabled=enabled, last=last, next=next_, hint=manual_hint)


def register(env: Environment) -> Environment:
    env.filters["interval_label"] = interval_label
    return env
