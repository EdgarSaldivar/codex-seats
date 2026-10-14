"""Tests for the launchd activation contract — the safety of the plist itself.

The plist is the boundary that decides *when* the unsandboxed runner starts. These assert
it activates only on queued/claimed work and is never a resident daemon, and that ``doctor``
flags a tampered plist. No ``launchctl`` is invoked (those are persistent system changes).
"""

from __future__ import annotations

import plistlib
import subprocess

import pytest
from codex_seats import dispatch_protocol as dp
from codex_seats import launchd_dispatch as ld
from codex_seats.worker_environment import minimal_worker_env
from codex_seats.write_boundary import launch_agent_control_paths

RELEASE_ID = "a" * 64


def _release(root):
    package = root / "releases" / RELEASE_ID / "lib" / "codex_seats"
    package.mkdir(parents=True)
    (package / "dispatch_runner.py").write_text("")
    (root / "current").symlink_to(f"releases/{RELEASE_ID}")


def test_plist_activates_only_on_queue_and_is_not_a_daemon(tmp_path):
    _release(tmp_path)
    plist = ld.render_plist(tmp_path)
    paths = dp.queue_paths(tmp_path)
    assert plist["Label"] == ld.LABEL
    assert plist["QueueDirectories"] == [
        str(paths["inbox"]),
        str(paths["processing"]),
    ]
    assert plist["RunAtLoad"] is False
    assert "KeepAlive" not in plist  # never a resident daemon
    assert "Sockets" not in plist  # no network listener
    assert plist["Umask"] == 0o077
    assert plist["ProgramArguments"][1:3] == ["-m", "codex_seats.dispatch_runner"]
    assert plist["EnvironmentVariables"]["CODEX_SEATS_ROOT"] == str(tmp_path.resolve())


def test_plist_bytes_roundtrip(tmp_path):
    _release(tmp_path)
    assert plistlib.loads(ld.plist_bytes(tmp_path))["Label"] == ld.LABEL


def test_service_path_matches_worker_protection(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert ld.plist_path().name == "io.codex-seats.dispatch.plist"
    assert ld.plist_path() in launch_agent_control_paths()


def test_installed_developer_tool_is_resolvable_in_worker_environment(tmp_path, monkeypatch):
    _release(tmp_path)
    tools = tmp_path / "developer tools"
    tools.mkdir()
    command = tools / "developer-probe"
    command.write_text("#!/bin/sh\nprintf 'developer-path-ok\\n'\n")
    command.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setenv("SYNTHETIC_TOKEN", "must-not-be-forwarded")
    launch_env = ld.render_plist(tmp_path)["EnvironmentVariables"]
    assert "SYNTHETIC_TOKEN" not in launch_env
    result = ld.bounded_process.run(
        ["developer-probe"], timeout=5, env=minimal_worker_env(launch_env), check=True,
    )
    assert result.stdout == b"developer-path-ok\n"


def test_doctor_passes_on_freshly_written_plist(tmp_path, monkeypatch):
    _release(tmp_path)
    written = tmp_path / "agent.plist"
    monkeypatch.setattr(ld, "plist_path", lambda: written)
    # a real, executable interpreter path so the interpreter check passes
    import sys

    monkeypatch.setattr(
        ld, "runner_python", lambda root: __import__("pathlib").Path(sys.executable)
    )
    ld.write_plist(tmp_path)
    checks = ld.doctor(tmp_path)
    failed = [msg for ok, msg in checks if not ok]
    assert failed == [], failed


def test_doctor_flags_tampered_activation(tmp_path, monkeypatch):
    _release(tmp_path)
    written = tmp_path / "agent.plist"
    monkeypatch.setattr(ld, "plist_path", lambda: written)
    plist = ld.render_plist(tmp_path)
    plist["RunAtLoad"] = True  # tamper: make it a load-time daemon
    plist["KeepAlive"] = True  # tamper: make it resident
    written.write_bytes(plistlib.dumps(plist))
    results = {msg: ok for ok, msg in ld.doctor(tmp_path)}
    assert results["RunAtLoad is false (activation only via queue)"] is False
    assert results["no KeepAlive (not a daemon)"] is False


def test_doctor_reports_missing_plist(tmp_path, monkeypatch):
    monkeypatch.setattr(ld, "plist_path", lambda: tmp_path / "nope.plist")
    checks = ld.doctor(tmp_path)
    assert len(checks) == 1 and checks[0][0] is False


def test_write_plist_refuses_symlink_target(tmp_path, monkeypatch):
    _release(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"untouched")
    written = tmp_path / "agent.plist"
    written.symlink_to(outside)
    monkeypatch.setattr(ld, "plist_path", lambda: written)
    with pytest.raises(RuntimeError, match="symlink"):
        ld.write_plist(tmp_path)
    assert outside.read_bytes() == b"untouched"


def test_install_surfaces_bootout_and_bootstrap_failures(tmp_path, monkeypatch):
    _release(tmp_path)
    written = tmp_path / "agent.plist"
    monkeypatch.setattr(ld, "plist_path", lambda: written)
    monkeypatch.setattr(
        ld, "runner_python", lambda _root: __import__("pathlib").Path(__file__)
    )
    calls = []

    def failing_bootout(*args):
        calls.append(args)
        if args[0] == "print":
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", "injected bootout")

    monkeypatch.setattr(ld, "_launchctl", failing_bootout)
    with pytest.raises(RuntimeError, match="bootout failed"):
        ld.install(tmp_path)
    assert [call[0] for call in calls] == ["print", "bootout"]

    def failing_bootstrap(*args):
        if args[0] == "print":
            return subprocess.CompletedProcess(
                args, 1, "", "Could not find service in domain"
            )
        return subprocess.CompletedProcess(args, 1, "", "injected bootstrap")

    monkeypatch.setattr(ld, "_launchctl", failing_bootstrap)
    with pytest.raises(RuntimeError, match="bootstrap failed"):
        ld.install(tmp_path)


def test_launchctl_is_bounded_and_never_uses_a_shell(monkeypatch):
    observed = {}

    def timeout(argv, **kwargs):
        observed["argv"] = argv
        observed.update(kwargs)
        raise ld.bounded_process.ProcessTimeoutError(
            ld.bounded_process.ProcessResult(tuple(argv), -9, b"", b"", 1),
            kwargs["timeout"],
        )

    monkeypatch.setattr(ld.bounded_process, "run", timeout)
    with pytest.raises(RuntimeError, match="timed out"):
        ld._launchctl("print", "gui/1/example")
    assert observed["timeout"] == ld.LAUNCHCTL_TIMEOUT_SECONDS


def test_status_does_not_treat_an_unknown_launchctl_failure_as_absence(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ld, "plist_path", lambda: tmp_path / "missing.plist")
    monkeypatch.setattr(
        ld,
        "_launchctl",
        lambda *_args: subprocess.CompletedProcess(
            _args, 77, "", "permission denied"
        ),
    )
    with pytest.raises(RuntimeError, match="without proving"):
        ld.status(tmp_path)
