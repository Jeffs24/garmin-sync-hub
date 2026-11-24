"""Zwift module settings.

The defaults are below; the Settings page saves changes in data/zwift_settings.json.
Before 1.2.0 the defaults could come from ZWIFT_* environment variables: when no
settings file exists yet, those are imported into it once (see LEGACY_ENV).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..paths import data_dir

log = logging.getLogger(__name__)

SCAN_INTERVALS = (15, 30, 60, 120, 240, 360, 720, 1440)

# Settings the web UI may change, with their type.
EDITABLE: dict[str, type] = {
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


# .env names of the editable settings before 1.2.0, imported once into the settings file.
LEGACY_ENV: dict[str, str] = {
    "scan_interval_minutes": "ZWIFT_SCAN_INTERVAL_MINUTES",
    "scan_days": "ZWIFT_SCAN_DAYS",
    "zwift_auto_sync": "ZWIFT_AUTO_SYNC",
    "auto_merge": "ZWIFT_AUTO_MERGE",
    "delete_source": "ZWIFT_DELETE_SOURCE",
    "copy_altitude": "ZWIFT_COPY_ALTITUDE",
    "min_overlap": "ZWIFT_MIN_OVERLAP",
    "min_position_ratio": "ZWIFT_MIN_POSITION_RATIO",
    "time_tolerance_s": "ZWIFT_TIME_TOLERANCE_S",
}


@dataclass
class Config:
    data_dir: Path
    scan_days: int = 7
    scan_interval_minutes: int = 30
    auto_merge: bool = False
    delete_source: bool = True
    copy_altitude: bool = False
    min_overlap: float = 0.5
    min_position_ratio: float = 0.5
    time_tolerance_s: int = 2
    zwift_auto_sync: bool = True
    # Before 1.6 the scan could be turned off: now pairs are always looked for, and auto-sync
    # (auto_merge) decides whether they are merged. Kept so old settings files still load.
    auto_scan: bool = True
    # How a Zwift copy is recognized: its device name, and its FIT manufacturer (260 = Zwift).
    source_names: list[str] = field(default_factory=lambda: ["zwift"])
    source_manufacturers: list[int] = field(default_factory=lambda: [260])

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
    def defaults(cls) -> "Config":
        return cls(data_dir=data_dir())

    @classmethod
    def load(cls) -> "Config":
        """The defaults, then the values saved from the web UI."""
        cfg = cls.defaults()
        try:
            saved = json.loads(cfg.settings_file.read_text())
        except FileNotFoundError:
            cfg._import_legacy_env()
            return cfg
        except (OSError, ValueError) as e:
            log.warning("Could not read %s: %s", cfg.settings_file, e)
            return cfg
        try:
            cfg.update(saved)
        except ValueError as e:
            log.warning("Ignoring saved Zwift settings: %s", e)
        return cfg

    def _import_legacy_env(self) -> None:
        """Keep the ZWIFT_* values of an old .env by saving them once in the settings file."""
        values = {name: os.environ[env] for name, env in LEGACY_ENV.items() if os.environ.get(env, "").strip()}
        if not values:
            return
        try:
            self.update(values)
            self.save()
        except (ValueError, OSError) as e:
            log.warning("Could not import the ZWIFT_* settings from .env: %s", e)
            return
        log.info("Imported %s from .env into %s. Change them in Settings → Zwift from now on.",
                 ", ".join(LEGACY_ENV[name] for name in values), self.settings_file)

    def update(self, values: dict) -> None:
        """Validate and apply editable settings. Raises ValueError, changing nothing."""
        clean = {}
        for name, value in values.items():
            kind = EDITABLE.get(name)
            if kind is None:
                continue
            if kind is bool:
                clean[name] = value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "on", "oui")
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
