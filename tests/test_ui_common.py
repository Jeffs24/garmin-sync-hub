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
    assert off.startswith("Off:") and "not synced" in off  # new workouts still show up


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


def test_single_sync_refreshes_the_saved_workout(client, monkeypatch):
    """A workout edited in Liftosaur shows its new values on the Workouts list after a re-sync."""
    import garmin_sync_hub.garmin as garmin
    import garmin_sync_hub.liftosaur.fit as fit
    import garmin_sync_hub.liftosaur.liftosaur as liftosaur
    import garmin_sync_hub.liftosaur.sync as lsync

    def workout(weight):
        return {"id": "w-x", "title": "Day 1", "start_time": "2026-11-24T13:30:18Z",
                "exercises": [{"title": "Chest Dip", "sets": [{"reps": 10, "weight_kg": weight}]}]}

    class FakeLiftosaur:
        def __init__(self, *a, **kw):
            pass

        def get_workouts(self, page=1, page_size=10):
            return {"workouts": [workout(0.0)], "page_count": 1}

    monkeypatch.setattr(liftosaur, "LiftosaurClient", FakeLiftosaur)
    monkeypatch.setattr(server, "load_config", lambda: {"liftosaur_api_key": "k"})
    monkeypatch.setattr(garmin, "get_client", lambda *a, **kw: object())
    monkeypatch.setattr(garmin, "find_activity_by_start_time", lambda *a, **kw: 42)
    monkeypatch.setattr(garmin, "rename_activity", lambda *a, **kw: None)
    monkeypatch.setattr(garmin, "set_description", lambda *a, **kw: None)
    monkeypatch.setattr(fit, "generate_fit", lambda *a, **kw: {})
    monkeypatch.setattr(lsync, "update_existing_activity_sets", lambda *a, **kw: True)
    _db = db.get_db()
    _db.set_app_config("workouts_page_1", {"workouts": [workout(66.6)], "page_count": 1})
    try:
        client.post("/api/sync/w-x", headers={"HX-Request": "true"})
        saved = _db.get_app_config("workouts_page_1")["workouts"][0]
        assert saved["exercises"][0]["sets"][0]["weight_kg"] == 0.0
    finally:
        _db.set_app_config("workouts_page_1", {})
        db.unsync("w-x")


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


def test_mappings_page_never_waits_for_liftosaur(client, monkeypatch):
    from garmin_sync_hub.liftosaur import liftosaur

    calls = []

    def iter_history(self, page_size=200):
        calls.append(1)
        yield {"exercises": [{"title": "Zorbo press"}]}

    monkeypatch.setattr(server, "load_config", lambda: {"liftosaur_api_key": "k"})
    monkeypatch.setattr(liftosaur.LiftosaurClient, "iter_history", iter_history)
    _db = db.get_db()
    _db.set_app_config(server.UNMAPPED_CACHE_KEY, {})
    try:
        page = client.get("/liftosaur/mappings").text
        assert calls == []  # the page itself never calls the API
        assert 'hx-get="/liftosaur/mappings/unmapped"' in page  # the list is refreshed after it shows
        card = client.get("/liftosaur/mappings/unmapped").text
        assert calls == [1] and "Zorbo press" in card and "hx-get" not in card
        page = client.get("/liftosaur/mappings").text  # now cached and recent
        assert "Zorbo press" in page and 'hx-get="/liftosaur/mappings/unmapped"' not in page
        client.get("/liftosaur/mappings/unmapped")
        assert calls == [1]  # a recent scan is not repeated
    finally:
        _db.set_app_config(server.UNMAPPED_CACHE_KEY, {})


def test_settings_explain_each_sync(client, monkeypatch):
    """How it works, with its drawing, is in Settings; the source pages link to it."""
    monkeypatch.setattr(server, "load_config", lambda: {
        "liftosaur_api_key": "", "user_profile": {}, "timing": {"working_set_seconds": 45},
        "update_existing": {"enabled": True, "match_window_minutes": 20}, "hr_fusion": {"enabled": True}})
    db.get_db().set_app_config("workouts_page_1", {"workouts": [], "page_count": 1})
    settings = client.get("/settings").text
    assert "How it works" in settings and "watch activity ±20 min" in settings and "45 s per set" in settings
    assert "heart rate (display only)" in settings and "Update Sets" in settings
    assert "watch data + GPS" in settings and "what is saved" in settings  # Zwift and Backup too
    page = client.get("/liftosaur/workouts").text
    assert 'href="/settings#liftosaur">How it works' in page and "card pipeline" not in page


def test_old_zwift_env_values_are_imported_once(tmp_path, monkeypatch):
    from garmin_sync_hub.zwift import config as zcfg

    monkeypatch.setattr(zcfg, "data_dir", lambda: tmp_path)
    monkeypatch.setenv("ZWIFT_AUTO_MERGE", "true")
    monkeypatch.setenv("ZWIFT_SCAN_DAYS", "14")
    cfg = zcfg.Config.load()
    assert cfg.auto_merge is True and cfg.scan_days == 14
    assert json.loads((tmp_path / "zwift_settings.json").read_text())["scan_days"] == 14
    monkeypatch.setenv("ZWIFT_SCAN_DAYS", "30")  # once saved, the file wins
    assert zcfg.Config.load().scan_days == 14


def test_defaults_without_env_or_file(tmp_path, monkeypatch):
    from garmin_sync_hub.zwift import config as zcfg

    monkeypatch.setattr(zcfg, "data_dir", lambda: tmp_path)
    for env in zcfg.LEGACY_ENV.values():
        monkeypatch.delenv(env, raising=False)
    cfg = zcfg.Config.load()
    assert (cfg.auto_merge, cfg.scan_days, cfg.source_manufacturers) == (False, 7, [260])
    assert not (tmp_path / "zwift_settings.json").exists()


def test_startup_lists_obsolete_env_variables(monkeypatch):
    from garmin_sync_hub.env import obsolete_env_messages

    for name in ("GARMIN_EMAIL", "GARMIN_PASSWORD", "ZWIFT_EMAIL", "ZWIFT_PASSWORD", "ZWIFT_AUTO_MERGE"):
        monkeypatch.delenv(name, raising=False)
    assert obsolete_env_messages() == []
    monkeypatch.setenv("GARMIN_PASSWORD", "x")
    monkeypatch.setenv("ZWIFT_AUTO_MERGE", "true")
    messages = obsolete_env_messages()
    assert any(m.startswith("GARMIN_PASSWORD in .env is ignored: connect Garmin") for m in messages)
    assert any(m.startswith("ZWIFT_AUTO_MERGE in .env is ignored: now in Settings") for m in messages)


def test_garmin_exercise_names_are_shown_not_just_numbers(client, monkeypatch):
    from garmin_sync_hub.liftosaur.mapper import fit_exercise_name, fit_exercise_names

    assert fit_exercise_name(24, 0) == "Alternating Dumbbell Shoulder Press"  # 0 is not "generic"
    assert fit_exercise_name(24, 4) == "Barbell Shoulder Press"
    assert fit_exercise_name(24, 9999) is None and fit_exercise_names(9999) == []
    names = client.get("/api/garmin-exercise-names/24").json()
    assert {"sub": 4, "name": "Barbell Shoulder Press"} in names
    monkeypatch.setattr(server, "_get_unmapped_exercises", lambda fetch=True: [])
    page = client.get("/liftosaur/mappings").text
    assert "Shoulder Press › " in page and "is not generic" in page


def test_resync_is_offered_on_every_synced_workout(client, monkeypatch):
    monkeypatch.setattr(server, "load_config", lambda: {"liftosaur_api_key": "", "user_profile": {}, "timing": {}})
    _db = db.get_db()
    _db.set_app_config("workouts_page_1", {"workouts": [
        {"id": "rs-1", "title": "Push", "exercises": [], "start_time": "2026-11-20T10:00:00Z"},
        {"id": "rs-2", "title": "Walk", "exercises": [], "start_time": "2026-11-19T10:00:00Z"}], "page_count": 1})
    db.mark_synced("rs-1", "11", "Push", action="uploaded")
    db.mark_synced("rs-2", "12", "Walk", action="updated")
    try:
        page = client.get("/liftosaur/workouts").text
        assert "resyncWorkout('rs-1', this, true)" in page  # replaces the uploaded activity
        assert "resyncWorkout('rs-2', this, false)" in page  # writes the sets into the watch activity again
    finally:
        db.unsync("rs-1"), db.unsync("rs-2")
        _db.set_app_config("workouts_page_1", {})


def test_restore_puts_the_watch_activity_back(client, monkeypatch):
    import garmin_sync_hub.garmin as garmin
    from garmin_sync_hub.liftosaur import originals

    class FakeClient:
        def get_activity(self, aid):
            return {"activityName": "Strength", "description": "from the watch"}

    originals.save_once(FakeClient(), 55, [{"setType": "ACTIVE", "repetitionCount": 9}])
    originals.save_once(FakeClient(), 55, [{"setType": "ACTIVE", "repetitionCount": 1}])  # kept once: the first
    calls = []
    monkeypatch.setattr(server, "load_config", lambda: {})
    monkeypatch.setattr(garmin, "get_client", lambda *a, **kw: object())
    monkeypatch.setattr(garmin, "update_exercise_sets", lambda c, aid, sets: calls.append(("sets", aid, sets)))
    monkeypatch.setattr(garmin, "rename_activity", lambda c, aid, name: calls.append(("name", aid, name)))
    monkeypatch.setattr(garmin, "set_description", lambda c, aid, text: calls.append(("description", aid, text)))
    db.mark_synced("rest-1", "55", "Push", action="updated")
    try:
        r = client.post("/api/workouts/rest-1/restore",
                        headers={"HX-Request": "true", "HX-Current-URL": "http://x/liftosaur/workouts"})
        assert r.headers["HX-Redirect"] == "/liftosaur/workouts?saved=restored#workout-rest-1"
        assert calls == [("sets", 55, [{"setType": "ACTIVE", "repetitionCount": 9}]), ("name", 55, "Strength"),
                         ("description", 55, "from the watch")]
        assert not db.is_synced("rest-1") and originals.load(55) is None
    finally:
        db.unsync("rest-1")
        originals.forget(55)


def test_restore_without_a_saved_original_is_a_notice(client, monkeypatch):
    db.mark_synced("rest-2", "56", "Push", action="updated")
    try:
        r = client.post("/api/workouts/rest-2/restore", headers={"HX-Request": "true"})
        assert "No copy of the watch activity" in json.loads(r.headers["HX-Trigger"])["notify"]["message"]
        assert db.is_synced("rest-2")
    finally:
        db.unsync("rest-2")


def test_workout_cards_have_the_four_buttons_and_filters(client, monkeypatch):
    monkeypatch.setattr(server, "load_config", lambda: {"liftosaur_api_key": "", "user_profile": {}, "timing": {}})
    workouts = [
        {"id": "f-1", "title": "Push", "exercises": [], "start_time": "2026-11-20T10:00:00Z"},
        {"id": "f-2", "title": "Pull", "exercises": [], "start_time": "2026-11-19T10:00:00Z"}]
    _db = db.get_db()
    _db.set_app_config("workouts_page_1", {"workouts": workouts, "page_count": 1})
    _db.set_app_config(server.ALL_WORKOUTS_KEY, {"at": datetime.now(timezone.utc).isoformat(), "workouts": workouts})
    db.mark_synced("f-1", "77", "Push", action="uploaded")
    try:
        page = client.get("/liftosaur/workouts").text
        assert page.count('class="card hist-card src-liftosaur"') == 2
        assert "connect.garmin.com/modern/activity/77" in page and "/api/workouts/f-1/restore" in page
        assert "/api/workouts/f-2/fit" in page and 'hx-post="/api/sync/f-2"' in page
        assert "opacity: 0.65" not in page  # synced cards are no longer faded
        pending = client.get("/liftosaur/workouts?status=pending").text
        assert "Pull" in pending and "/api/workouts/f-1/" not in pending
        synced = client.get("/liftosaur/workouts?status=synced").text
        assert "/api/workouts/f-1/restore" in synced and "/api/workouts/f-2/" not in synced
    finally:
        db.unsync("f-1")
        _db.set_app_config("workouts_page_1", {})
        _db.set_app_config(server.ALL_WORKOUTS_KEY, {})


def test_calorie_estimate_only_for_workouts_not_written_into_a_watch_activity(client, monkeypatch):
    monkeypatch.setattr(server, "load_config", lambda: {"liftosaur_api_key": "", "user_profile": {}, "timing": {}})
    times = {"start_time": "2026-11-20T10:00:00Z", "end_time": "2026-11-20T10:40:00Z"}
    _db = db.get_db()
    _db.set_app_config("workouts_page_1", {"workouts": [
        {"id": "cal-1", "title": "Watch", "exercises": [], **times},
        {"id": "cal-2", "title": "Upload", "exercises": [], **times}], "page_count": 1})
    db.mark_synced("cal-1", "81", "Watch", action="updated")
    db.mark_synced("cal-2", "82", "Upload", action="uploaded")
    try:
        page = client.get("/liftosaur/workouts").text
        watch, upload = page.split('id="workout-cal-1"')[1].split('id="workout-cal-2"')
        assert "Calories:" not in watch  # the watch measured them
        assert "Calories:" in upload
    finally:
        db.unsync("cal-1"), db.unsync("cal-2")
        _db.set_app_config("workouts_page_1", {})


def test_liftosaur_check_now_reads_liftosaur_again(client, monkeypatch):
    """New workouts show up as not synced: Check now forgets the saved lists, and a saved page
    older than a few minutes is read again (the saved one is kept when Liftosaur fails)."""
    _db = db.get_db()
    _db.set_app_config("workouts_page_1", {"workouts": [{"id": "old", "title": "Old", "exercises": []}], "page_count": 1})
    _db.set_app_config("workout_total", {"count": 1})
    r = client.post("/api/liftosaur/check", headers={"HX-Request": "true"})
    assert r.headers["HX-Refresh"] == "true"
    assert not _db.get_app_config("workouts_page_1") and not _db.get_app_config("workout_total")

    class FakeLiftosaur:
        calls = 0

        def __init__(self, *a, **kw):
            pass

        def get_workouts(self, page=1, page_size=10):
            FakeLiftosaur.calls += 1
            return {"workouts": [{"id": "new", "title": "New one", "exercises": []}], "page_count": 1}

        def get_workout_count(self):
            return 1

    import garmin_sync_hub.liftosaur.liftosaur as liftosaur
    monkeypatch.setattr(liftosaur, "LiftosaurClient", FakeLiftosaur)
    monkeypatch.setattr(server, "load_config", lambda: {"liftosaur_api_key": "k", "user_profile": {}, "timing": {}})
    try:
        assert "New one" in client.get("/liftosaur/workouts").text
        assert "New one" in client.get("/liftosaur/workouts").text and FakeLiftosaur.calls == 1  # recent: saved page
    finally:
        _db.set_app_config("workouts_page_1", {})
        _db.set_app_config("workout_total", {})


def test_workout_fit_download(client, monkeypatch):
    workout = {"id": "fit-1", "title": "Push", "start_time": "2026-11-20T10:00:00+00:00",
               "end_time": "2026-11-20T10:30:00+00:00",
               "exercises": [{"title": "Bench Press", "sets": [{"type": "normal", "reps": 5, "weight_kg": 60}]}]}
    _db = db.get_db()
    _db.set_app_config("workouts_page_1", {"workouts": [workout], "page_count": 1})
    try:
        r = client.get("/api/workouts/fit-1/fit")
        assert r.status_code == 200 and len(r.content) > 100 and b".FIT" in r.content[:14]
        assert r.headers["content-disposition"] == 'attachment; filename="2026-11-20_Push.fit"'
    finally:
        _db.set_app_config("workouts_page_1", {})


def test_generic_mapping_writes_the_category_only(tmp_path, monkeypatch):
    """Sub-id 65535 = no specific exercise: Garmin shows the category ("Shoulder Press")."""
    from fit_tool.fit_file import FitFile
    from fit_tool.profile.messages.exercise_title_message import ExerciseTitleMessage
    from fit_tool.profile.messages.set_message import SetMessage

    from garmin_sync_hub.liftosaur import fit as fit_mod
    from garmin_sync_hub.liftosaur.mapper import GENERIC_SUB, fit_exercise_name
    from garmin_sync_hub.liftosaur.sync import _garmin_exercise_label

    table = {"Landmine Shoulder Press": (24, GENERIC_SUB), "Arnold": (24, 1)}
    monkeypatch.setattr(fit_mod, "lookup_exercise", lambda name: (*table[name], name))
    workout = {"id": "g1", "title": "Push", "start_time": "2026-11-20T10:00:00Z", "end_time": "2026-11-20T10:30:00Z",
               "exercises": [{"title": "Landmine Shoulder Press", "sets": [{"type": "normal", "reps": 8, "weight_kg": 20}]},
                             {"title": "Arnold", "sets": [{"type": "normal", "reps": 10, "weight_kg": 12}]}]}
    path = tmp_path / "w.fit"
    fit_mod.generate_fit(workout, hr_samples=None, output_path=str(path))
    messages = [r.message for r in FitFile.from_file(str(path)).records]
    titles = [m for m in messages if isinstance(m, ExerciseTitleMessage)]
    sets = [m for m in messages if isinstance(m, SetMessage) and m.category]
    assert titles[0].exercise_category == 24 and titles[0].exercise_name is None
    assert titles[1].exercise_name == 1
    assert sets[0].category == [24] and not sets[0].category_subtype
    assert sets[1].category_subtype == [1]
    # same meaning for activities the watch recorded, and in the mappings list
    assert _garmin_exercise_label(24, GENERIC_SUB) == {"probability": 100.0, "category": "SHOULDER_PRESS", "name": None}
    assert fit_exercise_name(24, GENERIC_SUB) is None


def test_mapping_form_offers_generic_and_refuses_unknown_subs(client, monkeypatch):
    from garmin_sync_hub.liftosaur import mapper

    names = client.get("/api/garmin-exercise-names/24").json()
    assert names[0] == {"sub": 65535, "name": "Generic (category only)"}
    saved = []
    monkeypatch.setattr(mapper, "save_custom_mapping", lambda *a: saved.append(a))
    r = client.post("/api/mapping", data={"exercise_name": "Landmine Shoulder Press", "category": "24", "subcategory": "26"})
    assert "not a Garmin exercise of this category" in r.text and saved == []
    r = client.post("/api/mapping", data={"exercise_name": "Landmine Shoulder Press", "category": "24", "subcategory": "65535"})
    assert "Shoulder Press (generic)" in r.text and saved == [("Landmine Shoulder Press", 24, 65535)]
