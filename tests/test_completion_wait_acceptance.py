"""Offline acceptance coverage for completion-driven dispatch waits.

Queue submission, runner receipt publication, notification-backed client waits, and
CLI return values are real repository code.  ``_SimulatedConversationBoundary`` is
deliberately only a counter around the CLI call: it does not represent or verify a
live Codex/ChatGPT runtime.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from dataclasses import dataclass, field

import pytest

from codex_seats import dispatch_cli
from codex_seats import dispatch_client as client
from codex_seats import dispatch_protocol as dp
from codex_seats import dispatch_runner as runner
from codex_seats.workers import RunResult
from codex_seats.worktree_task import TaskArtifact
from registry_fixture import registry_settings, write_settings


NOW = 1_200.0
RELEASE_ID = "a" * 64


@dataclass
class _SimulatedConversationBoundary:
    """Count a modeled resumption only when one blocking CLI tool call returns."""

    modeled_resumptions: int = 0
    exit_code: int | None = None
    error: BaseException | None = None
    returned: threading.Event = field(default_factory=threading.Event)

    def invoke(self, call) -> None:
        try:
            self.exit_code = call()
        except BaseException as exc:  # retain thread failures for the test thread
            self.error = exc
        finally:
            self.modeled_resumptions += 1
            self.returned.set()


def _settings(root):
    package = root / "releases" / RELEASE_ID / "lib" / "codex_seats"
    package.mkdir(parents=True)
    (root / "current").symlink_to(f"releases/{RELEASE_ID}")
    repo = root / "repo"
    repo.mkdir()
    settings = registry_settings()
    settings["projects"] = {"sample-project": {"path": str(repo)}}
    return write_settings(root, settings)


def _parse(*arguments: str):
    parser = argparse.ArgumentParser()
    dispatch_cli.register(parser.add_subparsers(dest="command", required=True))
    return parser.parse_args(arguments)


def _artifact(request, status: str) -> TaskArtifact:
    message = "worker completed" if status == "completed" else "worker tests failed"
    return TaskArtifact(
        result=RunResult(
            code=0 if status == "completed" else 1,
            status=status,
            provider="codex",
            worker="seat4",
            label=request["label"],
            message=message,
        )
    )


def _join(thread: threading.Thread, boundary: _SimulatedConversationBoundary) -> None:
    thread.join(timeout=2)
    assert not thread.is_alive(), "blocking CLI call did not return"
    if boundary.error is not None:
        raise boundary.error


@pytest.mark.parametrize(
    ("terminal_status", "expected_exit"),
    (("completed", 0), ("failed", 1)),
)
def test_blocking_delegate_resumes_once_only_after_terminal_runner_receipt(
    tmp_path, monkeypatch, capsys, terminal_status, expected_exit
):
    settings = _settings(tmp_path)
    assert client.receipt_wait_backend() in {"kqueue", "inotify"}
    submitted = threading.Event()
    wait_started = threading.Event()
    submitted_id = []
    original_submit = client.submit_task
    original_wait = client.dispatch_events.QueueEventWaiter.wait

    def observed_submit(*args, **kwargs):
        request_id = original_submit(*args, **kwargs)
        submitted_id.append(request_id)
        submitted.set()
        return request_id

    def observed_wait(waiter, timeout):
        wait_started.set()
        return original_wait(waiter, timeout)

    # This acceptance fixture exercises head-side CLI behavior in a disposable root;
    # it never delegates a real worker from this managed test process.
    monkeypatch.delenv("CODEX_SEATS_MANAGED_WORKER", raising=False)
    monkeypatch.setattr(client, "submit_task", observed_submit)
    monkeypatch.setattr(client.dispatch_events.QueueEventWaiter, "wait", observed_wait)
    args = _parse(
        "delegate",
        "exercise the completion wait",
        "--repo",
        "sample-project",
        "--wait-timeout",
        "2",
        "--json",
    )
    boundary = _SimulatedConversationBoundary()
    cli_thread = threading.Thread(
        target=boundary.invoke,
        args=(lambda: dispatch_cli.cmd_delegate(args, settings, tmp_path),),
    )
    cli_thread.start()

    assert submitted.wait(timeout=1), "delegate did not publish its request"
    assert wait_started.wait(timeout=1), "delegate did not enter its event wait"
    assert not boundary.returned.is_set()
    assert boundary.modeled_resumptions == 0
    assert cli_thread.is_alive()

    def execute(root, current_settings, request, *, retain):
        del root, current_settings, retain
        return _artifact(request, terminal_status)

    outcomes = runner.drain_once(
        tmp_path, settings, now_fn=lambda: NOW, execute=execute
    )
    _join(cli_thread, boundary)

    request_id = submitted_id[0]
    assert outcomes == [(request_id, "completed")]
    assert boundary.modeled_resumptions == 1
    assert boundary.exit_code == expected_exit
    receipt = dp.read_receipt(tmp_path, request_id)
    assert receipt is not None
    assert receipt["status"] == terminal_status
    assert receipt["summary"] == (
        "worker completed" if terminal_status == "completed" else "worker tests failed"
    )
    assert json.loads(capsys.readouterr().out) == receipt


def test_multi_job_await_does_not_resume_for_a_partial_receipt(
    tmp_path, monkeypatch, capsys
):
    settings = _settings(tmp_path)
    monkeypatch.delenv("CODEX_SEATS_MANAGED_WORKER", raising=False)
    request_ids = [
        client.submit_task(
            tmp_path,
            settings,
            repo_id="sample-project",
            prompt=f"job {index}",
            origin_head="codex",
            now=NOW,
        )
        for index in range(2)
    ]
    args = _parse("await", *request_ids, "--wait-timeout", "2", "--json")
    boundary = _SimulatedConversationBoundary()
    wait_started = threading.Event()
    original_wait = client.dispatch_events.QueueEventWaiter.wait

    def observed_wait(waiter, timeout):
        wait_started.set()
        return original_wait(waiter, timeout)

    monkeypatch.setattr(client.dispatch_events.QueueEventWaiter, "wait", observed_wait)
    cli_thread = threading.Thread(
        target=boundary.invoke,
        args=(lambda: dispatch_cli.cmd_await(args, settings, tmp_path),),
    )
    cli_thread.start()
    assert wait_started.wait(timeout=1), "await did not enter its event wait"

    release_second = threading.Event()
    second_started = threading.Event()
    execution_count = 0

    def execute(root, current_settings, request, *, retain):
        nonlocal execution_count
        del root, current_settings, retain
        execution_count += 1
        if execution_count == 2:
            second_started.set()
            assert release_second.wait(timeout=2)
        return _artifact(request, "completed")

    runner_error = []

    def drain():
        try:
            runner.drain_once(tmp_path, settings, now_fn=lambda: NOW, execute=execute)
        except BaseException as exc:
            runner_error.append(exc)

    runner_thread = threading.Thread(target=drain)
    runner_thread.start()
    try:
        assert second_started.wait(timeout=1), "runner did not reach the second job"
        receipts = [dp.read_receipt(tmp_path, request_id) for request_id in request_ids]
        assert sum(receipt is not None for receipt in receipts) == 1
        assert not boundary.returned.is_set()
        assert boundary.modeled_resumptions == 0
    finally:
        release_second.set()

    runner_thread.join(timeout=2)
    assert not runner_thread.is_alive()
    if runner_error:
        raise runner_error[0]
    _join(cli_thread, boundary)

    assert boundary.modeled_resumptions == 1
    assert boundary.exit_code == 0
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [receipt["request_id"] for receipt in output] == request_ids


def test_dispatched_job_await_resumes_once_at_explicit_deadline(
    tmp_path, monkeypatch, capsys
):
    settings = _settings(tmp_path)
    request_id = client.submit_task(
        tmp_path,
        settings,
        repo_id="sample-project",
        prompt="remain pending through the client deadline",
        origin_head="codex",
        now=time.time(),
    )
    args = _parse("await", request_id, "--wait-timeout", "1", "--json")
    boundary = _SimulatedConversationBoundary()
    wait_started = threading.Event()
    original_wait = client.dispatch_events.QueueEventWaiter.wait

    def observed_wait(waiter, timeout):
        wait_started.set()
        return original_wait(waiter, timeout)

    monkeypatch.setattr(client.dispatch_events.QueueEventWaiter, "wait", observed_wait)
    cli_thread = threading.Thread(
        target=boundary.invoke,
        args=(lambda: dispatch_cli.cmd_await(args, settings, tmp_path),),
    )
    cli_thread.start()

    assert wait_started.wait(timeout=1), "await did not enter its blocking event wait"
    assert not boundary.returned.is_set()
    assert boundary.modeled_resumptions == 0
    _join(cli_thread, boundary)

    assert boundary.error is None
    assert boundary.modeled_resumptions == 1
    assert boundary.exit_code == dispatch_cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"timed out waiting for receipt(s): {request_id}" in captured.err
    assert client.job_status(tmp_path, request_id)["state"] == "waiting"
