"""Tests for Garmin upload module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from garmin_sync_hub.garmin import find_activity_by_start_time, generate_description


class TestFindActivityByStartTime:
    def _make_activities(self, *start_times: str) -> list[dict]:
        return [
            {"activityId": i + 1, "startTimeLocal": t}
            for i, t in enumerate(start_times)
        ]

    def test_exact_match(self) -> None:
        client = MagicMock()
        acts = self._make_activities("2026-04-01 20:00:00")
        with patch("garmin_sync_hub.garmin._limiter") as mock_limiter:
            mock_limiter.call.return_value = acts
            result = find_activity_by_start_time(client, "2026-04-01T20:00:00+00:00")
            assert result == 1

    def test_within_window(self) -> None:
        client = MagicMock()
        acts = self._make_activities("2026-04-01 20:05:00")
        with patch("garmin_sync_hub.garmin._limiter") as mock_limiter:
            mock_limiter.call.return_value = acts
            result = find_activity_by_start_time(client, "2026-04-01T20:00:00+00:00", window_minutes=10)
            assert result == 1

    def test_outside_window(self) -> None:
        client = MagicMock()
        acts = self._make_activities("2026-04-01 21:00:00")
        with patch("garmin_sync_hub.garmin._limiter") as mock_limiter:
            mock_limiter.call.return_value = acts
            result = find_activity_by_start_time(client, "2026-04-01T20:00:00+00:00", window_minutes=10)
            assert result is None

    def test_no_activities(self) -> None:
        client = MagicMock()
        with patch("garmin_sync_hub.garmin._limiter") as mock_limiter:
            mock_limiter.call.return_value = []
            result = find_activity_by_start_time(client, "2026-04-01T20:00:00+00:00")
            assert result is None

    def test_picks_closest(self) -> None:
        client = MagicMock()
        acts = self._make_activities("2026-04-01 21:00:00", "2026-04-01 20:02:00")
        with patch("garmin_sync_hub.garmin._limiter") as mock_limiter:
            mock_limiter.call.return_value = acts
            result = find_activity_by_start_time(client, "2026-04-01T20:00:00+00:00", window_minutes=10)
            assert result == 2  # the 20:02 one

    def test_invalid_target_time(self) -> None:
        client = MagicMock()
        result = find_activity_by_start_time(client, "not-a-date")
        assert result is None

    def test_api_error_returns_none(self) -> None:
        client = MagicMock()
        with patch("garmin_sync_hub.garmin._limiter") as mock_limiter:
            mock_limiter.call.side_effect = Exception("API error")
            result = find_activity_by_start_time(client, "2026-04-01T20:00:00+00:00")
            assert result is None


class TestGenerateDescription:
    def test_basic_description(self, sample_workout: dict) -> None:
        desc = generate_description(sample_workout, calories=200, avg_hr=95)
        assert "🏋️ Push" in desc
        assert "200 kcal" in desc
        assert "avg 95 bpm" in desc
        assert "garmin-sync-hub" in desc

    def test_includes_exercises(self, sample_workout: dict) -> None:
        desc = generate_description(sample_workout)
        assert "Bench Press" in desc
        assert "Shoulder Press" in desc

    def test_shows_sets_and_weight(self, sample_workout: dict) -> None:
        desc = generate_description(sample_workout)
        assert "3 sets" in desc  # 3 normal bench sets
        assert "60.0kg" in desc

    def test_no_calories(self, sample_workout: dict) -> None:
        desc = generate_description(sample_workout, calories=None, avg_hr=None)
        assert "kcal" not in desc
        assert "bpm" not in desc

    def test_duration(self, sample_workout: dict) -> None:
        desc = generate_description(sample_workout)
        assert "45 min" in desc

    def test_empty_workout(self) -> None:
        workout = {"title": "Empty", "exercises": []}
        desc = generate_description(workout)
        assert "Empty" in desc


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def request(self, method, domain, path, **kwargs):
        self.calls.append((method, domain, path, kwargs))
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"{}"
        resp.json.return_value = {"ok": True}
        return resp


class FakeGarminConnect:
    display_name = "jeff"
    full_name = "Jeff"

    def __init__(self) -> None:
        self.client = FakeTransport()
        self.uploaded: list[str] = []

    def upload_activity(self, path):
        self.uploaded.append(path)
        resp = MagicMock()
        resp.status_code = 201
        resp.content = b"{}"
        resp.json.return_value = {"detailedImportResult": {"successes": [{"internalId": 42}]}}
        return resp

    def get_activities_by_date(self, start, end, activitytype=None):
        return [{"activityId": 1}, {"activityId": 2}, {"activityId": 3}]

    def get_activity_exercise_sets(self, activity_id):
        return {"exerciseSets": [{"setType": "ACTIVE"}]}

    def set_activity_name(self, activity_id, title):
        return None


class TestGarminClientAdapter:
    def test_upload_returns_decoded_json(self, tmp_path) -> None:
        from garmin_sync_hub.garmin import GarminClient

        fit = tmp_path / "w.fit"
        fit.write_bytes(b".FIT")
        g = FakeGarminConnect()
        result = GarminClient(g).upload_activity(str(fit))
        assert result["detailedImportResult"]["successes"][0]["internalId"] == 42
        assert g.uploaded == [str(fit)]

    def test_upload_rejects_non_fit(self, tmp_path) -> None:
        from garmin_sync_hub.garmin import GarminClient, GarminConnectInvalidFileFormatError

        gpx = tmp_path / "w.gpx"
        gpx.write_text("x")
        with pytest.raises(GarminConnectInvalidFileFormatError):
            GarminClient(FakeGarminConnect()).upload_activity(str(gpx))

    def test_put_exercise_sets_uses_activity_endpoint(self) -> None:
        from garmin_sync_hub.garmin import GarminClient

        g = FakeGarminConnect()
        GarminClient(g).put_exercise_sets(7, [{"a": 1}])
        method, domain, path, kwargs = g.client.calls[0]
        assert (method, domain, path) == ("PUT", "connectapi", "/activity-service/activity/7/exerciseSets")
        assert kwargs["json"] == {"activityId": 7, "exerciseSets": [{"a": 1}]}

    def test_get_activities_by_date_honours_start_and_limit(self) -> None:
        from garmin_sync_hub.garmin import GarminClient

        acts = GarminClient(FakeGarminConnect()).get_activities(1, 1, start_date="2026-04-01")
        assert acts == [{"activityId": 2}]

    def test_get_client_without_tokens_raises(self, tmp_path, monkeypatch) -> None:
        from garmin_sync_hub.garmin import GarminConnectAuthenticationError, get_client

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        with pytest.raises(GarminConnectAuthenticationError):
            get_client()
