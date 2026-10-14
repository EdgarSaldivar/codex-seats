from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any

from .scheduler import capacity_reset_identity
from .seat_registry import (
    BUILTIN_CAPACITY_PROFILES,
    capacity_fingerprint,
    effective_relative_capacity,
)
from .state import parse_iso, reserve_for, seat_task_min_headroom


LEARNING_MIN_SAMPLES = 8
_WINDOW_ALIASES = {
    "five_hour": "5h",
    "five-hour": "5h",
    "seven_day": "weekly",
    "seven-day": "weekly",
}


def configured_capacity(
    settings: dict[str, Any] | None, seat_id: str | None
) -> tuple[str | None, float, str | None]:
    """Return the registered profile, effective multiplier, and fingerprint."""
    if not settings or not seat_id:
        return None, 1.0, None
    entry = settings.get("pool", {}).get("seats", {}).get(seat_id)
    if not isinstance(entry, dict):
        return None, 1.0, None
    return (
        str(entry["capacity_profile"]),
        effective_relative_capacity(entry),
        capacity_fingerprint(entry),
    )


def available_baseline_units(remaining_pct: float, multiplier: float) -> float:
    """Convert provider-native remaining percent to comparable 1x units."""
    return float(remaining_pct) * float(multiplier)


def usable_baseline_units(
    remaining_pct: float, reserve_pct: float, multiplier: float
) -> float:
    """Convert native percent after reserve to non-negative comparable units."""
    return max(0.0, float(remaining_pct) - float(reserve_pct)) * float(multiplier)


def _stable_window_name(name: object) -> str:
    value = str(name)
    return _WINDOW_ALIASES.get(value, value)


def _window_pct(window: dict[str, Any] | None) -> float | None:
    if not window:
        return None
    for key in ("pct", "remaining_percentage", "pct_left"):
        value = window.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    used = window.get("used_percentage", window.get("used_pct"))
    if isinstance(used, (int, float)) and not isinstance(used, bool):
        return 100.0 - float(used)
    return None


def _window_minutes(window: dict[str, Any]) -> float | None:
    value = window.get("window_minutes")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) > 0:
        return float(value)
    return None


def _window_plan_type(limits_or_seat: dict[str, Any], name: str) -> str | None:
    nested = limits_or_seat.get("last_probe_limits")
    limits = nested if isinstance(nested, dict) else limits_or_seat
    for raw_name, raw_window in limits.items():
        if _stable_window_name(raw_name) != name or not isinstance(raw_window, dict):
            continue
        value = raw_window.get("plan_type")
        if isinstance(value, str) and value.strip():
            return value.strip()[:128]
    return None


def active_capacity_windows(
    limits_or_seat: dict[str, Any], *, current: float
) -> list[tuple[str, float, float | None, float | None]]:
    """Return every reported, unexpired quota window in canonical order."""
    nested = limits_or_seat.get("last_probe_limits")
    limits = nested if isinstance(nested, dict) else limits_or_seat
    active: list[tuple[str, float, float | None, float | None]] = []
    for raw_name, raw_window in limits.items():
        if not isinstance(raw_window, dict):
            continue
        reset = raw_window.get("resets_at")
        reset_at = (
            float(reset)
            if isinstance(reset, (int, float)) and not isinstance(reset, bool)
            else None
        )
        if reset_at is not None and reset_at <= current:
            continue
        pct = _window_pct(raw_window)
        if pct is None:
            continue
        active.append(
            (_stable_window_name(raw_name), pct, reset_at, _window_minutes(raw_window))
        )
    return sorted(active, key=lambda item: item[0])


def capacity_window_signature(
    limits_or_seat: dict[str, Any], *, current: float
) -> str | None:
    """Identify the current quota shape without tying learning to reset dates."""
    windows = active_capacity_windows(limits_or_seat, current=current)
    if not windows:
        return None
    shape = [
        {"name": name, **({"minutes": minutes} if minutes is not None else {})}
        for name, _pct, _reset, minutes in windows
    ]
    return "windows:" + json.dumps(shape, sort_keys=True, separators=(",", ":"))


def capacity_window_burn(
    starting: dict[str, Any], ending: dict[str, Any], *, current: float
) -> float | None:
    """Return conservative burn when the same uncontaminated windows remain."""
    start = {
        name: pct
        for name, pct, _reset, _minutes in active_capacity_windows(starting, current=current)
    }
    end = {
        name: pct
        for name, pct, _reset, _minutes in active_capacity_windows(ending, current=current)
    }
    if not start or start.keys() != end.keys():
        return None
    burns = [start[name] - end[name] for name in start]
    if any(value < 0 for value in burns):
        return None
    return max(burns)


def _window_margin(
    base_margin: float,
    *,
    current: float,
    reset_at: float | None,
    window_minutes: float | None,
    taper_max_hours: float,
    taper_fraction: float,
) -> float:
    """Linearly release only the safety margin near a known reset."""
    if reset_at is None or window_minutes is None:
        return base_margin
    taper_seconds = min(taper_max_hours * 3600.0, window_minutes * 60.0 * taper_fraction)
    if taper_seconds <= 0:
        return base_margin
    remaining = max(0.0, reset_at - current)
    if remaining >= taper_seconds:
        return base_margin
    return base_margin * (remaining / taper_seconds)


def _learning_bucket(
    learning: dict[str, Any],
    *,
    signature: str,
    model: str,
    size: str,
    effort: str,
    capacity_identity: str | None,
    min_samples: int,
) -> tuple[float | None, dict[str, Any] | None]:
    # Deliberately consult only the current signature. Old rolling/monthly
    # cadence buckets remain in state as inert historical evidence.
    bucket = learning.get(
        learning_key(
            "codex",
            signature,
            model,
            size,
            effort,
            capacity_fingerprint=capacity_identity,
        )
    )
    if not isinstance(bucket, dict):
        return None, None
    samples = [
        float(value)
        for value in bucket.get("samples", [])
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    ]
    if len(samples) < min_samples:
        return None, bucket
    return _percentile90(samples), bucket


def _observation_time(seat: dict[str, Any], *, current: float) -> float | None:
    observation = seat.get("active_window_observation")
    observed: float | None = None
    if isinstance(observation, dict):
        raw_observed = observation.get("observed_at")
        if isinstance(raw_observed, (int, float)) and not isinstance(raw_observed, bool):
            observed = float(raw_observed)
        else:
            observed = parse_iso(observation.get("updated_at"))
    if observed is None:
        observed = parse_iso(seat.get("usage_updated_at") or seat.get("probed_at"))

    # Partial telemetry can update the seat-level observation while retaining
    # another credible future window from an older observation. Admission must
    # age the oldest active constituent, not make every retained window look as
    # fresh as the newest partial event.
    limits = seat.get("last_probe_limits")
    tracked = seat.get("usage_reset_windows")
    if not isinstance(limits, dict) or not isinstance(tracked, dict):
        return observed
    constituent_times: list[float] = []
    for raw_name, raw_window in limits.items():
        if not isinstance(raw_window, dict):
            continue
        reset = raw_window.get("resets_at")
        if (
            isinstance(reset, (int, float))
            and not isinstance(reset, bool)
            and float(reset) <= current
        ):
            continue
        tracked_window = tracked.get(raw_name)
        if not isinstance(tracked_window, dict):
            tracked_window = tracked.get(_stable_window_name(raw_name))
        tracked_at = (
            tracked_window.get("observed_at")
            if isinstance(tracked_window, dict)
            else None
        )
        if isinstance(tracked_at, (int, float)) and not isinstance(tracked_at, bool):
            constituent_times.append(float(tracked_at))
        elif observed is not None:
            constituent_times.append(observed)
    return min(constituent_times) if constituent_times else observed


def _capacity_tier(
    seat: dict[str, Any],
    *,
    current: float,
    stale_seconds: float,
    degraded_grace_seconds: float,
) -> tuple[str, str, float | None]:
    observed = _observation_time(seat, current=current)
    if observed is None or observed > current + 60:
        return "unknown", "active-window observation is missing or invalid", observed
    age = max(0.0, current - observed)
    if age > stale_seconds + degraded_grace_seconds:
        return (
            "unknown",
            f"last-good active-window observation is too old ({age / 3600:g}h)",
            observed,
        )

    account = seat.get("account_window_observation")
    impaired = False
    impairment = ""
    if isinstance(account, dict) and account.get("status") in {"failed", "partial"}:
        attempt = account.get("last_attempt_observed_at")
        attempt_at = (
            float(attempt)
            if isinstance(attempt, (int, float)) and not isinstance(attempt, bool)
            else parse_iso(account.get("last_attempt_at"))
        )
        if attempt_at is None or attempt_at >= observed:
            impaired = True
            impairment = f"latest account sweep is {account.get('status')}"
    if age <= stale_seconds and not impaired:
        return "fresh", f"last-good observation age {age / 3600:g}h", observed
    why = impairment or f"last-good observation exceeded the fresh limit ({age / 3600:g}h)"
    return "degraded", why, observed


def _reported_windows_known(seat: dict[str, Any], current: float) -> bool:
    """Reject unreadable reported windows before the legacy normalizer skips them."""
    windows = seat.get("last_probe_limits")
    if not isinstance(windows, dict) or not windows:
        return False
    try:
        for window in windows.values():
            if not isinstance(window, dict):
                return False
            reset = window.get("resets_at")
            if reset is not None:
                if (
                    not isinstance(reset, (int, float)) or isinstance(reset, bool)
                    or not math.isfinite(float(reset))
                ):
                    return False
                if reset <= current:
                    continue
            pct = _window_pct(window)
            if pct is None or not math.isfinite(pct) or not 0 < pct <= 100:
                return False
    except (OverflowError, TypeError, ValueError):
        return False
    return True


def capacity_task_fits(
    seat: dict[str, Any],
    size: str,
    *,
    current: float,
    stale_seconds: float,
    settings: dict[str, Any] | None = None,
    learning: dict[str, Any] | None = None,
    model: str = "gpt-6-sol",
    effort: str = "default",
    seat_id: str | None = None,
    details: dict[str, Any] | None = None,
    spend_local_reserve: bool = False,
    require_fresh_known: bool = False,
) -> tuple[bool, str]:
    """Fit a task against every authoritative active quota window on a seat.

    Legacy reserve-spend inputs are accepted for dispatch compatibility, but
    drain-first routing applies no capacity reserve. Burn, margin, risk, and
    task-floor requirements remain intact.
    """
    settings = settings or {}
    if require_fresh_known and not _reported_windows_known(seat, current):
        reason = "fresh known capacity requires positive balances in every reported active window"
        if details is not None:
            details.update(fits=False, tier="unknown", reason=reason)
        return False, reason
    execution = settings.get("execution", {})
    capacity = execution.get("capacity", {})
    grace = float(capacity.get("degraded_grace_seconds", 24 * 3600))
    tier, tier_reason, _observed = _capacity_tier(
        seat,
        current=current,
        stale_seconds=stale_seconds,
        degraded_grace_seconds=max(0.0, grace),
    )
    active = active_capacity_windows(seat, current=current)
    signature = capacity_window_signature(seat, current=current)
    profile, multiplier, fingerprint = configured_capacity(settings, seat_id)
    schema3_capacity = fingerprint is not None
    configured_reserve = (
        float(
            reserve_for(
                settings,
                seat_id,
                seat,
                datetime.fromtimestamp(current).astimezone(),
            )
        )
        if schema3_capacity and seat_id is not None
        else 0.0
    )
    # ``reserve_for`` returns the drain-first effective reserve (zero). Keep
    # the compatibility argument accepted without changing fit behavior.
    del spend_local_reserve
    reserve = configured_reserve
    floor = (
        seat_task_min_headroom(settings, seat_id, size)
        if schema3_capacity and seat_id is not None
        else 0.0
    )

    def finish(ok: bool, reason: str, binding: str | None = None) -> tuple[bool, str]:
        if details is not None:
            details.update(
                {
                    "fits": ok,
                    "tier": tier,
                    "signature": signature,
                    "capacity_profile": profile,
                    "capacity_multiplier": multiplier,
                    "capacity_fingerprint": fingerprint,
                    "binding_window": binding,
                    "reason": reason,
                }
            )
        return ok, reason

    if tier == "unknown":
        return finish(False, f"capacity tier unknown: {tier_reason}")
    if not active or signature is None:
        tier = "unknown"
        return finish(False, "capacity tier unknown: no active quota windows")
    if require_fresh_known and tier != "fresh":
        return finish(False, f"fresh known capacity required: {tier_reason}")
    if require_fresh_known and any(pct <= 0 for _name, pct, _reset, _minutes in active):
        return finish(False, "fresh known positive capacity required in every active window")
    if tier == "degraded" and size != "small":
        return finish(
            False,
            f"capacity tier degraded: {tier_reason}; only small work is admitted during grace",
        )

    min_samples = int(execution.get("learning_min_samples", LEARNING_MIN_SAMPLES))
    learned, bucket = _learning_bucket(
        learning or {},
        signature=signature,
        model=model,
        size=size,
        effort=effort,
        capacity_identity=fingerprint,
        min_samples=min_samples,
    )
    configured_fallbacks = capacity.get("fallback_burn_pct", {})
    fallback_burn = float(configured_fallbacks.get(size, 20.0 if size == "big" else 5.0))
    native_fallback_burn = fallback_burn / multiplier
    burn = native_fallback_burn if learned is None else learned
    base_margin = float(capacity.get("margin_pct", 5.0))
    degraded_margin = (
        float(capacity.get("degraded_extra_margin_pct", 10.0))
        if tier == "degraded"
        else 0.0
    )
    taper_max_hours = float(capacity.get("margin_taper_max_hours", 36.0))
    taper_fraction = float(capacity.get("margin_taper_window_fraction", 0.2))
    risk_floor = 0.0
    if isinstance(bucket, dict):
        configured_risk = bucket.get("risk_floor_pct")
        risk_identity = bucket.get("risk_until_reset")
        current_identity = capacity_reset_identity(
            seat.get("last_probe_limits", {}), current=current
        )
        if (
            isinstance(configured_risk, (int, float))
            and not isinstance(configured_risk, bool)
            and (risk_identity is None or risk_identity == current_identity)
        ):
            risk_floor = float(configured_risk)

    requirements: list[tuple[str, float, float, float]] = []
    for name, pct, reset_at, window_minutes in active:
        margin = degraded_margin + _window_margin(
            base_margin,
            current=current,
            reset_at=reset_at,
            window_minutes=window_minutes,
            taper_max_hours=taper_max_hours,
            taper_fraction=taper_fraction,
        )
        burn_requirement = burn + margin + reserve
        floor_requirement = floor + reserve
        required = (
            max(risk_floor, burn_requirement, floor_requirement)
            if schema3_capacity
            else max(risk_floor, burn + margin)
        )
        requirements.append((name, pct, required, margin))
    binding_name, binding_pct, binding_required, binding_margin = min(
        requirements, key=lambda item: (item[1] - item[2], item[0])
    )
    ok = all(pct >= required for _name, pct, required, _margin in requirements)
    source = "fallback burn" if learned is None else "learned p90 burn"
    observed = ", ".join(
        f"{name}={pct:g}%/{required:g}% required"
        for name, pct, required, _margin in requirements
    )
    if details is not None:
        details.update(
            {
                "predicted_burn_pct": burn,
                "predicted_native_burn_pct": burn,
                "predicted_baseline_burn_units": burn * multiplier,
                "prediction_source": source,
                "risk_floor_pct": risk_floor,
                "reserve_pct": reserve,
                "configured_reserve_pct": configured_reserve,
                "reserve_spent_for_job": False,
                "floor_pct": floor,
                "requirements": [
                    {
                        "window": name,
                        "headroom_pct": pct,
                        "required_pct": required,
                        "margin_pct": margin,
                        "available_baseline_units": available_baseline_units(
                            pct, multiplier
                        ),
                        "usable_baseline_units": usable_baseline_units(
                            pct, reserve, multiplier
                        ),
                    }
                    for name, pct, required, margin in requirements
                ],
            }
        )
    return finish(
        ok,
        f"capacity tier {tier} ({tier_reason}); {size} uses {source} {burn:g}% + margin; "
        f"active {observed}; binding window {binding_name}={binding_pct:g}% needs "
        f"{binding_required:g}% (margin {binding_margin:g}%)",
        binding_name,
    )


def seat_capacity_report(
    seat_id: str,
    seat: dict[str, Any],
    settings: dict[str, Any],
    *,
    current: float,
    learning: dict[str, Any] | None = None,
    model: str = "gpt-6-sol",
    effort: str = "default",
) -> dict[str, Any]:
    """Return the single routing/reporting view of one Codex seat's capacity.

    Busy state is intentionally absent: a busy seat still has capacity and can
    become eligible when its current task finishes.
    """
    status = str(seat.get("status") or "unknown")
    auth_ok = (
        status == "ok"
        and not bool(seat.get("auth_failure"))
        and not bool(seat.get("auth_quarantined_at"))
    )
    stale_seconds = float(settings.get("pool", {}).get("probe_stale_seconds", 21600))
    reference = datetime.fromtimestamp(current).astimezone()
    raw_pct = seat.get("pct_left")
    effective_pct = 50.0 if raw_pct is None else float(raw_pct)
    reserve = reserve_for(settings, seat_id, seat, reference)
    usable = effective_pct - reserve
    profile, multiplier, fingerprint = configured_capacity(settings, seat_id)
    entry = settings.get("pool", {}).get("seats", {}).get(seat_id, {})
    capacity_windows = [
        {
            "name": name,
            "remaining_pct": pct,
            "resets_at": reset_at,
            "window_minutes": window_minutes,
            "observed_plan_type": _window_plan_type(seat, name),
            "available_baseline_units": available_baseline_units(pct, multiplier),
            "usable_baseline_units": usable_baseline_units(
                pct, reserve, multiplier
            ),
        }
        for name, pct, reset_at, window_minutes in active_capacity_windows(
            seat, current=current
        )
    ]
    binding_window = (
        min(
            capacity_windows,
            key=lambda window: (
                window["usable_baseline_units"],
                window["name"],
            ),
        )
        if capacity_windows
        else None
    )
    result: dict[str, Any] = {
        "auth_ok": auth_ok,
        "provider_status": status,
        "reserve_pct": reserve,
        "usable_headroom_pct": usable,
        "capacity_profile": profile,
        "capacity_multiplier": multiplier,
        "capacity_fingerprint": fingerprint,
        "capacity_windows": capacity_windows,
        "binding_window": binding_window,
        "active_window_names": [window["name"] for window in capacity_windows],
    }
    if isinstance(entry, dict):
        declared = entry.get("relative_capacity")
        source = entry.get("capacity_source")
        verified_at = entry.get("capacity_verified_at")
        expected_short = BUILTIN_CAPACITY_PROFILES.get(str(profile), {}).get(
            "expects_five_hour_window"
        )
        short_reported = "5h" in result["active_window_names"]
        zero_windows = not capacity_windows
        if expected_short is True and not short_reported and not zero_windows:
            window_drift = True
            drift_reason = "Standard-style five-hour window is missing"
        elif expected_short is False and short_reported:
            window_drift = True
            drift_reason = "Premium unexpectedly reports a Standard-style five-hour window"
        else:
            window_drift = False
            drift_reason = None
        unverified_reasons = []
        if declared is None:
            unverified_reasons.append("relative capacity is undeclared")
        if source == "unverified":
            unverified_reasons.append("capacity source is unverified")
        if verified_at is None:
            unverified_reasons.append("capacity verification time is missing")
        result.update(
            {
                "declared_relative_multiplier": declared,
                "effective_relative_multiplier": multiplier,
                "capacity_source": source,
                "capacity_verified_at": verified_at,
                "unverified": bool(unverified_reasons),
                "unverified_reasons": unverified_reasons,
                "expected_five_hour_window": expected_short,
                "five_hour_window_reported": short_reported,
                "zero_authoritative_windows": zero_windows,
                "profile_window_drift": window_drift,
                "profile_window_drift_reason": drift_reason,
                "binding_available_baseline_units": (
                    binding_window["available_baseline_units"]
                    if binding_window is not None
                    else None
                ),
                "binding_usable_baseline_units": (
                    binding_window["usable_baseline_units"]
                    if binding_window is not None
                    else None
                ),
            }
        )
    for size in ("small", "big"):
        details: dict[str, Any] = {}
        capacity_fits, capacity_reason = capacity_task_fits(
            seat,
            size,
            current=current,
            stale_seconds=stale_seconds,
            settings=settings,
            learning=learning,
            model=model,
            effort=effort,
            seat_id=seat_id,
            details=details,
        )
        floor = seat_task_min_headroom(settings, seat_id, size)
        floor_fits = usable >= floor
        fits = auth_ok and capacity_fits and floor_fits
        if not auth_ok:
            reason = f"provider/auth health is {status}"
        elif not floor_fits:
            floor_reason = (
                f"usable headroom {usable:g}% is below the configured floor; "
                f"{size} tasks require {floor:g}%"
            )
            reason = (
                f"{capacity_reason}; {floor_reason}"
                if not capacity_fits
                else floor_reason
            )
        elif not capacity_fits:
            reason = capacity_reason
        else:
            reason = (
                f"{capacity_reason}; usable headroom {usable:g}% meets "
                f"{size} floor {floor:g}%"
            )
        details.update(
            {
                "fits": fits,
                "capacity_fits": capacity_fits,
                "floor_fits": floor_fits,
                "floor_pct": floor,
                "usable_headroom_pct": usable,
                "reason": reason,
            }
        )
        result[size] = details
        result[f"fits_{size}"] = fits
    result["tier"] = result["small"].get("tier", "unknown")
    result["reason"] = result["small"]["reason"]
    return result


def rolling_task_fits(*args: Any, **kwargs: Any) -> tuple[bool, str]:
    """Backward-compatible alias; admission is no longer role/cadence specific."""
    return capacity_task_fits(*args, **kwargs)


def _percentile90(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)]


def empirical_burn_ratio_report(
    settings: dict[str, Any], learning: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Compare mature, like-for-like Standard and Premium native burn buckets.

    This is deliberately advisory.  It neither edits settings nor feeds the
    admission prediction path.  A report is emitted only when both configured
    fingerprints have eight clean numeric samples for the exact same provider,
    window shape, model, size, and effort.
    """
    if not isinstance(learning, dict):
        return []
    seats = settings.get("pool", {}).get("seats", {})
    if not isinstance(seats, dict):
        return []

    configured: dict[str, dict[str, float]] = {
        "business-standard": {},
        "business-premium": {},
    }
    for entry in seats.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("lifecycle") not in {"active", "draining"}:
            continue
        profile = entry.get("capacity_profile")
        if profile not in configured:
            continue
        try:
            fingerprint = capacity_fingerprint(entry)
            configured[profile][fingerprint] = effective_relative_capacity(entry)
        except (KeyError, TypeError, ValueError):
            continue

    # key: fingerprint -> comparison dimensions -> clean samples
    buckets: dict[str, dict[tuple[str, str, str, str, str], list[float]]] = {}
    for key, bucket in learning.items():
        if not isinstance(key, str) or not isinstance(bucket, dict):
            continue
        parts = key.split("|")
        if len(parts) != 6:
            continue
        provider, window_shape, fingerprint, model, size, effort = parts
        if bucket.get("capacity_fingerprint") != fingerprint:
            continue
        samples = [
            float(value)
            for value in bucket.get("samples", [])
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) >= 0
        ]
        buckets.setdefault(fingerprint, {})[
            (provider, window_shape, model, size, effort)
        ] = samples

    reports: list[dict[str, Any]] = []
    for standard_fp, standard_multiplier in sorted(configured["business-standard"].items()):
        for premium_fp, premium_multiplier in sorted(configured["business-premium"].items()):
            standard_buckets = buckets.get(standard_fp, {})
            premium_buckets = buckets.get(premium_fp, {})
            for dimensions in sorted(standard_buckets.keys() & premium_buckets.keys()):
                standard_samples = standard_buckets[dimensions]
                premium_samples = premium_buckets[dimensions]
                if (
                    len(standard_samples) < LEARNING_MIN_SAMPLES
                    or len(premium_samples) < LEARNING_MIN_SAMPLES
                ):
                    continue
                standard_burn = _percentile90(standard_samples)
                premium_burn = _percentile90(premium_samples)
                if premium_burn <= 0 or standard_multiplier <= 0:
                    continue
                observed_ratio = standard_burn / premium_burn
                configured_ratio = premium_multiplier / standard_multiplier
                disagreement = observed_ratio / configured_ratio
                provider, window_shape, model, size, effort = dimensions
                reports.append(
                    {
                        "provider": provider,
                        "window_shape": window_shape,
                        "model": model,
                        "size": size,
                        "effort": effort,
                        "standard_fingerprint": standard_fp,
                        "premium_fingerprint": premium_fp,
                        "standard_samples": len(standard_samples),
                        "premium_samples": len(premium_samples),
                        "standard_native_burn_pct_p90": standard_burn,
                        "premium_native_burn_pct_p90": premium_burn,
                        "observed_standard_to_premium_ratio": observed_ratio,
                        "configured_standard_to_premium_ratio": configured_ratio,
                        # A mature deviation beyond 25% is intentionally a
                        # conspicuous advisory, never an admission input.
                        "material_disagreement": not 0.75 <= disagreement <= 1.25,
                    }
                )
    return reports


def learning_key(
    provider: str,
    cadence: str,
    model: str,
    size: str,
    effort: str,
    *,
    capacity_fingerprint: str | None = None,
) -> str:
    parts = [provider, cadence]
    if capacity_fingerprint is not None:
        parts.append(capacity_fingerprint)
    parts.extend((model, size, effort))
    return "|".join(parts)


def add_capacity_sample(
    learning: dict[str, Any],
    *,
    provider: str,
    cadence: str,
    model: str,
    size: str,
    effort: str,
    burn_pct: float,
    capacity_fingerprint: str | None = None,
    seat_id: str | None = None,
    reset_contaminated: bool = False,
    clean: bool = True,
) -> bool:
    key = learning_key(
        provider,
        cadence,
        model,
        size,
        effort,
        capacity_fingerprint=capacity_fingerprint,
    )
    bucket = learning.setdefault(key, {"samples": [], "rejected_reset": 0})
    if capacity_fingerprint is not None:
        bucket["capacity_fingerprint"] = capacity_fingerprint
    if seat_id is not None:
        bucket["seat_id"] = seat_id
    if reset_contaminated:
        bucket["rejected_reset"] = int(bucket.get("rejected_reset", 0)) + 1
        return False
    if not clean or burn_pct < 0:
        return False
    samples = bucket.setdefault("samples", [])
    samples.append(float(burn_pct))
    del samples[:-100]
    bucket["count"] = len(samples)
    if len(samples) >= LEARNING_MIN_SAMPLES:
        bucket["predicted_burn_pct"] = min(100.0, _percentile90(samples))
    return True


def raise_risk_floor(
    learning: dict[str, Any],
    key: str,
    floor: float,
    reset_identity: str | None,
    *,
    capacity_fingerprint: str | None = None,
    seat_id: str | None = None,
) -> None:
    bucket = learning.setdefault(key, {"samples": []})
    if capacity_fingerprint is not None:
        bucket["capacity_fingerprint"] = capacity_fingerprint
    if seat_id is not None:
        bucket["seat_id"] = seat_id
    bucket["risk_floor_pct"] = max(float(bucket.get("risk_floor_pct", 0)), float(floor))
    bucket["risk_until_reset"] = reset_identity
