"""The built-in exercise table must point to real Garmin exercises, the ones its comments name."""
import re
from pathlib import Path

from fit_tool.profile import profile_type as pt

from garmin_sync_hub.liftosaur import mapper

SOURCE = Path(mapper.__file__).read_text(encoding="utf-8")
LINE = re.compile(r'^\s*"(?P<name>[^"]+)":\s*\((?P<cat>\d+),\s*(?P<sub>\d+)\),\s*#\s*(?P<comment>.*)$')
ENTRIES = [m for line in SOURCE[SOURCE.index("EXERCISE_TO_GARMIN"):].splitlines() if (m := LINE.match(line))]


def exercise_enum(category: int):
    cat = pt.ExerciseCategory(category)
    return getattr(pt, "".join(p.capitalize() for p in cat.name.split("_")) + "ExerciseName", None)


def test_no_duplicate_names():
    """A name written twice silently keeps only its last value."""
    names = [m["name"] for m in ENTRIES]
    assert len(names) == len(set(names)), {n for n in names if names.count(n) > 1}


def test_every_entry_is_a_real_garmin_exercise_and_matches_its_comment():
    wrong = []
    for m in ENTRIES:
        cat, sub = int(m["cat"]), int(m["sub"])
        if cat == 65534 or sub == mapper.GENERIC_SUB:  # unknown, or the category alone
            continue
        exercise = exercise_enum(cat)(sub).name.lower()  # ValueError: not a Garmin exercise
        category = pt.ExerciseCategory(cat).name.lower()
        commented = [part.strip().split()[0] for part in m["comment"].split("/")[:2]]
        if commented != [category, exercise]:
            wrong.append(f'{m["name"]}: ({cat}, {sub}) is {category}/{exercise}, comment says {m["comment"]}')
    assert not wrong, "\n".join(wrong)


def test_generic_entries_say_so():
    for m in ENTRIES:
        if int(m["sub"]) == mapper.GENERIC_SUB:
            assert "generic" in m["comment"], m["name"]


def test_old_json_mappings_are_recovered_next_to_the_database(tmp_path, monkeypatch):
    """Before 1.5.2 the JSON file was ignored as soon as one mapping was in the database."""
    import json

    from garmin_sync_hub.db_sqlite import SQLiteDatabase

    database = SQLiteDatabase(tmp_path / "l.db")
    database.save_custom_mapping("Landmine Shoulder Press", 24, mapper.GENERIC_SUB)  # redone on the page
    legacy = tmp_path / "custom_mappings.json"
    legacy.write_text(json.dumps({"Landmine Shoulder Press": [24, 0], "Zorbo Press": [24, 4], "Odd Curl": [7, 3]}))
    monkeypatch.setattr("garmin_sync_hub.db.get_db", lambda: database)
    monkeypatch.setattr(mapper, "CUSTOM_MAPPINGS_PATH", legacy)
    monkeypatch.setattr(mapper, "_custom_mappings", {})
    monkeypatch.setattr(mapper, "_custom_loaded", False)
    monkeypatch.setattr(mapper, "imported_from_file", [])

    mapper._ensure_custom_loaded()
    assert mapper._custom_mappings == {"Landmine Shoulder Press": (24, mapper.GENERIC_SUB),  # the newer one wins
                                       "Zorbo Press": (24, 4), "Odd Curl": (7, 3)}
    assert mapper.imported_from_file == ["Odd Curl", "Zorbo Press"]
    assert database.get_custom_mappings()["Zorbo Press"] == (24, 4)  # now in the database for good
    assert not legacy.exists() and (tmp_path / "custom_mappings.json.imported").exists()  # kept as a backup


def test_history_follows_the_cursor(monkeypatch):
    from garmin_sync_hub.liftosaur import liftosaur

    pages = {None: {"records": ["a", "b"], "hasMore": True, "nextCursor": "c2"},
             "c2": {"records": ["c"], "hasMore": False}}
    calls = []

    def history(self, limit=50, cursor=None, **kw):
        calls.append(cursor)
        return pages[cursor]
    monkeypatch.setattr(liftosaur.LiftosaurClient, "_get_history", history)
    monkeypatch.setattr(liftosaur, "parse_history_record", lambda r: {"id": r})
    client = liftosaur.LiftosaurClient(api_key="k")
    assert [w["id"] for w in client.iter_history()] == ["a", "b", "c"] and calls == [None, "c2"]
