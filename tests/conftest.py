"""Keep every test away from the real data directory."""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="garmin-sync-hub-tests-")

import pytest


@pytest.fixture(autouse=True)
def _no_garmin_health_loop(monkeypatch):
    """The app checks Garmin in the background on startup; tests call check() themselves."""
    from garmin_sync_hub import server

    monkeypatch.setattr(server, "_garmin_health_loop", lambda: None)
