"""Typed provenance and recovery evidence for provider-wide Claude quota cooldowns.

Pure helpers: no clock reads and no I/O. Callers pass the current time and the
freshness policy explicitly, together with evidence extracted by
``quota_evidence_from_snapshot`` from the raw persisted usage snapshot. That
extraction never synthesizes per-window observation times from the snapshot's
global ``updated_at`` — a window only counts as observed when the persisted
entry itself says when it was observed — so a reset epoch taken from this
evidence is provider data, never guessed from human error text or vouched for
by an unrelated global timestamp.
"""
from __future__ import annotations

import math
import re
from typing import Any

QUOTA_KIND = "provider-quota"
PROVIDER_WINDOWS = ("five_hour", "seven_day")
# Brief grace after a provider-stated reset so the bound never lands exactly on
# the boundary the provider is still enforcing.
RESET_GRACE_SECONDS = 60
# A window's reset must fall inside its own rolling cycle (plus slack); a
# five-hour reset a day out or a weekly reset a month out is corrupt data, not
# evidence.
WINDOW_HORIZON_SECONDS = {
    "five_hour": 6 * 3600,
    "seven_day": 8 * 86400,
}

_FIVE_HOUR_RE = re.compile(r"session\W{0,20}limit|\b5\W?hour|\bfive\W?hour", re.I)
_SEVEN_DAY_RE = re.compile(r"week(?:ly)?\W{0,20}limit|\b7\W?day|\bseven\W?day", re.I)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def classify_quota_window(error: str | None) -> str | None:
    """Identify the provider-wide window a quota error names, or None."""
    if not error:
        return None
    five_hour = bool(_FIVE_HOUR_RE.search(error))
    seven_day = bool(_SEVEN_DAY_RE.search(error))
    if five_hour == seven_day:
        return None
    return "five_hour" if five_hour else "seven_day"


def quota_evidence_from_snapshot(payload: Any, at: float) -> dict[str, Any]:
    """Extract strictly validated per-window quota evidence from a raw snapshot.

    Nothing is synthesized or coerced. A window is kept only when its persisted
    entry supplies a finite non-boolean numeric used percentage in 0..100 and
    its own finite non-boolean ``observed_at`` no later than ``at``; a global
    ``updated_at`` never stands in for a missing per-window observation. A
    supplied reset epoch must be finite, strictly in the future, and within the
    window's own horizon — an implausible reset discards the whole window as
    corrupt rather than passing partially trusted data onward.
    """
    if not isinstance(payload, dict):
        return {}
    reference = _finite(at)
    if reference is None:
        return {}
    evidence: dict[str, Any] = {}
    for name in PROVIDER_WINDOWS:
        raw = payload.get(name)
        if not isinstance(raw, dict):
            continue
        used = _finite(raw.get("used_percentage"))
        if used is None or not 0 <= used <= 100:
            continue
        observed_at = _finite(raw.get("observed_at"))
        if observed_at is None or observed_at > reference:
            continue
        resets_at: float | None = None
        if raw.get("resets_at") is not None:
            resets_at = _finite(raw.get("resets_at"))
            if resets_at is None or not (
                reference < resets_at <= reference + WINDOW_HORIZON_SECONDS[name]
            ):
                continue
        evidence[name] = {
            "used_percentage": used,
            "remaining_percentage": 100 - used,
            "resets_at": resets_at,
            "observed_at": observed_at,
        }
    return evidence


def quota_cooldown_provenance(
    error: str | None,
    evidence: Any,
    created_at: float,
    *,
    usage_stale_seconds: float,
) -> dict[str, Any]:
    """Record when and why a provider-wide quota cooldown was created.

    The affected window's reset epoch is copied only from strict snapshot
    evidence — never parsed out of the human error text — and only when the
    backing observation was genuinely per-window, no later than creation, and
    still fresh under ``usage_stale_seconds`` at creation. A stale or global
    observation cannot make a recorded reset trustworthy.
    """
    window = classify_quota_window(error)
    resets_at: float | None = None
    observed_at: float | None = None
    stale_after = _finite(usage_stale_seconds)
    if window is not None and isinstance(evidence, dict) and stale_after is not None:
        entry = evidence.get(window)
        if isinstance(entry, dict):
            observed = _finite(entry.get("observed_at"))
            candidate = _finite(entry.get("resets_at"))
            if (
                observed is not None
                and observed <= created_at
                and created_at - observed <= stale_after
                and candidate is not None
                and created_at
                < candidate
                <= created_at + WINDOW_HORIZON_SECONDS[window]
            ):
                resets_at = candidate
                observed_at = observed
    return {
        "kind": QUOTA_KIND,
        "created_at": float(created_at),
        "window": window,
        "window_resets_at": resets_at,
        "window_observed_at": observed_at,
    }


def bounded_cooldown_until(provenance: Any, fallback_until: float) -> float:
    """Bound a new quota cooldown by trustworthy window reset evidence.

    Reset evidence can only shorten the wait: the result is never later than
    the existing finite fallback, so weekly-window data cannot impose a
    multi-day local cooldown.
    """
    fallback = float(fallback_until)
    if isinstance(provenance, dict) and provenance.get("kind") == QUOTA_KIND:
        horizon = WINDOW_HORIZON_SECONDS.get(provenance.get("window"))
        created_at = _finite(provenance.get("created_at"))
        resets_at = _finite(provenance.get("window_resets_at"))
        if (
            horizon is not None
            and created_at is not None
            and resets_at is not None
            and created_at < resets_at <= created_at + horizon
        ):
            return min(resets_at + RESET_GRACE_SECONDS, fallback)
    return fallback


def quota_cooldown_recovered(
    provenance: Any,
    evidence: Any,
    *,
    at: float,
    usage_stale_seconds: float,
) -> bool:
    """Return whether fresh usage evidence proves a typed quota cooldown reset.

    Conservative by construction. The provenance itself must be trustworthy:
    a typed provider-wide window, a creation time no later than now, a recorded
    reset inside that window's horizon, and a backing observation that was
    per-window and fresh when the cooldown was created. Every provider window
    must then show positive headroom from its own explicit observation — no
    later than now, within ``usage_stale_seconds``, and strictly after the
    failure — with any supplied reset finite, future, and plausibly inside
    that window's horizon. Finally the affected window must show either a new
    reset cycle or an observation strictly after the recorded reset. A global
    timestamp, a partial refresh, or a stale sibling window can never vouch
    for recovery.
    """
    reference = _finite(at)
    stale_after = _finite(usage_stale_seconds)
    if reference is None or stale_after is None or stale_after <= 0:
        return False
    if not isinstance(provenance, dict) or provenance.get("kind") != QUOTA_KIND:
        return False
    window = provenance.get("window")
    if window not in PROVIDER_WINDOWS:
        return False
    created_at = _finite(provenance.get("created_at"))
    recorded_reset = _finite(provenance.get("window_resets_at"))
    recorded_observed = _finite(provenance.get("window_observed_at"))
    if created_at is None or recorded_reset is None or recorded_observed is None:
        return False
    if created_at > reference:
        return False
    if not (
        created_at < recorded_reset <= created_at + WINDOW_HORIZON_SECONDS[window]
    ):
        return False
    if recorded_observed > created_at or created_at - recorded_observed > stale_after:
        return False
    if not isinstance(evidence, dict):
        return False
    for name in PROVIDER_WINDOWS:
        entry = evidence.get(name)
        if not isinstance(entry, dict):
            return False
        used = _finite(entry.get("used_percentage"))
        remaining = _finite(entry.get("remaining_percentage"))
        if used is None or not 0 <= used <= 100:
            return False
        if remaining is None or remaining <= 0:
            return False
        observed_at = _finite(entry.get("observed_at"))
        if observed_at is None or observed_at > reference:
            return False
        if reference - observed_at > stale_after:
            return False
        if observed_at <= created_at:
            return False
        if entry.get("resets_at") is not None:
            resets_at = _finite(entry.get("resets_at"))
            if resets_at is None or not (
                reference < resets_at <= reference + WINDOW_HORIZON_SECONDS[name]
            ):
                return False
    affected = evidence[window]
    fresh_reset = _finite(affected.get("resets_at"))
    fresh_observed = _finite(affected.get("observed_at"))
    if fresh_reset is None or fresh_observed is None:
        return False
    return fresh_reset > recorded_reset or fresh_observed > recorded_reset
