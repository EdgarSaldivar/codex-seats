from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .capacity import seat_capacity_report
from .display import (
    _claude_remaining,
    _control_claim,
    _reset_windows,
    _rotation_warning,
    _view_seat_ids,
    role_badges,
)
from .tui_style import ago, day, human, stamp, until, window_label
from . import dispatch_client
from .dispatch_protocol import DispatchError, count_ready_frames
from .state import (
    Ledger,
    claude_usage_age_seconds,
    claude_usage_fresh,
    effective_claude_mode,
    iso,
    load_claude_rate_limits,
    parse_iso,
    read_pool_snapshot,
    refresh_transients,
    seat_roles,
)
from .telemetry import refresh_passive_usage, telemetry_class_counts
from .operator_reporting import schema3_operator_report


def _active_usage_cycles(
    cycles: Any, limits: Any, *, current: float
) -> tuple[dict[str, Any], list[str]]:
    """Bound status output to cycles for the provider's current active windows."""
    if not isinstance(cycles, dict):
        return {}, []
    active_names: set[str] = set()
    if isinstance(limits, dict):
        for name, window in limits.items():
            if not isinstance(window, dict):
                continue
            reset = window.get("resets_at")
            if (
                isinstance(reset, (int, float))
                and not isinstance(reset, bool)
                and float(reset) <= current
            ):
                continue
            active_names.add(name)
    active = {
        name: cycles[name]
        for name in sorted(active_names)
        if isinstance(cycles.get(name), dict)
    }
    inactive_names = sorted(
        name
        for name in set(cycles) - active_names
        if name not in {"usage_cycles", "metadata"}
    )[:8]
    return active, inactive_names


def _next_active_reset(limits: Any, *, current: float) -> float | None:
    if not isinstance(limits, dict):
        return None
    resets = [reset for _name, reset in _reset_windows(limits) if reset > current]
    return min(resets) if resets else None

def _last_reset_observation(cycles: dict[str, Any], name: str) -> str:
    cycle = cycles.get(name)
    if not isinstance(cycle, dict):
        return "none observed since tracking began"
    raw = cycle.get("last_reset_observed_at")
    timestamp = float(raw) if isinstance(raw, (int, float)) else parse_iso(raw)
    if timestamp is None:
        return "none observed since tracking began"
    cause = cycle.get("last_reset_cause")
    suffix = f"; {cause}" if cause else ""
    return f"{stamp(timestamp)}{suffix}"


def resets_report(root: Path, settings: dict[str, Any]) -> str:
    """Human-readable normal quota-window reset inventory."""
    with Ledger(root) as data:
        refresh_transients(data, root, settings)
        refresh_passive_usage(data, root, settings)
        snapshot = json.loads(json.dumps(data))
    badges = role_badges(settings)
    mode, reason = effective_claude_mode(settings)
    claude_limits = load_claude_rate_limits(root) if mode != "off" else {}
    operator = schema3_operator_report(
        settings, snapshot, current=datetime.now().timestamp(), claude_limits=claude_limits
    )
    lines = [f"RESET INVENTORY{' ' * 35}{iso()}", "Codex windows:"]
    if operator:
        counts = operator["lifecycle"]
        lines.insert(
            1,
            f"Pool: {operator['healthy']} healthy / {operator['active']} active; "
            f"lifecycle active={counts['active']} draining={counts['draining']} "
            f"inactive={counts['inactive']} retired={counts['retired']}; "
            f"ceilings Codex={operator['current_codex_head_ceiling']} "
            f"Claude={operator['claude_head_ceiling']} safety={operator['global_safety_ceiling']}",
        )
    for seat_id in _view_seat_ids(settings):
        seat = snapshot["seats"][seat_id]
        entry = settings["pool"]["seats"][seat_id]
        lines.append(
            f"  {badges[seat_id]} {seat_id} [{human('lifecycle', entry['lifecycle']).lower()}]"
        )
        from .account import banked_reset_report
        bank = banked_reset_report(seat, settings, root=root, seat_id=seat_id)
        count = bank['available_count'] if bank['status'] == 'fresh' else 'unknown'
        lines.append(
            f"    banked resets: {count} ({human('bank_status', bank['status']).lower()}; "
            f"observed {ago(bank['observed_at'])})"
        )
        for credit in bank['credits']:
            granted = credit.get('granted_at')
            expires = credit.get('expires_at')
            lines.append(f"      {credit.get('title') or credit.get('reset_type') or 'credit'}: "
                         f"{credit.get('status') or 'unknown'}, granted {day(granted) if granted else 'unknown'}, "
                         f"expires {day(expires) if expires else ('none' if credit.get('expiry_status') == 'none' else 'unknown')}")
        report = operator["seat_capacity"][seat_id]
        declared = entry.get("relative_capacity")
        lines.append(
            f"    {entry['label']} · {human('capacity_profile', entry['capacity_profile'])} · "
            f"declared {'unverified' if declared is None else f'{float(declared):g}x'} / "
            f"effective {report['effective_relative_multiplier']:g}x · "
            f"{entry['capacity_source']} · verified {entry['capacity_verified_at'] or 'never'}"
        )
        if report["unverified"]:
            lines.append("    warning: unverified capacity profile")
        if report["profile_window_drift"]:
            lines.append(f"    warning: {report['profile_window_drift_reason']}")
        windows = _reset_windows(seat.get("last_probe_limits", {}))
        if not windows:
            lines.append("    windows: unknown")
        else:
            for name, reset_at in windows:
                raw = seat.get("last_probe_limits", {}).get(name, {})
                pct = raw.get("pct") if isinstance(raw, dict) else None
                left = "?" if not isinstance(pct, (int, float)) else f"{float(pct):g}% left"
                minutes = raw.get("window_minutes") if isinstance(raw, dict) else None
                lines.append(
                    f"    {window_label(name, minutes):7} {left:10} → {stamp(reset_at)} "
                    f"({until(reset_at)})"
                )
                if isinstance(pct, (int, float)):
                    report_window = next(
                        (
                            window
                            for window in operator["seat_capacity"][seat_id][
                                "capacity_windows"
                            ]
                            if window["name"] == name
                            or window_label(window["name"], short=True)
                            == window_label(name, minutes, short=True)
                        ),
                        None,
                    )
                    if report_window is not None:
                        lines.append(
                            "      baseline units: "
                            f"available={report_window['available_baseline_units']:g}, "
                            f"usable={report_window['usable_baseline_units']:g}"
                        )
                lines.append(
                    f"      last observed reset: "
                    f"{_last_reset_observation(seat.get('usage_cycles', {}), name)}"
                )

    limits = claude_limits
    lines.append(
        f"Claude windows (mode {human('claude_mode', mode)}{'; ' + reason if reason else ''}):"
    )
    if not limits:
        lines.append("  unknown; run a useful interactive Claude turn to refresh its status-line data")
    else:
        for name, reset_at in _reset_windows(limits):
            raw = limits.get(name, {})
            remaining = raw.get("remaining_percentage") if isinstance(raw, dict) else None
            left = "?" if not isinstance(remaining, (int, float)) else f"{float(remaining):g}% left"
            lines.append(
                f"  {window_label(name):7} {left:10} → {stamp(reset_at)} ({until(reset_at)})"
            )
            lines.append(
                f"    last observed reset: "
                f"{_last_reset_observation(limits.get('usage_cycles', {}), name)}"
            )
    lines.append(
        "Notes: Codex/Claude window times are provider timestamps. "
        "Last resets are forward observations only, never reconstructed history."
    )
    return "\n".join(lines)


def resets_status_json(root: Path, settings: dict[str, Any]) -> str:
    status = json.loads(status_json(root, settings))
    result = {
        "generated_at": status.get("generated_at"),
        "codex": {
            seat_id: {
                "role": seat.get("role"),
                "limits": seat.get("limits", {}),
                "next_reset_at": seat.get("next_reset_at"),
                "usage_cycles": seat.get("usage_cycles", {}),
                "banked_resets": seat.get("banked_resets", {}),
                "inactive_historical_window_names": seat.get(
                    "inactive_historical_window_names", []
                ),
                **(
                    {
                        "label": seat.get("label"),
                        "lifecycle": seat.get("lifecycle"),
                        "routable": seat.get("routable"),
                        "healthy": seat.get("healthy"),
                        "capacity_profile": seat.get("capacity_profile"),
                        "declared_relative_multiplier": seat.get(
                            "declared_relative_multiplier"
                        ),
                        "effective_relative_multiplier": seat.get(
                            "effective_relative_multiplier"
                        ),
                        "capacity_source": seat.get("capacity_source"),
                        "capacity_verified_at": seat.get("capacity_verified_at"),
                        "capacity_fingerprint": seat.get("capacity_fingerprint"),
                        "active_capacity_windows": seat.get(
                            "active_capacity_windows", []
                        ),
                        "binding_window": seat.get("binding_window"),
                        "binding_available_baseline_units": seat.get(
                            "binding_available_baseline_units"
                        ),
                        "binding_usable_baseline_units": seat.get(
                            "binding_usable_baseline_units"
                        ),
                        "unverified": seat.get("unverified"),
                        "profile_window_drift": seat.get("profile_window_drift"),
                        "profile_window_drift_reason": seat.get(
                            "profile_window_drift_reason"
                        ),
                    }
                    if "lifecycle" in seat
                    else {}
                ),
            }
            for seat_id, seat in status.get("seats", {}).items()
        },
        "claude": {
            "mode": status.get("claude", {}).get("mode"),
            "rate_limits": status.get("claude", {}).get("rate_limits", {}),
            "next_reset_at": status.get("claude", {}).get("next_reset_at"),
            "usage_cycles": status.get("claude", {}).get("usage_cycles", {}),
            "inactive_historical_window_names": status.get("claude", {}).get(
                "inactive_historical_window_names", []
            ),
        },
    }
    if "pool_summary" in status:
        result["pool_summary"] = status["pool_summary"]
    return json.dumps(result, indent=2)


def status_json(root: Path, settings: dict[str, Any]) -> str:
    from .provider_claude import claude_capacity_report
    from .runtime_readers import build_metadata, status_snapshot

    background_error = None
    try:
        background_jobs = dispatch_client.live_jobs(root, settings)
    except DispatchError:
        background_jobs = []
        background_error = "queue security validation failed"
    background_counts = dispatch_client.background_counts(background_jobs)

    def build_result(data: dict[str, Any]) -> dict[str, Any]:
        refresh_transients(data, root, settings)
        refresh_passive_usage(data, root, settings)
        roles = seat_roles(settings)
        claude_mode = effective_claude_mode(settings)[0]
        limits = load_claude_rate_limits(root) if claude_mode != "off" else {}
        five_hour = _claude_remaining(limits, "five_hour")
        seven_day = _claude_remaining(limits, "seven_day")
        remaining = [
            value
            for value in (five_hour, seven_day)
            if value is not None
        ]
        claude_state = data["providers"]["claude"]
        claude_capacity = claude_capacity_report(
            settings, claude_state, limits, mode=claude_mode
        )
        control = _control_claim(data, settings)
        current = datetime.now().timestamp()
        view_seat_ids = _view_seat_ids(settings)
        operator = schema3_operator_report(
            settings, data, current=current, claude_limits=limits
        )
        capacity = (
            operator["seat_capacity"]
            if operator
            else {
                seat_id: seat_capacity_report(
                    seat_id,
                    data["seats"][seat_id],
                    settings,
                    current=current,
                    learning=data.get("capacity_learning", {}),
                )
                for seat_id in view_seat_ids
            }
        )

        def seat_result(seat_id: str, seat: dict[str, Any]) -> dict[str, Any]:
            from .account import banked_reset_report
            limits = seat.get("last_probe_limits", {})
            active_cycles, inactive_names = _active_usage_cycles(
                seat.get("usage_cycles"), limits, current=current
            )
            report = capacity[seat_id]
            result = {
                "role": roles.get(seat_id),
                "type": seat.get("type"),
                "status": seat.get("status"),
                "auth_ok": report["auth_ok"],
                "busy": bool(seat.get("in_flight")),
                "pct_left": seat.get("pct_left"),
                "reserve_pct": report["reserve_pct"],
                "usable_headroom_pct": report["usable_headroom_pct"],
                "fits_small": report["fits_small"],
                "fits_big": report["fits_big"],
                "capacity_tier": report["tier"],
                "capacity_reason": report["reason"],
                "capacity": {"small": report["small"], "big": report["big"]},
                "probed_at": seat.get("probed_at"),
                "usage_updated_at": seat.get("usage_updated_at"),
                "usage_source": seat.get("usage_source"),
                "usage_cycles": active_cycles,
                "inactive_historical_window_names": inactive_names,
                "next_reset_at": _next_active_reset(limits, current=current),
                "active": (
                    {
                        key: seat["in_flight"].get(key)
                        for key in (
                            "kind", "size", "effort", "label", "started_at",
                            "tokens", "cached_tokens", "output_tokens", "context_pct",
                            "rollout_count", "session", "starting_pct",
                        )
                    }
                    if isinstance(seat.get("in_flight"), dict)
                    else None
                ),
                "limits": limits,
                "banked_resets": banked_reset_report(seat, settings, current, root=root, seat_id=seat_id),
            }
            entry = settings["pool"]["seats"][seat_id]
            result.update(
                {
                    "label": entry["label"],
                    "lifecycle": entry["lifecycle"],
                    "routable": entry["lifecycle"] == "active"
                    and bool(report["fits_small"]),
                    "healthy": seat_id in operator["healthy_seat_ids"],
                    "capacity_profile": report["capacity_profile"],
                    "declared_relative_multiplier": report[
                        "declared_relative_multiplier"
                    ],
                    "effective_relative_multiplier": report[
                        "effective_relative_multiplier"
                    ],
                    "capacity_source": report["capacity_source"],
                    "capacity_verified_at": report["capacity_verified_at"],
                    "capacity_fingerprint": report["capacity_fingerprint"],
                    "active_capacity_windows": report["capacity_windows"],
                    "binding_window": report["binding_window"],
                    "binding_available_baseline_units": report[
                        "binding_available_baseline_units"
                    ],
                    "binding_usable_baseline_units": report[
                        "binding_usable_baseline_units"
                    ],
                    "unverified": report["unverified"],
                    "unverified_reasons": report["unverified_reasons"],
                    "zero_authoritative_windows": report[
                        "zero_authoritative_windows"
                    ],
                    "expected_five_hour_window": report[
                        "expected_five_hour_window"
                    ],
                    "five_hour_window_reported": report[
                        "five_hour_window_reported"
                    ],
                    "profile_window_drift": report["profile_window_drift"],
                    "profile_window_drift_reason": report[
                        "profile_window_drift_reason"
                    ],
                }
            )
            if entry["lifecycle"] != "active":
                result["fits_small"] = False
                result["fits_big"] = False
            return result

        claude_cycles, claude_inactive = _active_usage_cycles(
            limits.get("usage_cycles"), limits, current=current
        )
        public_claude_limits = {
            key: value for key, value in limits.items() if key != "usage_cycles"
        }
        result = {
            "generated_at": iso(),
            "runtime_reader": build_metadata(
                "status-command",
                settings,
                root=root,
                process_identity=hashlib.sha256(
                    f"status-command:{os.getpid()}".encode()
                ).hexdigest()[:32],
                module_file=__file__,
            ),
            "settings_readers": status_snapshot(root),
            "seats": {
                seat_id: seat_result(seat_id, data["seats"][seat_id])
                for seat_id in view_seat_ids
            },
            "claude": {
                "mode": claude_mode,
                "status": claude_state.get("status"),
                "busy": bool(claude_state.get("in_flight")),
                "last_job_at": claude_state.get("last_job_at"),
                "last_resolved_model": claude_state.get("last_resolved_model"),
                "pct_left": (
                    min(remaining)
                    if five_hour is not None and seven_day is not None
                    else None
                ),
                "usage_fresh": claude_usage_fresh(settings, limits),
                "usage_age_seconds": claude_usage_age_seconds(limits),
                "fits_small": claude_capacity["fits_small"],
                "fits_big": claude_capacity["fits_big"],
                "rate_limits": public_claude_limits,
                "usage_cycles": claude_cycles,
                "inactive_historical_window_names": claude_inactive,
                "next_reset_at": _next_active_reset(limits, current=current),
                "models": claude_state.get("models", {}),
                "eligible_kinds": settings["providers"]["claude"].get("eligible_kinds", []),
            },
            "controller": (
                {
                    "seat": control[0],
                    "session": control[1].get("session", {}),
                    "starting_pct": control[1].get("starting_pct"),
                    "rotation_warning": (
                        _rotation_warning(data, settings) or (None, None, None)
                    )[2],
                }
                if control
                else None
            ),
            "pending": count_ready_frames(root),
            "background_jobs": {
                "jobs": background_jobs,
                "counts": background_counts,
                "wait_backend": dispatch_client.receipt_wait_backend(),
                "error": background_error,
            },
            "efficiency": {
                "raw_tokens": sum(int(job.get("raw_tokens", job.get("tokens", 0)) or 0) for job in data.get("jobs", [])),
                "cached_tokens": sum(int(job.get("cached_tokens", 0) or 0) for job in data.get("jobs", [])),
                "uncached_tokens": sum(int(job.get("uncached_tokens", 0) or 0) for job in data.get("jobs", [])),
                "output_tokens": sum(int(job.get("output_tokens", 0) or 0) for job in data.get("jobs", [])),
                "reasoning_tokens": sum(int(job.get("reasoning_tokens", 0) or 0) for job in data.get("jobs", [])),
                "telemetry_classification": telemetry_class_counts(data.get("jobs", [])),
                "learning_sample_count": sum(len(item.get("samples", [])) for item in data.get("capacity_learning", {}).values() if isinstance(item, dict)),
                "cap_risk_count": sum(bool(item.get("risk_floor_pct")) for item in data.get("capacity_learning", {}).values() if isinstance(item, dict)),
            },
        }
        lifecycle_counts = {
            lifecycle: sum(
                entry.get("lifecycle") == lifecycle
                for entry in settings["pool"]["seats"].values()
            )
            for lifecycle in ("active", "draining", "inactive", "retired")
        }
        result["seat_counts"] = {
            "healthy": operator["healthy"],
            **lifecycle_counts,
        }
        result["pool_summary"] = {
            key: value
            for key, value in operator.items()
            if key != "seat_capacity"
        }
        return result

    return json.dumps(build_result(read_pool_snapshot(root)), indent=2)
