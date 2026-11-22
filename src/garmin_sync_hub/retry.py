"""When to retry a failed sync or merge on its own (Liftosaur auto-sync and Zwift auto-merge).

A failure is retried after 30 minutes, then 2 hours, then 8 hours. After MAX_ATTEMPTS
failures it waits for a click: the error is probably not temporary.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

DELAYS = (timedelta(minutes=30), timedelta(hours=2), timedelta(hours=8))
MAX_ATTEMPTS = len(DELAYS) + 1


def next_retry(attempts: int, now: datetime | None = None) -> datetime | None:
    """When to try again after `attempts` failures in a row, or None to stop retrying."""
    if attempts < 1 or attempts >= MAX_ATTEMPTS:
        return None
    return (now or datetime.now(timezone.utc)) + DELAYS[attempts - 1]


def to_text(value: datetime | None) -> str | None:
    """Stored form (UTC, ISO 8601)."""
    return value.astimezone(timezone.utc).isoformat(timespec="seconds") if value else None


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" ", "T", 1))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
