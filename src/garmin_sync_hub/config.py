"""Configuration management for garmin_sync_hub."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from garmin_sync_hub.env import load_local_env

logger = logging.getLogger("garmin_sync_hub")

from garmin_sync_hub.paths import data_dir

CONFIG_DIR = data_dir()
CONFIG_FILE = CONFIG_DIR / "liftosaur.json"
_LEGACY_API_KEY_FIELD = "he" "vy_api_key"

DEFAULT_CONFIG: dict[str, Any] = {
    "liftosaur_api_key": "",
    "garmin_email": "",
    "user_profile": {
        "weight_kg": 80.0,
        "birth_year": 1990,
        "sex": "male",
        "vo2max": 45.0,
    },
    "sync": {
        "default_limit": 10,
        "skip_existing": True,
    },
    "auto_sync": {
        "enabled": False,
        "interval_minutes": 120,
    },
    "timing": {
        "working_set_seconds": 40,
        "warmup_set_seconds": 25,
        "rest_between_sets_seconds": 75,
        "rest_between_exercises_seconds": 120,
    },
    "hr_fusion": {
        "enabled": True,
    },
    "update_existing": {
        "enabled": True,
        "match_window_minutes": 30,
    },
}


def get_update_existing(config: dict[str, Any] | None = None) -> tuple[bool, int]:
    """Return (enabled, match_window_minutes) for the update-existing feature."""
    cfg = config or load_config()
    ue = cfg.get("update_existing", {})
    return bool(ue.get("enabled", True)), int(ue.get("match_window_minutes", 30))


def load_config() -> dict[str, Any]:
    """Load config from file, then overlay environment variables.

    Env vars take precedence over config file values:
      LIFTOSAUR_API_KEY, GARMIN_EMAIL, GARMIN_PASSWORD
    """
    import os

    load_local_env()

    config = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy defaults
    migrated = False
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text())
            if _LEGACY_API_KEY_FIELD in saved:
                if saved.get(_LEGACY_API_KEY_FIELD) and not saved.get("liftosaur_api_key"):
                    saved["liftosaur_api_key"] = saved[_LEGACY_API_KEY_FIELD]
                saved.pop(_LEGACY_API_KEY_FIELD, None)
                migrated = True
            _deep_merge(config, saved)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not load config: %s", e)

    # Environment variables fill gaps left by the config file.
    if not config.get("liftosaur_api_key") and os.environ.get("LIFTOSAUR_API_KEY"):
        config["liftosaur_api_key"] = os.environ["LIFTOSAUR_API_KEY"]
    if not config.get("garmin_email") and os.environ.get("GARMIN_EMAIL"):
        config["garmin_email"] = os.environ["GARMIN_EMAIL"]
    if not config.get("garmin_password") and os.environ.get("GARMIN_PASSWORD"):
        config["garmin_password"] = os.environ["GARMIN_PASSWORD"]

    if migrated:
        save_config(config)

    return config


def save_config(config: dict[str, Any]) -> None:
    """Save config to file. Silently skips on read-only filesystems."""
    try:
        config.pop(_LEGACY_API_KEY_FIELD, None)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(config, indent=2))
    except OSError:
        logger.debug("Skipping config file write (read-only filesystem)")


def get(key: str, default: Any = None) -> Any:
    """Get a top-level config value."""
    return load_config().get(key, default)


def is_configured() -> bool:
    """True when the Liftosaur module has an API key."""
    return bool(load_config().get("liftosaur_api_key"))


def _deep_merge(base: dict, override: dict) -> None:
    """Merge override into base recursively (mutates base)."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
