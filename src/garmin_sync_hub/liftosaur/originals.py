"""The watch activity as it was before a Liftosaur sync changed it, kept so Restore can put it back.

Saved once per Garmin activity, the first time its sets are rewritten: data/liftosaur_originals/<id>.json
holds the exercise sets, the name and the description Garmin had then.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..paths import data_dir

log = logging.getLogger("garmin_sync_hub")


def path(activity_id: int | str) -> Path:
    return data_dir() / "liftosaur_originals" / f"{activity_id}.json"


def save_once(client: Any, activity_id: int | str, exercise_sets: list[dict]) -> None:
    """Keep the activity before its first change. Best effort: never stops a sync."""
    target = path(activity_id)
    if target.exists():
        return
    name = description = None
    try:
        details = client.get_activity(activity_id) or {}
        name = details.get("activityName")
        description = details.get("description")
    except Exception as e:  # noqa: BLE001 - the sets are what matters
        log.warning("Could not read the name of %s before the sync: %s", activity_id, e)
    name = name if isinstance(name, str) else None
    description = description if isinstance(description, str) else None
    try:
        text = json.dumps({"exercise_sets": exercise_sets, "name": name, "description": description})
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".part")
        tmp.write_text(text)
        tmp.replace(target)
    except (OSError, TypeError, ValueError) as e:  # never stops a sync
        log.warning("Could not keep the original of %s: %s", activity_id, e)


def load(activity_id: int | str) -> dict | None:
    try:
        return json.loads(path(activity_id).read_text())
    except (OSError, ValueError):
        return None


def forget(activity_id: int | str) -> None:
    path(activity_id).unlink(missing_ok=True)
