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
    assert "Sync all (1)" in r.text and "Check now" in r.text
    assert 'href="/zwift" class="active"' in r.text
    # The old History and files page: account and file upload are in Settings → Zwift
    old = c.get("/zwift/account", follow_redirects=False)
    assert old.status_code == 303 and old.headers["location"] == "/settings#zwift"


def test_get_on_action_urls_goes_back_to_the_page(client):
    c, _ = client
    for path, target in ((("/zwift/scan", "/zwift"), ("/zwift/merge-all", "/zwift"), ("/zwift/check", "/zwift"),
                          ("/zwift/sync", "/zwift"), ("/zwift/upload", "/settings#zwift"), ("/zwift/login", "/settings#zwift"))):
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
    page = c.get("/zwift").text
    assert 'http-equiv="refresh"' not in page  # rows refresh themselves instead
    assert page.count('hx-trigger="every 3s"') == 2


def test_preview_opens_in_the_card(client):
    """Map and charts load into the pair's card (no separate page), like a workout's heart rate."""
    c, m = client
    hx = {"HX-Request": "true"}
    m.store.upsert_detected(1, 2, "Treadmill Run", "Zwift - Watopia", "2026-11-20T12:00:00+00:00", 0.97)
    r = c.get("/zwift/pairs/1/preview", headers=hx)
    assert "Simulate" in r.text and "first" in r.text and "<html" not in r.text
    folder = m.cfg.backup_dir / "1"
    folder.mkdir(parents=True)
    merged, _ = merge_track(watch_fit(START, 120), zwift_fit(START, 120))
    (folder / "1_merged.fit").write_bytes(merged)
    r = c.get("/zwift/pairs/1/preview", headers=hx)
    assert r.status_code == 200
    assert "L.polyline" in r.text and "Heart rate" in r.text and 'id="zw-map-1"' in r.text
    assert "deletes the watch activity #1 and the Zwift copy #2" in r.text
    assert c.get("/zwift/pairs/99/preview", headers=hx).status_code == 404
    old_link = c.get("/zwift/pairs/1/preview", follow_redirects=False)  # opened as a page: back to the card
    assert old_link.status_code == 303 and old_link.headers["location"] == "/zwift#pair-1"

    m.store.set_status(1, "simulated", "ok")
    page = c.get("/zwift").text
    assert 'hx-get="/zwift/pairs/1/preview" hx-trigger="toggle once"' in page and "Map and charts" in page
    assert 'href="/zwift/pairs/1/preview"' not in page


def test_overview_shows_every_source(client, monkeypatch):
    c, m = client
    m.store.upsert_detected(1, 2, "Run A", "Zwift A", "2026-11-20T12:00:00+00:00", 0.97)
    monkeypatch.setattr(server, "load_config", lambda: {"liftosaur_api_key": "", "user_profile": {}})
    r = c.get("/")
    assert r.status_code == 200
    assert "1 not synced" in r.text and "Review 1 pair" in r.text
    assert "Add your Liftosaur API key" in r.text
    assert "Not synced" in r.text  # the pair is in the activity feed
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
                     ("/mappings", "/settings/liftosaur/mappings"),
                     ("/liftosaur/mappings", "/settings/liftosaur/mappings")):
        r = c.get(old, follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == new


def test_settings_has_one_tab_per_source(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(server, "load_config", lambda: {"liftosaur_api_key": "", "user_profile": {}, "timing": {}})
    r = c.get("/settings")
    assert r.status_code == 200
    for tab in ("garmin", "liftosaur", "zwift"):
        assert f'data-tab-button="{tab}"' in r.text and f'data-tab="{tab}"' in r.text
    assert 'action="/zwift/settings"' in r.text and 'name="scan_days"' in r.text
    # Every source's auto-sync switch is in Settings too, and the Zwift file upload
    for prefix in ("set-lift-autosync", "set-zw-autosync", "set-dogs-autosync", "set-bk-autosync"):
        assert f'id="{prefix}-toggle"' in r.text
    assert 'action="/zwift/upload"' in r.text and 'href="/settings/liftosaur/mappings"' in r.text


def test_settings_never_calls_the_liftosaur_api(client, monkeypatch):
    c, _ = client
    from garmin_sync_hub.liftosaur import liftosaur

    def boom(self, *a, **k):
        raise AssertionError("settings page called the Liftosaur API")

    monkeypatch.setattr(server, "load_config", lambda: {"liftosaur_api_key": "x", "user_profile": {}, "timing": {}})
    monkeypatch.setattr(liftosaur.LiftosaurClient, "get_workouts", boom)
    r = c.get("/settings")
    assert r.status_code == 200


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


@pytest.fixture
def own_cfg(client, monkeypatch, tmp_path):
    """A private copy of the Zwift config, so settings tests don't leak into others."""
    import dataclasses

    c, m = client
    cfg = dataclasses.replace(m.cfg, data_dir=tmp_path, auto_merge=True, scan_interval_minutes=30)
    monkeypatch.setattr(m, "cfg", cfg)
    monkeypatch.setattr(m.service, "cfg", cfg)
    return c, m


def test_zwift_settings_are_saved_and_applied(own_cfg):
    from garmin_sync_hub.zwift.config import Config

    c, m = own_cfg
    r = c.post("/zwift/settings", data={
        "auto_merge": ["off"], "scan_interval_minutes": "60", "min_overlap": "0.6",
    }, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings?saved=zwift#zwift"
    assert (m.cfg.auto_merge, m.cfg.scan_interval_minutes) == (False, 60)
    assert m.cfg.min_overlap == 0.6
    assert m.state["next_scan"] is not None  # auto-sync off: auto-scan still looks for pairs

    reloaded = Config.defaults()
    reloaded.data_dir = m.cfg.data_dir
    import json
    reloaded.update(json.loads(m.cfg.settings_file.read_text()))
    assert reloaded.auto_merge is False and reloaded.min_overlap == 0.6

    page = c.get("/settings")
    assert 'name="min_overlap"' in page.text and 'value="0.6"' in page.text
    assert 'name="scan_days"' not in page.text  # no period to set: a scan looks back to the last one


def test_bad_zwift_settings_change_nothing(own_cfg):
    c, m = own_cfg
    r = c.post("/zwift/settings", data={"min_overlap": "5", "auto_merge": "off"}, follow_redirects=False)
    assert r.status_code == 303 and "error=Zwift+settings+not+saved" in r.headers["location"]
    assert m.cfg.auto_merge is True and m.cfg.min_overlap != 5
    assert "between 0 and 1" in c.get(r.headers["location"]).text


def test_zwift_autoscan_and_autosync_switches(own_cfg):
    c, m = own_cfg
    page = c.get("/zwift").text
    assert 'id="zw-autoscan-toggle"' in page and 'id="zw-autosync-toggle"' in page
    r = c.post("/zwift/autoscan", data={"enabled": "false", "interval": "120"})
    assert r.status_code == 200 and r.text.startswith("Off:") and "Check now" in r.text
    assert m.cfg.auto_scan is False and m.cfg.scan_interval_minutes == 120 and m.state["next_scan"] is None
    r = c.post("/zwift/autoscan", data={"enabled": "true", "interval": "120"})
    assert m.cfg.auto_scan is True and "Next:" in r.text
    r = c.post("/zwift/autosync", data={"enabled": "false"})
    assert r.text.startswith("Off:") and m.cfg.auto_merge is False and m.cfg.auto_scan is True


def test_scheduled_check_finds_pairs_and_merges_only_with_auto_sync(own_cfg, monkeypatch):
    _, m = own_cfg
    calls = []
    monkeypatch.setattr(m.service, "scan", lambda days=None: calls.append(("scan", days)))
    monkeypatch.setattr(m.service, "merge", lambda tid: calls.append(("merge", tid)))
    monkeypatch.setattr(m.zwift, "connected", lambda: False)
    m.store.upsert_detected(1, 2, "Run", "Zwift", "2026-11-20T12:00:00+00:00", 0.97)
    m.cfg.auto_merge = False
    assert m.run_scan()
    assert calls == [("scan", calls[0][1])] and calls[0][1] > 3000  # first scan: the whole history, no merge
    calls.clear()
    assert m.run_scan()
    assert calls == [("scan", 4)]  # then back to the last scan that worked, minus 3 days
    calls.clear()
    m.cfg.auto_merge = True
    assert m.run_scan()
    assert ("merge", 1) in calls  # auto-sync: what was found is merged
    calls.clear()
    m.store.set_status(1, "detected", None)
    assert m.run_scan(full=True)  # Check now: the whole history, merged too with auto-sync on
    assert calls[0][0] == "scan" and calls[0][1] > 3000 and ("merge", 1) in calls


def test_scan_loop_waits_and_wakes_on_settings_change(own_cfg):
    from datetime import datetime, timedelta, timezone

    _, m = own_cfg
    m._stop, m._wake = threading.Event(), threading.Event()  # a previous app shutdown set _stop
    m.state["last_scan"] = datetime.now(timezone.utc)
    m.cfg.scan_interval_minutes = 1440
    done = threading.Event()
    t = threading.Thread(target=lambda: (m._wait_for_next_scan(), done.set()), daemon=True)
    t.start()
    assert not done.wait(0.2)  # checked just now: waits for the interval
    m.state["last_scan"] = datetime.now(timezone.utc) - timedelta(days=2)
    m.settings_changed()
    assert done.wait(2)  # a check is overdue: returns right away


def test_pairs_page_links_to_how_it_works_in_settings(own_cfg):
    c, m = own_cfg
    page = c.get("/zwift").text
    assert 'href="/settings#zwift">How it works' in page and "card pipeline" not in page  # moved to Settings


def test_restore_button_only_for_merged_pairs_with_backups(client, monkeypatch):
    c, m = client
    m.store.upsert_detected(1, 2, "Run", "Zwift", "2026-11-20T12:00:00+00:00", 0.97)
    assert "/zwift/pairs/1/restore" not in c.get("/zwift").text
    assert c.post("/zwift/pairs/1/restore", follow_redirects=False).status_code == 409

    m.store.set_status(1, "merged", "Merged", new_id=101)
    folder = m.cfg.backup_dir / "1"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "1_watch.fit").write_bytes(b"x")
    assert "/zwift/pairs/1/restore" in c.get("/zwift").text
    called = threading.Event()
    monkeypatch.setattr(m.service, "restore", lambda tid: called.set())
    r = c.post("/zwift/pairs/1/restore", follow_redirects=False)
    assert r.status_code == 303 and called.wait(2)


def test_pair_actions_update_the_row_in_place(client, monkeypatch):
    c, m = client
    m.store.upsert_detected(1, 2, "Run A", "Zwift A", "2026-11-20T12:00:00+00:00", 0.97)
    monkeypatch.setattr(m.service, "simulate", lambda tid: None)
    r = c.post("/zwift/pairs/1/simulate", headers={"HX-Request": "true"})
    assert r.status_code == 200 and r.text.lstrip().startswith('<div class="card hist-card src-zwift"')
    assert 'id="pair-1"' in r.text and 'hx-get="/zwift/pairs/1/row"' in r.text  # running: refreshes itself
    m.store.set_status(1, "simulated", "ok")
    row = c.get("/zwift/pairs/1/row").text
    assert "hx-trigger" not in row.split(">")[0] and "Simulated, ready to merge" in row
    # The same four buttons as every card: Garmin, Re-sync (Merge here), Restore (not yet), FIT
    assert "connect.garmin.com/modern/activity/1" in row and "/zwift/pairs/1/merge" in row
    assert "/zwift/pairs/1/merged.fit" in row and 'title="Only a merged pair with its backups can be restored"' in row
    assert 'datetime="2026-11-20T12:00:00+00:00"' in row  # shown in local time by the page
    assert c.post("/zwift/pairs/1/ignore", follow_redirects=False).status_code == 303  # without HTMX


def test_pairs_filter_by_status(client):
    c, m = client
    m.store.upsert_detected(1, 2, "Waiting run", "Z1", "2026-11-20T12:00:00+00:00", 0.97)
    m.store.upsert_detected(3, 4, "Merged run", "Z2", "2026-11-19T12:00:00+00:00", 0.97)
    m.store.set_status(3, "merged", "Merged", new_id=9)
    page = c.get("/zwift?status=merged").text
    assert "Merged run" in page and "Waiting run" not in page
    assert 'href="/zwift?status=merged" class="active"' in page
    assert "Waiting run" in c.get("/zwift?status=nonsense").text  # unknown filter: all
    assert "No pair with this status" in c.get("/zwift?status=failed").text


def test_failed_pair_shows_its_next_retry(client):
    c, m = client
    m.cfg.auto_merge = True
    try:
        m.store.upsert_detected(1, 2, "Run", "Z", "2026-11-20T12:00:00+00:00", 0.97)
        m.store.mark_failed(1, "Import failed")
        assert "Next try with auto-merge" in c.get("/zwift").text
    finally:
        m.cfg.auto_merge = False


def test_zwift_account_is_connected_from_settings(client, monkeypatch):
    c, m = client
    monkeypatch.setattr(m.zwift, "connected", lambda: False)
    page = c.get("/settings").text
    assert 'action="/zwift/login"' in page and 'form="zwift-login"' in page and "Connect Zwift" in page

    def refuse(email, password):
        raise RuntimeError("bad password")

    monkeypatch.setattr(m.zwift, "login", refuse)
    r = c.post("/zwift/login", data={"email": "a@b.c", "password": "x"}, follow_redirects=False)
    assert r.headers["location"].startswith("/settings?error=Zwift+login+refused")
    assert r.headers["location"].endswith("#zwift")

    monkeypatch.setattr(m.zwift, "login", lambda email, password: None)
    r = c.post("/zwift/login", data={"email": "a@b.c", "password": "x"}, follow_redirects=False)
    assert r.headers["location"] == "/settings?saved=zwift-connected#zwift"
    monkeypatch.setattr(m.zwift, "connected", lambda: True)
    assert "Zwift account connected." in c.get(r.headers["location"]).text
    assert 'form="zwift-logout"' in c.get("/settings").text


def test_sync_now_runs_a_full_pass_in_the_background(client, monkeypatch):
    c, m = client
    done = threading.Event()
    monkeypatch.setattr(m, "run_scan", lambda: done.set() or True)
    r = c.post("/zwift/sync-now", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Zwift sync started" in r.headers["HX-Trigger"]
    assert done.wait(2)
    monkeypatch.setattr(m.garmin, "has_tokens", lambda: False)
    assert c.post("/zwift/sync-now", headers={"HX-Request": "true"}).status_code == 409
    assert "/zwift/sync-now" in c.get("/").text
