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

from ..backup.engine import PacedGarmin, StopBackup
from .store import MAX_DOGS_PER_ACTIVITY, DogsStore

log = logging.getLogger(__name__)

RECENT_PAGES = 1       # automatic runs look at the 100 latest activities
DISCOVER_LIMIT = 40    # activities read when looking for dogs
RECENT_KEPT = 30
PETS_PATH = "/family-api/profile-service/profile/pets"


def dog_ids(metadata: dict | None) -> list[int]:
    ids = (metadata or {}).get("dogProfileIds") or []
    return [int(i) for i in ids if str(i).isdigit()]


class DogTagger(PacedGarmin):
    def __init__(self, client_factory, store: DogsStore, **kwargs):
        super().__init__(client_factory, **kwargs)
        self.store = store
        self.matches: list[dict[str, Any]] = []   # last preview
        self.dog_names: dict[int, str] = {}       # names from the Garmin profile, set by discover()

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

    def candidates(self, activities: list[dict]) -> list[dict[str, Any]]:
        """Activities the rules select that still miss a dog (as far as we know)."""
        state = self.store.state
        out = []
        for a in activities:
            wanted = state.dogs_for(a.get("activityName"))
            done = set(state.tagged.get(str(a["activityId"]), []))
            missing = [d for d in wanted if d not in done]
            if missing:
                out.append({"id": int(a["activityId"]), "name": a.get("activityName") or "",
                            "start": str(a.get("startTimeLocal") or "")[:16], "dogs": missing})
        return out

    def preview(self) -> list[dict[str, Any]]:
        self.p.phase = "preview"
        self.matches = self.candidates(self.list_activities())
        self.p.counts["matching"] = len(self.matches)
        return self.matches

    def run(self, full: bool = False) -> None:
        """Add the missing dogs. full: every activity; otherwise the latest ones only."""
        self.p.phase = "list"
        todo = self.candidates(self.list_activities(max_pages=None if full else RECENT_PAGES))
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
        try:
            details = self.call(self.g.get_activity, activity_id) or {}
            metadata = dict(details.get("metadataDTO") or {})
            current = dog_ids(metadata)
            wanted = current + [d for d in item["dogs"] if d not in current]
            if wanted != current:
                if len(wanted) > MAX_DOGS_PER_ACTIVITY:
                    self.p.warn(f"{item['name']} ({activity_id}): Garmin allows {MAX_DOGS_PER_ACTIVITY} dogs at most")
                    return
                metadata.update(hasDog=True, dogProfileIds=wanted)
                self.call(self.g.update_activity, activity_id, {"metadataDTO": metadata})
                self.p.counts["tagged"] = self.p.counts.get("tagged", 0) + 1
                state.recent.insert(0, {"id": item["id"], "name": item["name"], "start": item["start"],
                                        "dogs": [d for d in wanted if d not in current]})
                del state.recent[RECENT_KEPT:]
            else:
                self.p.counts["already"] = self.p.counts.get("already", 0) + 1
            state.tagged[activity_id] = wanted
        except (InterruptedError, StopBackup):
            raise
        except Exception as e:  # noqa: BLE001 - one activity never stops the others
            self.p.warn(f"{item['name']} ({activity_id}): {e}")
