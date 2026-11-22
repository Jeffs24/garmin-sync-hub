import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from garmin_sync_hub.garmin_session import GarminSession, _jwt_expiry


def jwt(exp: datetime) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": int(exp.timestamp())}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{body}.sig"


NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session(tmp_path):
    return GarminSession(tmp_path)


def write_tokens(s: GarminSession, refresh_exp: datetime | None) -> None:
    s.token_dir.mkdir(parents=True, exist_ok=True)
    s.token_file.write_text(json.dumps({
        "di_token": jwt(NOW + timedelta(hours=1)),
        "di_refresh_token": jwt(refresh_exp) if refresh_exp else "opaque-token",
        "di_client_id": "x",
    }))


def test_jwt_expiry_ignores_garbage():
    assert _jwt_expiry("not-a-jwt") is None
    assert _jwt_expiry(None) is None
    assert _jwt_expiry("a.!!!.c") is None
    assert _jwt_expiry(jwt(NOW)) == NOW


def test_health_states(session):
    assert session.health(NOW)["state"] == "disconnected"

    write_tokens(session, NOW + timedelta(days=60))
    h = session.health(NOW)
    assert h["state"] == "ok" and h["refresh_expires"] == NOW + timedelta(days=60)

    write_tokens(session, NOW + timedelta(days=3))
    h = session.health(NOW)
    assert h["state"] == "warning" and "3 days" in h["message"]

    write_tokens(session, NOW - timedelta(days=1))
    assert session.health(NOW)["state"] == "error"

    write_tokens(session, None)  # refresh token not readable: no expiry warning
    assert session.health(NOW)["state"] == "ok"


def test_check_records_success_and_failure(session, monkeypatch):
    write_tokens(session, NOW + timedelta(days=60))

    class Ok:
        def get_user_profile(self):
            return {}

    class Refused:
        def get_user_profile(self):
            raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr(session, "client", lambda: Ok())
    assert session.check() is True and session.health()["state"] == "ok"

    monkeypatch.setattr(session, "client", lambda: Refused())
    assert session.check() is False
    h = session.health()
    assert h["state"] == "error" and h["last_error"] == "401 Unauthorized"

    monkeypatch.setattr(session, "client", lambda: Ok())
    session.check()
    assert session.health()["state"] == "ok"  # a later success clears the error


def test_overview_card_and_check_now(monkeypatch):
    from fastapi.testclient import TestClient

    from garmin_sync_hub import server
    from garmin_sync_hub.garmin_session import shared_session

    s = shared_session()
    monkeypatch.setattr(s, "has_tokens", lambda: True)
    monkeypatch.setattr(s, "token_expiry", lambda: {"access": None, "refresh": datetime.now(timezone.utc) + timedelta(days=2), "saved": None})
    monkeypatch.setattr(s, "check", lambda: True)
    monkeypatch.setattr(server, "_has_garmin_tokens", lambda config=None: True)
    monkeypatch.setattr(server, "is_configured", lambda: True, raising=False)
    server._is_configured_cache = None
    with TestClient(server.app) as c:
        page = c.get("/").text
        assert "Expires soon" in page and "Reconnect" in page and 'hx-post="/api/garmin/check"' in page
        card = c.post("/api/garmin/check").text
        assert card.strip().startswith("<div") and 'id="garmin-card"' in card
