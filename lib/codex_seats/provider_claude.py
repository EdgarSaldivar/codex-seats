from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .bounded_process import ProcessCancelledError
from .capacity import add_capacity_sample, learning_key, raise_risk_floor
from .claude_isolation import (
    DENIED_TOOLS,
    scrubbed_claude_env,
    structured_claude_result,
    write_isolation_files,
)
from .efforts import clamp_effort
from .managed import with_release_metadata
from .scheduler import capacity_reset_identity
from .telemetry import trim_jobs_with_archive
from .state import (
    Ledger,
    claude_usage_fresh,
    default_task_size,
    effective_claude_mode,
    iso,
    load_claude_quota_evidence,
    load_claude_rate_limits,
    now,
    refresh_transients,
)
from . import workers  # module-qualified for the test-mocked primitives below
from .workers import (
    AUTH_ERR,
    DEFAULT_TIMEOUT,
    MODEL_LIMIT_RE,
    RATE_ERR,
    REFUSAL_ERR,
    RunResult,
    _context_bytes,
    _maybe_notify,
    claude_cli_error_text,
    managed_prompt,
    normalize_prompt,
    parse_claude_token_fields,
    parse_claude_usage,
)
from .development_policy import (
    DevelopmentPolicyError,
    WORKER_ACCESS_ENV_KEY,
    WorkerAccessDecision,
    resolve_worker_access,
)
from .quota_cooldown import bounded_cooldown_until, quota_cooldown_provenance
from .write_boundary import boundary_plan, configured_seat_home_paths
from .task_policy import (
    DEFAULT_CLAUDE_MODEL_ROLES,
    current_claude_role_model,
    resolve_task_policy,
)
from .worker_config import (
    create_worker_capabilities_directory,
    link_portable_primary_skills,
    native_claude_mcp_names,
    portable_primary_mcp_servers,
    resolved_worker_mcp_specs,
)
from .worker_launch import create_worker_launch_directory
from .worktree_layout import WorktreeLayoutError

def effort_for_kind(settings: dict[str, Any], kind: str) -> str | None:
    return resolve_task_policy(settings, kind).effort


def _claude_model_roles(settings: dict[str, Any]) -> dict[str, str]:
    configured = settings.get("providers", {}).get("claude", {}).get("model_roles", {})
    if not isinstance(configured, dict):
        configured = {}
    return {
        role: current_claude_role_model(str(configured.get(role) or model))
        for role, model in DEFAULT_CLAUDE_MODEL_ROLES.items()
    }


def _resolve_claude_model(settings: dict[str, Any], value: str) -> str:
    roles = _claude_model_roles(settings)
    aliases = {
        "fable": roles.get("frontier"),
        "frontier": roles.get("frontier"),
        "opus": roles.get("complex"),
        "complex": roles.get("complex"),
        "sonnet": roles.get("routine"),
        "routine": roles.get("routine"),
    }
    return current_claude_role_model(str(aliases.get(value.lower()) or value))


def claude_model_candidates(
    settings: dict[str, Any],
    kind: str,
    size: str,
    explicit_model: str | None = None,
    capability: str | None = None,
) -> list[str]:
    """Return a capability-descending, task-fit Claude fallback chain."""
    config = settings.get("providers", {}).get("claude", {})
    roles = _claude_model_roles(settings)
    frontier = roles["frontier"]
    complex_model = roles["complex"]
    routine = roles["routine"]
    explicit = explicit_model or config.get("model")
    tier = resolve_task_policy(
        settings, kind, size=size, capability=capability
    ).capability
    if explicit:
        selected = _resolve_claude_model(settings, str(explicit))
        lowered = selected.lower()
        if "fable" in lowered or selected == frontier:
            candidates = [selected, complex_model, routine]
        elif "opus" in lowered or selected == complex_model:
            candidates = [selected, routine]
        else:
            candidates = [selected]
    elif tier == "frontier":
        candidates = [frontier, complex_model, routine]
    elif tier == "complex":
        candidates = [complex_model, routine]
    else:
        candidates = [routine]
    return list(dict.fromkeys(model for model in candidates if model))


def claude_model_available(provider: dict[str, Any], model: str) -> bool:
    states = provider.get("models")
    state = states.get(model, {}) if isinstance(states, dict) else {}
    return not isinstance(state, dict) or state.get("status") != "capped"


def claude_model_limit(error: str, model: str) -> bool:
    if not RATE_ERR.search(error):
        return False
    lowered = error.lower()
    family = next((name for name in ("fable", "opus", "sonnet") if name in model.lower()), None)
    return bool(MODEL_LIMIT_RE.search(error) or (family and family in lowered and "limit" in lowered))


def claude_headroom(limits: dict[str, Any]) -> float | None:
    remaining: list[float] = []
    for name in ("five_hour", "seven_day"):
        window = limits.get(name)
        value = window.get("remaining_percentage") if isinstance(window, dict) else None
        if isinstance(value, (int, float)):
            remaining.append(float(value))
    return min(remaining) if remaining else None


def claude_task_fits(
    settings: dict[str, Any],
    size: str,
    limits: dict[str, Any],
    *,
    allow_reserve: bool = False,
    require_fresh_known: bool = False,
    at: float | None = None,
) -> tuple[bool, str | None]:
    config = settings.get("providers", {}).get("claude", {})
    key = "big_task_min_headroom_pct" if size == "big" else "small_task_min_headroom_pct"
    required = float(config.get(key, 30 if size == "big" else 10))
    if size == "big" or require_fresh_known:
        if not claude_usage_fresh(settings, limits, at):
            return False, f"Claude usage is stale or unknown; routing requires {required:g}% fresh headroom"
        missing = [name for name in ("five_hour", "seven_day") if name not in limits]
        if missing:
            return False, (
                f"Claude {', '.join(missing)} capacity is unknown; routing requires "
                f"{required:g}% known headroom"
            )
    if allow_reserve:
        return True, None
    headroom = claude_headroom(limits)
    if headroom is None:
        if size == "big" or require_fresh_known:
            return False, f"Claude capacity is unknown; routing requires {required:g}% known headroom"
        return True, None
    if headroom < required:
        return False, f"Claude has {headroom:g}% headroom; {size} tasks require {required:g}%"
    return True, None


def claude_capacity_report(
    settings: dict[str, Any],
    provider: dict[str, Any],
    limits: dict[str, Any],
    *,
    mode: str | None = None,
) -> dict[str, Any]:
    """Return the shared Claude routing/reporting capacity verdict.

    Busy state is deliberately not part of the verdict: like a Codex seat, a
    busy Claude provider can still have capacity for the next task.  Keeping
    mode, provider health, model availability, and quota checks here prevents
    the human dashboard from drifting away from ``status --json``.
    """
    effective_mode = mode or effective_claude_mode(settings)[0]
    status = str(provider.get("status") or "unknown")
    applicable = effective_mode != "off" and status == "ok"
    result: dict[str, Any] = {
        "mode": effective_mode,
        "provider_status": status,
        "applicable": applicable,
    }
    task_profiles = {
        "small": ("review", "small"),
        "big": ("implementation", "big"),
    }
    for size, (kind, task_size) in task_profiles.items():
        quota_fits, quota_reason = claude_task_fits(settings, size, limits)
        candidates = claude_model_candidates(settings, kind, task_size)
        available_models = [
            model for model in candidates if claude_model_available(provider, model)
        ]
        model_available = bool(available_models)
        fits = applicable and model_available and quota_fits
        result[size] = {
            "fits": fits,
            "quota_fits": quota_fits,
            "quota_reason": quota_reason,
            "model_available": model_available,
            "candidate_models": candidates,
            "available_models": available_models,
        }
        result[f"fits_{size}"] = fits
    return result


def run_claude(
    root: Path,
    settings: dict[str, Any],
    request: dict[str, Any],
    *,
    access_decision: WorkerAccessDecision | None = None,
) -> RunResult:
    prompt, label = normalize_prompt(request["prompt"], request.get("label"))
    repository = Path(request.get("cd") or os.getcwd()).expanduser().resolve()
    kind = request.get("kind", "review")
    size = request.get("size") or default_task_size(settings, kind)
    # Resolve effort ONCE (request override, then the kind default — same rule as
    # the Codex head) so the --effort flag and the learning key cannot diverge.
    # The claude CLI has no "ultra"; unclamped it would silently run at default.
    effort, effort_downgraded_from = clamp_effort(
        request.get("effort") or effort_for_kind(settings, kind),
        supports_ultra=False,
    )
    mode, reason = effective_claude_mode(settings)
    if mode == "off":
        return RunResult(4, "disabled", "claude", "claude", label, message=reason or "Claude is hard-off")
    try:
        access = access_decision or resolve_worker_access(settings, request)
    except DevelopmentPolicyError as exc:
        return RunResult(
            2, "blocked_needs_controller", "claude", "claude", label,
            message=f"invalid worker access setting: {exc}",
        )
    try:
        mcp_servers = resolved_worker_mcp_specs(request.get("mcp", []), repository)
    except ValueError as exc:
        return RunResult(
            2, "blocked_needs_controller", "claude", "claude", label,
            message=f"Invalid worker MCP grant: {exc}",
        )
    # Trusted workers additionally inherit the primary Codex configuration's
    # portable capabilities for this one job (skills by --add-dir link, standard
    # MCP transports into the additive per-job config) without registration.
    # Explicit catalog grants and native configuration keep precedence; restricted
    # jobs are unchanged.
    portability_warnings: list[str] = []
    if access.trusted:
        portable_mcp, mcp_warnings = portable_primary_mcp_servers(
            root, settings, repository
        )
        portability_warnings.extend(mcp_warnings)
        if portable_mcp:
            native_names = native_claude_mcp_names(
                workers.resolve_executable("claude", settings["providers"]["claude"].get("command")), repository
            )
            if native_names is None:
                portability_warnings.append(
                    "Native Claude MCP names could not be checked; portable MCP forwarding "
                    "was omitted to preserve existing connections. Native discovery remains active."
                )
                portable_mcp = {}
            else:
                for name in sorted(native_names & portable_mcp.keys()):
                    portability_warnings.append(f"portable MCP {name!r} already exists in Claude; native configuration wins")
                    del portable_mcp[name]
        # Explicit worker/catalog grants win on a name collision.
        mcp_servers = {**portable_mcp, **mcp_servers}
    if not request.get("_capacity_checked"):
        small_canary = bool(request.get("small_canary")) and size == "small"
        fits, fit_reason = claude_task_fits(
            settings,
            size,
            load_claude_rate_limits(root),
            allow_reserve=bool(request.get("allow_reserve")),
            require_fresh_known=not small_canary,
        )
        if not fits:
            return RunResult(
                3,
                "reserved",
                "claude",
                "claude",
                label,
                message=fit_reason,
            )
    with Ledger(root) as data:
        refresh_transients(data, root, settings)
        provider = data["providers"]["claude"]
        if provider.get("status") != "ok" or provider.get("in_flight"):
            return RunResult(
                3,
                "unavailable",
                "claude",
                "claude",
                label,
                message="Claude unavailable or busy; caller retained ownership",
            )
        candidates = [
            model
            for model in claude_model_candidates(
                settings,
                kind,
                size,
                request.get("model"),
                request.get("capability_tier"),
            )
            if claude_model_available(provider, model)
        ]
        if not candidates:
            return RunResult(
                3,
                "unavailable",
                "claude",
                "claude",
                label,
                message="all task-fit Claude models are currently capped",
                fallback_safe=True,
            )
        job_id = f"{int(now())}-claude"
        provider["in_flight"] = {
            "job_id": job_id,
            "dispatcher_pid": os.getpid(),
            "child_pid": None,
            "kind": kind,
            "size": size,
            "label": label,
            "model": candidates[0],
            "attempted_models": [],
            "started_at": iso(),
            "started_ts": now(),
        }

    config = settings["providers"]["claude"]
    outputs: list[Path] = []
    attempted: list[str] = []
    capped_models: list[tuple[str, str]] = []
    refused_models: list[tuple[str, str]] = []
    selected_model: str | None = None
    provider_failure: str | None = None
    quota_error: str | None = None
    fallback_safe = False
    structured: dict[str, Any] | None = None
    status, text, duration, tokens, cost = "failed", "", 0, 0, None
    token_fields = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "uncached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "provider_tokens": 0,
    }
    declared_scopes = list(request.get("write_scope", []))
    external_scopes = list(request.get("external_write_scope", []))
    if request.get("sandbox") == "read-only":
        declared_scopes = []
        external_scopes = []
    private_temp: Path | None = None
    boundary = None
    launch_directory: Path | None = None
    capabilities_directory: Path | None = None
    skills_root: Path | None = None
    starting_limits = load_claude_rate_limits(root)
    # The worker runs in a disposable git worktree (supervise/goals) or the isolated
    # one-shot `run` path — a partial or failed change is contained and discarded with
    # the worktree, so the old in-place writable-boundary fingerprint detection is gone.
    # worker_hook still enforces the exfil/secret/command deny-lists inside the worktree.
    try:
        boundary = boundary_plan(
            repository,
            declared_scopes,
            external_scopes,
            strict_scope=request.get("strict_scope") is True,
            parallel_safe=request.get("parallel_safe") is True,
            allow_git_write=request.get("allow_git_write") is True,
        )
        scopes = list(boundary.effective_scope)
        private_parent = root / "private-tmp"
        private_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        private_temp = Path(tempfile.mkdtemp(prefix=f"{job_id}-", dir=private_parent))
        launch_directory = create_worker_launch_directory(root, job_id)
        if access.trusted:
            # Skill resource trees are linked into a readable, task-owned root so
            # native --add-dir discovery of .claude/skills works; native skills
            # win on a name collision. The launch directory itself is a denied
            # pool control, so the links live in a separate disposable directory.
            capabilities_directory = create_worker_capabilities_directory(root, job_id)
            skills_root, skill_warnings = link_portable_primary_skills(
                root, settings, repository, capabilities_directory
            )
            portability_warnings.extend(skill_warnings)
        settings_path, mcp_path, schema_path = write_isolation_files(
            launch_directory,
            repository,
            scopes,
            external_scopes,
            root / "worker-hook",
            private_temp=private_temp,
            protected_paths=request.get("protected_paths", []),
            allow_git_write=boundary.allow_git_write,
            git_metadata=boundary.git_metadata,
            control_root=root,
            runtime_settings=settings,
            mcp_servers=mcp_servers,
            trusted=access.trusted,
        )
        binary = workers.resolve_executable("claude", config.get("command"))
        with workers.PowerLease(settings, request.get("allow_battery", False)):
            for model in candidates:
                fallback_safe = False
                attempted.append(model)
                with Ledger(root) as data:
                    claim = data["providers"]["claude"].get("in_flight")
                    if isinstance(claim, dict) and claim.get("job_id") == job_id:
                        claim["model"] = model
                        claim["attempted_models"] = list(attempted)
                        claim["child_pid"] = None
                slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", model).strip("-") or "model"
                output = root / "logs" / f"{job_id}-{slug}.json"
                outputs.append(output)
                # --tools restricts builtins, not MCP permissions. Granted MCP
                # servers also need ToolSearch for deferred tool discovery.
                command = [
                    binary,
                    "-p",
                    "--output-format",
                    "json",
                    "--no-session-persistence",
                    "--permission-mode",
                    "dontAsk",
                    "--settings",
                    str(settings_path),
                    "--json-schema",
                    schema_path.read_text().strip(),
                    "--disallowed-tools",
                    "Agent" if access.trusted else ",".join(DENIED_TOOLS),
                    "--model",
                    model,
                ]
                if access.trusted:
                    if mcp_servers:
                        command[4:4] = ["--mcp-config", str(mcp_path)]
                    if skills_root is not None:
                        # Native skill discovery of .claude/skills in the linked
                        # task-owned directory; the documented --add-dir exception.
                        command[4:4] = ["--add-dir", str(skills_root)]
                else:
                    command[4:4] = [
                        "--disable-slash-commands",
                        "--no-chrome",
                        "--setting-sources",
                        "",
                        "--strict-mcp-config",
                        "--mcp-config",
                        str(mcp_path),
                        "--tools",
                        ",".join(
                            [
                                "Bash", "Edit", "Write", "Read", "Glob", "Grep",
                                *(["ToolSearch"] if mcp_servers else []),
                            ]
                        ),
                    ]
                if effort:
                    command += ["--effort", effort]
                command.append(
                    managed_prompt(f"Repository root: {repository}\n\n{prompt}")
                )
                attempt_status, attempt_text, _, attempt_duration = workers._execute(
                    command,
                    output,
                    int(request.get("timeout", DEFAULT_TIMEOUT)),
                    str(repository if boundary.balanced else private_temp),
                    # Trusted workers use ordinary OS temporary storage; the
                    # controller's private artifact directory stays separate.
                    scrubbed_claude_env(
                        private_temp,
                        temp_root=(
                            Path(tempfile.gettempdir()) if access.trusted else None
                        ),
                        **{WORKER_ACCESS_ENV_KEY: access.effective},
                        CODEX_SEATS_WRITE_SCOPE=json.dumps(scopes),
                        CODEX_SEATS_DECLARED_WRITE_SCOPE=json.dumps(declared_scopes),
                        CODEX_SEATS_EXTERNAL_WRITE_SCOPE=json.dumps(external_scopes),
                        CODEX_SEATS_TASK_ROOT=str(repository),
                        CODEX_SEATS_CONTROL_ROOT=str(root.resolve()),
                        CODEX_SEATS_PROTECTED_HOMES=json.dumps(
                            [
                                str(path)
                                for path in configured_seat_home_paths(root, settings)
                            ]
                        ),
                        CODEX_SEATS_PRIVATE_TEMP=str(private_temp),
                        CODEX_SEATS_PROTECTED_PATHS=json.dumps(request.get("protected_paths", [])),
                        CODEX_SEATS_ALLOW_GIT_WRITE="1" if boundary.allow_git_write else "0",
                        CODEX_SEATS_GIT_METADATA_PATHS=json.dumps(
                            [str(path) for path in boundary.git_metadata]
                        ),
                        CODEX_SEATS_RELEASE_ID=str(request.get("release_id") or ""),
                        CODEX_SEATS_DISPATCH_PROTOCOL=str(
                            request.get("protocol_version") or ""
                        ),
                    ),
                    root,
                    "claude",
                    "claude",
                )
                attempt_tokens, attempt_cost = parse_claude_usage(output)
                attempt_fields = parse_claude_token_fields(output)
                for key in token_fields:
                    token_fields[key] += attempt_fields[key]
                duration += attempt_duration
                tokens += attempt_tokens
                if attempt_cost is not None:
                    cost = (cost or 0.0) + attempt_cost
                cli_error = claude_cli_error_text(output, attempt_text)
                attempt_structured = structured_claude_result(output)
                if attempt_status == "completed" and not cli_error and attempt_structured:
                    structured = attempt_structured
                    status = str(attempt_structured["status"])
                    text, selected_model = attempt_text, model
                    break
                # A failed attempt only dirtied the disposable worktree, so falling back
                # to another model or provider is always safe.
                status, text = "failed", attempt_text
                if AUTH_ERR.search(cli_error):
                    provider_failure, fallback_safe = "needs-auth", True
                    break
                if RATE_ERR.search(cli_error):
                    fallback_safe = True
                    if claude_model_limit(cli_error, model):
                        capped_models.append((model, cli_error[-500:]))
                        continue
                    provider_failure = "cooldown"
                    quota_error = cli_error
                    break
                if REFUSAL_ERR.search(cli_error):
                    # A refusal is model-specific for this task (e.g. Fable's
                    # cyber classifiers), not a capacity state: advance the
                    # chain to the next model, never mark this one capped.
                    fallback_safe = True
                    refused_models.append((model, cli_error[-500:]))
                    continue
                fallback_safe = True
                break
    except ProcessCancelledError as exc:
        status, text, fallback_safe = "cancelled", str(exc), False
    except WorktreeLayoutError as exc:
        # An isolation/layout gate fails explicitly and terminally: never as
        # retryable capacity.
        status = "blocked_needs_controller"
        text = f"worker isolation failed: {exc}"
        fallback_safe = False
    except (FileNotFoundError, RuntimeError, OSError, ValueError) as exc:
        raw_error = str(exc)
        status, text = "failed", "Claude launch or isolation failed safely."
        if isinstance(exc, FileNotFoundError) or "executable not found" in raw_error:
            provider_failure = "missing"
        # The worktree is disposable, so a launch/isolation failure is always safe to fall back from.
        fallback_safe = True
    finally:
        if private_temp is not None:
            shutil.rmtree(private_temp, ignore_errors=True)
        if launch_directory is not None:
            shutil.rmtree(launch_directory, ignore_errors=True)
        if capabilities_directory is not None:
            # rmtree removes the symlinks themselves, never the primary skill
            # trees they point at.
            shutil.rmtree(capabilities_directory, ignore_errors=True)

    structured = with_release_metadata(
        structured, request.get("release_id"), request.get("protocol_version")
    )
    if isinstance(structured, dict):
        # A worker cannot supply or forge a controller-owned policy stamp.
        structured.pop("filesystem_policy", None)
        structured["worker_access"] = access.effective
        if access.trusted:
            structured["warnings"] = [
                *structured.get("warnings", []),
                *portability_warnings,
                "Provider-native skill/MCP/plugin inheritance still requires installed-runtime acceptance; provider-specific connectors may be nonportable.",
            ]
    with Ledger(root) as data:
        provider = data["providers"]["claude"]
        provider["in_flight"] = None
        provider["last_job_at"] = iso()
        provider["last_error"] = None if status == "completed" else text[-500:]
        model_states = provider.setdefault("models", {})
        if not isinstance(model_states, dict):
            model_states = provider["models"] = {}
        cooldown_until = now() + int(config.get("cooldown_seconds", 18000))
        for model, error in capped_models:
            model_states[model] = {
                "status": "capped",
                "status_until": cooldown_until,
                "last_error": error,
                "last_job_at": iso(),
            }
        for model, error in refused_models:
            # Forensics only — a refusal must NOT cap the model (it stays fully
            # available for every other task).
            state = model_states.setdefault(model, {})
            state.update({"last_refusal_at": iso(), "last_refusal": error})
        if selected_model:
            state = model_states.setdefault(selected_model, {})
            state.update({"status": "ok", "status_until": None, "last_error": None, "last_job_at": iso()})
            provider["last_resolved_model"] = selected_model
        elif attempted:
            provider["last_resolved_model"] = attempted[-1]
        ending_limits = load_claude_rate_limits(root)
        if provider_failure == "needs-auth":
            provider["status"] = "needs-auth"
            provider["cooldown_provenance"] = None
        elif provider_failure == "cooldown":
            provider["status"] = "cooldown"
            # Bound the provider-wide quota cooldown by the affected window's
            # snapshot reset epoch when one is available; the typed provenance
            # lets refresh_transients admit an evidence-gated early recovery.
            # Strict evidence only: a synthesized/global observation timestamp
            # must not make a recorded reset look trustworthy.
            failed_at = now()
            provenance = quota_cooldown_provenance(
                quota_error,
                load_claude_quota_evidence(root, failed_at),
                failed_at,
                usage_stale_seconds=float(
                    config.get("usage_stale_seconds", 6 * 3600)
                ),
            )
            provider["status_until"] = bounded_cooldown_until(provenance, cooldown_until)
            provider["cooldown_provenance"] = provenance
        elif provider_failure == "missing":
            provider["status"] = "missing"
            provider["cooldown_provenance"] = None
        final_status = "completed" if status == "completed" else (
            "capacity" if fallback_safe and status == "failed" else status
        )
        starting_headroom = claude_headroom(starting_limits)
        ending_headroom = claude_headroom(ending_limits)
        starting_identity = capacity_reset_identity(starting_limits)
        ending_identity = capacity_reset_identity(ending_limits)
        cadence = "subscription"
        # The resolved (clamped) effort from the top of run_claude — the same value
        # the CLI actually ran with; "default" when no effort applied at all.
        effort_key = effort or "default"
        sample_model = selected_model or (
            attempted[-1]
            if attempted
            else str(config.get("model") or DEFAULT_CLAUDE_MODEL_ROLES["routine"])
        )
        sample_key = learning_key("claude", cadence, sample_model, size, effort_key)
        if (
            status == "completed"
            and starting_identity is not None
            and starting_identity == ending_identity
            and isinstance(starting_headroom, (int, float))
            and isinstance(ending_headroom, (int, float))
        ):
            add_capacity_sample(
                data.setdefault("capacity_learning", {}),
                provider="claude",
                cadence=cadence,
                model=sample_model,
                size=size,
                effort=effort_key,
                burn_pct=max(0.0, float(starting_headroom) - float(ending_headroom)),
                clean=True,
            )
        elif status == "completed" and starting_identity != ending_identity:
            add_capacity_sample(
                data.setdefault("capacity_learning", {}),
                provider="claude",
                cadence=cadence,
                model=sample_model,
                size=size,
                effort=effort_key,
                burn_pct=0,
                reset_contaminated=True,
            )
        elif provider_failure == "cooldown":
            static_floor = float(
                config.get(
                    "big_task_min_headroom_pct" if size == "big" else "small_task_min_headroom_pct",
                    30 if size == "big" else 10,
                )
            )
            raise_risk_floor(
                data.setdefault("capacity_learning", {}),
                sample_key,
                static_floor + 10,
                ending_identity,
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
                "provider": "claude",
                "seat": None,
                "label": label,
                "kind": kind,
                "size": size,
                "model": selected_model or (attempted[-1] if attempted else None),
                "attempted_models": attempted,
                "refused_models": [model for model, _ in refused_models],
                "effort": effort,
                "effort_downgraded_from": effort_downgraded_from,
                "tokens": tokens,
                "cost_usd": cost,
                "raw_tokens": token_fields["provider_tokens"],
                "cached_tokens": token_fields["cached_input_tokens"],
                "uncached_tokens": token_fields["uncached_input_tokens"],
                **token_fields,
                "starting_capacity_pct": starting_headroom,
                "ending_capacity_pct": ending_headroom,
                "starting_windows": starting_limits,
                "ending_windows": ending_limits,
                "predicted_minutes": request.get("estimated_minutes"),
                "actual_minutes": duration / 60,
                "route_reason": request.get("route_reason"),
                "route_provider": request.get("route_provider"),
                "goal_id": request.get("goal_id"),
                "iteration": request.get("iteration"),
                "dynamic_requirement_pct": request.get("dynamic_requirement_pct"),
                "binding_window": request.get("binding_window"),
                "fallback_count": max(0, len(attempted) - 1),
                "write_boundary_mode": boundary.mode if boundary else "unresolved",
                "strict_scope": request.get("strict_scope") is True,
                "worker_access": access.effective,
                "git_capability": boundary.allow_git_write if boundary else False,
                "status": final_status,
                "secs": duration,
                "log": str(outputs[-1]) if outputs else None,
                "logs": [str(path) for path in outputs],
                "context_bytes": _context_bytes(root, settings, request["prompt"], str(repository)),
                "structured_result": structured,
            }
        )
        trim_jobs_with_archive(data, root)
    result_status = "completed" if status == "completed" else (
        "capacity" if fallback_safe and status == "failed" else status
    )
    if not request.get("_suppress_notify"):
        _maybe_notify(settings, label, result_status, "Claude", duration)
    return RunResult(
        0 if status == "completed" else 1,
        result_status,
        "claude",
        "claude",
        label,
        duration,
        tokens,
        str(outputs[-1]) if outputs else None,
        text[-500:],
        fallback_safe,
        selected_model or (attempted[-1] if attempted else None),
        structured,
        effort,
        effort_downgraded_from,
    )


# Provider selection and cross-provider dispatch live in router.py.
