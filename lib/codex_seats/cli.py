from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__, bounded_process
from .account import (
    AccountReadError, prepare_banked_reset_redemption, redeem_banked_reset,
    refresh_account_usage,
)
from .controller_context import current_controller
from .display import (
    dashboard,
    segment,
)
from .display_status import (
    resets_report,
    resets_status_json,
    status_json,
)
from .efforts import EFFORTS
from .policy_settings import DEFAULTS as POLICY_DEFAULTS
from .policy_settings import PolicyError, apply_policy, policy_report, preview_policy
from .state import (
    Ledger,
    PoolSnapshotError,
    claude_usage_age_seconds,
    claude_usage_fresh,
    effective_claude_mode,
    load_claude_rate_limits,
    load_settings,
    refresh_transients,
    runtime_seat_home,
    set_claude_mode,
    set_worker_access,
)
from .seat_registry import (
    SeatRegistryError,
    registered_seat_ids,
    resolve_readonly_selector,
    seat_number,
)
from .task_policy import TASK_KINDS
from .worker_config import sync_codex_config
from .workers import (
    codex_version,
    probe_seats,
    resolve_executable,
    terminate_claim,
)

ROOT = Path(os.environ.get("CODEX_SEATS_ROOT", Path.home() / ".codex-seats"))


def _operational_seat_ids(settings: dict[str, Any]) -> list[str]:
    return registered_seat_ids(settings, lifecycles=("active", "draining"))


def _selected_seat(
    selector: str | int | None,
    settings: dict[str, Any],
) -> str | None:
    """Resolve a strict seatN or numeric selector to a registered seat id."""
    if selector is None:
        return None
    try:
        return resolve_readonly_selector(str(selector).strip(), settings)
    except SeatRegistryError:
        return None


def _numeric_selected_seat(
    selector: str | int | None,
    settings: dict[str, Any],
) -> int | None:
    resolved = _selected_seat(selector, settings)
    if selector is not None and resolved is None:
        raise ValueError(f"unknown seat selector {selector!r}")
    if resolved is not None:
        lifecycle = settings["pool"]["seats"][resolved]["lifecycle"]
        if lifecycle != "active":
            raise ValueError(
                f"{resolved} is {lifecycle}; a forced control seat must be active"
            )
    return None if resolved is None else seat_number(resolved)


def _terminal_size() -> os.terminal_size:
    """Return current output columns and rows, including live resizes."""
    size = shutil.get_terminal_size(fallback=(120, 24))
    return os.terminal_size((max(1, size.columns), max(1, size.lines)))


_WATCH_VOLATILE_RE = re.compile(
    r"\b\d{1,2}:\d{2}:\d{2}(?= local)|"
    r"(?<=last ok )(?:—|\d+(?:\.\d+)?[smhd])|"
    r"(?<=next )(?:pending|after refresh|now|\d+(?:\.\d+)?[smhd])|"
    r"(?<![\w.])~?\d+(?:\.\d+)?(?:s|m(?:\d{2}s)?|h(?:\d{2}m)?|d(?:\d{2}h)?)(?!\w)"
)


def _captured_watch_signature(frame: str, refresh_snapshot: Any) -> tuple[str, Any, Any]:
    """Ignore clock-like churn while retaining operational state changes."""
    stable_frame = _WATCH_VOLATILE_RE.sub("<time>", frame)
    return (
        stable_frame,
        _snapshot_field(refresh_snapshot, "last_result"),
        _snapshot_field(refresh_snapshot, "last_succeeded"),
    )


def _snapshot_field(snapshot: Any, name: str) -> Any:
    if snapshot is None:
        return None
    if isinstance(snapshot, dict):
        return snapshot.get(name)
    return getattr(snapshot, name, None)


def cmd_pick_seat(args: argparse.Namespace, settings: dict[str, Any]) -> int:
    """Print the budget-optimal seat id for a task of ``--kind``/``--size`` (or ``-``
    if the pool is exhausted). A read-only primitive so out-of-process callers — the
    cross-model review gate, the spec red-team, an interactive launcher — get the SAME
    seat selection the build head gets, instead of hard-coding a seat or the default
    home. Does not claim the seat (reviews are short/read-only); exit 0 with a seat,
    exit 3 when the pool is exhausted so a caller can branch to a provider fallback.
    ``--json`` wraps the same answer in the agent envelope with the same exit codes."""
    from . import agent_json
    from .pool_view import read_snapshot
    from .seat_selection import pick_seat
    from .state import default_task_size

    kind = args.kind
    size = args.size or default_task_size(settings, kind)
    as_json = bool(getattr(args, "json", False))
    try:
        data = read_snapshot(ROOT, settings)
        seat = pick_seat(data["seats"], settings, size, set())
    except PoolSnapshotError as exc:
        if as_json:
            return agent_json.emit_failure("pool-unreadable", str(exc), 2)
        print(f"[codex-seat] {exc}", file=sys.stderr)
        return 2
    if as_json:
        answer = {"kind": kind, "size": size, "provider": "codex", "seat": seat}
        if seat is None:
            return agent_json.emit_failure(
                "exhausted",
                f"no active, idle Codex seat fits a {size} task; "
                "see `codex-seat capacity` for Claude/local capacity",
                3,
                data=answer,
            )
        label = settings["pool"]["seats"][seat]["label"]
        return agent_json.emit_success({**answer, "label": label})
    print(seat or "-")
    return 0 if seat else 3


def cmd_capacity(args: argparse.Namespace, settings: dict[str, Any], warnings: list[str]) -> int:
    """Where work can run now: every Codex seat, Claude, and local seat in one view.

    Read-only and offline: it reads cached usage, the live queue and the
    ``pick-seat`` selection, and never calls a provider. Use ``--json`` for the
    agent envelope; ``codex-seat seats refresh --all`` updates stale usage."""
    from . import agent_json
    from .pool_view import collect, render_text

    try:
        view = collect(ROOT, settings, settings_warnings=warnings)
    except PoolSnapshotError as exc:
        if args.json:
            return agent_json.emit_failure("pool-unreadable", str(exc), 2)
        print(f"codex-seat capacity: {exc}", file=sys.stderr)
        return 2
    if args.json:
        return agent_json.emit_success(view)
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    print(render_text(view, color=color, width=_terminal_size().columns if color else 200))
    return 0


def cmd_help(args: argparse.Namespace) -> int:
    from .agent_guide import AGENT_GUIDE

    del args
    print(AGENT_GUIDE, end="")
    return 0


def cmd_dash(args: argparse.Namespace, settings: dict[str, Any], warnings: list[str]) -> int:
    from .workers import TESTED_CODEX

    version, error = codex_version()
    version_warning = None
    if error:
        version_warning = f"WARNING: Codex executable unhealthy: {error}"
    elif version and TESTED_CODEX not in version:
        version_warning = f"WARNING: wrapper tested with Codex {TESTED_CODEX}; installed: {version}"
    output_is_tty = sys.stdout.isatty()
    color = not args.no_color and "NO_COLOR" not in os.environ and output_is_tty
    manage_direct = bool(getattr(args, "manage", False))
    watch_requested = bool(args.watch or manage_direct)
    if manage_direct:
        from .seat_manager import ManagerEligibilityError, ensure_interactive_terminal

        try:
            ensure_interactive_terminal()
        except ManagerEligibilityError as exc:
            print(f"dash --manage refused: {exc}", file=sys.stderr)
            return 2
    if not watch_requested:
        size = _terminal_size()
        print(
            dashboard(
                ROOT,
                settings,
                warnings,
                color=color,
                forensic=getattr(args, "forensic", False),
                width=size.columns,
            )
        )
        if version_warning:
            print(f"\n{version_warning}")
        return 0
    if args.interval <= 0:
        print("dash --interval must be greater than zero", file=sys.stderr)
        return 2
    if args.probe_interval <= 0:
        print("dash --probe-interval must be greater than zero", file=sys.stderr)
        return 2

    context = current_controller()
    declared_head = os.environ.get("CODEX_SEATS_HEAD", "").lower()
    excluded_context = (
        context.is_controller
        or bool(getattr(context, "managed_worker", False))
        or declared_head in {"codex", "claude"}
    )
    # A head's command pane gets the captured watch; an explicit --manage on a
    # real terminal is a human asking for the interactive manager, even there.
    captured = not output_is_tty or (excluded_context and not manage_direct)
    alternate_screen = not captured
    if captured:
        color = False
    if not captured and sys.stdin.isatty():
        return _run_interactive_dash(
            args,
            settings,
            warnings,
            version_warning=version_warning,
            color=color,
            direct=manage_direct,
        )
    refresher = None
    if not args.no_active_probe:
        from .watch import WatchRefresher

        refresher = WatchRefresher(ROOT, settings, args.probe_interval)
    from .display import DashboardViewState

    view_state = DashboardViewState()
    first_render = True
    captured_signature: tuple[str, Any, Any] | None = None
    # Route SIGTERM/SIGHUP through the same unwind as Ctrl-C so the `finally`
    # runs refresher.stop() and restores the terminal on a supervisor term.
    import signal

    def _interrupt(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    prev_signals: dict[int, Any] = {}
    for _sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            prev_signals[_sig] = signal.signal(_sig, _interrupt)
        except (ValueError, OSError):  # not main thread / unsupported platform
            pass
    try:
        if alternate_screen:
            sys.stdout.write("\033[?1049h")
            sys.stdout.flush()
        while True:
            settings_reader = getattr(args, "_settings_reader", None)
            if settings_reader is not None:
                settings_reader.heartbeat()
            size = _terminal_size()
            refresh_snapshot = refresher.snapshot() if refresher is not None else None
            watch_warnings = [*warnings, *([version_warning] if version_warning else [])]
            frame = dashboard(
                ROOT,
                settings,
                watch_warnings,
                color=color,
                forensic=getattr(args, "forensic", False),
                probe_interval=args.probe_interval,
                width=size.columns,
                height=size.lines,
                refresh_snapshot=refresh_snapshot,
                view_state=view_state,
            )
            if captured:
                signature = _captured_watch_signature(frame, refresh_snapshot)
                if captured_signature is None or signature != captured_signature:
                    sys.stdout.write(
                        ("" if captured_signature is None else "\n") + frame + "\n"
                    )
                    sys.stdout.flush()
                    captured_signature = signature
            else:
                # One buffered frame write prevents a controller or terminal
                # capture from observing a partially cleared dashboard.  J
                # erases only below the completed frame; no trailing newline
                # means an exactly full frame cannot scroll.
                sys.stdout.write("\033[H" + frame + "\033[J")
                sys.stdout.flush()
            # The cached dashboard above is deliberately visible before any
            # provider can block on authentication or an app-server.
            if first_render and refresher is not None:
                refresher.start()
            first_render = False
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        if refresher is not None:
            refresher.stop()
        for _sig, _prev in prev_signals.items():
            try:
                signal.signal(_sig, _prev)
            except (ValueError, OSError):
                pass
        if alternate_screen:
            sys.stdout.write("\033[?1049l")
            sys.stdout.flush()
    return 0


def _run_interactive_dash(
    args: argparse.Namespace,
    settings: dict[str, Any],
    warnings: list[str],
    *,
    version_warning: str | None,
    color: bool,
    direct: bool,
) -> int:
    """Run the keyboard-driven dashboard, including guarded login handoffs."""
    import signal
    import threading

    from . import seats_cli
    from .display import DashboardViewState
    from .seat_manager import (
        ManagerEligibilityError,
        ManagerError,
        SeatManager,
        ensure_interactive_terminal,
    )
    from .seat_manager_tui import ManagerUI, UIExit
    from .terminal import run_ui
    from .watch import WatchRefresher

    try:
        ensure_interactive_terminal()
    except ManagerEligibilityError as exc:
        # An ordinary watch in a head context is routed to captured mode
        # before this point. Reaching here means both streams claim a TTY.
        print(f"interactive seat management unavailable: {exc}", file=sys.stderr)
        return 2

    def new_refresher(current: dict[str, Any]) -> WatchRefresher:
        # Local-host probing runs configured probe argv, so it is attached only
        # here, behind the trusted interactive terminal check above. The
        # captured/controller watch never probes local hosts. --no-active-probe
        # clears ``periodic`` and therefore its scheduled probes as well.
        return WatchRefresher(
            ROOT,
            current,
            args.probe_interval,
            periodic=not args.no_active_probe,
            local_probes=True,
        )

    reader = getattr(args, "_settings_reader", None)

    def load_current() -> tuple[dict[str, Any], list[str]]:
        if reader is not None:
            current, current_warnings = reader.load(load_settings)
        else:
            current, current_warnings = load_settings(ROOT)
        warnings[:] = current_warnings
        return current, current_warnings

    refresher = new_refresher(settings)
    manager = SeatManager(
        ROOT, settings, refresher=refresher, settings_loader=load_current
    )
    ui = ManagerUI(manager, direct=direct, color=color)
    view_state = DashboardViewState()

    def overview(size: os.terminal_size) -> str:
        return dashboard(
            ROOT,
            manager.settings,
            [*warnings, *([version_warning] if version_warning else [])],
            color=color,
            forensic=getattr(args, "forensic", False),
            probe_interval=args.probe_interval,
            width=size.columns,
            height=max(1, size.lines - 1),
            refresh_snapshot=refresher.snapshot(),
            view_state=view_state,
        )

    def interrupt(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    previous: dict[int, Any] = {}
    for selected_signal in (signal.SIGTERM, signal.SIGHUP):
        try:
            previous[selected_signal] = signal.signal(selected_signal, interrupt)
        except (ValueError, OSError):
            pass

    code = 0
    try:
        while True:
            try:
                outcome = run_ui(
                    ui,
                    stdin=sys.stdin,
                    stdout=sys.stdout,
                    size=_terminal_size,
                    overview=overview,
                    redraw_interval=args.interval,
                    on_first_render=refresher.start,
                    heartbeat=reader.heartbeat if reader is not None else None,
                )
            except KeyboardInterrupt:
                outcome = UIExit("quit")
            if outcome.reason != "login" or outcome.login_seat_id is None:
                break

            # run_ui has restored termios and left the alternate screen before
            # this guarded handoff. No replacement is created unless the old
            # provider worker has actually stopped.
            if not refresher.stop(timeout=10.0):
                print(
                    "login refused: provider refresh did not quiesce within 10 seconds",
                    file=sys.stderr,
                )
                code = 2
                break
            manager.replace_refresher(None)
            heartbeat_stop = threading.Event()
            heartbeat_thread: threading.Thread | None = None
            if reader is not None:
                reader.heartbeat()
                def keep_heartbeat() -> None:
                    while not heartbeat_stop.wait(2.0):
                        try:
                            reader.heartbeat()
                        except OSError:
                            pass

                heartbeat_thread = threading.Thread(
                    target=keep_heartbeat,
                    name="seat-login-reader-heartbeat",
                    daemon=True,
                )
                heartbeat_thread.start()
            login_message: str
            try:
                if outcome.login_plan is None:
                    raise ManagerError(
                        "login confirmation snapshot is missing; select and confirm again"
                    )
                confirmed_settings = manager.validate_login_plan(outcome.login_plan)
                seats_cli._human_only_guard()
                seats_cli._trusted_terminal(
                    argparse.Namespace(from_current_user_request=True), "login"
                )
                seats_cli._run_login(
                    ROOT, confirmed_settings, outcome.login_plan.seat_id
                )
                login_message = f"Login returned for {outcome.login_seat_id}; cached state reloaded"
            except (ManagerError, seats_cli.SeatsCLIError, OSError, ValueError) as exc:
                login_message = f"Login failed: {exc}"
            finally:
                heartbeat_stop.set()
                if heartbeat_thread is not None:
                    heartbeat_thread.join(timeout=3.0)

            try:
                current, _new_warnings = load_current()
                manager.settings = current
                refresher = new_refresher(current)
                manager.replace_refresher(refresher)
                ui.message = login_message
                ui.view = "message"
            except (OSError, ValueError) as exc:
                print(f"dashboard reload after login failed closed: {exc}", file=sys.stderr)
                code = 2
                break
    finally:
        if not ui.shutdown(timeout=10.0):
            print(
                "manager provider work exceeded the 10-second shutdown bound",
                file=sys.stderr,
            )
            code = 2
        manager.close()
        if not refresher.stop(timeout=10.0):
            print(
                "provider refresh cancellation exceeded the 10-second shutdown bound",
                file=sys.stderr,
            )
            code = 2
        for selected_signal, old_handler in previous.items():
            try:
                signal.signal(selected_signal, old_handler)
            except (ValueError, OSError):
                pass
    return code


def cmd_probe(args: argparse.Namespace, settings: dict[str, Any]) -> int:
    resolved = _selected_seat(getattr(args, "seat", None), settings)
    if getattr(args, "seat", None) is not None and resolved is None:
        print(f"unknown seat selector: {args.seat}", file=sys.stderr)
        return 2
    targets = [resolved] if resolved else _operational_seat_ids(settings)
    if resolved:
        lifecycle = settings["pool"]["seats"][resolved]["lifecycle"]
        if lifecycle == "inactive":
            try:
                for message in refresh_account_usage(ROOT, settings, targets):
                    print(message)
            except AccountReadError as exc:
                print(exc, file=sys.stderr)
                return 2
            return 0
        if lifecycle == "retired":
            print(f"{resolved} is retired and cannot be probed", file=sys.stderr)
            return 2
    if not targets:
        print("no active or draining Codex seats to probe")
        return 0
    try:
        for message in probe_seats(ROOT, settings, targets):
            print(message)
    except AccountReadError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


def cmd_logins(args: argparse.Namespace, settings: dict[str, Any]) -> int:
    resolved = _selected_seat(getattr(args, "seat", None), settings)
    if getattr(args, "seat", None) is not None and resolved is None:
        print(f"unknown seat selector: {args.seat}", file=sys.stderr)
        return 2
    targets = [resolved] if resolved else _operational_seat_ids(settings)
    if resolved:
        lifecycle = settings["pool"]["seats"][resolved]["lifecycle"]
        if lifecycle == "retired":
            print(f"{resolved} is retired and cannot be inspected", file=sys.stderr)
            return 2
    if not targets:
        print("no active or draining Codex seats to inspect")
        return 0
    try:
        codex = resolve_executable("codex")
    except FileNotFoundError as exc:
        print(exc)
        return 2
    failures = 0
    for seat_id in targets:
        home = runtime_seat_home(ROOT, seat_id, settings)
        env = dict(os.environ, CODEX_HOME=str(home))
        try:
            result = bounded_process.run(
                [codex, "login", "status"], timeout=30, env=env
            )
        except bounded_process.ProcessError:
            result = None
        authenticated = result is not None and result.returncode == 0
        print(f"{seat_id}: {'authenticated' if authenticated else 'not-confirmed'}")
        if not authenticated:
            failures += 1
            print(
                f"  login: codex-seat seats login {seat_id} "
                "--from-current-user-request"
            )
    return 1 if failures else 0


def cmd_config(args: argparse.Namespace, settings: dict[str, Any]) -> int:
    if getattr(args, "config_command", None) == "policy":
        if (
            getattr(args, "sync_workers", False)
            or getattr(args, "set_primary_full_access", False)
            or getattr(args, "worker_access", None) is not None
            or getattr(args, "seat", None) is not None
            or getattr(args, "from_current_user_request", False)
        ):
            print("policy cannot be combined with other config actions", file=sys.stderr)
            return 2
        action = getattr(args, "policy_action", None) or "show"
        if action == "show":
            from . import agent_json

            if getattr(args, "policy_change", None) or getattr(args, "apply", False):
                print("policy show takes no change or --apply", file=sys.stderr)
                return 2
            try:
                return agent_json.emit_success(policy_report(settings))
            except PolicyError as exc:
                return agent_json.emit_failure("invalid-policy", f"invalid policy: {exc}", 2)
        if not getattr(args, "policy_change", None):
            print("policy set requires --set KEY VALUE", file=sys.stderr)
            return 2
        if getattr(args, "apply", False):
            from .seat_manager import ManagerEligibilityError, ensure_not_managed_worker
            try:
                ensure_not_managed_worker()
            except ManagerEligibilityError as exc:
                print(f"policy apply refused: {exc}", file=sys.stderr)
                return 2
        try:
            key, value = args.policy_change
            if isinstance(POLICY_DEFAULTS.get(key), bool):
                value = {"true": True, "false": False}.get(value, value)
            preview = preview_policy(ROOT, {key: value})
            applied = False
            if getattr(args, "apply", False):
                applied = apply_policy(ROOT, preview)
            print(json.dumps({
                "schema": 1,
                "ok": True,
                "digest": preview.digest,
                "before": policy_report(json.loads(preview.before)),
                "after": policy_report(json.loads(preview.after)),
                "changed": preview.before != preview.after,
                "applied": applied,
            }, indent=2, sort_keys=True))
        except (OSError, ValueError) as exc:
            print(f"policy unchanged: {exc}", file=sys.stderr)
            return 2
        return 0
    if getattr(args, "policy_action", None) or getattr(args, "policy_change", None):
        print("policy options require 'config policy'", file=sys.stderr)
        return 2
    worker_access = getattr(args, "worker_access", None)
    if worker_access is not None:
        if getattr(args, "seat", None) is not None:
            print("[codex-seat] --seat requires --sync-workers", file=sys.stderr)
            return 2
        context = current_controller()
        if context.managed_worker:
            print("[codex-seat] settings unchanged: managed workers cannot change worker access", file=sys.stderr)
            return 2
        if worker_access == "trusted-development" and (
            (not context.is_controller and not sys.stdin.isatty())
            or not getattr(args, "from_current_user_request", False)
        ):
            print(
                "[codex-seat] settings unchanged: enabling trusted-development requires "
                "an interactive controller or human terminal and --from-current-user-request",
                file=sys.stderr,
            )
            return 2
        try:
            set_worker_access(ROOT, worker_access)
        except (OSError, ValueError) as exc:
            print(f"[codex-seat] settings unchanged: {exc}", file=sys.stderr)
            return 2
        print(f"worker access: {worker_access}")
        return 0
    if getattr(args, "apply", False):
        print("[codex-seat] --apply requires 'config policy set'", file=sys.stderr)
        return 2
    if getattr(args, "from_current_user_request", False):
        print(
            "[codex-seat] --from-current-user-request requires --worker-access",
            file=sys.stderr,
        )
        return 2
    if getattr(args, "set_primary_full_access", False):
        if getattr(args, "seat", None) is not None:
            print(
                "[codex-seat] --seat requires --sync-workers",
                file=sys.stderr,
            )
            return 2
        from .primary_config import PrimaryConfigError, set_primary_full_access

        path = Path.home() / ".codex" / "config.toml"
        try:
            changed = set_primary_full_access(path)
        except (OSError, PrimaryConfigError) as exc:
            print(f"[codex-seat] primary config unchanged: {exc}", file=sys.stderr)
            return 2
        print(f"primary config: {'updated' if changed else 'already current'} ({path})")
        return 0
    if not getattr(args, "sync_workers", False):
        if getattr(args, "seat", None) is not None:
            print("[codex-seat] --seat requires --sync-workers", file=sys.stderr)
            return 2
        print(json.dumps(settings, indent=2))
        return 0
    resolved = _selected_seat(getattr(args, "seat", None), settings)
    if getattr(args, "seat", None) is not None and resolved is None:
        print(f"unknown seat selector: {args.seat}", file=sys.stderr)
        return 2
    targets = [resolved] if resolved else _operational_seat_ids(settings)
    if resolved:
        lifecycle = settings["pool"]["seats"][resolved]["lifecycle"]
        if lifecycle == "retired":
            print(f"{resolved} is retired and cannot be synchronized", file=sys.stderr)
            return 2
    failures = 0
    for seat_id in targets:
        try:
            changed = sync_codex_config(ROOT, settings, seat_id)
            print(f"{seat_id}: {'updated' if changed else 'already current'}")
        except (OSError, ValueError) as exc:
            failures += 1
            print(f"{seat_id}: failed ({exc})")
    return 1 if failures else 0


def cmd_supervise(args: argparse.Namespace, settings: dict[str, Any]) -> int:
    from dataclasses import replace

    from . import goalspec as goalspec_mod
    from . import worktree as worktree_mod
    from .goalspec import GoalSpec, SpecError
    from .heads import dispatch_head
    from .supervisor import supervise

    # Read the spec bytes ONCE and parse that snapshot; nothing re-reads the path.
    spec_path = Path(args.spec).expanduser()
    try:
        spec_bytes = spec_path.read_bytes()
    except OSError as exc:
        print(f"[codex-seat] GoalSpec unreadable: {exc}")
        return 2

    try:
        spec = GoalSpec.from_bytes(spec_bytes, source=str(spec_path))
    except SpecError as exc:
        print(f"[codex-seat] invalid goal spec: {exc}")
        return 2
    if args.max_iterations:
        cap = max(1, int(args.max_iterations))
        spec = replace(
            spec,
            max_iterations=min(cap, spec.max_iterations),
            pause_after_failures=min(spec.pause_after_failures, cap),
        )

    if getattr(args, "review", None):
        # Operator override at launch (the frozen spec bytes are unchanged):
        # recompute judge-path protection for the NEW command (additive — stale
        # protections from the spec's own command are kept, over-protection is
        # the safe direction) and surface the override loudly for the audit
        # trail (a silent `--review none` would strip the default gate with no
        # durable trace).
        overridden = args.review.strip()
        spec = replace(
            spec,
            review=overridden,
            protected=goalspec_mod._merged_unique(
                spec.protected,
                goalspec_mod._review_judge_paths(overridden, spec.repo)
                if overridden.lower() != "none" else (),
            ),
        )
        print(f"[codex-seat] OVERRIDE: --review {overridden!r} replaces the spec's review setting")

    provider = args.head or spec.head
    head_model = getattr(args, "head_model", None) or spec.head_model or None
    effort = getattr(args, "effort", None) or spec.effort or None
    # One identity for the whole run: stamps job-ledger entries (goal_id +
    # iteration).
    goal_id = args.branch or f"run-{int(time.time())}"
    try:
        selected_seat = _numeric_selected_seat(args.seat, settings)
    except ValueError as exc:
        print(f"[codex-seat] {exc}")
        return 2
    head = dispatch_head(
        ROOT, settings, provider=provider, seat=selected_seat, model=head_model,
        effort=effort, mcp=spec.mcp, protected_paths=spec.protected, goal_id=goal_id,
    )
    print(f"[codex-seat] supervising: {spec.goal}")
    print(
        f"[codex-seat] repo={spec.repo} head={provider}"
        f"{f' model={head_model}' if head_model else ''}{f' effort={effort}' if effort else ''} "
        f"max_iterations={spec.max_iterations} breaker={spec.pause_after_failures}"
    )
    policy = policy_report(settings)
    resolved_review = goalspec_mod.resolve_review_command(
        spec, mode=policy["review.mode"], strategy=policy["review.strategy"],
    )
    if resolved_review:
        origin = "spec" if spec.review.strip() else "default"
        label = "advisory review" if not spec.review.strip() and policy["review.mode"] == "advisory" else "terminal review gate"
        print(f"[codex-seat] {label} ({origin}): {resolved_review}")
    else:
        print("[codex-seat] no terminal review gate")
    outcome = supervise(
        spec,
        head,
        branch=args.branch,
        goal_id=goal_id,
        progress_path=args.progress,
        settings=settings,
        reader_heartbeat=(
            getattr(args, "_settings_reader").heartbeat
            if getattr(args, "_settings_reader", None) is not None
            else None
        ),
    )

    print(
        f"[codex-seat] {outcome.status.upper()} after {outcome.iterations} iteration(s): "
        f"{outcome.reason}"
    )
    print(f"[codex-seat] branch={outcome.branch} worktree={outcome.worktree}")
    if outcome.tokens:
        print(f"[codex-seat] head tokens: {outcome.tokens:,}")
    if outcome.status == "done":
        print(f"[codex-seat] changes are on branch '{outcome.branch}'; review and merge with git.")
    if getattr(args, "cleanup", False) and outcome.worktree:
        worktree_mod.open_owned(spec.repo, Path(outcome.worktree), outcome.branch).remove()
        print("[codex-seat] worktree removed (--cleanup)")
    return 0 if outcome.status == "done" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-seat", description="Managed Codex/Claude worker pool")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    from . import seats_cli

    seats_cli.register(sub)
    from . import local_cli

    local_cli.register(sub)


    pick = sub.add_parser("pick-seat", help="print the budget-optimal seat id (or '-' if the pool is exhausted)")
    pick.add_argument(
        "--kind",
        choices=list(TASK_KINDS),
        default="review",
    )
    pick.add_argument("--size", choices=["small", "big"], default=None)
    pick.add_argument(
        "--json",
        action="store_true",
        help="agent envelope; exit 3 with error.code=exhausted when nothing fits",
    )
    pick.set_defaults(handler="pick_seat")

    capacity = sub.add_parser(
        "capacity",
        help="where work can run now across Codex, Claude and local seats (read-only)",
        description=(
            "Read-only view of every seat: status, 5h/7d remaining, resets, which task "
            "sizes fit, busy work, banked resets, queued/running jobs, and the seat "
            "pick-seat would choose next per size. Never calls a provider; refresh "
            "stale usage with `codex-seat seats refresh --all`. Agents: use --json."
        ),
    )
    capacity.add_argument("--json", action="store_true", help="agent envelope output")
    capacity.set_defaults(handler="capacity")

    help_p = sub.add_parser("help", help="print a topic guide (agents)")
    help_p.add_argument("topic", choices=["agents"])
    help_p.set_defaults(handler="help")

    dash = sub.add_parser("dash", help="human dashboard")
    dash.add_argument("--no-color", action="store_true")
    dash.add_argument("--watch", action="store_true", help="continuously redraw the dashboard")
    dash.add_argument(
        "--manage",
        action="store_true",
        help="enter interactive seat management directly (trusted real terminal only)",
    )
    dash.add_argument("--interval", type=float, default=5, help="watch refresh seconds (default: 5)")
    dash.add_argument(
        "--probe-interval",
        type=float,
        default=None,
        help="active account probe seconds (default: pool.watch_probe_seconds or 300)",
    )
    dash.add_argument(
        "--no-active-probe",
        action="store_true",
        help="redraw cached/passive data without active Codex or Claude probes",
    )
    dash.add_argument(
        "--forensic",
        action="store_true",
        help="include reserve, source, last-pool-job, and raw telemetry details",
    )
    dash.set_defaults(handler="dash")
    status = sub.add_parser("status", help="plain or JSON pool state")
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler="status")
    resets = sub.add_parser("resets", help="show normal Codex and Claude quota reset times")
    resets.add_argument("-s", "--seat", help="seatN or numeric shorthand")
    resets.add_argument("--refresh", action="store_true", help="read Codex account snapshots now")
    resets.add_argument("--json", action="store_true")
    resets.set_defaults(handler="resets")
    redeem = sub.add_parser("redeem-reset", help="preview or manually redeem one banked Codex reset")
    redeem.add_argument(
        "seat", nargs="?", metavar="SEAT", help="registered seatN or numeric shorthand"
    )
    redeem.add_argument(
        "--seat", dest="seat_option", metavar="SEAT", help="same as the positional SEAT"
    )
    redeem.add_argument("--preview", action="store_true", help="read-only exact target preview")
    redeem.add_argument("--from-current-user-request", action="store_true")
    redeem.add_argument("--json", action="store_true")
    redeem.set_defaults(handler="redeem_reset")
    seg = sub.add_parser("segment", help="tmux status segment")
    seg.add_argument("--plain", action="store_true")
    seg.set_defaults(handler="segment")

    probe = sub.add_parser("probe", help="zero-token structured Codex account usage read")
    probe.add_argument("-s", "--seat", help="seatN or numeric shorthand")
    probe.set_defaults(handler="probe")
    logins = sub.add_parser("logins", help="check Codex authentication status")
    logins.add_argument("-s", "--seat", help="seatN or numeric shorthand")
    logins.set_defaults(handler="logins")

    provider = sub.add_parser("provider", help="configure optional providers")
    provider_sub = provider.add_subparsers(dest="provider_command", required=True)
    provider_status = provider_sub.add_parser("status")
    provider_status.add_argument("--json", action="store_true", help="agent envelope output")
    provider_status.set_defaults(handler="provider_status")
    provider_claude = provider_sub.add_parser(
        "claude", help="change Claude account settings (managed workers refused)"
    )
    claude_sub = provider_claude.add_subparsers(dest="claude_action", required=True)
    claude_set = claude_sub.add_parser(
        "set",
        help="set Claude mode, tier label, headroom floors or active usage refresh",
    )
    claude_set.add_argument(
        "--mode", choices=["off", "manual_only", "review_only", "opportunistic"]
    )
    claude_set.add_argument(
        "--tier", help="subscription tier label (e.g. 'Max 20x'); 'none' clears it"
    )
    claude_set.add_argument("--small-headroom", type=float, metavar="PCT")
    claude_set.add_argument("--big-headroom", type=float, metavar="PCT")
    claude_set.add_argument("--active-usage-refresh", choices=["on", "off"])
    claude_set.add_argument("--dry-run", action="store_true", help="preview without writing")
    claude_set.add_argument("--json", action="store_true", help="agent envelope output")
    claude_set.set_defaults(handler="provider_claude_set")
    provider_set = provider_sub.add_parser("set")
    provider_set.add_argument("provider_name", choices=["claude"])
    provider_set.add_argument(
        "mode", choices=["off", "manual_only", "review_only", "opportunistic"]
    )
    provider_set.add_argument("--from-current-user-request", action="store_true")
    provider_set.add_argument("--cancel-active", action="store_true")
    provider_set.add_argument("--reset-state", action="store_true", help="clear a known auth/install/cooldown state after fixing it")
    provider_set.add_argument("--expires-at", help="UTC ISO timestamp or 'clear'")
    provider_set.set_defaults(handler="provider_set")

    config = sub.add_parser("config", help="print or update non-secret Codex settings")
    config.add_argument(
        "config_command",
        nargs="?",
        choices=["policy"],
        help="show or set operator policy (previewed unless --apply)",
    )
    config.add_argument("policy_action", nargs="?", choices=["show", "set"])
    config.add_argument("--set", nargs=2, metavar=("KEY", "VALUE"), dest="policy_change")
    config_action = config.add_mutually_exclusive_group()
    config_action.add_argument(
        "--sync-workers",
        action="store_true",
        help="mirror primary Codex capabilities to operational seats",
    )
    config.add_argument(
        "-s", "--seat", help="limit --sync-workers to seatN or numeric shorthand"
    )
    config_action.add_argument(
        "--set-primary-full-access",
        action="store_true",
        help="atomically set primary config default_permissions to :danger-full-access",
    )
    config_action.add_argument(
        "--worker-access",
        choices=["restricted", "trusted-development"],
        help="select trusted worker development access or roll back to restricted",
    )
    config.add_argument(
        "--apply",
        action="store_true",
        help="apply 'config policy set' after its default preview",
    )
    config.add_argument(
        "--from-current-user-request",
        action="store_true",
        help="attest that the current user explicitly requested this settings change",
    )
    config.set_defaults(handler="config")

    projects = sub.add_parser("projects", help="manage the registered-project allowlist")
    projects_sub = projects.add_subparsers(dest="projects_action", required=True)
    projects_list = projects_sub.add_parser("list", help="list registered project ids and paths")
    projects_add = projects_sub.add_parser(
        "add", help="validate and atomically register a Git repository"
    )
    projects_add.add_argument("project_id", help="stable lowercase project identifier")
    projects_add.add_argument("path", help="path to the exact Git repository root")
    projects_rename = projects_sub.add_parser(
        "rename", help="atomically rename a project id and optionally update its path"
    )
    projects_rename.add_argument("old_id", help="currently registered project identifier")
    projects_rename.add_argument("new_id", help="new stable lowercase project identifier")
    projects_rename.add_argument(
        "--path", help="new exact Git root, for a repository directory that was also renamed"
    )
    for projects_parser in (projects_list, projects_add, projects_rename):
        projects_parser.set_defaults(handler="projects")

    supervise_p = sub.add_parser(
        "supervise", help="drive a goal spec to done via the approve-and-go loop"
    )
    supervise_p.add_argument("spec", help="path to a goal spec JSON file")
    supervise_p.add_argument(
        "--head", choices=["auto", "claude", "codex"], help="override the goal spec's head"
    )
    supervise_p.add_argument("--branch", help="branch name for the run worktree")
    supervise_p.add_argument("--progress", help="path to write a JSON heartbeat each iteration")
    supervise_p.add_argument(
        "--review",
        help="terminal review command run once after acceptance passes (overrides the "
        "spec's 'review'); non-zero feeds its output back for the head to fix. "
        "Diff is at $CODEX_SEATS_DIFF.",
    )
    supervise_p.add_argument("--head-model", help="override the build head's model id")
    supervise_p.add_argument(
        "--effort", choices=list(EFFORTS),
        help="override the build head's reasoning effort",
    )
    supervise_p.add_argument(
        "--max-iterations", type=int, help="cap iterations below the goal spec's own bound"
    )
    supervise_p.add_argument(
        "-s", "--seat",
        help="force a specific Codex seat",
    )
    supervise_p.add_argument(
        "--cleanup", action="store_true", help="remove the run worktree and branch when finished"
    )
    supervise_p.set_defaults(handler="supervise")

    stats = sub.add_parser(
        "stats",
        help="routing, outcomes, tokens, and effort",
    )
    stats.add_argument("--days", type=float, default=30.0)
    stats.add_argument("--json", action="store_true", dest="as_json")
    stats.set_defaults(handler="stats")
    from .review import configure_parser as configure_review_parser

    review_p = sub.add_parser("review", help="read-only structured review of an explicit diff")
    configure_review_parser(review_p)

    # interactive head launcher (codex|claude), wired for dispatch-based delegation
    head_p = sub.add_parser("head", help="launch an interactive head (codex|claude)")
    head_p.add_argument("head_kind", choices=["codex", "claude"])
    head_p.add_argument(
        "-s",
        "--seat",
        help="Codex seatN or numeric shorthand (default: primary)",
    )
    head_p.add_argument("--cd", help="repository to open")
    head_p.add_argument(
        "--safe",
        action="store_true",
        help="use the contained head profile instead of the default full-access profile (codex only)",
    )
    head_p.set_defaults(handler="head")

    # head-side delegation commands (delegate / await) live in their own module
    from . import dispatch_cli
    dispatch_cli.register(sub)

    from . import changesets
    changesets.register(sub)

    # Immutable release staging/new-work pointer management.  Registration has
    # no runtime side effects; activation remains an explicit subcommand.
    from . import releases
    releases.register(sub)

    # launchd dispatch-runner management. install/uninstall are persistent system changes
    # the human invokes explicitly; status/doctor are read-only.
    dispatch = sub.add_parser("dispatch", help="manage the launchd dispatch runner")
    dispatch_sub = dispatch.add_subparsers(dest="dispatch_action", required=True)
    for _action in ("install", "status", "doctor", "uninstall"):
        dispatch_sub.add_parser(_action, help=f"{_action} the dispatch LaunchAgent").set_defaults(
            handler="dispatch"
        )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    from . import seats_cli

    seats_cli.normalize_plan_args(args)
    if args.handler == "review":
        from .review import command as review_command

        return review_command(args, parser)
    if args.handler == "local":
        from .local_cli import cmd_local

        return cmd_local(args, ROOT)
    if args.handler == "help":
        return cmd_help(args)
    if args.handler == "dash" and bool(getattr(args, "manage", False)):
        from .seat_manager import ManagerEligibilityError, ensure_interactive_terminal

        try:
            ensure_interactive_terminal()
        except ManagerEligibilityError as exc:
            print(f"dash --manage refused: {exc}", file=sys.stderr)
            return 2

    def invoke(settings: dict[str, Any], warnings: list[str]) -> int:
        if getattr(args, "probe_interval", None) is None:
            args.probe_interval = float(
                settings.get("pool", {}).get("watch_probe_seconds", 300)
            )
        handlers = {
            "pick_seat": lambda: cmd_pick_seat(args, settings),
            "capacity": lambda: cmd_capacity(args, settings, warnings),
            "dash": lambda: cmd_dash(args, settings, warnings),
            "status": lambda: cmd_status(args, settings, warnings),
            "resets": lambda: cmd_resets(args, settings),
            "redeem_reset": lambda: cmd_redeem_reset(args, settings),
            "segment": lambda: cmd_segment(args, settings),
            "probe": lambda: cmd_probe(args, settings),
            "logins": lambda: cmd_logins(args, settings),
            "provider_status": lambda: cmd_provider_status(args, settings),
            "provider_set": lambda: cmd_provider_set(args, settings),
            "provider_claude_set": lambda: cmd_provider_claude_set(args),
            "config": lambda: cmd_config(args, settings),
            "projects": lambda: _cmd_projects(args),
            "supervise": lambda: cmd_supervise(args, settings),
            "stats": lambda: _cmd_stats(args, settings),
            "delegate": lambda: _cmd_delegate(args, settings),
            "await_jobs": lambda: _cmd_await(args, settings),
            "job": lambda: _cmd_job(args, settings),
            "changeset": lambda: _cmd_changeset(args, settings),
            "dispatch": lambda: _cmd_dispatch(args, settings),
            "head": lambda: _cmd_head(args, settings, warnings),
            "release": lambda: _cmd_release(args),
            "seats": lambda: seats_cli.cmd_seats(args, settings, ROOT),
        }
        return int(handlers[args.handler]())

    reader_kind = None
    if args.handler == "dash" and bool(
        getattr(args, "watch", False) or getattr(args, "manage", False)
    ):
        reader_kind = "dashboard-watch"
    elif args.handler == "supervise":
        reader_kind = "supervisor"
    if reader_kind is not None:
        from .runtime_readers import SettingsReader

        with SettingsReader(ROOT, reader_kind, module_file=__file__) as reader:
            args._settings_reader = reader
            settings, warnings = reader.load(load_settings)
            return invoke(settings, warnings)
    settings, warnings = load_settings(ROOT)
    return invoke(settings, warnings)


def _cmd_projects(args: argparse.Namespace) -> int:
    from .projects import cmd_projects

    return cmd_projects(args, ROOT)


def _cmd_stats(args: argparse.Namespace, settings: dict[str, Any]) -> int:
    from .stats import cmd_stats

    return cmd_stats(args, settings, ROOT)


def _cmd_delegate(args: argparse.Namespace, settings: dict[str, Any]) -> int:
    from .dispatch_cli import cmd_delegate

    return cmd_delegate(args, settings, ROOT)


def _cmd_await(args: argparse.Namespace, settings: dict[str, Any]) -> int:
    from .dispatch_cli import cmd_await

    return cmd_await(args, settings, ROOT)


def _cmd_job(args: argparse.Namespace, settings: dict[str, Any]) -> int:
    from .dispatch_cli import cmd_job

    return cmd_job(args, settings, ROOT)


def _cmd_changeset(args: argparse.Namespace, settings: dict[str, Any]) -> int:
    from .changesets import cmd_changeset

    return cmd_changeset(args, settings, ROOT)


def _cmd_head(args: argparse.Namespace, settings: dict[str, Any], warnings: list[str]) -> int:
    from .head_launcher import cmd_head

    invalid = [warning for warning in warnings if warning.startswith("invalid settings")]
    if invalid:
        print(f"[codex-seat] head refused: {'; '.join(invalid)}", file=sys.stderr)
        return 2
    try:
        args.seat = _numeric_selected_seat(args.seat, settings)
    except ValueError as exc:
        print(f"[codex-seat] {exc}", file=sys.stderr)
        return 2
    return cmd_head(args, settings, ROOT)


def _cmd_release(args: argparse.Namespace) -> int:
    from .releases import cmd_release

    return cmd_release(args, ROOT)


def _cmd_dispatch(args: argparse.Namespace, settings: dict[str, Any]) -> int:
    del settings
    from . import launchd_dispatch as ld

    action = args.dispatch_action
    if action == "install":
        print(f"installed + bootstrapped: {ld.install(ROOT)}")
        return 0
    if action == "uninstall":
        ld.uninstall(ROOT)
        print("uninstalled dispatch LaunchAgent")
        return 0
    if action == "status":
        print(json.dumps(ld.status(ROOT), indent=2))
        return 0
    if action == "doctor":
        ok = True
        for good, message in ld.doctor(ROOT):
            print(f"{'ok  ' if good else 'FAIL'} {message}")
            ok = ok and good
        return 0 if ok else 1
    print(f"unknown dispatch action: {action}", file=sys.stderr)
    return 2



# ---- provider/status/reset operator commands (merged from cli_operator, v7) ----


def cmd_provider_status(args: argparse.Namespace, settings: dict[str, Any]) -> int:
    mode, reason = effective_claude_mode(settings)
    with Ledger(ROOT) as data:
        refresh_transients(data, ROOT, settings)
        provider = data["providers"]["claude"]
        model_states = provider.get("models", {})
        capped = (
            [
                model for model, state in model_states.items()
                if isinstance(state, dict) and state.get("status") == "capped"
            ]
            if isinstance(model_states, dict)
            else []
        )
        report: dict[str, Any] = {
            "provider": "claude",
            "mode": mode,
            "mode_reason": reason,
            "status": provider.get("status", "unknown"),
            "busy": bool(provider.get("in_flight")),
            "last_job_at": provider.get("last_job_at"),
            "last_resolved_model": provider.get("last_resolved_model"),
            "capped_models": capped,
            "limits": None,
        }
    limits = load_claude_rate_limits(ROOT) if mode != "off" else {}
    if limits:
        report["limits"] = {
            "five_hour_pct": limits.get("five_hour", {}).get("remaining_percentage"),
            "seven_day_pct": limits.get("seven_day", {}).get("remaining_percentage"),
            "fresh": claude_usage_fresh(settings, limits),
            "age_seconds": claude_usage_age_seconds(limits),
        }
    if getattr(args, "json", False):
        from . import agent_json

        return agent_json.emit_success(report)
    print(f"claude mode: {mode}" + (f" ({reason})" if reason else ""))
    print(f"claude state: {report['status']}")
    print(f"active: {'yes' if report['busy'] else 'no'}")
    print(f"last job: {report['last_job_at'] or '-'}")
    print(f"last resolved model: {report['last_resolved_model'] or '-'}")
    if isinstance(model_states, dict):
        print("model caps: " + (", ".join(capped) if capped else "none"))
    if limits:
        five_hour = report["limits"]["five_hour_pct"]
        seven_day = report["limits"]["seven_day_pct"]
        marker = "" if report["limits"]["fresh"] else "~"
        print(
            "limits left: "
            f"5h={'?' if five_hour is None else f'{marker}{five_hour:.0f}%'}, "
            f"7d={'?' if seven_day is None else f'{marker}{seven_day:.0f}%'}"
        )
        usage_age = report["limits"]["age_seconds"]
        if usage_age is not None:
            print(f"usage capture age: {usage_age / 3600:.1f}h ({'fresh' if not marker else 'stale'})")
    elif mode != "off":
        print("limits left: awaiting the next useful Claude response")
    if mode == "off":
        print("limits left: disabled (cache not read)")
        print("hard-off guarantee: no binary lookup, auth check, usage probe, subprocess, or network call")
    return 0


def cmd_provider_set(args: argparse.Namespace, settings: dict[str, Any]) -> int:
    if args.provider_name != "claude":
        print("only the managed Claude route is recognized")
        return 2
    context = current_controller()
    if context.managed_worker or os.environ.get("CLAUDE_LEAD_WORKER") == "1":
        print("managed workers cannot change provider mode")
        return 2
    # --from-current-user-request is still accepted but no longer required:
    # heads and plain shells may enable Claude so agents can use all capacity.
    expiry_update = None if args.expires_at == "clear" else args.expires_at
    try:
        raw_settings = (
            set_claude_mode(ROOT, args.mode, expires_at=expiry_update)
            if args.expires_at is not None
            else set_claude_mode(ROOT, args.mode)
        )
    except ValueError as exc:
        print(f"provider configuration unchanged: {exc}")
        return 2
    effective_settings, warnings = load_settings(ROOT)
    if warnings:
        print("; ".join(f"warning: {warning}" for warning in warnings))
    claim = None
    with Ledger(ROOT) as data:
        refresh_transients(data, ROOT, effective_settings)
        provider = data["providers"]["claude"]
        if args.mode == "off":
            claim = provider.get("in_flight")
        elif args.reset_state:
            # Explicit recovery after the user fixes auth/installation. Merely
            # toggling mode does not erase a real cooldown or provider error.
            provider["status"] = "ok"
            provider["status_until"] = None
            provider["status_before_off"] = None
            provider["cooldown_provenance"] = None
            provider["last_error"] = None
            provider["models"] = {}
    canceled = terminate_claim(claim) if args.mode == "off" and args.cancel_active else False
    print(f"claude mode -> {args.mode}")
    if args.expires_at is not None:
        print(f"expiry -> {raw_settings['providers']['claude'].get('expires_at') or 'none'}")
    if claim:
        print("active Claude task " + ("termination requested" if canceled else "left to drain"))
    return 0


def cmd_provider_claude_set(args: argparse.Namespace) -> int:
    """Preview and apply Claude account settings through the account manager."""
    from . import agent_json
    from .claude_manager import ClaudeAccountManager
    from .seat_manager import ManagerError

    changes: dict[str, Any] = {}
    if args.mode is not None:
        changes["mode"] = args.mode
    if args.tier is not None:
        changes["subscription_tier"] = None if args.tier.lower() in {"none", "clear"} else args.tier
    if args.small_headroom is not None:
        changes["small_task_min_headroom_pct"] = args.small_headroom
    if args.big_headroom is not None:
        changes["big_task_min_headroom_pct"] = args.big_headroom
    if args.active_usage_refresh is not None:
        changes["active_usage_refresh"] = args.active_usage_refresh == "on"

    def refuse(message: str) -> int:
        if args.json:
            return agent_json.emit_failure("refused", message, 2)
        print(f"provider claude set refused: {message}", file=sys.stderr)
        return 2

    if not changes:
        return refuse(
            "pass at least one of --mode, --tier, --small-headroom, --big-headroom, "
            "--active-usage-refresh"
        )
    manager = ClaudeAccountManager(ROOT)
    try:
        preview = manager.preview(**changes)
        result = None
        if not args.dry_run:
            # The agent's own invocation is the current request; managed
            # workers are still refused inside commit.
            result = manager.commit(preview, from_current_user_request=True)
    except ManagerError as exc:
        return refuse(str(exc))
    finally:
        manager.close()
    report = {
        "planned": bool(args.dry_run),
        "changed": preview.changed if result is None else result.changed,
        "before": preview.as_dict()["before"],
        "after": preview.as_dict()["after"],
        "warnings": list(preview.warnings if result is None else result.warnings),
    }
    if args.json:
        return agent_json.emit_success(report)
    verb = "PLAN" if args.dry_run else "APPLIED"
    differences = [
        f"{key}: {report['before'].get(key)} -> {report['after'].get(key)}"
        for key in report["after"]
        if report["before"].get(key) != report["after"].get(key)
    ]
    print(f"{verb} claude settings: " + ("; ".join(differences) or "no change"))
    for warning in report["warnings"]:
        print(f"WARNING: {warning}")
    return 0


def cmd_segment(args: argparse.Namespace, settings: dict[str, Any]) -> int:
    print(segment(ROOT, settings, plain=args.plain), end="")
    return 0


def cmd_status(args: argparse.Namespace, settings: dict[str, Any], warnings: list[str]) -> int:
    try:
        if args.json:
            print(status_json(ROOT, settings))
        else:
            print(dashboard(ROOT, settings, warnings, color=False))
        return 0
    except PoolSnapshotError as exc:
        print(f"codex-seat status: {exc}", file=sys.stderr)
        return 2


def cmd_resets(args: argparse.Namespace, settings: dict[str, Any]) -> int:
    resolved = _selected_seat(getattr(args, "seat", None), settings)
    if getattr(args, "seat", None) is not None and resolved is None:
        print(f"unknown seat selector: {args.seat}", file=sys.stderr)
        return 2
    targets = [resolved] if resolved else _operational_seat_ids(settings)
    if resolved:
        lifecycle = settings["pool"]["seats"][resolved]["lifecycle"]
        if lifecycle == "retired":
            print(f"{resolved} is retired; use 'seats show' for history", file=sys.stderr)
            return 2
    messages: list[str] = []
    if args.refresh:
        try:
            messages = refresh_account_usage(ROOT, settings, targets)
        except AccountReadError as exc:
            print(exc, file=sys.stderr)
            return 2
        stream = sys.stderr if args.json else sys.stdout
        for message in messages:
            print(message, file=stream)
    code = 1 if messages and all("unavailable" in message for message in messages) else 0
    if args.json:
        from . import agent_json

        data = json.loads(resets_status_json(ROOT, settings))
        if code:
            return agent_json.emit_failure(
                "refresh-unavailable", "; ".join(messages), code, data=data
            )
        return agent_json.emit_success(data)
    print(resets_report(ROOT, settings))
    return code


def cmd_redeem_reset(args: argparse.Namespace, settings: dict[str, Any]) -> int:
    alias = getattr(args, "seat_option", None)
    if alias is not None and args.seat is not None and alias != args.seat:
        print("SEAT and --seat disagree; pass one", file=sys.stderr)
        return 2
    seat_id = _selected_seat(args.seat if args.seat is not None else alias, settings)
    if seat_id is None:
        print("unknown seat; use registered seatN or numeric shorthand", file=sys.stderr)
        return 2
    if os.environ.get("CODEX_SEATS_MANAGED_WORKER") == "1" or os.environ.get("CLAUDE_LEAD_WORKER") == "1":
        print("workers cannot redeem banked resets", file=sys.stderr)
        return 2
    try:
        preview = prepare_banked_reset_redemption(ROOT, settings, seat_id)
        safe = {key: value for key, value in preview.items() if not key.startswith("_")}
        expires = preview["credit"].get("expires_at")
        expiry_text = (time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(expires))
                       if isinstance(expires, (int, float)) else
                       "none" if preview["credit"].get("expiry_status") == "none" else "unknown")
        print(json.dumps(safe, indent=2) if args.json else
              f"Redeem one reset on {seat_id}, account {json.loads(preview['provider_identity'])['email']}, "
              f"credit {preview['credit']['fingerprint']} of {preview['available_count']} available; "
              f"expires {expiry_text}",
              file=sys.stderr if args.json and not args.preview else sys.stdout)
        if args.preview:
            return 0
        context = current_controller()
        if context.is_controller:
            if not args.from_current_user_request:
                raise AccountReadError("controller requires --from-current-user-request for this exact seat")
        elif not (sys.stdin.isatty() and sys.stdout.isatty()
                  and _confirm_reset_redemption(seat_id)):
            raise AccountReadError("human terminal confirmation was not given")
        result = redeem_banked_reset(ROOT, settings, preview,
                                     current_user_request=args.from_current_user_request)
        print(json.dumps(result, indent=2) if args.json else
              f"{seat_id}: {result['outcome']}; post-refresh: {result['refresh_error'] or 'ok'}")
        return 0 if result["outcome"] in {"reset", "alreadyRedeemed"} else 4
    except (AccountReadError, OSError, EOFError) as exc:
        print(f"reset action refused: {exc}", file=sys.stderr)
        return 3


def _confirm_reset_redemption(seat_id: str) -> bool:
    print(f"Type REDEEM {seat_id} to spend one credit: ", file=sys.stderr, end="", flush=True)
    return input() == f"REDEEM {seat_id}"


if __name__ == "__main__":
    raise SystemExit(main())
