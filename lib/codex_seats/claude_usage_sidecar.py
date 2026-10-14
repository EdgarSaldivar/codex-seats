"""Bounded, isolated Claude /usage sidecar for the shared watcher."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import bounded_process
from .state import effective_claude_mode, load_claude_rate_limits
from .worker_environment import minimal_worker_env

def _output_text(value: bytes | str) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value

_SIDECAR_RE = re.compile(r"cxwatch-claude-(\d+)-")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def reap_stale_sidecars(root: Path) -> None:
    """Kill tmux sessions and remove private-tmp dirs left by a watcher whose pid
    is gone. Both are named cxwatch-claude-<pid>-*; a crash/SIGKILL that skips
    close() would otherwise leak them forever (no later run reaped them). Best
    effort — never raises into the caller."""
    try:
        listed = _tmux("list-sessions", "-F", "#{session_name}")
        for name in _output_text(listed.stdout).splitlines():
            match = _SIDECAR_RE.match(name.strip())
            if match and not _pid_alive(int(match.group(1))):
                _tmux("kill-session", "-t", name.strip())
    except OSError:
        pass
    parent = root / "private-tmp"
    try:
        for child in parent.glob("cxwatch-claude-*"):
            match = _SIDECAR_RE.match(child.name)
            if match and not _pid_alive(int(match.group(1))):
                shutil.rmtree(child, ignore_errors=True)
    except OSError:
        pass


_TMUX_TIMEOUT = 10.0  # no single tmux call may block the refresh thread
# A newly created tmux session can briefly lose a list-panes race. Retry that
# local state query exactly once; repeated failures still surface as a dead pane.
_PANE_QUERY_ATTEMPTS = 2
_PANE_QUERY_RETRY_DELAY = 0.1


def _tmux(*arguments: str, timeout: float = _TMUX_TIMEOUT) -> bounded_process.ProcessResult:
    # Bounded and non-raising: a hung tmux server must never wedge the refresh
    # loop or shutdown (cancel_event is only polled between calls). Callers
    # already branch on returncode/output, so a timeout looks like a failed call.
    try:
        return bounded_process.run(["tmux", *arguments], timeout=timeout)
    except bounded_process.ProcessTimeoutError as exc:
        return bounded_process.ProcessResult(
            ("tmux", *arguments), 124, exc.result.stdout, b"tmux timed out", exc.result.pid
        )
    except bounded_process.ProcessLaunchError as exc:
        return bounded_process.ProcessResult(
            ("tmux", *arguments), 127, b"", str(exc).encode(), 0
        )


def _usage_window_versions(root: Path) -> dict[str, float]:
    # The shared loader excludes expired/invalid windows. A global cache update
    # can reflect only weekly usage, so it cannot prove session usage refreshed.
    limits = load_claude_rate_limits(root)
    versions = {}
    for name in ("five_hour", "seven_day"):
        window = limits.get(name)
        value = window.get("observed_at") if isinstance(window, dict) else None
        if isinstance(value, (int, float)):
            versions[name] = float(value)
    return versions


def _trusted_claude_cwd(root: Path, settings: dict[str, Any]) -> Path | None:
    """Pick a directory Claude has already recorded as human-trusted."""
    candidates = [root]
    configured = settings.get("providers", {}).get("claude", {}).get("usage_probe_cwd")
    if isinstance(configured, str) and configured:
        candidates.insert(0, Path(configured).expanduser())
    projects = settings.get("projects", {})
    if isinstance(projects, dict):
        for project in projects.values():
            if isinstance(project, dict) and isinstance(project.get("path"), str):
                candidates.append(Path(project["path"]).expanduser())
    try:
        state = json.loads((Path.home() / ".claude.json").read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    known = state.get("projects") if isinstance(state, dict) else {}
    known = known if isinstance(known, dict) else {}
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        project = known.get(str(resolved))
        if (
            resolved.is_dir()
            and isinstance(project, dict)
            and project.get("hasTrustDialogAccepted") is True
        ):
            return resolved
    return None


class ClaudeUsageSidecar:
    """One isolated, prompt-free Claude TUI used only for the local /usage command."""

    def __init__(
        self,
        root: Path,
        settings: dict[str, Any],
        cancel_event: threading.Event,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.root = root
        self.settings = settings
        self.cancel_event = cancel_event
        self.monotonic = monotonic
        self.sleep = sleep
        self.session = f"cxwatch-claude-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.directory: Path | None = None
        self.started = False
        self.last_error: str | None = None

    def _write_settings(self) -> tuple[Path, Path]:
        parent = self.root / "private-tmp"
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # pid-scoped like the tmux session, so reap_stale_sidecars can clean both
        # after an abnormal exit that skipped close().
        self.directory = Path(
            tempfile.mkdtemp(prefix=f"cxwatch-claude-{os.getpid()}-", dir=parent)
        )
        statusline = Path(__file__).with_name("claude_statusline.py")
        if not statusline.is_file():
            raise OSError("packaged Claude statusline is unavailable")
        payload = {
            "$schema": "https://json.schemastore.org/claude-code-settings.json",
            "statusLine": {
                "type": "command",
                "command": (
                    f"env CODEX_SEATS_ROOT={shlex.quote(str(self.root))} "
                    f"{shlex.quote(sys.executable)} {shlex.quote(str(statusline))}"
                ),
                "padding": 0,
            },
            "permissions": {
                "defaultMode": "dontAsk",
                "allow": [],
                "deny": [],
                "disableBypassPermissionsMode": "disable",
                "disableAutoMode": "disable",
            },
            "enableAllProjectMcpServers": False,
            "enabledMcpjsonServers": [],
            "disabledMcpjsonServers": ["*"],
            "enabledPlugins": {},
            "disableAgents": True,
            "disableSessionPersistence": True,
            "autoMemoryEnabled": False,
            "hooks": {},
        }
        settings_path = self.directory / "settings.json"
        mcp_path = self.directory / "mcp.json"
        settings_path.write_text(json.dumps(payload, sort_keys=True) + "\n")
        mcp_path.write_text('{"mcpServers":{}}\n')
        os.chmod(settings_path, 0o600)
        os.chmod(mcp_path, 0o600)
        return settings_path, mcp_path

    def start(self) -> tuple[bool, str]:
        if self.cancel_event.is_set():
            return False, "canceled"
        if self.started:
            dead = self._pane_dead()
            if dead is None:
                return True, "ready"
            detail = self._exit_detail(dead, self._capture())
            self.close()
            self.last_error = detail
            return False, detail
        mode, reason = effective_claude_mode(self.settings)
        if mode == "off":
            return False, f"disabled{f' ({reason})' if reason else ''}"
        if self.settings.get("providers", {}).get("claude", {}).get("active_usage_refresh", True) is not True:
            return False, "disabled (active Claude usage refresh is off)"
        if shutil.which("tmux") is None:
            return False, "tmux is unavailable"
        configured = str(self.settings.get("providers", {}).get("claude", {}).get("command") or "claude")
        executable = configured if Path(configured).is_file() else shutil.which(configured)
        if not executable:
            return False, f"Claude executable not found: {configured}"
        trusted_cwd = _trusted_claude_cwd(self.root, self.settings)
        if trusted_cwd is None:
            return False, "no previously trusted Claude workspace is available"
        reap_stale_sidecars(self.root)
        try:
            settings_path, mcp_path = self._write_settings()
        except OSError as exc:
            return False, f"cannot create isolated settings: {exc}"
        child_env = minimal_worker_env(
            CODEX_SEATS_ROOT=str(self.root),
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
            CLAUDE_CODE_DISABLE_TELEMETRY="1",
            CLAUDE_CODE_DISABLE_AUTO_MEMORY="1",
            DISABLE_AUTOUPDATER="1",
        )
        command = [
            "env", "-i", *(f"{key}={value}" for key, value in sorted(child_env.items())),
            str(executable),
            "--settings",
            str(settings_path),
            "--setting-sources",
            # Keep only the user's local trust/auth context. Project and local
            # settings stay excluded; explicit settings plus CLI flags still
            # disable tools, MCP, plugins, hooks, agents, and persistence.
            "user",
            "--strict-mcp-config",
            "--mcp-config",
            str(mcp_path),
            "--tools",
            "",
            "--permission-mode",
            "dontAsk",
            "--no-chrome",
            "--name",
            self.session,
        ]
        # Never answer a trust dialog on the user's behalf. Project/local
        # settings remain excluded below even when the working directory is an
        # already trusted project.
        result = _tmux("new-session", "-d", "-s", self.session, "-c", str(trusted_cwd))
        if result.returncode:
            self.close()
            detail = _output_text(result.stderr or result.stdout).strip()
            detail = detail or "tmux start failed"
            return False, detail.splitlines()[-1]
        self.started = True
        _tmux("set-option", "-t", self.session, "remain-on-exit", "on")
        launched = _tmux("send-keys", "-t", self.session, shlex.join(command), "Enter")
        if launched.returncode:
            detail = _output_text(launched.stderr or launched.stdout).strip()
            detail = detail or "cannot launch Claude"
            self.close()
            return False, detail
        return True, "ready"

    def _capture(self) -> str:
        return _output_text(_tmux("capture-pane", "-p", "-t", self.session).stdout)

    def _pane_dead(self) -> int | None:
        for attempt in range(_PANE_QUERY_ATTEMPTS):
            result = _tmux(
                "list-panes", "-t", self.session, "-F", "#{pane_dead} #{pane_dead_status}"
            )
            if not result.returncode:
                break
            if self.cancel_event.is_set():
                return -1
            if attempt + 1 < _PANE_QUERY_ATTEMPTS:
                self.sleep(_PANE_QUERY_RETRY_DELAY)
        else:
            return -1
        fields = _output_text(result.stdout).strip().split()
        if fields and fields[0] == "0":
            return None
        try:
            return int(fields[1]) if len(fields) > 1 else -1
        except ValueError:
            return -1

    @staticmethod
    def _exit_detail(status: int, pane: str) -> str:
        tail = pane.strip().splitlines()[-1] if pane.strip() else ""
        return f"Claude sidecar exited ({status}): {tail}".rstrip(": ")

    def probe(self, *, baseline: dict[str, float] | None = None) -> tuple[bool, str]:
        ready, detail = self.start()
        if not ready:
            self.last_error = detail
            return False, detail
        before = _usage_window_versions(self.root) if baseline is None else baseline
        startup_deadline = self.monotonic() + min(
            10.0,
            float(self.settings.get("pool", {}).get("account_probe_timeout_seconds", 30)),
        )
        tui_ready = False
        while self.monotonic() < startup_deadline and not self.cancel_event.is_set():
            dead = self._pane_dead()
            if dead is not None:
                failure = self._exit_detail(dead, self._capture())
                self.last_error = failure
                return False, failure
            pane = self._capture().lower()
            if any(text in pane for text in ("quick safety check", "trust this folder", "do you trust")):
                self.last_error = "Claude workspace trust is required"
                return False, self.last_error
            if any(text in pane for text in ("❯", "claude code", "welcome")):
                tui_ready = True
                break
            self.sleep(0.2)
        if not tui_ready:
            failure = "canceled" if self.cancel_event.is_set() else "Claude TUI startup timed out"
            self.last_error = failure
            return False, failure
        # This is the only text ever submitted to the sidecar. It is a local TUI
        # command, not a model prompt and therefore consumes no model tokens.
        sent = _tmux("send-keys", "-t", self.session, "-l", "/usage")
        if sent.returncode == 0:
            sent = _tmux("send-keys", "-t", self.session, "Enter")
        if sent.returncode:
            detail = _output_text(sent.stderr or sent.stdout).strip()
            detail = detail or "cannot submit /usage"
            self.last_error = detail
            return False, detail

        timeout = float(self.settings.get("pool", {}).get("account_probe_timeout_seconds", 30))
        deadline = self.monotonic() + timeout
        failure: str | None = None
        palette_confirmed = False
        dialog_closed = False
        dialog_loaded_at: float | None = None
        while self.monotonic() < deadline and not self.cancel_event.is_set():
            pane = self._capture()
            lowered = pane.lower()
            if (
                not palette_confirmed
                and "/usage" in lowered
                and "show session cost" in lowered
            ):
                # The first Enter can open the slash-command palette instead of
                # invoking its selected built-in command.
                _tmux("send-keys", "-t", self.session, "Enter")
                palette_confirmed = True
            if (
                dialog_loaded_at is None
                and "current session" in lowered
                and "current week" in lowered
                and "resets" in lowered
            ):
                dialog_loaded_at = self.monotonic()
            if (
                not dialog_closed
                and dialog_loaded_at is not None
                and self.monotonic() - dialog_loaded_at >= 1.0
            ):
                # Do not scrape the bars. Their presence only confirms the
                # local dialog loaded. A short dwell lets the TUI publish the
                # fetched result before closing it, after which the statusline
                # hook emits the machine-readable rate_limits object.
                _tmux("send-keys", "-t", self.session, "Escape")
                dialog_closed = True
            if "failed to load usage data" in lowered:
                failure = "Claude /usage failed to load usage data"
                break
            if dialog_closed:
                after = _usage_window_versions(self.root)
                incomplete = [
                    name for name in ("five_hour", "seven_day")
                    if not after.get(name, float("-inf")) > before.get(name, float("-inf"))
                ]
                if not incomplete:
                    self.last_error = None
                    return True, "ok"
                failure = "Claude /usage incomplete refresh: " + ", ".join(incomplete)
            dead = self._pane_dead()
            if dead is not None:
                failure = self._exit_detail(dead, pane)
                break
            if any(text in lowered for text in ("not logged in", "please log in", "authentication failed")):
                failure = "Claude authentication is unavailable"
                break
            if "unknown command" in lowered and "/usage" in lowered:
                failure = "Claude /usage command failed"
                break
            self.sleep(0.2)
        _tmux("send-keys", "-t", self.session, "Escape")
        failure = failure or ("canceled" if self.cancel_event.is_set() else "Claude /usage timed out")
        self.last_error = failure
        return False, failure

    def close(self) -> None:
        if self.started:
            _tmux("send-keys", "-t", self.session, "Escape")
            _tmux("kill-session", "-t", self.session)
        self.started = False
        if self.directory is not None:
            shutil.rmtree(self.directory, ignore_errors=True)
            self.directory = None
