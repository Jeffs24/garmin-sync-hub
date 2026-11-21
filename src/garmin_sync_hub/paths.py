"""Where garmin-sync-hub keeps its data (config, databases, tokens, backups)."""
from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    """``DATA_DIR`` (``/data`` in Docker), or ``~/.garmin-sync-hub`` when unset."""
    return Path(os.getenv("DATA_DIR") or "~/.garmin-sync-hub").expanduser()


def garmin_token_dir() -> Path:
    return data_dir() / "garminconnect"
