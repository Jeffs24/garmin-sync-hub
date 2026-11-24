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
