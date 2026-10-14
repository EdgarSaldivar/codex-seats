"""One trusted, loopback-only browser session for the existing seat manager."""

from __future__ import annotations

import difflib
import json
import math
import os
import queue
import secrets
import socket
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any

from .account import (
    AccountReadError, _auth_file_fingerprint, _identity_text, banked_reset_report,
    prepare_banked_reset_redemption, redeem_banked_reset,
)
from .capacity import (
    _capacity_tier, _observation_time, _reported_windows_known,
    _stable_window_name, active_capacity_windows,
)
from .policy_settings import policy_report
from .runtime_readers import SettingsReader
from .seat_manager import ManagerEligibilityError, ManagerError, SeatManager, ensure_management_eligible
from .seat_registry import CAPACITY_PLAN_PRESETS, derive_roles
from .state import (
    _load_settings_for_mutation, _merge, default_settings, read_pool_snapshot,
    parse_iso, runtime_seat_home, settings_lock,
)
from .watch import WatchRefresher

MAX_BODY = 16 * 1024
MAX_OPERATIONS = 32
OPERATION_TTL = 300.0
KINDS = frozenset({
    "pause", "reactivate", "redeem-reset", "settings", "claude", "add-seat",
    "edit-seat", "remove-seat", "set-role",
})
ASSETS = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
MIME = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8"}
CAPACITY_LABELS = {item["profile"]: item["label"] for item in CAPACITY_PLAN_PRESETS}


def _strict_settings(root: Path) -> tuple[dict[str, Any], list[str]]:
    with settings_lock(root):
        canonical, _before, _payload, warnings = _load_settings_for_mutation(root, require_existing=True)
    return _merge(default_settings(), canonical), warnings


def _diff(before: str, after: str) -> list[str]:
    lines = list(difflib.unified_diff(
        before.splitlines(), after.splitlines(), fromfile="current", tofile="proposed",
        lineterm="", n=0,
    ))
    if len(lines) > 200 or sum(len(line) for line in lines) > 128 * 1024:
        raise ValueError("preview diff is too large for the browser; use the terminal manager")
    return lines


def _json_diff(before: Any, after: Any) -> list[str]:
    return _diff(json.dumps(before, indent=2, sort_keys=True), json.dumps(after, indent=2, sort_keys=True))


def _cached_identity_report(root: Path, settings: dict[str, Any], seat_id: str,
                            runtime: dict[str, Any], current: float) -> dict[str, Any]:
    """Apply the native identity guards to the read-only pool snapshot."""
    cached = runtime.get("manager_identity")
    cached = cached if isinstance(cached, dict) else {}
    observed = cached.get("observed_at")
    observed = (float(observed) if isinstance(observed, (int, float))
                and not isinstance(observed, bool) and math.isfinite(observed) else None)
    fingerprint = _auth_file_fingerprint(runtime_seat_home(root, seat_id, settings))

    def unavailable(status: str, reason: str) -> dict[str, Any]:
        return {"status": status, "email": None, "observed_plan_type": None,
                "observed_at": observed, "reason": reason}

    if fingerprint is None:
        return unavailable("unavailable", "seat needs login before identity can be verified")
    if cached.get("auth_file_fingerprint") != fingerprint:
        status = "stale" if _identity_text(cached.get("email"), 320) else "unavailable"
        return unavailable(status, "login changed since identity was verified; refresh this seat")
    status = cached.get("status") if cached.get("status") in {"fresh", "stale"} else "unavailable"
    reason = _identity_text(cached.get("reason"), 256)
    stale_seconds = float(settings["pool"]["probe_stale_seconds"])
    if status == "fresh" and (observed is None or observed > current
                              or current - observed > stale_seconds):
        status = "stale"
        reason = "identity verification is older than the refresh freshness window"
    return {"status": status, "email": _identity_text(cached.get("email"), 320),
            "observed_plan_type": _identity_text(cached.get("observed_plan_type"), 128),
            "observed_at": observed, "reason": reason}


def _window_observed_at(runtime: dict[str, Any], name: str, default: float | None,
                        current: float) -> float | None:
    tracked = runtime.get("usage_reset_windows")
    if isinstance(tracked, dict):
        for raw_name, window in tracked.items():
            if _stable_window_name(raw_name) != name or not isinstance(window, dict):
                continue
            value = window.get("observed_at")
            if (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and 0 < value <= current):
                return float(value)
    return default


def _usage_observation_time(runtime: dict[str, Any], current: float) -> float | None:
    observation = runtime.get("active_window_observation")
    observation = observation if isinstance(observation, dict) else {}
    raw = observation.get("observed_at")
    if raw is None:
        raw = parse_iso(observation.get("updated_at")) or parse_iso(
            runtime.get("usage_updated_at") or runtime.get("probed_at")
        )
        if raw is None:
            raw = runtime.get("usage_observed_at")
    if (not isinstance(raw, (int, float)) or isinstance(raw, bool)
            or not math.isfinite(raw) or not 0 < raw <= current):
        return None
    # Age retained windows conservatively, after validating the latest observation.
    return _observation_time(
        {**runtime, "active_window_observation": {"observed_at": float(raw)}},
        current=current,
    )


@dataclass
class Operation:
    id: str
    kind: str
    seat: str | None
    status: str = "preparing"
    preview: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    native: Any = None
    created_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def public(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in (
            "id", "kind", "seat", "status", "preview", "result", "error",
        )}


class Session:
    """Serialize provider work and commits while HTTP handlers only enqueue."""

    def __init__(self, root: Path, settings: dict[str, Any], reader: SettingsReader, *,
                 no_active_probe: bool = False, warnings: list[str] | None = None) -> None:
        self.root = root
        self.reader = reader
        self.warnings = list(warnings or [])
        self.token = secrets.token_urlsafe(32)
        self._lock = threading.RLock()
        self._reader_lock = threading.Lock()
        self._closed = threading.Event()
        self._reader_closed = False
        self._operations: dict[str, Operation] = {}
        self._queue: queue.Queue[tuple[str, str] | None] = queue.Queue(maxsize=16)
        self.refresher = WatchRefresher(
            root, settings, float(settings["pool"]["watch_probe_seconds"]),
            periodic=not no_active_probe, local_probes=False,
        )
        self.manager = SeatManager(
            root, settings, refresher=self.refresher,
            settings_loader=self._load_current,
        )
        self._worker = threading.Thread(target=self._run, name="codex-seats-ui-operations", daemon=True)
        self._heartbeat = threading.Thread(target=self._beat, name="codex-seats-ui-reader", daemon=True)

    def start(self) -> None:
        try:
            self._worker.start()
            self._heartbeat.start()
            self.refresher.start()
        except BaseException:
            self.close()
            raise

    def close(self) -> bool:
        self._closed.set()
        with self._lock:
            for op in self._operations.values():
                if op.status == "preparing":
                    op.cancel_event.set()
                    op.status = "canceled"
                    op.native = None
                    op.finished_at = time.monotonic()
                elif op.status == "ready":
                    op.cancel_event.set()
                    op.status = "canceled"
                    op.native = None
                    op.finished_at = time.monotonic()
                elif op.status == "running" and op.kind in {"redeem-reset", "reactivate"}:
                    op.cancel_event.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        refresh_stopped = self.refresher.stop(timeout=5.0)
        if self._worker.ident:
            self._worker.join(timeout=5.0)
        if self._heartbeat.ident:
            self._heartbeat.join(timeout=2.0)
        stopped = not self._worker.is_alive()
        if stopped:
            self.manager.close()
        reader_closed = stopped and refresh_stopped and not self._heartbeat.is_alive() and self._reader_lock.acquire(timeout=1.0)
        if reader_closed:
            try:
                if not self._reader_closed:
                    self.reader.__exit__(None, None, None)
                    self._reader_closed = True
            finally:
                self._reader_lock.release()
        return stopped and refresh_stopped and reader_closed and not self._heartbeat.is_alive()

    def _load_current(self) -> tuple[dict[str, Any], list[str]]:
        with self._reader_lock:
            current, warnings = self.reader.load(_strict_settings)
            self.warnings = list(warnings)
            return current, warnings

    def _beat(self) -> None:
        while not self._closed.wait(10):
            try:
                with self._reader_lock:
                    self.reader.heartbeat()
            except OSError:
                self._closed.set()

    def _prune(self) -> None:
        now = time.monotonic()
        for key, op in list(self._operations.items()):
            if op.status in {"preparing", "ready"} and now - op.created_at > OPERATION_TTL:
                op.cancel_event.set()
                op.native = None
                op.status = "canceled"
                op.finished_at = now
            if op.status in {"done", "error", "canceled"} and now - (op.finished_at or op.created_at) > OPERATION_TTL:
                del self._operations[key]

    def operation(self, ident: str) -> dict[str, Any]:
        with self._lock:
            self._prune()
            op = self._operations.get(ident)
            if op is None:
                raise KeyError("operation not found")
            return op.public()

    def preview(self, body: dict[str, Any]) -> dict[str, Any]:
        kind = body.get("kind")
        if not isinstance(kind, str) or kind not in KINDS or set(body) - {"kind", "seat", "changes"}:
            raise ValueError("invalid preview request")
        seat = body.get("seat")
        changes = body.get("changes", {})
        if (seat is not None and (not isinstance(seat, str) or len(seat) > 80)) or not isinstance(changes, dict):
            raise ValueError("invalid seat or changes")
        if kind in {"pause", "reactivate", "redeem-reset", "edit-seat", "remove-seat", "set-role"} and not seat:
            raise ValueError("a registered seat id is required")
        if kind in {"settings", "claude", "add-seat"} and seat is not None:
            raise ValueError("this action has no seat selector")
        with self._lock:
            self._prune()
            if self._closed.is_set() or len(self._operations) >= MAX_OPERATIONS:
                raise ValueError("operation capacity reached; wait for a current operation")
            op = Operation(secrets.token_urlsafe(18), kind, seat)
            op.native = changes
            self._operations[op.id] = op
            try:
                self._queue.put_nowait((op.id, "prepare"))
            except queue.Full:
                del self._operations[op.id]
                raise
            return op.public()

    def confirm(self, ident: str) -> dict[str, Any]:
        with self._lock:
            self._prune()
            op = self._operations.get(ident)
            if op is None:
                raise KeyError("operation not found")
            if op.status != "ready" or self._closed.is_set() or time.monotonic() - op.created_at > OPERATION_TTL:
                raise ValueError("operation is not ready for confirmation")
            op.status = "running"
            try:
                self._queue.put_nowait((ident, "commit"))
            except queue.Full:
                op.status = "ready"
                raise
            return op.public()

    def cancel(self, ident: str) -> dict[str, Any]:
        with self._lock:
            op = self._operations.get(ident)
            if op is None:
                raise KeyError("operation not found")
            if op.status in {"preparing", "ready"}:
                op.cancel_event.set()
                op.native = None
                op.status = "canceled"
                op.finished_at = time.monotonic()
            else:
                raise ValueError("operation cannot be canceled now")
            return op.public()

    def _run(self) -> None:
        while not self._closed.is_set():
            item = self._queue.get()
            if item is None:
                break
            ident, phase = item
            with self._lock:
                op = self._operations.get(ident)
                if op is None or op.status == "canceled":
                    continue
            try:
                if phase == "prepare":
                    native, preview = self._prepare(op)
                    with self._lock:
                        if op.status == "preparing" and not op.cancel_event.is_set():
                            op.native, op.preview, op.status = native, preview, "ready"
                        else:
                            op.native = None
                else:
                    result = self._commit(op)
                    with self._lock:
                        op.result, op.status, op.finished_at = result, "done", time.monotonic()
            except (ManagerError, AccountReadError, ValueError, OSError) as exc:
                with self._lock:
                    if op.status != "canceled":
                        op.error, op.status, op.finished_at = str(exc), "error", time.monotonic()
            except Exception:
                with self._lock:
                    if op.status != "canceled":
                        op.error, op.status, op.finished_at = "operation failed; inspect native state before retrying", "error", time.monotonic()

    def _prepare(self, op: Operation) -> tuple[Any, dict[str, Any]]:
        changes = op.native
        kind, seat = op.kind, op.seat
        if kind == "settings":
            native = self.manager.preview_global_settings(changes)
            shown = {"title": "Apply global settings", "diff": _diff(native.before, native.after), "warnings": []}
        elif kind == "claude":
            native = self.manager.claude_manager().preview(**changes)
            public = native.as_dict()
            shown = {"title": "Change Claude account", "diff": _json_diff(public["before"], public["after"]), "warnings": list(native.warnings)}
        elif kind == "redeem-reset":
            if changes:
                raise ValueError("redemption does not accept changes")
            native = prepare_banked_reset_redemption(self.root, self.manager.settings, seat, cancel_event=op.cancel_event)
            shown = {"title": "Redeem one banked reset", "diff": [], "warnings": [],
                     "seat": seat, "account_email": json.loads(native["provider_identity"])["email"],
                     "available_count": native["available_count"],
                     "credit": {key: native["credit"].get(key) for key in (
                         "fingerprint", "title", "granted_at", "expires_at", "expiry_status",
                     )}, "description": "One provider credit will be consumed once confirmed."}
        else:
            if kind == "pause":
                if changes:
                    raise ValueError("pause does not accept changes")
                native = self.manager.preview_pause(seat)
                action = native.preview
            elif kind == "reactivate":
                if set(changes) - {"as_primary"} or type(changes.get("as_primary", False)) is not bool:
                    raise ValueError("reactivate accepts only as_primary boolean")
                current_settings = self.manager.reload()
                if current_settings["pool"]["seats"].get(seat, {}).get("lifecycle") == "active":
                    raise ValueError("active seat roles require the separate set-role action")
                native = self.manager.preview_reactivate(seat, as_primary=changes.get("as_primary", False))
                action = native
            elif kind == "add-seat":
                if set(changes) - {"label", "home", "profile"} or "label" not in changes:
                    raise ValueError("add-seat accepts label, home, and profile")
                native = self.manager.preview_add(**changes)
                action = native
            elif kind == "edit-seat":
                if not changes or set(changes) - {
                    "label", "home", "profile", "relative_capacity", "capacity_source",
                    "reserve_pct", "small_task_min_headroom_pct", "big_task_min_headroom_pct",
                }:
                    raise ValueError("edit-seat accepts only supported seat fields")
                native = self.manager.preview_set(seat, **changes)
                action = native
            elif kind == "set-role":
                if set(changes) != {"role"} or changes["role"] not in {"primary", "deputy", "worker"}:
                    raise ValueError("set-role requires primary, deputy, or worker")
                native = self.manager.preview_set_role(seat, changes["role"])
                action = native
            else:
                if changes:
                    raise ValueError("remove-seat does not accept changes")
                native = self.manager.preview_remove(seat)
                action = native
            shown = {"title": kind.replace("-", " ").capitalize(),
                     "diff": _diff(action.before, action.after),
                     "warnings": list(action.warnings), "seat": action.seat_id}
        return native, shown

    def _commit(self, op: Operation) -> dict[str, Any]:
        kind, native = op.kind, op.native
        if kind == "settings":
            changed = self.manager.commit_global_settings(native)
            return {"message": "Settings applied" if changed else "Settings unchanged"}
        if kind == "claude":
            result = self.manager.claude_manager().commit(native, from_current_user_request=True)
            self.manager.reload()
            return {"message": "Claude account updated" if result.changed else "Claude account unchanged"}
        if kind == "redeem-reset":
            result = redeem_banked_reset(
                self.root, self.manager.settings, native, current_user_request=True,
                cancel_event=op.cancel_event,
            )
            return {"message": result.get("error") or "Redemption attempt recorded",
                    "outcome": result["outcome"], "refresh_error": result.get("refresh_error")}
        if kind == "pause":
            result = self.manager.commit_pause(native)
            return {"message": "Seat draining" if result.pending_drain else "Seat paused"}
        if kind == "reactivate":
            checks = self.manager.run_activation_checks(native, op.cancel_event)
            if not checks.passed:
                raise ValueError(checks.detail)
            self.manager.commit_activation(native, checks)
            return {"message": "Seat activated"}
        result = self.manager.commit(native)
        return {"message": f"Seat {result.seat_id} {'updated' if result.changed else 'unchanged'}"}

    def state(self) -> dict[str, Any]:
        current = time.time()
        self.manager.reload()
        settings = self.manager.settings
        warnings = list(self.warnings)
        try:
            snapshot = read_pool_snapshot(self.root)
        except Exception as exc:
            snapshot = {"seats": {}, "jobs": []}
            warnings.append(f"Pool snapshot unavailable: {exc}")
        roles = derive_roles(settings)
        seats = []
        all_jobs = snapshot.get("jobs", [])
        jobs = all_jobs[-256:]
        if len(all_jobs) > len(jobs):
            warnings.append("Only the 256 most recent cached jobs are shown")
        for seat_id, entry in settings["pool"]["seats"].items():
            runtime = snapshot["seats"].get(seat_id, {})
            observed = _usage_observation_time(runtime, current)
            capacity_runtime = {**runtime, "active_window_observation": {"observed_at": observed}}
            valid_observation = (observed is not None and math.isfinite(observed)
                                 and 0 < observed <= current)
            tier, _reason, _ = _capacity_tier(
                capacity_runtime, current=current,
                stale_seconds=settings["pool"]["probe_stale_seconds"],
                degraded_grace_seconds=0,
            ) if valid_observation else ("unknown", "invalid observation", None)
            fresh = tier == "fresh" and _reported_windows_known(runtime, current)
            windows = [{"name": name, "remaining_pct": pct, "resets_at": reset,
                        "window_minutes": minutes, "freshness": "fresh" if fresh else "stale",
                        "observed_at": _window_observed_at(
                            runtime, name, observed if valid_observation else None, current,
                        )} for name, pct, reset, minutes in
                       (active_capacity_windows(runtime, current=current)
                        if _reported_windows_known(runtime, current) else [])]
            try:
                identity = _cached_identity_report(self.root, settings, seat_id, runtime, current)
                if identity.get("status") not in {"fresh", "verified"}:
                    fresh = False
                    for window in windows:
                        window["freshness"] = "stale"
                bank = banked_reset_report(runtime, settings, at=current, root=self.root, seat_id=seat_id)
                history = bank.get("history")
                if isinstance(history, list) and len(history) > 20:
                    bank["history"] = history[-20:]
                    bank["history_truncated"] = True
            except Exception as exc:
                identity = {"status": "unavailable", "email": None, "observed_plan_type": None, "reason": str(exc)}
                bank = {"status": "unknown", "available_count": None, "credits": [], "observed_at": None, "last_error": str(exc)}
            busy = bool(runtime.get("in_flight"))
            task = next((str(job.get("kind") or "working") for job in jobs
                         if job.get("seat") == seat_id and job.get("status") == "running"), None)
            seats.append({"id": seat_id, "label": entry.get("label", seat_id),
                          "lifecycle": entry.get("lifecycle"), "role": roles.get(seat_id),
                          "busy": busy, "task": task, "health": runtime.get("status", "unknown"),
                          "freshness": "fresh" if fresh else "stale" if valid_observation else "unknown",
                          "observed_at": observed if valid_observation else None,
                          "identity": {key: identity.get(key) for key in ("status", "email", "observed_plan_type", "reason")},
                          "capacity": {"profile": entry.get("capacity_profile"), "label": CAPACITY_LABELS.get(entry.get("capacity_profile"), entry.get("capacity_profile")),
                                       "relative_capacity": entry.get("relative_capacity"), "source": entry.get("capacity_source")},
                          "windows": windows, "banked_resets": bank})
        local_seats = []
        try:
            report = self.manager.local_manager().report(now=current, background_jobs=jobs)
            for seat_id, row in report["seats"].items():
                config, status = row["config"], row["status"] or {}
                status_key = status.get("key") or status.get("status") or "unknown"
                local_seats.append({"id": seat_id, "label": config.get("label", seat_id),
                                    "host": config.get("host"), "lifecycle": config.get("lifecycle"),
                                    "mode": row["effective_mode"], "status": status_key,
                                    "reason": status.get("detail") or status.get("reason"), "busy": status_key == "working",
                                    "provider": "local", "capabilities": {"edit": "terminal only", "login": "terminal only"}})
        except Exception as exc:
            warnings.append(f"Local seat cache unavailable: {exc}")
        try:
            claude = self.manager.claude_manager().report(now=current)
        except Exception as exc:
            claude = {"account": "claude", "status": None, "status_error": str(exc)}
            warnings.append(f"Claude cache unavailable: {exc}")
        refresh = self.refresher.selected_snapshot()
        return {"version": 1, "observed_at": current, "seats": seats,
                "local_seats": local_seats, "claude": claude, "policy": policy_report(settings),
                "refresh": {"active": refresh.active, "queued": refresh.queued,
                            "last_result": refresh.last_result}, "warnings": warnings}


class LoopbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], session: Session):
        self.session = session
        self._slots = threading.BoundedSemaphore(12)
        super().__init__(address, Handler)

    def get_request(self) -> tuple[socket.socket, Any]:
        sock, address = super().get_request()
        sock.settimeout(5.0)
        return sock, address

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class Handler(BaseHTTPRequestHandler):
    server: LoopbackHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *args: Any) -> None:
        pass

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.close_connection = True
        self._error(code, "invalid request")

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        if sum(len(key) + len(value) + 4 for key, value in self.headers.items()) > 8192:
            self._error(431, "headers too large")
            return False
        return True

    def _headers(self, length: int, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'")

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self._headers(len(body), "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send(status, {"error": message})

    def _authorized(self, mutation: bool) -> bool:
        origin = f"http://127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            self.close_connection = True
            self._error(403, "invalid Host")
            return False
        supplied_origin = self.headers.get("Origin")
        if (mutation and supplied_origin != origin) or (supplied_origin is not None and supplied_origin != origin):
            self.close_connection = True
            self._error(403, "invalid Origin")
            return False
        supplied_token = self.headers.get("X-Codex-Seats-Session", "")
        if self.path.startswith("/api/") and not secrets.compare_digest(supplied_token, self.server.session.token):
            self.close_connection = True
            self._error(403, "Session unavailable. Reopen the launch link from your terminal.")
            return False
        return True

    def _body(self) -> dict[str, Any]:
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("Content-Type must be application/json")
        if self.headers.get("Transfer-Encoding") is not None or len(self.headers.get_all("Content-Length", [])) != 1:
            raise ValueError("ambiguous request body")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or not raw_length.isdecimal() or int(raw_length) > MAX_BODY:
            self.close_connection = True
            raise ValueError("invalid or oversized JSON body")
        body = json.loads(self.rfile.read(int(raw_length)))
        if not isinstance(body, dict):
            raise ValueError("JSON body must be an object")
        return body

    def do_GET(self) -> None:
        if not self._authorized(False):
            return
        if self.path in {"/api/preview", "/api/confirm", "/api/cancel", "/api/refresh"}:
            self._error(405, "method not allowed")
            return
        if self.path == "/api/state":
            try:
                self._send(200, self.server.session.state())
            except Exception:
                self._error(503, "cached state unavailable")
            return
        if self.path.startswith("/api/operations/"):
            ident = self.path.removeprefix("/api/operations/")
            if not ident or "/" in ident or "?" in ident:
                self._error(404, "not found")
                return
            try:
                self._send(200, self.server.session.operation(ident))
            except KeyError:
                self._error(404, "operation not found")
            return
        asset = ASSETS.get(self.path)
        if asset is None:
            self._error(404, "not found")
            return
        try:
            content = resources.files("codex_seats").joinpath("ui", asset).read_bytes()
        except (FileNotFoundError, OSError):
            self._error(503, "UI asset unavailable")
            return
        if asset == "index.html":
            content = content.replace(b"__SESSION_TOKEN__", b"")
        self.send_response(200)
        self._headers(len(content), MIME[asset])
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        if not self._authorized(True):
            return
        if self.path == "/api/state" or self.path.startswith("/api/operations/"):
            self.close_connection = True
            self._error(405, "method not allowed")
            return
        if self.path not in {"/api/preview", "/api/confirm", "/api/cancel", "/api/refresh"}:
            self.close_connection = True
            self._error(404, "not found")
            return
        try:
            body = self._body()
            session = self.server.session
            if self.path == "/api/preview":
                result = session.preview(body)
            elif self.path == "/api/confirm":
                result = session.confirm(_only_id(body))
            elif self.path == "/api/cancel":
                result = session.cancel(_only_id(body))
            else:
                if set(body) != {"seat"} or not isinstance(body["seat"], str):
                    raise ValueError("refresh requires a registered seat id")
                seat = session.manager.request_refresh(body["seat"])
                result = {"message": f"Refresh queued for {seat}"}
            self._send(200, result)
        except (ValueError, KeyError, json.JSONDecodeError, queue.Full) as exc:
            self.close_connection = True
            self._error(409 if isinstance(exc, (KeyError, queue.Full)) else 400, str(exc))
        except (ManagerError, AccountReadError) as exc:
            self.close_connection = True
            self._error(400, str(exc))
        except Exception:
            self.close_connection = True
            self._error(500, "request failed")

    def do_PUT(self) -> None:
        if self._authorized(True):
            self.close_connection = True
            self._error(405, "method not allowed")

    do_DELETE = do_PUT
    do_PATCH = do_PUT
    do_HEAD = do_PUT
    do_OPTIONS = do_PUT
    do_TRACE = do_PUT


def _only_id(body: dict[str, Any]) -> str:
    if set(body) != {"id"} or not isinstance(body["id"], str):
        raise ValueError("operation id is required")
    return body["id"]


def launch(root: Path, *, port: int = 0, no_open: bool = False,
           no_active_probe: bool = False) -> int:
    """Run until interrupted. Eligibility precedes all filesystem state effects."""
    ensure_management_eligible()
    if os.environ.get("CLAUDE_LEAD_WORKER") == "1":
        raise ManagerEligibilityError("Claude lead workers cannot open browser management")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    root = Path(root)
    _strict_settings(root)
    reader = SettingsReader(root, "browser-ui", module_file=__file__).__enter__()
    session = None
    server = None
    serving: threading.Thread | None = None
    failure: list[BaseException] = []
    try:
        settings, warnings = reader.load(_strict_settings)
        session = Session(root, settings, reader, no_active_probe=no_active_probe, warnings=warnings)
        server = LoopbackHTTPServer(("127.0.0.1", port), session)
        session.start()
        url = f"http://127.0.0.1:{server.server_port}/#session={session.token}"
        print(url, flush=True)
        if not no_open:
            webbrowser.open(url)

        def serve() -> None:
            try:
                server.serve_forever(poll_interval=0.25)
            except BaseException as exc:
                failure.append(exc)

        serving = threading.Thread(target=serve, name="codex-seats-ui-http", daemon=True)
        serving.start()
        serving.join()
        if failure:
            raise failure[0]
    finally:
        if server is not None:
            if serving is not None and serving.is_alive():
                server.shutdown()
                serving.join(timeout=2.0)
            server.server_close()
        if session is None:
            reader.__exit__(None, None, None)
        elif not session.close():
            print("Session work did not quiesce; inspect native state before retrying an action.", flush=True)
    return 0
