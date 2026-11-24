"""Dogs, rules and what was tagged, saved in data/dogs.json."""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..paths import data_dir

log = logging.getLogger(__name__)

MATCH_MODES = {"exact": "is", "contains": "contains", "starts": "starts with"}
MAX_DOGS_PER_ACTIVITY = 10  # Garmin's limit
DOG_INTERVALS = (30, 60, 120, 360, 720, 1440)


@dataclass
class Rule:
    name: str
    dog_id: int
    match: str = "exact"   # exact | contains | starts (case is ignored)

    def matches(self, activity_name: str | None) -> bool:
        a, r = (activity_name or "").strip().casefold(), self.name.strip().casefold()
        if not r:
            return False
        return {"exact": a == r, "contains": r in a, "starts": a.startswith(r)}.get(self.match, False)


@dataclass
class DogsState:
    dogs: dict[str, str] = field(default_factory=dict)       # dog profile id -> name given here
    rules: list[Rule] = field(default_factory=list)
    scan: bool = True     # auto-scan: look for activities missing their dog every interval_minutes
    auto: bool = False    # auto-sync: add the dogs a scan finds without a click
    interval_minutes: int = 60
    tagged: dict[str, list[int]] = field(default_factory=dict)  # activity id -> dogs set by us
    restored: list[int] = field(default_factory=list)  # activities whose dogs Restore removed: never re-added on its own
    # Activities the rules give a dog to that do not have it yet ("not synced"), newest first:
    # {id, name, start, dogs}. Found by every check, even with auto-sync off.
    pending: list[dict[str, Any]] = field(default_factory=list)
    last_check: str | None = None
    # Dogs added and failures, newest first: {id, name, start, dogs, at, ok, error}
    recent: list[dict[str, Any]] = field(default_factory=list)
    last_run: str | None = None
    last_error: str | None = None

    def dog_name(self, dog_id: int | str) -> str:
        return self.dogs.get(str(dog_id)) or f"Dog {dog_id}"

    def dogs_for(self, activity_name: str | None) -> list[int]:
        """Dogs the rules give to an activity with this name, in rule order."""
        found: list[int] = []
        for rule in self.rules:
            if rule.matches(activity_name) and rule.dog_id not in found:
                found.append(rule.dog_id)
        return found


class DogsStore:
    """Thread-safe access to data/dogs.json."""

    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "dogs.json"
        self._lock = threading.RLock()
        self.state = self._load()

    def _load(self) -> DogsState:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return DogsState()
        except (OSError, ValueError) as e:
            log.warning("Ignoring %s: %s", self.path, e)
            return DogsState()
        rules = [Rule(**r) for r in raw.pop("rules", []) if isinstance(r, dict)]
        known = {k: v for k, v in raw.items() if k in DogsState.__dataclass_fields__}
        return DogsState(rules=rules, **known)

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".part")
            tmp.write_text(json.dumps(asdict(self.state), indent=2))
            tmp.replace(self.path)

    # --- edits from the page --------------------------------------------------

    def set_dog(self, dog_id: Any, name: str) -> None:
        dog = _dog_id(dog_id)
        with self._lock:
            self.state.dogs[str(dog)] = (name or "").strip()[:60] or f"Dog {dog}"
            self.save()

    def remove_dog(self, dog_id: Any) -> None:
        dog = _dog_id(dog_id)
        with self._lock:
            self.state.dogs.pop(str(dog), None)
            self.state.rules = [r for r in self.state.rules if r.dog_id != dog]
            self.save()

    def add_rule(self, name: str, dog_id: Any, match: str = "exact") -> None:
        name = (name or "").strip()
        if not name:
            raise ValueError("Enter the activity name to look for")
        if match not in MATCH_MODES:
            raise ValueError("Unknown match mode")
        dog = _dog_id(dog_id)
        with self._lock:
            if str(dog) not in self.state.dogs:
                raise ValueError("Choose one of your dogs")
            if any(r.name.casefold() == name.casefold() and r.dog_id == dog and r.match == match
                   for r in self.state.rules):
                raise ValueError("This rule already exists")
            self.state.rules.append(Rule(name=name[:100], dog_id=dog, match=match))
            self.save()

    def remove_rule(self, index: int) -> None:
        with self._lock:
            if not 0 <= index < len(self.state.rules):
                raise ValueError("Unknown rule")
            del self.state.rules[index]
            self.save()

    def set_scan(self, enabled: bool, interval: int | None = None) -> None:
        with self._lock:
            self.state.scan = enabled
            if interval is not None:
                if interval not in DOG_INTERVALS:
                    raise ValueError("Unknown interval")
                self.state.interval_minutes = interval
            self.save()

    def set_auto(self, enabled: bool, interval: int | None = None) -> None:
        with self._lock:
            self.state.auto = enabled
            if interval is not None:
                if interval not in DOG_INTERVALS:
                    raise ValueError("Unknown interval")
                self.state.interval_minutes = interval
            self.save()


def _dog_id(value: Any) -> int:
    try:
        dog = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("A dog id is a number") from None
    if dog <= 0:
        raise ValueError("A dog id is a positive number")
    return dog
