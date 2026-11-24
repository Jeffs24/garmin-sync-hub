"""Find dogs and add them to the activities the rules select.

Garmin Connect keeps the dogs of an activity in its metadata: ``metadataDTO.hasDog`` and
``metadataDTO.dogProfileIds``. The web app adds a dog by sending back the whole current
metadata with those two fields changed (PUT /activity-service/activity/{id}); doing the same
keeps every other field (favorite, lap count…). Dogs already on an activity are kept.
Your dogs are listed by the profile service on gcx.<domain> (the host the Connect web app uses for
family and pet profiles), with their names. The activity details only say ``hasDog`` there, without
the ids, so reading activities is kept as a fallback (older activities and backups can carry the ids).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import retry, templating
from ..backup.engine import PacedGarmin, StopBackup
from .store import MAX_DOGS_PER_ACTIVITY, DogsStore

log = logging.getLogger(__name__)

RECENT_PAGES = 1       # automatic runs look at the 100 latest activities
DISCOVER_LIMIT = 40    # activities read when looking for dogs
LOG_KEPT = 1000      # dogs added and failures kept for the Activity feed
PETS_PATH = "/family-api/profile-service/profile/pets"


def dog_ids(metadata: dict | None) -> list[int]:
    ids = (metadata or {}).get("dogProfileIds") or []
    return [int(i) for i in ids if str(i).isdigit()]


def scan_summary(activities: list[dict], store: DogsStore) -> dict[str, Any]:
    """What a preview looked at: how many activities, back to when, and names that look like a
    dog activity but match no rule (a different spelling is the usual reason a walk is missed)."""
    state = store.state
    starts = sorted(str(a.get("startTimeLocal") or "")[:10] for a in activities if a.get("startTimeLocal"))
    near: dict[str, int] = {}
    for a in activities:
        name = (a.get("activityName") or "").strip()
        if not state.dogs_for(name) and any(w in name.casefold() for w in ("dog", "chien", "walk", "marche")):
            near[name] = near.get(name, 0) + 1
    return {"checked": len(activities), "oldest": starts[0] if starts else None,
            "matching_total": sum(1 for a in activities if state.dogs_for(a.get("activityName"))),
            "near_misses": sorted(near.items(), key=lambda kv: -kv[1])[:10]}


class DogTagger(PacedGarmin):
    def __init__(self, client_factory, store: DogsStore, **kwargs):
        super().__init__(client_factory, **kwargs)
        self.store = store
        self.matches: list[dict[str, Any]] = []   # last preview
        self.dog_names: dict[int, str] = {}       # names from the Garmin profile, set by discover()
        self.scan: dict[str, Any] = {}            # what the last preview looked at

    # --- finding dogs -------------------------------------------------------

    def discover(self, backup_root: Path | None = None, limit: int = DISCOVER_LIMIT) -> dict[int, str]:
        """Dog ids -> the name of one activity it is on ("" for a dog read from the profile).

        Reads the dogs of the Garmin profile first. Only when there are none: the activities
        saved by the backup (no request), then the latest activities on Garmin Connect.
        """
        found: dict[int, str] = {}
        self.p.phase = "discover"
        for dog, name in self.profile_dogs().items():
            self.dog_names[dog] = name
            found.setdefault(dog, "")  # on the profile, not found through an activity
        if found:
            return found
        if backup_root and (backup_root / "activities").exists():
            for path in (backup_root / "activities").glob("*/*/*.json"):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                details = record.get("details") if isinstance(record, dict) else None
                for dog in dog_ids((details or {}).get("metadataDTO")):
                    found.setdefault(dog, (record.get("summary") or {}).get("activityName") or path.stem)
        activities = self.list_activities(max_pages=1)[:limit]
        self.p.total, self.p.done = len(activities), 0
        for a in activities:
            details = self.call(self.g.get_activity, str(a["activityId"])) or {}
            for dog in dog_ids(details.get("metadataDTO")):
                found.setdefault(dog, a.get("activityName") or str(a["activityId"]))
            self.p.done += 1
        return found

    def profile_dogs(self) -> dict[int, str]:
        """Active dogs of the Garmin profile -> their name. Empty when Garmin does not answer."""
        try:
            client = self.g.client
            domain = getattr(client, "domain", None) or getattr(client, "_domain", None) or "garmin.com"
            url = f"https://gcx.{domain}{PETS_PATH}"
            response = self.call(client.request, "GET", "gcx", url, api=True)
            pets = response.json() if hasattr(response, "json") else response
        except (InterruptedError, StopBackup):
            raise
        except Exception as e:  # noqa: BLE001 - fall back to reading activities
            log.warning("Could not read the dogs of the Garmin profile: %s", e)
            return {}
        dogs: dict[int, str] = {}
        for pet in pets if isinstance(pets, list) else []:
            if not isinstance(pet, dict) or str(pet.get("species") or "dog").lower() != "dog":
                continue
            if str(pet.get("status") or "active").lower() != "active" or not str(pet.get("id", "")).isdigit():
                continue
            dogs[int(pet["id"])] = str(pet.get("name") or pet.get("nickname") or f"Dog {pet['id']}")
        return dogs

    # --- tagging --------------------------------------------------------------

    def candidates(self, activities: list[dict], scheduled: bool = False) -> list[dict[str, Any]]:
        """Activities the rules select that still miss a dog (as far as we know).

        scheduled: leave out failures whose next try is not due (or that have no try left),
        like Liftosaur and Zwift; a click always tries them again.
        """
        state = self.store.state
        held = self._held_failures() if scheduled else set()
        held |= set(state.restored)  # Restore removed their dogs: only Re-sync adds them back
        out = []
        for a in activities:
            if int(a["activityId"]) in held:
                continue
            wanted = state.dogs_for(a.get("activityName"))
            done = set(state.tagged.get(str(a["activityId"]), []))
            missing = [d for d in wanted if d not in done]
            if missing:
                out.append({"id": int(a["activityId"]), "name": a.get("activityName") or "",
                            "start": str(a.get("startTimeLocal") or "")[:16], "dogs": missing})
        return out

    def preview(self) -> list[dict[str, Any]]:
        self.p.phase = "preview"
        activities = self.list_activities()
        self.matches = self.candidates(activities)
        self.p.counts["matching"] = len(self.matches)
        self.scan = scan_summary(activities, self.store)
        return self.matches

    def check(self, full: bool = False) -> list[dict[str, Any]]:
        """Scan: find the activities that miss their dog, without changing Garmin: they show up as
        not synced on the Dogs page. full ("Check now", or the first scan): the whole history;
        otherwise back to the last scan that worked, minus a margin."""
        self.p.phase = "check"
        state = self.store.state
        since = None if full else templating.scan_since(state.last_check)
        activities = self.list_activities(since=since.strftime("%Y-%m-%d") if since else None)
        found = self.candidates(activities)
        checked = {int(a["activityId"]) for a in activities}
        # What was not looked at this time stays; what was is replaced by what was found.
        state.pending = found + [p for p in state.pending if p["id"] not in checked]
        state.pending.sort(key=lambda p: p.get("start") or "", reverse=True)
        self.scan = scan_summary(activities, self.store)
        state.last_check = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.p.counts["checked"], self.p.counts["missing"] = len(activities), len(found)
        return found

    def sync_pending(self) -> None:
        """Sync all: add the dogs to every activity found by the checks."""
        todo = list(self.store.state.pending)
        self.p.phase, self.p.total, self.p.done = "tag", len(todo), 0
        try:
            for item in reversed(todo):  # oldest first
                self.tag(item)
                self.p.done += 1
                if self.p.done % 10 == 0:
                    self.store.save()
        finally:
            self.store.state.last_run = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.store.save()

    def scheduled(self, auto: bool) -> None:
        """The automatic run: check the latest activities, and add the dogs when auto-sync is on
        (failures wait for their next try, like Liftosaur and Zwift)."""
        self.check(full=False)
        if not auto:
            return
        held = self._held_failures()
        todo = [p for p in self.store.state.pending if p["id"] not in held]
        self.p.phase, self.p.total, self.p.done = "tag", len(todo), 0
        try:
            for item in reversed(todo):
                self.tag(item)
                self.p.done += 1
        finally:
            self.store.state.last_run = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.store.save()

    def _held_failures(self) -> set[int]:
        now = datetime.now(timezone.utc)
        held = set()
        for e in self.store.state.recent:
            if not e.get("ok", True):
                retry_at = retry.parse(e.get("retry_at"))
                if retry_at is None or retry_at > now:
                    held.add(e["id"])
        return held

    def run(self, full: bool = False, scheduled: bool = False) -> None:
        """Add the missing dogs. full: every activity; otherwise the latest ones only."""
        self.p.phase = "list"
        todo = self.candidates(self.list_activities(max_pages=None if full else RECENT_PAGES), scheduled)
        self.p.phase, self.p.total, self.p.done = "tag", len(todo), 0
        try:
            for item in reversed(todo):  # oldest first
                self.tag(item)
                self.p.done += 1
                if self.p.done % 10 == 0:
                    self.store.save()
        finally:
            self.store.state.last_run = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.store.save()

    def tag(self, item: dict[str, Any]) -> None:
        state = self.store.state
        activity_id = str(item["id"])
        # Handled now: no longer "not synced" (a failure shows as failed instead).
        state.pending = [p for p in state.pending if p["id"] != int(item["id"])]
        try:
            details = self.call(self.g.get_activity, activity_id) or {}
            metadata = dict(details.get("metadataDTO") or {})
            current = dog_ids(metadata)
            wanted = current + [d for d in item["dogs"] if d not in current]
            if wanted != current:
                if len(wanted) > MAX_DOGS_PER_ACTIVITY:
                    self._log(item, ok=False, error=f"Garmin allows {MAX_DOGS_PER_ACTIVITY} dogs at most")
                    return
                metadata.update(hasDog=True, dogProfileIds=wanted)
                self.call(self.g.update_activity, activity_id, {"metadataDTO": metadata})
                self.p.counts["tagged"] = self.p.counts.get("tagged", 0) + 1
                self._log(item, dogs=[d for d in wanted if d not in current])
            else:
                self.p.counts["already"] = self.p.counts.get("already", 0) + 1
                self._forget_failures(item["id"])
            state.tagged[activity_id] = wanted
        except (InterruptedError, StopBackup):
            raise
        except Exception as e:  # noqa: BLE001 - one activity never stops the others
            self._log(item, ok=False, error=str(e))

    def resync(self, activity_id: int) -> None:
        """Re-sync button: add the dogs the rules give to this activity, even after a Restore."""
        state = self.store.state
        if activity_id in state.restored:
            state.restored.remove(activity_id)
        details = self.call(self.g.get_activity, str(activity_id)) or {}
        name = details.get("activityName") or ""
        start = str((details.get("summaryDTO") or {}).get("startTimeLocal") or "")[:16]
        start = start or next((p.get("start") or "" for p in state.pending if p["id"] == activity_id), "")
        dogs = state.dogs_for(name)
        if not dogs:
            raise ValueError(f"No rule gives a dog to “{name}”")
        state.tagged.pop(str(activity_id), None)
        self.tag({"id": activity_id, "name": name, "start": start or self._start_of(activity_id), "dogs": dogs})

    def restore(self, activity_id: int) -> list[int]:
        """Restore button: remove the dogs this app added; the dogs that were there before stay.
        Returns the dogs removed. The activity is then left alone by the automatic runs."""
        state = self.store.state
        added: set[int] = set()
        for e in state.recent:  # newest first: what was added since the last Restore
            if e["id"] != activity_id or not e.get("ok", True):
                continue
            if e.get("restored"):
                break
            added.update(e.get("dogs") or [])
        if not added:
            raise ValueError("This app added no dog to this activity")
        details = self.call(self.g.get_activity, str(activity_id)) or {}
        metadata = dict(details.get("metadataDTO") or {})
        current = dog_ids(metadata)
        keep = [d for d in current if d not in added]
        removed = [d for d in current if d in added]
        if removed:
            metadata.update(hasDog=bool(keep), dogProfileIds=keep)
            self.call(self.g.update_activity, str(activity_id), {"metadataDTO": metadata})
        state.tagged[str(activity_id)] = keep
        if activity_id not in state.restored:
            state.restored.append(activity_id)
        name = details.get("activityName") or next((e["name"] for e in state.recent if e["id"] == activity_id), "")
        self._log({"id": activity_id, "name": name, "start": self._start_of(activity_id), "dogs": removed or sorted(added)},
                  restored=True)
        return removed

    def _start_of(self, activity_id: int) -> str:
        return next((e.get("start") or "" for e in self.store.state.recent if e["id"] == activity_id), "")

    def _log(self, item: dict[str, Any], *, ok: bool = True, dogs: list[int] | None = None,
             error: str | None = None, restored: bool = False) -> None:
        """Keep what happened for the Dogs page and the Activity feed (newest first)."""
        attempts = 1 + max((e.get("attempts", 1) for e in self.store.state.recent
                            if not e.get("ok", True) and e["id"] == item["id"]), default=0)
        self._forget_failures(item["id"])
        now = datetime.now(timezone.utc)
        entry = {"id": item["id"], "name": item["name"], "start": item["start"], "dogs": dogs or item["dogs"],
                 "at": now.isoformat(timespec="seconds"), "ok": ok}
        if restored:
            entry["restored"] = True
        if not ok:
            entry["error"] = (error or "")[:300]
            entry["attempts"] = attempts
            entry["retry_at"] = retry.to_text(retry.next_retry(attempts, now))
            self.p.warn(f"{item['name']} ({item['id']}): {error}")
        log = self.store.state.recent
        log.insert(0, entry)
        del log[LOG_KEPT:]

    def _forget_failures(self, activity_id: int) -> None:
        """A failure no longer needs attention once the activity has its dog."""
        log = self.store.state.recent
        log[:] = [e for e in log if e.get("ok", True) or e["id"] != activity_id]
