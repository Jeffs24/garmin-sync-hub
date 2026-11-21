"""Sync Liftosaur workouts to Garmin Connect."""

from importlib.metadata import version, PackageNotFoundError

from garmin_sync_hub.env import load_local_env

load_local_env()

try:
    __version__ = version("garmin_sync_hub")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"
