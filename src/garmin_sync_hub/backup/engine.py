"""Back up a Garmin Connect account into a folder, adding only what is new at each run.

Layout (BACKUP_DIR)::

    README.txt
    activities/<category>/<year>/<YYYY-MM-DD_HHMM>_<name>_<id>.fit   original file (.fit, or .tcx/.gpx)
    activities/<category>/<year>/<YYYY-MM-DD_HHMM>_<name>_<id>.json  summary, details (description),
                                       weather, time in HR zones, and the sets of strength workouts
    activities/index.csv
    account/*.json                     profile, statistics, devices (and their settings), records,
                                       goals, zones, FTP, lactate threshold, badges, training plans…
    gear/gear.json, gear/defaults.json and gear/<name>_<id>/{details,stats,activities}.json  (active and retired)
    workouts/<name>_<id>.fit           plus workouts/workouts.json
    courses/<name>_<id>.fit and .gpx   plus courses/courses.json
    health/<metric>/<year>/<YYYY-MM-DD>.json   one file per metric and day, Garmin's raw answer
    health/daily_summary.csv
    history/<name>/<year>.json         calendar, body composition, weigh-ins, blood pressure,
                                       endurance and hill scores, race predictions… (one file per year)

A file that exists is never downloaded again (except account data and the last two days of
health data, which Garmin still completes), so an interrupted run resumes where it stopped.
Every file is written to a temporary name first, then renamed: a crash never leaves half a file.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)

from ..garmin_session import GarminAuthRequired

log = logging.getLogger(__name__)

PAUSE_S = 0.4                     # between two Garmin requests
RATE_LIMIT_WAITS = (60, 180)      # seconds to wait after a 429 before giving up for this run
REFRESH_DAYS = 2                  # health days fetched again at each run (Garmin completes them later)
PAGE = 100

# Daily health data: folder name -> request for one day.
HEALTH_METRICS: dict[str, Callable[[Garmin, str], Any]] = {
    "daily_summary": lambda g, d: g.get_user_summary(d),
    "sleep": lambda g, d: g.get_sleep_data(d),
    "hrv": lambda g, d: g.get_hrv_data(d),
    "heart_rate": lambda g, d: g.get_heart_rates(d),
    "resting_heart_rate": lambda g, d: g.get_rhr_day(d),
    "stress": lambda g, d: g.get_stress_data(d),
    "body_battery": lambda g, d: g.get_body_battery(d, d),
    "steps": lambda g, d: g.get_steps_data(d),
    "floors": lambda g, d: g.get_floors(d),
    "intensity_minutes": lambda g, d: g.get_intensity_minutes_data(d),
    "respiration": lambda g, d: g.get_respiration_data(d),
    "spo2": lambda g, d: g.get_spo2_data(d),
    "hydration": lambda g, d: g.get_hydration_data(d),
    "weight": lambda g, d: g.get_daily_weigh_ins(d),
    "training_readiness": lambda g, d: g.get_training_readiness(d),
    "training_status": lambda g, d: g.get_training_status(d),
    "max_metrics": lambda g, d: g.get_max_metrics(d),  # VO2 max, fitness age inputs
    "all_day_events": lambda g, d: g.get_all_day_events(d),
}

# Data over a period, saved per year: health/history/<name>/<year>.json. A past year is saved once.
HISTORY_RANGES: dict[str, Callable[[Garmin, str, str], Any]] = {
    "body_composition": lambda g, s, e: g.get_body_composition(s, e),
    "weigh_ins": lambda g, s, e: g.get_weigh_ins(s, e),
    "blood_pressure": lambda g, s, e: g.get_blood_pressure(s, e),
    "endurance_score": lambda g, s, e: g.get_endurance_score(s, e),
    "hill_score": lambda g, s, e: g.get_hill_score(s, e),
    "race_predictions": lambda g, s, e: g.get_race_predictions(s, e, "daily"),
    "running_tolerance": lambda g, s, e: g.get_running_tolerance(s, e),
    "lactate_threshold": lambda g, s, e: g.get_lactate_threshold(latest=False, start_date=s, end_date=e),
    "sleep_stats": lambda g, s, e: g.get_sleep_stats_overall(s, e),
}

# Account data: file name -> request. Small, written again at each run.
ACCOUNT_DATA: dict[str, Callable[[Garmin], Any]] = {
    "user_profile": lambda g: g.get_user_profile(),
    "profile_settings": lambda g: g.get_userprofile_settings(),
    "personal_information": lambda g: g.get_personal_information(),
    "user_statistics": lambda g: g.get_user_statistics(),
    "devices": lambda g: g.get_devices(),
    "historical_devices": lambda g: g.get_historical_devices(),
    "personal_records": lambda g: g.get_personal_record(include_history=True),
    "goals": lambda g: g.get_user_goals(),
    "lifetime_totals": lambda g: g.get_lifetime_totals(),
    "heart_rate_zones": lambda g: g.get_heart_rate_zones(),
    "power_zones": lambda g: g.get_power_zones(),
    "cycling_ftp": lambda g: g.get_cycling_ftp(),
    "lactate_threshold": lambda g: g.get_lactate_threshold(),
    "race_predictions": lambda g: g.get_race_predictions(),
    "fitness_age": lambda g: g.get_fitnessage_data(date.today().isoformat()),
    "one_rep_max": lambda g: g.get_one_rep_max(),
    "strength_benchmarks": lambda g: g.get_strength_benchmarks(),
    "training_plans": lambda g: g.get_active_training_plans(),
    "earned_badges": lambda g: g.get_earned_badges(),
    "completed_challenges": lambda g: g.get_completed_virtual_challenges(),
    "ecg": lambda g: g.get_ecg_list(),
    "golf": lambda g: g.get_golf_summary(),
}

# Extra data saved with each activity, next to its file (most are not in the FIT file:
# description, weather, sets edited on Garmin Connect).
ACTIVITY_EXTRAS: dict[str, Callable[[Garmin, str], Any]] = {
    "details": lambda g, i: g.get_activity(i),
    "weather": lambda g, i: g.get_activity_weather(i),
    "hr_time_in_zones": lambda g, i: g.get_activity_hr_in_timezones(i),
}
STRENGTH_TYPES = {"strength_training", "indoor_cardio", "hiit", "pilates", "yoga"}

SUMMARY_COLUMNS = (
    "calendarDate", "totalSteps", "totalDistanceMeters", "totalKilocalories", "activeKilocalories",
    "restingHeartRate", "minHeartRate", "maxHeartRate", "averageStressLevel",
    "bodyBatteryHighestValue", "bodyBatteryLowestValue", "sleepingSeconds", "floorsAscended",
    "moderateIntensityMinutes", "vigorousIntensityMinutes",
)
INDEX_COLUMNS = ("activityId", "startTimeLocal", "category", "type", "name", "distance_km",
                 "duration_min", "calories", "averageHR", "file")

README = """Garmin Connect backup made by garmin-sync-hub.

activities/  every activity as the file Garmin keeps (FIT for watch activities), by category and year.
             Re-import one in Garmin Connect: Activities > Import Data (FIT, GPX or TCX).
             The .json next to each file holds what the file lacks: name, description, weather,
             time in heart rate zones, and the sets of strength workouts as edited on Garmin Connect.
             index.csv lists them all. An activity entered by hand has no file, only its .json.
account/     profile, statistics, devices and their settings, personal records (with history), goals,
             heart rate and power zones, FTP, lactate threshold, race predictions, fitness age,
             one-rep max, badges, challenges, training plans, ECG and golf summaries.
gear/        all your gear, active and retired: details, totals (distance, time) and the activities it was used in.
workouts/    your workouts as FIT files (copy one to a watch's NewFiles folder) and workouts.json.
courses/     your courses as FIT and GPX files, and courses.json.
health/      one JSON file per metric and per day (Garmin's raw answer): daily summary, sleep, HRV,
             heart rate, resting heart rate, stress, Body Battery, steps, floors, intensity minutes,
             respiration, SpO2, hydration, weight, training readiness and status, VO2 max, events.
             daily_summary.csv sums up each day.
history/     data over a period, one file per year: calendar (scheduled workouts, events), body
             composition, weigh-ins, blood pressure, endurance and hill scores, race predictions,
             running tolerance, lactate threshold, sleep statistics.
"""


class StopBackup(RuntimeError):
    """Stop the whole run (not just one item); the next run resumes."""


class RateLimited(StopBackup):
    """Garmin keeps answering 429."""


class SignInRefused(StopBackup):
    """Garmin is not connected, or refuses the saved sign-in."""


@dataclass
class Progress:
    phase: str = ""
    done: int = 0
    total: int = 0
    message: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def warn(self, text: str) -> None:
        log.warning("Backup: %s", text)
        if len(self.warnings) < 50:
            self.warnings.append(text)


def slug(text: str, limit: int = 60) -> str:
    """File-name safe version of an activity or workout name."""
    text = re.sub(r"[^\w\-]+", "-", str(text or "").strip(), flags=re.UNICODE).strip("-_")
    return (text[:limit].rstrip("-_") or "activity")


def write_atomic(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    if isinstance(data, str):
        tmp.write_text(data, encoding="utf-8")
    else:
        tmp.write_bytes(data)
    tmp.replace(path)


def write_json(path: Path, value: Any) -> None:
    write_atomic(path, json.dumps(value, indent=2, ensure_ascii=False, default=str))


_ID_AT_END = re.compile(r"_(\d+)$")


class AnyEvent:
    """Looks like a threading.Event; set when any of the events is set (app shutdown, Stop button)."""

    def __init__(self, *events: threading.Event):
        self.events = events

    def is_set(self) -> bool:
        return any(e.is_set() for e in self.events)

    def wait(self, timeout: float | None = None) -> bool:
        waited = 0.0
        while not self.is_set():
            if timeout is not None and waited >= timeout:
                return False
            self.events[0].wait(1.0)
            waited += 1.0
        return True


class PacedGarmin:
    """Garmin requests spaced out, waiting out rate limits, stopped by `stop` (shared by jobs)."""

    def __init__(self, client_factory: Callable[[], Garmin], *, stop: threading.Event | None = None,
                 progress: Progress | None = None, pause_s: float | None = None):
        self.client_factory = client_factory
        self.stop = stop or threading.Event()
        self.p = progress or Progress()
        self.pause_s = PAUSE_S if pause_s is None else pause_s
        self._client: Garmin | None = None

    @property
    def g(self) -> Garmin:
        if self._client is None:
            try:
                self._client = self.client_factory()
            except (GarminConnectAuthenticationError, GarminAuthRequired) as e:
                raise SignInRefused(f"Garmin refused the sign-in ({e}): reconnect in Settings → Garmin") from None
        return self._client

    def call(self, fn: Callable, *args, **kwargs):
        """One Garmin request, paced, waiting out rate limits."""
        for wait in (*RATE_LIMIT_WAITS, None):
            if self.stop.is_set():
                raise InterruptedError
            time.sleep(self.pause_s)
            try:
                return fn(*args, **kwargs)
            except (GarminConnectAuthenticationError, GarminAuthRequired) as e:
                raise SignInRefused(f"Garmin refused the sign-in ({e}): reconnect in Settings → Garmin") from None
            except GarminConnectTooManyRequestsError:
                if wait is None:
                    raise RateLimited("Garmin is limiting requests: the next run resumes from here") from None
                self.p.message = f"Garmin is limiting requests, waiting {wait} s…"
                log.info("Rate limited by Garmin, waiting %s s", wait)
                if self.stop.wait(wait):
                    raise InterruptedError from None
        raise AssertionError("unreachable")

    def list_activities(self, max_pages: int | None = None) -> list[dict]:
        """Activity summaries, newest first (all of them, or the first pages)."""
        # Garmin may return fewer than PAGE activities per page: only an empty page means the
        # end of the history (stopping at a short page used to miss every older activity).
        activities: list[dict] = []
        seen: set = set()
        start, pages = 0, 0
        while True:
            page = self.call(self.g.get_activities, start, PAGE) or []
            pages += 1
            new = [a for a in page if isinstance(a, dict) and a.get("activityId")
                   and a["activityId"] not in seen]
            if not new:
                return activities  # end of the history (or Garmin repeating itself)
            seen.update(a["activityId"] for a in new)
            activities.extend(new)
            self.p.message = f"{len(activities)} activities listed"
            if max_pages and pages >= max_pages:
                return activities
            start += len(page)


class Backup(PacedGarmin):
    def __init__(self, client_factory: Callable[[], Garmin], root: Path, *, health: bool = True,
                 health_since: date | None = None, stop: threading.Event | None = None,
                 progress: Progress | None = None, pause_s: float | None = None, today: date | None = None):
        super().__init__(client_factory, stop=stop, progress=progress, pause_s=pause_s)
        self.root = root
        self.health = health
        self.health_since = health_since
        self.today = today or date.today()
        self._categories: dict[int, str] = {}

    def _phase(self, name: str, total: int = 0) -> None:
        self.p.phase, self.p.done, self.p.total, self.p.message = name, 0, total, ""

    # --- run ----------------------------------------------------------------

    def run(self) -> Progress:
        self.root.mkdir(parents=True, exist_ok=True)
        write_atomic(self.root / "README.txt", README)
        self.backup_account()
        self.backup_gear()
        first_day = self.backup_activities()
        self.backup_workouts()
        self.backup_courses()
        since = self.health_since or first_day or self.today - timedelta(days=365)
        self.backup_history(min(first_day or since, since), include_health=self.health)
        if self.health:
            self.backup_health(since)
        self._phase("done")
        return self.p

    # --- account ------------------------------------------------------------

    def backup_account(self) -> None:
        self._phase("account", len(ACCOUNT_DATA))
        for name, fetch in ACCOUNT_DATA.items():
            try:
                write_json(self.root / "account" / f"{name}.json", self.call(fetch, self.g))
            except (InterruptedError, StopBackup):
                raise
            except Exception as e:  # noqa: BLE001 - one missing item never stops the backup
                self.p.warn(f"account/{name}: {e}")
            self.p.done += 1
        self.backup_device_settings()

    def backup_device_settings(self) -> None:
        """Settings of each device (watch faces, data screens, alarms…): account/devices/<name>_<id>.json."""
        try:
            devices = json.loads((self.root / "account" / "devices.json").read_text(encoding="utf-8")) or []
        except (OSError, ValueError):
            return
        for d in devices if isinstance(devices, list) else []:
            device_id = d.get("deviceId") if isinstance(d, dict) else None
            if not device_id:
                continue
            name = d.get("productDisplayName") or d.get("displayName") or "device"
            try:
                write_json(self.root / "account" / "devices" / f"{slug(name)}_{device_id}.json",
                           self.call(self.g.get_device_settings, str(device_id)))
            except (InterruptedError, StopBackup):
                raise
            except Exception as e:  # noqa: BLE001
                self.p.warn(f"device {name} settings: {e}")

    # --- history per year (calendar, body composition, scores…) ----------------

    def history_path(self, name: str, year: int) -> Path:
        return self.root / "history" / name / f"{year}.json"

    def backup_history(self, since: date, include_health: bool) -> None:
        """Data over a period, one file per year. Past years are saved once, this year at each run."""
        ranges: dict[str, Callable] = {"calendar": None}  # type: ignore[dict-item]
        if include_health:
            ranges.update(HISTORY_RANGES)
        years = list(range(since.year, self.today.year + 1))
        todo = [(n, y) for n in ranges for y in years
                if y == self.today.year or not self.history_path(n, y).exists()]
        self._phase("history", len(todo))
        for name, year in todo:
            start = max(date(year, 1, 1), since).isoformat()
            end = min(date(year, 12, 31), self.today).isoformat()
            try:
                if name == "calendar":
                    value = self.call(self.g.get_calendar_year, year)
                else:
                    value = self.call(HISTORY_RANGES[name], self.g, start, end)
                write_json(self.history_path(name, year), value)
            except (InterruptedError, StopBackup):
                raise
            except Exception as e:  # noqa: BLE001
                self.p.warn(f"history/{name} {year}: {e}")
            self.p.done += 1
            self.p.message = f"{name} {year}"

    # --- gear -----------------------------------------------------------------

    def backup_gear(self) -> None:
        """Every piece of gear, active and retired, with its totals and the activities it was used in."""
        self._phase("gear")
        try:
            gear = self.call(self.g.get_gear_list, 0, 1000, gear_statuses=["ACTIVE", "RETIRED"]) or []
        except (InterruptedError, StopBackup):
            raise
        except Exception as e:  # noqa: BLE001
            self.p.warn(f"gear: {e}")
            return
        write_json(self.root / "gear" / "gear.json", gear)
        profile_number = self._profile_number()
        if profile_number:
            try:
                write_json(self.root / "gear" / "defaults.json", self.call(self.g.get_gear_defaults, profile_number))
            except (InterruptedError, StopBackup):
                raise
            except Exception as e:  # noqa: BLE001
                self.p.warn(f"gear defaults: {e}")
        self.p.total = len(gear)
        for item in gear:
            uuid = item.get("uuid") or item.get("gearUuid")
            if not uuid:
                continue
            name = item.get("displayName") or item.get("customMakeModel") or item.get("gearMakeName") or "gear"
            folder = self.root / "gear" / f"{slug(name)}_{str(uuid).replace('-', '')[:8]}"
            for part, fetch in (("details", self.g.get_gear_details), ("stats", self.g.get_gear_stats),
                                ("activities", self.g.get_gear_activities)):
                try:
                    write_json(folder / f"{part}.json", self._gear_call(fetch, uuid))
                except (InterruptedError, StopBackup):
                    raise
                except Exception as e:  # noqa: BLE001
                    self.p.warn(f"gear {name} ({part}): {e}")
            self.p.done += 1
        self.p.counts["gear"] = len(gear)

    def _gear_call(self, fetch: Callable, uuid: str):
        """Gear calls take the dashed uuid (v2) or the plain one (older endpoints)."""
        try:
            return self.call(fetch, uuid)
        except (InterruptedError, StopBackup):
            raise
        except Exception:  # noqa: BLE001
            if "-" not in uuid:
                raise
            return self.call(fetch, uuid.replace("-", ""))

    def _profile_number(self) -> str | None:
        for path in ("user_profile", "profile_settings"):
            try:
                data = json.loads((self.root / "account" / f"{path}.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for key in ("id", "profileId", "userProfileNumber", "userProfileId"):
                if isinstance(data, dict) and data.get(key):
                    return str(data[key])
        return None

    # --- activities ---------------------------------------------------------

    def _known_activities(self) -> set[int]:
        """Activities already saved: their summary .json is written last."""
        known = set()
        for path in (self.root / "activities").glob("*/*/*.json"):
            m = _ID_AT_END.search(path.stem)
            if m:
                known.add(int(m.group(1)))
        return known

    def _category(self, activity_type: dict) -> str:
        """Top-level activity type (running, cycling, swimming, fitness_equipment…)."""
        if not self._categories:
            try:
                types = self.call(self.g.get_activity_types) or []
            except (InterruptedError, StopBackup):
                raise
            except Exception as e:  # noqa: BLE001
                self.p.warn(f"activity types: {e}")
                types = []
            by_id = {t.get("typeId"): t for t in types if isinstance(t, dict)}
            for type_id, t in by_id.items():
                node, seen = t, set()
                while node.get("parentTypeId") in by_id and node.get("parentTypeId") not in (17, None) \
                        and node["typeId"] not in seen:
                    seen.add(node["typeId"])
                    node = by_id[node["parentTypeId"]]
                self._categories[type_id] = node.get("typeKey") or "other"
            self._categories.setdefault(-1, "other")
        return self._categories.get(activity_type.get("typeId"), activity_type.get("typeKey") or "other")

    def activity_base(self, a: dict) -> Path:
        started = str(a.get("startTimeLocal") or a.get("startTimeGMT") or "")
        try:
            when = datetime.fromisoformat(started.replace(" ", "T"))
        except ValueError:
            when = datetime(1970, 1, 1)
        category = slug(self._category(a.get("activityType") or {}), 40)
        name = f"{when:%Y-%m-%d_%H%M}_{slug(a.get('activityName'))}_{a['activityId']}"
        return self.root / "activities" / category / f"{when:%Y}" / name

    def save_activity(self, a: dict) -> None:
        base = self.activity_base(a)
        try:
            raw = self.call(self.g.download_activity, str(a["activityId"]),
                            dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL)
        except GarminConnectNotFoundError:
            raw = None  # entered by hand: no file, the summary is the record
        if raw:
            if raw[:2] == b"PK":
                with zipfile.ZipFile(io.BytesIO(raw)) as z:
                    for member in z.namelist():
                        ext = Path(member).suffix.lower() or ".fit"
                        write_atomic(base.with_suffix(ext), z.read(member))
                        break
            else:
                write_atomic(base.with_suffix(".fit"), raw)
        record: dict[str, Any] = {"summary": a}
        extras = dict(ACTIVITY_EXTRAS)
        if (a.get("activityType") or {}).get("typeKey") in STRENGTH_TYPES:
            extras["exercise_sets"] = lambda g, i: g.get_activity_exercise_sets(i)
        for name, fetch in extras.items():
            try:
                record[name] = self.call(fetch, self.g, str(a["activityId"]))
            except (InterruptedError, StopBackup):
                raise
            except Exception as e:  # noqa: BLE001 - the file and summary are what matters
                record[name] = None
                self.p.warn(f"activity {a['activityId']} {name}: {e}")
        write_json(base.with_suffix(".json"), record)  # last: marks the activity as saved

    def backup_activities(self) -> date | None:
        """Save new activities; return the day of the first one (where health data starts)."""
        self._phase("activities")
        activities = self.list_activities()
        known = self._known_activities()
        todo = [a for a in activities if int(a["activityId"]) not in known]
        self.p.total, self.p.done = len(todo), 0
        for a in reversed(todo):  # oldest first: an interrupted run leaves no gap in recent ones
            try:
                self.save_activity(a)
                self.p.counts["activities_new"] = self.p.counts.get("activities_new", 0) + 1
            except (InterruptedError, StopBackup):
                raise
            except Exception as e:  # noqa: BLE001
                self.p.warn(f"activity {a['activityId']} ({a.get('activityName')}): {e}")
            self.p.done += 1
        self.p.counts["activities"] = len(self._known_activities())
        self.write_index()
        days = [str(a.get("startTimeLocal") or "")[:10] for a in activities]
        days = [d for d in days if re.match(r"\d{4}-\d{2}-\d{2}$", d)]
        return date.fromisoformat(min(days)) if days else None

    def write_index(self) -> None:
        rows = []
        for path in sorted((self.root / "activities").glob("*/*/*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                a = record.get("summary", record) if isinstance(record, dict) else {}
            except (OSError, ValueError):
                continue
            files = [p.name for p in path.parent.glob(path.stem + ".*") if p.suffix != ".json"]
            rows.append({
                "activityId": a.get("activityId"),
                "startTimeLocal": a.get("startTimeLocal"),
                "category": path.parent.parent.name,
                "type": (a.get("activityType") or {}).get("typeKey"),
                "name": a.get("activityName"),
                "distance_km": round(a["distance"] / 1000, 2) if a.get("distance") else "",
                "duration_min": round(a["duration"] / 60, 1) if a.get("duration") else "",
                "calories": a.get("calories") or "",
                "averageHR": a.get("averageHR") or "",
                "file": str(Path(path.parent.parent.name) / path.parent.name / files[0]) if files else "",
            })
        rows.sort(key=lambda r: str(r["startTimeLocal"]), reverse=True)
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=INDEX_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        write_atomic(self.root / "activities" / "index.csv", out.getvalue())

    # --- workouts and courses -------------------------------------------------

    def backup_workouts(self) -> None:
        self._phase("workouts")
        try:
            workouts, start = [], 0
            while True:
                page = self.call(self.g.get_workouts, start, PAGE) or []
                workouts.extend(page)
                if len(page) < PAGE:
                    break
                start += PAGE
        except (InterruptedError, StopBackup):
            raise
        except Exception as e:  # noqa: BLE001
            self.p.warn(f"workouts: {e}")
            return
        write_json(self.root / "workouts" / "workouts.json", workouts)
        self.p.total = len(workouts)
        for w in workouts:
            wid = w.get("workoutId")
            path = self.root / "workouts" / f"{slug(w.get('workoutName'))}_{wid}.fit"
            if wid and not path.exists():
                try:
                    write_atomic(path, self.call(self.g.download_workout, wid))
                except (InterruptedError, StopBackup):
                    raise
                except Exception as e:  # noqa: BLE001
                    self.p.warn(f"workout {wid}: {e}")
            self.p.done += 1
        self.p.counts["workouts"] = len(workouts)

    def backup_courses(self) -> None:
        self._phase("courses")
        try:
            courses = self.call(self.g.get_courses) or []
        except (InterruptedError, StopBackup):
            raise
        except Exception as e:  # noqa: BLE001
            self.p.warn(f"courses: {e}")
            return
        write_json(self.root / "courses" / "courses.json", courses)
        self.p.total = len(courses)
        for c in courses:
            cid = c.get("courseId")
            for fmt in ("fit", "gpx"):
                path = self.root / "courses" / f"{slug(c.get('courseName'))}_{cid}.{fmt}"
                if cid and not path.exists():
                    try:
                        write_atomic(path, self.call(self.g.download_course, cid, fmt=fmt))
                    except (InterruptedError, StopBackup):
                        raise
                    except Exception as e:  # noqa: BLE001
                        self.p.warn(f"course {cid} ({fmt}): {e}")
            self.p.done += 1
        self.p.counts["courses"] = len(courses)

    # --- health -------------------------------------------------------------

    def health_path(self, metric: str, day: date) -> Path:
        return self.root / "health" / metric / f"{day:%Y}" / f"{day.isoformat()}.json"

    def backup_health(self, since: date) -> None:
        last = self.today - timedelta(days=1)  # today is not over
        days = [since + timedelta(days=i) for i in range((last - since).days + 1)]
        refresh = {last - timedelta(days=i) for i in range(REFRESH_DAYS)}
        todo = [(d, m) for d in days for m in HEALTH_METRICS
                if d in refresh or not self.health_path(m, d).exists()]
        self._phase("health", len(todo))
        for day, metric in todo:
            try:
                value = self.call(HEALTH_METRICS[metric], self.g, day.isoformat())
                write_json(self.health_path(metric, day), value)
                if metric == "daily_summary":
                    self.p.counts["health_days_new"] = self.p.counts.get("health_days_new", 0) + 1
            except (InterruptedError, StopBackup):
                raise
            except Exception as e:  # noqa: BLE001
                self.p.warn(f"health/{metric} {day}: {e}")
            self.p.done += 1
            self.p.message = day.isoformat()
        self.write_daily_summary()

    def write_daily_summary(self) -> None:
        rows = []
        for path in sorted((self.root / "health" / "daily_summary").glob("*/*.json")):
            try:
                s = json.loads(path.read_text(encoding="utf-8")) or {}
            except (OSError, ValueError):
                continue
            if isinstance(s, dict):
                rows.append({c: s.get(c, "") if c != "calendarDate" else path.stem for c in SUMMARY_COLUMNS})
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=SUMMARY_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        write_atomic(self.root / "health" / "daily_summary.csv", out.getvalue())
        self.p.counts["health_days"] = len(rows)


def folder_size(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def now() -> datetime:
    return datetime.now(timezone.utc)
