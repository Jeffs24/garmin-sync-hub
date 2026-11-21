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
    assert "Connect Zwift" in r.text
    assert 'href="/zwift" class="active"' in r.text


def test_get_on_action_urls_goes_back_to_the_page(client):
    c, _ = client
    for path in ("/zwift/scan", "/zwift/merge-all", "/zwift/sync", "/zwift/login"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/zwift"


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


def test_dashboard_shows_zwift_summary(client, monkeypatch):
    c, m = client
    m.store.upsert_detected(1, 2, "Run A", "Zwift A", "2026-11-20T12:00:00+00:00", 0.97)
    monkeypatch.setattr(server, "load_config", lambda: {"liftosaur_api_key": "", "user_profile": {}})
    r = c.get("/")
    assert r.status_code == 200
    assert "1 pair waiting" in r.text
    assert "Add your Liftosaur API key" in r.text


def test_summary_of_merged_file():
    merged, _ = merge_track(watch_fit(START, 120), zwift_fit(START, 120))
    pv = summarize(merged)
    assert pv.records == 120 and pv.records_with_position == 120
    assert pv.stats["GPS points"] == "120 / 120"
