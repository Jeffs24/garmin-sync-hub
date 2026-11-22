"""Shared Garmin Connect session (python-garminconnect): one login for every module."""
from __future__ import annotations

import base64
import io
import json
import logging
import threading
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from garminconnect import Garmin

log = logging.getLogger(__name__)


EXPIRY_WARNING_DAYS = 7


def _jwt_expiry(token: Any) -> datetime | None:
    """The ``exp`` claim of a JWT, without checking its signature (display only)."""
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        exp = float(data["exp"])
        return datetime.fromtimestamp(exp, timezone.utc)
    except (ValueError, KeyError, TypeError, OverflowError, OSError):
        return None


class GarminAuthRequired(RuntimeError):
    pass


class DuplicateActivity(RuntimeError):
    """Garmin refused the file because it already has an activity from it."""


class GarminSession:
    """Holds one logged-in client; tokens live in ``token_dir``."""

    def __init__(self, token_dir: Path):
        self.token_dir = token_dir
        self._client: Garmin | None = None
        self._pending: tuple[Garmin, Any] | None = None
        self._lock = threading.Lock()
        # Outcome of the last connection check (see check()).
        self.last_ok: datetime | None = None
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None

    # --- connection health ------------------------------------------------

    def check(self) -> bool:
        """Make one light API call and remember whether it worked."""
        now = datetime.now(timezone.utc)
        if not self.has_tokens():
            return False
        try:
            self.client().get_user_profile()
        except Exception as e:  # noqa: BLE001 - any failure means the connection is not usable
            log.warning("Garmin connection check failed: %s", e)
            self.last_error, self.last_error_at = str(e) or e.__class__.__name__, now
            self.reset()  # reload the token file next time, in case it was renewed
            return False
        self.last_ok = now
        return True

    def token_expiry(self) -> dict[str, datetime | None]:
        """Expiry dates read from the stored tokens (None when unknown)."""
        out: dict[str, datetime | None] = {"access": None, "refresh": None, "saved": None}
        try:
            data = json.loads(self.token_file.read_text())
            out["saved"] = datetime.fromtimestamp(self.token_file.stat().st_mtime, timezone.utc)
        except (OSError, ValueError):
            return out
        if not isinstance(data, dict):
            return out
        for key, field in (("access", "di_token"), ("refresh", "di_refresh_token")):
            out[key] = _jwt_expiry(data.get(field))
        return out

    def health(self, now: datetime | None = None) -> dict[str, Any]:
        """Connection state for the UI: state is ok, warning, error or disconnected."""
        now = now or datetime.now(timezone.utc)
        h: dict[str, Any] = {"state": "disconnected", "message": "Not connected",
                             "last_ok": self.last_ok, "last_error": self.last_error,
                             "last_error_at": self.last_error_at, "refresh_expires": None}
        if not self.has_tokens():
            return h
        expiry = self.token_expiry()
        h["refresh_expires"] = expiry["refresh"]
        if self.last_error_at and (not self.last_ok or self.last_error_at > self.last_ok):
            h.update(state="error", message="Garmin refused the last request. Reconnect Garmin if this persists.")
        elif expiry["refresh"] and expiry["refresh"] <= now:
            h.update(state="error", message="The Garmin sign-in has expired. Reconnect Garmin.")
        elif expiry["refresh"] and expiry["refresh"] - now < timedelta(days=EXPIRY_WARNING_DAYS):
            days = max(1, (expiry["refresh"] - now).days)
            h.update(state="warning", message=f"The Garmin sign-in expires in {days} day{'s' if days > 1 else ''}. Reconnect Garmin to renew it.")
        elif self.last_ok is None:
            h.update(state="ok", message="Connected (not checked yet)")
        else:
            h.update(state="ok", message="Connected")
        return h

    @property
    def token_file(self) -> Path:
        return self.token_dir / "garmin_tokens.json"

    def has_tokens(self) -> bool:
        return self.token_file.exists()

    def client(self) -> Garmin:
        with self._lock:
            if self._client is None:
                if not self.has_tokens():
                    raise GarminAuthRequired("Garmin login required")
                g = Garmin()
                g.login(str(self.token_dir))
                self._client = g
            return self._client

    def start_login(self, email: str, password: str) -> bool:
        """Return True when an MFA code is needed, False when logged in."""
        g = Garmin(email=email, password=password, return_on_mfa=True)
        status, state = g.login()
        if status == "needs_mfa":
            self._pending = (g, state)
            return True
        self._finish(g)
        return False

    def submit_mfa(self, code: str) -> None:
        if not self._pending:
            raise GarminAuthRequired("No login is waiting for a code")
        g, state = self._pending
        g.resume_login(state, code.strip())
        self._pending = None
        self._finish(g)

    def _finish(self, g: Garmin) -> None:
        self.token_dir.mkdir(parents=True, exist_ok=True)
        g.client.dump(str(self.token_dir))
        with self._lock:
            self._client = None  # reload from the token store, profile included
        self.client()
        self.last_ok, self.last_error, self.last_error_at = datetime.now(timezone.utc), None, None

    def logout(self) -> None:
        with self._lock:
            self._client = None
        self.token_file.unlink(missing_ok=True)
        self.last_ok, self.last_error, self.last_error_at = None, None, None

    def reset(self) -> None:
        """Forget the loaded client so the token file is read again."""
        with self._lock:
            self._client = None

    # --- activity helpers -------------------------------------------------

    def activities(self, days: int) -> list[dict[str, Any]]:
        start = (date.today() - timedelta(days=days)).isoformat()
        end = (date.today() + timedelta(days=1)).isoformat()
        return self.client().get_activities_by_date(start, end)

    def activities_between(self, start: date, end: date) -> list[dict[str, Any]]:
        return self.client().get_activities_by_date(start.isoformat(), end.isoformat())

    def activity(self, activity_id: int) -> dict[str, Any]:
        return self.client().get_activity(str(activity_id))

    def original_fit(self, activity_id: int) -> tuple[bytes, bytes]:
        """Return (raw download, extracted FIT bytes)."""
        raw = self.client().download_activity(
            str(activity_id), dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL)
        if raw[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                fits = [n for n in z.namelist() if n.lower().endswith(".fit")]
                if not fits:
                    raise ValueError(f"no FIT file in the original of {activity_id}")
                return raw, z.read(fits[0])
        return raw, raw

    def gear_uuids(self, activity_id: int) -> list[str]:
        try:
            gear = self.client().get_activity_gear(str(activity_id)) or []
        except Exception as e:  # noqa: BLE001 - gear is best effort
            log.warning("Could not read gear of %s: %s", activity_id, e)
            return []
        return [g["uuid"] for g in gear if isinstance(g, dict) and g.get("uuid")]

    def _around(self, start: datetime) -> list[dict[str, Any]]:
        return self.activities_between(start.date() - timedelta(days=1), start.date() + timedelta(days=1))

    def exists(self, activity_id: int, start: datetime) -> bool:
        """Whether the activity is still on Garmin, checked in the activity list.

        Used after an error: Garmin sometimes answers 5xx or times out even
        though the request went through.
        """
        return any(int(a.get("activityId", 0)) == int(activity_id) for a in self._around(start))

    def find_new(self, start: datetime, exclude: set[int]) -> int | None:
        """Id of an activity starting within 60 s of ``start`` that is not in ``exclude``."""
        for a in self._around(start):
            aid = int(a.get("activityId", 0))
            if aid in exclude:
                continue
            try:
                a_start = datetime.strptime(a["startTimeGMT"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            if abs((a_start - start).total_seconds()) <= 60:
                return aid
        return None

    def delete(self, activity_id: int, start: datetime, checks: int = 6, pause_s: float = 10) -> None:
        """Delete and make sure the activity is gone, whatever Garmin answered."""
        try:
            self.client().delete_activity(str(activity_id))
        except Exception as e:  # noqa: BLE001 - verified below
            log.warning("Delete of %s answered %s; checking whether it went through", activity_id, e)
        for i in range(checks):
            if not self.exists(activity_id, start):
                return
            if i < checks - 1:
                time.sleep(pause_s)
        raise RuntimeError(f"activity {activity_id} is still on Garmin after deleting it")

    def import_fit(self, path: Path, start: datetime, exclude: set[int],
                   wait_s: int = 300, pause_s: float = 10) -> int:
        """Import ``path`` and return the id of the new activity.

        Raises DuplicateActivity when Garmin says it already has this file.
        After any other error, still looks for the activity: Garmin may have
        taken the file despite a 5xx or a timeout.
        """
        error: Exception | None = None
        try:
            result = self.client().import_activity(str(path))
            detailed = (result or {}).get("detailedImportResult") or {}
            for s in detailed.get("successes") or []:
                if s.get("internalId"):
                    return int(s["internalId"])
        except Exception as e:  # noqa: BLE001
            if "duplicate" in str(e).lower():
                raise DuplicateActivity(str(e)) from e
            log.warning("Import of %s answered %s; looking for the activity anyway", path.name, e)
            error = e
        # Processing is asynchronous: look for the activity by start time.
        deadline = time.monotonic() + wait_s
        while True:
            found = self.find_new(start, exclude)
            if found:
                return found
            if time.monotonic() >= deadline:
                break
            time.sleep(pause_s)
        if error:
            raise RuntimeError(f"import failed ({error}) and no new activity appeared")
        raise TimeoutError("imported activity not found after import")

    def restore(self, activity_id: int, *, name: str | None, description: str | None,
                type_dto: dict[str, Any] | None, gear: list[str]) -> list[str]:
        """Re-apply metadata; return the list of things that failed."""
        failed = []
        fields: dict[str, Any] = {}
        if name:
            fields["activityName"] = name
        if description:
            fields["description"] = description
        if type_dto and type_dto.get("typeId"):
            fields["activityTypeDTO"] = {k: type_dto[k] for k in ("typeId", "typeKey", "parentTypeId") if k in type_dto}
        if fields:
            try:
                self.client().update_activity(str(activity_id), fields)
            except Exception as e:  # noqa: BLE001
                log.warning("Metadata not restored on %s: %s", activity_id, e)
                failed.append("name/description/type")
        for uuid in gear:
            try:
                self.client().add_gear_to_activity(uuid, str(activity_id))
            except Exception as e:  # noqa: BLE001
                log.warning("Gear %s not linked to %s: %s", uuid, activity_id, e)
                failed.append(f"gear {uuid}")
        return failed


_shared: GarminSession | None = None
_shared_lock = threading.Lock()


def shared_session() -> GarminSession:
    """The one Garmin session of the app, with tokens in ``DATA_DIR/garminconnect``."""
    global _shared
    from .paths import garmin_token_dir

    with _shared_lock:
        if _shared is None or _shared.token_dir != garmin_token_dir():
            _shared = GarminSession(garmin_token_dir())
        return _shared
