"""Garmin account backup: layout, incremental runs, rate limits, settings and pages."""
import io
import json
import threading
import zipfile
from datetime import date

import pytest
from garminconnect import GarminConnectNotFoundError, GarminConnectTooManyRequestsError

from garmin_sync_hub.backup import engine
from garmin_sync_hub.backup.engine import Backup, RateLimited, slug
from garmin_sync_hub.backup.settings import BackupSettings

TODAY = date(2026, 11, 22)


def fit_zip(payload: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("123_ACTIVITY.fit", payload)
    return buf.getvalue()


class FakeGarmin:
    """Answers every get_* call; records them all."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.rate_limited = 0
        self.activities = [
            {"activityId": 2, "activityName": "Morning Run", "startTimeLocal": "2026-11-20 07:12:00",
             "activityType": {"typeId": 1, "typeKey": "running"}, "distance": 10000, "duration": 3000},
            {"activityId": 1, "activityName": "Push / Pull", "startTimeLocal": "2026-11-19 18:00:00",
             "activityType": {"typeId": 13, "typeKey": "strength_training"}, "duration": 3600},
            {"activityId": 3, "activityName": "Manual swim", "startTimeLocal": "2026-11-18 12:00:00",
             "activityType": {"typeId": 26, "typeKey": "lap_swimming"}},
        ]

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if self.rate_limited:
                self.rate_limited -= 1
                raise GarminConnectTooManyRequestsError("429")
            return {"from": name, "args": [str(a) for a in args]}
        return call

    def _log(self, name, *args):
        self.calls.append((name, args, {}))

    def get_activities(self, start, limit):
        self._log("get_activities", start, limit)
        return self.activities[start:start + limit]

    def get_activity_types(self):
        self._log("get_activity_types")
        return [{"typeId": 17, "typeKey": "all", "parentTypeId": None},
                {"typeId": 1, "typeKey": "running", "parentTypeId": 17},
                {"typeId": 13, "typeKey": "strength_training", "parentTypeId": 29},
                {"typeId": 29, "typeKey": "fitness_equipment", "parentTypeId": 17},
                {"typeId": 26, "typeKey": "lap_swimming", "parentTypeId": 27},
                {"typeId": 27, "typeKey": "swimming", "parentTypeId": 17}]

    def download_activity(self, activity_id, dl_fmt=None):
        self._log("download_activity", activity_id)
        if activity_id == "3":
            raise GarminConnectNotFoundError("no file")
        return fit_zip(b"FIT-" + activity_id.encode())

    def get_workouts(self, start, limit):
        self._log("get_workouts", start, limit)
        return [{"workoutId": 7, "workoutName": "Intervals 5x1k"}] if start == 0 else []

    def download_workout(self, workout_id):
        self._log("download_workout", workout_id)
        return b"WORKOUT-FIT"

    def get_courses(self):
        self._log("get_courses")
        return [{"courseId": 9, "courseName": "Lake loop"}]

    def download_course(self, course_id, fmt="gpx"):
        self._log("download_course", course_id, fmt)
        return f"COURSE-{fmt}".encode()

    def get_gear_list(self, start=0, limit=20, gear_statuses=None):
        self._log("get_gear_list", tuple(gear_statuses or ()))
        return [{"uuid": "abcd-1234", "displayName": "Pegasus 41"},
                {"uuid": "ef01-5678", "displayName": "Old shoes", "gearStatusName": "retired"}]

    def get_devices(self):
        self._log("get_devices")
        return [{"deviceId": 555, "productDisplayName": "Forerunner 965"}]

    def get_user_profile(self):
        self._log("get_user_profile")
        return {"id": 4242}


def run(tmp_path, g, **kw):
    b = Backup(lambda: g, tmp_path, pause_s=0, today=TODAY, **kw)
    return b, b.run()


def names(g, name):
    return [c for c in g.calls if c[0] == name]


def test_first_run_layout(tmp_path):
    g = FakeGarmin()
    _, p = run(tmp_path, g, health_since=date(2026, 11, 19))
    run_fit = tmp_path / "activities/running/2026/2026-11-20_0712_Morning-Run_2.fit"
    assert run_fit.read_bytes() == b"FIT-2"
    strength = tmp_path / "activities/fitness_equipment/2026/2026-11-19_1800_Push-Pull_1.json"
    record = json.loads(strength.read_text())
    assert record["summary"]["activityName"] == "Push / Pull"
    assert record["details"]["from"] == "get_activity" and record["exercise_sets"]["from"] == "get_activity_exercise_sets"
    assert "exercise_sets" not in json.loads(run_fit.with_suffix(".json").read_text())
    manual = list((tmp_path / "activities/swimming/2026").glob("*_3.*"))
    assert [m.suffix for m in manual] == [".json"]  # entered by hand: summary only
    index = (tmp_path / "activities/index.csv").read_text().splitlines()
    assert len(index) == 4 and index[1].startswith("2,2026-11-20 07:12:00,running,running,Morning Run,10.0,50.0")

    assert (tmp_path / "workouts/Intervals-5x1k_7.fit").read_bytes() == b"WORKOUT-FIT"
    assert (tmp_path / "courses/Lake-loop_9.gpx").read_bytes() == b"COURSE-gpx"
    assert (tmp_path / "courses/Lake-loop_9.fit").exists()
    assert json.loads((tmp_path / "account/personal_records.json").read_text())["from"] == "get_personal_record"
    assert (tmp_path / "account/devices/Forerunner-965_555.json").exists()

    assert names(g, "get_gear_list")[0][1] == (("ACTIVE", "RETIRED"),)
    assert (tmp_path / "gear/Old-shoes_ef015678/activities.json").exists()
    assert json.loads((tmp_path / "gear/defaults.json").read_text())["args"] == ["4242"]

    # health: 19, 20 and 21 November (today is not over), every metric
    for metric in engine.HEALTH_METRICS:
        assert sorted(f.stem for f in (tmp_path / "health" / metric / "2026").glob("*.json")) == [
            "2026-11-19", "2026-11-20", "2026-11-21"]
    assert (tmp_path / "health/daily_summary.csv").read_text().count("\n") == 4
    assert (tmp_path / "history/calendar/2026.json").exists()
    assert (tmp_path / "history/body_composition/2026.json").exists()
    assert (tmp_path / "README.txt").exists()
    assert not list(tmp_path.rglob("*.part"))
    assert p.counts["activities"] == 3 and p.counts["activities_new"] == 3 and p.counts["gear"] == 2


def test_second_run_only_adds_what_is_new(tmp_path):
    g = FakeGarmin()
    run(tmp_path, g, health_since=date(2026, 11, 10))
    g2 = FakeGarmin()
    g2.activities.insert(0, {"activityId": 4, "activityName": "Ride", "startTimeLocal": "2026-11-21 17:00:00",
                             "activityType": {"typeId": 1, "typeKey": "running"}})
    _, p = run(tmp_path, g2, health_since=date(2026, 11, 10))
    assert [c[1][0] for c in names(g2, "download_activity")] == ["4"]
    assert p.counts["activities_new"] == 1 and p.counts["activities"] == 4
    # only the last two days of health data are fetched again
    days = {c[1][0] for c in names(g2, "get_sleep_data")}
    assert days == {"2026-11-20", "2026-11-21"}
    assert names(g2, "download_workout") == []  # workouts already saved
    assert {c[1][0] for c in names(g2, "get_calendar_year")} == {2026}  # this year again only


def test_health_can_be_turned_off(tmp_path):
    g = FakeGarmin()
    run(tmp_path, g, health=False)
    assert not (tmp_path / "health").exists()
    assert names(g, "get_sleep_data") == [] and names(g, "get_body_composition") == []
    assert (tmp_path / "history/calendar/2026.json").exists()


def test_rate_limit_is_waited_out_then_stops_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "RATE_LIMIT_WAITS", (0, 0))
    g = FakeGarmin()
    g.rate_limited = 2  # two 429s, then fine
    run(tmp_path, g, health=False)
    assert (tmp_path / "account/user_statistics.json").exists()
    g = FakeGarmin()
    g.rate_limited = 3  # keeps refusing
    with pytest.raises(RateLimited):
        run(tmp_path / "b", g, health=False)


def test_stop_interrupts(tmp_path):
    stop = threading.Event()
    stop.set()
    with pytest.raises(InterruptedError):
        Backup(lambda: FakeGarmin(), tmp_path, pause_s=0, stop=stop).run()


def test_slug():
    assert slug("Push / Pull: day 1") == "Push-Pull-day-1"
    assert slug("") == "activity"
    assert slug("Course à pied") == "Course-à-pied"


def test_settings_validation(tmp_path, monkeypatch):
    from garmin_sync_hub.backup import settings as bs

    monkeypatch.setattr(bs, "data_dir", lambda: tmp_path)
    s = BackupSettings.load()
    assert (s.auto, s.interval_minutes, s.health) == (False, 1440, True)
    s.update({"auto": "true", "interval_minutes": "720", "rclone_remote": "gdrive:Garmin", "health_since": "2020-01-01"})
    s.save()
    assert BackupSettings.load().rclone_remote == "gdrive:Garmin"
    for bad in ({"interval_minutes": "5"}, {"rclone_remote": "no-colon"}, {"rclone_remote": "--config:x"},
                {"health_since": "2999-01-01"}, {"health_since": "yesterday"}):
        with pytest.raises(ValueError):
            s.update(bad)
    assert s.interval_minutes == 720


@pytest.fixture
def hub(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from garmin_sync_hub import server
    from garmin_sync_hub.backup import settings as bs
    from garmin_sync_hub.backup import web as bw

    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.setattr(bs, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(bw, "data_dir", lambda: tmp_path)
    m = server.backup_module
    m.settings = BackupSettings()
    m.state, m.progress = {}, None
    m.export = {"running": False, "error": None}
    m._stop, m._cancel = threading.Event(), threading.Event()  # an earlier app shutdown set _stop
    monkeypatch.setattr(m, "start", lambda: None)
    monkeypatch.setattr(server, "_has_garmin_tokens", lambda config=None: True)
    monkeypatch.setattr(server.zwift_module, "start", lambda: None)
    server._is_configured_cache = None
    with TestClient(server.app) as c:
        yield c, m


def test_backup_page_and_overview(hub, monkeypatch):
    c, m = hub
    monkeypatch.setattr(m.garmin, "has_tokens", lambda: True)
    page = c.get("/backup").text
    assert "No backup yet" in page and "Back up now" in page and 'id="bk-autosync-toggle"' in page
    assert "What is saved" in page and "gear/" in page
    assert 'href="/backup" class="active"' in page
    overview = c.get("/").text
    assert "src-backup" in overview and "Back up now" in overview


def test_run_writes_state_and_counts(hub, monkeypatch):
    c, m = hub
    g = FakeGarmin()
    monkeypatch.setattr(m.garmin, "client", lambda: g)
    monkeypatch.setattr(engine, "PAUSE_S", 0)
    m.settings.health = False
    assert m.run() is True
    s = m.summary()
    assert s["error"] is None and s["last_ok"] and s["counts"]["activities"] == 3 and s["counts"]["size"] > 0
    assert json.loads((m._state_path()).read_text())["counts"]["activities"] == 3
    page = c.get("/backup").text
    assert "3 activities saved" in page


def test_run_without_garmin_is_refused(hub, monkeypatch):
    c, m = hub
    monkeypatch.setattr(m.garmin, "has_tokens", lambda: False)
    r = c.post("/backup/run", headers={"HX-Request": "true"})
    assert r.status_code == 409 and "Connect Garmin" in r.text


def test_autosync_and_settings(hub):
    c, m = hub
    r = c.post("/backup/autosync", data={"enabled": "true", "interval": "720"})
    assert r.status_code == 200 and "Next:" in r.text
    assert m.settings.auto is True and m.settings.interval_minutes == 720
    r = c.post("/backup/settings", data={"health": "off", "health_since": "", "rclone_remote": "gdrive:Garmin"},
               follow_redirects=False)
    assert r.headers["location"] == "/settings?saved=backup#backup"
    assert m.settings.health is False and m.settings.rclone_remote == "gdrive:Garmin"
    r = c.post("/backup/settings", data={"rclone_remote": "nope"}, follow_redirects=False)
    assert "error=Backup+settings+not+saved" in r.headers["location"]
    settings_page = c.get("/settings").text
    assert 'data-tab-button="backup"' in settings_page and 'form="backup-settings"' in settings_page


def test_rclone_test_without_rclone(hub, monkeypatch):
    c, _ = hub
    monkeypatch.setattr("shutil.which", lambda name: None)
    r = c.post("/backup/rclone/test", data={"remote": "gdrive:Garmin"})
    assert "rclone is not installed" in r.text


def test_zip_export_and_download(hub):
    c, m = hub
    (m.root / "activities").mkdir(parents=True)
    (m.root / "activities" / "a.fit").write_bytes(b"FIT")
    (m.root / "half.part").write_bytes(b"x")
    m.build_export()
    latest = m.latest_export()
    assert latest and latest.name.startswith("garmin-backup-")
    with zipfile.ZipFile(latest) as z:
        assert z.namelist() == ["garmin-backup/activities/a.fit"]
    r = c.get("/backup/export/download")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"


def test_refused_sign_in_stops_at_once(tmp_path):
    from garminconnect import GarminConnectAuthenticationError

    from garmin_sync_hub.backup.engine import SignInRefused

    class Refusing(FakeGarmin):
        def get_user_profile(self):
            raise GarminConnectAuthenticationError("Username and password are required")

    g = Refusing()
    with pytest.raises(SignInRefused, match="reconnect in Settings"):
        run(tmp_path, g, health=False)
    assert names(g, "get_activities") == []  # nothing else was tried


def lsd(*names):
    return "".join(f"          -1 2026-11-22 10:00:00        -1 {n}\n" for n in names)


def fake_rclone(config: str, folders: dict[str, str]):
    def run(args):
        if args[:2] == ["config", "show"]:
            return (bool(config), config or "Couldn't find remote")
        if args[0] == "lsd":
            return (args[1] in folders, folders.get(args[1], "directory not found"))
        raise AssertionError(args)
    return run


DRIVE_FULL = "[gdrive]\ntype = drive\nscope = drive\ntoken = {\"secret\": 1}\n"
DRIVE_FILE = "[gdrive]\ntype = drive\nscope = drive.file\ntoken = {}\n"


def test_rclone_check_existing_folder():
    from garmin_sync_hub.backup.rclone_check import check_destination

    checks = check_destination("gdrive:Backups/Garmin", fake_rclone(DRIVE_FULL, {
        "gdrive:": lsd("Backups", "My Photos"), "gdrive:Backups": lsd("Garmin", "Phone")}))
    assert checks[0] == ("ok", "gdrive answers. Folders at gdrive:: Backups, My Photos.")
    assert checks[-1] == ("ok", "Backups go into the existing folder gdrive:Backups/Garmin.")
    assert not any(level == "warn" for level, _ in checks)
    assert "secret" not in repr(checks)


def test_rclone_check_limited_scope_and_duplicates():
    from garmin_sync_hub.backup.rclone_check import check_destination

    checks = check_destination("gdrive:Backups", fake_rclone(DRIVE_FILE, {"gdrive:": lsd("Backups", "Backups")}))
    levels = [level for level, _ in checks]
    assert levels.count("warn") == 2
    assert "only sees the folders it created" in checks[1][1]
    assert "2 folders are named “Backups” in gdrive:" in checks[-1][1]

    checks = check_destination("gdrive:New/Sub", fake_rclone(DRIVE_FULL, {"gdrive:": lsd("Backups")}))
    assert checks[-1] == ("info", "“New” does not exist in gdrive: yet: it is created at the first copy.")


def test_rclone_check_unknown_remote():
    from garmin_sync_hub.backup.rclone_check import check_destination

    assert check_destination("nas:x", fake_rclone("", {}))[0][0] == "fail"


def test_rclone_test_button_shows_the_checks(hub, monkeypatch):
    c, m = hub
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/rclone")
    run = fake_rclone(DRIVE_FILE, {"gdrive:": lsd("Backups")})
    monkeypatch.setattr(m, "rclone", lambda args, timeout=None, full=False: run(args))
    r = c.post("/backup/rclone/test", data={"remote": "gdrive:Backups"})
    assert "state-warning" in r.text and "only sees the folders it created" in r.text
    assert "Folders at gdrive:: Backups" in r.text


def test_backup_runs_show_in_the_activity_feed():
    from garmin_sync_hub import activity

    runs = [{"at": "2026-11-23T03:00:00+00:00", "ok": False, "stopped": False, "error": "Garmin refused the sign-in",
             "retry_at": "2026-11-23T03:30:00+00:00"},
            {"at": "2026-11-22T03:00:00+00:00", "ok": True, "activities_new": 2, "health_days_new": 1, "warnings": 0},
            {"at": "2026-11-21T03:00:00+00:00", "ok": True},
            {"at": "2026-11-20T03:00:00+00:00", "ok": False, "stopped": True, "error": "Stopped."}]
    failed, saved, same, stopped = activity.backup_events(runs)
    assert (failed.state, failed.result, failed.retry_at.minute) == ("failed", "Failed", 30)
    assert saved.result == "2 activities, 1 day of health data saved" and saved.link == "/backup"
    assert same.result == "Up to date" and stopped.state == "muted"
    feed = activity.build_feed([], [], [], [], activity.backup_events(runs))
    assert activity.counts(feed)["backup"] == 4 and activity.counts(feed)["attention"] == 1


def test_failed_backup_is_retried_sooner(hub, monkeypatch):
    from datetime import datetime, timedelta, timezone

    _, m = hub
    m.settings.auto, m.settings.interval_minutes = True, 1440
    monkeypatch.setattr(m.garmin, "client", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    m.run()
    assert m.state["failures"] == 1 and m.state["runs"][0]["ok"] is False
    last = datetime.fromisoformat(m.state["last_finished"])
    assert m._due() - last == timedelta(minutes=30)  # not 24 h
    m.state.update(failures=0, retry_at=None, error=None)
    assert m._due() - last == timedelta(hours=24)
