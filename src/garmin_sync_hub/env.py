"""Environment loading helpers."""

from __future__ import annotations

from dotenv import find_dotenv, load_dotenv


def load_local_env() -> None:
    """Load a local .env file into os.environ if one is present.

    Existing exported environment variables keep precedence.
    """
    dotenv_path = find_dotenv(usecwd=True)
    if dotenv_path:
        load_dotenv(dotenv_path=dotenv_path, override=False)


# Variables that no longer do anything: sign-ins and settings moved to the web UI (1.2.0).
OBSOLETE_ENV: dict[str, str] = {
    "GARMIN_EMAIL": "connect Garmin in Settings → Garmin",
    "GARMIN_PASSWORD": "connect Garmin in Settings → Garmin",
    "ZWIFT_EMAIL": "connect Zwift in Settings → Zwift",
    "ZWIFT_PASSWORD": "connect Zwift in Settings → Zwift",
    "ZWIFT_SOURCE_NAMES": "no longer configurable",
    "ZWIFT_SOURCE_MANUFACTURERS": "no longer configurable",
}


def obsolete_env_messages() -> list[str]:
    """One line per obsolete variable still set, for the startup log."""
    import os

    from garmin_sync_hub.zwift.config import LEGACY_ENV

    found = {name: hint for name, hint in OBSOLETE_ENV.items() if os.environ.get(name)}
    found.update({env: "now in Settings → Zwift" for env in LEGACY_ENV.values() if os.environ.get(env)})
    return [f"{name} in .env is ignored: {hint}. You can remove it." for name, hint in sorted(found.items())]
