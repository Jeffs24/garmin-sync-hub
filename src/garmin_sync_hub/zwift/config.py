"""Zwift module settings, from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..paths import data_dir


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
        )
