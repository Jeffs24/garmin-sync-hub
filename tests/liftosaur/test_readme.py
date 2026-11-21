from pathlib import Path


def test_readme_credits_reused_projects():
    readme = Path(__file__).resolve().parents[2] / "README.md"
    content = readme.read_text(encoding="utf-8")

    for url in (
        "https://github.com/drkostas/hevy2garmin",
        "https://github.com/Zettt/liftosaur2garmin",
    ):
        assert url in content
    assert "Konstantinos Georgiou" in content
    assert "Andreas Zeitler" in content
    assert "drkostas/liftosaur2garmin" not in content
