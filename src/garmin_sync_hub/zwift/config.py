"""Zwift module settings.

Environment variables give the defaults; values saved from the Settings page
(data/zwift_settings.json) take precedence.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from ..paths import data_dir

log = logging.getLogger(__name__)

SCAN_INTERVALS = (15, 30, 60, 120, 240, 360, 720, 1440)

# Settings the web UI may change, with their type.
EDITABLE: dict[str, type] = {
    "auto_scan": bool,
    "scan_interval_minutes": int,
    "scan_days": int,
    "zwift_auto_sync": bool,
    "auto_merge": bool,
    "delete_source": bool,
    "copy_altitude": bool,
    "min_overlap": float,
    "min_position_ratio": float,
    "time_tolerance_s": int,
}


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "oui")


@dataclass
class Config:
    data_dir: Path
    scan_days: int
    scan_interval_minutes: int
    auto_merge: bool
    delete_source: bool
    copy_altitude: bool
    min_overlap: float
    min_position_ratio: float
    time_tolerance_s: int
    source_names: list[str]
    source_manufacturers: list[int]
    zwift_email: str | None = None
    zwift_password: str | None = None
    zwift_auto_sync: bool = True
    auto_scan: bool = True

    @property
    def token_dir(self) -> Path:
        return self.data_dir / "garminconnect"

    @property
    def zwift_token_file(self) -> Path:
        return self.data_dir / "zwift_tokens.json"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "zwift_backups"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "zwift.db"

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "zwift_settings.json"

    @classmethod
    def load(cls) -> "Config":
        """Environment defaults, then the values saved from the web UI."""
        cfg = cls.from_env()
        try:
            saved = json.loads(cfg.settings_file.read_text())
        except FileNotFoundError:
            return cfg
        except (OSError, ValueError) as e:
            log.warning("Could not read %s: %s", cfg.settings_file, e)
            return cfg
        try:
            cfg.update(saved)
        except ValueError as e:
            log.warning("Ignoring saved Zwift settings: %s", e)
        return cfg

    def update(self, values: dict) -> None:
        """Validate and apply editable settings. Raises ValueError, changing nothing."""
        clean = {}
        for name, value in values.items():
            kind = EDITABLE.get(name)
            if kind is None:
                continue
            if kind is bool:
                clean[name] = value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "on")
            else:
                try:
                    clean[name] = kind(value)
                except (TypeError, ValueError):
                    raise ValueError(f"{name}: not a number") from None
        if "scan_interval_minutes" in clean and not 5 <= clean["scan_interval_minutes"] <= 1440:
            raise ValueError("Scan interval must be between 5 and 1440 minutes")
        if "scan_days" in clean and not 1 <= clean["scan_days"] <= 365:
            raise ValueError("Period scanned must be between 1 and 365 days")
        for name in ("min_overlap", "min_position_ratio"):
            if name in clean and not 0 < clean[name] <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if "time_tolerance_s" in clean and not 0 <= clean["time_tolerance_s"] <= 60:
            raise ValueError("Time tolerance must be between 0 and 60 seconds")
        for name, value in clean.items():
            setattr(self, name, value)

    def save(self) -> None:
        """Write the editable settings next to the other data."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        data = {name: getattr(self, name) for name in EDITABLE}
        self.settings_file.write_text(json.dumps(data, indent=2))

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            data_dir=data_dir(),
            scan_days=int(os.getenv("ZWIFT_SCAN_DAYS", "7")),
            scan_interval_minutes=int(os.getenv("ZWIFT_SCAN_INTERVAL_MINUTES", "30")),
            auto_merge=_bool("ZWIFT_AUTO_MERGE", False),
            delete_source=_bool("ZWIFT_DELETE_SOURCE", True),
            copy_altitude=_bool("ZWIFT_COPY_ALTITUDE", False),
            min_overlap=float(os.getenv("ZWIFT_MIN_OVERLAP", "0.5")),
            min_position_ratio=float(os.getenv("ZWIFT_MIN_POSITION_RATIO", "0.5")),
            time_tolerance_s=int(os.getenv("ZWIFT_TIME_TOLERANCE_S", "2")),
            source_names=[s.strip() for s in os.getenv("ZWIFT_SOURCE_NAMES", "zwift").split(",") if s.strip()],
            # 260 = Zwift in the FIT profile
            source_manufacturers=[int(s) for s in os.getenv("ZWIFT_SOURCE_MANUFACTURERS", "260").split(",") if s.strip()],
            zwift_email=os.getenv("ZWIFT_EMAIL") or None,
            zwift_password=os.getenv("ZWIFT_PASSWORD") or None,
            zwift_auto_sync=_bool("ZWIFT_AUTO_SYNC", True),
            auto_scan=_bool("ZWIFT_AUTO_SCAN", True),
        )
