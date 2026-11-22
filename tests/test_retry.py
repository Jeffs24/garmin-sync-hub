"""Automatic retries of failed Liftosaur syncs and Zwift merges."""
from datetime import datetime, timedelta, timezone

from garmin_sync_hub import retry
from garmin_sync_hub.db_sqlite import SQLiteDatabase
from garmin_sync_hub.zwift.store import Store

NOW = datetime(2026, 11, 22, 12, tzinfo=timezone.utc)


def test_backoff_then_stop():
    assert retry.next_retry(1, NOW) == NOW + timedelta(minutes=30)
    assert retry.next_retry(2, NOW) == NOW + timedelta(hours=2)
    assert retry.next_retry(3, NOW) == NOW + timedelta(hours=8)
    assert retry.next_retry(retry.MAX_ATTEMPTS, NOW) is None
    assert retry.parse(retry.to_text(NOW)) == NOW
    assert retry.parse("2026-11-22 12:00:00") == NOW  # SQLite datetime('now') form


def test_liftosaur_failures_count_and_wait(tmp_path):
    db = SQLiteDatabase(tmp_path / "l.db")
    db.record_failure("w1", "Legs", "boom")
    first = db.get_failures()[0]
    assert first["attempts"] == 1 and first["retry_at"]
    assert db.failures_on_hold() == {"w1"}  # retry in 30 min
    later = retry.parse(first["retry_at"]) + timedelta(seconds=1)
    assert db.failures_on_hold(now=later) == set()  # due: auto-sync tries it again
    for _ in range(retry.MAX_ATTEMPTS - 1):
        db.record_failure("w1", "Legs", "boom")
    last = db.get_failures()[0]
    assert last["attempts"] == retry.MAX_ATTEMPTS and last["retry_at"] is None
    assert db.failures_on_hold(now=later + timedelta(days=30)) == {"w1"}  # waits for a click
    db.mark_synced("w1", "42", "Legs", action="uploaded")
    assert db.failures_on_hold() == set()


def test_liftosaur_sync_skips_held_workouts(monkeypatch):
    from garmin_sync_hub.liftosaur import sync as sync_mod

    workouts = [{"id": "held", "title": "A", "exercises": []}, {"id": "free", "title": "B", "exercises": []}]

    class FakeLiftosaur:
        def __init__(self, *a, **kw):
            pass

        def get_workout_count(self):
            return len(workouts)

    tried = []
    monkeypatch.setattr(sync_mod, "LiftosaurClient", FakeLiftosaur)
    monkeypatch.setattr(sync_mod, "fetch_workouts", lambda client, **kw: workouts)
    monkeypatch.setattr(sync_mod, "generate_fit", lambda w, **kw: tried.append(w["id"]) or {
        "exercises": 0, "total_sets": 0, "calories": 0})
    stats = sync_mod.sync(config={"liftosaur_api_key": "k", "user_profile": {}}, limit=10, dry_run=True,
                          skip_ids={"held"})
    assert "held" not in tried and "free" in tried
    assert stats["skipped"] >= 1


def test_zwift_failed_merge_waits_then_is_due(tmp_path):
    store = Store(tmp_path / "z.db")
    store.upsert_detected(1, 2, "Run", "Zwift", "2026-11-20T12:00:00+00:00", 0.97)
    store.mark_failed(1, "Import failed")
    row = store.get(1)
    assert row["status"] == "failed" and row["attempts"] == 1
    assert store.due_retries(NOW.replace(year=2020)) == []
    assert store.due_retries(retry.parse(row["retry_at"]) + timedelta(seconds=1)) == [1]
    store.set_status(1, "running", None)  # a retry in progress keeps the count
    assert store.get(1)["attempts"] == 1
    store.set_status(1, "merged", "ok", new_id=9)
    assert store.get(1)["attempts"] == 0 and store.get(1)["retry_at"] is None


def test_zwift_store_adds_retry_columns_to_old_databases(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE pairs (target_id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL,
        target_name TEXT, source_name TEXT, start_utc TEXT, overlap REAL, status TEXT NOT NULL,
        message TEXT, new_id INTEGER, position_ratio REAL, source_file TEXT, updated_at TEXT NOT NULL)""")
    con.commit()
    con.close()
    store = Store(path)
    store.upsert_detected(1, 2, "Run", "Zwift", "2026-11-20T12:00:00+00:00", 0.97)
    store.mark_failed(1, "boom")
    assert store.get(1)["attempts"] == 1


def test_auto_merge_retries_a_due_failure(tmp_path):
    from test_service import make  # FakeGarmin lives in tests/zwift/test_service.py

    svc, g, store = make(tmp_path, fail_import_merged=True)
    svc.cfg.auto_merge = True
    svc.scan()
    svc.merge_pending()
    assert store.get(1)["status"] == "failed" and store.get(1)["attempts"] == 1
    g.fail_import_merged = False
    svc.merge_pending()  # not due yet
    assert store.get(1)["status"] == "failed"
    with store._lock, store._db:
        store._db.execute("UPDATE pairs SET retry_at='2000-01-01T00:00:00+00:00' WHERE target_id=1")
    svc.merge_pending()
    assert store.get(1)["status"] == "merged" and store.get(1)["attempts"] == 0
