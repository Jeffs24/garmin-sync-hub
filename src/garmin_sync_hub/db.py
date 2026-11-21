"""Database layer for tracking synced Liftosaur workouts (SQLite in DATA_DIR).

Module-level functions are backwards-compatible wrappers around the
singleton Database instance.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from garmin_sync_hub.db_interface import Database

_instance: Database | None = None

def get_db() -> Database:
    """Get or create the singleton Database instance."""
    global _instance
    if _instance is None:
        from garmin_sync_hub.db_sqlite import SQLiteDatabase

        _instance = SQLiteDatabase()
    return _instance


def reset() -> None:
    """Clear the singleton so the next call to get_db() re-creates it."""
    global _instance
    _instance = None


# ── Backwards-compatible module-level wrappers ──────────────────────────────
# These accept **kw to silently swallow the old db_path= keyword argument.


def is_synced(workout_id: str, **kw) -> bool:
    """Check if a workout has already been synced."""
    return get_db().is_synced(workout_id)


def get_garmin_id(workout_id: str, **kw) -> str | None:
    """Get the Garmin activity ID for a synced workout."""
    return get_db().get_garmin_id(workout_id)


def mark_synced(
    workout_id: str,
    garmin_activity_id: str | None = None,
    title: str = "",
    calories: int | None = None,
    avg_hr: int | None = None,
    source_updated_at: str | None = None,
    action: str | None = None,
    **kw,
) -> None:
    """Record a successfully synced workout ("uploaded" or "updated" on Garmin)."""
    return get_db().mark_synced(workout_id, garmin_activity_id, title, calories, avg_hr, source_updated_at, action)


def record_failure(workout_id: str, title: str, error: str) -> None:
    """Remember a workout that failed to sync; cleared when it syncs."""
    return get_db().record_failure(workout_id, title, error)


def unsync(workout_id: str, **kw) -> bool:
    """Remove a sync record. Returns True if a record was deleted."""
    return get_db().unsync(workout_id)


def unsync_all(**kw) -> int:
    """Remove all sync records. Returns count of deleted records."""
    return get_db().unsync_all()


def get_synced_count(**kw) -> int:
    """Get total number of synced workouts."""
    return get_db().get_synced_count()


def get_recent_synced(limit: int = 10, **kw) -> list[dict]:
    """Get recently synced workouts."""
    return get_db().get_recent_synced(limit)


def record_sync_log(
    synced: int = 0,
    skipped: int = 0,
    failed: int = 0,
    trigger: str = "manual",
    **kw,
) -> None:
    """Persist a sync run result."""
    return get_db().record_sync_log(synced, skipped, failed, trigger)


def get_sync_log(limit: int = 20, **kw) -> list[dict]:
    """Get recent sync log entries."""
    return get_db().get_sync_log(limit)


def get_cached_hr(workout_id: str, **kw) -> dict | None:
    """Get cached HR data for a workout. Returns None if not cached."""
    return get_db().get_cached_hr(workout_id)


def cache_hr(workout_id: str, data: dict, **kw) -> None:
    """Cache HR data for a workout."""
    return get_db().cache_hr(workout_id, data)
