"""Pure, shared schema-3 operator reporting.

This module converts already-read registry, ledger, claim, and passive-provider
facts into one stable health/capacity summary.  It performs no filesystem or
provider access, so every human and JSON surface can use the same definitions.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .availability import HeadOrigin, schema3_concurrency_report
from .capacity import empirical_burn_ratio_report, seat_capacity_report
from .seat_registry import registered_seat_ids


HEALTHY_DEFINITION = (
    "active lifecycle, provider/auth status ok, and the shared capacity policy "
    "admits small work from at least one current authoritative quota window"
)


def schema3_operator_report(
    settings: Mapping[str, Any],
    ledger: Mapping[str, Any],
    *,
    current: float,
    claude_limits: Mapping[str, Any] | None = None,
    live_codex_claims: Iterable[str] | None = None,
    claude_live_claim: bool | None = None,
) -> dict[str, Any]:
    """Return lifecycle, health, worker-slot, ceiling, and advisory truth."""
    entries = settings.get("pool", {}).get("seats", {})
    runtime_seats = ledger.get("seats", {})
    if not isinstance(entries, Mapping) or not isinstance(runtime_seats, Mapping):
        return {}

    ids = registered_seat_ids(settings)
    lifecycle = {
        state: sum(
            isinstance(entries.get(seat_id), Mapping)
            and entries[seat_id].get("lifecycle") == state
            for seat_id in ids
        )
        for state in ("active", "draining", "inactive", "retired")
    }
    active_ids = [
        seat_id
        for seat_id in ids
        if isinstance(entries.get(seat_id), Mapping)
        and entries[seat_id].get("lifecycle") == "active"
    ]
    seat_capacity: dict[str, dict[str, Any]] = {}
    for seat_id in ids:
        runtime = runtime_seats.get(seat_id, {})
        if not isinstance(runtime, Mapping):
            runtime = {}
        seat_capacity[seat_id] = seat_capacity_report(
            seat_id,
            dict(runtime),
            dict(settings),
            current=current,
            learning=(
                dict(ledger.get("capacity_learning", {}))
                if isinstance(ledger.get("capacity_learning"), Mapping)
                else {}
            ),
        )
    healthy_ids = [
        seat_id
        for seat_id in active_ids
        if seat_capacity[seat_id]["auth_ok"]
        and seat_capacity[seat_id]["fits_small"]
        and not seat_capacity[seat_id].get("zero_authoritative_windows", True)
    ]
    live_codex = (
        list(live_codex_claims)
        if live_codex_claims is not None
        else [
            seat_id
            for seat_id in ids
            if isinstance(runtime_seats.get(seat_id), Mapping)
            and isinstance(runtime_seats[seat_id].get("in_flight"), Mapping)
        ]
    )
    provider = ledger.get("providers", {}).get("claude", {})
    if not isinstance(provider, Mapping):
        provider = {}
    origins = [HeadOrigin("codex", seat_id) for seat_id in active_ids]
    origins.append(HeadOrigin("claude"))
    concurrency = schema3_concurrency_report(
        settings,
        ledger,
        origins,
        current=current,
        codex_live_claims=live_codex,
        claude_live_claim=(
            isinstance(provider.get("in_flight"), Mapping)
            if claude_live_claim is None
            else claude_live_claim
        ),
        authenticated_codex=[
            seat_id for seat_id in ids if seat_capacity[seat_id]["auth_ok"]
        ],
        claude_limits=claude_limits,
    )
    current_head = next(
        (
            seat_id
            for seat_id in active_ids
            if seat_id in set(live_codex)
            if isinstance(runtime_seats.get(seat_id), Mapping)
            and isinstance(runtime_seats[seat_id].get("in_flight"), Mapping)
            and runtime_seats[seat_id]["in_flight"].get("kind") == "control"
        ),
        settings.get("pool", {}).get("primary"),
    )
    current_origin = next(
        (
            origin
            for origin in concurrency.get("origins", [])
            if origin.get("head") == "codex" and origin.get("seat") == current_head
        ),
        concurrency.get("default_primary_codex_head", {}),
    )
    ratios = empirical_burn_ratio_report(
        dict(settings),
        dict(ledger.get("capacity_learning", {}))
        if isinstance(ledger.get("capacity_learning"), Mapping)
        else {},
    )
    return {
        "healthy": len(healthy_ids),
        "active": lifecycle["active"],
        "healthy_seat_ids": healthy_ids,
        "healthy_definition": HEALTHY_DEFINITION,
        "lifecycle": lifecycle,
        "registered_total": len(ids),
        "global_safety_ceiling": concurrency["safety_ceiling"],
        "current_codex_head": current_head,
        "current_codex_head_ceiling": current_origin.get("pool_ceiling", 0),
        "default_primary_codex_head_ceiling": concurrency[
            "default_primary_codex_head_ceiling"
        ],
        "claude_head_ceiling": concurrency["claude_head_ceiling"],
        "conservative_mixed_origin_ceiling": concurrency[
            "conservative_mixed_origin_ceiling"
        ],
        "effective_worker_identities": {
            "codex": list(concurrency["codex"]["worker_identities"]),
            "claude": concurrency["claude"]["slots"],
        },
        "effective_worker_slots": {
            "codex": concurrency["codex"]["slots"],
            "claude": concurrency["claude"]["slots"],
        },
        "head_capacity": concurrency,
        "empirical_standard_premium_ratio_advisories": ratios,
        "seat_capacity": seat_capacity,
    }
