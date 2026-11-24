"""FastAPI web dashboard for garmin_sync_hub."""

from __future__ import annotations

import contextvars
import html
from contextlib import asynccontextmanager
import logging
import os
import re
import secrets
import threading
import time
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from jinja2 import Environment, FileSystemLoader

from garmin_sync_hub import activity, db, templating
from garmin_sync_hub.auth import (
    SESSION_COOKIE,
    auth_enabled,
    check_password,
    sign_session,
    verify_session,
)
from garmin_sync_hub.config import get_update_existing, is_configured, load_config, save_config
from garmin_sync_hub.liftosaur.sync import sync

logger = logging.getLogger("garmin_sync_hub")

# The request being served, so every page can highlight its place in the navigation.
_current_request: contextvars.ContextVar[Request | None] = contextvars.ContextVar("current_request", default=None)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
FAVICON_ICO_PATH = STATIC_DIR / "favicon.ico"


def _get_cat_names() -> dict[int, str]:
    """Canonical Garmin FIT exercise category names."""
    return {
        0: "Bench Press", 1: "Calf Raise", 2: "Cardio", 3: "Carry", 4: "Chop",
        5: "Core", 6: "Crunch", 7: "Curl", 8: "Deadlift", 9: "Flye",
        10: "Hip Raise", 11: "Hip Stability", 12: "Hip Swing", 13: "Hyperextension",
        14: "Lateral Raise", 15: "Leg Curl", 16: "Leg Raise", 17: "Lunge",
        18: "Olympic Lift", 19: "Plank", 20: "Plyo", 21: "Pull Up", 22: "Push Up",
        23: "Row", 24: "Shoulder Press", 25: "Shoulder Stability", 26: "Shrug",
        27: "Sit Up", 28: "Squat", 29: "Total Body", 30: "Triceps Extension",
        31: "Warm Up", 32: "Run", 33: "Cycling", 36: "Yoga", 38: "Battle Ropes",
        39: "Elliptical", 41: "Indoor Bike", 42: "Indoor Row", 47: "Stair Machine",
        52: "Treadmill", 65534: "Unknown",
    }
_jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)


def _workout_anchor(workout_id: Any) -> str:
    """HTML id of a workout card, used as the #fragment of links to it."""
    return "workout-" + re.sub(r"[^A-Za-z0-9_-]", "-", str(workout_id))


_jinja_env.filters["workout_anchor"] = _workout_anchor
templating.register(_jinja_env)


def _render(template_name: str, **ctx) -> HTMLResponse:
    t = _jinja_env.get_template(template_name)
    ctx.setdefault("auth_enabled", auth_enabled())
    ctx.setdefault("request", _current_request.get())
    return HTMLResponse(t.render(**ctx))


def _has_garmin_tokens(config: dict[str, Any] | None = None) -> bool:
    from garmin_sync_hub.garmin import has_tokens

    try:
        return has_tokens()
    except Exception:
        return False


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _start_background_jobs()
    try:
        yield
    finally:
        _stop_background_jobs()


app = FastAPI(title="garmin-sync-hub", docs_url=None, redoc_url=None, lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

from garmin_sync_hub.zwift.web import ZwiftModule  # noqa: E402

zwift_module = ZwiftModule()
app.include_router(zwift_module.router)

from garmin_sync_hub.backup.web import BackupModule  # noqa: E402

backup_module = BackupModule()
app.include_router(backup_module.router)

from garmin_sync_hub.dogs.web import DogsModule  # noqa: E402
from garmin_sync_hub.dogs.store import MATCH_MODES as DOG_MATCH_MODES  # noqa: E402

dogs_module = DogsModule()
app.include_router(dogs_module.router)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico() -> FileResponse:
    return FileResponse(FAVICON_ICO_PATH, media_type="image/vnd.microsoft.icon")


@app.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    """For Docker and monitoring: the app answers. Needs no sign-in, so it says little.

    Always 200 while the app runs: a Garmin problem is reported, not treated as a crash
    (restarting the container would not fix it).
    """
    from garmin_sync_hub import __version__

    running = {t.name for t in threading.enumerate()}
    return JSONResponse({
        "status": "ok",
        "version": __version__,
        "garmin": _garmin_health()["state"],
        "jobs": {
            "zwift_scan": "zwift-scan" in running,
            "garmin_check": "garmin-check" in running,
            "liftosaur_autosync": _autosync_timer is not None,
        },
    })


# ── Auto-sync state ─────────────────────────────────────────────────────────

_autosync_timer: threading.Timer | None = None
_autosync_lock = threading.Lock()
_sync_executing = threading.Lock()  # Prevents concurrent sync execution
_sync_lock_acquired_at: float = 0  # time.time() when lock was acquired
_SYNC_LOCK_TIMEOUT = 300  # 5 minutes — force-release if exceeded
_last_sync_time: datetime | None = None
_failed_ids: set[str] = set()  # Workouts that failed upload this session (retried next session)
_VALID_AUTOSYNC_INTERVALS = (30, 60, 120, 240, 360, 720, 1440)
_pending_garmin_logins: dict[str, dict[str, Any]] = {}


def _acquire_sync_lock() -> bool:
    """Try to acquire the sync lock. Force-release if held too long (hung sync)."""
    global _sync_lock_acquired_at
    if _sync_executing.acquire(blocking=False):
        _sync_lock_acquired_at = time.time()
        return True
    # Check if the lock has been held too long (hung sync)
    if _sync_lock_acquired_at and (time.time() - _sync_lock_acquired_at) > _SYNC_LOCK_TIMEOUT:
        logger.warning("Sync lock held for >%ds — force-releasing (likely hung)", _SYNC_LOCK_TIMEOUT)
        try:
            _sync_executing.release()
        except RuntimeError:
            pass
        if _sync_executing.acquire(blocking=False):
            _sync_lock_acquired_at = time.time()
            return True
    return False


def _still_unmapped(items: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Drop exercises that were mapped since the list was cached."""
    from garmin_sync_hub.liftosaur.mapper import lookup_exercise

    return [(name, count) for name, count in items if lookup_exercise(name)[0] == 65534]


UNMAPPED_CACHE_KEY = "unmapped_scan"   # {"counts": {name: n}, "at": ISO time}
UNMAPPED_MAX_AGE = timedelta(hours=1)
UNMAPPED_PAGES = 5                      # recent workouts scanned: 5 pages of 10


def _cached_unmapped() -> tuple[list[tuple[str, int]] | None, bool]:
    """(unmapped exercises from the last scan or None, whether that scan is recent)."""
    try:
        _db = db.get_db()
        cached = _db.get_app_config(UNMAPPED_CACHE_KEY)
        if cached and isinstance(cached.get("counts"), dict):
            at = datetime.fromisoformat(cached["at"]) if cached.get("at") else None
            fresh = at is not None and datetime.now(timezone.utc) - at < UNMAPPED_MAX_AGE
            return _still_unmapped(sorted(cached["counts"].items(), key=lambda x: -x[1])), fresh
        legacy = _db.get_app_config("unmapped_exercises")  # before 0.4.1: counts only, no time
        if legacy and isinstance(legacy, dict):
            return _still_unmapped(sorted(legacy.items(), key=lambda x: -x[1])), False
    except Exception:  # noqa: BLE001 - the list is a hint, never block a page
        logger.debug("Could not read the unmapped exercises cache", exc_info=True)
    return None, False


def _save_unmapped(counts: dict[str, int]) -> None:
    db.get_db().set_app_config(UNMAPPED_CACHE_KEY, {
        "counts": counts, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})


def _scan_unmapped() -> list[tuple[str, int]]:
    """Ask Liftosaur for recent workouts (slow: one call per page) and cache what is unmapped."""
    from garmin_sync_hub.liftosaur.liftosaur import LiftosaurClient
    from garmin_sync_hub.liftosaur.mapper import lookup_exercise

    client = LiftosaurClient(api_key=load_config().get("liftosaur_api_key"))
    counts: dict[str, int] = {}
    for pg in range(1, UNMAPPED_PAGES + 1):
        data = client.get_workouts(page=pg, page_size=10)
        for w in data.get("workouts", []):
            for ex in w.get("exercises", []):
                name = ex.get("title") or ex.get("name", "")
                if name and lookup_exercise(name)[0] == 65534:
                    counts[name] = counts.get(name, 0) + 1
        if pg >= data.get("page_count", 1):
            break
    _save_unmapped(counts)
    return sorted(counts.items(), key=lambda x: -x[1])


def _get_unmapped_exercises(fetch: bool = True) -> list[tuple[str, int]]:
    """Unmapped exercises of recent workouts.

    A scan less than an hour old is used as is. With fetch=False the Liftosaur API is never
    called (whatever is cached, or an empty list); otherwise an old or missing list is rescanned.
    """
    cached, fresh = _cached_unmapped()
    if fresh or not fetch:
        return cached or []
    try:
        return _scan_unmapped()
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not scan Liftosaur for unmapped exercises: %s", e)
        return cached or []


def _run_autosync() -> None:
    """Execute a sync and reschedule if still enabled."""
    global _last_sync_time
    config = load_config()
    auto_cfg = config.get("auto_sync", {})
    if not auto_cfg.get("enabled", False):
        return

    if not _acquire_sync_lock():
        logger.info("Auto-sync: skipped — another sync is running")
        _schedule_autosync(auto_cfg.get("interval_minutes", 30))
        return

    logger.info("Auto-sync: running scheduled sync")
    liftosaur_auth_failed = False
    try:
        # Failed workouts wait for their retry time (30 min, 2 h, 8 h, then a click).
        result = sync(limit=10, dry_run=False, skip_ids=db.get_db().failures_on_hold())
    except Exception as e:
        from garmin_sync_hub.liftosaur.liftosaur import LiftosaurAuthError

        if isinstance(e, LiftosaurAuthError):
            logger.error("Auto-sync: Liftosaur API key invalid — disabling auto-sync. %s", e)
            config["auto_sync"]["enabled"] = False
            save_config(config)
            liftosaur_auth_failed = True
        result = {"synced": 0, "skipped": 0, "failed": 1, "error": str(e)}
    finally:
        _sync_executing.release()

    if liftosaur_auth_failed:
        return  # Don't reschedule

    _last_sync_time = datetime.now(timezone.utc)
    _record_sync_log(result, trigger="auto")

    # Reschedule
    _schedule_autosync(auto_cfg.get("interval_minutes", 30))


def _schedule_autosync(interval_minutes: int) -> None:
    """Schedule the next auto-sync run."""
    global _autosync_timer
    with _autosync_lock:
        if _autosync_timer is not None:
            _autosync_timer.cancel()
        _autosync_timer = threading.Timer(interval_minutes * 60, _run_autosync)
        _autosync_timer.daemon = True
        _autosync_timer.start()
        _autosync_next[0] = datetime.now(timezone.utc) + timedelta(minutes=interval_minutes)


def _stop_autosync() -> None:
    """Cancel any pending auto-sync timer."""
    global _autosync_timer
    with _autosync_lock:
        if _autosync_timer is not None:
            _autosync_timer.cancel()
            _autosync_timer = None
        _autosync_next[0] = None


_autosync_next: list[datetime | None] = [None]  # when the pending timer fires


def _parse_autosync_interval(raw: Any, default: int = 120) -> int:
    """Parse the posted auto-sync interval and clamp to known values."""
    try:
        interval = int(raw)
    except (TypeError, ValueError):
        return default
    if interval not in _VALID_AUTOSYNC_INTERVALS:
        return default
    return interval


def _record_sync_log(result: dict, trigger: str = "manual") -> None:
    """Record a sync result to SQLite."""
    db.record_sync_log(
        synced=result.get("synced", 0),
        skipped=result.get("skipped", 0),
        failed=result.get("failed", 0),
        trigger=trigger,
    )


def _record_failure(workout_id: str, title: str, error: str) -> None:
    """Remember a failed workout for the activity feed, without masking the error."""
    try:
        db.record_failure(workout_id, title or workout_id, error)
    except Exception:  # noqa: BLE001
        logger.debug("Could not record the failure of %s", workout_id)


def htmx_notice(message: str, kind: str = "error") -> HTMLResponse:
    """HTMX reply that shows a floating notice (see notify() in base.html) and changes nothing else."""
    return HTMLResponse("", headers={
        "HX-Trigger": json.dumps({"notify": {"message": message, "kind": kind}}),
        "HX-Reswap": "none",
    })


def _with_notice(request: Request, key: str, anchor: str = "") -> str:
    """The page the HTMX request came from, with ?saved=<key> (a notice in base.html)."""
    from urllib.parse import parse_qsl, urlsplit

    current = urlsplit(request.headers.get("HX-Current-URL") or "/")
    query = [(k, v) for k, v in parse_qsl(current.query) if k not in ("saved", "error")]
    query.append(("saved", key))
    return f"{current.path or '/'}?{urlencode(query)}" + (f"#{anchor}" if anchor else "")


def liftosaur_autosync_line(status: dict[str, Any]) -> str:
    """The line under the Liftosaur auto-sync switch (same macro as Zwift)."""
    return templating.render_autosync_line(
        _jinja_env, status["enabled"], status.get("last_at"), status.get("next_at"),
        "workouts sync only when you click “Sync now”.")


def _get_autosync_status() -> dict[str, Any]:
    """Build auto-sync status dict for templates."""
    config = load_config()
    auto_cfg = config.get("auto_sync", {})
    enabled = auto_cfg.get("enabled", False)
    interval = auto_cfg.get("interval_minutes", 30)

    # Times are shown in the viewer's time zone by the templates.
    return {
        "enabled": enabled,
        "interval_minutes": interval,
        "last_at": _last_sync_time,
        "next_at": _autosync_next[0] if enabled else None,
    }


GARMIN_CHECK_MINUTES = 30
_garmin_check_stop = threading.Event()


def _garmin_health_loop() -> None:
    """Check the Garmin connection now, then every GARMIN_CHECK_MINUTES."""
    from garmin_sync_hub.garmin_session import shared_session

    while not _garmin_check_stop.is_set():
        try:
            shared_session().check()
        except Exception:  # noqa: BLE001 - never let the loop die
            logger.exception("Garmin connection check crashed")
        _garmin_check_stop.wait(GARMIN_CHECK_MINUTES * 60)


def _start_background_jobs() -> None:
    """Start the Zwift scan loop, the Garmin check and, if enabled, the Liftosaur auto-sync timer."""
    from garmin_sync_hub.env import obsolete_env_messages

    for message in obsolete_env_messages():
        logger.warning(message)
    zwift_module.start()
    backup_module.start()
    dogs_module.start()
    _garmin_check_stop.clear()
    threading.Thread(target=_garmin_health_loop, name="garmin-check", daemon=True).start()
    config = load_config()
    auto_cfg = config.get("auto_sync", {})
    if auto_cfg.get("enabled", False):
        interval = auto_cfg.get("interval_minutes", 30)
        logger.info("Auto-sync enabled on startup: every %d min", interval)
        _schedule_autosync(interval)


def _stop_background_jobs() -> None:
    _garmin_check_stop.set()
    zwift_module.stop()
    backup_module.stop()
    dogs_module.stop()
    _stop_autosync()


_is_configured_cache: bool | None = None

_SETUP_EXEMPT_PATHS = {
    "/login",
    "/setup",
    "/api/sync-one",
    "/api/validate-liftosaur",
    "/api/garmin/login/start",
    "/api/garmin/login/finish",
    "/api/garmin/import-token-file",
    "/api/garmin/export-token-file",
}

_AUTH_EXEMPT_PATHS = {
    "/login",
    "/setup",
    "/favicon.ico",
    "/api/validate-liftosaur",
    "/api/garmin/login/start",
    "/api/garmin/login/finish",
    "/api/garmin/import-token-file",
    "/api/garmin/export-token-file",
}

@app.middleware("http")
async def check_setup(request: Request, call_next):
    global _is_configured_cache
    _current_request.set(request)
    path = request.url.path
    if path in ("/favicon.ico", "/healthz") or path.startswith("/static"):
        return await call_next(request)

    if auth_enabled() and path not in _AUTH_EXEMPT_PATHS:
        if not verify_session(request.cookies.get(SESSION_COOKIE)):
            if path.startswith("/api/"):
                from starlette.responses import Response
                return Response("Unauthorized", status_code=401)
            return RedirectResponse(f"/login?next={path}")

    if path in _SETUP_EXEMPT_PATHS:
        return await call_next(request)

    # The hub needs a Garmin connection; each module checks its own settings.
    if _is_configured_cache is not True:
        _is_configured_cache = _has_garmin_tokens()
        if not _is_configured_cache:
            return RedirectResponse("/setup")
    return await call_next(request)


# ── Auth pages ───────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Show login form."""
    if not auth_enabled() or verify_session(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse("/")
    error = request.query_params.get("error")
    return HTMLResponse(_jinja_env.get_template("login.html").render(error=error))


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    """Verify password, set session cookie, and redirect."""
    next_url = request.query_params.get("next", "/")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"
    if not check_password(password):
        return HTMLResponse(
            _jinja_env.get_template("login.html").render(error="Wrong password."),
            status_code=401,
        )
    response = RedirectResponse(next_url, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        sign_session(),
        httponly=True,
        samesite="strict",
        max_age=30 * 24 * 3600,
    )
    return response


@app.post("/logout")
async def logout():
    """Clear session cookie and redirect to login."""
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


# ── Pages ────────────────────────────────────────────────────────────────────

def _liftosaur_summary(config: dict) -> dict[str, Any]:
    """Counts and auto-sync state of the Liftosaur module, without slow API calls when possible."""
    configured = bool(config.get("liftosaur_api_key"))
    synced = db.get_synced_count()
    total = 0
    if configured:
        try:
            # Cached count first (instant), Liftosaur API otherwise
            _db = db.get_db()
            cached = _db.get_app_config("workout_total")
            if cached and isinstance(cached, dict):
                total = cached.get("count", 0)
            else:
                from garmin_sync_hub.liftosaur.liftosaur import LiftosaurClient

                total = LiftosaurClient(api_key=config.get("liftosaur_api_key")).get_workout_count()
                _db.set_app_config("workout_total", {"count": total})
        except Exception:
            pass
    sync_log = db.get_sync_log(1)
    auto_sync = _get_autosync_status()
    return {
        "configured": configured,
        "synced": synced,
        "total": total,
        "pending": max(0, total - synced),
        "failed": len(db.get_db().get_failures()),
        "auto_sync": auto_sync,
        "autosync_line": liftosaur_autosync_line(auto_sync),
        "autosync_intervals": _VALID_AUTOSYNC_INTERVALS,
        "last_run": sync_log[0] if sync_log else None,
    }


def _activity_feed() -> list:
    _db = db.get_db()
    dogs_state = dogs_module.store.state
    events = activity.build_feed(_db.get_all_synced(), _db.get_failures(), zwift_module.store.all(),
                                 activity.dogs_events(dogs_state.recent, dogs_state.dog_name),
                                 activity.backup_events(backup_module.state.get("runs") or []))
    # A retry time only means something while the automation that retries is on.
    retrying = {
        "liftosaur": bool(load_config().get("auto_sync", {}).get("enabled")),
        "zwift": zwift_module.cfg.auto_merge and zwift_module.cfg.auto_scan,
        "dogs": dogs_module.store.state.auto,
        "backup": backup_module.settings.auto,
    }
    for e in events:
        if e.retry_at and not retrying.get(e.source):
            e.retry_at = None
    return events


@app.get("/", response_class=HTMLResponse)
async def overview(request: Request, feed: str = "all"):
    config = load_config()
    if feed not in activity.FILTERS:
        feed = "all"
    events = _activity_feed()
    return _render(
        "overview.html",
        garmin_connected=_has_garmin_tokens(config),
        garmin=_garmin_health(),
        lift=_liftosaur_summary(config),
        zwift=zwift_module.summary(),
        backup=backup_module.summary(),
        dogs=dogs_module.summary(),
        feed=feed,
        events=activity.filter_events(events, feed)[:10],
        feed_counts=activity.counts(events),
        request=request,
    )


def _garmin_health() -> dict[str, Any]:
    from garmin_sync_hub.garmin_session import shared_session

    return shared_session().health()


@app.post("/api/garmin/check", response_class=HTMLResponse)
async def api_garmin_check():
    """Check the Garmin connection now; returns the Overview's Garmin card."""
    from garmin_sync_hub.garmin_session import shared_session

    await run_in_threadpool(shared_session().check)
    return _render("partials/garmin_card.html", garmin=_garmin_health())


ACTIVITY_PAGE_SIZE = 50


@app.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request, source: str = "all", page: int = 1):
    if source not in activity.FILTERS:
        source = "all"
    events = _activity_feed()
    shown = activity.filter_events(events, source)
    pages = max(1, -(-len(shown) // ACTIVITY_PAGE_SIZE))
    page = max(1, min(page, pages))
    start = (page - 1) * ACTIVITY_PAGE_SIZE
    return _render(
        "activity.html",
        source=source,
        events=shown[start:start + ACTIVITY_PAGE_SIZE],
        feed_counts=activity.counts(events),
        page=page,
        pages=pages,
        total=len(shown),
        request=request,
    )


# Pages that moved when the app got one section per source.
for _old, _new in (
    ("/dashboard", "/"),
    ("/history", "/activity?source=liftosaur"),
    ("/workouts", "/liftosaur/workouts"),
    ("/mappings", "/liftosaur/mappings"),
    ("/liftosaur", "/liftosaur/workouts"),
):
    app.add_api_route(_old, (lambda target=_new: RedirectResponse(target, status_code=307)),
                      methods=["GET"], include_in_schema=False)



@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    config = load_config()
    return _render(
        "setup.html",
        config=config,
        is_cloud=False,
        use_cloud_inline_garmin_login=False,
        garmin_auth_worker_base_url="",
        garmin_connected=_has_garmin_tokens(config),
        setup_saved=request.query_params.get("saved") == "1",
    )


@app.post("/setup")
async def setup_save(
    liftosaur_api_key: str = Form(""),
    garmin_email: str = Form(""),
    weight_kg: float = Form(80.0),
    birth_year: int = Form(1990),
    sex: str = Form("male"),
):
    config = load_config()
    if liftosaur_api_key:
        config["liftosaur_api_key"] = liftosaur_api_key
    if garmin_email:
        config["garmin_email"] = garmin_email
    config["user_profile"]["weight_kg"] = weight_kg
    config["user_profile"]["birth_year"] = birth_year
    config["user_profile"]["sex"] = sex
    save_config(config)
    if not _has_garmin_tokens(config):
        return RedirectResponse("/setup?saved=1", status_code=303)
    return RedirectResponse("/", status_code=303)


# ── Garmin auth APIs ───────────────────────────────────────────────────────


def _garmin_auth_error(error: Exception) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", str(error))
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned[:200] or "Unknown Garmin authentication error"


@app.post("/api/garmin/login/start")
async def api_garmin_login_start(request: Request):
    body = await request.json()
    email = str(body.get("email", "")).strip()
    password = str(body.get("password", "")).strip()
    if not email or not password:
        return JSONResponse({"error": "Garmin email and password are required"}, status_code=400)

    from garmin_sync_hub.garmin import GarminNeedsMFA, start_login

    config = load_config()
    config["garmin_email"] = email
    save_config(config)
    try:
        result = start_login(email, password)
        return JSONResponse({"ok": True, "status": result["status"], "display_name": result.get("display_name")})
    except GarminNeedsMFA as exc:
        login_id = secrets.token_urlsafe(16)
        _pending_garmin_logins[login_id] = {"state": exc.login_state, "email": email, "created_at": time.time()}
        return JSONResponse({"ok": True, "status": "needs_mfa", "login_id": login_id})
    except Exception as exc:
        logger.warning("Garmin login start failed: %s", exc)
        return JSONResponse({"error": _garmin_auth_error(exc)}, status_code=400)


@app.post("/api/garmin/login/finish")
async def api_garmin_login_finish(request: Request):
    body = await request.json()
    login_id = str(body.get("login_id", "")).strip()
    mfa_code = str(body.get("mfa_code", "")).strip()
    pending = _pending_garmin_logins.get(login_id)
    if not login_id or not pending:
        return JSONResponse({"error": "Garmin login session expired. Start again."}, status_code=400)
    if not mfa_code:
        return JSONResponse({"error": "Verification code is required"}, status_code=400)

    from garmin_sync_hub.garmin import finish_login

    try:
        config = load_config()
        result = finish_login(mfa_code, pending["state"], pending.get("email"))
        _pending_garmin_logins.pop(login_id, None)
        return JSONResponse({"ok": True, "status": result["status"], "display_name": result.get("display_name")})
    except Exception as exc:
        logger.warning("Garmin login finish failed: %s", exc)
        return JSONResponse({"error": _garmin_auth_error(exc)}, status_code=400)


@app.post("/api/garmin/import-token-file")
async def api_garmin_import_token_file(
    token_file: UploadFile = File(...),
    garmin_email: str = Form(""),
):
    from garmin_sync_hub.garmin import save_token_payload

    try:
        payload = json.loads((await token_file.read()).decode())
        if not isinstance(payload, dict) or not payload.get("di_token") or not payload.get("di_refresh_token"):
            raise ValueError("not a garmin-sync-hub Garmin token file (garmin_tokens.json)")
        save_token_payload(payload)
        global _is_configured_cache
        _is_configured_cache = None
        if garmin_email:
            config = load_config()
            config["garmin_email"] = garmin_email
            save_config(config)
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.warning("Garmin token import failed: %s", exc)
        return JSONResponse({"error": _garmin_auth_error(exc)}, status_code=400)


@app.get("/api/garmin/export-token-file")
async def api_garmin_export_token_file():
    from garmin_sync_hub.garmin import token_file_path

    path = token_file_path()
    if not path.exists():
        return JSONResponse({"error": "No local Garmin token file found"}, status_code=404)
    return FileResponse(path, filename=path.name, media_type="application/json")


@app.get("/liftosaur/workouts", response_class=HTMLResponse)
async def workouts_page(request: Request):
    config = load_config()
    workouts = []
    page = int(request.query_params.get("page", 1))
    page_count = 1
    fetch_error = None
    try:
        from garmin_sync_hub.liftosaur.liftosaur import LiftosaurClient

        _db = db.get_db()
        cache_key = f"workouts_page_{page}"

        # Try DB cache first (populated during sync). Fall back to Liftosaur API on miss.
        cached = _db.get_app_config(cache_key)
        if cached:
            workouts_raw = cached.get("workouts", [])
            page_count = cached.get("page_count", 1)
        else:
            data = LiftosaurClient(api_key=config.get("liftosaur_api_key")).get_workouts(page=page, page_size=10)
            workouts_raw = data.get("workouts", [])
            page_count = data.get("page_count", 1)
            _db.set_app_config(cache_key, {"workouts": workouts_raw, "page_count": page_count})

        # Batch check sync status (1 query instead of N)
        workout_ids = [w.get("id", "") for w in workouts_raw]
        synced_map = _db.get_synced_ids(workout_ids) if hasattr(_db, "get_synced_ids") else {
            wid: db.get_garmin_id(wid) for wid in workout_ids if db.is_synced(wid)
        }
        # Check for workouts edited in Liftosaur since last sync
        stale_ids = set(_db.get_stale_synced(workouts_raw))
        sync_actions = _db.get_sync_actions(workout_ids)

        # Get profile for calorie calculation
        profile = config.get("user_profile", {})
        weight_kg = profile.get("weight_kg", 80.0)
        birth_year = profile.get("birth_year", 1990)
        vo2max = profile.get("vo2max", 45.0)

        for w in workouts_raw:
            w["start_time"] = w.get("start_time") or w.get("startTime", "")
            w["end_time"] = w.get("end_time") or w.get("endTime", "")
            if w["id"] in synced_map:
                w["status"] = "uploaded"
                w["sync_action"] = sync_actions.get(w["id"])
                gid = synced_map[w["id"]]
                if gid:
                    w["garmin_match"] = {"garmin_id": gid, "garmin_name": w.get("title", "")}
                if w["id"] in stale_ids:
                    w["edited_since_sync"] = True
            else:
                w["status"] = "pending"

            # Calculate calorie breakdown for display
            try:
                start = w["start_time"]
                end = w["end_time"]
                if start and end:
                    from garmin_sync_hub.liftosaur.fit import _parse_timestamp, _DEFAULT_HR_BPM
                    start_dt = _parse_timestamp(start)
                    end_dt = _parse_timestamp(end)
                    duration_s = (end_dt - start_dt).total_seconds()
                    workout_year = start_dt.year
                    age = workout_year - birth_year
                    # Default HR (no samples available in listing)
                    hr = _DEFAULT_HR_BPM
                    kcal_per_min = (
                        -95.7735 + 0.634 * hr + 0.404 * vo2max
                        + 0.394 * weight_kg + 0.271 * age
                    ) / 4.184
                    total_kcal = max(0, round(max(0.0, kcal_per_min) * (duration_s / 60.0)))
                    duration_min = int(duration_s // 60)
                    w["cal_info"] = {
                        "duration_min": duration_min,
                        "avg_hr": hr,
                        "hr_source": "default 90 bpm",
                        "weight_kg": weight_kg,
                        "age": age,
                        "vo2max": vo2max,
                        "kcal_per_min": round(kcal_per_min, 2),
                        "total_kcal": total_kcal,
                    }
            except Exception:
                pass

        workouts = workouts_raw
    except Exception as e:
        logger.error("Failed to fetch workouts: %s", e)
        fetch_error = str(e)
    hr_fusion = config.get("hr_fusion", {}).get("enabled", True)
    lift = _liftosaur_summary(config)
    return _render("workouts.html", workouts=workouts, hr_fusion_enabled=hr_fusion, page=page, page_count=page_count,
                   fetch_error=fetch_error, lift=lift, flow=_liftosaur_flow(config), request=request)


def _liftosaur_flow(config: dict[str, Any]) -> dict[str, Any]:
    """Settings shown in the "How it works" card of Liftosaur → Workouts."""
    from garmin_sync_hub.liftosaur.fit import DEFAULT_HR_BPM
    from garmin_sync_hub.liftosaur.mapper import EXERCISE_TO_GARMIN, _custom_mappings, _ensure_custom_loaded

    _ensure_custom_loaded()
    update_existing, match_window = get_update_existing(config)
    auto = config.get("auto_sync", {})
    timing = config.get("timing", {})
    return {
        "update_existing": update_existing,
        "match_window": match_window,
        "auto_sync": bool(auto.get("enabled")),
        "interval": auto.get("interval_minutes", 120),
        "mapping_count": len(set(EXERCISE_TO_GARMIN) | set(_custom_mappings)),
        "working_set": timing.get("working_set_seconds", 40),
        "rest_sets": timing.get("rest_between_sets_seconds", 75),
        "default_hr": DEFAULT_HR_BPM,
        "hr_fusion": config.get("hr_fusion", {}).get("enabled", True),
    }


@app.get("/api/workout/{workout_id}/hr", response_class=HTMLResponse)
async def api_workout_hr(request: Request, workout_id: str):
    """Fetch HR data for a workout's matched Garmin activity. Returns JSON for Chart.js.

    Results are cached in SQLite — first load hits Garmin API, subsequent loads are instant.
    """
    from fastapi.responses import JSONResponse

    config = load_config()

    # Check if HR fusion is enabled
    if not config.get("hr_fusion", {}).get("enabled", True):
        return JSONResponse({"error": "HR fusion disabled in settings"}, status_code=404)

    # Check cache first
    cached = db.get_cached_hr(workout_id)
    if cached:
        return JSONResponse(cached)

    try:
        from garmin_sync_hub.liftosaur.liftosaur import LiftosaurClient
        from garmin_sync_hub.garmin import RateLimiter, get_client
        from garmin_sync_hub.liftosaur.matcher import fetch_garmin_activities, match_workouts_to_garmin

        client = LiftosaurClient(api_key=config.get("liftosaur_api_key"))
        data = client.get_workouts(page=1, page_size=10)
        workouts = data.get("workouts", [])
        workout = next((w for w in workouts if w["id"] == workout_id), None)
        if not workout:
            return JSONResponse({"error": "Workout not found"}, status_code=404)

        garmin_client = get_client(config.get("garmin_email"))
        garmin_acts = fetch_garmin_activities(garmin_client, count=1000)
        matches = match_workouts_to_garmin([workout], garmin_acts)

        if workout_id not in matches:
            return JSONResponse({"error": "No matching Garmin activity"}, status_code=404)

        garmin_id = matches[workout_id]["garmin_id"]
        limiter = RateLimiter(delay=1.0)

        # Fetch activity summary for avg/max HR
        details = limiter.call(garmin_client.get_activity, garmin_id)

        # Get workout start/end timestamps to slice daily HR
        from garmin_sync_hub.liftosaur.fit import _parse_timestamp
        w_start = workout.get("start_time") or workout.get("startTime", "")
        w_end = workout.get("end_time") or workout.get("endTime", "")
        start_dt = _parse_timestamp(w_start)
        end_dt = _parse_timestamp(w_end)
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)
        total_duration_s = max(1, (end_ms - start_ms) / 1000)

        # Fetch daily HR data and slice to workout window
        date_str = w_start[:10]
        daily_hr = limiter.call(garmin_client.get_heart_rates, date_str)
        hr_values = daily_hr.get("heartRateValues", []) if isinstance(daily_hr, dict) else []

        hr_samples = []
        for entry in hr_values:
            if isinstance(entry, list) and len(entry) >= 2 and entry[1] is not None:
                ts, bpm = entry[0], entry[1]
                if start_ms - 60000 <= ts <= end_ms + 60000:  # ±1 min buffer
                    secs_from_start = (ts - start_ms) / 1000
                    hr_samples.append({"time": max(0, secs_from_start), "hr": bpm})

        hr_samples.sort(key=lambda x: x["time"])

        # Build exercise segments — proportional to actual workout duration
        exercises = workout.get("exercises", [])
        seg_colors = ["#3b82f6", "#22c55e", "#f97316", "#a855f7", "#ef4444", "#06b6d4", "#eab308", "#ec4899"]
        total_sets = sum(len(ex.get("sets", [])) for ex in exercises)
        segments = []
        cursor = 0.0
        for i, ex in enumerate(exercises):
            n_sets = len(ex.get("sets", []))
            if total_sets > 0:
                ex_duration = total_duration_s * (n_sets / total_sets)
            else:
                ex_duration = total_duration_s / max(1, len(exercises))
            segments.append({
                "name": ex.get("title") or ex.get("name", f"Exercise {i+1}"),
                "start": round(cursor),
                "end": round(cursor + ex_duration),
                "color": seg_colors[i % len(seg_colors)],
            })
            cursor += ex_duration

        result = {
            "hr_samples": hr_samples,
            "segments": segments,
            "garmin_id": garmin_id,
            "garmin_name": matches[workout_id].get("garmin_name", ""),
            "avg_hr": details.get("averageHR") or details.get("summaryDTO", {}).get("averageHR"),
            "max_hr": details.get("maxHR") or details.get("summaryDTO", {}).get("maxHR"),
            "calories": details.get("calories") or details.get("summaryDTO", {}).get("calories"),
        }

        # Cache for instant subsequent loads
        db.cache_hr(workout_id, result)

        return JSONResponse(result)

    except Exception as e:
        logger.error("HR data fetch failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/sync")
async def sync_page(request: Request):
    return RedirectResponse("/")


WORKOUT_LOOKUP_MAX_PAGES = 20


def _find_workout_page(workout_id: str) -> int | None:
    """Page of the Workouts list showing this workout: cache first, then the Liftosaur API."""
    _db = db.get_db()
    if hasattr(_db, "find_cached_workout_page"):
        page = _db.find_cached_workout_page(workout_id)
        if page:
            return page
    from garmin_sync_hub.liftosaur.liftosaur import LiftosaurClient

    api_key = load_config().get("liftosaur_api_key")
    if not api_key:
        return None
    client = LiftosaurClient(api_key=api_key)
    for page in range(1, WORKOUT_LOOKUP_MAX_PAGES + 1):
        cache_key = f"workouts_page_{page}"
        cached = _db.get_app_config(cache_key)
        if not cached:
            cached = client.get_workouts(page=page, page_size=10)
            cached = {"workouts": cached.get("workouts", []), "page_count": cached.get("page_count", 1)}
            _db.set_app_config(cache_key, cached)
        if any(str(w.get("id")) == str(workout_id) for w in cached.get("workouts", [])):
            return page
        if page >= (cached.get("page_count") or 1):
            return None
    return None


@app.get("/liftosaur/workouts/{workout_id}")
async def workout_page(workout_id: str):
    """Open the Workouts list on the page of this workout, scrolled to it."""
    try:
        page = await run_in_threadpool(_find_workout_page, workout_id)
    except Exception as e:  # noqa: BLE001 - fall back to the list
        logger.warning("Could not locate workout %s: %s", workout_id, e)
        page = None
    if page is None:
        return RedirectResponse("/liftosaur/workouts?" + urlencode({"error": "That workout is no longer in your recent Liftosaur history."}), status_code=303)
    return RedirectResponse(f"/liftosaur/workouts?page={page}#{_workout_anchor(workout_id)}", status_code=303)


@app.get("/liftosaur/mappings", response_class=HTMLResponse)
async def mappings_page(request: Request):
    from garmin_sync_hub.liftosaur.mapper import EXERCISE_TO_GARMIN, _custom_mappings, _ensure_custom_loaded

    _ensure_custom_loaded()

    CAT_NAMES = _get_cat_names()

    from garmin_sync_hub.liftosaur.mapper import fit_exercise_name

    def label(cat: int, subcat: int) -> str:
        """What Garmin shows: the category and the exact exercise name."""
        exercise = fit_exercise_name(cat, subcat)
        return CAT_NAMES.get(cat, f"Category {cat}") + (f" › {exercise}" if exercise else "")

    mappings = []
    for name, (cat, subcat) in sorted(EXERCISE_TO_GARMIN.items()):
        mappings.append((name, cat, subcat, label(cat, subcat)))
    for name, (cat, subcat) in sorted(_custom_mappings.items()):
        mappings.append((name, cat, subcat, f"{label(cat, subcat)} (custom)"))

    # Unmapped exercises: from the cache only. Scanning Liftosaur takes several API calls,
    # so an old or missing list is refreshed by /liftosaur/mappings/unmapped after the page shows.
    unmapped, fresh = _cached_unmapped()

    custom_list = [(name, cat, subcat, label(cat, subcat))
                   for name, (cat, subcat) in sorted(_custom_mappings.items())]

    return _render(
        "mappings.html",
        mappings=mappings,
        total=len(mappings),
        custom_count=len(_custom_mappings),
        custom_list=custom_list,
        unmapped=unmapped or [],
        unmapped_stale=not fresh,
    )


@app.get("/liftosaur/mappings/unmapped", response_class=HTMLResponse)
async def mappings_unmapped(request: Request):
    """The unmapped exercises card, rescanning Liftosaur when the cached list is old."""
    unmapped = await run_in_threadpool(_get_unmapped_exercises)
    return _render("partials/unmapped_card.html", unmapped=unmapped, unmapped_stale=False)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    config = load_config()
    unmapped: dict[str, int] = {}
    try:
        # Cached list only: calling the Liftosaur API here made the page slow.
        for name, count in _get_unmapped_exercises(fetch=False):
            unmapped[name] = count
    except Exception:
        pass
    return _render(
        "settings.html",
        config=config,
        zwift_cfg=zwift_module.cfg,
        zwift_connected=zwift_module.zwift.connected(),
        backup_settings=backup_module.settings,
        dogs_state=dogs_module.store.state,
        match_modes=DOG_MATCH_MODES,
        backup_root=backup_module.root,
        rclone_config=backup_module.rclone_config(),
        unmapped=sorted(unmapped.items(), key=lambda x: -x[1]),
        garmin_connected=_has_garmin_tokens(config),
        is_cloud=False,
        use_cloud_inline_garmin_login=False,
        garmin_auth_worker_base_url="",
    )


@app.post("/settings")
async def settings_save(
    liftosaur_api_key: str = Form(""), garmin_email: str = Form(""),
    weight_kg: float = Form(80.0), birth_year: int = Form(1990), sex: str = Form("male"), vo2max: float = Form(45.0),
    working_set_seconds: int = Form(40), warmup_set_seconds: int = Form(25),
    rest_between_sets_seconds: int = Form(75), rest_between_exercises_seconds: int = Form(120),
    hr_fusion_enabled: str = Form("off"),
    update_existing_enabled: str = Form("off"),
    match_window_minutes: int = Form(30),
):
    config = load_config()
    if liftosaur_api_key:
        config["liftosaur_api_key"] = liftosaur_api_key
    if garmin_email:
        config["garmin_email"] = garmin_email
    config["user_profile"].update(weight_kg=weight_kg, birth_year=birth_year, sex=sex, vo2max=vo2max)
    config["timing"].update(
        working_set_seconds=working_set_seconds, warmup_set_seconds=warmup_set_seconds,
        rest_between_sets_seconds=rest_between_sets_seconds,
        rest_between_exercises_seconds=rest_between_exercises_seconds,
    )
    config.setdefault("hr_fusion", {})["enabled"] = hr_fusion_enabled == "on"
    ue = config.setdefault("update_existing", {})
    ue["enabled"] = update_existing_enabled == "on"
    ue["match_window_minutes"] = max(1, min(1440, match_window_minutes))
    save_config(config)

    return RedirectResponse("/settings?saved=settings", status_code=303)


# ── API (HTMX) ──────────────────────────────────────────────────────────────


@app.post("/api/mapping", response_class=HTMLResponse)
async def api_save_mapping(request: Request):
    """Save a custom exercise mapping."""
    form = await request.form()
    exercise_name = form.get("exercise_name", "").strip()
    category = int(form.get("category", 65534))
    subcategory = int(form.get("subcategory", 0))

    if not exercise_name:
        return HTMLResponse('<div class="toast toast-error">Exercise name required</div>')

    # Validate category ID exists
    valid_cats = set(_get_cat_names().keys())
    if category not in valid_cats:
        return HTMLResponse(f'<div class="toast toast-error">Invalid category ID {category}</div>')

    from garmin_sync_hub.liftosaur.mapper import save_custom_mapping
    save_custom_mapping(exercise_name, category, subcategory)

    cat_label = _get_cat_names().get(category, f"Category {category}")
    from garmin_sync_hub.liftosaur.mapper import fit_exercise_name

    exercise = fit_exercise_name(category, subcategory)
    shown = html.escape(f"{cat_label} › {exercise}" if exercise else cat_label)
    return HTMLResponse(f'<div class="toast toast-success">Mapped "{html.escape(exercise_name)}" → {shown} ({category}:{subcategory}). <a href="/liftosaur/mappings">Reload</a></div>')


@app.post("/api/mapping/delete", response_class=HTMLResponse)
async def api_delete_mapping(request: Request):
    """Delete a custom exercise mapping."""
    form = await request.form()
    exercise_name = form.get("exercise_name", "").strip()
    if not exercise_name:
        return HTMLResponse('<div class="toast toast-error">Exercise name required</div>')

    from garmin_sync_hub.liftosaur.mapper import _custom_mappings
    from garmin_sync_hub.liftosaur.mapper import CUSTOM_MAPPINGS_PATH
    try:
        db.get_db().delete_custom_mapping(exercise_name)
    except Exception as e:
        logger.warning("Could not delete mapping %s: %s", exercise_name, e)
    if CUSTOM_MAPPINGS_PATH.exists():
        try:
            data = json.loads(CUSTOM_MAPPINGS_PATH.read_text())
            data.pop(exercise_name, None)
            CUSTOM_MAPPINGS_PATH.write_text(json.dumps(data, indent=2))
        except Exception:
            pass
    _custom_mappings.pop(exercise_name, None)

    return HTMLResponse(f'<div class="toast toast-success">Deleted mapping for "{exercise_name}". <a href="/liftosaur/mappings">Reload</a></div>')


@app.get("/api/validate-liftosaur")
async def api_validate_liftosaur(request: Request):
    """Test a Liftosaur API key. Used by setup page."""
    from fastapi.responses import JSONResponse
    key = request.query_params.get("key", "")
    if not key:
        return JSONResponse({"error": "No key provided"}, status_code=400)
    try:
        from garmin_sync_hub.liftosaur.liftosaur import LiftosaurClient

        count = LiftosaurClient(api_key=key).get_workout_count()
        return JSONResponse({"valid": True, "workout_count": count})
    except Exception as e:
        return JSONResponse({"valid": False, "error": str(e)}, status_code=400)


@app.get("/api/garmin-exercise-names/{category}")
async def api_garmin_exercise_names(category: int):
    """Named exercises of a Garmin category, for the mapping forms: [{"sub": 0, "name": "..."}]."""
    from garmin_sync_hub.liftosaur.mapper import fit_exercise_names

    return JSONResponse([{"sub": sub, "name": name} for sub, name in fit_exercise_names(category)])


@app.get("/api/garmin-categories")
async def api_garmin_categories(request: Request):
    """Return Garmin FIT exercise categories for the mapping UI."""
    from fastapi.responses import JSONResponse
    return JSONResponse({str(k): v for k, v in _get_cat_names().items()})


@app.post("/api/pull-garmin-profile", response_class=HTMLResponse)
async def api_pull_garmin_profile(request: Request):
    """Pull weight, birth date, and gender from Garmin Connect."""
    config = load_config()
    try:
        from garmin_sync_hub.garmin import RateLimiter, get_client

        garmin_client = get_client(config.get("garmin_email"))
        limiter = RateLimiter(delay=1.0)
        raw = limiter.call(garmin_client.get_user_profile)
        profile = raw.get("userData", {}) if isinstance(raw, dict) else {}

        weight = profile.get("weight")  # grams
        birth = profile.get("birthDate")  # "YYYY-MM-DD"
        gender = profile.get("gender")  # "MALE" / "FEMALE"
        vo2max = profile.get("vo2MaxRunning")

        updates = []
        if weight:
            weight_kg = round(weight / 1000, 1)
            config["user_profile"]["weight_kg"] = weight_kg
            updates.append(f"{weight_kg} kg")
        if birth:
            birth_year = int(birth[:4])
            config["user_profile"]["birth_year"] = birth_year
            updates.append(f"born {birth_year}")
        if gender:
            sex = gender.lower()
            config["user_profile"]["sex"] = sex
            updates.append(sex)
        if vo2max:
            config["user_profile"]["vo2max"] = float(vo2max)
            updates.append(f"VO2max {vo2max}")

        if updates:
            save_config(config)
            msg = "Pulled from Garmin: " + ", ".join(updates)
            return HTMLResponse(f'<div class="toast toast-success" style="margin-bottom: 12px;">{msg}</div><script>setTimeout(()=>location.reload(),1500)</script>')
        return HTMLResponse('<div class="toast toast-error" style="margin-bottom: 12px;">No profile data found on Garmin.</div>')
    except Exception as e:
        return HTMLResponse(f'<div class="toast toast-error" style="margin-bottom: 12px;">Failed: {e}</div>')


@app.post("/api/sync", response_class=HTMLResponse)
async def api_sync(request: Request):
    global _last_sync_time

    form = await request.form()
    scope = form.get("scope", "recent")

    # Map scope to sync args
    sync_kwargs: dict = {"dry_run": False}
    if scope == "all":
        sync_kwargs["fetch_all"] = True
    elif scope.isdigit():
        sync_kwargs["limit"] = int(scope)
    else:
        # Time-based: compute "since" date
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        deltas = {
            "24h": timedelta(hours=24),
            "7d": timedelta(days=7),
            "30d": timedelta(days=30),
            "6mo": timedelta(days=180),
            "1y": timedelta(days=365),
        }
        delta = deltas.get(scope, timedelta(hours=24))
        since_dt = now - delta
        sync_kwargs["since"] = since_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        sync_kwargs["fetch_all"] = True  # paginate until we hit the date

    if not _acquire_sync_lock():
        return HTMLResponse('<div class="toast toast-error">Another sync is already running. Please wait.</div>')

    try:
        result = sync(**sync_kwargs)
    except Exception as e:
        result = {"synced": 0, "skipped": 0, "failed": 1, "unmapped": [], "error": str(e)}
    finally:
        _sync_executing.release()
    _last_sync_time = datetime.now(timezone.utc)
    _record_sync_log(result, trigger=f"manual ({scope})")
    return _render("partials/sync_result.html", result=result)


@app.post("/api/sync/{workout_id}", response_class=HTMLResponse)
async def api_sync_single(request: Request, workout_id: str):
    try:
        from garmin_sync_hub.liftosaur.liftosaur import LiftosaurClient
        from garmin_sync_hub.liftosaur.fit import generate_fit
        from garmin_sync_hub.garmin import get_client, rename_activity, set_description, upload_fit, generate_description, find_activity_by_start_time
        from garmin_sync_hub.liftosaur.sync import update_existing_activity_sets
        import tempfile

        # force_upload=true skips dedup (used by re-sync after edit)
        force_upload = request.query_params.get("force") == "1"

        config = load_config()
        client = LiftosaurClient(api_key=config.get("liftosaur_api_key"))
        workout = None
        page = 1
        while True:
            data = client.get_workouts(page=page, page_size=10)
            workout = next((w for w in data.get("workouts", []) if w["id"] == workout_id), None)
            if workout or page >= data.get("page_count", page):
                break
            page += 1
        if not workout:
            return htmx_notice("Workout not found in Liftosaur.")

        garmin_client = get_client(config.get("garmin_email"))
        workout_start = workout.get("start_time")

        # Dedup: check if activity already exists on Garmin (skip if force)
        update_existing, match_window = get_update_existing(config)
        existing_id = None
        if update_existing and not force_upload and workout_start:
            existing_id = find_activity_by_start_time(garmin_client, workout_start, window_minutes=match_window)

        with tempfile.TemporaryDirectory() as tmp:
            fit_path = f"{tmp}/{workout_id}.fit"
            result = generate_fit(workout, hr_samples=None, output_path=fit_path)
            if existing_id:
                aid = existing_id
                logger.info("Activity already on Garmin (%s), updating sets", aid)
                update_existing_activity_sets(garmin_client, aid, workout)
            else:
                upload_result = upload_fit(garmin_client, fit_path, workout_start=workout_start)
                aid = upload_result.get("activity_id")
            if aid:
                rename_activity(garmin_client, aid, workout["title"])
                set_description(garmin_client, aid, generate_description(workout, calories=result.get("calories"), avg_hr=result.get("avg_hr")))
            db.mark_synced(workout_id=workout_id, garmin_activity_id=str(aid) if aid else None, title=workout["title"], calories=result.get("calories"), avg_hr=result.get("avg_hr"), source_updated_at=workout.get("updated_at"), action="updated" if existing_id else "uploaded")

        # Reload the page so the card shows its new status, with a notice.
        return HTMLResponse("", headers={"HX-Redirect": _with_notice(request, "workout", _workout_anchor(workout_id))})
    except Exception as e:
        _record_failure(workout_id, (locals().get("workout") or {}).get("title", ""), str(e))
        return htmx_notice(f"Upload failed: {e}")


@app.post("/api/unsync/{workout_id}")
async def api_unsync(request: Request, workout_id: str):
    """Remove a workout's sync record so it can be re-synced."""
    from fastapi.responses import JSONResponse

    garmin_id = db.get_garmin_id(workout_id)
    deleted = db.unsync(workout_id)
    if not deleted:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)

    # Optionally delete the Garmin activity too
    form = await request.form()
    delete_garmin = form.get("delete_garmin") in ("true", "1", True)
    garmin_deleted = False
    if delete_garmin and garmin_id:
        try:
            config = load_config()
            from garmin_sync_hub.garmin import get_client
            client = get_client(config.get("garmin_email"))
            client.delete_activity(int(garmin_id))
            garmin_deleted = True
            logger.info("Deleted Garmin activity %s for workout %s", garmin_id, workout_id)
        except Exception as e:
            logger.warning("Failed to delete Garmin activity %s: %s", garmin_id, e)

    # Clear cached workout pages so the workouts page reflects the change
    _db = db.get_db()
    for page in range(1, 11):
        _db.set_app_config(f"workouts_page_{page}", {})

    logger.info("Unsynced workout %s (garmin_id=%s, garmin_deleted=%s)", workout_id, garmin_id, garmin_deleted)
    return JSONResponse({"ok": True, "garmin_deleted": garmin_deleted})


@app.post("/api/workouts/{workout_id}/undo")
async def api_undo_workout(request: Request, workout_id: str):
    """Undo an upload: delete the Garmin activity this app created, then mark the workout pending.

    Only for workouts uploaded as a new activity. A watch activity that was updated is never deleted.
    """
    sync_actions = db.get_db().get_sync_actions([workout_id])
    if workout_id not in sync_actions:
        return htmx_notice("This workout is not synced.")
    if sync_actions[workout_id] != "uploaded":
        return htmx_notice("Only a workout uploaded as a new Garmin activity can be undone.")
    garmin_id = db.get_garmin_id(workout_id)
    if garmin_id:
        try:
            from garmin_sync_hub.garmin import get_client

            client = get_client(load_config().get("garmin_email"))
            await run_in_threadpool(client.delete_activity, int(garmin_id))
        except Exception as e:  # noqa: BLE001 - keep the record: nothing was undone
            logger.warning("Undo of %s: could not delete Garmin activity %s: %s", workout_id, garmin_id, e)
            return htmx_notice(f"Garmin did not delete the activity: {e}")
    db.unsync(workout_id)
    _clear_workout_pages()
    logger.info("Undid workout %s (deleted Garmin activity %s)", workout_id, garmin_id)
    return HTMLResponse("", headers={"HX-Redirect": _with_notice(request, "undone", _workout_anchor(workout_id))})


def _clear_workout_pages() -> None:
    """Forget cached workout pages so the list shows the new sync state."""
    _db = db.get_db()
    for page in range(1, 11):
        _db.set_app_config(f"workouts_page_{page}", {})


@app.post("/api/unsync-all")
async def api_unsync_all(request: Request):
    """Remove ALL sync records. Does not delete from Garmin."""
    from fastapi.responses import JSONResponse

    form = await request.form()
    confirm = form.get("confirm", "")
    if confirm != "RESET":
        return JSONResponse({"ok": False, "error": "Send confirm=RESET to proceed"}, status_code=400)

    count = db.unsync_all()

    # Clear cached workout pages
    _db = db.get_db()
    for page in range(1, 11):
        _db.set_app_config(f"workouts_page_{page}", {})

    logger.info("Unsynced all %d workouts", count)
    return JSONResponse({"ok": True, "count": count})


@app.post("/api/toggle-autosync", response_class=HTMLResponse)
async def api_toggle_autosync(request: Request):
    form = await request.form()
    enabled_raw = form.get("enabled", "false")
    enabled = enabled_raw in ("true", "True", "1", True)
    interval = _parse_autosync_interval(form.get("interval", 120))

    config = load_config()
    config.setdefault("auto_sync", {})
    config["auto_sync"]["enabled"] = enabled
    config["auto_sync"]["interval_minutes"] = interval
    save_config(config)

    if enabled:
        _schedule_autosync(interval)
        logger.info("Auto-sync enabled: every %d min", interval)
    else:
        _stop_autosync()
        logger.info("Auto-sync disabled")

    return HTMLResponse(liftosaur_autosync_line(_get_autosync_status()))


@app.post("/api/sync-one")
async def api_sync_one(request: Request):
    """Sync exactly 1 unsynced workout. Returns JSON with status."""
    from fastapi.responses import JSONResponse

    if not _acquire_sync_lock():
        return JSONResponse({"error": "Sync already running", "busy": True})

    from garmin_sync_hub.liftosaur.liftosaur import LiftosaurAuthError

    try:
        return await _do_sync_one(request)
    except LiftosaurAuthError:
        # Raised while listing workouts, before any workout is picked
        return JSONResponse({"synced": 0, "error": "Liftosaur API key is invalid or expired.", "remaining": -1, "done": False}, status_code=401)
    except Exception as e:  # noqa: BLE001 - answer in JSON so the page can show it
        logger.exception("Sync failed")
        return JSONResponse({"synced": 0, "error": f"Sync failed: {str(e)[:200]}", "remaining": -1, "done": False}, status_code=500)
    finally:
        _sync_executing.release()


async def _do_sync_one(request: Request):
    """Inner sync logic, called with _sync_executing lock held."""
    from fastapi.responses import JSONResponse

    config = load_config()
    liftosaur_api_key = config.get("liftosaur_api_key")

    if not liftosaur_api_key:
        return JSONResponse({"error": "Liftosaur API key not configured"}, status_code=400)

    from garmin_sync_hub.liftosaur.liftosaur import LiftosaurAuthError, LiftosaurClient
    from garmin_sync_hub.garmin import get_client, upload_fit, rename_activity, set_description, generate_description
    from garmin_sync_hub.liftosaur.fit import generate_fit
    from garmin_sync_hub.liftosaur.sync import update_existing_activity_sets
    import tempfile

    client = LiftosaurClient(api_key=liftosaur_api_key)

    # Find first unsynced workout, paginating through recent history
    total_count = client.get_workout_count()
    # Cache total for dashboard
    _db = db.get_db()
    _db.set_app_config("workout_total", {"count": total_count})
    synced_count = db.get_synced_count()
    remaining = max(0, total_count - synced_count)

    unsynced = None
    unmapped_found: dict[str, int] = {}
    page = 1
    max_pages = min(10, (remaining // 10) + 2)  # Don't search forever
    while page <= max_pages:
        data = client.get_workouts(page=page, page_size=10)
        workouts = data.get("workouts", [])
        if not workouts:
            break
        # Refresh the workouts-page cache while we already have the data
        _db.set_app_config(
            f"workouts_page_{page}",
            {"workouts": workouts, "page_count": data.get("page_count", 1)},
        )
        for w in workouts:
            if not unsynced and not db.is_synced(w["id"]) and w["id"] not in _failed_ids:
                unsynced = w
            # Track unmapped exercises while we're iterating
            from garmin_sync_hub.liftosaur.mapper import lookup_exercise
            for ex in w.get("exercises", []):
                name = ex.get("title") or ex.get("name", "")
                if name and lookup_exercise(name)[0] == 65534:
                    unmapped_found[name] = unmapped_found.get(name, 0) + 1
        if unsynced:
            break
        if page >= data.get("page_count", page):
            break
        page += 1
    # Update unmapped cache in DB
    if unmapped_found:
        _save_unmapped(unmapped_found)

    if not unsynced:
        return JSONResponse({"synced": 0, "remaining": 0, "done": True})

    # Sync this one workout
    try:
        from garmin_sync_hub.garmin import find_activity_by_start_time
        garmin_client = get_client(config.get("garmin_email"))
        workout_start = unsynced.get("start_time")

        # Dedup: check if this workout already exists on Garmin before uploading.
        # Prevents duplicates when a prior sync uploaded successfully but crashed
        # before marking the workout as synced in the DB.
        update_existing, match_window = get_update_existing(config)
        existing_id = None
        if update_existing and workout_start:
            existing_id = find_activity_by_start_time(garmin_client, workout_start, window_minutes=match_window)

        if existing_id:
            logger.info("Activity already on Garmin (%s), updating sets for %s", existing_id, unsynced["title"])
            aid = existing_id
            update_existing_activity_sets(garmin_client, aid, unsynced)
            # Still generate FIT to get calorie estimate
            with tempfile.TemporaryDirectory() as tmp:
                fit_path = f"{tmp}/{unsynced['id']}.fit"
                result = generate_fit(unsynced, hr_samples=None, output_path=fit_path)
            rename_activity(garmin_client, aid, unsynced["title"])
            desc = generate_description(unsynced, calories=result.get("calories"), avg_hr=result.get("avg_hr"))
            set_description(garmin_client, aid, desc)
        else:
            with tempfile.TemporaryDirectory() as tmp:
                fit_path = f"{tmp}/{unsynced['id']}.fit"
                result = generate_fit(unsynced, hr_samples=None, output_path=fit_path)
                upload_result = upload_fit(garmin_client, fit_path, workout_start=workout_start)
                aid = upload_result.get("activity_id")
                if aid:
                    rename_activity(garmin_client, aid, unsynced["title"])
                    desc = generate_description(unsynced, calories=result.get("calories"), avg_hr=result.get("avg_hr"))
                    set_description(garmin_client, aid, desc)

        db.mark_synced(
            workout_id=unsynced["id"],
            garmin_activity_id=str(aid) if aid else None,
            title=unsynced["title"],
            calories=result.get("calories"),
            avg_hr=result.get("avg_hr"),
            source_updated_at=unsynced.get("updated_at"),
            action="updated" if existing_id else "uploaded",
        )

        remaining = client.get_workout_count() - db.get_synced_count()
        return JSONResponse({"synced": 1, "title": unsynced["title"], "remaining": max(0, remaining), "done": remaining <= 0})
    except Exception as e:
        logger.error("Sync failed for %s: %s", unsynced.get("title", "?"), str(e)[:300])
        err = str(e)

        # Liftosaur API key invalid — hard stop, point to setup
        if isinstance(e, LiftosaurAuthError):
            return JSONResponse({"synced": 0, "error": "Liftosaur API key is invalid or expired. Go to Setup to enter a new key.", "remaining": -1, "done": False}, status_code=401)

        # Auth errors are hard stops — user needs to reconnect
        if "Login failed" in err or "OAuth" in err or "token" in err:
            return JSONResponse({"synced": 0, "error": "Garmin connection expired. Go to Setup to reconnect.", "remaining": -1, "done": False}, status_code=500)

        # EU consent error — hard stop with clear instructions
        if "upload consent" in err.lower() or "EU location" in err:
            return JSONResponse({
                "synced": 0,
                "error": "Garmin requires upload consent. Open connect.garmin.com/modern/settings, scroll to Data, enable Device Upload, then try again.",
                "eu_consent": True,
                "remaining": -1, "done": False
            }, status_code=500)

        # Other upload errors — skip this workout for now, don't mark as synced
        # Track in-memory so we don't retry it in the same sync session
        _failed_ids.add(unsynced["id"])
        _record_failure(unsynced["id"], unsynced.get("title", ""), err)
        remaining = client.get_workout_count() - db.get_synced_count() - len(_failed_ids)
        logger.warning("Skipping failed workout %s (will retry next session), %d remaining", unsynced["title"], remaining)
        return JSONResponse({"synced": 0, "skipped_error": True, "title": unsynced["title"], "remaining": max(0, remaining), "done": remaining <= 0})




def run_server(host: str = "0.0.0.0", port: int = 8000) -> None:
    import uvicorn
    logging.basicConfig(format="%(message)s", level=logging.INFO, force=True)
    logger.info("Starting garmin_sync_hub dashboard at http://localhost:%d", port)
    uvicorn.run(app, host=host, port=port, log_level="warning")
