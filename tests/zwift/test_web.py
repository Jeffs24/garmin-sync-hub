import threading

import pytest
from fastapi.testclient import TestClient

from garmin_sync_hub import server
from garmin_sync_hub.zwift.merge import merge_track
from garmin_sync_hub.zwift.preview import summarize
from garmin_sync_hub.zwift.store import Store

from fitbuilder import watch_fit, zwift_fit

START = 1_100_000_000


@pytest.fixture
def client(tmp_path, monkeypatch):
    m = server.zwift_module
    m.cfg.data_dir = tmp_path
    m.store = Store(tmp_path / "zwift.db")
    m.service.store = m.store
    m.service.cfg = m.cfg
    monkeypatch.setattr(server, "_has_garmin_tokens", lambda config=None: True)
    monkeypatch.setattr(m.garmin, "has_tokens", lambda: True)
    monkeypatch.setattr(m, "start", lambda: None)
    server._is_configured_cache = None
    with TestClient(server.app) as c:
        yield c, m


def test_zwift_page_lists_pairs_and_escapes(client):
    c, m = client
    m.store.upsert_detected(1, 2, "Treadmill <b>", "Zwift - Watopia", "2026-11-20T12:00:00+00:00", 0.97)
    r = c.get("/zwift")
    assert r.status_code == 200
    assert "Zwift - Watopia" in r.text
    assert "Treadmill &lt;b&gt;" in r.text
    assert "/zwift/pairs/1/merge" in r.text
    assert "Merge all (1)" in r.text
    assert 'href="/zwift" class="active"' in r.text
    assert "Connect Zwift" in c.get("/zwift/account").text


def test_get_on_action_urls_goes_back_to_the_page(client):
    c, _ = client
    for path, target in ((("/zwift/scan", "/zwift"), ("/zwift/merge-all", "/zwift"),
                          ("/zwift/sync", "/zwift/account"), ("/zwift/login", "/zwift/account"))):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == target


def test_merge_all_queues_waiting_pairs(client, monkeypatch):
    c, m = client
    m.store.upsert_detected(1, 2, "Run A", "Zwift A", "2026-11-20T12:00:00+00:00", 0.97)
    m.store.upsert_detected(3, 4, "Run B", "Zwift B", "2026-11-19T12:00:00+00:00", 0.97)
    done = threading.Event()
    monkeypatch.setattr(m.service, "merge_all", lambda: done.set())
    r = c.post("/zwift/merge-all", follow_redirects=False)
    assert r.status_code == 303
    assert {row["status"] for row in m.store.all()} == {"queued"}
    assert done.wait(2)
    assert 'http-equiv="refresh"' in c.get("/zwift").text


def test_preview_page(client):
    c, m = client
    m.store.upsert_detected(1, 2, "Treadmill Run", "Zwift - Watopia", "2026-11-20T12:00:00+00:00", 0.97)
    r = c.get("/zwift/pairs/1/preview")
    assert "Simulate" in r.text and "first" in r.text
    folder = m.cfg.backup_dir / "1"
    folder.mkdir(parents=True)
    merged, _ = merge_track(watch_fit(START, 120), zwift_fit(START, 120))
    (folder / "1_merged.fit").write_bytes(merged)
    r = c.get("/zwift/pairs/1/preview")
    assert r.status_code == 200
    assert "L.polyline" in r.text and "Heart rate" in r.text
    assert "delete the watch activity #1 and the Zwift copy #2" in r.text
    assert c.get("/zwift/pairs/99/preview").status_code == 404


def test_overview_shows_every_source(client, monkeypatch):
    c, m = client
    m.store.upsert_detected(1, 2, "Run A", "Zwift A", "2026-11-20T12:00:00+00:00", 0.97)
    monkeypatch.setattr(server, "load_config", lambda: {"liftosaur_api_key": "", "user_profile": {}})
    r = c.get("/")
    assert r.status_code == 200
    assert "1 ready" in r.text and "Review 1 pair" in r.text
    assert "Add your Liftosaur API key" in r.text
    assert "Ready to merge" in r.text  # the pair is in the activity feed
    assert "Zwift → Garmin" in r.text


def test_summary_of_merged_file():
    merged, _ = merge_track(watch_fit(START, 120), zwift_fit(START, 120))
    pv = summarize(merged)
    assert pv.records == 120 and pv.records_with_position == 120
    assert pv.stats["GPS points"] == "120 / 120"


def test_activity_page_filters_by_source(client, monkeypatch):
    c, m = client
    m.store.upsert_detected(1, 2, "Run A", "Zwift A", "2026-11-20T12:00:00+00:00", 0.97)
    m.store.set_status(1, "merged", "done", new_id=555)
    m.store.upsert_detected(3, 4, "Run B", "Zwift B", "2026-11-19T12:00:00+00:00", 0.97)
    m.store.set_status(3, "failed", "Garmin said no")
    r = c.get("/activity")
    assert r.status_code == 200
    assert "Map added" in r.text and "connect.garmin.com/modern/activity/555" in r.text
    assert "Garmin said no" in r.text
    attention = c.get("/activity?source=attention").text
    assert "Garmin said no" in attention and "Map added" not in attention
    assert "Nothing synced yet." in c.get("/activity?source=liftosaur").text


def test_old_pages_redirect(client):
    c, _ = client
    for old, new in (("/history", "/activity?source=liftosaur"), ("/workouts", "/liftosaur/workouts"),
                     ("/mappings", "/liftosaur/mappings")):
        r = c.get(old, follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == new


def test_settings_has_one_tab_per_source(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(server, "load_config", lambda: {"liftosaur_api_key": "", "user_profile": {}, "timing": {}})
    r = c.get("/settings")
    assert r.status_code == 200
    for tab in ("garmin", "liftosaur", "zwift"):
        assert f'data-tab-button="{tab}"' in r.text and f'data-tab="{tab}"' in r.text
    assert "ZWIFT_AUTO_MERGE" in r.text


def test_sync_one_reports_a_bad_liftosaur_key_as_json(client, monkeypatch):
    c, _ = client
    from garmin_sync_hub.liftosaur import liftosaur

    def refuse(self, *a, **k):
        raise liftosaur.LiftosaurAuthError("bad key")

    monkeypatch.setattr(server, "load_config", lambda: {"liftosaur_api_key": "x", "user_profile": {}})
    monkeypatch.setattr(liftosaur.LiftosaurClient, "get_workout_count", refuse)
    r = c.post("/api/sync-one")
    assert r.status_code == 401
    assert "Liftosaur API key" in r.json()["error"]
