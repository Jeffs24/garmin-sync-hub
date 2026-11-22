"""Web pages and background jobs of the Zwift map module (mounted under /zwift)."""
from __future__ import annotations

import html
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader

from .. import retry, templating
from ..activity import ZWIFT_STATES
from ..auth import auth_enabled
from ..garmin_session import GarminAuthRequired, shared_session
from .config import EDITABLE, SCAN_INTERVALS, Config
from .preview import summarize
from .service import Service
from .store import Store
from .zwift_api import ZwiftClient

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = templating.register(Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True))

# Same labels and colors as the Activity feed: status -> (label, state).
STATUS_STATES = ZWIFT_STATES
STATUS_LABELS = {status: label for status, (label, _state) in ZWIFT_STATES.items()}

# Filter tabs of the Pairs page: key -> (label, statuses).
PAIR_FILTERS = {
    "all": ("All", None),
    "waiting": ("Ready to merge", ("detected", "simulated", "queued", "running")),
    "merged": ("Merged", ("merged",)),
    "failed": ("Failed", ("failed",)),
    "ignored": ("Ignored or restored", ("ignored", "restored")),
}
BUSY = ("running", "queued")


def filter_pairs(rows: list[dict], key: str) -> list[dict]:
    statuses = PAIR_FILTERS.get(key, PAIR_FILTERS["all"])[1]
    return rows if statuses is None else [r for r in rows if r["status"] in statuses]

_PAIR_ACTIONS = ("/scan", "/merge-all")
_ACCOUNT_ACTIONS = ("/upload", "/sync")
_SETTINGS_ACTIONS = ("/login", "/logout")
SETTINGS = "/settings#zwift"  # the Zwift account is connected from Settings → Zwift
ACCOUNT = "/zwift/account"


def _render(name: str, request: Request, **ctx) -> HTMLResponse:
    ctx.setdefault("auth_enabled", auth_enabled())
    return HTMLResponse(_env.get_template(name).render(request=request, **ctx))


def svg_chart(name: str, points: list[tuple[float, float]], invert: bool = False) -> str:
    """Small line chart as inline SVG (x = minutes)."""
    w, h, pad = 640, 120, 4
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, x1 = min(xs), max(xs) or 1
    y0, y1 = min(ys), max(ys)
    if y1 - y0 < 1e-9:
        y1 = y0 + 1

    def px(x: float, y: float) -> str:
        fx = pad + (x - x0) / ((x1 - x0) or 1) * (w - 2 * pad)
        fy = (y - y0) / (y1 - y0)
        fy = fy if invert else 1 - fy
        return f"{fx:.1f},{pad + fy * (h - 2 * pad):.1f}"

    line = " ".join(px(x, y) for x, y in points)
    fmt = (lambda v: f"{int(v)}:{int(v % 1 * 60):02d}") if name.startswith("Pace") else (lambda v: f"{v:.0f}")
    top, bottom = (y0, y1) if invert else (y1, y0)
    label = html.escape(name)
    return (
        f"<div class='chart'><div style='display:flex;justify-content:space-between'><b>{label}</b>"
        f"<span class='zw-msg'>{fmt(bottom)} – {fmt(top)}</span></div>"
        f"<svg viewBox='0 0 {w} {h}' preserveAspectRatio='none' role='img' aria-label='{label}'>"
        f"<polyline points='{line}' fill='none' stroke='var(--accent)' stroke-width='1.5' "
        "vector-effect='non-scaling-stroke'/></svg>"
        "<div class='zw-msg' style='display:flex;justify-content:space-between'><span>0 min</span>"
        f"<span>{x1:.0f} min</span></div></div>"
    )


def autosync_status(m: "ZwiftModule") -> str:
    """One line under the auto-sync switch: last and next scan (same macro as Liftosaur)."""
    return templating.render_autosync_line(
        _env, m.cfg.auto_scan, m.state["last_scan"], m.state["next_scan"],
        "scans run only when you click “Scan now”.")


class ZwiftModule:
    """Everything the Zwift module needs at runtime: service, store, Zwift client, scheduler."""

    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config.load()
        self.garmin = shared_session()
        self.store = Store(self.cfg.db_path)
        self.service = Service(self.cfg, self.garmin, self.store)
        self.zwift = ZwiftClient(self.cfg.zwift_token_file)
        self._sync_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.state = {
            "last_error": None,
            "last_scan": None,
            "next_scan": None,
            "upload_results": [],
            "zwift": {"running": False, "counts": None, "last": None, "error": None,
                      "since": None, "finished_at": None},
        }
        self.router = self._build_router()

    # --- background work ----------------------------------------------------

    def zwift_sync(self, since: datetime) -> None:
        """Run a Zwift history sync; only one at a time."""
        zs = self.state["zwift"]
        if not self._sync_lock.acquire(blocking=False):
            return
        zs.update(running=True, error=None, since=since.date().isoformat(), counts=None, last=None)

        def progress(counts, message):
            zs.update(counts=dict(counts), last=message)
        try:
            zs["counts"] = self.service.sync_zwift(self.zwift, since, progress)
        except Exception as e:  # noqa: BLE001
            log.exception("Zwift sync failed")
            zs["error"] = str(e)
        finally:
            zs["running"] = False
            zs["finished_at"] = datetime.now(timezone.utc)
            self._sync_lock.release()

    def _loop(self) -> None:
        if not self.zwift.connected() and self.cfg.zwift_email and self.cfg.zwift_password:
            try:
                self.zwift.login(self.cfg.zwift_email, self.cfg.zwift_password)
            except Exception as e:  # noqa: BLE001
                log.warning("Automatic Zwift login failed: %s", e)
        while not self._stop.is_set():
            if self.cfg.auto_scan:
                self.run_scan()
            self._wait_for_next_scan()

    def run_scan(self) -> None:
        """One automatic pass: find pairs, fetch new Zwift rides, merge if auto-merge is on."""
        self.state["last_scan"] = datetime.now(timezone.utc)
        if not self.garmin.has_tokens():
            return
        try:
            self.service.scan()
            if self.zwift.connected() and self.cfg.zwift_auto_sync:
                self.zwift_sync(datetime.now(timezone.utc) - timedelta(days=self.cfg.scan_days))
            self.service.merge_pending()
            self.state["last_error"] = None
        except Exception as e:  # noqa: BLE001
            log.exception("Zwift scan failed")
            self.state["last_error"] = str(e)

    def _due(self) -> datetime | None:
        """When the next automatic scan runs, or None when auto-sync is off."""
        if not self.cfg.auto_scan:
            return None
        last = self.state["last_scan"]
        if last is None:
            return datetime.now(timezone.utc)
        return last + timedelta(minutes=self.cfg.scan_interval_minutes)

    def _wait_for_next_scan(self) -> None:
        """Sleep until the next scan is due; settings changes wake the loop up."""
        while not self._stop.is_set():
            due = self._due()
            self.state["next_scan"] = due
            timeout = None if due is None else max(0.0, (due - datetime.now(timezone.utc)).total_seconds())
            if timeout == 0:
                return
            if not self._wake.wait(timeout):
                return  # time is up
            self._wake.clear()

    def settings_changed(self) -> None:
        """Apply new scan settings now instead of after the current wait."""
        self.state["next_scan"] = self._due()
        self._wake.set()

    def start(self) -> None:
        self._stop.clear()
        threading.Thread(target=self._loop, name="zwift-scan", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run_bg(self, target_id: int, fn) -> None:
        self.store.set_status(target_id, "running", None)

        def job():
            try:
                fn(target_id)
            except Exception:  # noqa: BLE001 - status already recorded
                log.exception("Action on %s failed", target_id)
        threading.Thread(target=job, daemon=True).start()

    def summary(self) -> dict:
        """Counts for the dashboard."""
        rows = self.store.all()
        return {
            "waiting": sum(1 for r in rows if r["status"] in ("detected", "simulated")),
            "busy": sum(1 for r in rows if r["status"] in ("running", "queued")),
            "merged": sum(1 for r in rows if r["status"] == "merged"),
            "failed": sum(1 for r in rows if r["status"] == "failed"),
            "zwift_connected": self.zwift.connected(),
            "auto_merge": self.cfg.auto_merge,
            "auto_scan": self.cfg.auto_scan,
            "scan_interval_minutes": self.cfg.scan_interval_minutes,
            "last_scan": self.state["last_scan"],
            "next_scan": self.state["next_scan"],
            "last_error": self.state["last_error"],
        }

    # --- routes -------------------------------------------------------------

    def _build_router(self) -> APIRouter:
        router = APIRouter(prefix="/zwift")
        m = self

        for paths, target in ((_PAIR_ACTIONS, "/zwift"), (_ACCOUNT_ACTIONS, ACCOUNT), (_SETTINGS_ACTIONS, SETTINGS)):
            for path in paths:
                router.add_api_route(path, lambda target=target: RedirectResponse(target, status_code=303),
                                     methods=["GET"], include_in_schema=False)

        def pair_ctx() -> dict:
            return {"status_labels": STATUS_LABELS, "status_states": STATUS_STATES,
                    "can_restore": m.service.can_restore, "auto_retry": m.cfg.auto_merge and m.cfg.auto_scan,
                    "max_attempts": retry.MAX_ATTEMPTS, "parse_time": retry.parse}

        def render_tab(request: Request, tab: str) -> HTMLResponse:
            all_rows = m.store.all()
            status = request.query_params.get("status", "all")
            if status not in PAIR_FILTERS:
                status = "all"
            rows = filter_pairs(all_rows, status)
            # Pairs being merged refresh their own row; only the account sync reloads the page.
            busy = m.state["zwift"]["running"] if tab == "account" else False
            results = m.state["upload_results"] if tab == "account" else []
            if tab == "account":
                m.state["upload_results"] = []
            return _render(
                "zwift.html", request, tab=tab, cfg=m.cfg, rows=rows, busy=busy,
                total_pairs=len(all_rows), status_filter=status,
                filter_labels={k: label for k, (label, _s) in PAIR_FILTERS.items()},
                filter_counts={k: len(filter_pairs(all_rows, k)) for k in PAIR_FILTERS},
                waiting=sum(1 for r in all_rows if r["status"] in ("detected", "simulated")),
                garmin_connected=m.garmin.has_tokens(), zwift_connected=m.zwift.connected(),
                zs=m.state["zwift"], last_error=m.state["last_error"], upload_results=results,
                scan_intervals=SCAN_INTERVALS, autosync_status=autosync_status(m), **pair_ctx(),
            )

        def render_row(request: Request, target_id: int) -> HTMLResponse:
            row = m.store.get(target_id)
            if not row:
                raise HTTPException(404)
            return _render("partials/zwift_pair_row.html", request, r=row, **pair_ctx())

        @router.get("", response_class=HTMLResponse)
        def page(request: Request) -> HTMLResponse:
            return render_tab(request, "pairs")

        @router.get("/account", response_class=HTMLResponse)
        def account_page(request: Request) -> HTMLResponse:
            return render_tab(request, "account")

        @router.post("/scan")
        def scan() -> RedirectResponse:
            try:
                m.service.scan()
                m.state["last_error"] = None
            except GarminAuthRequired:
                m.state["last_error"] = "Garmin is not connected"
            except Exception as e:  # noqa: BLE001
                m.state["last_error"] = str(e)
            return RedirectResponse("/zwift", status_code=303)

        @router.post("/merge-all")
        def merge_all() -> RedirectResponse:
            waiting = [r["target_id"] for r in m.store.all() if r["status"] in ("detected", "simulated")]
            for tid in waiting:
                m.store.set_status(tid, "queued", "Waiting for its turn")
            if waiting:
                threading.Thread(target=m.service.merge_all, daemon=True).start()
            return RedirectResponse("/zwift", status_code=303)

        @router.post("/settings")
        async def save_settings(request: Request) -> RedirectResponse:
            form = await request.form()
            # Checkboxes post "off" from a hidden input, then "on" when ticked: keep the last value.
            values = {name: form.getlist(name)[-1] for name in EDITABLE if form.getlist(name)}
            try:
                m.cfg.update(values)
                m.cfg.save()
            except (ValueError, OSError) as e:
                return RedirectResponse("/settings?" + urlencode({"error": f"Zwift settings not saved: {e}"}) + "#zwift", status_code=303)
            m.settings_changed()
            return RedirectResponse("/settings?saved=zwift#zwift", status_code=303)

        @router.post("/autosync", response_class=HTMLResponse)
        def autosync(enabled: str = Form("false"), interval: str = Form("")) -> HTMLResponse:
            values: dict = {"auto_scan": enabled}
            if interval:
                values["scan_interval_minutes"] = interval
            try:
                m.cfg.update(values)
                m.cfg.save()
            except (ValueError, OSError) as e:
                return HTMLResponse(f"<span class='st-failed'>{html.escape(str(e))}</span>")
            m.settings_changed()
            return HTMLResponse(autosync_status(m))

        @router.post("/upload")
        async def upload(files: list[UploadFile] = File(...)) -> RedirectResponse:
            results = []
            for f in files[:200]:
                data = await f.read()
                try:
                    results.append(m.service.add_source_file(f.filename or "file.fit", data))
                except GarminAuthRequired:
                    results.append("Garmin is not connected")
                    break
                except Exception as e:  # noqa: BLE001
                    log.exception("Upload of %s failed", f.filename)
                    results.append(f"{f.filename}: error ({e})")
            m.state["upload_results"] = results
            return RedirectResponse(ACCOUNT, status_code=303)

        @router.post("/login")
        def zwift_login(email: str = Form(...), password: str = Form(...)) -> RedirectResponse:
            try:
                m.zwift.login(email, password)
            except Exception as e:  # noqa: BLE001
                return RedirectResponse("/settings?" + urlencode({"error": f"Zwift login refused: {e}"}) + "#zwift",
                                        status_code=303)
            return RedirectResponse("/settings?saved=zwift-connected#zwift", status_code=303)

        @router.post("/logout")
        def zwift_logout() -> RedirectResponse:
            m.zwift.logout()
            return RedirectResponse("/settings?saved=zwift-disconnected#zwift", status_code=303)

        @router.post("/sync")
        def zwift_sync_route(since: str = Form(...)) -> RedirectResponse:
            try:
                start = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
            except ValueError:
                raise HTTPException(400, "invalid date")
            if m.zwift.connected() and m.garmin.has_tokens():
                threading.Thread(target=m.zwift_sync, args=(start,), daemon=True).start()
            return RedirectResponse(ACCOUNT, status_code=303)

        @router.post("/pairs/{target_id}/{action}")
        def act(request: Request, target_id: int, action: str):
            if not m.store.get(target_id):
                raise HTTPException(404)
            if action == "simulate":
                m._run_bg(target_id, m.service.simulate)
            elif action == "merge":
                m._run_bg(target_id, m.service.merge)
            elif action == "ignore":
                m.service.ignore(target_id)
            elif action == "restore":
                if not m.service.can_restore(target_id):
                    raise HTTPException(409, "Only a merged pair with its backups can be restored")
                m._run_bg(target_id, m.service.restore)
            else:
                raise HTTPException(404)
            if request.headers.get("HX-Request"):
                return render_row(request, target_id)  # the row updates in place
            return RedirectResponse("/zwift", status_code=303)

        @router.get("/pairs/{target_id}/row", response_class=HTMLResponse)
        def pair_row(request: Request, target_id: int) -> HTMLResponse:
            return render_row(request, target_id)

        @router.get("/pairs/{target_id}/merged.fit")
        def download(target_id: int) -> FileResponse:
            path = m.cfg.backup_dir / str(target_id) / f"{target_id}_merged.fit"
            if not path.exists():
                raise HTTPException(404)
            return FileResponse(path, filename=f"{target_id}_merged.fit", media_type="application/octet-stream")

        @router.get("/pairs/{target_id}/preview", response_class=HTMLResponse)
        def preview(request: Request, target_id: int) -> HTMLResponse:
            row = m.store.get(target_id)
            if not row:
                raise HTTPException(404)
            path = m.cfg.backup_dir / str(target_id) / f"{target_id}_merged.fit"
            if not path.exists():
                return _render("zwift_preview.html", request, row=row, has_file=False, pv=summarize_empty(),
                               charts=[], delete_source=m.cfg.delete_source)
            pv = summarize(path.read_bytes())
            charts = [svg_chart(n, pts, invert=n.startswith("Pace")) for n, pts in pv.series.items()]
            return _render("zwift_preview.html", request, row=row, has_file=True, pv=pv, charts=charts,
                           delete_source=m.cfg.delete_source)

        return router


def summarize_empty():
    from .preview import Preview
    return Preview()
