"""Dogs page: find your dogs, rules by activity name, apply them to past and new activities."""
from __future__ import annotations

import html
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader

from .. import templating
from ..auth import auth_enabled
from ..backup.engine import AnyEvent, Progress, StopBackup
from ..backup.settings import backup_dir
from ..garmin_session import shared_session
from .engine import DogTagger
from .store import DOG_INTERVALS, MATCH_MODES, DogsStore

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = templating.register(Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True))

JOBS = {"discover": "Looking for your dogs", "preview": "Finding matching activities",
        "apply": "Adding dogs to past activities", "auto": "Adding dogs to new activities"}


def _render(name: str, request: Request | None, **ctx) -> HTMLResponse:
    ctx.setdefault("auth_enabled", auth_enabled())
    return HTMLResponse(_env.get_template(name).render(request=request, **ctx))


class DogsModule:
    def __init__(self, store: DogsStore | None = None):
        self.store = store or DogsStore()
        self.garmin = shared_session()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._wake = threading.Event()
        self.job: dict = {"kind": None, "progress": None, "result": None, "error": None, "finished": None}
        self.matches: list[dict] = []
        self.router = self._build_router()

    # --- jobs -----------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._lock.locked()

    def _tagger(self, progress: Progress) -> DogTagger:
        return DogTagger(self.garmin.client, self.store, stop=AnyEvent(self._stop, self._cancel), progress=progress)

    def run_job(self, kind: str) -> bool:
        """discover | preview | apply | auto (blocking). False when a job is already running."""
        if not self._lock.acquire(blocking=False):
            return False
        self._cancel.clear()
        progress = Progress()
        self.job = {"kind": kind, "progress": progress, "result": None, "error": None, "finished": None}
        try:
            tagger = self._tagger(progress)
            if kind == "discover":
                found = tagger.discover(backup_dir())
                new = [d for d in found if str(d) not in self.store.state.dogs]
                for dog in new:
                    self.store.set_dog(dog, f"Dog {dog}")
                self.job["result"] = {"found": found, "new": new}
            elif kind == "preview":
                self.matches = tagger.preview()
                self.job["result"] = {"matching": len(self.matches)}
            else:
                tagger.run(full=kind == "apply")
                self.matches = []
                self.job["result"] = dict(progress.counts)
            self.store.state.last_error = None
        except InterruptedError:
            self.job["error"] = "Stopped. Run it again to continue: activities already done are skipped."
        except StopBackup as e:
            self.job["error"] = str(e)
        except Exception as e:  # noqa: BLE001
            log.exception("Dogs job %s failed", kind)
            self.job["error"] = f"Failed: {e}"
        finally:
            if self.job["error"] and kind == "auto":
                self.store.state.last_error = self.job["error"]
            self.store.save()
            self.job["finished"] = datetime.now(timezone.utc)
            self._lock.release()
        return True

    def start_job(self, kind: str) -> bool:
        if self.running:
            return False
        threading.Thread(target=self.run_job, args=(kind,), name=f"dogs-{kind}", daemon=True).start()
        return True

    # --- schedule -----------------------------------------------------------

    def _due(self) -> datetime | None:
        s = self.store.state
        if not s.auto or not s.rules:
            return None
        last = datetime.fromisoformat(s.last_run) if s.last_run else None
        return (last + timedelta(minutes=s.interval_minutes)) if last else datetime.now(timezone.utc)

    def _loop(self) -> None:
        while not self._stop.is_set():
            due = self._due()
            timeout = None if due is None else max(0.0, (due - datetime.now(timezone.utc)).total_seconds())
            if timeout == 0:
                if self.garmin.has_tokens() and not self.running:
                    self.run_job("auto")
                if self._due() and self._due() <= datetime.now(timezone.utc):
                    self._stop.wait(300)  # the run failed before recording itself: retry later
                continue
            if self._wake.wait(timeout):
                self._wake.clear()

    def start(self) -> None:
        self._stop.clear()
        threading.Thread(target=self._loop, name="dogs-schedule", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def summary(self) -> dict:
        s = self.store.state
        return {"dogs": len(s.dogs), "rules": len(s.rules), "tagged": sum(1 for v in s.tagged.values() if v),
                "auto": s.auto, "last_error": s.last_error}

    # --- routes -------------------------------------------------------------

    def _build_router(self) -> APIRouter:
        router = APIRouter(prefix="/dogs")
        m = self

        def ctx() -> dict:
            s = m.store.state
            last = datetime.fromisoformat(s.last_run) if s.last_run else None
            line = templating.render_autosync_line(_env, s.auto and bool(s.rules), last, m._due(),
                                                   "dogs are added only from “Preview past activities”."
                                                   if s.rules else "add a rule first.")
            return {"s": s, "job": m.job, "jobs": JOBS, "running": m.running, "matches": m.matches,
                    "match_modes": MATCH_MODES, "intervals": DOG_INTERVALS, "autosync_line": line,
                    "garmin_connected": m.garmin.has_tokens()}

        def back(error: str | None = None, saved: bool = False) -> RedirectResponse:
            query = urlencode({"error": error}) if error else ("saved=dogs" if saved else "")
            return RedirectResponse("/dogs" + (f"?{query}" if query else ""), status_code=303)

        @router.get("", response_class=HTMLResponse)
        def page(request: Request) -> HTMLResponse:
            return _render("dogs.html", request, **ctx())

        @router.get("/status", response_class=HTMLResponse)
        def status(request: Request) -> HTMLResponse:
            return _render("partials/dogs_status.html", request, **ctx())

        @router.post("/run/{kind}", response_class=HTMLResponse)
        def run(request: Request, kind: str) -> HTMLResponse:
            if kind not in ("discover", "preview", "apply"):
                raise HTTPException(404)
            if not m.garmin.has_tokens():
                raise HTTPException(409, "Connect Garmin first (Settings → Garmin).")
            if kind != "discover" and not m.store.state.rules:
                raise HTTPException(409, "Add a rule first.")
            m.start_job(kind)
            return _render("partials/dogs_status.html", request, **ctx())

        @router.post("/stop", response_class=HTMLResponse)
        def stop_job(request: Request) -> HTMLResponse:
            m._cancel.set()
            return _render("partials/dogs_status.html", request, **ctx())

        @router.post("/dog")
        def save_dog(dog_id: str = Form(...), name: str = Form("")) -> RedirectResponse:
            try:
                m.store.set_dog(dog_id, name)
            except ValueError as e:
                return back(str(e))
            return back(saved=True)

        @router.post("/dog/{dog_id}/delete")
        def delete_dog(dog_id: str) -> RedirectResponse:
            try:
                m.store.remove_dog(dog_id)
            except ValueError as e:
                return back(str(e))
            return back(saved=True)

        @router.post("/rules")
        def add_rule(name: str = Form(""), dog_id: str = Form(""), match: str = Form("exact")) -> RedirectResponse:
            try:
                m.store.add_rule(name, dog_id, match)
            except ValueError as e:
                return back(str(e))
            m.matches = []
            m._wake.set()
            return back(saved=True)

        @router.post("/rules/{index}/delete")
        def delete_rule(index: int) -> RedirectResponse:
            try:
                m.store.remove_rule(index)
            except ValueError as e:
                return back(str(e))
            m.matches = []
            return back(saved=True)

        @router.post("/autosync", response_class=HTMLResponse)
        def autosync(enabled: str = Form("false"), interval: str = Form("")) -> HTMLResponse:
            try:
                m.store.set_auto(enabled in ("true", "True", "1", "on"), int(interval) if interval else None)
            except ValueError as e:
                return HTMLResponse(f"<span class='state-failed'>{html.escape(str(e))}</span>")
            m._wake.set()
            return HTMLResponse(ctx()["autosync_line"])

        for path in ("/dog", "/rules", "/stop", "/autosync"):
            router.add_api_route(path, lambda: RedirectResponse("/dogs", status_code=303),
                                 methods=["GET"], include_in_schema=False)
        return router
