"""Managed Qwen Code execution for a controller-selected local seat.

The provider consumes the local-seat control plane's bounded probe,
nonblocking claim, and optional host lease. It remains independent of
frontier-account capacity state even when the router selects it as phase zero.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import bounded_process, local_lease, local_seats, workers
from .development_policy import (
    DevelopmentPolicyError,
    WORKER_ACCESS_ENV_KEY,
    WorkerAccessDecision,
    resolve_worker_access,
)
from .managed import RESULT_SCHEMA, with_release_metadata
from .scope import resolved_scope_paths
from .seat_registry import LOCAL_ID_RE, validate_local_registries
from .state import Ledger, atomic_write_json, iso, load_settings, now
from .task_policy import resolve_task_policy
from .telemetry import JOB_TELEMETRY_SCHEMA, trim_jobs_with_archive
from .worker_config import resolved_worker_mcp_specs
from .worker_environment import minimal_worker_env
from .worker_launch import create_worker_launch_directory
from .workers import DEFAULT_TIMEOUT, RunResult, managed_prompt, normalize_prompt
from .write_boundary import (
    boundary_plan,
    launch_agent_control_paths,
    configured_seat_home_paths,
    known_credential_store_paths,
    pool_runtime_paths,
)


LOCAL_POLICY_VERSION = "qwen-local-v1"
MAX_QWEN_OUTPUT_BYTES = 4 * 1024 * 1024

# Qwen Code 0.16.0 internal tool names. The structured-output tool is synthetic
# and registered by --json-schema; it is deliberately never excluded so Qwen
# can always finish the managed receipt contract.
_WRITE_TOOLS = ("edit", "write_file", "notebook_edit")
_ALWAYS_EXCLUDED = (
    "agent",
    "task",  # legacy alias for agent
    "skill",
    "save_memory",
    "todo_write",
    "cron_create",
    "cron_list",
    "cron_delete",
    "monitor",
    "task_stop",
    "ask_user_question",
    "enter_worktree",
    "exit_worktree",
    "send_message",
    "exit_plan_mode",
    "tool_search",
)
_DISABLED_SLASH_COMMANDS = (
    "auth",
    "mcp",
    "extensions",
    "hooks",
    "memory",
    "remember",
    "dream",
    "skills",
    "agents",
    "cron",
    "monitor",
    "tasks",
    "worktree",
)


class LocalOutputError(ValueError):
    """Qwen emitted an invalid or contradictory terminal envelope."""


class LocalSandboxUnavailable(RuntimeError):
    """The OS sandbox could not start before Qwen emitted any agent output."""


def _reload_settings(root: Path, supplied: dict[str, Any]) -> dict[str, Any]:
    """Reload the authoritative settings snapshot when one is installed."""
    if not (root / "settings.json").exists():
        return supplied
    current, _warnings = load_settings(root)
    return current


def _disabled(
    label: str, seat_id: str | None, message: str, *, status: str = "disabled"
) -> RunResult:
    return RunResult(
        4 if status == "disabled" else 3,
        status,
        "local",
        seat_id,
        label,
        message=message,
        fallback_safe=True,
    )


def _prestart_failure(
    label: str,
    seat_id: str | None,
    message: str,
    *,
    status: str = "unavailable",
    code: int = 3,
) -> RunResult:
    return RunResult(
        code,
        status,
        "local",
        seat_id,
        label,
        message=message,
        fallback_safe=True,
    )


def _excluded_tools(*, writable: bool) -> list[str]:
    # Qwen's --allowed-tools is only an approval bypass, not a capability
    # allowlist. The run is autonomous under the OS sandbox, while this actual
    # exclusion list removes delegation, persistence, and writes for read-only
    # jobs from the registered tool pool.
    excluded = list(_ALWAYS_EXCLUDED)
    if not writable:
        excluded.extend(_WRITE_TOOLS)
    return excluded


def _qwen_settings(
    mcp_servers: Mapping[str, Any], *, model: str, base_url: str,
    context_window_size: int | None = None,
) -> dict[str, Any]:
    """Return the controller-owned, highest-precedence Qwen settings."""
    return {
        "$version": 2,
        "general": {"chatRecording": False, "checkpointing": {"enabled": False}},
        "telemetry": {
            "enabled": False,
            "logPrompts": False,
            "includeSensitiveSpanAttributes": False,
        },
        "privacy": {"usageStatisticsEnabled": False},
        "memory": {
            "enableManagedAutoMemory": False,
            "enableManagedAutoDream": False,
            "enableAutoSkill": False,
            "enableTeamMemory": False,
            "enableTeamMemorySync": False,
        },
        "disableAllHooks": True,
        "hooks": {},
        "experimental": {"cron": False},
        "mcpServers": dict(mcp_servers),
        "mcp": {"allowed": sorted(mcp_servers), "excluded": []},
        "modelProviders": {
            "openai": [
                {
                    "id": model,
                    "name": model,
                    "baseUrl": base_url,
                    "envKey": "OPENAI_API_KEY",
                    "generationConfig": {
                        "extra_body": {"enable_thinking": True},
                        **({"contextWindowSize": context_window_size}
                           if context_window_size is not None else {}),
                    },
                }
            ]
        },
        "model": {
            "name": model,
            "enableOpenAILogging": False,
            "skipLoopDetection": True,
        },
        "security": {
            "auth": {"selectedType": "openai"},
            "folderTrust": {"enabled": False},
        },
        "tools": {"sandbox": False, "toolSearch": {"enabled": False}},
        "slashCommands": {"disabled": list(_DISABLED_SLASH_COMMANDS)},
    }


def _seatbelt_string(value: str | Path) -> str:
    """Encode a literal path for Qwen's Scheme-like Seatbelt profile."""
    return json.dumps(str(value), ensure_ascii=True)


def _seatbelt_rule(action: str, operation: str, path: Path, *, tree: bool) -> str:
    matcher = "subpath" if tree else "literal"
    return f"({action} {operation} ({matcher} {_seatbelt_string(path)}))"


def _seatbelt_tree_rules(action: str, operation: str, path: Path) -> list[str]:
    """Cover both the directory entry itself and every descendant."""
    return [
        _seatbelt_rule(action, operation, path, tree=False),
        _seatbelt_rule(action, operation, path, tree=True),
    ]


def _seatbelt_regex_rule(action: str, operation: str, pattern: str) -> str:
    return f"({action} {operation} (regex #{json.dumps(pattern, ensure_ascii=True)}))"


def _glob_path_regex(path: Path) -> str:
    """Translate trusted path globs to an anchored Seatbelt-compatible regex."""
    rendered = str(path)
    pieces: list[str] = []
    for character in rendered:
        if character == "*":
            pieces.append("[^/]*")
        elif character == "?":
            pieces.append("[^/]")
        else:
            pieces.append(re.escape(character))
    return "^" + "".join(pieces) + r"(?:/.*)?$"


def _seatbelt_path_rules(action: str, operation: str, path: Path) -> list[str]:
    if any(marker in str(path) for marker in "*?"):
        return [_seatbelt_regex_rule(action, operation, _glob_path_regex(path))]
    return _seatbelt_tree_rules(action, operation, path)


def _write_seatbelt_profile(
    launch_directory: Path,
    repository: Path,
    scopes: list[Any],
    external_scopes: list[Any],
    *,
    root: Path,
    settings: Mapping[str, Any],
    boundary: Any,
    protected_paths: list[Any],
) -> Path:
    """Write the restricted Qwen child profile used by macOS sandbox-exec.

    Qwen's bundled profile has useful write confinement but deliberately grants
    host-wide reads.  Managed workers additionally hide provider credentials,
    controller state, and repository secret files, matching the other seats.
    """
    qwen_directory = launch_directory / ".qwen"
    qwen_directory.mkdir(mode=0o700)
    runtime = launch_directory / "qwen-runtime"
    home = launch_directory / "qwen-home"
    writes = resolved_scope_paths(scopes, repository)
    writes += resolved_scope_paths(external_scopes, repository, external=True)
    lines = ["(version 1)", "(allow default)", "(deny file-write*)"]
    for device in (
        Path("/dev/null"),
        Path("/dev/stdin"),
        Path("/dev/stdout"),
        Path("/dev/stderr"),
        Path("/dev/tty"),
        Path("/dev/fd"),
    ):
        lines.extend(_seatbelt_tree_rules("allow", "file-write*", device))
    for path in (launch_directory, runtime, home):
        lines.extend(_seatbelt_tree_rules("allow", "file-write*", path))
    for path, kind in writes:
        if kind == "tree":
            lines.extend(_seatbelt_tree_rules("allow", "file-write*", path))
        else:
            lines.append(_seatbelt_rule("allow", "file-write*", path, tree=False))

    # Git administration and protected paths remain read-only even under a
    # balanced whole-repository grant.
    for path in boundary.git_metadata:
        lines.extend(_seatbelt_tree_rules("deny", "file-write*", path))
    for relative in protected_paths:
        if not isinstance(relative, str):
            raise ValueError("protected paths must be strings")
        protected = (repository / relative).resolve(strict=False)
        try:
            protected.relative_to(repository)
        except ValueError as exc:
            raise ValueError(f"protected path escapes repository: {relative}") from exc
        lines.extend(_seatbelt_tree_rules("deny", "file-write*", protected))

    denied = [
        *known_credential_store_paths(),
        *launch_agent_control_paths(),
        *pool_runtime_paths(root, settings),
        *configured_seat_home_paths(root, settings),
    ]
    # Exact known stores are denied for both read and write.  The launch child
    # is re-granted last because it is intentionally nested below control root.
    for path in dict.fromkeys(Path(item) for item in denied):
        lines.extend(_seatbelt_path_rules("deny", "file-read*", path))
        lines.extend(_seatbelt_path_rules("deny", "file-write*", path))

    # Secret names are denied recursively across the real user home, not only
    # in the repository. Qwen should see source but never ambient dotenv,
    # provider-auth, private-key, or keychain material.
    escaped_home = re.escape(str(Path.home().resolve()))
    secret_names = (
        r"(?:\.env(?:\.[^/]+)?|auth\.json|\.?credentials\.json|"
        r"[^/]+\.(?:key|pem|p12|pfx|keychain|keychain-db))"
    )
    secret_regex = f"^{escaped_home}(?:/.*)?/{secret_names}$"
    lines.append(_seatbelt_regex_rule("deny", "file-read*", secret_regex))
    lines.append(_seatbelt_regex_rule("deny", "file-write*", secret_regex))
    # Qwen resolves its controller-owned settings path with realpathSync before
    # reading it.  Permit lstat of the worker-launch parent itself, while keeping
    # sibling launch contents hidden by the broad runtime deny above.
    lines.append(
        _seatbelt_rule(
            "allow", "file-read-metadata", launch_directory.parent, tree=False
        )
    )
    lines.extend(_seatbelt_tree_rules("allow", "file-read*", launch_directory))
    lines.extend(_seatbelt_tree_rules("allow", "file-write*", launch_directory))

    profile = qwen_directory / "sandbox-macos-codex-seats.sb"
    profile.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(profile, 0o600)
    return profile


def _qwen_env(
    launch_directory: Path,
    repository: Path,
    root: Path,
    settings: Mapping[str, Any],
    request: Mapping[str, Any],
    access: WorkerAccessDecision,
    scopes: list[Any],
    declared_scopes: list[Any],
    external_scopes: list[Any],
) -> dict[str, str]:
    qwen_home = launch_directory / "qwen-home"
    runtime = launch_directory / "qwen-runtime"
    qwen_home.mkdir(mode=0o700, exist_ok=True)
    runtime.mkdir(mode=0o700, exist_ok=True)
    system_settings = qwen_home / "settings.json"
    env = minimal_worker_env(
        HOME=str(qwen_home),
        QWEN_HOME=str(qwen_home),
        QWEN_RUNTIME_DIR=str(runtime),
        QWEN_CODE_SYSTEM_SETTINGS_PATH=str(system_settings),
        QWEN_CODE_SYSTEM_DEFAULTS_PATH=str(system_settings),
        QWEN_TELEMETRY_ENABLED="0",
        QWEN_TELEMETRY_LOG_PROMPTS="0",
        QWEN_TELEMETRY_INCLUDE_SENSITIVE_SPAN_ATTRIBUTES="0",
        QWEN_USAGE_STATISTICS_ENABLED="0",
        QWEN_CODE_MEMORY_TEAM="0",
        QWEN_CODE_MEMORY_TEAM_SYNC="0",
        QWEN_CODE_ENABLE_CRON="0",
        QWEN_SANDBOX="sandbox-exec",
        NO_BROWSER="1",
        CODEX_SEATS_MANAGED_WORKER="1",
        **{WORKER_ACCESS_ENV_KEY: access.effective},
        CODEX_SEATS_WRITE_SCOPE=json.dumps(scopes, separators=(",", ":")),
        CODEX_SEATS_DECLARED_WRITE_SCOPE=json.dumps(
            declared_scopes, separators=(",", ":")
        ),
        CODEX_SEATS_EXTERNAL_WRITE_SCOPE=json.dumps(
            external_scopes, separators=(",", ":")
        ),
        CODEX_SEATS_TASK_ROOT=str(repository),
        CODEX_SEATS_CONTROL_ROOT=str(root.resolve()),
        CODEX_SEATS_PROTECTED_HOMES=json.dumps(
            [str(path) for path in configured_seat_home_paths(root, settings)],
            separators=(",", ":"),
        ),
        CODEX_SEATS_PROTECTED_PATHS=json.dumps(
            request.get("protected_paths", []), separators=(",", ":")
        ),
        CODEX_SEATS_RELEASE_ID=str(request.get("release_id") or ""),
        CODEX_SEATS_DISPATCH_PROTOCOL=str(request.get("protocol_version") or ""),
        TMPDIR=str(runtime),
        TMP=str(runtime),
        TEMP=str(runtime),
    )
    # The endpoint is explicitly configured for no-auth local inference.  Add
    # the documented dummy only after the common secret-key rejection has built
    # a scrubbed allowlist; no inherited credential is ever consulted.
    env["OPENAI_API_KEY"] = "local-no-auth"
    # Qwen resolves custom profiles as .qwen/sandbox-macos-$NAME.sb below its
    # launch cwd and applies the profile to itself and all tool children.
    env["SEATBELT_PROFILE"] = "codex-seats"
    return env


def _resolve_qwen(command: str) -> str:
    if "/" in command:
        candidate = Path(command)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    else:
        found = shutil.which(command)
        if found:
            return os.path.realpath(found)
    raise FileNotFoundError("configured Qwen executable not found")


def _count(value: Any, field: str, *, required: bool = False) -> int:
    if value is None and not required:
        return 0
    if type(value) is not int or value < 0:
        raise LocalOutputError(f"Qwen result {field} must be a nonnegative integer")
    return value


def _structured_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(RESULT_SCHEMA["required"]):
        raise LocalOutputError("Qwen result has an invalid structured_result shape")
    if value.get("status") not in {"completed", "failed", "blocked_needs_controller"}:
        raise LocalOutputError("Qwen structured_result has an invalid status")
    if not isinstance(value.get("summary"), str):
        raise LocalOutputError("Qwen structured_result summary must be a string")
    for field in ("changed_files", "test_evidence", "warnings"):
        items = value.get(field)
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise LocalOutputError(f"Qwen structured_result {field} must contain strings")
    needs = value.get("needs_controller")
    if needs is not None and not isinstance(needs, str):
        raise LocalOutputError("Qwen structured_result needs_controller is invalid")
    return dict(value)


def _parse_qwen_events(payload: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Decode bounded JSONL and retain only safe progress fields for telemetry."""
    def reject_constant(_value: str) -> None:
        raise ValueError("non-JSON numeric constant")

    try:
        # Qwen delimits frames with LF bytes. str.splitlines() also splits on
        # Unicode separators that JSON.stringify may leave inside strings.
        lines = payload.split(b"\n")
        if lines[-1] == b"":
            lines.pop()
        document = [
            json.loads(line.decode("utf-8"), parse_constant=reject_constant)
            for line in lines
        ]
    except (UnicodeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise LocalOutputError("Qwen emitted malformed JSONL output") from exc
    if any(
        not isinstance(event, dict)
        or not isinstance(event.get("type"), str)
        or not event["type"]
        for event in document
    ):
        raise LocalOutputError("Qwen emitted malformed JSONL event")
    names: set[str] = set()
    tool_counts: dict[str, int] = {}
    last_tools: list[str] = []
    tool_calls = 0
    tool_errors = 0
    for event in document:
        kind = event["type"]
        if kind == "stream_event" and not isinstance(event.get("event"), dict):
            raise LocalOutputError("Qwen emitted malformed JSONL event")
        if kind not in {"assistant", "user"}:
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            raise LocalOutputError("Qwen emitted malformed JSONL event")
        for block in content:
            if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                raise LocalOutputError("Qwen emitted malformed JSONL event")
            if kind == "user" and block["type"] == "tool_result":
                tool_errors += block.get("is_error") is True
                continue
            if kind != "assistant" or block["type"] != "tool_use":
                continue
            tool_calls += 1
            name = block.get("name")
            if not isinstance(name, str) or not name:
                raise LocalOutputError("Qwen emitted malformed JSONL event")
            if re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", name):
                names.add(name)
                tool_counts[name] = tool_counts.get(name, 0) + 1
                last_tools = (last_tools + [name])[-8:]
    return document, {
        "qwen_event_count": len(document),
        "qwen_tool_call_count": tool_calls,
        "qwen_tool_names": sorted(names)[:32],
        "qwen_tool_call_counts": dict(sorted(tool_counts.items())[:32]),
        "qwen_last_tool_names": last_tools,
        "qwen_tool_error_count": tool_errors,
    }


def parse_qwen_result(
    document: list[dict[str, Any]], *, returncode: int, max_turns: int
) -> tuple[dict[str, Any], dict[str, int], int, int, str]:
    """Validate the terminal event without weakening structured result or usage checks."""
    results = [
        item
        for item in document
        if isinstance(item, dict) and item.get("type") == "result"
    ]
    if len(results) != 1 or document[-1] is not results[0]:
        if returncode == 53 and not results:
            raise LocalOutputError(
                "Qwen exceeded max-session-turns (exit code 53) before final result"
            )
        raise LocalOutputError("Qwen output must end with exactly one result message")
    result = results[0]
    subtype = result.get("subtype")
    is_error = result.get("is_error")
    if not isinstance(subtype, str) or type(is_error) is not bool:
        raise LocalOutputError("Qwen result is missing subtype/is_error")
    duration_ms = _count(result.get("duration_ms"), "duration_ms", required=True)
    num_turns = _count(result.get("num_turns"), "num_turns", required=True)
    if num_turns > max_turns:
        raise LocalOutputError("Qwen result exceeded the configured turn limit")
    usage_value = result.get("usage")
    if not isinstance(usage_value, dict):
        raise LocalOutputError("Qwen result usage must be an object")
    usage = {
        "input_tokens": _count(usage_value.get("input_tokens"), "usage.input_tokens", required=True),
        "output_tokens": _count(usage_value.get("output_tokens"), "usage.output_tokens", required=True),
        "cache_read_input_tokens": _count(
            usage_value.get("cache_read_input_tokens"),
            "usage.cache_read_input_tokens",
        ),
        "cache_creation_input_tokens": _count(
            usage_value.get("cache_creation_input_tokens"),
            "usage.cache_creation_input_tokens",
        ),
    }
    cached = usage["cache_read_input_tokens"] + usage["cache_creation_input_tokens"]
    if cached > usage["input_tokens"]:
        raise LocalOutputError("Qwen cached input exceeds total input usage")
    provider_total = usage["input_tokens"] + usage["output_tokens"]
    total_value = usage_value.get("total_tokens")
    usage["reported_total_tokens"] = (
        provider_total
        if total_value is None
        else _count(total_value, "usage.total_tokens", required=True)
    )
    if usage["reported_total_tokens"] != provider_total:
        raise LocalOutputError("Qwen result total_tokens contradicts input/output usage")
    usage["total_tokens"] = provider_total

    success = subtype == "success" and is_error is False
    failure = subtype.startswith("error_") and is_error is True
    if not success and not failure:
        raise LocalOutputError("Qwen result subtype and is_error contradict each other")
    if success and returncode != 0:
        raise LocalOutputError("Qwen reported success with a nonzero exit status")
    if failure and returncode == 0:
        raise LocalOutputError("Qwen reported an error with a zero exit status")
    if failure:
        raise LocalOutputError("Qwen execution failed with a final error result")

    structured = _structured_result(result.get("structured_result"))
    rendered = result.get("result")
    if not isinstance(rendered, str):
        raise LocalOutputError("Qwen success result is missing its rendered result")
    try:
        if json.loads(rendered) != structured:
            raise LocalOutputError("Qwen result and structured_result contradict each other")
    except (ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise LocalOutputError("Qwen rendered result is not the structured result") from exc
    return structured, usage, duration_ms, num_turns, rendered


def _safe_job_id(request: Mapping[str, Any], seat_id: str) -> str:
    identity = request.get("attempt_id") or request.get("request_id")
    if isinstance(identity, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", identity):
        raw = identity
    else:
        raw = f"{int(now())}-{os.getpid()}"
    return f"local-{seat_id}-{raw}"


def _write_log(path: Path, stdout: bytes, stderr: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = stdout
    if stderr:
        payload += b"\n-- qwen stderr --\n" + stderr
    path.write_bytes(payload)
    os.chmod(path, 0o600)


def run_local(
    root: Path,
    settings: dict[str, Any],
    request: dict[str, Any],
    *,
    access_decision: WorkerAccessDecision | None = None,
) -> RunResult:
    """Run Qwen on one explicit local seat without provider fallback."""
    prompt_arg = request["prompt"]
    prompt, label = normalize_prompt(prompt_arg, request.get("label"))
    seat_id = request.get("seat")
    if not isinstance(seat_id, str) or LOCAL_ID_RE.fullmatch(seat_id) is None:
        return _prestart_failure(
            label, None, "local execution requires an explicitly selected local seat", code=2
        )

    # A caller that already knows the global circuit breaker is off must not
    # trigger settings reload, probe/cache access, or executable discovery.
    if settings.get("providers", {}).get("local", {}).get("mode", "off") == "off":
        return _disabled(label, seat_id, "local provider is hard-off")

    try:
        settings = _reload_settings(root, settings)
        validate_local_registries(settings)
    except (OSError, ValueError, TypeError) as exc:
        return _prestart_failure(
            label,
            seat_id,
            f"invalid local execution settings: {exc}",
            status="blocked_needs_controller",
            code=2,
        )
    if settings.get("providers", {}).get("local", {}).get("mode", "off") == "off":
        return _disabled(label, seat_id, "local provider became hard-off")
    seat = settings.get("local_seats", {}).get(seat_id)
    if not isinstance(seat, dict):
        return _prestart_failure(label, seat_id, "selected local seat is not registered")
    if local_seats.effective_mode(settings, seat) == "off":
        return _disabled(label, seat_id, "selected local seat is off")
    try:
        access = access_decision or resolve_worker_access(settings, request)
    except DevelopmentPolicyError as exc:
        return _prestart_failure(
            label,
            seat_id,
            f"invalid worker access setting: {exc}",
            status="blocked_needs_controller",
            code=2,
        )
    if access.trusted:
        access = WorkerAccessDecision(
            access.configured,
            "restricted",
            "local Qwen always requires the managed OS sandbox",
        )

    profile = resolve_task_policy(
        settings,
        request.get("kind", "implementation"),
        size=request.get("size", "auto"),
        capability=request.get("capability_tier", "auto"),
        task_shape=request.get("task_shape"),
        validation=request.get("validation"),
    )
    task_shape = profile.task_shape
    validation = profile.validation
    if seat.get("lifecycle") != "active" or seat.get("harness") != "qwen":
        return _prestart_failure(
            label, seat_id, "selected local seat lifecycle or harness is ineligible"
        )
    if not local_seats.capability_matches(seat, task_shape, validation):
        return _prestart_failure(label, seat_id, "selected local seat capability mismatch")
    model = seat["model_profile"]
    requested_model = request.get("model")
    if requested_model is not None and requested_model != model:
        return _prestart_failure(label, seat_id, "requested model does not match local seat profile")
    host_id = seat["host"]
    host = settings["local_hosts"][host_id]
    base_url = host.get("base_url")
    if not isinstance(base_url, str):
        return _prestart_failure(label, seat_id, "local host has no validated base_url")

    availability = local_seats.observe_host(root, settings, host_id)
    decision = local_seats.eligibility(
        settings,
        seat_id,
        availability,
        task_shape=task_shape,
        validation=validation,
    )
    if not decision["eligible"]:
        return _prestart_failure(
            label,
            seat_id,
            f"local seat unavailable: {decision['state']} ({decision['reason']})",
        )

    job_id = _safe_job_id(request, seat_id)
    output = root / "logs" / f"{job_id}.qwen.json"
    metadata_path = root / "logs" / f"{job_id}.local-metadata.json"
    structured: dict[str, Any] | None = None
    usage: dict[str, int] | None = None
    progress: dict[str, Any] = {
        "qwen_event_count": 0,
        "qwen_tool_call_count": 0,
        "qwen_tool_names": [],
        "qwen_tool_call_counts": {},
        "qwen_last_tool_names": [],
        "qwen_tool_error_count": 0,
    }
    qwen_duration_ms = 0
    num_turns: int | None = None
    qwen_exit_code: int | None = None
    duration = 0
    status = "failed"
    message = "local Qwen execution did not start"
    fallback_safe = True
    launch_directory: Path | None = None
    boundary = None
    lease_lost = False

    with local_seats.claim(
        root,
        settings,
        seat_id,
        availability,
        task_shape=task_shape,
        validation=validation,
    ) as claim:
        if not claim["acquired"]:
            return _prestart_failure(
                label,
                seat_id,
                f"local seat unavailable: {claim['state']} ({claim['reason']})",
            )
        started_at = now()
        try:
            declared_scopes = list(request.get("write_scope", []))
            external_scopes = list(request.get("external_write_scope", []))
            if request.get("sandbox") == "read-only":
                declared_scopes = []
                external_scopes = []
            elif not declared_scopes:
                declared_scopes = [{"path": ".", "type": "tree"}]
            repository = Path(request.get("cd") or os.getcwd()).expanduser().resolve()
            boundary = boundary_plan(
                repository,
                declared_scopes,
                external_scopes,
                strict_scope=request.get("strict_scope") is True,
                parallel_safe=request.get("parallel_safe") is True,
                allow_git_write=request.get("allow_git_write") is True,
            )
            scopes = list(boundary.effective_scope)
            mcp_servers = resolved_worker_mcp_specs(request.get("mcp", []), repository)
            binary = _resolve_qwen(settings["providers"]["local"].get("command", "qwen"))
            if sys.platform != "darwin" or shutil.which("sandbox-exec") is None:
                raise RuntimeError(
                    "restricted local Qwen execution requires macOS sandbox-exec"
                )
            launch_directory = create_worker_launch_directory(root, job_id)
            qwen_home = launch_directory / "qwen-home"
            qwen_home.mkdir(mode=0o700)
            qwen_runtime = launch_directory / "qwen-runtime"
            qwen_runtime.mkdir(mode=0o700)
            settings_path = qwen_home / "settings.json"
            schema_path = launch_directory / "result-schema.json"
            mcp_path = launch_directory / "mcp.json"
            atomic_write_json(
                settings_path,
                _qwen_settings(
                    mcp_servers, model=model, base_url=base_url,
                    context_window_size=seat.get("context_window_size"),
                ),
                mode=0o600,
            )
            atomic_write_json(schema_path, RESULT_SCHEMA, mode=0o600)
            atomic_write_json(mcp_path, {"mcpServers": mcp_servers}, mode=0o600)
            _write_seatbelt_profile(
                launch_directory,
                repository,
                scopes,
                external_scopes,
                root=root,
                settings=settings,
                boundary=boundary,
                protected_paths=list(request.get("protected_paths", [])),
            )
            # Do not run from the repository: that prevents project Qwen config,
            # skills, plugins, and MCP from joining this controller-owned run.
            # Explicit write roots remain visible through --include-directories.
            include_paths = [str(repository)]
            include_paths.extend(
                str(path if kind == "tree" else path.parent)
                for path, kind in resolved_scope_paths(
                    external_scopes, repository, external=True
                )
            )
            include_paths = list(dict.fromkeys(include_paths))
            if len(include_paths) > 5:
                raise ValueError("Qwen supports the repository plus at most four external roots")
            excluded = _excluded_tools(writable=bool(scopes or external_scopes))
            allowed_mcp = sorted(mcp_servers) or [f"codex-seats-none-{os.getpid()}"]
            command = [
                binary,
                "--auth-type",
                "openai",
                "--openai-base-url",
                base_url,
                "--model",
                model,
                "--output-format",
                "stream-json",
                "--json-schema",
                f"@{schema_path}",
                "--max-session-turns",
                str(settings["providers"]["local"].get("max_turns", 100)),
                "--approval-mode",
                "yolo",
                "--exclude-tools",
                ",".join(excluded),
                "--chat-recording=false",
                "--mcp-config",
                str(mcp_path),
                "--allowed-mcp-server-names",
                ",".join(allowed_mcp),
                "--disabled-slash-commands",
                ",".join(_DISABLED_SLASH_COMMANDS),
            ]
            command.extend(["--include-directories", ",".join(include_paths)])
            command.extend(
                ["--prompt", managed_prompt(f"Repository root: {repository}\n\n{prompt}")]
            )
            env = _qwen_env(
                launch_directory,
                repository,
                root,
                settings,
                request,
                access,
                scopes,
                declared_scopes,
                external_scopes,
            )
            with local_lease.hold(host.get("lease_url")) as host_lease:
                try:
                    process_result = bounded_process.run(
                        command,
                        timeout=int(request.get("timeout", DEFAULT_TIMEOUT)),
                        cwd=launch_directory,
                        env=env,
                        max_output_bytes=MAX_QWEN_OUTPUT_BYTES,
                        cancel_event=host_lease.lost if host_lease is not None else None,
                    )
                except bounded_process.ProcessLaunchError:
                    raise
                except BaseException:
                    lease_lost = bool(host_lease and host_lease.lost.is_set())
                    # Any other exception crossed a successfully launched child
                    # (or conservatively cannot prove otherwise); fallback in
                    # the dirty worktree is unsafe.
                    fallback_safe = False
                    raise
            if (
                process_result.returncode == 71
                and not process_result.stdout
                and b"sandbox" in process_result.stderr.lower()
            ):
                _write_log(output, process_result.stdout, process_result.stderr)
                raise LocalSandboxUnavailable(
                    "local OS sandbox could not start before Qwen execution"
                )
            fallback_safe = False
            _write_log(output, process_result.stdout, process_result.stderr)
            qwen_exit_code = process_result.returncode
            events, progress = _parse_qwen_events(process_result.stdout)
            max_turns = int(settings["providers"]["local"].get("max_turns", 100))
            structured, usage, qwen_duration_ms, num_turns, _rendered = parse_qwen_result(
                events, returncode=process_result.returncode, max_turns=max_turns
            )
            status = structured["status"]
            message = structured["summary"][-500:]
        except bounded_process.ProcessCancelledError as exc:
            if lease_lost:
                status = "preempted"
                message = "local host admission lease was lost"
            else:
                status, message = "cancelled", str(exc)
            fallback_safe = False
        except local_lease.LocalLeaseUnavailable as exc:
            status, message, fallback_safe = "unavailable", str(exc), True
        except LocalSandboxUnavailable as exc:
            status, message, fallback_safe = "unavailable", str(exc), True
        except bounded_process.ProcessTimeoutError as exc:
            status, message, fallback_safe = "timeout", "local Qwen execution timed out", False
            _write_log(output, exc.result.stdout, exc.result.stderr)
        except bounded_process.ProcessOutputLimitError as exc:
            status, message, fallback_safe = "failed", str(exc), False
        except (bounded_process.ProcessError, FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            status, message = "failed", str(exc)
        finally:
            duration = round(now() - started_at)
            if launch_directory is not None:
                shutil.rmtree(launch_directory, ignore_errors=True)

    if not fallback_safe and status in {"failed", "timeout", "cancelled"}:
        ending_availability = local_seats.probe_host(root, settings, host_id)
        if ending_availability.get("state") == "gaming":
            status = "preempted"
            message = "local execution was preempted when the host entered gaming mode"

    structured = with_release_metadata(
        structured, request.get("release_id"), request.get("protocol_version")
    )
    if isinstance(structured, dict):
        structured.pop("filesystem_policy", None)
        structured["worker_access"] = access.effective
    metadata = {
        "ts": iso(),
        "provider": "local",
        "task_id": request.get("task_id"),
        "attempt_id": request.get("attempt_id") or request.get("request_id"),
        "request_id": request.get("request_id") or request.get("attempt_id"),
        "review_cycle": request.get("review_cycle"),
        "local_seat": seat_id,
        "host": host_id,
        "resource_group": seat["resource_group"],
        "model_profile": model,
        "runtime_fingerprint": seat["runtime_fingerprint"],
        "local_policy_version": LOCAL_POLICY_VERSION,
        "worker_access": access.effective,
        "write_boundary_mode": boundary.mode if boundary else "unresolved",
        "status": status,
        "fallback_safe": fallback_safe,
        "secs": duration,
        "qwen_duration_ms": qwen_duration_ms,
        "qwen_num_turns": num_turns,
        "qwen_exit_code": qwen_exit_code,
        **progress,
        "local_usage_available": usage is not None,
        "local_input_tokens": usage["input_tokens"] if usage else None,
        "local_output_tokens": usage["output_tokens"] if usage else None,
        "local_cache_read_input_tokens": usage["cache_read_input_tokens"]
        if usage
        else None,
        "local_cache_creation_input_tokens": usage["cache_creation_input_tokens"]
        if usage
        else None,
        "local_total_tokens": usage["total_tokens"] if usage else None,
        "local_reported_total_tokens": usage["reported_total_tokens"]
        if usage
        else None,
        "log": str(output) if output.exists() else None,
        "structured_result": structured,
    }
    try:
        atomic_write_json(metadata_path, metadata, mode=0o600)
    except OSError:
        # Execution result remains authoritative; failure to persist optional
        # telemetry must not manufacture a retry-safe provider failure.
        pass
    cached_tokens = (
        usage["cache_read_input_tokens"] + usage["cache_creation_input_tokens"]
        if usage
        else None
    )
    uncached_tokens = usage["input_tokens"] - cached_tokens if usage else None
    with Ledger(root) as data:
        data["jobs"].append(
            {
                "telemetry_schema": JOB_TELEMETRY_SCHEMA if usage else None,
                "telemetry_classification": "current" if usage else "usage-unavailable",
                "release_id": request.get("release_id"),
                "protocol_version": request.get("protocol_version"),
                "id": job_id,
                "campaign_run_id": os.environ.get("CODEX_SEATS_BATCH_ID"),
                "ts": iso(),
                "provider": "local",
                "seat": seat_id,
                "host": host_id,
                "resource_group": seat["resource_group"],
                "label": label,
                "kind": request.get("kind", "implementation"),
                "size": profile.size,
                "task_shape": task_shape,
                "validation": validation,
                "task_id": request.get("task_id"),
                "attempt_id": request.get("attempt_id") or request.get("request_id"),
                "review_cycle": request.get("review_cycle"),
                "model": model,
                "model_profile": model,
                "runtime_fingerprint": seat["runtime_fingerprint"],
                "local_policy_version": LOCAL_POLICY_VERSION,
                **(
                    {
                        "tokens": usage["total_tokens"],
                        "raw_tokens": usage["total_tokens"],
                        "cached_tokens": cached_tokens,
                        "uncached_tokens": uncached_tokens,
                    }
                    if usage
                    else {}
                ),
                "input_tokens": usage["input_tokens"] if usage else None,
                "cached_input_tokens": cached_tokens,
                "uncached_input_tokens": uncached_tokens,
                "output_tokens": usage["output_tokens"] if usage else None,
                "reasoning_tokens": 0 if usage else None,
                "provider_tokens": usage["total_tokens"] if usage else None,
                "local_reported_total_tokens": usage["reported_total_tokens"]
                if usage
                else None,
                "local_usage_available": usage is not None,
                **progress,
                "predicted_minutes": request.get("estimated_minutes"),
                "actual_minutes": duration / 60,
                "route_reason": request.get("route_reason"),
                "route_provider": request.get("route_provider"),
                "fallback_count": max(0, int(request.get("_fallback_count", 0) or 0)),
                "write_boundary_mode": boundary.mode if boundary else "unresolved",
                "strict_scope": request.get("strict_scope") is True,
                "worker_access": access.effective,
                "status": status,
                "secs": duration,
                "qwen_duration_ms": qwen_duration_ms,
                "qwen_num_turns": num_turns,
                "qwen_exit_code": qwen_exit_code,
                "log": str(output) if output.exists() else None,
                "structured_result": structured,
            }
        )
        trim_jobs_with_archive(data, root)
    result_status = "unavailable" if status == "preempted" else status
    workers._maybe_notify(settings, label, result_status, seat_id, duration)
    return RunResult(
        0
        if status == "completed"
        else (5 if status == "blocked_needs_controller" else (3 if status == "preempted" else 1)),
        result_status,
        "local",
        seat_id,
        label,
        duration,
        usage["total_tokens"] if usage else None,
        str(output) if output.exists() else None,
        message[-500:],
        fallback_safe,
        model,
        structured,
    )
