from __future__ import annotations

import copy
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .seat_registry import registered_seat_ids
from .state import (
    iso,
    parse_iso,
    restore_account_usage_snapshot,
    runtime_seat_home,
)


TAIL_START_BYTES = 256 * 1024
TAIL_MAX_BYTES = 8 * 1024 * 1024
DISCOVERY_MARGIN_SECONDS = 30
JOB_TELEMETRY_SCHEMA = "codex-seat.job.v5.3"
JOBS_CAP = 1000


def trim_jobs_with_archive(data: dict[str, Any], root: Path, *, cap: int = JOBS_CAP) -> None:
    """Truncate ``data["jobs"]`` to the newest ``cap`` entries, archiving the
    dropped ones to ``<root>/archive/jobs-YYYY-MM.jsonl`` (bucketed by the job's
    own ``ts`` month) so long-horizon routing trends survive the ledger cap.
    Archiving is best-effort: an unwritable archive must never break job
    recording, so the truncation is applied first and OSErrors are swallowed."""
    jobs = data.get("jobs") or []
    if len(jobs) <= cap:
        return
    dropped, data["jobs"] = jobs[:-cap], jobs[-cap:]
    try:
        archive = Path(root) / "archive"
        archive.mkdir(mode=0o700, exist_ok=True)
        buckets: dict[str, list[str]] = {}
        fallback_month = datetime.now().strftime("%Y-%m")
        for job in dropped:
            month = str((job or {}).get("ts", ""))[:7]
            if len(month) != 7 or month[4] != "-":
                month = fallback_month
            buckets.setdefault(month, []).append(json.dumps(job, default=str))
        for month, lines in buckets.items():
            with open(archive / f"jobs-{month}.jsonl", "a") as handle:
                handle.write("\n".join(lines) + "\n")
    except OSError:
        pass


PRECISE_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "provider_tokens",
)


def validate_job_tokens(job: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    values: dict[str, int] = {}
    for key in PRECISE_TOKEN_FIELDS:
        value = job.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(f"{key} must be a nonnegative integer")
        else:
            values[key] = value
    if len(values) != len(PRECISE_TOKEN_FIELDS):
        return errors
    if values["input_tokens"] != values["cached_input_tokens"] + values["uncached_input_tokens"]:
        errors.append("input_tokens must equal cached_input_tokens + uncached_input_tokens")
    if values["provider_tokens"] != values["input_tokens"] + values["output_tokens"]:
        errors.append("provider_tokens must equal input_tokens + output_tokens")
    return errors


def classify_job_telemetry(job: dict[str, Any]) -> str:
    """Classify in memory; legacy ledger history is never rewritten."""
    if job.get("telemetry_schema") == JOB_TELEMETRY_SCHEMA:
        return "current" if not validate_job_tokens(job) else "malformed-current"
    if (
        job.get("provider") == "local"
        and job.get("telemetry_schema") is None
        and job.get("local_usage_available") is False
        and job.get("telemetry_classification") == "usage-unavailable"
        and all(job.get(key) is None for key in PRECISE_TOKEN_FIELDS)
        and not {"tokens", "raw_tokens", "cached_tokens", "uncached_tokens"} & set(job)
    ):
        return "usage-unavailable"
    legacy_fields = {"tokens", "raw_tokens", "cached_tokens", "uncached_tokens"}
    if legacy_fields & set(job):
        return "legacy"
    return "unclassified"


def telemetry_class_counts(jobs: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "current": 0,
        "malformed-current": 0,
        "usage-unavailable": 0,
        "legacy": 0,
        "unclassified": 0,
    }
    for job in jobs:
        if isinstance(job, dict):
            classification = classify_job_telemetry(job)
        else:
            classification = "unclassified"
        counts[classification] += 1
    return counts


def _event_time(value: Any, fallback: float) -> float:
    if isinstance(value, str):
        parsed = parse_iso(value)
        if parsed is not None:
            return parsed
    return fallback


def read_rollout_telemetry(path: Path) -> dict[str, Any] | None:
    """Read newest usage and turn context without loading a full rollout."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size <= 0:
        return None

    window = min(size, TAIL_START_BYTES)
    while True:
        try:
            with path.open("rb") as handle:
                handle.seek(max(0, size - window))
                payload = handle.read()
        except OSError:
            return None
        token_event: dict[str, Any] | None = None
        turn_context: dict[str, Any] | None = None
        turn_context_at: Any = None
        for raw_line in reversed(payload.splitlines()):
            try:
                event = json.loads(raw_line)
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            body = event.get("payload")
            if (
                token_event is None
                and event.get("type") == "event_msg"
                and isinstance(body, dict)
                and body.get("type") == "token_count"
            ):
                token_event = {
                    "timestamp": event.get("timestamp"),
                    "info": body.get("info") if isinstance(body.get("info"), dict) else {},
                    "rate_limits": (
                        body.get("rate_limits")
                        if isinstance(body.get("rate_limits"), dict)
                        else {}
                    ),
                }
            elif (
                turn_context is None
                and event.get("type") == "turn_context"
                and isinstance(body, dict)
            ):
                collaboration = (
                    body.get("collaboration_mode")
                    if isinstance(body.get("collaboration_mode"), dict)
                    else {}
                )
                collaboration_settings = (
                    collaboration.get("settings")
                    if isinstance(collaboration.get("settings"), dict)
                    else {}
                )
                turn_context = {
                    "mode": collaboration.get("mode") or "default",
                    "model": body.get("model") or collaboration_settings.get("model"),
                    "effort": (
                        body.get("effort")
                        or collaboration_settings.get("reasoning_effort")
                    ),
                    "approval_policy": body.get("approval_policy"),
                    "approvals_reviewer": body.get("approvals_reviewer"),
                    "sandbox": (
                        body.get("sandbox_policy", {}).get("type")
                        if isinstance(body.get("sandbox_policy"), dict)
                        else None
                    ),
                }
                turn_context_at = event.get("timestamp")
            if token_event is not None and turn_context is not None:
                token_event["session"] = turn_context
                token_event["session_at"] = turn_context_at
                return token_event
        if window >= size or window >= TAIL_MAX_BYTES:
            if token_event is not None:
                token_event["session"] = turn_context or {}
                token_event["session_at"] = turn_context_at
            return token_event
        window = min(size, window * 2, TAIL_MAX_BYTES)


def _rollouts(home: Path, after: float | None = None) -> list[tuple[Path, Any]]:
    sessions = home / "sessions"
    if not sessions.is_dir():
        return []
    found: list[tuple[Path, Any]] = []
    for path in sessions.rglob("rollout-*.jsonl"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if not path.is_file() or path.is_symlink():
            continue
        if after is not None and stat.st_mtime < after:
            continue
        found.append((path, stat))
    return found


def _safe_cached_path(home: Path, value: str) -> Path | None:
    try:
        sessions = (home / "sessions").resolve()
        path = Path(value)
        resolved = path.resolve()
        if not resolved.is_relative_to(sessions) or path.is_symlink():
            return None
        return path
    except (OSError, ValueError):
        return None


def _window_name(minutes: float) -> str:
    if 270 <= minutes <= 330:
        return "5h"
    if 9_000 <= minutes <= 11_000:
        return "weekly"
    if 40_000 <= minutes <= 46_000:
        return "monthly"
    if minutes < 24 * 60:
        return f"{minutes / 60:g}h"
    return f"{minutes / (24 * 60):g}d"


def _display_reset(reset_at: float) -> str:
    reset = datetime.fromtimestamp(reset_at).astimezone()
    if reset.date() == datetime.now().astimezone().date():
        return f"resets {reset:%H:%M}"
    return f"resets {reset:%H:%M} on {reset.day} {reset:%b}"


def _clean_pct(value: float) -> int | float:
    rounded = round(value)
    return int(rounded) if abs(value - rounded) < 0.001 else round(value, 1)


def limits_from_event(rate_limits: dict[str, Any], at: float | None = None) -> dict[str, Any]:
    current = time.time() if at is None else at
    limits: dict[str, Any] = {}
    for key in ("primary", "secondary", "individual_limit"):
        raw = rate_limits.get(key)
        if not isinstance(raw, dict):
            continue
        try:
            used = float(raw["used_percent"])
            minutes = float(raw["window_minutes"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= used <= 100 or minutes <= 0:
            continue
        reset_at = raw.get("resets_at")
        try:
            reset_at = float(reset_at) if reset_at is not None else None
        except (TypeError, ValueError):
            reset_at = None
        # An expired short window no longer constrains routing. A future model
        # event or structured account read will publish its replacement window.
        if reset_at is not None and reset_at <= current:
            continue
        name = _window_name(minutes)
        left = _clean_pct(100 - used)
        limits[name] = {
            "pct": left,
            "kind": "left",
            "used_pct": _clean_pct(used),
            "window_minutes": int(minutes),
            "resets_at": reset_at,
            "resets": _display_reset(reset_at) if reset_at is not None else "",
        }
        plan_type = raw.get("plan_type")
        if isinstance(plan_type, str) and plan_type.strip():
            # Provider-owned descriptive metadata only. Routing continues to
            # use the explicitly configured capacity profile and multiplier.
            limits[name]["plan_type"] = plan_type.strip()[:128]
    return limits


def _cached_entry(path: Path, stat: Any, cache: dict[str, Any]) -> dict[str, Any] | None:
    key = str(path)
    previous = cache.get(key)
    if (
        isinstance(previous, dict)
        and previous.get("mtime_ns") == stat.st_mtime_ns
        and previous.get("cache_schema") == 4
    ):
        return previous
    telemetry = read_rollout_telemetry(path)
    if telemetry is None:
        return None
    info = telemetry.get("info", {})
    total = info.get("total_token_usage") if isinstance(info, dict) else {}
    last = info.get("last_token_usage") if isinstance(info, dict) else {}
    context_window = info.get("model_context_window") if isinstance(info, dict) else None
    total = total if isinstance(total, dict) else {}
    last = last if isinstance(last, dict) else {}
    try:
        context_pct = (
            100 * float(last.get("total_tokens", 0)) / float(context_window)
            if context_window
            else None
        )
    except (TypeError, ValueError, ZeroDivisionError):
        context_pct = None
    entry = {
        "cache_schema": 4,
        "path": key,
        "mtime_ns": stat.st_mtime_ns,
        "mtime": stat.st_mtime,
        "birthtime": getattr(stat, "st_birthtime", stat.st_mtime),
        "event_at": telemetry.get("timestamp"),
        "event_ts": _event_time(telemetry.get("timestamp"), stat.st_mtime),
        "tokens": int(total.get("total_tokens", 0) or 0),
        "cached_tokens": int(total.get("cached_input_tokens", 0) or 0),
        "output_tokens": int(total.get("output_tokens", 0) or 0),
        "context_pct": round(context_pct, 1) if context_pct is not None else None,
        "rate_limits": telemetry.get("rate_limits", {}),
        "session": telemetry.get("session", {}),
        "session_at": telemetry.get("session_at"),
        "session_ts": _event_time(telemetry.get("session_at"), stat.st_mtime),
    }
    cache[key] = entry
    return entry


def record_usage_cycles(
    seat: dict[str, Any],
    limits: dict[str, Any],
    entry: dict[str, Any],
    observed_at: float,
) -> None:
    """Track observed quota cycles without mistaking a moving target for a reset."""
    cycles = seat.setdefault("usage_cycles", {})
    if not isinstance(cycles, dict):
        cycles = seat["usage_cycles"] = {}
    observed_iso = iso(observed_at)
    for name, limit in limits.items():
        if not isinstance(limit, dict) or not isinstance(limit.get("pct"), (int, float)):
            continue
        pct = float(limit["pct"])
        reset_at = limit.get("resets_at")
        reset_at = float(reset_at) if isinstance(reset_at, (int, float)) else None
        current = cycles.get(name)
        if not isinstance(current, dict):
            current = {
                "observed_resets": 0,
                "current_reset_at": reset_at,
                "first_seen_at": observed_iso,
                "last_seen_at": observed_iso,
                "starting_pct": pct,
                "last_pct": pct,
                "minimum_pct": pct,
                "consumed_pct": 0,
                "previous_cycles": [],
                "raw_tokens_at_first": int(entry.get("tokens", 0) or 0),
                "cached_tokens_at_first": int(entry.get("cached_tokens", 0) or 0),
                "output_tokens_at_first": int(entry.get("output_tokens", 0) or 0),
            }
            cycles[name] = current
        else:
            old_reset = current.get("current_reset_at")
            old_reset = float(old_reset) if isinstance(old_reset, (int, float)) else None
            last_pct = current.get("last_pct")
            last_pct = float(last_pct) if isinstance(last_pct, (int, float)) else pct
            boundary_advanced = (
                reset_at is not None
                and old_reset is not None
                and reset_at > old_reset + 60
            )
            recovered = pct >= last_pct + 5
            prior_boundary_passed = old_reset is not None and observed_at >= old_reset - 120
            if boundary_advanced and (recovered or prior_boundary_passed):
                reset_cause = str(entry.get("reset_cause") or "provider-window")
                history = current.get("previous_cycles")
                if not isinstance(history, list):
                    history = []
                starting = current.get("starting_pct")
                minimum = current.get("minimum_pct")
                starting = float(starting) if isinstance(starting, (int, float)) else last_pct
                minimum = float(minimum) if isinstance(minimum, (int, float)) else last_pct
                history.append(
                    {
                        "reset_at": old_reset,
                        "first_seen_at": current.get("first_seen_at"),
                        "last_seen_at": current.get("last_seen_at"),
                        "starting_pct": _clean_pct(starting),
                        "ending_pct": _clean_pct(last_pct),
                        "minimum_pct": _clean_pct(minimum),
                        "consumed_pct": _clean_pct(max(0.0, starting - minimum)),
                        "unused_pct": _clean_pct(last_pct),
                        "observed_at": observed_iso,
                        "reset_cause": reset_cause,
                    }
                )
                current = {
                    "observed_resets": int(current.get("observed_resets", 0)) + 1,
                    "last_reset_observed_at": observed_iso,
                    "last_reset_cause": reset_cause,
                    "current_reset_at": reset_at,
                    "first_seen_at": observed_iso,
                    "last_seen_at": observed_iso,
                    "starting_pct": pct,
                    "last_pct": pct,
                    "minimum_pct": pct,
                    "consumed_pct": 0,
                    "previous_cycles": history[-20:],
                    "raw_tokens_at_first": int(entry.get("tokens", 0) or 0),
                    "cached_tokens_at_first": int(entry.get("cached_tokens", 0) or 0),
                    "output_tokens_at_first": int(entry.get("output_tokens", 0) or 0),
                }
                cycles[name] = current
            elif boundary_advanced:
                # Rolling targets can move without an actual allowance reset.
                current["current_reset_at"] = reset_at
                current["target_advanced_unconfirmed_at"] = observed_iso

        starting = current.get("starting_pct")
        minimum = current.get("minimum_pct")
        starting = float(starting) if isinstance(starting, (int, float)) else pct
        minimum = min(float(minimum), pct) if isinstance(minimum, (int, float)) else pct
        current.update(
            {
                "last_seen_at": observed_iso,
                "last_pct": _clean_pct(pct),
                "minimum_pct": _clean_pct(minimum),
                "consumed_pct": _clean_pct(max(0.0, starting - minimum)),
                "raw_tokens_at_last": int(entry.get("tokens", 0) or 0),
                "cached_tokens_at_last": int(entry.get("cached_tokens", 0) or 0),
                "output_tokens_at_last": int(entry.get("output_tokens", 0) or 0),
            }
        )


def apply_usage_limits(
    seat: dict[str, Any],
    limits: dict[str, Any],
    entry: dict[str, Any],
    observed_at: float,
    *,
    source: str,
    updated_at: str | None = None,
    observation_complete: bool = False,
) -> bool:
    """Apply one ordered window observation without overreading partial omissions."""
    complete_account = source == "account" and observation_complete
    if source == "account":
        record_account_observation_success(
            seat, observed_at, complete=complete_account
        )
    stored_at = seat.get("usage_observed_at")
    if not isinstance(stored_at, (int, float)):
        stored_at = parse_iso(seat.get("usage_updated_at") or seat.get("probed_at"))
    if isinstance(stored_at, (int, float)) and observed_at < float(stored_at):
        return False
    if not limits and not complete_account:
        return False
    if limits:
        record_usage_cycles(seat, limits, entry, observed_at)
    tracked_resets = seat.setdefault("usage_reset_windows", {})
    if not isinstance(tracked_resets, dict):
        tracked_resets = seat["usage_reset_windows"] = {}
    for name in list(tracked_resets):
        tracked = tracked_resets.get(name)
        reset_at = tracked.get("resets_at") if isinstance(tracked, dict) else None
        if not isinstance(reset_at, (int, float)) or float(reset_at) <= observed_at:
            tracked_resets.pop(name, None)
    if complete_account:
        # A successful complete enumeration is the sole source whose omission
        # proves that an unexpired, previously active window is now absent.
        tracked_resets.clear()
    for name, raw in limits.items():
        reset_at = raw.get("resets_at") if isinstance(raw, dict) else None
        if isinstance(reset_at, (int, float)) and float(reset_at) > observed_at:
            tracked_resets[name] = {
                **copy.deepcopy(raw),
                "observed_at": observed_at,
                "source": source,
            }
    active_limits: dict[str, Any] = {}
    if not complete_account:
        previous_limits = seat.get("last_probe_limits")
        if isinstance(previous_limits, dict):
            for name, raw in previous_limits.items():
                reset_at = raw.get("resets_at") if isinstance(raw, dict) else None
                if isinstance(reset_at, (int, float)) and float(reset_at) > observed_at:
                    active_limits[name] = copy.deepcopy(raw)
        for name, raw in tracked_resets.items():
            if name in active_limits or not isinstance(raw, dict):
                continue
            active_limits[name] = {
                key: copy.deepcopy(value)
                for key, value in raw.items()
                if key not in {"observed_at", "source"}
            }
    active_limits.update(copy.deepcopy(limits))
    previous_pct = seat.get("pct_left")
    previous_resets = {
        name: item.get("resets_at")
        for name, item in seat.get("last_probe_limits", {}).items()
        if isinstance(item, dict)
    }
    new_resets = {name: item.get("resets_at") for name, item in active_limits.items()}
    left = (
        min(float(item["pct"]) for item in active_limits.values())
        if active_limits
        else None
    )
    clean_left = _clean_pct(left) if left is not None else None
    if previous_pct != clean_left or previous_resets != new_resets:
        seat["spent_since_probe"] = 0
    # deepcopy: `limits` is the caller's dict and its window entries are mutated
    # in place elsewhere; aliasing it would let those mutations corrupt the seat.
    seat["last_probe_limits"] = active_limits
    seat["pct_left"] = clean_left
    seat["type"] = "window" if active_limits else "unknown"
    timestamp = updated_at or iso(observed_at)
    seat["probed_at"] = timestamp
    seat["usage_updated_at"] = timestamp
    # Numeric observation time is the ORDERING key. iso()/usage_updated_at
    # truncates to whole seconds, so a same-second stale event would otherwise
    # pass the freshness guard and roll a newer read backward.
    seat["usage_observed_at"] = float(observed_at)
    seat["usage_source"] = source
    seat["active_window_observation"] = {
        "source": source,
        "complete": complete_account,
        "observed_at": float(observed_at),
        "updated_at": timestamp,
        "observed_windows": sorted(limits),
        "active_windows": sorted(active_limits),
    }
    if seat.get("status") == "capped" and left is not None and left > 0:
        seat["status"], seat["status_until"] = "ok", None
    if source == "account":
        # A watcher/controller started before passive-observation ordering was
        # fixed can still write an old rollout over these visible fields. It
        # preserves unknown ledger keys, so retain an independent structured
        # snapshot that current readers can use to repair that rollback.
        seat["account_usage_snapshot"] = {
            "limits": copy.deepcopy(active_limits),
            "pct_left": clean_left,
            "type": seat["type"],
            "observed_at": observed_at,
            "updated_at": timestamp,
            "spent_since_probe": seat.get("spent_since_probe", 0),
            "usage_reset_windows": copy.deepcopy(tracked_resets),
            "usage_cycles": copy.deepcopy(seat.get("usage_cycles", {})),
            "complete": complete_account,
            "active_window_observation": copy.deepcopy(
                seat["active_window_observation"]
            ),
        }
    return True


def _account_observation(seat: dict[str, Any]) -> dict[str, Any]:
    observation = seat.setdefault("account_window_observation", {})
    if not isinstance(observation, dict):
        observation = seat["account_window_observation"] = {}
    return observation


def record_account_observation_success(
    seat: dict[str, Any], observed_at: float, *, complete: bool
) -> None:
    """Record account sweep health without replacing newer attempt metadata."""
    observation = _account_observation(seat)
    timestamp = iso(observed_at)
    prior_success = observation.get("last_success_observed_at")
    if not isinstance(prior_success, (int, float)) or observed_at >= float(prior_success):
        observation["last_success_at"] = timestamp
        observation["last_success_observed_at"] = float(observed_at)
    prior_complete = observation.get("last_complete_observed_at")
    if complete and (
        not isinstance(prior_complete, (int, float))
        or observed_at >= float(prior_complete)
    ):
        observation["last_complete_at"] = timestamp
        observation["last_complete_observed_at"] = float(observed_at)
    prior_attempt = observation.get("last_attempt_observed_at")
    if not isinstance(prior_attempt, (int, float)) or observed_at >= float(prior_attempt):
        stale_since = None
        if not complete:
            stale_since = observation.get("stale_since") or timestamp
        observation.update(
            {
                "status": "success" if complete else "partial",
                "last_attempt_at": timestamp,
                "last_attempt_observed_at": float(observed_at),
                "last_attempt_complete": complete,
                "last_error": None,
                "stale_since": stale_since,
            }
        )


def record_account_observation_failure(
    seat: dict[str, Any], observed_at: float, error: str | None
) -> None:
    """Record a failed sweep while preserving every last-good usage field."""
    observation = _account_observation(seat)
    timestamp = iso(observed_at)
    prior_failure = observation.get("last_failure_observed_at")
    if not isinstance(prior_failure, (int, float)) or observed_at >= float(prior_failure):
        observation["last_failure_at"] = timestamp
        observation["last_failure_observed_at"] = float(observed_at)
    prior_attempt = observation.get("last_attempt_observed_at")
    if not isinstance(prior_attempt, (int, float)) or observed_at >= float(prior_attempt):
        stale_since = observation.get("stale_since") or timestamp
        observation.update(
            {
                "status": "failed",
                "last_attempt_at": timestamp,
                "last_attempt_observed_at": float(observed_at),
                "last_attempt_complete": False,
                "last_error": error,
                "stale_since": stale_since,
            }
        )


def _restore_newer_account_usage(seat: dict[str, Any]) -> bool:
    """Repair visible usage if legacy passive code replaced a newer account read."""
    return restore_account_usage_snapshot(seat)


def _apply_latest(seat: dict[str, Any], entry: dict[str, Any], at: float) -> bool:
    rate_limits = entry.get("rate_limits")
    if not isinstance(rate_limits, dict) or not rate_limits:
        return False
    limits = limits_from_event(rate_limits, at=at)
    if not limits:
        return False
    observed_at = float(entry.get("event_ts", entry.get("mtime", at)) or at)
    # Account reads and explicit probes are authoritative observations too. An
    # older rollout can be discovered later (especially after an interactive
    # seat becomes idle), but it must never roll the visible balance backward.
    # Compare the NUMERIC observation time; usage_updated_at is second-truncated
    # and would tie same-second events. Fall back to it only for legacy seats.
    stored_at = seat.get("usage_observed_at")
    if not isinstance(stored_at, (int, float)):
        stored_at = parse_iso(seat.get("usage_updated_at") or seat.get("probed_at"))
    if isinstance(stored_at, (int, float)) and observed_at < float(stored_at):
        return False
    updated_at = entry.get("event_at") or iso(float(entry.get("mtime", at)))
    apply_usage_limits(
        seat,
        limits,
        entry,
        observed_at,
        source="passive",
        updated_at=updated_at,
    )
    seat["telemetry_file"] = entry.get("path")
    seat["telemetry_mtime_ns"] = entry.get("mtime_ns")
    return True


def refresh_passive_usage(
    data: dict[str, Any],
    root: Path,
    settings: dict[str, Any],
    targets: list[str] | None = None,
    *,
    force_discover: bool = False,
    at: float | None = None,
) -> list[str]:
    """Refresh exact first-party usage and active-session token totals from rollouts."""
    current = time.time() if at is None else at
    changed: list[str] = []
    scan_interval = max(1.0, float(settings.get("pool", {}).get("telemetry_scan_seconds", 5)))
    if targets is None:
        targets = registered_seat_ids(settings, lifecycles=("active", "draining"))
    for seat_id in targets:
        if seat_id not in data.get("seats", {}):
            continue
        seat = data["seats"][seat_id]
        restored_account = _restore_newer_account_usage(seat)
        home = runtime_seat_home(root, seat_id, settings)
        claim = seat.get("in_flight") if isinstance(seat.get("in_flight"), dict) else None
        entries: list[dict[str, Any]] = []

        if claim is not None:
            precise_started = claim.get("started_ts")
            started = (
                float(precise_started)
                if isinstance(precise_started, (int, float))
                else parse_iso(claim.get("started_at"))
            )
            margin = 0 if isinstance(precise_started, (int, float)) else DISCOVERY_MARGIN_SECONDS
            threshold = None if started is None else started - margin
            cache = claim.setdefault("telemetry_cache", {})
            last_scan = float(claim.get("telemetry_scan_at", 0) or 0)
            if (
                force_discover
                or current - last_scan >= scan_interval
                or (not cache and last_scan <= 0)
            ):
                paths = _rollouts(home, threshold)
                claim["telemetry_scan_at"] = current
                live_paths = {str(path) for path, _ in paths}
                for key in list(cache):
                    if key not in live_paths:
                        cache.pop(key, None)
            else:
                paths = []
                for key in list(cache):
                    path = _safe_cached_path(home, key)
                    if path is None:
                        cache.pop(key, None)
                        continue
                    try:
                        paths.append((path, path.stat()))
                    except OSError:
                        cache.pop(key, None)
            for path, stat in paths:
                entry = _cached_entry(path, stat, cache)
                if entry is not None:
                    entries.append(entry)
            if entries:
                claim["tokens"] = sum(int(entry.get("tokens", 0)) for entry in entries)
                claim["cached_tokens"] = sum(
                    int(entry.get("cached_tokens", 0)) for entry in entries
                )
                claim["output_tokens"] = sum(
                    int(entry.get("output_tokens", 0)) for entry in entries
                )
                claim["rollout_count"] = len(entries)
                contexts = [
                    float(entry["context_pct"])
                    for entry in entries
                    if isinstance(entry.get("context_pct"), (int, float))
                ]
                claim["context_pct"] = max(contexts) if contexts else None
                session_entries = [entry for entry in entries if entry.get("session")]
                if session_entries:
                    root_session = min(
                        session_entries,
                        key=lambda entry: (
                            float(entry.get("birthtime", entry.get("mtime", 0))),
                            str(entry.get("path", "")),
                        ),
                    )
                    claim["session"] = root_session.get("session", {})
        else:
            cache: dict[str, Any] = {}
            preferred = (
                _safe_cached_path(home, str(seat["telemetry_file"]))
                if seat.get("telemetry_file")
                else None
            )
            paths: list[tuple[Path, Any]] = []
            preferred_stat: Any = None
            if preferred is not None:
                try:
                    preferred_stat = preferred.stat()
                except OSError:
                    preferred = None
            last_scan = float(seat.get("telemetry_scan_at", 0) or 0)
            discover = (
                force_discover
                or last_scan <= 0
                or current - last_scan >= scan_interval
            )
            if discover:
                discovered = _rollouts(home)
                seat["telemetry_scan_at"] = current
                if discovered:
                    paths = [max(discovered, key=lambda item: item[1].st_mtime_ns)]
            elif preferred is not None and preferred_stat is not None:
                # Known files are cheap to stat and are reparsed immediately
                # when changed; only the full sessions tree walk is throttled.
                paths = [(preferred, preferred_stat)]
            for path, stat in paths:
                # Reparse only when the remembered rollout changed.
                if (
                    not force_discover
                    and seat.get("telemetry_file") == str(path)
                    and seat.get("telemetry_mtime_ns") == stat.st_mtime_ns
                ):
                    continue
                entry = _cached_entry(path, stat, cache)
                if entry is not None:
                    entries.append(entry)

        if not entries:
            if restored_account:
                changed.append(seat_id)
            continue
        latest = max(entries, key=lambda entry: float(entry.get("event_ts", 0)))
        seat["session_total_tokens"] = int(latest.get("tokens", 0))
        seat["session_cached_tokens"] = int(latest.get("cached_tokens", 0))
        seat["session_output_tokens"] = int(latest.get("output_tokens", 0))
        if _apply_latest(seat, latest, current) or restored_account:
            changed.append(seat_id)
    return changed
