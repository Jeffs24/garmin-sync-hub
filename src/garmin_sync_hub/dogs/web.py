"""Dogs page: find your dogs, rules by activity name, apply them to past and new activities."""
from __future__ import annotations

import html
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader

from .. import templating
from ..auth import auth_enabled
from ..backup.engine import AnyEvent, Progress, StopBackup, find_activity_file
from ..backup.settings import backup_dir
from ..garmin_session import shared_session
from .engine import DogTagger
from .store import DOG_INTERVALS, MATCH_MODES, DogsStore

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = templating.register(Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True))

# Filter tabs of the Dogs history, like Liftosaur and Zwift: key -> label.
HISTORY_FILTERS = {"all": "All", "pending": "Not synced", "added": "Synced", "failed": "Failed", "restored": "Restored"}
PAGE_SIZE = 10


def history(recent: list[dict], pending: list[dict] = ()) -> list[dict]:
    """One entry per activity, with its state for the tabs: what the log says last (newest
    first), then the activities found by the checks that still miss their dog ("not synced")."""
    seen: set = set()
    rows = []
    for e in recent:
        if e.get("id") in seen:
            continue
        seen.add(e.get("id"))
        state = "failed" if not e.get("ok", True) else "restored" if e.get("restored") else "added"
        rows.append({**e, "state": state})
    waiting = [{**p, "state": "pending"} for p in pending if p.get("id") not in seen]
    return waiting + rows


def _back_with_notice(request: Request, key: str, anchor: str) -> str:
    """The page the HTMX request came from (keeps its tab and page), with a notice."""
    from urllib.parse import parse_qsl, urlsplit

    current = urlsplit(request.headers.get("HX-Current-URL") or "/dogs")
    query = [(k, v) for k, v in parse_qsl(current.query) if k not in ("saved", "error")] + [("saved", key)]
    return f"{current.path or '/dogs'}?{urlencode(query)}#{anchor}"


JOBS = {"discover": "Looking for your dogs", "check": "Checking your whole history",
        "sync": "Adding the dogs", "scheduled": "Checking your latest activities",
        # before 1.6
        "preview": "Finding matching activities", "apply": "Adding dogs to past activities",
        "auto": "Adding dogs to new activities"}
# Buttons of before 1.6, still posted by old pages: Preview past activities, Add to N, Sync now.
OLD_KINDS = {"preview": "check", "apply": "sync", "auto": "check"}


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
                    self.store.set_dog(dog, tagger.dog_names.get(dog) or f"Dog {dog}")
                for dog, name in tagger.dog_names.items():  # replace a placeholder name, keep a chosen one
                    if self.store.state.dogs.get(str(dog)) == f"Dog {dog}":
                        self.store.set_dog(dog, name)
                self.job["result"] = {"found": found, "new": new,
                                      "unnamed": [d for d in new if d not in tagger.dog_names]}
            elif kind == "preview":
                self.matches = tagger.preview()
                self.job["result"] = {"matching": len(self.matches), **tagger.scan}
            elif kind == "check":
                found = tagger.check(full=True)
                self.job["result"] = {"missing": len(found), **tagger.scan}
            elif kind == "sync":
                tagger.sync_pending()
                self.job["result"] = dict(progress.counts)
            elif kind == "scheduled":
                tagger.scheduled(auto=self.store.state.auto)
                self.job["result"] = dict(progress.counts)
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
            if self.job["error"] and kind in ("auto", "scheduled"):
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
        """Next automatic check: always once there are rules (auto-sync only decides whether
        the dogs are added too)."""
        s = self.store.state
        if not s.rules:
            return None
        times = [datetime.fromisoformat(t) for t in (s.last_run, s.last_check) if t]
        last = max(times) if times else None
        return (last + timedelta(minutes=s.interval_minutes)) if last else datetime.now(timezone.utc)

    def _loop(self) -> None:
        while not self._stop.is_set():
            due = self._due()
            timeout = None if due is None else max(0.0, (due - datetime.now(timezone.utc)).total_seconds())
            if timeout == 0:
                if self.garmin.has_tokens() and not self.running:
                    self.run_job("scheduled")
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
        """Counts for the Overview card."""
        s = self.store.state
        return {
            "dogs": len(s.dogs), "rules": len(s.rules), "dog_names": list(s.dogs.values()),
            "tagged": sum(1 for v in s.tagged.values() if v),
            "pending": len(s.pending),
            "failed": sum(1 for e in s.recent if not e.get("ok", True)),
            "auto": s.auto and bool(s.rules), "interval_minutes": s.interval_minutes,
            "next_run": self._due(), "running": self.running, "last_error": s.last_error,
            "last_run": datetime.fromisoformat(s.last_run) if s.last_run else None,
        }

    def autosync_line(self) -> str:
        """The line under the auto-sync switch (Dogs page and Settings → Dogs)."""
        s = self.store.state
        last = datetime.fromisoformat(s.last_run) if s.last_run else None
        return templating.render_autosync_line(
            _env, s.auto and bool(s.rules), last, self._due(),
            f"activities missing their dog are still found every {templating.interval_label(s.interval_minutes)} "
            "and wait in “Not synced” for a click." if s.rules else "add a rule first (Settings → Dogs).")

    # --- routes -------------------------------------------------------------

    def _build_router(self) -> APIRouter:
        router = APIRouter(prefix="/dogs")
        m = self

        def ctx() -> dict:
            return {"s": m.store.state, "job": m.job, "jobs": JOBS, "running": m.running, "matches": m.matches,
                    "match_modes": MATCH_MODES, "intervals": DOG_INTERVALS, "autosync_line": m.autosync_line(),
                    "garmin_connected": m.garmin.has_tokens()}

        def back(error: str | None = None, saved: bool = False, to: str = "") -> RedirectResponse:
            """Back to the page the form was on: Settings → Dogs, or the Dogs page."""
            query = urlencode({"error": error}) if error else ("saved=dogs" if saved else "")
            base, anchor = ("/settings", "#dogs") if to == "settings" else ("/dogs", "")
            return RedirectResponse(base + (f"?{query}" if query else "") + anchor, status_code=303)

        @router.get("", response_class=HTMLResponse)
        def page(request: Request) -> HTMLResponse:
            rows = history(m.store.state.recent, m.store.state.pending)
            status = request.query_params.get("status", "all")
            if status not in HISTORY_FILTERS:
                status = "all"
            counts = {k: (len(rows) if k == "all" else sum(1 for r in rows if r["state"] == k)) for k in HISTORY_FILTERS}
            chosen = rows if status == "all" else [r for r in rows if r["state"] == status]
            try:
                page_no = max(1, int(request.query_params.get("page", 1)))
            except ValueError:
                page_no = 1
            page_count = max(1, -(-len(chosen) // PAGE_SIZE))
            page_no = min(page_no, page_count)
            return _render("dogs.html", request, **ctx(), history=chosen[(page_no - 1) * PAGE_SIZE: page_no * PAGE_SIZE],
                           status_filter=status, filter_labels=HISTORY_FILTERS, filter_counts=counts,
                           page=page_no, page_count=page_count, total_history=len(rows))

        def one_activity(request: Request, activity_id: int, action: str) -> HTMLResponse:
            """Re-sync or Restore of one activity, right away (a few Garmin requests)."""
            if not m.garmin.has_tokens():
                return HTMLResponse("", headers=templating.notice_headers("Connect Garmin first (Settings → Garmin).", "error"))
            if not m._lock.acquire(blocking=False):
                return HTMLResponse("", headers=templating.notice_headers("A dogs job is running: try again when it is done.", "info"))
            try:
                tagger = m._tagger(Progress())
                if action == "resync":
                    tagger.resync(activity_id)
                else:
                    tagger.restore(activity_id)
            except (ValueError, StopBackup) as e:
                return HTMLResponse("", headers=templating.notice_headers(str(e), "error"))
            except Exception as e:  # noqa: BLE001
                log.exception("Dogs %s of %s failed", action, activity_id)
                return HTMLResponse("", headers=templating.notice_headers(f"Garmin did not take it: {e}", "error"))
            finally:
                m.store.save()
                m._lock.release()
            key = "dogs-added" if action == "resync" else "dogs-removed"
            return HTMLResponse("", headers={"HX-Redirect": _back_with_notice(request, key, f"dog-{activity_id}")})

        @router.post("/activities/{activity_id}/resync", response_class=HTMLResponse)
        def resync_activity(request: Request, activity_id: int) -> HTMLResponse:
            return one_activity(request, activity_id, "resync")

        @router.post("/activities/{activity_id}/restore", response_class=HTMLResponse)
        def restore_activity(request: Request, activity_id: int) -> HTMLResponse:
            return one_activity(request, activity_id, "restore")

        @router.get("/activities/{activity_id}/fit")
        def activity_file(activity_id: int):
            """The activity's original file: the backup copy, else downloaded from Garmin."""
            saved = find_activity_file(backup_dir(), activity_id)
            if saved:
                return FileResponse(saved, filename=saved.name, media_type="application/octet-stream")
            if not m.garmin.has_tokens():
                raise HTTPException(409, "Connect Garmin first (Settings → Garmin).")
            try:
                _raw, fit = m.garmin.original_fit(activity_id)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(502, f"Garmin did not send the file: {e}") from None
            return Response(fit, media_type="application/octet-stream",
                            headers={"Content-Disposition": f'attachment; filename="{activity_id}.fit"'})

        @router.get("/status", response_class=HTMLResponse)
        def status(request: Request) -> HTMLResponse:
            response = _render("partials/dogs_status.html", request, **ctx())
            # The status card polls while a job runs: once it is done, reload the page so the
            # list shows what the check found or what the sync changed.
            if not m.running and m.job.get("kind") in ("check", "sync") and request.headers.get("HX-Trigger") == "dogs-status":
                response.headers["HX-Refresh"] = "true"
            return response

        @router.post("/run/{kind}", response_class=HTMLResponse)
        def run(request: Request, kind: str) -> HTMLResponse:
            kind = OLD_KINDS.get(kind, kind)
            if kind not in ("discover", "check", "sync"):
                raise HTTPException(404)
            if not m.garmin.has_tokens():
                raise HTTPException(409, "Connect Garmin first (Settings → Garmin).")
            if kind != "discover" and not m.store.state.rules:
                raise HTTPException(409, "Add a rule first (Settings → Dogs).")
            started = m.start_job(kind)
            response = _render("partials/dogs_status.html", request, **ctx())
            if kind != "discover":
                message = ({"check": "Checking your whole history: activities missing their dog show up as not synced.",
                            "sync": "Adding the dogs to the activities not synced."}[kind]
                           if started else "A dogs job is already running.")
                response.headers["HX-Trigger"] = templating.notice_headers(message, "success" if started else "info")["HX-Trigger"]
            return response

        @router.post("/stop", response_class=HTMLResponse)
        def stop_job(request: Request) -> HTMLResponse:
            m._cancel.set()
            return _render("partials/dogs_status.html", request, **ctx())

        @router.post("/dog")
        def save_dog(dog_id: str = Form(...), name: str = Form(""), next: str = Form("")) -> RedirectResponse:
            try:
                m.store.set_dog(dog_id, name)
            except ValueError as e:
                return back(str(e), to=next)
            return back(saved=True, to=next)

        @router.post("/dog/{dog_id}/delete")
        def delete_dog(dog_id: str, next: str = "") -> RedirectResponse:
            try:
                m.store.remove_dog(dog_id)
            except ValueError as e:
                return back(str(e), to=next)
            return back(saved=True, to=next)

        @router.post("/rules")
        def add_rule(name: str = Form(""), dog_id: str = Form(""), match: str = Form("exact"),
                     next: str = Form("")) -> RedirectResponse:
            try:
                m.store.add_rule(name, dog_id, match)
            except ValueError as e:
                return back(str(e), to=next)
            m.matches = []
            m._wake.set()
            return back(saved=True, to=next)

        @router.post("/rules/{index}/delete")
        def delete_rule(index: int, next: str = "") -> RedirectResponse:
            try:
                m.store.remove_rule(index)
            except ValueError as e:
                return back(str(e), to=next)
            m.matches = []
            return back(saved=True, to=next)

        @router.post("/autosync", response_class=HTMLResponse)
        def autosync(enabled: str = Form("false"), interval: str = Form("")) -> HTMLResponse:
            try:
                m.store.set_auto(enabled in ("true", "True", "1", "on"), int(interval) if interval else None)
            except ValueError as e:
                return HTMLResponse(f"<span class='state-failed'>{html.escape(str(e))}</span>")
            m._wake.set()
            return HTMLResponse(m.autosync_line())

        for path in ("/dog", "/rules", "/stop", "/autosync"):
            router.add_api_route(path, lambda: RedirectResponse("/dogs", status_code=303),
                                 methods=["GET"], include_in_schema=False)
        return router
