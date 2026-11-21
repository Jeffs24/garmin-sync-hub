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

SOURCES = {
    "liftosaur": "Liftosaur → Garmin",
    "zwift": "Zwift → Garmin",
}

# Feed tabs: key -> label. "attention" = failed, or waiting for you.
FILTERS = {
    "all": "All",
    "liftosaur": "Liftosaur",
    "zwift": "Zwift",
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
            link="/liftosaur/workouts",
        ))
    for row in failures:
        events.append(Event(
            source="liftosaur",
            when=_parse_time(row.get("failed_at")),
            title=row.get("title") or row.get("workout_id", ""),
            result="Failed",
            state="failed",
            detail=row.get("error") or "",
            link="/liftosaur/workouts",
        ))
    return events


# --- Zwift -------------------------------------------------------------------

_ZWIFT_STATES = {
    # status: (result, state)
    "merged": ("Map added", "ok"),
    "failed": ("Failed", "failed"),
    "detected": ("Ready to merge", "pending"),
    "simulated": ("Simulated, ready to merge", "pending"),
    "queued": ("Queued", "pending"),
    "running": ("Running…", "pending"),
    "ignored": ("Ignored", "muted"),
}


def zwift_events(pairs: Iterable[dict]) -> list[Event]:
    events = []
    for row in pairs:
        result, state = _ZWIFT_STATES.get(row.get("status", ""), (row.get("status", ""), "muted"))
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


def build_feed(synced: Iterable[dict], failures: Iterable[dict], pairs: Iterable[dict]) -> list[Event]:
    """Every event, newest first."""
    events = liftosaur_events(synced, failures) + zwift_events(pairs)
    events.sort(key=lambda e: e.when, reverse=True)
    return events
