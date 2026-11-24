"""Backup page, schedule, zip export and rclone copy (mounted under /backup)."""
from __future__ import annotations

import html
import json
import logging
import shutil
import subprocess
import threading
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader

from .. import retry, templating
from ..auth import auth_enabled
from ..garmin_session import GarminAuthRequired, shared_session
from ..paths import data_dir
from .engine import AnyEvent, Backup, Progress, StopBackup, find_activity_file, folder_size, inventory
from .rclone_check import check_destination
from .settings import BACKUP_INTERVALS, BackupSettings, backup_dir

log = logging.getLogger(__name__)

RUNS_KEPT = 100  # backup runs kept for the Activity feed
PAGE_SIZE = 10
# Filter tabs of the backup runs, like the other source pages.
RUN_FILTERS = {"all": "All", "new": "New data", "uptodate": "Up to date", "failed": "Failed or stopped"}
# What a run can add: key in Progress.added -> (singular, plural).
ADDED_LABELS = {"activities": ("activity", "activities"), "health_days": ("day of health data", "days of health data"),
                "history": ("history file", "history files"), "gear": ("piece of gear", "pieces of gear"),
                "workouts": ("workout", "workouts"), "courses": ("course", "courses")}


def run_state(run: dict) -> str:
    if not run.get("ok"):
        return "failed"
    new = run.get("new") or {"activities": run.get("activities_new"), "health_days": run.get("health_days_new")}
    return "new" if any(new.values()) else "uptodate"

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = templating.register(Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True))

PHASES = {"account": "Account data", "gear": "Gear", "activities": "Activities", "history": "History per year", "workouts": "Workouts",
          "courses": "Courses", "health": "Health data", "copy": "Copy with rclone", "done": "Done"}


def _render(name: str, request: Request | None, **ctx) -> HTMLResponse:
    ctx.setdefault("auth_enabled", auth_enabled())
    return HTMLResponse(_env.get_template(name).render(request=request, **ctx))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class BackupModule:
    def __init__(self):
        self.settings = BackupSettings.load()
        self.garmin = shared_session()
        self._lock = threading.Lock()
        self._stop = threading.Event()   # app shutdown
        self._cancel = threading.Event()  # "Stop" button
        self._wake = threading.Event()
        self.progress: Progress | None = None
        self.export: dict[str, Any] = {"running": False, "error": None}
        self.state = self._load_state()
        self.router = self._build_router()

    # --- state --------------------------------------------------------------

    @property
    def root(self) -> Path:
        return backup_dir()

    @staticmethod
    def _state_path() -> Path:
        return data_dir() / "backup_state.json"

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self._state_path().read_text())
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        try:
            self._state_path().parent.mkdir(parents=True, exist_ok=True)
            self._state_path().write_text(json.dumps(self.state, indent=2))
        except OSError as e:
            log.warning("Could not save the backup state: %s", e)

    @property
    def running(self) -> bool:
        return self._lock.locked()

    def summary(self) -> dict[str, Any]:
        s = self.state
        return {
            "running": self.running,
            "auto": self.settings.auto,
            "interval_minutes": self.settings.interval_minutes,
            "last_finished": _parse(s.get("last_finished")),
            "last_ok": _parse(s.get("last_ok")),
            "error": s.get("error"),
            "counts": s.get("counts") or {},
            "next_run": self._due() if self.settings.auto else None,
            "garmin_connected": self.garmin.has_tokens(),
        }

    # --- running ------------------------------------------------------------

    def run(self) -> bool:
        """One backup run (blocking). False when another run is going on."""
        if not self._lock.acquire(blocking=False):
            return False
        self._cancel.clear()
        progress = self.progress = Progress()
        self.state.update(last_started=_iso(datetime.now(timezone.utc)), error=None)
        error = None
        try:
            since = date.fromisoformat(self.settings.health_since) if self.settings.health_since else None
            stop = AnyEvent(self._stop, self._cancel)
            Backup(self.garmin.client, self.root, health=self.settings.health, health_since=since,
                   stop=stop, progress=progress).run()
            if self.settings.rclone_remote:
                progress.phase, progress.message = "copy", self.settings.rclone_remote
                ok, out = self.rclone(["copy", str(self.root), self.settings.rclone_remote, "--exclude", "*.part"])
                if not ok:
                    error = f"Copy to {self.settings.rclone_remote} failed: {out}"
        except InterruptedError:
            error = "Stopped. The next run resumes where this one stopped."
        except StopBackup as e:
            error = str(e)
        except GarminAuthRequired:
            error = "Garmin is not connected."
        except PermissionError as e:
            error = (f"Cannot write to {self.root} ({e.strerror}). On the server, give the folder to the app's "
                     "user: sudo chown -R 1000:1000 garmin-backup")
        except Exception as e:  # noqa: BLE001
            log.exception("Backup failed")
            error = f"Backup failed: {e}"
        finally:
            finished = datetime.now(timezone.utc)
            counts = dict(self.state.get("counts") or {})
            counts.update(progress.counts)
            try:
                counts["size"] = folder_size(self.root) if self.root.exists() else 0
            except OSError:
                pass
            stopped = error is not None and error.startswith("Stopped")
            failures = 0 if error is None or stopped else int(self.state.get("failures") or 0) + 1
            retry_at = retry.next_retry(failures, finished) if failures else None
            self.state.update(last_finished=_iso(finished), error=error, counts=counts,
                              warnings=progress.warnings[:20], failures=failures, retry_at=_iso(retry_at))
            if error is None:
                self.state["last_ok"] = _iso(finished)
            run = {"at": _iso(finished), "ok": error is None, "stopped": stopped, "error": error,
                   "activities_new": progress.counts.get("activities_new", 0),
                   "health_days_new": progress.counts.get("health_days_new", 0),
                   "new": {k[:-4]: v for k, v in progress.counts.items() if k.endswith("_new")},
                   "added": progress.added,
                   "warnings": len(progress.warnings), "retry_at": _iso(retry_at)}
            self.state["runs"] = [run, *(self.state.get("runs") or [])][:RUNS_KEPT]
            self.refresh_inventory()
            self._save_state()
            self._lock.release()
        return True

    def refresh_inventory(self) -> dict[str, Any]:
        """Count what the backup holds (after each run, or the first time the page needs it)."""
        try:
            self.state["inventory"] = inventory(self.root) if self.root.exists() else {}
        except OSError as e:
            log.warning("Could not list the backup: %s", e)
        return self.state.get("inventory") or {}

    def start_run(self) -> bool:
        if self.running:
            return False
        threading.Thread(target=self.run, name="garmin-backup", daemon=True).start()
        return True

    def cancel(self) -> None:
        self._cancel.set()

    # --- schedule -----------------------------------------------------------

    def _due(self) -> datetime | None:
        """Next scheduled run: the interval, or sooner after a failure (30 min, 2 h, 8 h, like the
        other sources); after the last retry, back to the interval."""
        if not self.settings.auto:
            return None
        last = _parse(self.state.get("last_finished"))
        if last is None:
            return datetime.now(timezone.utc)
        due = last + timedelta(minutes=self.settings.interval_minutes)
        retry_at = _parse(self.state.get("retry_at"))
        return min(due, retry_at) if retry_at else due

    def _loop(self) -> None:
        while not self._stop.is_set():
            due = self._due()
            timeout = None if due is None else max(0.0, (due - datetime.now(timezone.utc)).total_seconds())
            if timeout == 0:
                if self.garmin.has_tokens():
                    self.run()
                else:
                    self._stop.wait(3600)
                continue
            if self._wake.wait(timeout):
                self._wake.clear()

    def settings_changed(self) -> None:
        self._wake.set()

    def start(self) -> None:
        self._stop.clear()
        threading.Thread(target=self._loop, name="backup-schedule", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # --- rclone and zip -------------------------------------------------------

    @staticmethod
    def rclone_config() -> Path:
        return data_dir() / "rclone.conf"

    def rclone(self, args: list[str], timeout: float | None = None, full: bool = False) -> tuple[bool, str]:
        """Run rclone with the app's config file; (ok, output: the last lines, or all of it with full)."""
        if not shutil.which("rclone"):
            return False, "rclone is not installed (it is included in the Docker image)."
        try:
            done = subprocess.run(["rclone", *args, "--config", str(self.rclone_config())],
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "rclone did not answer in time."
        if full:
            return done.returncode == 0, done.stdout + done.stderr
        out = (done.stdout + done.stderr).strip().splitlines()
        return done.returncode == 0, " ".join(out[-3:])[:500]

    def export_dir(self) -> Path:
        return data_dir() / "backup_exports"

    def latest_export(self) -> Path | None:
        files = sorted(self.export_dir().glob("garmin-backup-*.zip"))
        return files[-1] if files else None

    def build_export(self) -> None:
        if self.export["running"]:
            return
        self.export.update(running=True, error=None, done=0, total=0)
        try:
            files = [p for p in self.root.rglob("*") if p.is_file() and not p.name.endswith(".part")]
            self.export["total"] = len(files)
            self.export_dir().mkdir(parents=True, exist_ok=True)
            target = self.export_dir() / f"garmin-backup-{datetime.now():%Y%m%d-%H%M}.zip"
            tmp = target.with_suffix(".zip.part")
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                for path in files:
                    z.write(path, Path("garmin-backup") / path.relative_to(self.root))
                    self.export["done"] += 1
            tmp.replace(target)
            for old in self.export_dir().glob("garmin-backup-*.zip"):
                if old != target:
                    old.unlink(missing_ok=True)  # keep only the latest zip
        except Exception as e:  # noqa: BLE001
            log.exception("Zip export failed")
            self.export["error"] = f"Zip failed: {e}"
        finally:
            self.export["running"] = False

    # --- routes -------------------------------------------------------------

    def _build_router(self) -> APIRouter:
        router = APIRouter(prefix="/backup")
        m = self

        def page_ctx() -> dict:
            latest = m.latest_export()
            return {
                "b": m.summary(), "s": m.settings, "progress": m.progress, "phases": PHASES,
                "warnings": m.state.get("warnings") or [], "root": m.root,
                "intervals": BACKUP_INTERVALS, "export": m.export,
                "latest_export": latest, "latest_export_size": latest.stat().st_size if latest else 0,
                "autosync_line": backup_autosync_line(m),
            }

        @router.get("", response_class=HTMLResponse)
        def page(request: Request) -> HTMLResponse:
            runs = [{**r, "state": run_state(r)} for r in m.state.get("runs") or []]
            status = request.query_params.get("status", "all")
            if status not in RUN_FILTERS:
                status = "all"
            counts = {k: (len(runs) if k == "all" else sum(1 for r in runs if r["state"] == k)) for k in RUN_FILTERS}
            chosen = runs if status == "all" else [r for r in runs if r["state"] == status]
            try:
                page_no = max(1, int(request.query_params.get("page", 1)))
            except ValueError:
                page_no = 1
            page_count = max(1, -(-len(chosen) // PAGE_SIZE))
            page_no = min(page_no, page_count)
            inv = m.state.get("inventory")
            if inv is None and m.root.exists() and not m.running:
                inv = m.refresh_inventory()
                m._save_state()
            return _render("backup.html", request, **page_ctx(), inventory=inv or {},
                           runs=chosen[(page_no - 1) * PAGE_SIZE: page_no * PAGE_SIZE], status_filter=status,
                           filter_labels=RUN_FILTERS, filter_counts=counts, page=page_no, page_count=page_count,
                           added_labels=ADDED_LABELS, total_runs=len(runs))

        @router.get("/activities/{activity_id}/file")
        def activity_file(activity_id: int) -> FileResponse:
            """The file of an activity as saved in the backup."""
            path = find_activity_file(m.root, activity_id)
            if not path:
                raise HTTPException(404, "This activity has no file in the backup")
            return FileResponse(path, filename=path.name, media_type="application/octet-stream")

        @router.get("/status", response_class=HTMLResponse)
        def status(request: Request) -> HTMLResponse:
            return _render("partials/backup_status.html", request, **page_ctx())

        @router.post("/run", response_class=HTMLResponse)
        def run_now(request: Request) -> HTMLResponse:
            if not m.garmin.has_tokens():
                raise HTTPException(409, "Connect Garmin first (Settings → Garmin).")
            started = m.start_run()
            response = _render("partials/backup_status.html", request, **page_ctx())
            message = "Backup started: it adds only what is new." if started else "A backup is already running."
            response.headers["HX-Trigger"] = templating.notice_headers(message, "success" if started else "info")["HX-Trigger"]
            return response

        @router.post("/stop", response_class=HTMLResponse)
        def stop_run(request: Request) -> HTMLResponse:
            m.cancel()
            return _render("partials/backup_status.html", request, **page_ctx())

        @router.post("/autosync", response_class=HTMLResponse)
        def autosync(enabled: str = Form("false"), interval: str = Form("")) -> HTMLResponse:
            values: dict = {"auto": enabled}
            if interval:
                values["interval_minutes"] = interval
            try:
                m.settings.update(values)
                m.settings.save()
            except (ValueError, OSError) as e:
                return HTMLResponse(f"<span class='state-failed'>{html.escape(str(e))}</span>")
            m.settings_changed()
            return HTMLResponse(backup_autosync_line(m))

        @router.post("/settings")
        async def save_settings(request: Request) -> RedirectResponse:
            form = await request.form()
            values = {name: form.getlist(name)[-1] for name in ("health", "health_since", "rclone_remote")
                      if form.getlist(name)}
            try:
                m.settings.update(values)
                m.settings.save()
            except (ValueError, OSError) as e:
                return RedirectResponse("/settings?" + urlencode({"error": f"Backup settings not saved: {e}"})
                                        + "#backup", status_code=303)
            return RedirectResponse("/settings?saved=backup#backup", status_code=303)

        @router.post("/rclone/test", response_class=HTMLResponse)
        def rclone_test(remote: str = Form("")) -> HTMLResponse:
            remote = remote.strip() or m.settings.rclone_remote
            if ":" not in remote:
                return HTMLResponse("<span class='state-failed'>Enter a destination like gdrive:Garmin.</span>")
            if not shutil.which("rclone"):
                return HTMLResponse("<span class='state-failed'>rclone is not installed (it is included in the Docker image).</span>")
            checks = check_destination(remote, lambda args: m.rclone(args, timeout=60, full=True))
            css = {"ok": "state-ok", "warn": "state-warning", "fail": "state-failed", "info": ""}
            return HTMLResponse("".join(
                f"<div class='{css[level]}' style='margin-top: 4px;'>{html.escape(text)}</div>" for level, text in checks))

        @router.post("/export", response_class=HTMLResponse)
        def export(request: Request) -> HTMLResponse:
            if not m.root.exists():
                raise HTTPException(409, "Nothing to zip yet: run a backup first.")
            if not m.export["running"]:
                threading.Thread(target=m.build_export, name="backup-zip", daemon=True).start()
                m.export.update(running=True, done=0, total=0, error=None)
            return _render("partials/backup_status.html", request, **page_ctx())

        @router.get("/export/download")
        def download() -> FileResponse:
            latest = m.latest_export()
            if not latest:
                raise HTTPException(404, "No zip yet")
            return FileResponse(latest, filename=latest.name, media_type="application/zip")

        for path in ("/run", "/stop", "/export", "/settings", "/autosync"):
            router.add_api_route(path, lambda: RedirectResponse("/backup", status_code=303),
                                 methods=["GET"], include_in_schema=False)
        return router


def backup_autosync_line(m: BackupModule) -> str:
    s = m.summary()
    return templating.render_autosync_line(_env, s["auto"], s["last_finished"], s["next_run"],
                                           "backups run only when you click “Back up now”.")
