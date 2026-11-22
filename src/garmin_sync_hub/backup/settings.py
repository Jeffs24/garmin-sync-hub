"""Backup settings, saved from the web UI in data/backup_settings.json."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from ..paths import data_dir

log = logging.getLogger(__name__)

BACKUP_INTERVALS = (360, 720, 1440, 4320, 10080)  # 6 h, 12 h, 1 day, 3 days, 1 week


def backup_dir() -> Path:
    """Where the backup is written: ``BACKUP_DIR`` (``/backup`` in Docker), or data/garmin_backup."""
    return Path(os.getenv("BACKUP_DIR") or data_dir() / "garmin_backup").expanduser()


@dataclass
class BackupSettings:
    auto: bool = False                 # run on a schedule
    interval_minutes: int = 1440       # once a day
    health: bool = True                # daily health data (sleep, HRV, stress…)
    health_since: str = ""             # YYYY-MM-DD; empty = since the first activity
    rclone_remote: str = ""            # e.g. "gdrive:Garmin"; empty = no copy

    @staticmethod
    def path() -> Path:
        return data_dir() / "backup_settings.json"

    @classmethod
    def load(cls) -> "BackupSettings":
        s = cls()
        try:
            s.update(json.loads(cls.path().read_text()))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("Ignoring backup settings: %s", e)
        return s

    def update(self, values: dict) -> None:
        """Validate and apply. Raises ValueError, changing nothing."""
        clean: dict = {}
        if "auto" in values:
            clean["auto"] = _bool(values["auto"])
        if "health" in values:
            clean["health"] = _bool(values["health"])
        if "interval_minutes" in values:
            try:
                minutes = int(values["interval_minutes"])
            except (TypeError, ValueError):
                raise ValueError("Interval: not a number") from None
            if not 60 <= minutes <= 10080:
                raise ValueError("Interval must be between 1 hour and 1 week")
            clean["interval_minutes"] = minutes
        if "health_since" in values:
            since = str(values["health_since"] or "").strip()
            if since:
                try:
                    if date.fromisoformat(since) > date.today():
                        raise ValueError
                except ValueError:
                    raise ValueError("Health data since: a past date, as YYYY-MM-DD") from None
            clean["health_since"] = since
        if "rclone_remote" in values:
            remote = str(values["rclone_remote"] or "").strip()
            if remote and (":" not in remote or remote.startswith("-")):
                raise ValueError("rclone destination looks like remote:folder (for example gdrive:Garmin)")
            clean["rclone_remote"] = remote
        for name, value in clean.items():
            setattr(self, name, value)

    def save(self) -> None:
        self.path().parent.mkdir(parents=True, exist_ok=True)
        self.path().write_text(json.dumps(asdict(self), indent=2))


def _bool(value) -> bool:
    return value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "on")
