"""Offline Remote wake acceptance: real receipt events, stubbed Codex client only."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import sys
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
from unittest.mock import Mock

import pytest

from codex_seats import (
    dispatch_cli,
    dispatch_client,
    dispatch_lease,
    dispatch_protocol,
    head_launcher,
)
from codex_seats import remote_wake as wake
from codex_seats import workers
from codex_seats.worker_environment import minimal_worker_env
from codex_seats.state import default_settings

REAL_POPEN = subprocess.Popen
THREAD = "01234567-89ab-cdef-0123-456789abcdef"
JOB = "a" * 32
OTHER = "b" * 32


@pytest.fixture
def remote(tmp_path, monkeypatch):
    for key in (
        "CODEX_SEATS_MANAGED_WORKER", "CODEX_CONTROL_SEAT", "CODEX_SEATS_HEAD",
        "CODEX_SESSION_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    # Short path for macOS AF_UNIX; no live socket/client is contacted.
    with tempfile.TemporaryDirectory(prefix="wake-", dir="/tmp") as directory:
        home = Path(directory)
        control = home / "app-server-control"
        control.mkdir(mode=0o700)
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(control / "app-server-control.sock"))
            monkeypatch.setenv("CODEX_HOME", directory)
            monkeypatch.setenv("CODEX_THREAD_ID", THREAD)
            resolver = Mock(return_value="/stub/codex")
            monkeypatch.setattr(workers, "resolve_executable", resolver)
            popen = Mock()
            monkeypatch.setattr(wake.subprocess, "Popen", popen)
            dispatch_protocol.ensure_queue(tmp_path)
            yield home, popen, resolver


def arm(tmp_path, jobs=None, timeout=60):
    result = wake.arm(tmp_path, jobs or [JOB], wait_timeout=timeout)
    return tmp_path / "remote-wakes" / result["wake_id"]


def receipt(job=JOB, status="completed", **extras):
    return {"request_id": job, "status": status, **extras}


def parser():
    result = argparse.ArgumentParser()
    dispatch_cli.register(result.add_subparsers(dest="command", required=True))
    return result


@pytest.mark.parametrize("command", ["delegate", "await"])
def test_explicit_parser_defaults_and_deadline(command):
    args = parser().parse_args([command, JOB])
    assert not args.wake_head
    args = parser().parse_args([command, JOB, "--wake-head", "--wait-timeout", "0"])
    assert args.wake_head and args.wait_timeout == 0


@pytest.mark.parametrize("key,value", [
    ("CODEX_SEATS_MANAGED_WORKER", "1"), ("CODEX_SEATS_HEAD", "claude"),
    ("CODEX_SEATS_HEAD", "other"), ("CODEX_CONTROL_SEAT", "seat1"),
    ("CODEX_THREAD_ID", ""), ("CODEX_THREAD_ID", "--thread=other"),
    ("CODEX_THREAD_ID", THREAD + "\n"), ("CODEX_HOME", ""),
    ("CODEX_HOME", "relative/home"), ("CODEX_HOME", "/missing/home"),
])
def test_reject_context_before_submission(tmp_path, remote, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    submit = Mock()
    monkeypatch.setattr(dispatch_client, "submit_task", submit)
    args = parser().parse_args(["delegate", "private prompt", "--wake-head"])
    assert dispatch_cli.cmd_delegate(args, {}, tmp_path) == 2
    submit.assert_not_called()
    remote[1].assert_not_called()
    assert not (tmp_path / "remote-wakes").exists()


def test_plain_shell_refused(remote, monkeypatch):
    monkeypatch.delenv("CODEX_HOME")
    monkeypatch.delenv("CODEX_THREAD_ID")
    with pytest.raises(dispatch_protocol.DispatchError):
        wake.prepare(30)


def test_chatgpt_remote_marker_uses_owned_account_default(remote, monkeypatch):
    home = remote[0]
    monkeypatch.delenv("CODEX_HOME")
    monkeypatch.setenv("CODEX_SESSION_ID", "session-remote")
    monkeypatch.setattr(wake, "_default_codex_home", lambda: home)
    owner = wake.prepare(30)
    assert owner.thread == THREAD
    assert owner.home == str(home)


def test_thread_without_home_or_session_marker_is_refused(remote, monkeypatch):
    monkeypatch.delenv("CODEX_HOME")
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    with pytest.raises(dispatch_protocol.DispatchError):
        wake.prepare(30)


@pytest.mark.parametrize("endpoint", ["ws://localhost:1", "wss://example", "unix:///other", ""])
def test_only_owned_endpoint(remote, endpoint):
    with pytest.raises(dispatch_protocol.DispatchError):
        wake.prepare(30, remote=endpoint)
    remote[2].assert_not_called()


@pytest.mark.parametrize("seconds", [-1, float("nan"), float("inf"), 604801])
def test_invalid_deadline(remote, seconds):
    with pytest.raises(dispatch_protocol.DispatchError):
        wake.prepare(seconds)


def test_refuse_poll_fallback(remote, monkeypatch):
    monkeypatch.setattr(dispatch_client, "receipt_wait_backend", lambda: "poll")
    with pytest.raises(dispatch_protocol.DispatchError):
        wake.prepare(30)


@pytest.mark.parametrize("mutation", ["missing", "file", "symlink", "writable_directory"])
def test_refuse_incompatible_control_socket(remote, mutation):
    home = remote[0]
    endpoint = home / "app-server-control" / "app-server-control.sock"
    if mutation == "writable_directory":
        endpoint.parent.chmod(0o777)
    else:
        endpoint.unlink()
        if mutation == "file":
            endpoint.touch()
        elif mutation == "symlink":
            endpoint.symlink_to(home / "absent")
    with pytest.raises((OSError, dispatch_protocol.DispatchError)):
        wake.prepare(30)


@pytest.mark.parametrize("jobs", [[], ["../bad"], [JOB + "\n"], [JOB] * 33])
def test_job_identity_bounds(tmp_path, remote, jobs):
    with pytest.raises(dispatch_protocol.DispatchError):
        wake.arm(tmp_path, jobs, wait_timeout=10)
    remote[1].assert_not_called()


def test_arm_detaches_without_wait_and_preserves_exact_identity(tmp_path, remote, monkeypatch):
    home, popen, resolver = remote
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("PYTHONPATH", "/untrusted")
    monkeypatch.setenv("NODE_OPTIONS", "--require=/untrusted")
    wait = Mock(side_effect=AssertionError("parent must not wait"))
    monkeypatch.setattr(dispatch_client, "wait_for_receipts", wait)
    directory = arm(tmp_path, [OTHER, JOB, JOB])
    resolver.assert_called_once_with("codex", None)
    argv = popen.call_args.args[0]
    kwargs = popen.call_args.kwargs
    assert argv[1:3] == ["-I", "-c"]
    assert argv[-1] == str(directory)
    assert kwargs["env"]["CODEX_THREAD_ID"] == THREAD
    assert kwargs["env"]["CODEX_HOME"] == str(home)
    for key in ("OPENAI_API_KEY", "PYTHONPATH", "NODE_OPTIONS", "CODEX_SEATS_MANAGED_WORKER"):
        assert key not in kwargs["env"]
    assert kwargs["start_new_session"] and kwargs["close_fds"]
    assert not kwargs["shell"] and kwargs["cwd"] == "/"
    assert all(kwargs[key] == subprocess.DEVNULL for key in ("stdin", "stdout", "stderr"))
    assert directory.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in directory.iterdir())
    data = json.loads((directory / "owner.json").read_text())
    assert data["jobs"] == [JOB, OTHER]
    assert data["thread"] == THREAD and data["home"] == str(home)
    assert "CODEX_THREAD_ID" not in minimal_worker_env()


def test_exact_queue_argv_and_clean_environment(remote):
    home, popen, _ = remote
    popen.return_value.wait.return_value = 0
    owner = wake.prepare(10)
    assert wake._queue(owner, "bounded message") == 0
    assert popen.call_args.args[0] == [
        "/stub/codex", "queue", "--thread", THREAD, "--message", "bounded message",
        "--remote", "unix://",
    ]
    kwargs = popen.call_args.kwargs
    assert kwargs["env"]["CODEX_HOME"] == str(home)
    assert "CODEX_THREAD_ID" not in kwargs["env"]
    assert kwargs["shell"] is False and kwargs["start_new_session"] is True
    assert all(kwargs[key] == subprocess.DEVNULL for key in ("stdin", "stdout", "stderr"))
    popen.return_value.wait.assert_called_once_with(timeout=wake.QUEUE_TIMEOUT)


def test_group_wait_once_and_queue_once(tmp_path, remote, monkeypatch):
    directory = arm(tmp_path, [OTHER, JOB])
    wait = Mock(return_value={JOB: receipt(), OTHER: receipt(OTHER, "failed")})
    queue = Mock(return_value=0)
    monkeypatch.setattr(dispatch_client, "wait_for_receipts", wait)
    monkeypatch.setattr(wake, "_queue", queue)
    wake.run_waiter(directory)
    wait.assert_called_once()
    assert wait.call_args.args == (str(tmp_path.resolve()), [JOB, OTHER])
    assert wait.call_args.kwargs["event_backend"] in {"kqueue", "inotify"}
    assert 0 <= wait.call_args.kwargs["wait_timeout"] <= 60
    queue.assert_called_once()
    assert queue.call_args.args[0].thread == THREAD
    message = queue.call_args.args[1]
    assert f"{JOB}=completed" in message and f"{OTHER}=failed" in message
    assert f"codex-seat await {JOB} {OTHER} --wait-timeout 0 --json" in message
    assert (directory / "terminal_set.json").exists()
    assert (directory / "queue_success.json").exists()
    wake.run_waiter(directory)
    result = wake.arm(tmp_path, [JOB, OTHER], wait_timeout=5)
    assert result["status"] == "duplicate_suppressed"
    assert wait.call_count == queue.call_count == remote[1].call_count == 1


def test_capacity_transition_wakes_once_then_keeps_terminal_wait(
    tmp_path, remote, monkeypatch
):
    directory = arm(tmp_path, [JOB, OTHER])

    def wait(_root, _jobs, **kwargs):
        transition = kwargs["on_transition"]
        transition(JOB, {"state": "waiting_capacity"})
        transition(JOB, {"state": "waiting_capacity"})
        transition(OTHER, {"state": "waiting_capacity"})
        return {JOB: receipt(), OTHER: receipt(OTHER)}

    queue = Mock(return_value=0)
    monkeypatch.setattr(dispatch_client, "wait_for_receipts", Mock(side_effect=wait))
    monkeypatch.setattr(wake, "_queue", queue)

    wake.run_waiter(directory)

    assert queue.call_count == 2
    capacity_message = queue.call_args_list[0].args[1]
    terminal_message = queue.call_args_list[1].args[1]
    assert f"{JOB}=waiting_capacity" in capacity_message
    assert "remains queued" in capacity_message
    assert "cancel it" in capacity_message
    assert OTHER not in capacity_message
    assert "completion wake: terminal_set" in terminal_message
    assert (directory / "capacity_waiting.json").exists()
    assert (directory / "capacity_queue_attempt.json").exists()
    assert (directory / "capacity_queue_success.json").exists()
    assert (directory / "terminal_set.json").exists()
    assert (directory / "queue_success.json").exists()


def test_real_capacity_artifacts_wake_head_and_wait_for_terminal(
    tmp_path, remote, monkeypatch
):
    frame = {"request_id": JOB, "operation": "task"}
    dispatch_protocol.write_request(tmp_path, frame)
    paths = dispatch_protocol.queue_paths(tmp_path)
    dispatch_lease.write_atomic(
        paths["processing"] / f".{JOB}.retry.json",
        {
            "request_id": JOB,
            "request_hash": dispatch_protocol.content_hash(frame),
            "not_before": 1,
        },
    )
    directory = arm(tmp_path)
    capacity_queued = threading.Event()

    def queue(_owner, message):
        if "capacity wake" in message:
            capacity_queued.set()
        return 0

    queued = Mock(side_effect=queue)
    monkeypatch.setattr(wake, "_queue", queued)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(wake.run_waiter, directory)
        try:
            assert capacity_queued.wait(5)
            assert not future.done()
        finally:
            dispatch_protocol.write_receipt(tmp_path, receipt())
        future.result(timeout=5)

    assert queued.call_count == 2
    assert "capacity wake" in queued.call_args_list[0].args[1]
    assert "completion wake" in queued.call_args_list[1].args[1]


def test_capacity_delivery_failure_does_not_abort_terminal_wait(
    tmp_path, remote, monkeypatch
):
    directory = arm(tmp_path)

    def wait(_root, _jobs, **kwargs):
        kwargs["on_transition"](JOB, {"state": "waiting_capacity"})
        return {JOB: receipt()}

    queue = Mock(side_effect=[wake.QueueLaunchError("SECRET_RAW_ERROR"), 0])
    monkeypatch.setattr(dispatch_client, "wait_for_receipts", Mock(side_effect=wait))
    monkeypatch.setattr(wake, "_queue", queue)

    wake.run_waiter(directory)

    assert queue.call_count == 2
    assert (directory / "capacity_queue_failed.json").exists()
    assert (directory / "terminal_set.json").exists()
    assert (directory / "queue_success.json").exists()
    assert "SECRET_RAW_ERROR" not in "".join(
        path.read_text() for path in directory.iterdir()
    )


def test_capacity_write_ahead_failure_never_queues_advisory(
    tmp_path, remote, monkeypatch
):
    directory = arm(tmp_path)

    def wait(_root, _jobs, **kwargs):
        kwargs["on_transition"](JOB, {"state": "waiting_capacity"})
        return {JOB: receipt()}

    original = wake._event

    def event(path, name, **kwargs):
        if name == "capacity_queue_attempt":
            raise OSError("disk full")
        return original(path, name, **kwargs)

    queue = Mock(return_value=0)
    monkeypatch.setattr(dispatch_client, "wait_for_receipts", Mock(side_effect=wait))
    monkeypatch.setattr(wake, "_event", event)
    monkeypatch.setattr(wake, "_queue", queue)

    wake.run_waiter(directory)

    queue.assert_called_once()
    assert "completion wake" in queue.call_args.args[1]
    assert (directory / "capacity_queue_failed.json").exists()
    assert (directory / "terminal_set.json").exists()
    assert not (directory / "watcher_failed.json").exists()


def test_initial_all_unknown_watcher_race_gets_one_bounded_retry(
    tmp_path, remote, monkeypatch
):
    directory = arm(tmp_path)
    wait = Mock(
        side_effect=[
            dispatch_protocol.DispatchError("transient startup race"),
            {JOB: receipt()},
        ]
    )
    sleep = Mock()
    queue = Mock(return_value=0)
    monkeypatch.setattr(dispatch_client, "wait_for_receipts", wait)
    monkeypatch.setattr(wake.time, "sleep", sleep)
    monkeypatch.setattr(wake, "_queue", queue)

    wake.run_waiter(directory)

    assert wait.call_count == 2
    sleep.assert_called_once_with(wake.INITIAL_DISCOVERY_RETRY_DELAY)
    queue.assert_called_once()
    assert (directory / "terminal_set.json").exists()
    assert not (directory / "watcher_failed.json").exists()


def test_discovered_job_watcher_failure_is_not_retried(tmp_path, remote, monkeypatch):
    directory = arm(tmp_path)
    wait = Mock(side_effect=dispatch_protocol.DispatchError("watch failed"))
    status = Mock(return_value={"request_id": JOB, "state": "waiting", "terminal": False})
    sleep = Mock()
    queue = Mock(return_value=0)
    monkeypatch.setattr(dispatch_client, "wait_for_receipts", wait)
    monkeypatch.setattr(dispatch_client, "job_status", status)
    monkeypatch.setattr(wake.time, "sleep", sleep)
    monkeypatch.setattr(wake, "_queue", queue)

    wake.run_waiter(directory)

    wait.assert_called_once()
    sleep.assert_not_called()
    queue.assert_called_once()
    assert (directory / "watcher_failed.json").exists()


def test_real_os_wait_does_not_queue_partial_group(tmp_path, remote, monkeypatch):
    directory = arm(tmp_path, [JOB, OTHER])
    observed_first = threading.Event()
    queue = Mock(return_value=0)
    real_wait = dispatch_client.wait_for_receipts

    def wait(*args, **kwargs):
        capacity_transition = kwargs.pop("on_transition")

        def on_transition(job, snapshot):
            capacity_transition(job, snapshot)
            if job == JOB and snapshot["state"] == "completed":
                observed_first.set()
        return real_wait(*args, **kwargs, on_transition=on_transition)

    wrapped = Mock(side_effect=wait)
    monkeypatch.setattr(dispatch_client, "wait_for_receipts", wrapped)
    monkeypatch.setattr(wake, "_queue", queue)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(wake.run_waiter, directory)
        try:
            dispatch_protocol.write_receipt(tmp_path, receipt())
            assert observed_first.wait(5)
            assert not future.done()
            queue.assert_not_called()
        finally:
            dispatch_protocol.write_receipt(tmp_path, receipt(OTHER))
        future.result(timeout=5)
    assert wrapped.call_count == queue.call_count == 1


@pytest.mark.parametrize("failure", ["deadline", "watcher_failed", "incomplete"])
def test_deadline_and_actionable_failure_notify_once(tmp_path, remote, monkeypatch, failure):
    directory = arm(tmp_path, [JOB, OTHER], timeout=0)
    if failure == "watcher_failed":
        wait = Mock(side_effect=OSError("secret raw watcher log"))
        monkeypatch.setattr(dispatch_client, "wait_for_receipts", wait)
    elif failure == "incomplete":
        wait = Mock(return_value={JOB: receipt()})
        monkeypatch.setattr(dispatch_client, "wait_for_receipts", wait)
    else:
        dispatch_protocol.write_receipt(tmp_path, receipt())
    queue = Mock(return_value=0)
    monkeypatch.setattr(wake, "_queue", queue)
    wake.run_waiter(directory)
    queue.assert_called_once()
    expected = "deadline" if failure == "deadline" else "watcher_failed"
    assert expected in queue.call_args.args[1]
    assert (directory / (expected + ".json")).exists()
    assert "secret" not in "".join(p.read_text() for p in directory.iterdir())
    if failure == "deadline":
        assert f"{JOB}=completed" in queue.call_args.args[1]
        assert f"{OTHER}=unknown" in queue.call_args.args[1]


@pytest.mark.parametrize("status", ["capacity", "disabled"])
def test_provider_terminal_status_is_preserved(tmp_path, remote, monkeypatch, status):
    directory = arm(tmp_path)
    monkeypatch.setattr(
        dispatch_client,
        "wait_for_receipts",
        Mock(return_value={JOB: receipt(status=status)}),
    )
    queue = Mock(return_value=0)
    monkeypatch.setattr(wake, "_queue", queue)

    wake.run_waiter(directory)

    queue.assert_called_once()
    assert f"{JOB}={status}" in queue.call_args.args[1]
    assert (directory / "terminal_set.json").exists()
    assert not (directory / "watcher_failed.json").exists()


@pytest.mark.parametrize("status", ["completed", "timeout", "bad\nSECRET\x1b", ["secret"]])
def test_no_arbitrary_receipt_text_in_wake_or_telemetry(tmp_path, remote, monkeypatch, status):
    directory = arm(tmp_path)
    raw = receipt(status=status, prompt="SECRET", summary="SECRET", error="SECRET")
    monkeypatch.setattr(dispatch_client, "wait_for_receipts", Mock(return_value={JOB: raw}))
    queue = Mock(return_value=0)
    monkeypatch.setattr(wake, "_queue", queue)
    wake.run_waiter(directory)
    assert "SECRET" not in queue.call_args.args[1]
    assert "SECRET" not in "".join(p.read_text() for p in directory.iterdir())


def test_maximum_group_message_bound():
    jobs = [f"{index:032x}" for index in range(wake.MAX_JOBS)]
    statuses = dict.fromkeys(jobs, "blocked_needs_controller")
    message = wake._message(jobs, statuses, "watcher_failed")
    assert len(message.encode()) <= wake.MAX_MESSAGE
    assert all(job in message for job in jobs)


@pytest.mark.parametrize("result", [
    1, -9, subprocess.TimeoutExpired("private command", 30),
    OSError("unknown after spawn"), wake.QueueLaunchError("private raw error"),
])
def test_queue_failures_never_retry(tmp_path, remote, monkeypatch, result):
    directory = arm(tmp_path)
    monkeypatch.setattr(dispatch_client, "wait_for_receipts", Mock(return_value={JOB: receipt()}))
    queue = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
    monkeypatch.setattr(wake, "_queue", queue)
    wake.run_waiter(directory)
    wake.run_waiter(directory)
    assert wake.arm(tmp_path, [JOB], wait_timeout=30)["status"] == "duplicate_suppressed"
    queue.assert_called_once()
    name = "queue_failed" if isinstance(result, wake.QueueLaunchError) else "queue_uncertain"
    assert (directory / (name + ".json")).exists()
    assert "private raw error" not in "".join(p.read_text() for p in directory.iterdir())
    assert "private command" not in "".join(p.read_text() for p in directory.iterdir())


def test_queue_timeout_uses_bounded_cleanup(remote, monkeypatch):
    process = remote[1].return_value
    process.wait.side_effect = subprocess.TimeoutExpired("queue", 30)
    cleanup = Mock()
    monkeypatch.setattr(wake.bounded_process, "terminate_and_reap", cleanup)
    with pytest.raises(subprocess.TimeoutExpired):
        wake._queue(wake.prepare(10), "message")
    cleanup.assert_called_once()
    assert remote[1].call_count == 1


def test_launch_failure_retains_record_and_never_relaunches(tmp_path, remote):
    remote[1].side_effect = OSError("private raw error")
    with pytest.raises(dispatch_protocol.DispatchError, match="do not resubmit"):
        arm(tmp_path)
    result = wake.arm(tmp_path, [JOB], wait_timeout=10)
    assert result["status"] == "duplicate_suppressed"
    directory = tmp_path / "remote-wakes" / result["wake_id"]
    assert (directory / "launch_failed.json").exists()
    remote[1].assert_called_once()


def test_crash_after_attempt_claim_suppresses_delivery(tmp_path, remote, monkeypatch):
    directory = arm(tmp_path)
    wake._event(directory, "wait_started")
    wake._event(directory, "queue_attempt")
    queue = Mock()
    monkeypatch.setattr(wake, "_queue", queue)
    wake.run_waiter(directory)
    queue.assert_not_called()
    assert (directory / "duplicate_suppressed.json").exists()


def test_concurrent_duplicate_arms_launch_only_one(tmp_path, remote):
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: wake.arm(tmp_path, [JOB], wait_timeout=60), range(2)))
    assert sorted(r["status"] for r in results) == ["armed", "duplicate_suppressed"]
    remote[1].assert_called_once()


@pytest.mark.parametrize("key,value", [("CODEX_THREAD_ID", "other"),
                                         ("CODEX_HOME", "/other"),
                                         ("CODEX_SEATS_MANAGED_WORKER", "1"),
                                         ("CODEX_SEATS_HEAD", "claude")])
def test_child_must_still_match_owner(tmp_path, remote, monkeypatch, key, value):
    directory = arm(tmp_path)
    monkeypatch.setenv(key, value)
    queue = Mock()
    monkeypatch.setattr(wake, "_queue", queue)
    with pytest.raises(dispatch_protocol.DispatchError):
        wake.run_waiter(directory)
    queue.assert_not_called()


def test_endpoint_lost_after_arm_fails_closed(tmp_path, remote, monkeypatch):
    directory = arm(tmp_path)
    (remote[0] / "app-server-control" / "app-server-control.sock").unlink()
    monkeypatch.setattr(dispatch_client, "wait_for_receipts", Mock(return_value={JOB: receipt()}))
    queue = Mock()
    monkeypatch.setattr(wake, "_queue", queue)
    wake.run_waiter(directory)
    queue.assert_not_called()
    assert (directory / "queue_failed.json").exists()


@pytest.mark.parametrize("unsafe", ["symlink", "public"])
def test_state_directory_security(tmp_path, remote, unsafe):
    directory = tmp_path / "remote-wakes"
    if unsafe == "symlink":
        directory.symlink_to(tmp_path / "dispatch")
    else:
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
    with pytest.raises(dispatch_protocol.DispatchError):
        arm(tmp_path)
    remote[1].assert_not_called()


def test_failed_write_ahead_prevents_queue(tmp_path, remote, monkeypatch):
    directory = arm(tmp_path)
    monkeypatch.setattr(dispatch_client, "wait_for_receipts", Mock(return_value={JOB: receipt()}))
    original = wake._event

    def event(directory, name, **kwargs):
        if name == "queue_attempt":
            raise OSError("disk full")
        return original(directory, name, **kwargs)

    monkeypatch.setattr(wake, "_event", event)
    queue = Mock()
    monkeypatch.setattr(wake, "_queue", queue)
    with pytest.raises(OSError):
        wake.run_waiter(directory)
    queue.assert_not_called()


def test_delegate_wake_submits_once_and_returns_promptly(tmp_path, remote, monkeypatch, capsys):
    submit = Mock(return_value=JOB)
    monkeypatch.setattr(dispatch_client, "submit_task", submit)
    monkeypatch.setattr(dispatch_client, "wait_for_receipt", Mock(side_effect=AssertionError))
    args = parser().parse_args(["delegate", "SECRET prompt", "--repo", "test", "--wake-head", "--json"])
    assert dispatch_cli.cmd_delegate(args, {}, tmp_path) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["job_ids"] == [JOB] and result["status"] == "armed"
    submit.assert_called_once()
    assert "SECRET" not in "".join(p.read_text() for p in (tmp_path / "remote-wakes").rglob("*.json"))


def test_delegate_arm_failure_reports_submitted_id(tmp_path, remote, monkeypatch, capsys):
    submit = Mock(return_value=JOB)
    monkeypatch.setattr(dispatch_client, "submit_task", submit)
    remote[1].side_effect = OSError("fixture launch failure")
    args = parser().parse_args(["delegate", "task", "--repo", "test", "--wake-head"])
    assert dispatch_cli.cmd_delegate(args, {}, tmp_path) == 2
    assert f"Job {JOB} already submitted; do not resubmit" in capsys.readouterr().err
    submit.assert_called_once()


def test_await_wake_cli_returns_one_group(tmp_path, remote, capsys):
    args = parser().parse_args(["await", JOB, OTHER, "--wake-head", "--json"])
    assert dispatch_cli.cmd_await(args, {}, tmp_path) == 0
    assert json.loads(capsys.readouterr().out)["job_ids"] == [JOB, OTHER]
    remote[1].assert_called_once()


@pytest.mark.parametrize("head", ["claude", "codex"])
def test_legacy_await_never_arms_remote(tmp_path, remote, monkeypatch, head):
    monkeypatch.setenv("CODEX_SEATS_HEAD", head)
    dispatch_protocol.write_receipt(tmp_path, receipt())
    args = parser().parse_args(["await", JOB, "--wait-timeout", "0"])
    assert dispatch_cli.cmd_await(args, {}, tmp_path) == 0
    remote[1].assert_not_called()
    assert not (tmp_path / "remote-wakes").exists()


def test_claude_routing_unchanged_and_codex_describes_grouped_wake(tmp_path):
    argv, env, _ = head_launcher.claude_head_command(
        tmp_path, default_settings(), binary="/stub/claude"
    )
    policy = argv[argv.index("--append-system-prompt") + 1]
    assert policy == head_launcher.render_head_policy({}, codex=False)
    assert "--wake-head" not in policy and "codex queue" not in policy
    assert "call `codex-seat await` once" in policy
    assert env["CODEX_SEATS_HEAD"] == "claude"
    assert "--wake-head" in head_launcher.render_head_policy({}, codex=True)
    codex_policy = head_launcher.render_head_policy({}, codex=True)
    assert "allow the turn to end" in codex_policy
    assert "--wait-timeout 0 --json" in codex_policy


def test_detached_entrypoint_with_offline_fake_codex(tmp_path, remote, monkeypatch):
    """Exercise the actual isolated interpreter, not only mocked Popen arguments."""
    capture = tmp_path / "queue-argv.json"
    binary = tmp_path / "fake-codex"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(capture)!r}, 'w') as stream:\n"
        "    json.dump({'argv': sys.argv[1:], 'home': os.environ['CODEX_HOME']}, stream)\n"
    )
    binary.chmod(0o700)
    remote[2].return_value = str(binary)
    processes = []

    def spawn(*args, **kwargs):
        child = REAL_POPEN(*args, **kwargs)
        processes.append(child)
        return child

    monkeypatch.setattr(wake.subprocess, "Popen", spawn)
    dispatch_protocol.write_receipt(tmp_path, receipt())
    directory = arm(tmp_path)
    child = processes[0]
    try:
        assert child.wait(timeout=10) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
    assert len(processes) == 1
    data = json.loads(capture.read_text())
    assert data['argv'][:3] == ['queue', '--thread', THREAD]
    assert data['argv'][-2:] == ['--remote', 'unix://']
    assert data['home'] == str(remote[0])
    assert (directory / 'queue_success.json').exists()
    assert 'CODEX_THREAD_ID' not in minimal_worker_env()


def test_queue_launch_oserror_is_definite_failure(remote):
    remote[1].side_effect = FileNotFoundError("private raw error")
    with pytest.raises(wake.QueueLaunchError, match="queue_launch_failed"):
        wake._queue(wake.prepare(10), "message")


def test_waiter_concurrent_entry_claims_once(tmp_path, remote, monkeypatch):
    directory = arm(tmp_path)
    wait = Mock(return_value={JOB: receipt()})
    queue = Mock(return_value=0)
    monkeypatch.setattr(dispatch_client, "wait_for_receipts", wait)
    monkeypatch.setattr(wake, "_queue", queue)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: wake.run_waiter(directory), range(2)))
    wait.assert_called_once()
    queue.assert_called_once()


def test_different_owners_do_not_adopt_each_others_wake(tmp_path, remote, monkeypatch):
    first = arm(tmp_path)
    monkeypatch.setenv("CODEX_THREAD_ID", "fedcba98-7654-3210-fedc-ba9876543210")
    second = arm(tmp_path)
    assert first != second
    assert remote[1].call_count == 2


def test_unsafe_owner_record_refused(tmp_path, remote, monkeypatch):
    directory = arm(tmp_path)
    record = directory / "owner.json"
    record.chmod(0o644)
    queue = Mock()
    monkeypatch.setattr(wake, "_queue", queue)
    with pytest.raises(dispatch_protocol.DispatchError):
        wake.run_waiter(directory)
    queue.assert_not_called()


@pytest.mark.parametrize("key,value", [("CODEX_SEATS_MANAGED_WORKER", "1"),
                                         ("CODEX_SEATS_HEAD", "claude"),
                                         ("CODEX_THREAD_ID", "")])
def test_unauthorized_internal_entry_does_not_read_or_write_state(tmp_path, remote, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    read = Mock(side_effect=AssertionError("must not read"))
    event = Mock(side_effect=AssertionError("must not write"))
    monkeypatch.setattr(wake, "_read", read)
    monkeypatch.setattr(wake, "_event", event)
    monkeypatch.setattr(sys, "argv", ["waiter", str(tmp_path)])
    with pytest.raises(dispatch_protocol.DispatchError):
        wake.main()
    read.assert_not_called()
    event.assert_not_called()


def test_directory_durability_precedes_detached_launch(tmp_path, remote, monkeypatch):
    synced = []
    original = wake._sync_directory

    def sync(path):
        original(path)
        synced.append(path)

    def launch(*args, **kwargs):
        assert synced[:2] == [tmp_path.resolve(), tmp_path.resolve() / "remote-wakes"]
        assert len(synced) >= 4  # owner and arm records also durable before launch

    monkeypatch.setattr(wake, "_sync_directory", sync)
    remote[1].side_effect = launch
    arm(tmp_path)
    remote[1].assert_called_once()


def test_cli_rejects_claude_wake_await_before_wait(tmp_path, remote, monkeypatch):
    monkeypatch.setenv("CODEX_SEATS_HEAD", "claude")
    wait = Mock(side_effect=AssertionError("must not wait"))
    monkeypatch.setattr(dispatch_client, "wait_for_receipts", wait)
    args = parser().parse_args(["await", JOB, "--wake-head"])
    assert dispatch_cli.cmd_await(args, {}, tmp_path) == 2
    wait.assert_not_called()
    remote[1].assert_not_called()
