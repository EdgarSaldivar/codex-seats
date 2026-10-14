"""Provider routing: pick which provider runs a task, and dispatch it.

Routing is separate from ``provider_claude`` so it is a neutral concern rather
than something the Claude "hands" module happens to own. This
makes ``provider_claude`` and ``provider_codex`` symmetric execution engines
and gives a single place that will grow the budget-aware head/hands selection.

All dependencies are explicit imports (no wildcard copy): low-level helpers from
``workers``/``state``, provider entry points from ``provider_claude`` /
``provider_codex``. ``workers`` depends on nothing here, so the workers↔router
cycle is gone. Import ``choose_provider``/``dispatch_run`` from ``router``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import local_seats
from .state import (
    Ledger,
    effective_claude_mode,
    load_claude_rate_limits,
    load_settings,
    refresh_transients,
)
from .development_policy import DevelopmentPolicyError, resolve_worker_access
from .workers import RunResult, normalize_prompt
from .provider_claude import (
    claude_model_available,
    claude_model_candidates,
    claude_task_fits,
    run_claude,
)
from .provider_codex import run_codex
from .provider_local import run_local
from .seat_registry import active_seat_ids, effective_relative_capacity
from .task_policy import (
    AUTHORING_KINDS,
    TaskProfile,
    claude_provider_eligible,
    resolve_task_policy,
)


def _codex_request(request: dict[str, Any], policy: TaskProfile) -> dict[str, Any]:
    """Return a Codex-bound request with a runner-owned model selection."""
    selected = request.get("model")
    if not isinstance(selected, str) or selected.startswith("claude"):
        selected = policy.codex_model
    bound = dict(request)
    bound["provider"] = "codex"
    bound["model"] = selected
    return bound


def _claude_request(request: dict[str, Any]) -> dict[str, Any]:
    """Return a Claude-bound request without leaking a Codex model id."""
    bound = dict(request)
    bound["provider"] = "claude"
    selected = bound.get("model")
    if isinstance(selected, str) and not (
        selected.startswith("claude")
        or selected.lower() in {"fable", "opus", "sonnet", "frontier", "complex", "routine"}
    ):
        bound.pop("model", None)
    return bound


def _local_request(
    request: dict[str, Any], policy: TaskProfile, seat_id: str, review_cycle: int
) -> dict[str, Any]:
    """Bind one controller-selected local seat and stable routing telemetry."""
    bound = dict(request)
    bound.update(
        provider="local",
        seat=seat_id,
        model=None,
        task_shape=policy.task_shape,
        validation=policy.validation,
        review_cycle=review_cycle,
    )
    bound.pop("model", None)
    return bound


def _local_attempt_state(
    root: Path, request: dict[str, Any]
) -> tuple[int, bool]:
    """Return prior local author cycles and whether this exact attempt was preempted."""
    task_id = request.get("task_id")
    attempt_id = request.get("attempt_id") or request.get("request_id")
    if not isinstance(task_id, str) and not isinstance(attempt_id, str):
        return 0, False
    with Ledger(root) as data:
        jobs = data.get("jobs", [])
        local_jobs = [
            job
            for job in jobs
            if isinstance(job, dict)
            and job.get("provider") == "local"
        ]
    cycles = (
        sum(
            job.get("status") == "completed" and job.get("task_id") == task_id
            for job in local_jobs
        )
        if isinstance(task_id, str)
        else 0
    )
    preempted = any(
        isinstance(attempt_id, str)
        and job.get("attempt_id") == attempt_id
        and job.get("status") == "preempted"
        for job in local_jobs
    )
    return cycles, preempted


def _local_auto_policy(policy: TaskProfile) -> bool:
    """Conservative auto-routing gate; explicit local routing remains broader."""
    return (
        policy.kind
        in {"implementation", "mechanical", "debug", "test", "docs", "research"}
        and policy.capability != "frontier"
        and policy.task_shape != "design"
        and policy.validation == "strong"
        and not policy.risk_flags
    )


def _select_local_seat(
    root: Path,
    settings: dict[str, Any],
    request: dict[str, Any],
    policy: TaskProfile,
    *,
    automatic: bool,
    excluded_seats: set[str] | None = None,
) -> tuple[str | None, int, str]:
    """Probe each host once and choose a ready local seat without queueing."""
    provider = settings.get("providers", {}).get("local", {})
    global_mode = provider.get("mode", "off") if isinstance(provider, dict) else "off"
    if global_mode in {"off", "shadow"}:
        return None, 0, f"local_{global_mode}"
    if automatic and not _local_auto_policy(policy):
        return None, 0, "task_policy_ineligible"
    if not automatic and policy.kind in {"review", "audit", "security"}:
        return None, 0, "local_author_cannot_fill_review_or_security_role"

    cycles, preempted = _local_attempt_state(root, request)
    if preempted:
        return None, cycles, "attempt_preempted_by_host"
    repair_budget = provider.get("repair_budget", 2)
    if automatic and cycles >= repair_budget:
        return None, cycles, "repair_budget_exhausted"

    availability_by_host: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []
    excluded = excluded_seats or set()
    for seat_id in sorted(settings.get("local_seats", {})):
        if seat_id in excluded:
            continue
        seat = settings["local_seats"][seat_id]
        mode = local_seats.effective_mode(settings, seat)
        if mode in {"off", "shadow"}:
            continue
        if automatic and mode == "canary" and (
            policy.size != "small" or policy.capability != "routine"
        ):
            reasons.append(f"{seat_id}:canary_scope")
            continue
        host_id = seat.get("host")
        if host_id not in availability_by_host:
            availability_by_host[host_id] = local_seats.observe_host(
                root, settings, host_id
            )
        decision = local_seats.eligibility(
            settings,
            seat_id,
            availability_by_host[host_id],
            task_shape=policy.task_shape,
            validation=policy.validation,
        )
        if decision["eligible"]:
            return seat_id, cycles, "ready"
        reasons.append(f"{seat_id}:{decision['reason']}")
    return None, cycles, ";".join(reasons) or "no_local_seats"


def _try_local_auto(
    root: Path,
    settings: dict[str, Any],
    request: dict[str, Any],
    policy: TaskProfile,
    *,
    access_decision: Any,
    fallback_count: int,
) -> RunResult | None:
    attempted: set[str] = set()
    while len(attempted) < len(settings.get("local_seats", {})):
        seat_id, review_cycle, _reason = _select_local_seat(
            root,
            settings,
            request,
            policy,
            automatic=True,
            excluded_seats=attempted,
        )
        if seat_id is None:
            return None
        bound = _local_request(request, policy, seat_id, review_cycle)
        bound.update(
            route_provider="local",
            route_reason="router:phase0-local-ready",
            _fallback_count=fallback_count,
        )
        result = run_local(root, settings, bound, access_decision=access_decision)
        # Only a failure proven to precede child launch may try another local
        # candidate. Once Qwen starts, the worktree may be dirty and the result
        # is terminal for this attempt.
        if not result.fallback_safe:
            return result
        attempted.add(seat_id)
        fallback_count += 1
    return None


def _non_one_x_seats(settings: dict[str, Any]) -> set[str]:
    entries = settings.get("pool", {}).get("seats", {})
    return {
        seat_id
        for seat_id in active_seat_ids(settings)
        if abs(effective_relative_capacity(entries[seat_id]) - 1.0) >= 1e-9
    }


def choose_provider(
    requested: str,
    kind: str,
    settings: dict[str, Any],
    data: dict[str, Any],
    *,
    size: str = "small",
    claude_limits: dict[str, Any] | None = None,
    allow_reserve: bool = False,
    model: str | None = None,
    small_canary: bool = False,
) -> tuple[str, str | None]:
    policy = resolve_task_policy(settings, kind, size=size)
    if requested == "codex":
        return "codex", None
    mode, reason = effective_claude_mode(settings)
    if requested == "claude":
        if mode == "off":
            return "disabled", reason or "Claude is hard-off"
        fits, fit_reason = claude_task_fits(
            settings,
            size,
            claude_limits or {},
            allow_reserve=allow_reserve,
            require_fresh_known=not (small_canary and size == "small"),
        )
        if not fits:
            return "unavailable", fit_reason
        candidates = claude_model_candidates(
            settings, kind, size, model, policy.capability
        )
        state = data.get("providers", {}).get("claude", {})
        if not any(claude_model_available(state, candidate) for candidate in candidates):
            return "unavailable", "all task-fit Claude models are currently capped"
        return "claude", None
    if mode in {"off", "manual_only"}:
        return "codex", None
    if preferred_provider(settings, kind) == "codex":
        # Codex-preferred (abundant pool). Route to Codex; dispatch_run's
        # Codex->Claude exhaustion fallback covers a drained pool when auto-Claude
        # is permitted for this kind. This is the config-driven symmetric default.
        return "codex", None
    # Claude-preferred kind (a quality lens): use Claude when it's actually
    # available and permitted, else fall to Codex.
    state = data.get("providers", {}).get("claude", {})
    fits, _ = claude_task_fits(
        settings, size, claude_limits or {}, require_fresh_known=True
    )
    candidates = claude_model_candidates(
        settings, kind, size, model, policy.capability
    )
    model_available = any(claude_model_available(state, candidate) for candidate in candidates)
    if (
        claude_provider_eligible(
            policy,
            mode,
            capacity_available=fits,
            provider_available=(
                model_available
                and state.get("status") == "ok"
                and not state.get("in_flight")
            ),
        )
    ):
        return "claude", None
    return "codex", None


def _dispatch_auto_authoring(
    root: Path,
    settings: dict[str, Any],
    request: dict[str, Any],
    policy: TaskProfile,
    *,
    access_decision: Any,
    claude_limits: dict[str, Any] | None,
) -> RunResult:
    """Run the authoring state machine for a typed interactive-head request.

    1. Spend eligible Codex 1x rolling seats.
    2. Prefer the family opposite the current head.
    3. Use the remaining same-family pool.
    4. As a last resort, run an isolated worker on the active head identity.

    Each provider call is no-queue. The durable inbox alone owns retry when every
    real capacity source is exhausted.
    """
    kind, size = policy.kind, policy.size
    origin_head = str(request.get("_origin_head") or "")
    base_excluded = set(request.get("_excluded_seats", []))
    fallback_count = int(request.get("_fallback_count", 0) or 0)

    local_result = _try_local_auto(
        root,
        settings,
        request,
        policy,
        access_decision=access_decision,
        fallback_count=fallback_count,
    )
    if local_result is not None:
        return local_result

    one_x = _codex_request(request, policy)
    one_x["no_queue"] = True
    one_x["route_provider"] = "codex"
    one_x["route_reason"] = "router:phase1-codex-1x"
    one_x["_excluded_seats"] = sorted(base_excluded | _non_one_x_seats(settings))
    result = run_codex(root, settings, one_x, access_decision=access_decision)
    if not _codex_pool_exhausted(result):
        return result
    fallback_count += 1

    def try_claude(reason: str) -> RunResult | None:
        if not _auto_claude_permitted(settings, kind):
            return None
        assert claude_limits is not None
        with Ledger(root) as data:
            refresh_transients(data, root, settings)
            choice, _ = choose_provider(
                "claude",
                kind,
                settings,
                data,
                size=size,
                claude_limits=claude_limits,
                allow_reserve=bool(request.get("allow_reserve")),
                model=None,
            )
        if choice != "claude":
            return None
        bound = _claude_request(request)
        bound.update(
            {
                "route_provider": "claude",
                "route_reason": reason,
                "_capacity_checked": True,
                "_suppress_notify": True,
                "_fallback_count": fallback_count,
            }
        )
        return run_claude(root, settings, bound, access_decision=access_decision)

    if origin_head == "codex":
        opposing = try_claude("router:phase2-opposite-head-family")
        if opposing is not None and not opposing.fallback_safe:
            return opposing
        if opposing is not None:
            fallback_count += 1

    codex_pool = _codex_request(request, policy)
    codex_pool.update(
        {
            "no_queue": True,
            "route_provider": "codex",
            "route_reason": "router:phase2-opposite-head-family"
            if origin_head == "claude"
            else "router:phase3-codex-pool",
            "_fallback_count": fallback_count,
        }
    )
    result = run_codex(root, settings, codex_pool, access_decision=access_decision)
    if not _codex_pool_exhausted(result):
        return result
    fallback_count += 1

    if origin_head == "claude":
        head_worker = try_claude("router:phase3-claude-head-worker")
        if head_worker is not None:
            return head_worker

    if (
        origin_head == "codex"
        and settings.get("execution", {}).get("head_worker_fallback", True)
        and isinstance(request.get("_origin_seat"), str)
    ):
        # This is a fresh managed worker call in a disposable worktree, not inline
        # authoring in the interactive head. Forced-seat admission still enforces the
        # seat's real capacity, reserve, health, and authentication checks.
        head_worker = _codex_request(request, policy)
        head_worker.update(
            {
                "seat": request["_origin_seat"],
                "_excluded_seats": [],
                "no_queue": True,
                "route_provider": "codex",
                "route_reason": "router:phase4-codex-head-worker",
                "_fallback_count": fallback_count,
            }
        )
        return run_codex(root, settings, head_worker, access_decision=access_decision)

    return result


def dispatch_run(
    root: Path,
    settings: dict[str, Any],
    request: dict[str, Any],
    *,
    controller_snapshot: bool = False,
) -> RunResult:
    request = dict(request)
    # One controller snapshot owns routing and access for the entire job.  A
    # fallback receives the same immutable decision instead of re-reading a
    # setting between provider attempts.
    if not controller_snapshot and (root / "settings.json").exists():
        settings, warnings = load_settings(root)
        access_warnings = [warning for warning in warnings if "worker_access" in warning]
        if access_warnings:
            prompt, label = normalize_prompt(request["prompt"], request.get("label"))
            del prompt
            return RunResult(
                2, "blocked_needs_controller", "", None, label,
                message="invalid worker access setting: " + "; ".join(access_warnings),
            )
    try:
        access_decision = resolve_worker_access(settings, request)
    except DevelopmentPolicyError as exc:
        prompt, label = normalize_prompt(request["prompt"], request.get("label"))
        del prompt
        return RunResult(
            2, "blocked_needs_controller", "", None, label,
            message=f"invalid worker access setting: {exc}",
        )
    requested = request.get("provider", "auto")
    kind = request.get("kind", "implementation")
    policy = resolve_task_policy(
        settings,
        kind,
        size=request.get("size"),
        capability=request.get("capability_tier"),
        task_shape=request.get("task_shape"),
        validation=request.get("validation"),
        effort=request.get("effort"),
        estimated_minutes=request.get("estimated_minutes"),
        parallel_safe=bool(request.get("parallel_safe", False)),
        scope_hints=request.get("scope_hints", []),
        risk_flags=request.get("risk_flags", []),
        override_reason=request.get("override_reason"),
    )
    size = policy.size
    request["size"] = size
    request["capability_tier"] = policy.capability
    request["estimated_minutes"] = policy.estimated_minutes
    if requested == "local":
        attempted: set[str] = set()
        last_result: RunResult | None = None
        reason = "no_local_seats"
        while len(attempted) < len(settings.get("local_seats", {})):
            seat_id, review_cycle, reason = _select_local_seat(
                root,
                settings,
                request,
                policy,
                automatic=False,
                excluded_seats=attempted,
            )
            if seat_id is None:
                if last_result is not None:
                    return last_result
                prompt, label = normalize_prompt(
                    request["prompt"], request.get("label")
                )
                del prompt
                if reason == "attempt_preempted_by_host":
                    return RunResult(
                        1,
                        "failed",
                        "local",
                        None,
                        label,
                        message=(
                            "explicit local attempt was preempted by the host; "
                            "refusing to re-run the same attempt"
                        ),
                        fallback_safe=False,
                    )
                return RunResult(
                    3,
                    "unavailable",
                    "local",
                    None,
                    label,
                    message=f"no eligible local seat: {reason}",
                    fallback_safe=True,
                )
            bound = _local_request(request, policy, seat_id, review_cycle)
            bound["route_provider"] = "local"
            bound["route_reason"] = "router:explicit-local"
            result = run_local(
                root, settings, bound, access_decision=access_decision
            )
            if not result.fallback_safe:
                return result
            last_result = result
            attempted.add(seat_id)
        if last_result is not None:
            return last_result
        prompt, label = normalize_prompt(request["prompt"], request.get("label"))
        del prompt
        return RunResult(
            3,
            "unavailable",
            "local",
            None,
            label,
            message=f"no eligible local seat: {reason}",
            fallback_safe=True,
        )
    mode, mode_reason = effective_claude_mode(settings)
    if (
        requested == "auto"
        and kind in AUTHORING_KINDS
        and request.get("_origin_head") in {"codex", "claude"}
    ):
        limits = None if mode == "off" else load_claude_rate_limits(root)
        return _dispatch_auto_authoring(
            root,
            settings,
            request,
            policy,
            access_decision=access_decision,
            claude_limits=limits,
        )
    # Hard-off remains fail-closed: do not even read the passive Claude cache.
    if mode == "off":
        if requested in {"auto", "codex"}:
            codex_request = _codex_request(request, policy)
            codex_request["route_provider"] = "codex"
            if not codex_request.get("route_reason"):
                codex_request["route_reason"] = "router:claude-hard-off"
            return run_codex(
                root, settings, codex_request, access_decision=access_decision
            )
        prompt, label = normalize_prompt(request["prompt"], request.get("label"))
        del prompt
        return RunResult(
            4, "disabled", "claude", "claude", label,
            message=mode_reason or "Managed Claude is hard-off",
        )
    limits = load_claude_rate_limits(root)
    with Ledger(root) as data:
        refresh_transients(data, root, settings)
        provider, reason = choose_provider(
            requested,
            kind,
            settings,
            data,
            size=size,
            claude_limits=limits,
            allow_reserve=bool(request.get("allow_reserve")),
            model=request.get("model"),
            small_canary=bool(request.get("small_canary")),
        )
    # Persist the routing decision so the job ledger records it even for
    # direct dispatches. Fallback branches below copy this request, so a
    # spilled job still shows the ORIGINAL decision (route_provider) while its
    # `provider` + `fallback_count` fields show where it actually landed.
    request["route_provider"] = provider
    if not request.get("route_reason"):  # never clobber a caller-supplied value
        request["route_reason"] = reason or f"router:{requested}->{provider}"
    if provider == "disabled":
        prompt, label = normalize_prompt(request["prompt"], request.get("label"))
        del prompt
        return RunResult(4, "disabled", "claude", "claude", label, message=reason)
    if provider == "unavailable":
        prompt, label = normalize_prompt(request["prompt"], request.get("label"))
        del prompt
        return RunResult(3, "reserved", "claude", "claude", label, message=reason)
    if provider == "claude":
        claude_request = _claude_request(request)
        claude_request["_capacity_checked"] = True
        claude_request["_suppress_notify"] = requested == "auto"
        result = run_claude(
            root, settings, claude_request, access_decision=access_decision
        )
        if requested == "auto" and result.fallback_safe:
            codex_request = _codex_request(request, policy)
            codex_request["_fallback_count"] = 1
            codex_result = run_codex(
                root, settings, codex_request, access_decision=access_decision
            )
            if codex_result.message is None:
                codex_result.message = (
                    f"Claude capacity unavailable after {result.model or 'task-fit model'}; "
                    "safely fell back to Codex"
                )
            return codex_result
        return result

    # provider == "codex". For an
    # AUTO request we make the fallback symmetric with the Claude->Codex branch
    # above: try codex without self-queuing, and if the pool is exhausted fall to
    # Claude — but only when auto-Claude is actually permitted for this mode+kind
    # (never in manual_only / off), so the fallback can't smuggle Claude past the
    # operator's policy. Provider capacity is returned to the caller; only the
    # durable dispatch inbox owns retry state.
    if requested != "auto" or not _auto_claude_permitted(settings, kind):
        return run_codex(
            root,
            settings,
            _codex_request(request, policy),
            access_decision=access_decision,
        )
    probe_request = _codex_request(request, policy)
    probe_request["no_queue"] = True
    result = run_codex(root, settings, probe_request, access_decision=access_decision)
    if not _codex_pool_exhausted(result):
        return result
    with Ledger(root) as data:
        refresh_transients(data, root, settings)
        claude_choice, _ = choose_provider(
            "claude", kind, settings, data, size=size, claude_limits=limits,
            allow_reserve=bool(request.get("allow_reserve")), model=None,
        )
    if claude_choice == "claude":
        claude_request = _claude_request(request)
        claude_request["_capacity_checked"] = True
        claude_request["_suppress_notify"] = True
        claude_request["_fallback_count"] = int(request.get("_fallback_count", 0) or 0) + 1
        claude_result = run_claude(
            root, settings, claude_request, access_decision=access_decision
        )
        if claude_result.message is None:
            claude_result.message = "Codex pool exhausted; safely fell back to Claude"
        return claude_result
    # Neither provider is available. Return the original Codex capacity result;
    # the durable dispatch runner decides whether and when to retry it.
    return result


def _codex_pool_exhausted(result: RunResult) -> bool:
    """A codex run that found no schedulable seat. Status is unavailable and no seat
    was claimed (``worker``
    is the seat field on RunResult)."""
    return result.status in {"queued", "unavailable"} and str(result.worker) in {"-", "None"}


def preferred_provider(settings: dict[str, Any], kind: str) -> str:
    """Which provider ``auto`` prefers for ``kind`` — config-driven (settings
    ``execution.prefer_provider_by_kind``). Unknown kinds default to ``codex`` (the
    abundant pool). The non-preferred provider is still reachable via the router's
    bidirectional exhaustion fallback, so this is a preference, not a hard route."""
    return resolve_task_policy(settings, kind).preferred_provider


def _auto_claude_permitted(settings: dict[str, Any], kind: str) -> bool:
    """Would an ``auto`` request be allowed to use Claude for this kind, given the
    operator's mode? Mirrors the auto gate in ``choose_provider`` so the exhaustion
    fallback never overrides manual_only/off or a review_only mode for a non-review kind."""
    mode, _ = effective_claude_mode(settings)
    if mode in {"off", "manual_only"}:
        return False
    return claude_provider_eligible(
        resolve_task_policy(settings, kind),
        mode,
        capacity_available=True,
        provider_available=True,
    )
