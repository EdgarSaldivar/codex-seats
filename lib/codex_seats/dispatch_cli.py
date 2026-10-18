"""Command-line helpers for submitting and awaiting dispatched worker tasks."""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

from . import agent_json, dispatch_client, remote_wake
from .controller_context import current_controller
from .dispatch_protocol import DispatchError
from .efforts import EFFORTS
from .task_policy import (
    DELEGATE_KINDS,
    MAX_RUN_TIMEOUT_SECONDS,
    REQUESTED_CAPABILITIES,
    REQUESTED_SIZES,
    RISK_FLAGS,
    TASK_SHAPES,
    VALIDATION_LEVELS,
)

DEFAULT_QUEUE_TIMEOUT_SECONDS = 2 * 60 * 60
# A blocking client is only an observer.  Its default must cover the largest provider
# run (plus the pinned-child cleanup allowance), rather than silently giving up at 30m.
DEFAULT_WAIT_TIMEOUT_SECONDS = (
    DEFAULT_QUEUE_TIMEOUT_SECONDS + MAX_RUN_TIMEOUT_SECONDS + 60
)
EXIT_SUCCESS = 0
EXIT_ERROR = 2
EXIT_UNKNOWN = 3


def register(subparsers) -> None:
    """Register delegation and bounded job-control commands."""
    delegate = subparsers.add_parser("delegate", help="delegate one worker task")
    delegate.add_argument("prompt")
    delegate.add_argument("--repo")
    delegate.add_argument(
        "--provider", choices=["auto", "codex", "claude", "local"], default="auto"
    )
    delegate.add_argument(
        "--kind",
        choices=list(DELEGATE_KINDS),
        default="implementation",
    )
    delegate.add_argument("--task-shape", choices=list(TASK_SHAPES))
    delegate.add_argument(
        "--validation", choices=list(VALIDATION_LEVELS)
    )
    delegate.add_argument("--task-id")
    delegate.add_argument("--size", choices=list(REQUESTED_SIZES), default="auto")
    delegate.add_argument(
        "--tier",
        dest="capability_tier",
        choices=list(REQUESTED_CAPABILITIES),
        default="auto",
    )
    delegate.add_argument(
        "--effort",
        choices=list(EFFORTS),
        help="override the kind's reasoning effort; non-default values require --override-reason",
    )
    delegate.add_argument(
        "--estimated-minutes",
        type=float,
        help="task estimate used by policy to derive the provider execution timeout",
    )
    delegate.add_argument(
        "--parallel-safe",
        action="store_true",
        help="allow concurrent same-repo work only when scope hints are disjoint",
    )
    delegate.add_argument(
        "--scope-hint",
        dest="scope_hints",
        action="append",
        default=[],
        help="repo-relative concurrency hint (repeatable; not a filesystem grant)",
    )
    delegate.add_argument(
        "--read-only",
        action="store_true",
        help="run the worker with no repository or external write access",
    )
    delegate.add_argument(
        "--mcp",
        dest="mcp",
        action="append",
        default=[],
        help="grant a named server from the repo's committed MCP catalog",
    )
    delegate.add_argument(
        "--risk",
        dest="risk_flags",
        action="append",
        choices=list(RISK_FLAGS),
        default=[],
    )
    delegate.add_argument("--override-reason")
    delegate.add_argument(
        "--spend-reserve",
        metavar="SEAT",
        help="for this job only, spend the selected Codex seat's local reserve; "
        "requires --provider codex and --from-current-user-request",
    )
    delegate.add_argument(
        "--from-current-user-request",
        action="store_true",
        help="confirm current-user authority for --spend-reserve",
    )
    # Queue timeout is the request's staleness guard, NOT the provider work budget.
    # Keep it generous so a task queued behind a long-running job is not expired before the
    # serial runner reaches it; MAX_TTL_SECONDS (24h) is the hard ceiling.
    delegate.add_argument(
        "--queue-timeout",
        "--ttl",
        dest="queue_timeout",
        type=int,
        default=DEFAULT_QUEUE_TIMEOUT_SECONDS,
        help="seconds the request may remain queued (sets expires_at; --ttl is an alias)",
    )
    delegate.add_argument(
        "--wait-timeout",
        "--timeout",
        dest="wait_timeout",
        type=int,
        default=DEFAULT_WAIT_TIMEOUT_SECONDS,
        help="seconds this client waits for a receipt (--timeout is an alias)",
    )
    delegate.add_argument("--detach", action="store_true")
    delegate.add_argument(
        "--wake-head", action="store_true",
        help="detach and queue one completion wake to the owning Remote Codex head",
    )
    delegate.add_argument("--json", action="store_true")
    delegate.set_defaults(handler="delegate")

    await_jobs = subparsers.add_parser("await", help="await detached worker jobs")
    await_jobs.add_argument("job_ids", nargs="+")
    await_jobs.add_argument(
        "--wait-timeout",
        "--timeout",
        dest="wait_timeout",
        type=int,
        default=DEFAULT_WAIT_TIMEOUT_SECONDS,
        help="seconds this client waits for receipts (--timeout is an alias)",
    )
    await_jobs.add_argument("--json", action="store_true")
    await_jobs.add_argument(
        "--wake-head", action="store_true",
        help="arm one detached Remote wake for this complete receipt set and return",
    )
    await_jobs.set_defaults(handler="await_jobs")

    job = subparsers.add_parser("job", help="inspect or cancel a dispatched job")
    job_commands = job.add_subparsers(dest="job_action", required=True)
    status = job_commands.add_parser("status", help="show durable job lifecycle")
    status.add_argument("job_id")
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler="job")

    tail = job_commands.add_parser("tail", help="show bounded matching runner log lines")
    tail.add_argument("job_id")
    tail.add_argument("--lines", type=int, default=50)
    tail.add_argument("--summary", action="store_true")
    tail.add_argument("--json", action="store_true")
    tail.set_defaults(handler="job")

    cancel = job_commands.add_parser("cancel", help="request cancellation of one job")
    cancel.add_argument("job_id")
    cancel.add_argument("--queue-timeout", type=int, default=600)
    cancel.add_argument("--wait-timeout", type=int, default=600)
    cancel.add_argument("--json", action="store_true")
    cancel.set_defaults(handler="job")

    listing = job_commands.add_parser("list", help="list sanitized live background jobs")
    listing.add_argument("--json", action="store_true")
    listing.add_argument(
        "--watch",
        action="store_true",
        help="redraw on queue lifecycle events (interactive terminal only)",
    )
    listing.set_defaults(handler="job")


def resolve_repo_id(settings, cwd, explicit=None) -> str:
    """Return explicit, or the project id whose path contains ``cwd``."""
    if explicit is not None:
        return explicit

    cwd_path = Path(cwd).expanduser().resolve()
    projects = settings.get("projects") or {}
    matches = []
    if isinstance(projects, dict):
        for repo_id, project in projects.items():
            if not isinstance(project, dict) or not isinstance(project.get("path"), str):
                continue
            project_path = Path(project["path"]).expanduser().resolve()
            if cwd_path == project_path or project_path in cwd_path.parents:
                matches.append((len(project_path.parts), repo_id))

    if not matches:
        raise DispatchError(f"no registered project contains cwd: {cwd_path}")
    return max(matches, key=lambda match: match[0])[1]


def head_identity(env, settings=None) -> tuple[str, str | None]:
    """Return the shared controller context's dispatch origin identity."""
    return current_controller(env).dispatch_identity(settings or {})


def _controller_dispatch_identity(settings) -> tuple[str, str | None]:
    context = current_controller()
    if context.managed_worker:
        raise DispatchError("managed workers cannot delegate or cancel jobs")
    return context.dispatch_identity(settings)


def _print_receipt(receipt, *, as_json) -> None:
    if as_json:
        print(json.dumps(receipt))
        return

    parts = [
        str(receipt.get("request_id", "unknown")),
        str(receipt.get("status", "unknown")),
    ]
    for field in ("provider", "seat"):
        if receipt.get(field):
            parts.append(f"{field}={receipt[field]}")
    if receipt.get("changeset_id"):
        parts.append(f"changeset={receipt['changeset_id']}")
    detail = receipt.get("summary") or receipt.get("reason")
    if detail:
        parts.append(" ".join(str(detail).splitlines()))
    print(" ".join(parts))


class _TTYProgress:
    """Emit one stderr line for each observable lifecycle transition."""

    def __init__(self, enabled):
        self.enabled = bool(enabled)
        self._last = {}

    def submitted(self, request_id):
        self._last[request_id] = "waiting"
        if self.enabled:
            print(f"job {request_id[:8]} submitted · waiting", file=sys.stderr)

    def __call__(self, request_id, snapshot):
        state = str(snapshot.get("state", "unknown"))
        provider = snapshot.get("provider")
        seat = snapshot.get("seat")
        if state == self._last.get(request_id):
            return
        self._last[request_id] = state
        if not self.enabled:
            return
        detail = ""
        if provider:
            detail += f" · {provider}"
        if seat:
            detail += f"/{seat}"
        print(f"job {request_id[:8]} {state}{detail}", file=sys.stderr)


def _progress(as_json):
    context = current_controller()
    return _TTYProgress(
        not as_json and (sys.stderr.isatty() or context.is_controller)
    )


def _job_lines(jobs, *, width=None):
    from .display import _plain_clip

    lines = []
    for job in jobs:
        assignment = job.get("provider") or job.get("requested_provider") or "?"
        if job.get("seat"):
            assignment += f"/{job['seat']}"
        elapsed = int(job.get("elapsed_seconds", 0))
        line = (
            f"{job['request_id'][:8]} {job['state']} {assignment} "
            f"{job['kind']}/{job['size']} {elapsed}s {job['label']}"
        ).rstrip()
        lines.append(
            _plain_clip(line, int(width)) if isinstance(width, int) else line
        )
    return lines


def _watch_jobs(root, settings) -> int:
    if not sys.stdout.isatty():
        raise DispatchError("job list --watch requires an interactive terminal")
    stream = dispatch_client.watch_live_jobs(root, settings)
    try:
        for jobs in stream:
            print("\033[2J\033[H", end="")
            print("BACKGROUND WORK")
            width = shutil.get_terminal_size(fallback=(120, 24)).columns
            lines = _job_lines(jobs, width=max(1, width))
            print("\n".join(lines) if lines else "none")
            sys.stdout.flush()
    except KeyboardInterrupt:
        print()
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    return EXIT_SUCCESS


def cmd_delegate(args, settings, root) -> int:
    request_id = None
    wake = bool(getattr(args, "wake_head", False))
    try:
        origin_head, origin_seat = _controller_dispatch_identity(settings)
        if wake:
            remote_wake.prepare(args.wait_timeout)
        repo_id = resolve_repo_id(settings, Path.cwd(), args.repo)
        request_id = dispatch_client.submit_task(
            root,
            settings,
            repo_id=repo_id,
            prompt=args.prompt,
            origin_head=origin_head,
            origin_seat=origin_seat,
            provider=args.provider,
            kind=args.kind,
            task_shape=getattr(args, "task_shape", None),
            validation=getattr(args, "validation", None),
            task_id=getattr(args, "task_id", None),
            size=args.size,
            capability_tier=getattr(args, "capability_tier", "auto"),
            effort=getattr(args, "effort", None),
            estimated_minutes=getattr(args, "estimated_minutes", None),
            parallel_safe=bool(getattr(args, "parallel_safe", False)),
            scope_hints=getattr(args, "scope_hints", []),
            risk_flags=getattr(args, "risk_flags", []),
            override_reason=getattr(args, "override_reason", None),
            read_only=bool(getattr(args, "read_only", False)),
            mcp=getattr(args, "mcp", []),
            spend_reserve_seat=getattr(args, "spend_reserve", None),
            from_current_user_request=bool(
                getattr(args, "from_current_user_request", False)
            ),
            queue_timeout_seconds=getattr(
                args, "queue_timeout", getattr(args, "ttl", 600)
            ),
            now=time.time(),
        )
        if wake:
            result = remote_wake.arm(root, [request_id], wait_timeout=args.wait_timeout)
            print(json.dumps(result) if args.json else request_id)
            if not args.json:
                print(f"Remote wake {result['status']}: {result['wake_id']}", file=sys.stderr)
            return 0
        if args.detach:
            print(request_id)
            return 0

        progress = _progress(args.json)
        progress.submitted(request_id)
        wait_kwargs = {
            "wait_timeout": getattr(
                args, "wait_timeout", getattr(args, "timeout", 1800)
            )
        }
        if progress.enabled:
            wait_kwargs["on_transition"] = progress
        receipt = dispatch_client.wait_for_receipt(root, request_id, **wait_kwargs)
        _print_receipt(receipt, as_json=args.json)
        return 0 if receipt["status"] == "completed" else 1
    except (DispatchError, TimeoutError, OSError) as exc:
        if wake and request_id is not None:
            print(
                f"Job {request_id} already submitted; do not resubmit. "
                "Wake not confirmed; review durable wake evidence and receipts.",
                file=sys.stderr,
            )
        print(str(exc), file=sys.stderr)
        return 2


def cmd_await(args, settings, root) -> int:
    del settings
    try:
        if getattr(args, "wake_head", False):
            result = remote_wake.arm(root, args.job_ids, wait_timeout=args.wait_timeout)
            print(json.dumps(result) if args.json else
                  f"Remote wake {result['status']}: {result['wake_id']}")
            return 0
        progress = _progress(args.json)
        wait_kwargs = {
            "wait_timeout": getattr(
                args, "wait_timeout", getattr(args, "timeout", 1800)
            )
        }
        if progress.enabled:
            wait_kwargs["on_transition"] = progress
        receipts = dispatch_client.wait_for_receipts(root, args.job_ids, **wait_kwargs)
        for request_id in args.job_ids:
            _print_receipt(receipts[request_id], as_json=args.json)
        return 0 if all(
            receipt["status"] == "completed" for receipt in receipts.values()
        ) else 1
    except dispatch_client.ReceiptTimeout as exc:
        for request_id in args.job_ids:
            receipt = exc.receipts.get(request_id)
            if receipt is not None:
                _print_receipt(receipt, as_json=args.json)
        print(str(exc), file=sys.stderr)
        return 2
    except (DispatchError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


def cmd_job(args, settings, root) -> int:
    """Handle the status/tail/cancel family with stable 0/3/2 exits."""
    try:
        if args.job_action == "list":
            if getattr(args, "watch", False):
                if args.json:
                    raise DispatchError("job list --watch cannot be combined with --json")
                return _watch_jobs(root, settings)
            jobs = dispatch_client.live_jobs(root, settings)
            if args.json:
                return agent_json.emit_success({"jobs": jobs})
            else:
                for line in _job_lines(jobs):
                    print(line)
            return EXIT_SUCCESS

        if args.job_action == "status":
            result = dispatch_client.job_status(root, args.job_id)
            if args.json:
                if result["state"] == "unknown":
                    return agent_json.emit_failure(
                        "unknown-job", f"no durable record of job {args.job_id}",
                        EXIT_UNKNOWN, data=result,
                    )
                return agent_json.emit_success(result)
            else:
                print(f"{result['request_id']} {result['state']}")
            return EXIT_UNKNOWN if result["state"] == "unknown" else EXIT_SUCCESS

        if args.job_action == "tail":
            status = dispatch_client.job_status(root, args.job_id)
            result = dispatch_client.tail_job(
                root,
                args.job_id,
                lines=args.lines,
                include_summary=args.summary,
            )
            result["state"] = status["state"]
            if args.json:
                print(json.dumps(result))
            else:
                for line in result["lines"]:
                    print(line)
                terminal = result.get("terminal")
                if terminal:
                    detail = terminal.get("summary") or ""
                    print(f"terminal {terminal.get('status')} {detail}".rstrip())
            return EXIT_UNKNOWN if status["state"] == "unknown" else EXIT_SUCCESS

        origin_head, origin_seat = _controller_dispatch_identity(settings)
        cancel_id = dispatch_client.submit_cancel(
            root,
            settings,
            target_request_id=args.job_id,
            origin_head=origin_head,
            origin_seat=origin_seat,
            queue_timeout_seconds=args.queue_timeout,
            now=time.time(),
        )
        receipt = dispatch_client.wait_for_receipt(
            root, cancel_id, wait_timeout=args.wait_timeout
        )
        if args.json:
            print(json.dumps(receipt))
        else:
            detail = receipt.get("reason") or ""
            print(
                f"{args.job_id} {receipt.get('result', receipt.get('status', 'error'))} "
                f"{detail}".rstrip()
            )
        result = receipt.get("result")
        if receipt.get("status") != "completed" or result == "error":
            return EXIT_ERROR
        if result == "unknown":
            return EXIT_UNKNOWN
        return EXIT_SUCCESS
    except (DispatchError, TimeoutError, OSError) as exc:
        if args.json and args.job_action in {"list", "status"}:
            return agent_json.emit_failure("dispatch-error", str(exc), EXIT_ERROR)
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
