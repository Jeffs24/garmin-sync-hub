from pathlib import Path


def test_readme_credits_liftosaur2garmin():
    readme = Path(__file__).resolve().parents[2] / "README.md"
    content = readme.read_text(encoding="utf-8")

    assert "https://github.com/drkostas/liftosaur2garmin" in content
    assert "Konstantinos Georgiou" in content
