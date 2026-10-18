"""One read-only model of where work can run now, across every provider.

``collect(root, settings, now)`` is the single source for ``codex-seat capacity``
and the terminal manager's summary strip and seat table, so agents and humans
see the same model. It is built only from the existing readers: the shared pool
snapshot, the Codex capacity report (the same verdict routing uses), the Claude
capacity report, the cached local-host availability, the live dispatch queue,
and the ``pick-seat`` selection. Reading never writes state and never calls a
provider; data is as fresh as the last refresh (see each entry's ``fresh`` and
``observed_at``).

Entry fields (one per Codex seat, the Claude account, and each local seat):

* ``provider`` ``codex`` | ``claude`` | ``local``; ``seat`` stable id
  (``seatN``, ``claude``, or the local seat id); ``label``; ``plan``
* ``role`` ``primary`` | ``worker`` for Codex, else ``None``; ``used_last`` is
  true for the primary, which routing spends only when no worker fits
* ``lifecycle`` registry lifecycle; Claude is ``active`` or ``off``
* ``mode`` effective provider mode (Claude/local), else ``None``
* ``status`` raw provider state string (Codex ``ok``/``needs-login``/
  ``capped``/``error``/...; Claude ``ok``/``off``/``cooldown``/``needs-auth``/
  ...; local ``ready``/``working``/``stale``/``off``/...). Callers map it to
  display chips.
* ``busy`` and ``task`` (``{"label", "kind", "job_id"}`` or ``None``)
* ``windows`` ``[{"name", "minutes", "remaining_pct", "resets_at", "fresh"}]``
  and ``left`` ``{"5h": pct|None, "7d": pct|None}``
* ``fits`` subset of ``["small", "big"]`` (capacity verdict, independent of
  ``busy``); ``ready`` true when it is idle and a small task fits now
* ``reason`` why it does not fit (or ``None``); ``fresh`` usage freshness
* ``banked_resets`` available banked Codex resets (``None`` when unknown) and
  ``banked_resets_status`` (``fresh``/``stale``/``unknown``)
* ``observed_at`` epoch seconds of the usage observation, or ``None``
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from . import dispatch_client, tui_style
from .account import banked_reset_report
from .capacity import seat_capacity_report
from .claude_manager import resolve_subscription_tier
from .dispatch_protocol import DispatchError
from .provider_claude import claude_capacity_report
from .seat_registry import derive_roles, registered_seat_ids
from .state import (
    claude_usage_age_seconds,
    claude_usage_fresh,
    effective_claude_mode,
    load_claude_rate_limits,
    parse_iso,
    read_pool_snapshot,
    refresh_transients,
)
from .telemetry import refresh_passive_usage

SIZES = ("small", "big")
# Provider window names mapped to the two shortcuts agents ask about.
_WINDOW_NAMES = {
    "5h": "5h",
    "five_hour": "5h",
    "weekly": "7d",
    "seven_day": "7d",
}
_WINDOW_MINUTES = {"5h": 300.0, "7d": 10_080.0}


def read_snapshot(root: Path, settings: dict[str, Any]) -> dict[str, Any]:
    """The pool snapshot exactly as ``pick-seat`` reads it; in memory only.

    Raises :class:`state.PoolSnapshotError` when the ledger is unreadable.
    """
    snapshot = read_pool_snapshot(root)
    refresh_transients(snapshot, root, settings)
    refresh_passive_usage(snapshot, root, settings)
    return snapshot


def next_seats(snapshot: dict[str, Any], settings: dict[str, Any]) -> dict[str, str | None]:
    """The Codex seat ``pick-seat --size`` would choose now, per size."""
    from .seat_selection import pick_seat

    return {size: pick_seat(snapshot["seats"], settings, size, set()) for size in SIZES}


def codex_plan_name(settings: dict[str, Any], seat_id: str) -> str:
    """The configured plan profile in human form; never inferred from a multiplier."""
    entry = settings.get("pool", {}).get("seats", {}).get(seat_id, {})
    profile = entry.get("capacity_profile") if isinstance(entry, dict) else None
    return tui_style.human("capacity_profile", profile if isinstance(profile, str) else "")


def _claim_request_id(claim: dict[str, Any]) -> str | None:
    for key in ("request_id", "label"):
        value = claim.get(key)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value):
            return value
    return None


def _task(claim: Any, jobs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Describe a live claim, preferring the queued job's sanitized label."""
    if not isinstance(claim, dict):
        return None
    request_id = _claim_request_id(claim)
    job = next(
        (item for item in jobs if request_id and item.get("request_id") == request_id),
        None,
    )
    if job is not None:
        return {
            "label": str(job.get("label") or "managed job"),
            "kind": job.get("kind"),
            "job_id": request_id,
        }
    return {
        "label": str(claim.get("label") or claim.get("kind") or "managed job"),
        "kind": claim.get("kind"),
        "job_id": request_id or claim.get("job_id"),
    }


def _left(windows: list[dict[str, Any]]) -> dict[str, float | None]:
    left: dict[str, float | None] = {"5h": None, "7d": None}
    for window in windows:
        if window["name"] in left:
            left[window["name"]] = window["remaining_pct"]
    return left


def _fits(report: dict[str, Any], *, applicable: bool = True) -> list[str]:
    if not applicable:
        return []
    return [size for size in SIZES if report.get(f"fits_{size}")]


def codex_entries(
    root: Path,
    settings: dict[str, Any],
    snapshot: dict[str, Any],
    jobs: list[dict[str, Any]],
    *,
    current: float,
) -> list[dict[str, Any]]:
    roles = derive_roles(settings)
    learning = snapshot.get("capacity_learning", {})
    learning = learning if isinstance(learning, dict) else {}
    entries: list[dict[str, Any]] = []
    for seat_id in registered_seat_ids(
        settings, lifecycles=("active", "draining", "inactive")
    ):
        config = settings["pool"]["seats"][seat_id]
        lifecycle = str(config["lifecycle"])
        seat = snapshot.get("seats", {}).get(seat_id, {})
        seat = seat if isinstance(seat, dict) else {}
        report = seat_capacity_report(
            seat_id, seat, settings, current=current, learning=learning
        )
        fresh = report.get("tier") == "fresh"
        windows = [
            {
                "name": _WINDOW_NAMES.get(window["name"], window["name"]),
                "minutes": window.get("window_minutes"),
                "remaining_pct": window["remaining_pct"],
                "resets_at": window.get("resets_at"),
                "fresh": fresh,
            }
            for window in report.get("capacity_windows", [])
        ]
        status = str(seat.get("status") or "unknown")
        if seat.get("auth_failure") and status == "ok":
            status = "error"
        task = _task(seat.get("in_flight"), jobs)
        fits = _fits(report, applicable=lifecycle == "active")
        bank = banked_reset_report(seat, settings, at=current, root=root, seat_id=seat_id)
        observed = parse_iso(seat.get("usage_updated_at") or seat.get("probed_at"))
        entries.append(
            {
                "provider": "codex",
                "seat": seat_id,
                "label": str(config["label"]),
                "plan": codex_plan_name(settings, seat_id),
                "role": roles.get(seat_id),
                "used_last": roles.get(seat_id) == "primary",
                "lifecycle": lifecycle,
                "mode": None,
                "status": status,
                "busy": task is not None,
                "task": task,
                "windows": windows,
                "left": _left(windows),
                "fits": fits,
                "ready": lifecycle == "active" and task is None and "small" in fits,
                "reason": (
                    f"seat is {lifecycle}" if lifecycle != "active"
                    else None if report.get("fits_small") else report.get("reason")
                ),
                "fresh": fresh,
                "banked_resets": bank.get("available_count"),
                "banked_resets_status": bank.get("status"),
                "observed_at": observed,
            }
        )
    return entries


def claude_entry(
    root: Path,
    settings: dict[str, Any],
    snapshot: dict[str, Any],
    jobs: list[dict[str, Any]],
    *,
    current: float,
) -> dict[str, Any]:
    mode, mode_reason = effective_claude_mode(settings, at=current)
    provider = snapshot["providers"]["claude"]
    # Hard-off guarantee: the usage cache is not read while Claude is off.
    limits = load_claude_rate_limits(root, at=current) if mode != "off" else {}
    fresh = mode != "off" and claude_usage_fresh(settings, limits, at=current)
    windows = []
    for raw_name in ("five_hour", "seven_day"):
        window = limits.get(raw_name)
        remaining = window.get("remaining_percentage") if isinstance(window, dict) else None
        if not isinstance(remaining, (int, float)) or isinstance(remaining, bool):
            continue
        name = _WINDOW_NAMES[raw_name]
        reset = window.get("resets_at")
        windows.append(
            {
                "name": name,
                "minutes": _WINDOW_MINUTES[name],
                "remaining_pct": float(remaining),
                "resets_at": float(reset) if isinstance(reset, (int, float)) else None,
                "fresh": fresh,
            }
        )
    report = claude_capacity_report(settings, provider, limits, mode=mode)
    fits = _fits(report, applicable=bool(report.get("applicable")))
    task = _task(provider.get("in_flight"), jobs)
    age = claude_usage_age_seconds(limits, at=current) if mode != "off" else None
    if mode == "off":
        reason = mode_reason or "Claude mode is off"
    elif "small" not in fits:
        small = report.get("small", {})
        reason = (
            f"provider status is {provider.get('status')}"
            if not report.get("applicable")
            else small.get("quota_reason") or "no available Claude model"
        )
    else:
        reason = None
    tier = resolve_subscription_tier(
        settings.get("providers", {}).get("claude", {}).get("subscription_tier")
    )["label"]
    return {
        "provider": "claude",
        "seat": "claude",
        "label": "Claude",
        "plan": tier or "tier unknown",
        "role": None,
        "used_last": False,
        "lifecycle": "off" if mode == "off" else "active",
        "mode": mode,
        "status": str(provider.get("status") or "unknown"),
        "busy": task is not None,
        "task": task,
        "windows": windows,
        "left": _left(windows),
        "fits": fits,
        "ready": mode != "off" and task is None and "small" in fits,
        "reason": reason,
        "fresh": fresh,
        "banked_resets": None,
        "banked_resets_status": None,
        "observed_at": None if age is None else current - age,
    }


def local_entries(
    root: Path,
    settings: dict[str, Any],
    jobs: list[dict[str, Any]],
    *,
    current: float,
) -> list[dict[str, Any]]:
    """Local seats from the cached host availability; never probes a host."""
    seats = settings.get("local_seats")
    hosts = settings.get("local_hosts")
    if not isinstance(seats, dict) or not seats or not isinstance(hosts, dict):
        return []
    from .display import _local_status

    entries: list[dict[str, Any]] = []
    for row in _local_status(root, settings, jobs, at=current)["seats"]:
        status = str(row["key"])
        mode = str(row["mode"])
        task = None
        if status == "working":
            job = next(
                (
                    item for item in jobs
                    if item.get("provider") == "local"
                    and item.get("state") == "running"
                    and item.get("seat") == row["seat"]
                ),
                {},
            )
            task = {
                "label": str(row["detail"] or "managed job"),
                "kind": job.get("kind"),
                "job_id": job.get("request_id"),
            }
        usable = status in {"ready", "working"} and mode not in {"off", "shadow"}
        fits = ["small"] if mode == "canary" else list(SIZES)
        fits = fits if usable else []
        entries.append(
            {
                "provider": "local",
                "seat": row["seat"],
                "label": row["label"],
                "plan": row["model"],
                "role": None,
                "used_last": False,
                "lifecycle": row["lifecycle"],
                "mode": mode,
                "status": status,
                "busy": task is not None,
                "task": task,
                "windows": [],
                "left": {"5h": None, "7d": None},
                "fits": fits,
                "ready": usable and task is None,
                "reason": None if usable else (row["detail"] or status),
                "fresh": not row["stale"] and row["observed_at"] is not None,
                "banked_resets": None,
                "banked_resets_status": None,
                "observed_at": row["observed_at"],
            }
        )
    return entries


def _job_summary(job: dict[str, Any]) -> dict[str, Any]:
    return {
        key: job.get(key)
        for key in (
            "request_id", "state", "provider", "seat", "kind", "size", "label",
            "elapsed_seconds",
        )
    }


def _warnings(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for entry in entries:
        if entry["lifecycle"] not in {"active", "draining"}:
            continue
        where = {"provider": entry["provider"], "seat": entry["seat"]}
        status = entry["status"]
        if entry["provider"] == "local":
            if status in {"degraded", "model_mismatch"}:
                warnings.append({**where, "code": "local-" + status.replace("_", "-"),
                                 "message": f"{entry['seat']}: {entry['reason']}"})
            continue
        if status in {"needs-login", "needs-auth", "missing"}:
            warnings.append({**where, "code": "needs-login",
                             "message": f"{entry['seat']} needs sign-in ({status})"})
        elif status not in {"ok", "capped", "cooldown"}:
            warnings.append({**where, "code": "provider-status",
                             "message": f"{entry['seat']} provider is {status}"})
        if not entry["fresh"]:
            warnings.append({**where, "code": "stale-usage",
                             "message": f"{entry['seat']} usage is stale or unknown; "
                             f"run `codex-seat seats refresh {entry['seat']}`"
                             if entry["provider"] == "codex"
                             else f"{entry['seat']} usage is stale or unknown"})
    return warnings


SUMMARY_KEYS = (
    "ready", "total", "running", "queued", "waiting_capacity", "next", "banked_resets",
)


def summary(view: dict[str, Any]) -> dict[str, Any]:
    """The one summary strip: Ready N/M · Running · Queued · Next · Resets.

    ``total`` counts enabled entries across every provider: active Codex seats,
    Claude when its mode is on, and active local seats not off/shadow. ``ready``
    counts those that are idle and fit a small task (the smallest unit of work). ``running``
    counts live work once; ``banked_resets`` sums fresh banked-reset counts
    (``None`` when no seat has a fresh count). The capacity command, the
    dashboard header and the terminal manager all show these numbers.
    """
    return {key: view["pool"][key] for key in SUMMARY_KEYS}


def _summary(
    entries: list[dict[str, Any]],
    jobs: list[dict[str, Any]],
    picked: dict[str, str | None],
) -> dict[str, Any]:
    labels = {entry["seat"]: entry["label"] for entry in entries if entry["provider"] == "codex"}
    counts = dispatch_client.background_counts(jobs)
    job_ids = {job.get("request_id") for job in jobs}
    # A claim started outside the queue still occupies a seat; count it once.
    direct_running = sum(
        1 for entry in entries
        if entry["busy"] and entry["provider"] != "local"
        and (entry["task"] or {}).get("job_id") not in job_ids
    )
    enabled = [
        entry for entry in entries
        if entry["lifecycle"] == "active" and entry.get("mode") not in {"off", "shadow"}
    ]
    fresh_banks = [
        entry["banked_resets"] for entry in entries
        if entry["provider"] == "codex" and entry["banked_resets_status"] == "fresh"
        and isinstance(entry["banked_resets"], int)
    ]
    return {
        "ready": sum(1 for entry in enabled if entry["ready"]),
        "total": len(enabled),
        "running": counts.get("running", 0) + direct_running,
        "queued": counts.get("queued", 0),
        "waiting_capacity": counts.get("capacity", 0),
        "next": {
            size: (
                {"provider": "codex", "seat": seat_id, "label": labels.get(seat_id, seat_id)}
                if seat_id
                else None
            )
            for size, seat_id in picked.items()
        },
        "banked_resets": sum(fresh_banks) if fresh_banks else None,
    }


def collect(
    root: Path,
    settings: dict[str, Any],
    now: float | None = None,
    *,
    settings_warnings: list[str] | None = None,
    snapshot: dict[str, Any] | None = None,
    jobs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return ``{"pool": {...}, "entries": [...], "jobs": [...]}``; read-only.

    A caller that already read the snapshot (via :func:`read_snapshot`) and the
    live jobs may pass them in to avoid a second read. Raises
    :class:`state.PoolSnapshotError` when the pool ledger is unreadable.
    """
    current = time.time() if now is None else float(now)
    if snapshot is None:
        snapshot = read_snapshot(root, settings)
    warnings: list[dict[str, Any]] = [
        {"provider": None, "seat": None, "code": "settings", "message": text}
        for text in settings_warnings or []
    ]
    if jobs is None:
        try:
            jobs = dispatch_client.live_jobs(root, settings, now=current)
        except DispatchError as exc:
            jobs = []
            warnings.append({"provider": None, "seat": None, "code": "queue-unavailable",
                             "message": f"background work is unavailable: {exc}"})
    entries = [
        *codex_entries(root, settings, snapshot, jobs, current=current),
        claude_entry(root, settings, snapshot, jobs, current=current),
        *local_entries(root, settings, jobs, current=current),
    ]
    warnings.extend(_warnings(entries))
    pool = {
        "observed_at": current,
        **_summary(entries, jobs, next_seats(snapshot, settings)),
        "warnings": warnings,
    }
    return {
        "pool": pool,
        "entries": entries,
        "jobs": [_job_summary(job) for job in jobs],
    }


_ROLE_BADGES = {"primary": "P", "worker": "W"}


def _role_badge(entry: dict[str, Any]) -> str:
    if entry["provider"] == "claude":
        return "C"
    if entry["provider"] == "local":
        return "L"
    return _ROLE_BADGES.get(str(entry["role"]), "–")


def _chip(entry: dict[str, Any]) -> tui_style.Chip:
    task = entry["task"]["label"] if entry["task"] else None
    exhausted = next(
        (
            tui_style.window_label(window["name"], window["minutes"], short=True)
            for window in entry["windows"]
            if window["remaining_pct"] <= 0
        ),
        None,
    )
    return tui_style.status_chip(
        entry["provider"],
        entry["status"],
        task=task,
        lifecycle=entry["lifecycle"],
        mode=entry["mode"],
        exhausted=exhausted,
        stale=bool(entry["windows"]) and not entry["fresh"],
    )


def render_text(
    view: dict[str, Any], *, color: bool = False, width: int = 200
) -> str:
    """The same model as a table, in the shared terminal vocabulary."""
    pool = summary(view)
    current = float(view["pool"]["observed_at"])

    def target(size: str) -> str:
        chosen = pool["next"].get(size)
        return chosen["label"] if chosen else "none"

    strip: tui_style.Parts = [
        (f"Ready {pool['ready']}/{pool['total']}",
         "ok" if pool["ready"] == pool["total"] else "warn"),
        (" · ", "muted"),
        (f"Running {pool['running']}", "live" if pool["running"] else None),
        (" · ", "muted"),
        (f"Queued {pool['queued']}", None),
    ]
    if pool["waiting_capacity"]:
        strip += [(" · ", "muted"), (f"Waiting {pool['waiting_capacity']}", "warn")]
    strip += [
        (" · ", "muted"),
        (f"Next small → {target('small')}, big → {target('big')}", "accent"),
    ]
    if pool["banked_resets"] is not None:
        strip += [(" · ", "muted"), (f"Resets {pool['banked_resets']}", None)]

    rows = []
    for entry in view["entries"]:
        resets = [window["resets_at"] for window in entry["windows"] if window["resets_at"]]
        fresh = entry["fresh"]
        rows.append({
            "role": _role_badge(entry),
            "seat": (
                entry["label"] if entry["label"] == entry["seat"] or entry["provider"] != "codex"
                else f"{entry['seat']} {entry['label']}"
            ) + f" ({entry['plan']})",
            "5h": tui_style.usage_bar(entry["left"]["5h"], 8, fresh=fresh),
            "7d": tui_style.usage_bar(entry["left"]["7d"], 8, fresh=fresh),
            "reset": tui_style.until(min(resets), current) if resets else "—",
            "chip": _chip(entry),
            "fits": "+".join(entry["fits"]) or "none",
        })
    seat_width = max([len("SEAT (PLAN)"), *(tui_style.display_width(row["seat"]) for row in rows)])
    reset_width = max([len("RESET"), *(len(row["reset"]) for row in rows)])
    chip_width = max([len("STATUS"), *(tui_style.display_width(row["chip"].text) for row in rows)])

    def pad(text: str, cells: int) -> str:
        return text + " " * max(0, cells - tui_style.display_width(text))

    lines = [
        tui_style.render_parts(strip, width, color),
        "",
        tui_style.render_parts([(
            f"ROLE  {pad('SEAT (PLAN)', seat_width)}  {pad('5H', 13)}  {pad('7D', 13)}  "
            f"{pad('RESET', reset_width)}  {pad('STATUS', chip_width)}  FITS",
            "muted",
        )], width, color),
    ]
    for row in rows:
        chip = row["chip"]
        parts: tui_style.Parts = [
            (pad(row["role"], 4), "accent" if row["role"] == "P" else None),
            ("  ", None),
            (pad(row["seat"], seat_width), None),
            ("  ", None),
            *row["5h"],
            ("  ", None),
            *row["7d"],
            ("  ", None),
            (pad(row["reset"], reset_width), "muted"),
            ("  ", None),
            (pad(chip.text, chip_width), chip.role),
            ("  ", None),
            (row["fits"], "muted" if row["fits"] == "none" else None),
        ]
        lines.append(tui_style.render_parts(parts, width, color))
    if any(entry["used_last"] for entry in view["entries"]):
        lines.append(tui_style.render_parts(
            [("P = primary, used only when no worker fits", "muted")], width, color
        ))
    for warning in view["pool"]["warnings"]:
        lines.append(tui_style.render_parts([(f"! {warning['message']}", "warn")], width, color))
    return "\n".join(lines)
