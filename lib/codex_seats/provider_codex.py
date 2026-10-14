from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from . import dispatch_protocol
from .bounded_process import ProcessCancelledError
from .capacity import (
    add_capacity_sample,
    capacity_task_fits,
    capacity_window_burn,
    capacity_window_signature,
    configured_capacity,
    learning_key,
    raise_risk_floor,
)
from .efforts import clamp_effort
from .managed import read_structured_result, with_release_metadata, write_result_schema
from .provider_claude import effort_for_kind
from .scheduler import capacity_reset_identity
from .scope import worker_permission_profile, worker_profile_overrides
from .seat_selection import pick_seat
from .state import (
    Ledger,
    default_task_size,
    iso,
    load_settings,
    now,
    refresh_transients,
    runtime_seat_home,
    seat_task_min_headroom,
    usable_headroom,
)
from .seat_registry import registered_seat_ids
from .telemetry import refresh_passive_usage, trim_jobs_with_archive
from .worker_config import (
    _sync_config_file,
    _worker_isolation_overrides,
    sync_codex_config,
    sync_codex_capability_roots,
    trusted_worker_overrides,
    validate_trusted_codex_config,
    worker_mcp_overrides,
)
from . import workers  # module-qualified for the test-mocked primitives below
from .workers import (
    AUTH_ERR,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    PERMISSION_ERR,
    RATE_ERR,
    RunResult,
    _context_bytes,
    _maybe_notify,
    codex_cli_error_text,
    managed_prompt,
    managed_worker_env,
    normalize_prompt,
    parse_codex_tokens,
    probe_is_stale,
    probe_seats,
    quarantine_auth,
)
from .development_policy import (
    DevelopmentPolicyError,
    WORKER_ACCESS_ENV_KEY,
    WorkerAccessDecision,
    resolve_worker_access,
)
from .write_boundary import boundary_plan, configured_seat_home_paths
from .worker_launch import create_worker_launch_directory


def _forced_registry_error(settings: dict[str, Any], seat_id: str) -> str | None:
    entries = settings.get("pool", {}).get("seats", {})
    if seat_id not in set(registered_seat_ids(settings)):
        return f"forced seat unavailable: {seat_id} is not registered"
    lifecycle = entries[seat_id]["lifecycle"]
    if lifecycle != "active":
        return (
            f"forced seat unavailable: {seat_id} is {lifecycle}; "
            "new claims require an active seat"
        )
    return None


def _reserve_spend_seat(request: dict[str, Any]) -> str | None:
    """Revalidate the runner's narrow internal reserve-spend translation."""
    authority = request.get("_reserve_spend_authority")
    if "_reserve_spend_authority" not in request:
        return None
    if (
        not isinstance(authority, dict)
        or set(authority) != {"version", "seat", "source"}
        or type(authority.get("version")) is not int
        or authority.get("version")
        != dispatch_protocol.RESERVE_SPEND_AUTHORITY_VERSION
        or not isinstance(authority.get("seat"), str)
        or not dispatch_protocol.SEAT_RE.fullmatch(authority["seat"])
        or authority.get("source")
        != dispatch_protocol.RESERVE_SPEND_AUTHORITY_SOURCE
    ):
        return ""
    return authority["seat"]

def run_codex(
    root: Path,
    settings: dict[str, Any],
    request: dict[str, Any],
    *,
    access_decision: WorkerAccessDecision | None = None,
) -> RunResult:
    prompt_arg = request["prompt"]
    prompt, label = normalize_prompt(prompt_arg, request.get("label"))
    if request.get("sandbox") == "danger-full-access":
        return RunResult(2, "failed", "codex", None, label, message="managed workers prohibit full-access mode")
    try:
        access = access_decision or resolve_worker_access(settings, request)
    except DevelopmentPolicyError as exc:
        return RunResult(
            2, "blocked_needs_controller", "codex", None, label,
            message=f"invalid worker access setting: {exc}",
        )
    logs = root / "logs"
    kind = request.get("kind", "implementation")
    forced = request.get("seat")
    if isinstance(forced, int):
        forced = f"seat{forced}"
    reserve_spend_seat = _reserve_spend_seat(request)
    if reserve_spend_seat == "" or (
        reserve_spend_seat is not None and reserve_spend_seat != forced
    ):
        return RunResult(
            2,
            "failed",
            "codex",
            forced,
            label,
            message="invalid internal reserve-spend authority",
        )
    if reserve_spend_seat is not None and request.get("allow_reserve"):
        return RunResult(
            2,
            "failed",
            "codex",
            forced,
            label,
            message="reserve-spend authority cannot be combined with allow_reserve",
        )
    size = request.get("size") or default_task_size(settings, kind)
    # "ultra" (Sol Ultra subagent mode) 400s at the API unless the account is
    # enrolled — gate it behind providers.codex.supports_ultra, clamping to max.
    effort, effort_downgraded_from = clamp_effort(
        request.get("effort") or effort_for_kind(settings, kind),
        supports_ultra=bool(
            settings.get("providers", {}).get("codex", {}).get("supports_ultra", False)
        ),
    )
    selected_model = request.get("model") or DEFAULT_MODEL
    fit_details: dict[str, Any] = {}
    with Ledger(root) as data:
        claim_hook = request.get("_claim_hook")
        if callable(claim_hook):
            claim_hook("after_provider_ledger_lock_before_settings_reload")
        # Atomically reload the authoritative topology without taking the
        # settings lock; a disappeared file yields the empty registry.
        settings, _warnings = load_settings(root)
        if callable(claim_hook):
            claim_hook("after_provider_reload_before_claim_publication")
        refresh_transients(data, root, settings)
        refresh_passive_usage(data, root, settings)
        if forced:
            seat = forced
            if seat in set(request.get("_excluded_seats", [])):
                return RunResult(
                    3,
                    "unavailable",
                    "codex",
                    seat,
                    label,
                    message="forced seat is excluded by the active-head identity",
                )
            registry_error = _forced_registry_error(settings, seat)
            if registry_error is not None:
                return RunResult(
                    3,
                    "unavailable",
                    "codex",
                    seat,
                    label,
                    message=registry_error,
                )
            required_headroom = seat_task_min_headroom(settings, seat, size)
            current = data["seats"].get(seat)
            if not current or current.get("status") != "ok" or current.get("in_flight"):
                return RunResult(3, "unavailable", "codex", seat, label, message="forced seat unavailable")
            if size == "big" and current.get("pct_left") is None and not request.get("allow_reserve"):
                return RunResult(
                    3,
                    "reserved",
                    "codex",
                    seat,
                    label,
                    message="seat capacity is unknown; big tasks require a known balance",
                )
            headroom = usable_headroom(settings, seat, current)
            if headroom < required_headroom and not request.get("allow_reserve"):
                headroom_reason = (
                    f"{seat} has {headroom:g}% usable headroom; {size} tasks require "
                    f"{required_headroom:g}%."
                )
                return RunResult(
                    3,
                    "reserved",
                    "codex",
                    seat,
                    label,
                    message=headroom_reason,
                )
            if not request.get("allow_reserve"):
                fits, fit_reason = capacity_task_fits(
                    current,
                    size,
                    current=now(),
                    stale_seconds=float(
                        settings.get("pool", {}).get(
                            "probe_stale_seconds", 6 * 3600
                        )
                    ),
                    settings=settings,
                    learning=data.get("capacity_learning", {}),
                    model=str(selected_model),
                    effort=effort or "default",
                    seat_id=seat,
                    details=fit_details,
                )
                if not fits:
                    return RunResult(
                        3,
                        "reserved",
                        "codex",
                        seat,
                        label,
                        message=fit_reason,
                    )
            else:
                profile, multiplier, fingerprint = configured_capacity(settings, seat)
                fit_details.update(
                    {
                        "fits": True,
                        "tier": "bypassed",
                        "signature": capacity_window_signature(current, current=now()),
                        "capacity_profile": profile,
                        "capacity_multiplier": multiplier,
                        "capacity_fingerprint": fingerprint,
                        "binding_window": None,
                        "reason": "explicit --allow-reserve bypass",
                    }
                )
        else:
            seat = pick_seat(
                data["seats"],
                settings,
                size,
                set(request.get("_excluded_seats", [])),
                learning=data.get("capacity_learning", {}),
                model=str(selected_model),
                effort=effort or "default",
            )
        if not seat:
            return RunResult(
                3,
                "unavailable",
                "codex",
                "-",
                label,
                message="no schedulable seat; caller retained ownership",
            )
        required_headroom = seat_task_min_headroom(settings, seat, size)
        if not fit_details:
            capacity_task_fits(
                data["seats"][seat],
                size,
                current=now(),
                stale_seconds=float(
                    settings.get("pool", {}).get("probe_stale_seconds", 6 * 3600)
                ),
                settings=settings,
                learning=data.get("capacity_learning", {}),
                model=str(selected_model),
                effort=effort or "default",
                seat_id=seat,
                details=fit_details,
            )
        job_id = f"{int(now())}-{seat}"
        data["seats"][seat]["in_flight"] = {
            "job_id": job_id,
            "dispatcher_pid": os.getpid(),
            "child_pid": None,
            "kind": kind,
            "size": size,
            "effort": effort,
            "label": label,
            "started_at": iso(),
            "started_ts": now(),
            "starting_pct": data["seats"][seat].get("pct_left"),
            "starting_limits": json.loads(json.dumps(data["seats"][seat].get("last_probe_limits", {}))),
            "capacity_fit_tier": fit_details.get("tier"),
            "capacity_signature": fit_details.get("signature"),
            "capacity_profile": fit_details.get("capacity_profile"),
            "capacity_multiplier": fit_details.get("capacity_multiplier", 1.0),
            "capacity_fingerprint": fit_details.get("capacity_fingerprint"),
            "predicted_native_burn_pct": fit_details.get("predicted_native_burn_pct"),
            "predicted_baseline_burn_units": fit_details.get(
                "predicted_baseline_burn_units"
            ),
            "capacity_fit_reason": fit_details.get("reason"),
            "capacity_binding_window": fit_details.get("binding_window"),
        }
        probed_at = data["seats"][seat].get("probed_at")
        stale = probe_is_stale(
            probed_at,
            int(settings["pool"].get("probe_stale_seconds", 6 * 3600)),
        )

    retry_request: dict[str, Any] | None = None
    if stale and not request.get("no_probe") and not forced:
        probe_seats(root, settings, [seat])
        with Ledger(root) as data:
            current = data["seats"][seat]
            revalidated_details: dict[str, Any] = {}
            capacity_fits, _ = capacity_task_fits(
                current,
                size,
                current=now(),
                stale_seconds=float(
                    settings.get("pool", {}).get("probe_stale_seconds", 6 * 3600)
                ),
                settings=settings,
                learning=data.get("capacity_learning", {}),
                model=str(selected_model),
                effort=effort or "default",
                seat_id=seat,
                details=revalidated_details,
            )
            if (
                usable_headroom(settings, seat, current) < required_headroom
                or not capacity_fits
            ):
                current["in_flight"] = None
                retry_request = dict(request)
                retry_request["_excluded_seats"] = [*request.get("_excluded_seats", []), seat]
            elif isinstance(current.get("in_flight"), dict):
                current["in_flight"].update(
                    {
                        "starting_pct": current.get("pct_left"),
                        "starting_limits": json.loads(
                            json.dumps(current.get("last_probe_limits", {}))
                        ),
                        "capacity_fit_tier": revalidated_details.get("tier"),
                        "capacity_signature": revalidated_details.get("signature"),
                        "capacity_profile": revalidated_details.get("capacity_profile"),
                        "capacity_multiplier": revalidated_details.get(
                            "capacity_multiplier", 1.0
                        ),
                        "capacity_fingerprint": revalidated_details.get(
                            "capacity_fingerprint"
                        ),
                        "predicted_native_burn_pct": revalidated_details.get(
                            "predicted_native_burn_pct"
                        ),
                        "predicted_baseline_burn_units": revalidated_details.get(
                            "predicted_baseline_burn_units"
                        ),
                        "capacity_fit_reason": revalidated_details.get("reason"),
                        "capacity_binding_window": revalidated_details.get("binding_window"),
                    }
                )
    if retry_request is not None:
        # The recursive selection must happen after the ledger context releases its flock.
        return run_codex(
            root, settings, retry_request, access_decision=access
        )

    output = logs / f"{job_id}.jsonl"
    structured: dict[str, Any] | None = None
    private_temp: Path | None = None
    boundary = None
    launch_directory: Path | None = None
    capability_warnings: list[str] = []
    declared_scopes = list(request.get("write_scope", []))
    external_scopes = list(request.get("external_write_scope", []))
    if request.get("sandbox") == "read-only":
        declared_scopes = []
        external_scopes = []
    elif not declared_scopes:
        # A writable run with no declared scope defaults to the whole repo tree.
        # Empty scope otherwise maps to read-only (a footgun that silently blocks
        # every write); a lone worker has no peer to clobber, and secret/.git/control
        # denials in the permission profile are independent of this scope.
        declared_scopes = [{"path": ".", "type": "tree"}]
    repository = Path(request.get("cd") or os.getcwd()).expanduser().resolve()
    try:
        boundary = boundary_plan(
            repository,
            declared_scopes,
            external_scopes,
            strict_scope=request.get("strict_scope") is True,
            parallel_safe=request.get("parallel_safe") is True,
            allow_git_write=request.get("allow_git_write") is True,
        )
        sync_codex_config(root, settings, seat)
        if access.trusted:
            validate_trusted_codex_config(runtime_seat_home(root, seat, settings))
            capability_warnings = sync_codex_capability_roots(root, settings, seat)
        binary = workers.resolve_executable("codex", request.get("command"))
        scopes = list(boundary.effective_scope)
        temp_parent = root / "private-tmp"
        temp_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(temp_parent, 0o700)
        private_temp = Path(tempfile.mkdtemp(prefix=f"{job_id}-", dir=temp_parent))
        os.chmod(private_temp, 0o700)
        launch_directory = create_worker_launch_directory(root, job_id)
        profile_name = "managed-" + re.sub(r"[^A-Za-z0-9_-]", "-", job_id)
        profile_text = ""
        if not access.trusted:
            profile_text = worker_permission_profile(
                profile_name,
                repository,
                scopes,
                external_scopes,
                private_temp,
                protected_paths=request.get("protected_paths", []),
                generic_temp=tempfile.gettempdir(),
                allow_git_write=boundary.allow_git_write,
                git_metadata=boundary.git_metadata,
                control_root=root,
                settings=settings,
            )
            profile_path = launch_directory / "permissions.config.toml"
            _sync_config_file(profile_path, profile_path, profile_text.encode())
        command = [
            binary,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--config",
            'approval_policy="never"',
        ]
        if not access.trusted:
            command.insert(2, "--strict-config")
        home = runtime_seat_home(root, seat, settings)
        for override in [
            *(
                ['default_permissions=":danger-full-access"', *trusted_worker_overrides()]
                if access.trusted
                else [
                    *worker_profile_overrides(profile_text, profile_name),
                    *_worker_isolation_overrides(home),
                ]
            ),
            # Grant the controller-declared MCP servers AFTER the blanket disable so
            # their enable + approve-mode wins (a new server isn't in the seat config,
            # so isolation never disabled it; this makes it usable headlessly).
            *worker_mcp_overrides(request.get("mcp", []), repository),
        ]:
            command += ["--config", override]
        schema_path = launch_directory / "result-schema.json"
        result_path = private_temp / "result.json"
        write_result_schema(schema_path)
        command += ["--output-schema", str(schema_path), "--output-last-message", str(result_path)]
        if selected_model and selected_model != "native":
            command += ["-m", selected_model]
        if effort:
            command += ["-c", f"model_reasoning_effort={effort}"]
        if request.get("cd"):
            command += ["--cd", str(repository)]
        command.append(managed_prompt(prompt))
        # Trusted workers use ordinary OS temporary storage; restricted workers
        # keep their private controller-owned scratch as TMPDIR.
        job_temp = (
            Path(tempfile.gettempdir()) if access.trusted else private_temp
        )
        env = managed_worker_env(
            CODEX_HOME=str(home),
            CODEX_SEATS_MANAGED_WORKER="1",
            **{WORKER_ACCESS_ENV_KEY: access.effective},
            CODEX_SEATS_WRITE_SCOPE=json.dumps(scopes),
            CODEX_SEATS_DECLARED_WRITE_SCOPE=json.dumps(declared_scopes),
            CODEX_SEATS_TASK_ROOT=str(repository),
            CODEX_SEATS_CONTROL_ROOT=str(root.resolve()),
            CODEX_SEATS_PROTECTED_HOMES=json.dumps(
                [str(path) for path in configured_seat_home_paths(root, settings)]
            ),
            CODEX_SEATS_PRIVATE_TEMP=str(private_temp),
            CODEX_SEATS_EXTERNAL_WRITE_SCOPE=json.dumps(external_scopes),
            CODEX_SEATS_ALLOW_GIT_WRITE="1" if boundary.allow_git_write else "0",
            CODEX_SEATS_GIT_METADATA_PATHS=json.dumps(
                [str(path) for path in boundary.git_metadata]
            ),
            CODEX_SEATS_PROTECTED_PATHS=json.dumps(request.get("protected_paths", [])),
            CODEX_SEATS_RELEASE_ID=str(request.get("release_id") or ""),
            CODEX_SEATS_DISPATCH_PROTOCOL=str(
                request.get("protocol_version") or ""
            ),
            TMPDIR=str(job_temp),
            TMP=str(job_temp),
            TEMP=str(job_temp),
        )
        with workers.PowerLease(settings, request.get("allow_battery", False)):
            status, text, _, duration = workers._execute(
                command,
                output,
                int(request.get("timeout", DEFAULT_TIMEOUT)),
                None,
                env,
                root,
                seat,
                "codex",
            )
        tokens = parse_codex_tokens(output, text)
        structured = read_structured_result(result_path)
        if structured and structured["status"] != "completed":
            status = structured["status"]
        elif status != "completed" and PERMISSION_ERR.search(codex_cli_error_text(output, text) or text):
            status = "blocked_needs_controller"
            structured = {
                "status": status,
                "summary": "Essential operation was denied by the managed worker boundary.",
                "changed_files": [], "test_evidence": [],
                "warnings": ["Independent safe alternatives were exhausted."],
                "needs_controller": "Review the denied operation and perform it outside the managed worker if appropriate.",
            }
    except ProcessCancelledError as exc:
        status, text, duration, tokens = "cancelled", str(exc), 0, 0
    except (FileNotFoundError, RuntimeError, OSError, ValueError) as exc:
        status, text, duration, tokens = "failed", str(exc), 0, 0
    finally:
        if private_temp is not None:
            shutil.rmtree(private_temp, ignore_errors=True)
        if launch_directory is not None:
            shutil.rmtree(launch_directory, ignore_errors=True)

    structured = with_release_metadata(
        structured, request.get("release_id"), request.get("protocol_version")
    )
    if isinstance(structured, dict):
        structured["worker_access"] = access.effective
        if capability_warnings:
            structured["warnings"] = [
                *structured.get("warnings", []), *capability_warnings
            ]
    with Ledger(root) as data:
        current = data["seats"][seat]
        refresh_passive_usage(
            data,
            root,
            settings,
            [seat],
            force_discover=True,
        )
        claim = current.get("in_flight")
        if isinstance(claim, dict):
            # The rollout tree includes native subagents that share this seat;
            # stdout's turn.completed usage only accounts for the root rollout.
            tokens = max(tokens, int(claim.get("tokens", 0) or 0))
            cached_tokens = int(claim.get("cached_tokens", 0) or 0)
            output_tokens = int(claim.get("output_tokens", 0) or 0)
            reasoning_tokens = int(claim.get("reasoning_tokens", 0) or 0)
            starting_pct = claim.get("starting_pct")
            starting_limits = claim.get("starting_limits", {})
            capacity_fit_tier = claim.get("capacity_fit_tier")
            starting_signature = claim.get("capacity_signature")
            claim_capacity_profile = claim.get("capacity_profile")
            claim_capacity_multiplier = claim.get("capacity_multiplier", 1.0)
            claim_capacity_fingerprint = claim.get("capacity_fingerprint")
            predicted_native_burn_pct = claim.get("predicted_native_burn_pct")
            predicted_baseline_burn_units = claim.get(
                "predicted_baseline_burn_units"
            )
            capacity_fit_reason = claim.get("capacity_fit_reason")
            capacity_binding_window = claim.get("capacity_binding_window")
        else:
            cached_tokens = output_tokens = reasoning_tokens = 0
            starting_pct = None
            starting_limits = {}
            capacity_fit_tier = None
            starting_signature = None
            claim_capacity_profile = None
            claim_capacity_multiplier = 1.0
            claim_capacity_fingerprint = None
            predicted_native_burn_pct = None
            predicted_baseline_burn_units = None
            capacity_fit_reason = None
            capacity_binding_window = None
        provider_tokens = max(0, int(tokens), max(0, int(output_tokens)))
        output_tokens = max(0, int(output_tokens))
        input_tokens = provider_tokens - output_tokens
        cached_tokens = min(input_tokens, max(0, int(cached_tokens)))
        uncached_input_tokens = input_tokens - cached_tokens
        reasoning_tokens = max(0, int(reasoning_tokens))
        current["in_flight"] = None
        cli_error = ""
        if status != "completed":
            cli_error = codex_cli_error_text(output, text)
            if AUTH_ERR.search(cli_error):
                quarantine_auth(current, cli_error)
            elif RATE_ERR.search(cli_error):
                current["status"] = "capped"
                current["status_until"] = now() + int(settings["pool"].get("cap_cooldown_seconds", 18000))
        current["spent_since_probe"] = current.get("spent_since_probe", 0) + tokens
        current["last_job_at"] = iso()
        end_limits = current.get("last_probe_limits", {})
        learning_at = now()
        end_signature = capacity_window_signature(end_limits, current=learning_at)
        start_identity = capacity_reset_identity(starting_limits, current=learning_at)
        end_identity = capacity_reset_identity(end_limits, current=learning_at)
        signatures_match = (
            isinstance(starting_signature, str)
            and starting_signature == end_signature
        )
        reset_contaminated = not signatures_match or start_identity != end_identity
        burn_pct = capacity_window_burn(
            starting_limits, end_limits, current=learning_at
        )
        risk_signature = (
            starting_signature
            if claim_capacity_fingerprint is not None
            else end_signature
        )
        risk_reset_identity = (
            start_identity
            if claim_capacity_fingerprint is not None
            else end_identity
        )
        if (
            status == "completed"
            and isinstance(end_signature, str)
            and burn_pct is not None
        ):
            add_capacity_sample(
                data.setdefault("capacity_learning", {}),
                provider="codex",
                cadence=end_signature,
                model=str(selected_model),
                size=size,
                effort=effort or "default",
                burn_pct=burn_pct,
                capacity_fingerprint=claim_capacity_fingerprint,
                seat_id=seat if claim_capacity_fingerprint is not None else None,
                reset_contaminated=reset_contaminated,
                clean=(
                    signatures_match
                    and start_identity is not None
                    and start_identity == end_identity
                ),
            )
        elif RATE_ERR.search(cli_error) and isinstance(risk_signature, str):
            sample_key = learning_key(
                "codex",
                risk_signature,
                str(selected_model),
                size,
                effort or "default",
                capacity_fingerprint=claim_capacity_fingerprint,
            )
            raise_risk_floor(
                data.setdefault("capacity_learning", {}),
                sample_key,
                required_headroom + 10,
                risk_reset_identity,
                capacity_fingerprint=claim_capacity_fingerprint,
                seat_id=seat if claim_capacity_fingerprint is not None else None,
            )
        data["jobs"].append(
            {
                "telemetry_schema": "codex-seat.job.v5.3",
                "telemetry_classification": "current",
                "release_id": request.get("release_id"),
                "protocol_version": request.get("protocol_version"),
                "id": job_id,
                "campaign_run_id": os.environ.get("CODEX_SEATS_BATCH_ID"),
                "ts": iso(),
                "provider": "codex",
                "seat": seat,
                "label": label,
                "kind": kind,
                "size": size,
                "effort": effort,
                "effort_downgraded_from": effort_downgraded_from,
                "model": selected_model,
                "tokens": tokens,
                "raw_tokens": provider_tokens,
                "cached_tokens": cached_tokens,
                "uncached_tokens": uncached_input_tokens,
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_tokens,
                "uncached_input_tokens": uncached_input_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens,
                "provider_tokens": provider_tokens,
                "starting_capacity_pct": starting_pct,
                "ending_capacity_pct": current.get("pct_left"),
                "starting_windows": starting_limits,
                "ending_windows": json.loads(json.dumps(current.get("last_probe_limits", {}))),
                "capacity_fit_tier": capacity_fit_tier,
                "capacity_window_signature": starting_signature,
                "ending_capacity_window_signature": end_signature,
                "capacity_profile": claim_capacity_profile,
                "capacity_multiplier": claim_capacity_multiplier,
                "capacity_fingerprint": claim_capacity_fingerprint,
                "predicted_native_burn_pct": predicted_native_burn_pct,
                "predicted_baseline_burn_units": predicted_baseline_burn_units,
                "capacity_fit_reason": capacity_fit_reason,
                "capacity_binding_window": capacity_binding_window,
                "predicted_minutes": request.get("estimated_minutes"),
                "actual_minutes": duration / 60,
                "route_reason": request.get("route_reason"),
                "route_provider": request.get("route_provider"),
                "goal_id": request.get("goal_id"),
                "iteration": request.get("iteration"),
                "dynamic_requirement_pct": request.get("dynamic_requirement_pct"),
                "binding_window": request.get("binding_window"),
                "fallback_count": max(0, int(request.get("_fallback_count", 0) or 0)),
                "write_boundary_mode": boundary.mode if boundary else "unresolved",
                "strict_scope": request.get("strict_scope") is True,
                "worker_access": access.effective,
                "status": status,
                "secs": duration,
                "log": str(output),
                "context_bytes": _context_bytes(root, settings, prompt_arg, request.get("cd")),
                "structured_result": structured,
            }
        )
        trim_jobs_with_archive(data, root)
    _maybe_notify(settings, label, status, seat, duration)
    return RunResult(
        0 if status == "completed" else (5 if status == "blocked_needs_controller" else 1),
        status,
        "codex",
        seat,
        label,
        duration,
        tokens,
        str(output),
        text[-500:],
        False,
        selected_model,
        structured,
        effort,
        effort_downgraded_from,
    )
