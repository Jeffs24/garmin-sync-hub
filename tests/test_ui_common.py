"""Pieces shared by every page: notices, auto-sync line, health check, Liftosaur undo."""
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from garmin_sync_hub import db, server, templating


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "_has_garmin_tokens", lambda config=None: True)
    monkeypatch.setattr(server.zwift_module, "start", lambda: None)
    server._is_configured_cache = None
    with TestClient(server.app) as c:
        yield c


def test_interval_labels():
    assert [templating.interval_label(m) for m in (15, 60, 90, 120, 1440)] == [
        "15 min", "1 hour", "90 min", "2 hours", "24 hours"]


def test_autosync_line_is_the_same_for_both_sources():
    last = datetime(2026, 11, 22, 10, tzinfo=timezone.utc)
    on = server.liftosaur_autosync_line({"enabled": True, "last_at": last, "next_at": None})
    assert 'datetime="2026-11-22T10:00:00Z"' in on and on.startswith("Last:")
    off = server.liftosaur_autosync_line({"enabled": False})
    assert off.startswith("Off:") and "Sync now" in off


def test_liftosaur_autosync_toggle_shows_next_run(client, monkeypatch):
    saved = {}
    monkeypatch.setattr(server, "load_config", lambda: {"auto_sync": dict(saved)})
    monkeypatch.setattr(server, "save_config", lambda cfg: saved.update(cfg["auto_sync"]))
    r = client.post("/api/toggle-autosync", data={"enabled": "true", "interval": "60"})
    assert r.status_code == 200 and "Next:" in r.text and "<time" in r.text
    r = client.post("/api/toggle-autosync", data={"enabled": "false", "interval": "60"})
    assert r.text.startswith("Off:")
    assert server._autosync_timer is None


def test_settings_save_shows_a_notice(client, monkeypatch):
    monkeypatch.setattr(server, "save_config", lambda cfg: None)
    r = client.post("/settings", data={"garmin_email": ""}, follow_redirects=False)
    assert r.headers["location"] == "/settings?saved=settings"
    page = client.get(r.headers["location"]).text
    assert "Settings saved." in page
    assert 'id="settings-save"' in page and "Save Zwift settings" not in page  # one button, label set by tab


def test_error_notice_is_escaped(client):
    page = client.get("/activity?error=<script>x</script>").text
    assert "&lt;script&gt;x&lt;/script&gt;" in page


def test_healthz_needs_no_sign_in(client, monkeypatch):
    monkeypatch.setenv("HUB_PASSWORD", "secret")
    monkeypatch.setattr(server, "auth_enabled", lambda: True)
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and "garmin" in body and set(body["jobs"]) == {
        "zwift_scan", "garmin_check", "liftosaur_autosync"}


def test_single_sync_failure_is_a_notice(client, monkeypatch):
    class Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("Liftosaur is down")

    monkeypatch.setattr("garmin_sync_hub.liftosaur.liftosaur.LiftosaurClient", Boom)
    monkeypatch.setattr(server, "load_config", lambda: {"liftosaur_api_key": "k"})
    r = client.post("/api/sync/w-x", headers={"HX-Request": "true"})
    trigger = json.loads(r.headers["HX-Trigger"])
    assert "Liftosaur is down" in trigger["notify"]["message"] and r.headers["HX-Reswap"] == "none"


class FakeGarmin:
    def __init__(self, fail=False):
        self.deleted, self.fail = [], fail

    def delete_activity(self, aid):
        if self.fail:
            raise RuntimeError("Garmin said no")
        self.deleted.append(aid)


@pytest.fixture(autouse=True)
def _forget_test_workouts():
    """The Liftosaur database is shared by the test session: leave it as it was."""
    yield
    for wid in ("undo-1", "undo-2", "undo-3"):
        db.unsync(wid)
    conn = db.get_db()._get_conn()
    conn.execute("DELETE FROM sync_failures WHERE workout_id = 'w-x'")
    conn.commit()
    conn.close()


def _patch_garmin(monkeypatch, garmin):
    monkeypatch.setattr(server, "load_config", lambda: {"garmin_email": ""})
    monkeypatch.setattr("garmin_sync_hub.garmin.get_client", lambda email=None: garmin)


def test_undo_deletes_the_uploaded_activity(client, monkeypatch):
    garmin = FakeGarmin()
    _patch_garmin(monkeypatch, garmin)
    db.mark_synced("undo-1", "555", "Push", action="uploaded")
    r = client.post("/api/workouts/undo-1/undo",
                    headers={"HX-Request": "true", "HX-Current-URL": "http://x/liftosaur/workouts?page=2"})
    assert garmin.deleted == [555]
    assert not db.is_synced("undo-1")
    assert r.headers["HX-Redirect"] == "/liftosaur/workouts?page=2&saved=undone#workout-undo-1"


def test_undo_never_deletes_a_watch_activity(client, monkeypatch):
    garmin = FakeGarmin()
    _patch_garmin(monkeypatch, garmin)
    db.mark_synced("undo-2", "556", "Pull", action="updated")
    r = client.post("/api/workouts/undo-2/undo", headers={"HX-Request": "true"})
    assert garmin.deleted == [] and db.is_synced("undo-2")
    assert "Only a workout uploaded" in json.loads(r.headers["HX-Trigger"])["notify"]["message"]


def test_undo_keeps_the_record_when_garmin_refuses(client, monkeypatch):
    _patch_garmin(monkeypatch, FakeGarmin(fail=True))
    db.mark_synced("undo-3", "557", "Legs", action="uploaded")
    r = client.post("/api/workouts/undo-3/undo", headers={"HX-Request": "true"})
    assert db.is_synced("undo-3")
    assert "Garmin said no" in json.loads(r.headers["HX-Trigger"])["notify"]["message"]
