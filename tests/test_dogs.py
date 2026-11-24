"""Dogs on activities: rules, tagging without losing metadata, discovery and the page."""
import json
import threading

import pytest

from garmin_sync_hub.dogs.engine import DogTagger
from garmin_sync_hub.dogs.store import DogsStore, Rule

REX, MILO = 9001, 9002


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class FakeClient:
    """garminconnect's client: request() reaches other Garmin hosts with a full URL."""

    domain = "garmin.com"

    def __init__(self, pets=None, error: Exception | None = None):
        self.pets, self.error, self.urls = pets or [], error, []

    def request(self, method, _domain, url, **kwargs):
        self.urls.append((method, url))
        if self.error:
            raise self.error
        return FakeResponse(self.pets)


class FakeGarmin:
    def __init__(self, n_walks: int = 3):
        self.activities = [{"activityId": 100 + i, "activityName": "Dog Walk" if i % 2 == 0 else "Easy Run",
                            "startTimeLocal": f"2026-11-{10 + i:02d} 08:00:00"} for i in range(2 * n_walks)]
        self.metadata = {a["activityId"]: {"lapCount": 3, "favorite": True} for a in self.activities}
        self.puts: list[tuple] = []
        self.gets: list[str] = []
        self.list_calls: list[int] = []

    def get_activities(self, start, limit):
        self.list_calls.append(start)
        return self.activities[start:start + limit]

    def get_activity(self, activity_id):
        self.gets.append(activity_id)
        name = next(a["activityName"] for a in self.activities if a["activityId"] == int(activity_id))
        return {"activityId": int(activity_id), "activityName": name,
                "metadataDTO": dict(self.metadata[int(activity_id)])}

    def update_activity(self, activity_id, fields):
        self.puts.append((activity_id, fields))
        self.metadata[int(activity_id)] = fields["metadataDTO"]


@pytest.fixture
def store(tmp_path):
    s = DogsStore(tmp_path / "dogs.json")
    s.set_dog(REX, "Rex")
    s.add_rule("dog walk", REX)
    return s


def tagger(store, g):
    return DogTagger(lambda: g, store, pause_s=0)


def test_rule_matching():
    assert Rule("Dog Walk", 1).matches("  dog walk ")
    assert not Rule("Dog Walk", 1).matches("Dog Walk with Milo")
    assert Rule("dog", 1, "contains").matches("Morning DOG walk")
    assert Rule("Dog", 1, "starts").matches("Dog park") and not Rule("Dog", 1, "starts").matches("Hot dog")
    assert not Rule("", 1, "contains").matches("anything")


def test_store_validation_and_persistence(store, tmp_path):
    with pytest.raises(ValueError):
        store.add_rule("Dog Walk", REX)  # same rule (case is ignored)
    with pytest.raises(ValueError):
        store.add_rule("Walk", 1234)  # unknown dog
    with pytest.raises(ValueError):
        store.set_dog("abc", "x")
    store.set_dog(MILO, "Milo")
    store.add_rule("Dog Walk", MILO, "contains")
    again = DogsStore(tmp_path / "dogs.json")
    assert again.state.dogs == {str(REX): "Rex", str(MILO): "Milo"}
    assert again.state.dogs_for("Dog Walk") == [REX, MILO]
    again.remove_dog(REX)
    assert [r.dog_id for r in again.state.rules] == [MILO]


def test_apply_tags_matching_activities_and_keeps_metadata(store):
    g = FakeGarmin()
    g.metadata[102]["dogProfileIds"] = [MILO]  # another dog already there
    g.metadata[102]["hasDog"] = True
    t = tagger(store, g)
    t.run(full=True)
    assert sorted(a for a, _ in g.puts) == ["100", "102", "104"]
    body = dict(g.puts)["102"]["metadataDTO"]
    assert body == {"lapCount": 3, "favorite": True, "hasDog": True, "dogProfileIds": [MILO, REX]}
    assert all(a % 2 == 0 for a in map(int, store.state.tagged)) and len(store.state.tagged) == 3
    assert t.p.counts["tagged"] == 3
    assert store.state.recent[0]["dogs"] == [REX] and store.state.last_run

    g.puts.clear(), g.gets.clear()
    tagger(store, g).run(full=True)
    assert g.puts == [] and g.gets == []  # nothing left to do, no activity read


def test_activity_that_already_has_the_dog_is_not_written(store):
    g = FakeGarmin(n_walks=1)
    g.metadata[100].update(hasDog=True, dogProfileIds=[REX])
    t = tagger(store, g)
    t.run(full=True)
    assert g.puts == [] and t.p.counts["already"] == 1 and store.state.tagged["100"] == [REX]


def test_automatic_run_only_reads_the_latest_page(store):
    g = FakeGarmin(n_walks=120)  # 240 activities
    tagger(store, g).run(full=False)
    assert g.list_calls == [0] and len(g.puts) == 50  # walks among the latest 100


def test_too_many_dogs_is_skipped(store):
    g = FakeGarmin(n_walks=1)
    g.metadata[100]["dogProfileIds"] = list(range(1, 11))
    t = tagger(store, g)
    t.run(full=True)
    assert g.puts == [] and "10 dogs at most" in t.p.warnings[0]


def test_preview_lists_candidates_without_reading_activities(store):
    g = FakeGarmin()
    matches = tagger(store, g).preview()
    assert [m["id"] for m in matches] == [100, 102, 104] and matches[0]["dogs"] == [REX]
    assert g.gets == []


def test_discover_from_backup_and_latest_activities(store, tmp_path):
    folder = tmp_path / "backup" / "activities" / "walking" / "2026"
    folder.mkdir(parents=True)
    (folder / "2026-11-01_0800_Dog-Walk_1.json").write_text(json.dumps(
        {"summary": {"activityName": "Old walk"}, "details": {"metadataDTO": {"dogProfileIds": [MILO]}}}))
    g = FakeGarmin(n_walks=1)
    g.metadata[100]["dogProfileIds"] = [REX]
    found = tagger(store, g).discover(tmp_path / "backup")
    assert found == {MILO: "Old walk", REX: "Dog Walk"}


def test_discover_reads_the_dogs_of_the_profile(store, tmp_path):
    g = FakeGarmin(n_walks=1)
    g.client = FakeClient(pets=[
        {"id": 589432, "name": "Torii", "species": "dog", "status": "active"},
        {"id": 7, "name": "Mittens", "species": "cat", "status": "active"},
        {"id": 8, "name": "Old dog", "species": "dog", "status": "inactive"},
    ])
    t = tagger(store, g)
    assert t.discover(tmp_path / "backup") == {589432: ""}
    assert t.dog_names == {589432: "Torii"}
    assert g.client.urls == [("GET", "https://gcx.garmin.com/family-api/profile-service/profile/pets")]
    assert g.gets == []  # activities are not read when the profile has the dogs


def test_discover_falls_back_to_activities_when_the_profile_fails(store, tmp_path):
    g = FakeGarmin(n_walks=1)
    g.client = FakeClient(error=RuntimeError("403"))
    g.metadata[100]["dogProfileIds"] = [REX]
    t = tagger(store, g)
    assert t.discover(tmp_path / "backup") == {REX: "Dog Walk"}
    assert t.dog_names == {}


def test_stop_interrupts(store):
    stop = threading.Event()
    stop.set()
    with pytest.raises(InterruptedError):
        DogTagger(lambda: FakeGarmin(), store, pause_s=0, stop=stop).run(full=True)


@pytest.fixture
def hub(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from garmin_sync_hub import server
    from garmin_sync_hub.backup import engine

    monkeypatch.setattr(engine, "PAUSE_S", 0)
    m = server.dogs_module
    m.store = DogsStore(tmp_path / "dogs.json")
    m.matches, m.job = [], {"kind": None, "progress": None, "result": None, "error": None, "finished": None}
    m._stop, m._cancel = threading.Event(), threading.Event()
    monkeypatch.setattr(m, "start", lambda: None)
    monkeypatch.setattr(m.garmin, "has_tokens", lambda: True)
    monkeypatch.setattr(server, "_has_garmin_tokens", lambda config=None: True)
    monkeypatch.setattr(server.zwift_module, "start", lambda: None)
    monkeypatch.setattr(server.backup_module, "start", lambda: None)
    server._is_configured_cache = None
    with TestClient(server.app) as c:
        yield c, m


def test_restore_removes_only_our_dogs_and_resync_adds_them_back(store):
    from garmin_sync_hub.activity import dogs_events

    g = FakeGarmin(n_walks=1)
    g.metadata[100] = {"lapCount": 3, "hasDog": True, "dogProfileIds": [MILO]}  # Milo was there before
    t = tagger(store, g)
    t.run(full=True)
    assert g.metadata[100]["dogProfileIds"] == [MILO, REX]

    assert t.restore(100) == [REX]
    assert g.metadata[100] == {"lapCount": 3, "hasDog": True, "dogProfileIds": [MILO]}
    assert 100 in store.state.restored and store.state.recent[0]["restored"]
    assert t.candidates(g.activities) == []  # automatic runs leave a restored activity alone
    assert dogs_events(store.state.recent[:1], store.state.dog_name)[0].result == "Rex removed"
    with pytest.raises(ValueError):
        t.restore(100)  # nothing added since the restore

    t.resync(100)
    assert g.metadata[100]["dogProfileIds"] == [MILO, REX] and 100 not in store.state.restored


def test_dogs_page_lists_activities_with_the_same_buttons(hub, monkeypatch):
    c, m = hub
    m.store.state.recent = [
        {"id": 7, "name": "Dog Walk", "start": "2026-11-20T08:00", "dogs": [REX], "at": "2026-11-20T09:00:00+00:00", "ok": True},
        {"id": 8, "name": "Long walk", "start": "2026-11-19T08:00", "dogs": [REX], "at": "2026-11-19T09:00:00+00:00",
         "ok": False, "error": "Garmin said no"},
        {"id": 7, "name": "Dog Walk", "start": "2026-11-20T08:00", "dogs": [REX], "at": "2026-11-18T09:00:00+00:00", "ok": True},
    ]
    try:
        page = c.get("/dogs").text
        assert page.count('class="card hist-card src-dogs"') == 2  # one card per activity
        assert "/dogs/activities/7/restore" in page and "/dogs/activities/8/restore" not in page
        assert "/dogs/activities/8/resync" in page and "/dogs/activities/7/fit" in page
        assert "Garmin said no" in page
        failed = c.get("/dogs?status=failed").text
        assert "Long walk" in failed and "/dogs/activities/7/" not in failed
    finally:
        m.store.state.recent = []


def test_dogs_page_flow(hub, monkeypatch):
    c, m = hub
    page = c.get("/dogs").text
    assert "Find my dogs" in page and "No dog yet" in page and "Settings → Dogs" in page
    settings = c.get("/settings").text
    assert "reads the dogs of your Garmin profile" in settings and 'data-tab-button="dogs"' in settings
    r = c.post("/dogs/dog", data={"dog_id": "77", "name": "Temp", "next": "settings"}, follow_redirects=False)
    assert r.headers["location"] == "/settings?saved=dogs#dogs"
    assert c.post("/dogs/dog/77/delete?next=settings", follow_redirects=False).headers["location"] == "/settings?saved=dogs#dogs"
    assert 'href="/dogs" class="active"' in page

    r = c.post("/dogs/dog", data={"dog_id": str(REX), "name": "Rex"}, follow_redirects=False)
    assert r.headers["location"] == "/dogs?saved=dogs"
    r = c.post("/dogs/rules", data={"name": "Dog Walk", "dog_id": str(REX), "match": "exact"}, follow_redirects=False)
    assert r.headers["location"] == "/dogs?saved=dogs"
    r = c.post("/dogs/rules", data={"name": "", "dog_id": str(REX)}, follow_redirects=False)
    assert "error=" in r.headers["location"]

    g = FakeGarmin()
    monkeypatch.setattr(m.garmin, "client", lambda: g)
    assert m.run_job("preview")
    page = c.get("/dogs").text
    assert "3 activities to update" in page and "Add to 3 activities" in page
    assert m.run_job("apply")
    page = c.get("/dogs").text
    assert "Done: dog added to 3 activities" in page and "3 activities have their dog" in page
    assert "connect.garmin.com/modern/activity/104" in page

    r = c.post("/dogs/autosync", data={"enabled": "true", "interval": "60"})
    assert "Next:" in r.text and m.store.state.auto is True


def test_jobs_need_garmin_and_rules(hub, monkeypatch):
    c, m = hub
    assert c.post("/dogs/run/preview").status_code == 409  # no rule yet
    monkeypatch.setattr(m.garmin, "has_tokens", lambda: False)
    r = c.post("/dogs/run/discover")
    assert r.status_code == 409 and "Connect Garmin" in r.text


def test_short_pages_do_not_end_the_history(store):
    """Garmin may answer with fewer activities than asked: keep going until an empty page."""
    g = FakeGarmin(n_walks=60)  # 120 activities, served 20 at a time
    full = g.get_activities

    def short_pages(start, limit):
        return full(start, min(limit, 20))
    g.get_activities = short_pages
    t = tagger(store, g)
    t.run(full=True)
    assert len(g.puts) == 60  # every walk, the oldest included
    assert g.list_calls[-1] == 120  # stopped on the empty page


def test_preview_reports_what_it_checked(store):
    g = FakeGarmin(n_walks=2)
    g.activities.append({"activityId": 999, "activityName": "Dog walk with Milo", "startTimeLocal": "2025-01-02 08:00:00"})
    g.metadata[999] = {}
    t = tagger(store, g)
    t.preview()
    assert t.scan["checked"] == 5 and t.scan["oldest"] == "2025-01-02" and t.scan["matching_total"] == 2
    assert t.scan["near_misses"] == [("Dog walk with Milo", 1)]


def test_failures_are_logged_and_cleared_once_the_dog_is_added(store):
    g = FakeGarmin(n_walks=1)
    real_put = g.update_activity

    def refuse(activity_id, fields):
        raise RuntimeError("API Error 500")
    g.update_activity = refuse
    tagger(store, g).run(full=True)
    assert [(e["id"], e["ok"], e["error"]) for e in store.state.recent] == [(100, False, "API Error 500")]
    assert "100" not in store.state.tagged  # tried again next time

    g.update_activity = real_put
    tagger(store, g).run(full=True)
    assert [(e["id"], e["ok"]) for e in store.state.recent] == [(100, True)]  # the failure is gone


def test_dog_events_in_the_activity_feed(store):
    from garmin_sync_hub import activity

    log = [{"id": 5, "name": "Dog Walk", "start": "2026-11-20 08:00", "dogs": [REX], "at": "2026-11-21T09:00:00+00:00", "ok": True},
           {"id": 6, "name": "Dog Walk", "start": "2026-11-19 08:00", "dogs": [REX], "at": "2026-11-21T09:01:00+00:00",
            "ok": False, "error": "API Error 500"}]
    events = activity.dogs_events(log, store.state.dog_name)
    ok, failed = events
    assert (ok.source, ok.result, ok.state, ok.garmin_url.endswith("/5"), ok.link) == ("dogs", "Rex added", "ok", True, "/dogs")
    assert (failed.result, failed.state, failed.detail) == ("Failed", "failed", "Rex: API Error 500")
    feed = activity.build_feed([], [], [], events)
    assert activity.counts(feed)["dogs"] == 2 and activity.counts(feed)["attention"] == 1


def test_overview_and_activity_show_dogs(hub):
    c, m = hub
    m.store.set_dog(REX, "Rex")
    m.store.add_rule("Dog Walk", REX)
    m.store.state.recent = [{"id": 5, "name": "Dog Walk", "start": "2026-11-20 08:00", "dogs": [REX],
                             "at": "2026-11-21T09:00:00+00:00", "ok": True}]
    m.store.state.tagged = {"5": [REX]}
    overview = c.get("/").text
    assert "src-dogs" in overview and "1 activity with a dog" in overview and "Rex" in overview
    page = c.get("/activity?source=dogs").text
    assert "Dogs → Garmin" in page and "Rex added" in page and 'href="/activity?source=dogs" class="active"' in page


def test_sync_now_from_the_overview(hub, monkeypatch):
    c, m = hub
    m.store.set_dog(REX, "Rex")
    m.store.add_rule("Dog Walk", REX)
    started = []
    monkeypatch.setattr(m, "start_job", lambda kind: started.append(kind) or True)
    assert 'hx-post="/dogs/run/auto"' in c.get("/").text
    r = c.post("/dogs/run/auto", headers={"HX-Request": "true"})
    assert started == ["auto"] and "Dogs sync started" in r.headers["HX-Trigger"]


def test_scheduled_runs_wait_for_the_retry_of_a_failure(store):
    g = FakeGarmin(n_walks=1)
    real_put = g.update_activity
    g.update_activity = lambda activity_id, fields: (_ for _ in ()).throw(RuntimeError("API Error 500"))
    tagger(store, g).run(full=True)
    failure = store.state.recent[0]
    assert failure["attempts"] == 1 and failure["retry_at"]
    g.update_activity = real_put
    tagger(store, g).run(scheduled=True)  # retry not due yet: left alone
    assert g.puts == []
    tagger(store, g).run()  # a click (Sync now) tries it at once
    assert [a for a, _ in g.puts] == ["100"] and store.state.recent[0]["ok"]


def test_overview_refreshes_while_a_sync_runs(hub, monkeypatch):
    c, m = hub
    page = c.get("/").text
    assert 'id="source-grid"' in page and 'hx-trigger="refresh"' in page
    monkeypatch.setattr(type(m), "running", property(lambda self: True))
    assert 'hx-trigger="refresh, every 3s"' in c.get("/").text
