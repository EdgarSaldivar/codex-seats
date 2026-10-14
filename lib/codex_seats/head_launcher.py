"""Launch an interactive head (Codex or Claude) wired for dispatch-based delegation.

The Codex head defaults to the registered **primary** seat, whose registry ``home`` may be
``~/.codex`` so the terminal head and the phone/Remote head are one account. A terminal launch may instead
select another authenticated pool seat when the primary is low on budget. It exports
``CODEX_CONTROL_SEAT`` so ``codex-seat delegate`` stamps ``origin.seat`` and the runner
holds the selected seat out of ordinary worker selection. The controller may reuse that
identity only for the final isolated managed-worker fallback.

Command/env construction is pure and unit-tested; :func:`launch` performs the ``exec``.
The default terminal head pins Codex's official ``:danger-full-access`` built-in, matching
the primary config used by Remote. ``--safe`` retains the explicitly contained user-defined
``head`` profile.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from . import bounded_process, workers
from .seat_registry import active_seat_ids
from .state import runtime_seat_home
from .policy_settings import policy_report


REMOTE_COMPLETION_GUIDANCE = (
    "- In an owning Remote Codex session with exact CODEX_THREAD_ID and CODEX_HOME "
    "or CODEX_SESSION_ID, "
    "use `codex-seat delegate --detach` for independent units and arm one grouped "
    "`codex-seat await <job-id>... --wake-head` with an explicit --wait-timeout at "
    "the dependency boundary. For one unit, `delegate --wake-head` combines submission "
    "and arming. Continue independent work, then allow the turn to end. The receipt "
    "watcher queues a capacity advisory and a terminal or deadline message to the "
    "owning conversation. On a capacity wake inspect that exact job once and keep "
    "waiting or cancel it before reassigning. On terminal wake read the exact receipts "
    "once with `codex-seat await <job-id>... --wait-timeout 0 --json`. Never poll, "
    "resubmit live work, or treat uncertain queue delivery or a deadline as permission "
    "to repeat work. If arming fails retain job IDs and inspect durable wake evidence."
)


def render_head_policy(settings: dict[str, Any], *, codex: bool = False) -> str:
    """Render delegation and review preferences for an interactive head."""
    policy = policy_report(settings)
    delegation = policy["execution.delegation_policy"]
    if delegation == "always":
        direction = "Delegate every implementation unit through `codex-seat delegate`."
    elif delegation == "manual":
        direction = (
            "Delegate through `codex-seat delegate` when the user requests it; "
            "otherwise work inline within your permissions."
        )
    else:
        direction = (
            f"Delegate implementation estimated above {policy['execution.inline_max_minutes']} "
            "minutes through `codex-seat delegate`; smaller work may be done inline "
            "within your permissions."
        )
    lines = [
        "Codex Seats orchestration policy:",
        "- You are the orchestration head and final acceptor.",
        f"- {direction}",
        "- Managed worker authoring occurs only in isolated delegated calls; "
        "managed workers must not delegate again.",
        "- Never invoke provider-native subagents to bypass managed worker boundaries.",
        "- Classify the dominant intellectual work and add real risk flags; use "
        "--kind docs for documenting decided changes, --kind security for security "
        "analysis, --kind ui_ux --risk ui for interaction work, and --kind research "
        "or deep_research for bounded or open-ended research.",
        "- The runner selects provider model and effort. Use --tier routine|complex|frontier, "
        "--size small|big, --effort low|medium|high|xhigh|max|ultra and a concise "
        "--override-reason only when the default is insufficient.",
        "- For independent delegated units use `codex-seat delegate --detach`; "
        "continue useful work, then call `codex-seat await` once with all job IDs "
        "at a dependency boundary. Do not wait idly for quota reset.",
        "- Inspect every receipt, diff, test result, and warning. Apply a verified "
        "ChangeSet only after acceptance; you remain the final acceptor.",
        "- Review mode governs automatic goal supervision only. `delegate`, "
        "`await`, and ChangeSet apply never review on their own; run "
        "`codex-seat review` only when a review is explicitly requested "
        f"(default strategy: {policy['review.strategy']}).",
        "- An explicit goal review command is a blocking gate even if review mode "
        "is off.",
    ]
    if policy["review.mode"] != "off":
        lines.append(
            f"- Goal supervision of a goal with an empty review field runs an "
            f"automatic {policy['review.strategy']} review that is "
            f"{policy['review.mode']}."
        )
    if policy["execution.head_worker_fallback"]:
        lines.append(
            "- If ordinary worker capacity is unavailable, the controller may use "
            "its active head identity for a fresh isolated managed worker call. "
            "That worker authors the change."
        )
    if codex:
        lines.append(REMOTE_COMPLETION_GUIDANCE)
    return "\n".join(lines) + "\n"


CODEX_DAEMON_VERSION_TIMEOUT_SECONDS = 10.0


class CodexDaemonError(RuntimeError):
    """The shared Codex daemon is unavailable, stopped, or stale."""


def codex_daemon_versions(
    binary: str,
    home: str | Path,
    *,
    run: Any = bounded_process.run,
) -> dict[str, str]:
    """Return the official CLI/managed/running daemon version report."""
    argv = [binary, "app-server", "daemon", "version"]
    try:
        result = run(
            argv,
            timeout=CODEX_DAEMON_VERSION_TIMEOUT_SECONDS,
            env={**os.environ, "CODEX_HOME": str(Path(home).expanduser())},
        )
    except bounded_process.ProcessError as exc:
        raise CodexDaemonError(f"cannot inspect the shared Codex daemon: {exc}") from exc
    if result.returncode:
        detail = result.stderr_text(errors="replace").strip()
        raise CodexDaemonError(
            "cannot inspect the shared Codex daemon: "
            + (detail or f"exit {result.returncode}")
        )
    try:
        payload = json.loads(result.stdout_text())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodexDaemonError(
            "Codex daemon version returned malformed JSON"
        ) from exc
    required = ("status", "cliVersion", "managedCodexVersion", "appServerVersion")
    if not isinstance(payload, dict) or any(
        not isinstance(payload.get(key), str) or not payload[key]
        for key in required
    ):
        raise CodexDaemonError("Codex daemon version report is incomplete")
    return {key: payload[key] for key in required}


def check_codex_daemon(
    binary: str,
    home: str | Path,
    *,
    run: Any = bounded_process.run,
) -> dict[str, str]:
    """Read-only check that the shared daemon is running and version-consistent.

    The launcher never updates or restarts the shared daemon: an upstream update
    restarts it and disconnects every attached client, including Remote. A stopped
    or stale daemon is refused with explicit operator guidance instead.
    """
    versions = codex_daemon_versions(binary, home, run=run)
    cli_version = versions["cliVersion"]
    managed_version = versions["managedCodexVersion"]
    server_version = versions["appServerVersion"]
    if (
        versions["status"] == "running"
        and cli_version == managed_version == server_version
    ):
        return versions
    state = (
        "stale" if versions["status"] == "running"
        else f"not running (status {versions['status']})"
    )
    raise CodexDaemonError(
        f"the shared Codex daemon is {state}: CLI {cli_version}, managed "
        f"{managed_version}, app-server {server_version}. codex-seat does not update "
        "or restart it. From a trusted terminal, after checking that no attached "
        "Remote or terminal session will be interrupted, run `codex app-server "
        "daemon update` (it restarts the shared daemon) or restart Remote from the "
        "ChatGPT desktop app, then rerun `codex-seat head codex`."
    )


def launcher_diagnostics(root: str | Path, settings: dict[str, Any]) -> dict[str, Any]:
    """Identify the launcher snapshot without implying provider-side reloads."""
    from .runtime_readers import build_metadata

    return build_metadata(
        "head-launcher",
        settings,
        root=root,
        process_identity=(f"{os.getpid():032x}"[-32:]),
        module_file=__file__,
    )


def _launcher_environment(root: str | Path, settings: dict[str, Any]) -> dict[str, str]:
    diagnostics = launcher_diagnostics(root, settings)
    release = diagnostics["release"]
    supported = diagnostics["supported_settings"]
    return {
        "CODEX_SEATS_LAUNCHER_DIAGNOSTICS": json.dumps(
            diagnostics, sort_keys=True, separators=(",", ":")
        ),
        "CODEX_SEATS_LAUNCHER_RELEASE_ID": release["id"] or "unpinned",
        "CODEX_SEATS_LAUNCHER_SETTINGS_MIN": str(supported["min"]),
        "CODEX_SEATS_LAUNCHER_SETTINGS_MAX": str(supported["max"]),
    }


def _primary_seat(settings: dict[str, Any]) -> str:
    primary = settings.get("pool", {}).get("primary")
    if primary is None:
        raise ValueError("cannot launch a Codex head: the seat registry has no primary")
    if primary not in set(active_seat_ids(settings)):
        raise ValueError(
            f"cannot launch a Codex head: configured primary {primary!r} is not active"
        )
    return primary


def _head_seat(settings: dict[str, Any], selected: int | None) -> str:
    if selected is None:
        return _primary_seat(settings)
    seat = f"seat{selected}"
    if seat not in set(active_seat_ids(settings)):
        registered = settings.get("pool", {}).get("seats", {})
        if seat in registered:
            lifecycle = registered[seat].get("lifecycle", "unknown")
            raise ValueError(
                f"cannot launch a Codex head on {seat}: lifecycle is {lifecycle}, not active"
            )
        raise ValueError(f"unknown registered Codex seat: {seat}")
    return seat


def validate_safe_head_profile(home: str | Path) -> None:
    """Fail fast unless the seat config defines a usable ``head`` permission profile.

    A safe head pins ``default_permissions="head"``. Without a ``[permissions.head]``
    profile carrying a filesystem table, Codex would not run the contained head the
    operator asked for, so the launch is refused instead of falling back. Only
    ``config.toml`` is parsed; credential files are never opened.
    """
    import tomllib

    config = Path(home).expanduser() / "config.toml"
    guidance = (
        f"define [permissions.head] with a non-empty [permissions.head.filesystem] "
        f"table in {config}, or launch without --safe and set "
        "execution.head_permissions to full-access"
    )
    try:
        document = tomllib.loads(config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(
            f"cannot launch a safe Codex head: {config} does not exist; {guidance}"
        ) from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(
            f"cannot launch a safe Codex head: {config} is unreadable or invalid "
            f"TOML ({type(exc).__name__}); {guidance}"
        ) from None
    permissions = document.get("permissions")
    profile = permissions.get("head") if isinstance(permissions, dict) else None
    if not isinstance(profile, dict):
        raise ValueError(
            f"cannot launch a safe Codex head: {config} has no [permissions.head] "
            f"profile; {guidance}"
        )
    filesystem = profile.get("filesystem")
    if not isinstance(filesystem, dict) or not filesystem:
        raise ValueError(
            f"cannot launch a safe Codex head: the [permissions.head] profile in "
            f"{config} has no filesystem rules; {guidance}"
        )


def codex_head_command(
    root: str | Path,
    settings: dict[str, Any],
    *,
    seat: int | None = None,
    repo: str | None = None,
    safe: bool = False,
    binary: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Return ``(argv, env_overlay)`` for an interactive Codex head."""
    seat_id = _head_seat(settings, seat)
    home = runtime_seat_home(Path(root), seat_id, settings)
    mode = "safe" if safe else policy_report(settings)["execution.head_permissions"]
    safe = mode == "safe"
    if safe:
        validate_safe_head_profile(home)
    env = {
        "CODEX_HOME": str(home),
        "CODEX_CONTROL_SEAT": seat_id,
        "CODEX_SEATS_HEAD": "codex",
        "CODEX_SEATS_HEAD_MODE": mode,
        **_launcher_environment(root, settings),
    }
    argv = [binary or workers.resolve_executable("codex", None)]
    # CLI config overrides otherwise select an embedded server, which owns the
    # thread separately from Remote and causes an "active writer" conflict.
    # Only the primary identity can use the shared Remote controller identity;
    # other seats still need their explicit launcher environment for attribution.
    shared_remote = seat_id == settings["pool"].get("primary") and (
        home / "app-server-control" / "app-server-control.sock"
    ).is_socket()
    if shared_remote:
        argv += ["--remote", "unix://"]
    # Interactive heads must not inherit approval_policy="never" from synchronized
    # worker-oriented config.
    argv += [
        "--ask-for-approval",
        "on-request",
        "-c",
        'approvals_reviewer="user"',
        "-c",
        f"developer_instructions={json.dumps(render_head_policy(settings, codex=True))}",
        "-c",
        'default_permissions="head"' if safe else 'default_permissions=":danger-full-access"',
    ]
    if repo:
        argv += ["-C", str(Path(repo).expanduser())]
    elif shared_remote:
        # A remote client otherwise inherits the server's cwd, not this shell's.
        argv += ["-C", os.getcwd()]
    return argv, env


def claude_head_command(
    root: str | Path,
    settings: dict[str, Any],
    *,
    repo: str | None = None,
    binary: str | None = None,
) -> tuple[list[str], dict[str, str], str | None]:
    """Return ``(argv, env_overlay, cwd)`` for an interactive Claude head.

    Claude Code isn't under the Codex seatbelt, so it needs no profile flag here; it takes
    its working directory by being launched in it. ``CODEX_SEATS_HEAD=claude`` makes
    ``delegate`` stamp the right origin (and, having no codex seat, hold none out).
    """
    from .claude_isolation import claude_head_settings

    root_path = Path(root).expanduser()
    settings_file = root_path / ".claude-head-settings.json"
    settings_file.write_text(
        json.dumps(
            claude_head_settings(control_root=root_path, settings=settings), indent=2
        ) + "\n"
    )
    env = {
        "CODEX_SEATS_HEAD": "claude",
        **_launcher_environment(root, settings),
    }
    argv = [
        binary or "claude",
        "--settings",
        str(settings_file),
        "--append-system-prompt",
        render_head_policy(settings),
    ]
    cwd = str(Path(repo).expanduser()) if repo else None
    return argv, env, cwd


def launch(argv: list[str], env_overlay: dict[str, str], cwd: str | None = None) -> None:
    """Replace this process with the head. ``cwd`` (if given) is entered first."""
    if cwd:
        os.chdir(cwd)
    os.execvpe(argv[0], argv, {**os.environ, **env_overlay})


def cmd_head(args: Any, settings: dict[str, Any], root: str | Path) -> int:
    selected_seat = getattr(args, "seat", None)
    safe = bool(getattr(args, "safe", False))
    if args.head_kind == "codex":
        try:
            seat_id = _head_seat(settings, selected_seat)
            home = runtime_seat_home(Path(root), seat_id, settings)
            binary = workers.resolve_executable("codex", None)
            shared_remote = seat_id == settings["pool"].get("primary") and (
                home / "app-server-control" / "app-server-control.sock"
            ).is_socket()
            if shared_remote:
                check_codex_daemon(binary, home)
            argv, env = codex_head_command(
                root,
                settings,
                seat=selected_seat,
                repo=args.cd,
                safe=safe,
                binary=binary,
            )
        except (CodexDaemonError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        launch(argv, env)
    else:
        if selected_seat is not None:
            print("--seat is only valid for a Codex head", file=sys.stderr)
            return 2
        if safe:
            print("--safe is only valid for a Codex head", file=sys.stderr)
            return 2
        argv, env, cwd = claude_head_command(root, settings, repo=args.cd)
        launch(argv, env, cwd)
    return 0  # not reached: launch() execs
