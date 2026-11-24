"""One activity feed for every source that writes to Garmin.

Each module keeps its own records (Liftosaur: synced workouts and failures in
liftosaur.db; Zwift: pairs in zwift.db). This module reads both and turns them
into the same :class:`Event` shape, so the UI can list, filter and count them
together. Adding a source means adding one ``*_events`` function.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import quote

SOURCES = {
    "liftosaur": "Liftosaur → Garmin",
    "zwift": "Zwift → Garmin",
    "dogs": "Dogs → Garmin",
    "backup": "Garmin → Backup",
}

# Feed tabs: key -> label. "attention" = failed, or waiting for you.
FILTERS = {
    "all": "All",
    "liftosaur": "Liftosaur",
    "zwift": "Zwift",
    "dogs": "Dogs",
    "backup": "Backup",
    "attention": "Needs attention",
}

GARMIN_ACTIVITY_URL = "https://connect.garmin.com/modern/activity/{}"


@dataclass
class Event:
    source: str            # key of SOURCES
    when: datetime         # when it happened (UTC)
    title: str
    result: str            # what was done, in a few words
    state: str             # "ok" | "pending" | "failed" | "muted"
    detail: str = ""       # error or extra information
    garmin_id: str | None = None
    link: str | None = None  # page with the details in this app
    retry_at: datetime | None = None  # next automatic retry of a failure
    retry_by: str = ""     # what retries it ("auto-sync", "auto-merge")

    @property
    def source_label(self) -> str:
        return SOURCES.get(self.source, self.source)

    @property
    def garmin_url(self) -> str | None:
        return GARMIN_ACTIVITY_URL.format(self.garmin_id) if self.garmin_id else None

    @property
    def needs_attention(self) -> bool:
        return self.state in ("failed", "pending")

    @property
    def iso(self) -> str:
        return self.when.isoformat(timespec="seconds")


def _parse_time(value: Any) -> datetime:
    """SQLite 'YYYY-MM-DD HH:MM:SS' (UTC) or ISO 8601 -> aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text.replace(" ", "T", 1))
        except ValueError:
            return datetime.fromtimestamp(0, timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --- Liftosaur ---------------------------------------------------------------

_LIFTOSAUR_RESULTS = {
    "uploaded": "Uploaded",
    "updated": "Watch activity updated",
}


def _workout_link(workout_id) -> str:
    """Page of one Liftosaur workout (it redirects to the right page of the list)."""
    if not workout_id:
        return "/liftosaur/workouts"
    return f"/liftosaur/workouts/{quote(str(workout_id), safe='')}"


def liftosaur_events(synced: Iterable[dict], failures: Iterable[dict]) -> list[Event]:
    events = []
    for row in synced:
        events.append(Event(
            source="liftosaur",
            when=_parse_time(row.get("synced_at")),
            title=row.get("title") or row.get("workout_id", ""),
            result=_LIFTOSAUR_RESULTS.get(row.get("action") or "", "Synced"),
            state="ok",
            detail=" · ".join(p for p in (
                f"{row['calories']} kcal" if row.get("calories") else "",
                f"avg {row['avg_hr']} bpm" if row.get("avg_hr") else "",
            ) if p),
            garmin_id=row.get("garmin_activity_id"),
            link=_workout_link(row.get("workout_id")),
        ))
    for row in failures:
        events.append(Event(
            source="liftosaur",
            when=_parse_time(row.get("failed_at")),
            title=row.get("title") or row.get("workout_id", ""),
            result="Failed",
            state="failed",
            detail=row.get("error") or "",
            link=_workout_link(row.get("workout_id")),
            retry_at=_parse_time(row["retry_at"]) if row.get("retry_at") else None,
            retry_by="auto-sync",
        ))
    return events


# --- Zwift -------------------------------------------------------------------

ZWIFT_STATES = {
    # status: (result, state)
    "merged": ("Map added", "ok"),
    "failed": ("Failed", "failed"),
    "detected": ("Ready to merge", "pending"),
    "simulated": ("Simulated, ready to merge", "pending"),
    "queued": ("Queued", "pending"),
    "running": ("Running…", "pending"),
    "ignored": ("Ignored", "muted"),
    "restored": ("Original restored", "ok"),
}


def zwift_events(pairs: Iterable[dict]) -> list[Event]:
    events = []
    for row in pairs:
        result, state = ZWIFT_STATES.get(row.get("status", ""), (row.get("status", ""), "muted"))
        has_preview = row.get("status") in ("simulated", "merged")
        events.append(Event(
            source="zwift",
            when=_parse_time(row.get("updated_at") or row.get("start_utc")),
            title=row.get("target_name") or row.get("source_name") or f"#{row.get('target_id')}",
            result=result,
            state=state,
            detail=row.get("message") or "",
            garmin_id=str(row["new_id"]) if row.get("new_id") else None,
            link=f"/zwift/pairs/{row['target_id']}/preview" if has_preview else "/zwift",
            retry_at=(_parse_time(row["retry_at"])
                      if row.get("status") == "failed" and row.get("retry_at") else None),
            retry_by="auto-merge",
        ))
    return events


# --- feed --------------------------------------------------------------------

def filter_events(events: list[Event], kind: str) -> list[Event]:
    if kind in SOURCES:
        return [e for e in events if e.source == kind]
    if kind == "attention":
        return [e for e in events if e.needs_attention]
    return [e for e in events if e.state != "muted"]


def counts(events: list[Event]) -> dict[str, int]:
    return {kind: len(filter_events(events, kind)) for kind in FILTERS}


# --- Dogs --------------------------------------------------------------------

def dogs_events(log: Iterable[dict], dog_name=lambda d: f"Dog {d}") -> list[Event]:
    """Dogs added to activities, and activities that could not get theirs."""
    events = []
    for row in log:
        ok = row.get("ok", True)
        restored = ok and row.get("restored")
        names = ", ".join(dog_name(d) for d in row.get("dogs") or [])
        events.append(Event(
            source="dogs",
            when=_parse_time(row.get("at") or row.get("start")),
            title=row.get("name") or f"#{row.get('id')}",
            result=(f"{names} removed" if restored else f"{names} added") if ok else "Failed",
            state=("muted" if restored else "ok") if ok else "failed",
            detail=(f"Activity of {str(row.get('start') or '')[:16].replace('T', ' ')}" if ok
                    else f"{names}: {row.get('error') or ''}".strip(": ")),
            garmin_id=str(row["id"]) if row.get("id") else None,
            link="/dogs",
            retry_at=_parse_time(row["retry_at"]) if not ok and row.get("retry_at") else None,
            retry_by="auto-sync",
        ))
    return events


# --- Backup ------------------------------------------------------------------

def backup_events(runs: Iterable[dict]) -> list[Event]:
    """Backup runs: what each one added, or why it failed."""
    events = []
    for run in runs:
        added = []
        if run.get("activities_new"):
            n = run["activities_new"]
            added.append(f"{n} activit{'y' if n == 1 else 'ies'}")
        if run.get("health_days_new"):
            n = run["health_days_new"]
            added.append(f"{n} day{'' if n == 1 else 's'} of health data")
        if run.get("ok"):
            result, state = (", ".join(added) + " saved") if added else "Up to date", "ok"
        elif run.get("stopped"):
            result, state = "Stopped", "muted"
        else:
            result, state = "Failed", "failed"
        detail = run.get("error") or ""
        if run.get("ok") and run.get("warnings"):
            detail = f"{run['warnings']} item{'' if run['warnings'] == 1 else 's'} could not be saved (retried next time)"
        events.append(Event(
            source="backup", when=_parse_time(run.get("at")), title="Garmin account backup",
            result=result, state=state, detail=detail, link="/backup",
            retry_at=_parse_time(run["retry_at"]) if run.get("retry_at") and state == "failed" else None,
            retry_by="auto-sync",
        ))
    return events


def build_feed(synced: Iterable[dict], failures: Iterable[dict], pairs: Iterable[dict],
               dogs: Iterable[Event] = (), backups: Iterable[Event] = ()) -> list[Event]:
    """Every event, newest first."""
    events = liftosaur_events(synced, failures) + zwift_events(pairs) + list(dogs) + list(backups)
    events.sort(key=lambda e: e.when, reverse=True)
    return events
