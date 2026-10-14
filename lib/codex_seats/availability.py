"""Pure schema-3 worker availability and concurrency accounting.

The provider claim paths remain authoritative.  This module only computes the
number of admission frames the current registered identities can safely occupy;
it performs no filesystem access, process checks, provider probes, or mutation.
Callers supply already-observed authentication and live-claim facts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .capacity import capacity_task_fits
from .dispatch_admission import MAX_ACTIVE_JOBS
from .provider_claude import (
    claude_model_available,
    claude_model_candidates,
    claude_task_fits,
)
from .seat_registry import registered_seat_ids, seat_number
from .state import effective_claude_mode


SAFETY_CEILING = MAX_ACTIVE_JOBS


@dataclass(frozen=True)
class HeadOrigin:
    """The identity occupied by the head which submitted an admission frame."""

    head: str
    seat: str | None = None


class OriginError(ValueError):
    """A schema-3 task origin cannot identify a currently valid head."""


def resolve_schema3_origin(
    settings: Mapping[str, Any], origin: HeadOrigin
) -> HeadOrigin:
    """Resolve and validate one task origin against the current registry.

    Seatless Codex submissions are ordinary CLI traffic and therefore use the
    configured active primary.  Explicit identities are never repaired: stale,
    unregistered, or cross-provider origins are rejected by the coordinator.
    """
    try:
        entries = settings["pool"]["seats"]
        primary = settings["pool"].get("primary")
    except (KeyError, TypeError) as exc:
        raise OriginError("schema-3 task origin requires a valid seat registry") from exc
    if not isinstance(entries, Mapping):
        raise OriginError("schema-3 task origin requires a valid seat registry")
    if origin.head == "claude":
        if origin.seat is not None:
            raise OriginError("Claude head origin must not name a Codex seat")
        return origin
    if origin.head != "codex":
        raise OriginError("head origin is unknown")
    seat_id = origin.seat if origin.seat is not None else primary
    if not isinstance(seat_id, str) or seat_id not in entries:
        raise OriginError("Codex head origin is not a registered identity")
    entry = entries[seat_id]
    if not isinstance(entry, Mapping) or entry.get("lifecycle") != "active":
        raise OriginError("Codex head origin is not an active identity")
    return HeadOrigin("codex", seat_id)


def _origin_record(
    origin: HeadOrigin,
    *,
    registered: set[str],
    active: set[str],
    codex_slots: tuple[str, ...],
    claude_slots: int,
    allow_head_worker_fallback: bool,
) -> dict[str, Any]:
    invalid_reason: str | None = None
    excluded: str | None = None
    if origin.head == "codex":
        if origin.seat not in registered:
            invalid_reason = "Codex head origin is not a registered identity"
        elif origin.seat not in active:
            invalid_reason = "Codex head origin is not an active identity"
        else:
            excluded = origin.seat
    elif origin.head == "claude":
        if origin.seat is not None:
            invalid_reason = "Claude head origin must not name a Codex seat"
    else:
        invalid_reason = "head origin is unknown"

    eligible = tuple(seat for seat in codex_slots if seat != excluded)
    worker_count = len(eligible)
    head_fallback_slots = 0
    if allow_head_worker_fallback and invalid_reason is None:
        if origin.head == "codex" and origin.seat in codex_slots:
            head_fallback_slots = 1
        elif origin.head == "claude" and claude_slots:
            head_fallback_slots = 1
    # Ordinarily the managed Claude identity cannot simultaneously be the Claude
    # head and a worker. The explicit fallback is counted separately and used only
    # after the ordinary/opposing pools fail.
    origin_claude_slots = 0 if origin.head == "claude" else claude_slots
    pool_ceiling = (
        min(
            SAFETY_CEILING,
            worker_count + origin_claude_slots + head_fallback_slots,
        )
        if invalid_reason is None
        else 0
    )
    return {
        "head": origin.head,
        "seat": origin.seat,
        "valid": invalid_reason is None,
        "reason": invalid_reason,
        "excluded_codex_head_seat": excluded,
        "codex_worker_slots": worker_count,
        "codex_worker_identities": list(eligible),
        "claude_worker_slots": origin_claude_slots,
        "head_worker_fallback_slots": head_fallback_slots,
        "pool_ceiling": pool_ceiling,
    }


def schema3_concurrency_report(
    settings: Mapping[str, Any],
    ledger: Mapping[str, Any],
    origins: Iterable[HeadOrigin],
    *,
    current: float,
    codex_live_claims: Iterable[str] = (),
    claude_live_claim: bool = False,
    authenticated_codex: Iterable[str] | None = None,
    claude_limits: Mapping[str, Any] | None = None,
    reserve_spend_seats: Iterable[str] = (),
) -> dict[str, Any]:
    """Return deterministic, fail-closed schema-3 identity slot accounting.

    Busy identities count because their jobs already occupy admission frames.
    An idle identity has to prove current health, authentication, and usable
    authoritative capacity.  A draining identity is useful only for its already
    live claim.  Invalid input returns a zero ceiling instead of synthesizing
    capacity.
    """
    empty = {
        "valid": False,
        "safety_ceiling": SAFETY_CEILING,
        "current_pool_ceiling": 0,
        "conservative_mixed_origin_ceiling": 0,
        "default_primary_codex_head_ceiling": 0,
        "claude_head_ceiling": 0,
        "codex": {"worker_identities": [], "slots": 0, "identities": []},
        "claude": {"slots": 0, "state": "unavailable", "reason": "invalid schema-3 state"},
        "origins": [],
    }
    try:
        entries = settings["pool"]["seats"]
        runtime_seats = ledger.get("seats", {})
        providers = ledger.get("providers", {})
        if (
            not isinstance(entries, Mapping)
            or not isinstance(runtime_seats, Mapping)
            or not isinstance(providers, Mapping)
        ):
            return empty
        provider = providers.get("claude", {})
        if not isinstance(provider, Mapping):
            return empty
        registered_ids = registered_seat_ids(settings)
    except (KeyError, TypeError, ValueError):
        return empty

    registered = set(registered_ids)
    # Protocol-6 reserve-spend authority remains readable for compatibility,
    # but drain-first accounting no longer reserves capacity to spend.
    del reserve_spend_seats
    live = set(codex_live_claims) & registered
    authenticated = (
        set(authenticated_codex) & registered
        if authenticated_codex is not None
        else None
    )
    try:
        stale_seconds = float(
            settings.get("pool", {}).get("probe_stale_seconds", 6 * 3600)
        )
    except (TypeError, ValueError):
        return empty
    identity_records: list[dict[str, Any]] = []
    codex_slots: list[str] = []
    active: set[str] = set()

    for seat_id in registered_ids:
        entry = entries.get(seat_id)
        runtime = runtime_seats.get(seat_id)
        lifecycle = entry.get("lifecycle") if isinstance(entry, Mapping) else None
        if lifecycle == "active":
            active.add(seat_id)
        is_live = seat_id in live and isinstance(runtime, Mapping) and isinstance(
            runtime.get("in_flight"), Mapping
        )
        eligible = False
        state = "unavailable"
        reason = f"lifecycle is {lifecycle or 'invalid'}"
        if lifecycle in {"inactive", "retired"}:
            state = str(lifecycle)
        elif lifecycle == "draining":
            if is_live:
                eligible, state, reason = True, "busy", "live draining claim occupies a frame"
            else:
                state, reason = "draining", "draining identity has no live existing claim"
        elif lifecycle == "active" and is_live:
            eligible, state, reason = True, "busy", "live claim occupies a frame"
        elif lifecycle == "active" and isinstance(runtime, Mapping):
            auth_ok = (
                seat_id in authenticated
                if authenticated is not None
                else runtime.get("status") == "ok"
            )
            if runtime.get("status") != "ok":
                reason = "provider health/authentication is not ok"
            elif not auth_ok or runtime.get("auth_quarantined_at"):
                reason = "authentication is not usable"
            else:
                details: dict[str, Any] = {}
                try:
                    fits, fit_reason = capacity_task_fits(
                        dict(runtime),
                        "small",
                        current=current,
                        stale_seconds=stale_seconds,
                        settings=dict(settings),
                        learning=(
                            dict(ledger.get("capacity_learning", {}))
                            if isinstance(ledger.get("capacity_learning"), Mapping)
                            else {}
                        ),
                        seat_id=seat_id,
                        details=details,
                    )
                except (KeyError, TypeError, ValueError):
                    fits, fit_reason = False, "capacity state is invalid"
                eligible = bool(fits)
                state = "idle" if eligible else "unavailable"
                reason = "healthy idle identity has usable capacity" if eligible else fit_reason
        identity_records.append(
            {
                "id": seat_id,
                "lifecycle": lifecycle,
                "state": state,
                "live_claim": is_live,
                "eligible": eligible,
                "reason": reason,
            }
        )
        if eligible:
            codex_slots.append(seat_id)

    claude_claim_present = isinstance(provider.get("in_flight"), Mapping)
    claude_busy = bool(claude_live_claim and claude_claim_present)
    claude_eligible = False
    claude_state = "unavailable"
    claude_reason = "Claude is not eligible"
    if claude_busy:
        claude_eligible = True
        claude_state = "busy"
        claude_reason = "live Claude claim occupies a frame"
    else:
        mode, mode_reason = effective_claude_mode(dict(settings), at=current)
        if mode == "off":
            claude_state = "off"
            claude_reason = mode_reason or "configured mode is off"
        elif provider.get("status") != "ok":
            claude_reason = "provider health/authentication is not ok"
        else:
            limits = dict(claude_limits) if isinstance(claude_limits, Mapping) else {}
            try:
                quota_ok, quota_reason = claude_task_fits(
                    dict(settings),
                    "small",
                    limits,
                    require_fresh_known=True,
                    at=current,
                )
                models = set(
                    claude_model_candidates(dict(settings), "review", "small")
                    + claude_model_candidates(
                        dict(settings), "implementation", "small"
                    )
                )
                model_ok = any(
                    claude_model_available(dict(provider), model) for model in models
                )
            except (KeyError, TypeError, ValueError):
                quota_ok, quota_reason, model_ok = (
                    False,
                    "passive Claude capacity state is invalid",
                    False,
                )
            if not quota_ok:
                claude_reason = quota_reason or "passive capacity is not eligible"
            elif not model_ok:
                claude_reason = "all configured Claude worker models are capped"
            else:
                claude_eligible = True
                claude_state = "idle"
                claude_reason = "healthy provider has usable passive capacity"

    def origin_order(origin: HeadOrigin) -> tuple[str, int, str]:
        try:
            number = seat_number(origin.seat) if origin.seat is not None else 0
        except ValueError:
            number = 2**63 - 1
        return origin.head, number, origin.seat or ""

    canonical_origins = tuple(sorted(set(origins), key=origin_order))
    allow_head_worker_fallback = bool(
        settings.get("execution", {}).get("head_worker_fallback", True)
    )
    origin_records = [
        _origin_record(
            origin,
            registered=registered,
            active=active,
            codex_slots=tuple(codex_slots),
            claude_slots=int(claude_eligible),
            allow_head_worker_fallback=allow_head_worker_fallback,
        )
        for origin in canonical_origins
    ]
    conservative_ceiling = min(
        (record["pool_ceiling"] for record in origin_records), default=0
    )
    primary = settings.get("pool", {}).get("primary")
    primary_record = _origin_record(
        HeadOrigin("codex", primary if isinstance(primary, str) else None),
        registered=registered,
        active=active,
        codex_slots=tuple(codex_slots),
        claude_slots=int(claude_eligible),
        allow_head_worker_fallback=allow_head_worker_fallback,
    )
    claude_head_record = _origin_record(
        HeadOrigin("claude"),
        registered=registered,
        active=active,
        codex_slots=tuple(codex_slots),
        claude_slots=int(claude_eligible),
        allow_head_worker_fallback=allow_head_worker_fallback,
    )
    # The configured primary is the default/current Codex head.  An empty pool
    # has no Codex default, so its current ceiling is the Claude-head view.
    current_ceiling = (
        primary_record["pool_ceiling"]
        if isinstance(primary, str)
        else claude_head_record["pool_ceiling"]
    )
    return {
        "valid": True,
        "safety_ceiling": SAFETY_CEILING,
        "current_pool_ceiling": current_ceiling,
        "conservative_mixed_origin_ceiling": conservative_ceiling,
        "default_primary_codex_head_ceiling": primary_record["pool_ceiling"],
        "claude_head_ceiling": claude_head_record["pool_ceiling"],
        "default_primary_codex_head": primary_record,
        "claude_head": claude_head_record,
        "codex": {
            "worker_identities": list(codex_slots),
            "slots": len(codex_slots),
            "identities": identity_records,
        },
        "claude": {
            "slots": int(claude_eligible),
            "state": claude_state,
            "live_claim": claude_busy,
            "reason": claude_reason,
        },
        "origins": origin_records,
    }
