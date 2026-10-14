from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import bounded_process
from .state import (
    Ledger,
    age_h,
    claude_usage_fresh,
    default_task_size,
    effective_claude_mode,
    iso,
    load_settings,
    load_claude_rate_limits,
    now,
    reserve_for,
    runtime_seat_home,
    seat_task_min_headroom,
    task_min_headroom,
    usable_headroom,
)
from .seat_selection import pick_seat
from .telemetry import refresh_passive_usage
from .capacity import add_capacity_sample, learning_key, raise_risk_floor
from .claude_isolation import (
    DENIED_TOOLS,
    result_schema_json,
    scrubbed_claude_env,
    structured_claude_result,
    write_isolation_files,
)
from .managed import (
    TASK_WRAPPER,
    WORKER_CONTRACT,
    read_structured_result,
    write_result_schema,
)
from .scope import worker_permission_profile, worker_profile_overrides
from .worker_environment import minimal_worker_env


DEFAULT_TIMEOUT = 1800
DEFAULT_MODEL = "gpt-6-sol"
TESTED_CODEX = "0.153.1"
MANAGED_CLAUDE_ENABLED = True
AUTH_ERR = re.compile(
    r"not logged in|login required|401|unauthorized|invalid.?grant|authentication|"
    r"refresh token.{0,40}(?:revoked|invalidated|invalid|expired)|"
    r"(?:revoked|invalidated|invalid|expired).{0,40}refresh token|token invalidated",
    re.I,
)
RATE_ERR = re.compile(
    r"rate limit|usage limit|quota|429|too many requests|limit reached|"
    r"reached (?:your )?.{0,40} limit|out of extra usage",
    re.I,
)
PERMISSION_ERR = re.compile(r"permission denied|operation not permitted|sandbox.{0,40}(?:deny|blocked)|approval required", re.I)
MODEL_LIMIT_RE = re.compile(r"(?:reached|hit|exceeded).{0,40}\b(fable|opus|sonnet)\b.{0,40}\blimit\b", re.I)
# Safety-classifier refusals (Fable's cyber classifiers can false-positive on
# security/audit work). claude_cli_error_text joins subtype / api_error_status /
# terminal_reason / result one-per-line and returns "" on success — these anchors
# only ever see failed-run fields, never a successful task's prose. Deliberately
# no bare \brefusal\b: a failed run's error text may merely mention the word.
REFUSAL_ERR = re.compile(
    r"^refusal$"
    r"|stop_reason\W{0,20}refusal|refusal\W{0,20}stop_reason"
    r"|unable to respond to this request.{0,80}Usage Policy",
    re.I | re.M,
)


@dataclass
class RunResult:
    code: int
    status: str
    provider: str
    worker: str
    label: str
    duration: int = 0
    tokens: int | None = 0
    log: str | None = None
    message: str | None = None
    fallback_safe: bool = False
    model: str | None = None
    structured_result: dict[str, Any] | None = None
    effort: str | None = None
    effort_downgraded_from: str | None = None


def resolve_executable(name: str, configured: str | None = None) -> str:
    candidates: list[str | None] = [configured]
    if name == "codex":
        candidates.extend(
            [
                os.environ.get("CODEX_BIN"),
                str(Path.home() / ".local/bin/codex"),
                shutil.which(name),
                "/opt/homebrew/bin/codex",
                "/usr/local/bin/codex",
                "/opt/homebrew/lib/node_modules/@openai/codex/node_modules/@openai/"
                "codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex",
            ]
        )
    elif name == "claude":
        candidates.extend(
            [
                os.environ.get("CLAUDE_BIN"),
                shutil.which(name),
                "/opt/homebrew/bin/claude",
                str(Path.home() / ".local/bin/claude"),
            ]
        )
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return os.path.realpath(candidate)
    raise FileNotFoundError(f"{name} executable not found")


def parse_codex_tokens(path: Path, text: str) -> int:
    total = 0
    try:
        for line in path.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            usage = event.get("usage") if isinstance(event, dict) else None
            if isinstance(usage, dict):
                total += int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
    except OSError:
        pass
    if not total:
        matches = re.findall(r"tokens used[^\d]*([\d,]+)", text, re.I)
        if matches:
            total = int(matches[-1].replace(",", ""))
    return total


def parse_claude_token_fields(path: Path) -> dict[str, int]:
    try:
        payload = json.loads(path.read_text(errors="replace"))
    except (OSError, ValueError):
        return {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "uncached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "provider_tokens": 0,
        }
    usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
    usage = usage if isinstance(usage, dict) else {}
    def count(key: str) -> int:
        value = usage.get(key, 0)
        return max(0, int(value)) if isinstance(value, (int, float)) else 0

    uncached = count("input_tokens")
    cached = count("cache_creation_input_tokens") + count("cache_read_input_tokens")
    output = count("output_tokens")
    reasoning = count("reasoning_tokens")
    input_total = uncached + cached
    return {
        "input_tokens": input_total,
        "cached_input_tokens": cached,
        "uncached_input_tokens": uncached,
        "output_tokens": output,
        "reasoning_tokens": reasoning,
        "provider_tokens": input_total + output,
    }


def parse_claude_usage(path: Path) -> tuple[int, float | None]:
    fields = parse_claude_token_fields(path)
    try:
        payload = json.loads(path.read_text(errors="replace"))
    except (OSError, ValueError):
        return 0, None
    cost = payload.get("total_cost_usd") if isinstance(payload, dict) else None
    return fields["provider_tokens"], float(cost) if isinstance(cost, (int, float)) else None


def codex_cli_error_text(path: Path, fallback: str = "") -> str:
    """Return only Codex transport/CLI failures, excluding agent transcript text."""
    messages: list[str] = []
    saw_json = False
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return fallback
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            if line.strip():
                messages.append(line.strip())
            continue
        saw_json = True
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "error" and event.get("message"):
            messages.append(str(event["message"]))
        elif event_type == "turn.failed":
            error = event.get("error")
            if isinstance(error, dict) and error.get("message"):
                messages.append(str(error["message"]))
            elif error:
                messages.append(str(error))
    return "\n".join(messages) if saw_json else fallback


def quarantine_auth(seat: dict[str, Any], error: str) -> None:
    """Persist an authoritative auth failure without retaining token material."""
    lowered = error.lower()
    if "refresh token" in lowered or "token invalidated" in lowered:
        reason = "refresh token revoked or invalidated"
    elif re.search(r"invalid.?grant", error, re.I):
        reason = "invalid grant"
    elif "401" in error or "unauthorized" in lowered:
        reason = "unauthorized"
    else:
        reason = "authentication failed"
    seat["status"] = "needs-login"
    seat["auth_quarantined_at"] = iso()
    seat["auth_failure"] = reason


def recover_auth(seat: dict[str, Any]) -> None:
    """Clear quarantine after an authoritative successful Codex probe."""
    was_needs_login = seat.get("status") == "needs-login"
    seat["auth_quarantined_at"] = None
    seat["auth_failure"] = None
    if was_needs_login:
        if seat.get("status_until") and now() < seat["status_until"]:
            seat["status"] = "capped"
        else:
            seat["status"] = "ok"
            seat["status_until"] = None


def claude_cli_error_text(path: Path, fallback: str = "") -> str:
    """Return Claude's top-level CLI error without scanning successful task output."""
    try:
        payload = json.loads(path.read_text(errors="replace"))
    except (OSError, ValueError):
        return fallback
    if not isinstance(payload, dict):
        return fallback
    if payload.get("is_error") is not True and payload.get("subtype") == "success":
        return ""
    messages = [
        payload.get("subtype"),
        payload.get("api_error_status"),
        payload.get("terminal_reason"),
        payload.get("result"),
    ]
    return "\n".join(str(value) for value in messages if value is not None and value != "")


def on_ac_power() -> bool | None:
    try:
        result = bounded_process.run(
            ["/usr/bin/pmset", "-g", "batt"], timeout=5
        )
    except bounded_process.ProcessError:
        return None
    text = (result.stdout + result.stderr).decode("utf-8", "replace")
    if "AC Power" in text:
        return True
    if "Battery Power" in text:
        return False
    return None


class PowerLease:
    def __init__(self, settings: dict[str, Any], allow_battery: bool = False):
        self.settings = settings
        self.allow_battery = allow_battery
        self.process: subprocess.Popen[bytes] | None = None
        self.held = os.environ.get("CODEX_SEATS_POWER_HELD") == "1"

    def __enter__(self) -> "PowerLease":
        if self.held or not self.settings.get("power", {}).get("caffeinate", True):
            return self
        ac = on_ac_power()
        if ac is False and self.settings.get("power", {}).get("require_ac", True) and not self.allow_battery:
            raise RuntimeError("autonomous runs require AC power; pass --allow-battery to override")
        command = ["/usr/bin/caffeinate", "-i"]
        if ac is True:
            command.append("-s")
        command += ["-w", str(os.getpid())]
        # Long-lived OS power lease: scoped to this context manager and explicitly
        # terminated and reaped in __exit__; it is not a synchronous command.
        self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return self

    def __exit__(self, *_: Any) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass


def normalize_prompt(prompt_or_file: str, label: str | None) -> tuple[str, str]:
    if os.path.isfile(prompt_or_file):
        path = os.path.abspath(prompt_or_file)
        return (
            TASK_WRAPPER.format(path=path),
            label or Path(path).stem,
        )
    short = prompt_or_file[:48] + ("…" if len(prompt_or_file) > 48 else "")
    return prompt_or_file, label or short


def managed_prompt(prompt: str) -> str:
    return WORKER_CONTRACT + "\n\nTask:\n" + prompt


def _context_bytes(root: Path, settings: dict[str, Any], prompt_arg: str, cwd: str | None) -> dict[str, int]:
    spec_bytes = Path(prompt_arg).stat().st_size if os.path.isfile(prompt_arg) else len(prompt_arg.encode())
    primary = settings["pool"]["primary"]
    # A Claude-only pool has no Codex primary and therefore no global guidance.
    global_path = (
        runtime_seat_home(root, primary, settings) / "AGENTS.md" if primary else None
    )
    repository_bytes = 0
    if cwd:
        current = Path(cwd).resolve()
        for directory in (current, *current.parents):
            candidate = directory / "AGENTS.md"
            if candidate.exists():
                repository_bytes += candidate.stat().st_size
                break
    return {
        "global_guidance": (
            global_path.stat().st_size if global_path and global_path.exists() else 0
        ),
        "worker_contract": len(WORKER_CONTRACT.encode()),
        "wrapper": len(TASK_WRAPPER.format(path=os.path.abspath(prompt_arg)).encode()) if os.path.isfile(prompt_arg) else 0,
        "task_specification": spec_bytes,
        "repository_instructions": repository_bytes,
    }


def managed_worker_env(**extra: str) -> dict[str, str]:
    return minimal_worker_env(**extra)


def probe_is_stale(probed_at: str | None, stale_seconds: int) -> bool:
    probe_age = age_h(probed_at)
    return probe_age is None or probe_age * 3600 > stale_seconds


def _set_child_claim(root: Path, worker: str, provider: str, child_pid: int) -> None:
    with Ledger(root) as data:
        target = data["seats"][worker] if provider == "codex" else data["providers"]["claude"]
        if target.get("in_flight") and target["in_flight"].get("dispatcher_pid") == os.getpid():
            target["in_flight"]["child_pid"] = child_pid


def _execute(
    command: list[str],
    output: Path,
    timeout: int,
    cwd: str | None,
    env: dict[str, str],
    root: Path,
    worker: str,
    provider: str,
) -> tuple[str, str, int, int]:
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    started = now()
    status = "completed"
    return_code = 0
    with open(output, "w") as sink:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        # Provider lifecycle exception: this supervised long-lived child is
        # deadline-bound below, group-terminated on timeout, and always waited.
        try:
            group = os.getpgid(process.pid)
        except OSError:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise RuntimeError("could not verify provider process group")
        if group != process.pid:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise RuntimeError("provider did not start in an isolated process group")
        _set_child_claim(root, worker, provider, process.pid)
        try:
            return_code = process.wait(timeout=timeout)
            if return_code != 0:
                status = "failed"
        except subprocess.TimeoutExpired:
            status, return_code = "timeout", 124
            bounded_process.terminate_and_reap(
                process, group, command, term_grace=5
            )
        except BaseException:
            # A managed dispatch cancellation is delivered as an exception so
            # each nested supervisor can stop and reap its own verified child.
            bounded_process.terminate_and_reap(
                process, group, command, term_grace=1
            )
            raise
    text = output.read_text(errors="replace")[-12000:] if output.exists() else ""
    return status, text, return_code, round(now() - started)


def _maybe_notify(settings: dict[str, Any], label: str, status: str, worker: str, duration: int) -> None:
    # Worker/turn/failure notifications are intentionally silent. Only a
    # The controller owns completion notifications for supervised work.
    del settings, label, status, worker, duration


def terminate_claim(claim: dict[str, Any] | None) -> bool:
    if not claim:
        return False
    pid = claim.get("child_pid") or claim.get("dispatcher_pid")
    if not pid:
        return False
    try:
        os.killpg(int(pid), signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(int(pid), signal.SIGTERM)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False


def probe_seats(
    root: Path,
    settings: dict[str, Any],
    targets: list[str],
) -> list[str]:
    """Refresh structured account usage under WatchRefresher's provider lock.

    No terminal, config synchronization, or ledger claim is needed for the
    read-only account path.
    """
    import threading

    from .account import refresh_account_usage
    from .watch import acquire_refresh_lock, release_refresh_lock

    descriptor = acquire_refresh_lock(root, threading.Event())
    if descriptor is None:
        return ["probe: another usage refresh is active; retry shortly"]
    try:
        # The on-disk registry is authoritative over the caller's snapshot.
        del settings
        current_settings, _warnings = load_settings(root)
        return refresh_account_usage(root, current_settings, targets)
    finally:
        release_refresh_lock(descriptor)


def codex_version() -> tuple[str | None, str | None]:
    try:
        binary = resolve_executable("codex")
        result = bounded_process.run([binary, "--version"], timeout=10)
    except (FileNotFoundError, bounded_process.ProcessError) as exc:
        return None, str(exc)
    if result.returncode != 0:
        detail = result.stderr_text(errors="replace").strip()
        return None, detail or f"exit {result.returncode}"
    return result.stdout_text(errors="replace").strip(), None

# worker_config is a low-layer module (no longer imports workers), so this is a
# one-directional dependency.
from .worker_config import (
    _sync_config_file, _config_segment, _worker_isolation_overrides, worker_mcp_overrides,
    expected_config_payload, static_config_payload, persisted_config_payload, sync_codex_config,
)
