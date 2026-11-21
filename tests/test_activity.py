from datetime import datetime, timezone

from garmin_sync_hub import activity


def test_feed_merges_sources_newest_first_and_filters():
    synced = [
        {"workout_id": "w1", "title": "Push day", "synced_at": "2026-11-20 10:00:00", "action": "updated",
         "garmin_activity_id": "11", "calories": 300, "avg_hr": 120},
        {"workout_id": "w0", "title": "Old", "synced_at": "2026-11-01 10:00:00", "action": None},
    ]
    failures = [{"workout_id": "w2", "title": "Legs", "error": "boom", "failed_at": "2026-11-21 08:00:00"}]
    pairs = [
        {"target_id": 5, "source_id": 6, "target_name": "Run", "status": "merged", "new_id": 99,
         "updated_at": "2026-11-20T12:00:00+00:00", "message": None},
        {"target_id": 7, "source_id": 8, "target_name": "Ride", "status": "detected",
         "updated_at": "2026-11-19T12:00:00+00:00", "message": None},
        {"target_id": 9, "source_id": 10, "target_name": "Skip", "status": "ignored",
         "updated_at": "2026-11-22T12:00:00+00:00", "message": None},
    ]
    events = activity.build_feed(synced, failures, pairs)
    assert [e.title for e in events] == ["Skip", "Legs", "Run", "Push day", "Ride", "Old"]
    assert events[1].when == datetime(2026, 11, 21, 8, tzinfo=timezone.utc)

    push = events[3]
    assert push.result == "Watch activity updated" and push.state == "ok"
    assert push.garmin_url.endswith("/11") and "300 kcal" in push.detail
    assert events[5].result == "Synced"  # synced before the action was recorded
    assert events[2].result == "Map added" and events[2].link == "/zwift/pairs/5/preview"

    all_titles = [e.title for e in activity.filter_events(events, "all")]
    assert "Skip" not in all_titles  # ignored pairs are hidden from "All"
    assert [e.title for e in activity.filter_events(events, "attention")] == ["Legs", "Ride"]
    assert activity.counts(events) == {"all": 5, "liftosaur": 3, "zwift": 3, "attention": 2}


def test_sync_records_action_and_failures(tmp_path):
    from garmin_sync_hub.db_sqlite import SQLiteDatabase

    db = SQLiteDatabase(tmp_path / "l.db")
    db.record_failure("w1", "Legs", "boom")
    assert [f["workout_id"] for f in db.get_failures()] == ["w1"]
    db.mark_synced("w1", "42", "Legs", action="uploaded")
    assert db.get_failures() == []
    assert db.get_all_synced()[0]["action"] == "uploaded"
