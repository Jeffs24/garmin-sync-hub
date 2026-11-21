"""Garmin Connect API helpers for the Liftosaur module.

Authentication is shared with the rest of garmin-sync-hub: one login, one
token store, handled by :mod:`garmin_sync_hub.garmin_session` on top of the
python-garminconnect library. :class:`GarminClient` keeps the small API the
Liftosaur sync code was written against.
"""

from __future__ import annotations

import io
import json
import logging
import time
from pathlib import Path
from typing import Any

from garminconnect import (  # noqa: F401 - re-exported for callers
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectInvalidFileFormatError,
    GarminConnectTooManyRequestsError,
)

from garmin_sync_hub.garmin_session import GarminAuthRequired, shared_session

logger = logging.getLogger("garmin_sync_hub")


class GarminNeedsMFA(Exception):
    """Raised when Garmin requires a second factor to complete login."""

    def __init__(self, login_state: Any = None):
        super().__init__("Garmin requires a verification code")
        self.login_state = login_state


class RateLimiter:
    """Small retry helper for Garmin API calls."""

    def __init__(self, delay: float = 1.0, max_retries: int = 0, base_wait: float = 30.0):
        self.delay = delay
        self.max_retries = max_retries
        self.base_wait = base_wait

    def call(self, func, *args, **kwargs):
        attempt = 0
        while True:
            if self.delay and attempt > 0:
                time.sleep(self.delay)
            try:
                return func(*args, **kwargs)
            except GarminConnectTooManyRequestsError:
                if attempt >= self.max_retries:
                    raise
                wait_time = self.base_wait * (attempt + 1)
                logger.warning("Rate limited by Garmin. Retrying in %ss (attempt %d/%d)...",
                               wait_time, attempt + 1, self.max_retries)
                time.sleep(wait_time)
                attempt += 1


_limiter = RateLimiter(delay=1.0, max_retries=3, base_wait=30)


def _json(response: Any) -> Any:
    if response is None or isinstance(response, (dict, list)):
        return response or {}
    if getattr(response, "status_code", 200) == 204 or not getattr(response, "content", b"x"):
        return {}
    return response.json()


class GarminClient:
    """The Garmin endpoints the Liftosaur sync needs, over a garminconnect client."""

    activity_url = "/activity-service/activity"

    def __init__(self, garmin: Any):
        self._g = garmin
        self.display_name = getattr(garmin, "display_name", None)
        self.full_name = getattr(garmin, "full_name", None)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        return _json(self._g.client.request(method, "connectapi", path, api=True, **kwargs))

    def upload_activity(self, activity_path: str) -> Any:
        path = Path(activity_path)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {activity_path}")
        if path.suffix.lower() != ".fit":
            raise GarminConnectInvalidFileFormatError(f"Invalid file format '{path.suffix}'")
        return _json(self._g.upload_activity(str(path)))

    def set_activity_name(self, activity_id: str | int, title: str) -> Any:
        return _json(self._g.set_activity_name(str(activity_id), title))

    def get_activities(self, start: int = 0, limit: int = 20, activitytype: str | None = None,
                       start_date: str | None = None) -> list[Any]:
        if start_date:
            acts = self._g.get_activities_by_date(start_date, start_date, activitytype)
            return acts[start:start + limit]
        return self._g.get_activities(start, limit, activitytype) or []

    def get_activity(self, activity_id: str | int) -> dict[str, Any]:
        return self._g.get_activity(str(activity_id))

    def get_exercise_sets(self, activity_id: str | int) -> dict[str, Any]:
        return self._g.get_activity_exercise_sets(activity_id) or {}

    def put_exercise_sets(self, activity_id: str | int, exercise_sets: list[dict[str, Any]]) -> Any:
        payload = {"activityId": int(activity_id), "exerciseSets": exercise_sets}
        return self._request("PUT", f"{self.activity_url}/{activity_id}/exerciseSets", json=payload)

    def get_heart_rates(self, cdate: str) -> dict[str, Any]:
        return self._g.get_heart_rates(cdate)

    def get_user_profile(self) -> dict[str, Any]:
        return self._g.get_user_profile()

    def delete_activity(self, activity_id: int) -> Any:
        return _json(self._g.delete_activity(str(activity_id)))

    def put_json(self, path: str, payload: dict[str, Any]) -> Any:
        return self._request("PUT", path, json=payload)

    def post_files(self, path: str, files: dict[str, Any]) -> Any:
        return self._request("POST", path, files=files)


def token_file_path(token_dir: str | None = None) -> Path:
    return shared_session().token_file


def has_tokens() -> bool:
    return shared_session().has_tokens()


def get_client(email: str | None = None, password: str | None = None,
               token_dir: str | None = None) -> GarminClient:
    """Client for the shared Garmin session. Connect Garmin in the web UI first."""
    session = shared_session()
    try:
        return GarminClient(session.client())
    except GarminAuthRequired as exc:
        if email and password:
            if session.start_login(email, password):
                raise GarminNeedsMFA() from exc
            return GarminClient(session.client())
        raise GarminConnectAuthenticationError("No Garmin tokens found. Connect Garmin first.") from exc


def start_login(email: str, password: str, token_dir: str | None = None) -> dict[str, Any]:
    """Start a login; raises GarminNeedsMFA when a code is needed."""
    session = shared_session()
    if session.start_login(email, password):
        raise GarminNeedsMFA(session)
    return {"status": "connected", "display_name": getattr(session.client(), "display_name", None)}


def finish_login(mfa_code: str, login_state: Any = None, email: str | None = None,
                 token_dir: str | None = None) -> dict[str, Any]:
    session = shared_session()
    session.submit_mfa(mfa_code)
    return {"status": "connected", "display_name": getattr(session.client(), "display_name", None)}


def export_token_payload(token_dir: str | None = None) -> dict[str, Any]:
    return json.loads(shared_session().token_file.read_text())


def save_token_payload(payload: dict[str, Any], *, token_dir: str | None = None) -> Path:
    """Install a token file exported from another garmin-sync-hub."""
    session = shared_session()
    session.token_dir.mkdir(parents=True, exist_ok=True)
    session.token_file.write_text(json.dumps(payload))
    session.reset()
    return session.token_file


def upload_fit(client: GarminClient, fit_path: str | Path, workout_start: str | None = None) -> dict:
    fit_path = Path(fit_path)
    if not fit_path.exists():
        raise FileNotFoundError(f"FIT file not found: {fit_path}")
    try:
        resp = _limiter.call(client.upload_activity, str(fit_path))
    except Exception as exc:
        response = getattr(exc, "response", None)
        if response is None and exc.__cause__:
            response = getattr(exc.__cause__, "response", None)
        if response is not None:
            body = response.text[:2000] if hasattr(response, "text") else str(response)
            logger.error("Upload rejected - status=%s body=%s", getattr(response, "status_code", "?"), body)
            raise RuntimeError(
                f"Garmin upload failed ({getattr(response, 'status_code', '?')}): {body}"
            ) from exc
        logger.error("Upload failed (no response): %s", str(exc)[:300])
        raise
    upload_id = None
    activity_id = None
    if isinstance(resp, dict):
        detail = resp.get("detailedImportResult", {})
        upload_id = detail.get("uploadId")
        successes = detail.get("successes", [])
        if successes and isinstance(successes, list):
            activity_id = successes[0].get("internalId")
    if not activity_id and workout_start:
        for attempt, wait in enumerate([3, 5, 10], 1):
            time.sleep(wait)
            activity_id = find_activity_by_start_time(client, workout_start)
            if activity_id:
                break
            logger.info("  Activity not found yet (attempt %d/%d), retrying...", attempt, 3)
    return {"upload_id": upload_id, "activity_id": activity_id}


def find_activity_by_start_time(
    client: GarminClient,
    target_start: str,
    window_minutes: int = 30,
) -> int | None:
    from datetime import datetime

    try:
        target = datetime.fromisoformat(target_start.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    try:
        target_date = target.strftime("%Y-%m-%d")
        activities = _limiter.call(client.get_activities, 0, 10, start_date=target_date)
    except Exception:
        return None
    closest_id = None
    closest_delta = None
    for act in activities:
        act_type = act.get("activityType", {}).get("typeKey", "")
        if act_type and act_type not in ("strength_training", "other"):
            continue
        act_start_str = act.get("startTimeGMT") or act.get("startTimeLocal", "")
        try:
            if "T" not in act_start_str:
                act_start_str = act_start_str.replace(" ", "T")
            act_start = datetime.fromisoformat(act_start_str)
            target_naive = target.replace(tzinfo=None) if target.tzinfo else target
            act_naive = act_start.replace(tzinfo=None) if act_start.tzinfo else act_start
            delta = abs((act_naive - target_naive).total_seconds())
            if delta < window_minutes * 60 and (closest_delta is None or delta < closest_delta):
                closest_delta = delta
                closest_id = act.get("activityId")
        except (ValueError, TypeError):
            continue
    return closest_id


def rename_activity(client: GarminClient, activity_id: int, name: str) -> None:
    _limiter.call(client.set_activity_name, activity_id, name)
    logger.info("  Renamed activity %s to '%s'", activity_id, name)


def set_description(client: GarminClient, activity_id: int, description: str) -> None:
    url = f"/activity-service/activity/{activity_id}"
    payload = {"activityId": activity_id, "description": description}
    client.put_json(url, payload)
    time.sleep(1.0)
    logger.info("  Description set (%d chars)", len(description))


def get_exercise_sets(client: GarminClient, activity_id: int) -> list[dict]:
    data = _limiter.call(client.get_exercise_sets, activity_id)
    return data.get("exerciseSets", [])


def update_exercise_sets(client: GarminClient, activity_id: int, exercise_sets: list[dict]) -> None:
    _limiter.call(client.put_exercise_sets, activity_id, exercise_sets)
    time.sleep(1.0)
    logger.info("  Updated exercise sets for activity %s", activity_id)


def upload_image(client: GarminClient, activity_id: int, image_bytes: bytes, filename: str = "image.png") -> None:
    files = {"file": (filename, io.BytesIO(image_bytes))}
    client.post_files(f"/activity-service/activity/{activity_id}/image", files=files)
    time.sleep(1.0)
    logger.info("  Image uploaded (%dKB)", len(image_bytes) // 1024)


def generate_description(workout: dict, calories: int | None = None, avg_hr: int | None = None) -> str:
    lines: list[str] = []
    title = workout.get("title", "Workout")
    duration_s = 0
    start = workout.get("start_time") or workout.get("startTime", "")
    end = workout.get("end_time") or workout.get("endTime", "")
    if start and end:
        try:
            from datetime import datetime

            t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(end.replace("Z", "+00:00"))
            duration_s = int((t1 - t0).total_seconds())
        except Exception:
            pass
    lines.append(f"🏋️ {title}")
    if duration_s > 0:
        lines.append(f"⏱️ {duration_s // 60} min")
    if calories:
        lines.append(f"🔥 {calories} kcal")
    if avg_hr:
        lines.append(f"❤️ avg {avg_hr} bpm")
    exercises = workout.get("exercises", [])
    if exercises:
        lines.append("")
        for ex in exercises:
            name = ex.get("title") or ex.get("name", "Unknown")
            all_sets = ex.get("sets", [])
            normal = [s for s in all_sets if s.get("type") == "normal"]
            warmup = [s for s in all_sets if s.get("type") == "warmup"]
            if normal:
                weights = [s.get("weight_kg") or s.get("weight", 0) for s in normal]
                reps = [s.get("reps", 0) for s in normal]
                lines.append(f"• {name}: {len(normal)} sets · {max(weights):.1f}kg × {max(reps)}")
            elif warmup:
                s_label = "set" if len(warmup) == 1 else "sets"
                lines.append(f"• {name}: {len(warmup)} warmup {s_label}")
    lines.append("\n- synced by garmin-sync-hub")
    return "\n".join(lines)
