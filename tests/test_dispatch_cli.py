"""Tests for the head-side dispatch command helpers."""

from __future__ import annotations

import argparse
import json
import re

import pytest

from codex_seats import dispatch_cli
from codex_seats import dispatch_client
from codex_seats import dispatch_protocol


NOW = 1_000.0
RELEASE_ID = "a" * 64


@pytest.fixture(autouse=True)
def unmanaged_dispatch_client(monkeypatch):
    monkeypatch.delenv("CODEX_SEATS_MANAGED_WORKER", raising=False)
    monkeypatch.delenv("CODEX_CONTROL_SEAT", raising=False)
    monkeypatch.delenv("CODEX_SEATS_HEAD", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_TURN_ID", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)


def _settings(tmp_path):
    package = tmp_path / "releases" / RELEASE_ID / "lib" / "codex_seats"
    package.mkdir(parents=True, exist_ok=True)
    current = tmp_path / "current"
    if not current.exists():
        current.symlink_to(f"releases/{RELEASE_ID}")
    return {"projects": {"sample-project": {"path": str(tmp_path)}}}


def _delegate_args(**overrides):
    values = {
        "prompt": "Implement the bounded change.",
        "repo": "sample-project",
        "provider": "auto",
        "kind": "implementation",
        "size": "big",
        "ttl": 600,
        "timeout": 1800,
        "detach": False,
        "json": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_register_adds_delegate_await_and_job_parsers():
    parser = argparse.ArgumentParser()
    dispatch_cli.register(parser.add_subparsers(dest="command", required=True))

    delegate = parser.parse_args(["delegate", "do work"])
    assert delegate.handler == "delegate"
    assert delegate.provider == "auto"
    assert delegate.kind == "implementation"
    assert delegate.task_shape is None
    assert delegate.validation is None
    assert delegate.task_id is None
    assert delegate.size == "auto"
    assert delegate.effort is None
    assert delegate.read_only is False
    assert delegate.mcp == []
    assert delegate.spend_reserve is None
    assert delegate.from_current_user_request is False
    assert delegate.queue_timeout == dispatch_cli.DEFAULT_QUEUE_TIMEOUT_SECONDS
    assert delegate.wait_timeout == dispatch_cli.DEFAULT_WAIT_TIMEOUT_SECONDS

    routed = parser.parse_args(
        [
            "delegate",
            "investigate",
            "--task-shape",
            "investigate",
            "--validation",
            "weak",
            "--task-id",
            "logical-task:17",
        ]
    )
    assert routed.task_shape == "investigate"
    assert routed.validation == "weak"
    assert routed.task_id == "logical-task:17"

    local = parser.parse_args(["delegate", "do local work", "--provider", "local"])
    assert local.provider == "local"

    await_jobs = parser.parse_args(["await", "a", "b"])
    assert await_jobs.handler == "await_jobs"
    assert await_jobs.job_ids == ["a", "b"]
    assert await_jobs.wait_timeout == dispatch_cli.DEFAULT_WAIT_TIMEOUT_SECONDS

    status = parser.parse_args(["job", "status", "a" * 32, "--json"])
    assert status.handler == "job"
    assert status.job_action == "status"
    tail = parser.parse_args(["job", "tail", "a" * 32, "--lines", "7"])
    assert tail.lines == 7
    cancel = parser.parse_args(["job", "cancel", "a" * 32])
    assert cancel.job_action == "cancel"
    listing = parser.parse_args(["job", "list", "--json"])
    assert listing.job_action == "list"
    watched = parser.parse_args(["job", "list", "--watch"])
    assert watched.watch is True


def test_retired_native_canary_flags_and_command_are_rejected():
    # The development-v1 native-canary machinery is removed; argparse rejects
    # its retired flags and the clear-canary job command as unknown.
    parser = argparse.ArgumentParser()
    dispatch_cli.register(parser.add_subparsers(dest="command", required=True))
    for argv in (
        ["delegate", "claude-native-smoke-v1", "--native-canary"],
        ["delegate", "work", "--expect-source-revision", "a" * 40],
        ["delegate", "work", "--expect-provider-version", "1.2.3"],
        ["delegate", "work", "--evidence", "ref"],
        ["job", "clear-canary", "a" * 32],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_timeout_flag_names_and_compatibility_aliases_are_separate():
    parser = argparse.ArgumentParser()
    dispatch_cli.register(parser.add_subparsers(dest="command", required=True))

    named = parser.parse_args(
        ["delegate", "work", "--queue-timeout", "11", "--wait-timeout", "22"]
    )
    aliases = parser.parse_args(
        ["delegate", "work", "--ttl", "33", "--timeout", "44"]
    )
    awaited = parser.parse_args(["await", "job", "--timeout", "55"])
    assert (named.queue_timeout, named.wait_timeout) == (11, 22)
    assert (aliases.queue_timeout, aliases.wait_timeout) == (33, 44)
    assert awaited.wait_timeout == 55

    help_text = parser.format_help() + parser._subparsers._group_actions[0].choices[
        "delegate"
    ].format_help()
    normalized_help = " ".join(help_text.split())
    assert "expires_at" in normalized_help
    assert "client waits" in normalized_help
    assert "provider execution timeout" in normalized_help
    assert "not a filesystem grant" in normalized_help
    assert "no repository or external write access" in normalized_help
    assert "committed MCP catalog" in normalized_help


def test_head_identity_defaults_to_codex_with_control_seat():
    assert dispatch_cli.head_identity({"CODEX_CONTROL_SEAT": "seat4"}) == (
        "codex",
        "seat4",
    )


def test_head_identity_defaults_to_codex_without_control_seat():
    assert dispatch_cli.head_identity({}) == ("codex", None)


def test_head_identity_respects_explicit_head():
    env = {"CODEX_CONTROL_SEAT": "seat2", "CODEX_SEATS_HEAD": "claude"}
    assert dispatch_cli.head_identity(env) == ("claude", "seat2")


def test_seatless_remote_controller_uses_configured_primary():
    settings = {"pool": {"primary": "seat4"}}
    remote = {"CODEX_THREAD_ID": "thread", "CODEX_HOME": "/codex/home"}
    assert dispatch_cli.head_identity(remote, settings) == ("codex", "seat4")
    assert dispatch_cli.head_identity({"CODEX_SEATS_HEAD": "claude"}, settings) == (
        "claude",
        None,
    )


def test_resolve_repo_id_explicit_wins(tmp_path):
    assert dispatch_cli.resolve_repo_id({}, tmp_path, "chosen") == "chosen"


def test_resolve_repo_id_matches_parent_path(tmp_path):
    nested = tmp_path / "src" / "package"
    nested.mkdir(parents=True)
    assert dispatch_cli.resolve_repo_id(_settings(tmp_path), nested) == "sample-project"


def test_resolve_repo_id_rejects_unknown_cwd(tmp_path):
    project = tmp_path / "project"
    unknown = tmp_path / "unknown"
    project.mkdir()
    unknown.mkdir()
    settings = {"projects": {"sample-project": {"path": str(project)}}}

    with pytest.raises(dispatch_protocol.DispatchError, match="registered project"):
        dispatch_cli.resolve_repo_id(settings, unknown)


def test_cmd_delegate_submits_valid_frame_and_returns_completed(
    tmp_path, monkeypatch, capsys
):
    settings = _settings(tmp_path)
    waited = {}

    def fake_wait(root, request_id, *, wait_timeout):
        waited.update(root=root, request_id=request_id, wait_timeout=wait_timeout)
        return {"request_id": request_id, "status": "completed"}

    monkeypatch.delenv("CODEX_CONTROL_SEAT", raising=False)
    monkeypatch.delenv("CODEX_SEATS_HEAD", raising=False)
    monkeypatch.setattr(dispatch_cli.time, "time", lambda: NOW)
    monkeypatch.setattr(dispatch_client, "wait_for_receipt", fake_wait)

    result = dispatch_cli.cmd_delegate(_delegate_args(), settings, tmp_path)

    assert result == 0
    assert waited["root"] == tmp_path
    assert waited["wait_timeout"] == 1800
    ready = (
        dispatch_protocol.queue_paths(tmp_path)["inbox"]
        / f"{waited['request_id']}.ready"
    )
    request = dispatch_protocol.read_request_secure(ready)
    dispatch_protocol.validate_request(request, settings, now=NOW)
    assert request["prompt"] == "Implement the bounded change."
    assert request["origin"] == {"head": "codex"}
    assert request["repo_id"] == "sample-project"
    assert capsys.readouterr().out.strip().endswith("completed")


def test_cmd_delegate_threads_read_only_into_the_frame(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(dispatch_cli.time, "time", lambda: NOW)
    args = _delegate_args(read_only=True, detach=True)

    assert dispatch_cli.cmd_delegate(args, settings, tmp_path) == 0

    ready = next(dispatch_protocol.queue_paths(tmp_path)["inbox"].glob("*.ready"))
    request = dispatch_protocol.read_request_secure(ready)
    assert request["read_only"] is True


def test_cmd_delegate_threads_named_mcp_grants_into_the_frame(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(dispatch_cli.time, "time", lambda: NOW)
    args = _delegate_args(mcp=["visual", "browser"], detach=True)

    assert dispatch_cli.cmd_delegate(args, settings, tmp_path) == 0

    ready = next(dispatch_protocol.queue_paths(tmp_path)["inbox"].glob("*.ready"))
    request = dispatch_protocol.read_request_secure(ready)
    assert request["mcp"] == ["visual", "browser"]


def test_cmd_delegate_threads_audited_effort_override(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(dispatch_cli.time, "time", lambda: NOW)
    args = _delegate_args(
        effort="max",
        override_reason="high-stakes interaction",
        detach=True,
    )

    assert dispatch_cli.cmd_delegate(args, settings, tmp_path) == 0
    ready = next(dispatch_protocol.queue_paths(tmp_path)["inbox"].glob("*.ready"))
    request = dispatch_protocol.read_request_secure(ready)
    assert request["effort"] == "max"
    assert request["override_reason"] == "high-stakes interaction"
    assert request["protocol_version"] == 8


def test_cmd_delegate_threads_routing_facts_and_task_identity(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(dispatch_cli.time, "time", lambda: NOW)
    args = _delegate_args(
        task_shape="design",
        validation="weak",
        task_id="logical-task:17",
        detach=True,
    )

    assert dispatch_cli.cmd_delegate(args, settings, tmp_path) == 0
    ready = next(dispatch_protocol.queue_paths(tmp_path)["inbox"].glob("*.ready"))
    request = dispatch_protocol.read_request_secure(ready)
    assert request["task_shape"] == "design"
    assert request["validation"] == "weak"
    assert request["task_id"] == "logical-task:17"
    assert request["protocol_version"] == request["protocol_min"] == 8


def test_cmd_delegate_threads_confirmed_reserve_authority(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(dispatch_cli.time, "time", lambda: NOW)
    args = _delegate_args(
        provider="codex",
        spend_reserve="seat2",
        from_current_user_request=True,
        detach=True,
    )

    assert dispatch_cli.cmd_delegate(args, settings, tmp_path) == 0
    ready = next(dispatch_protocol.queue_paths(tmp_path)["inbox"].glob("*.ready"))
    request = dispatch_protocol.read_request_secure(ready)
    assert request["reserve_spend_authority"]["seat"] == "seat2"
    assert request["protocol_version"] == 8


def test_managed_worker_cannot_self_authorize_reserve_spending(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("CODEX_SEATS_MANAGED_WORKER", "1")
    result = dispatch_cli.cmd_delegate(
        _delegate_args(
            provider="codex",
            spend_reserve="seat2",
            from_current_user_request=True,
            detach=True,
        ),
        _settings(tmp_path),
        tmp_path,
    )
    assert result == dispatch_cli.EXIT_ERROR
    assert "managed workers" in capsys.readouterr().err


def test_normal_receipt_surfaces_changeset_identity(capsys):
    change_id = dispatch_protocol.new_request_id()
    dispatch_cli._print_receipt(
        {
            "request_id": dispatch_protocol.new_request_id(),
            "status": "completed",
            "changeset_id": change_id,
        },
        as_json=False,
    )
    assert f"changeset={change_id}" in capsys.readouterr().out


def test_cmd_delegate_detaches_without_waiting(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path)

    def unexpected_wait(*args, **kwargs):
        raise AssertionError("detached delegate must not wait")

    monkeypatch.delenv("CODEX_CONTROL_SEAT", raising=False)
    monkeypatch.delenv("CODEX_SEATS_HEAD", raising=False)
    monkeypatch.setattr(dispatch_cli.time, "time", lambda: NOW)
    monkeypatch.setattr(dispatch_client, "wait_for_receipt", unexpected_wait)

    result = dispatch_cli.cmd_delegate(
        _delegate_args(detach=True), settings, tmp_path
    )

    assert result == 0
    request_id = capsys.readouterr().out.strip()
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)
    ready = dispatch_protocol.queue_paths(tmp_path)["inbox"] / f"{request_id}.ready"
    assert ready.is_file()


def test_cmd_delegate_refuses_managed_worker(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_SEATS_MANAGED_WORKER", "1")
    result = dispatch_cli.cmd_delegate(
        _delegate_args(detach=True), _settings(tmp_path), tmp_path
    )
    assert result == dispatch_cli.EXIT_ERROR
    assert "managed workers" in capsys.readouterr().err


def test_cmd_delegate_attributes_remote_controller_to_primary(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    settings["pool"] = {"primary": "seat3"}
    monkeypatch.setenv("CODEX_THREAD_ID", "remote-thread")
    monkeypatch.setenv("CODEX_HOME", "/codex/home")
    captured = {}

    def submit(*_args, **kwargs):
        captured.update(kwargs)
        return "a" * 32

    monkeypatch.setattr(dispatch_client, "submit_task", submit)
    assert (
        dispatch_cli.cmd_delegate(
            _delegate_args(detach=True), settings, tmp_path
        )
        == 0
    )
    assert (captured["origin_head"], captured["origin_seat"]) == (
        "codex",
        "seat3",
    )


def test_cmd_await_returns_zero_when_all_receipts_completed(tmp_path, capsys):
    request_ids = [dispatch_protocol.new_request_id() for _ in range(2)]
    for request_id in request_ids:
        dispatch_protocol.write_receipt(
            tmp_path, {"request_id": request_id, "status": "completed"}
        )
    args = argparse.Namespace(job_ids=request_ids, timeout=1, json=True)

    assert dispatch_cli.cmd_await(args, {}, tmp_path) == 0
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [receipt["request_id"] for receipt in output] == request_ids


def test_cmd_await_returns_one_when_a_receipt_is_not_completed(tmp_path, capsys):
    completed_id = dispatch_protocol.new_request_id()
    failed_id = dispatch_protocol.new_request_id()
    dispatch_protocol.write_receipt(
        tmp_path, {"request_id": completed_id, "status": "completed"}
    )
    dispatch_protocol.write_receipt(
        tmp_path, {"request_id": failed_id, "status": "failed"}
    )
    args = argparse.Namespace(
        job_ids=[completed_id, failed_id], timeout=1, json=False
    )

    assert dispatch_cli.cmd_await(args, {}, tmp_path) == 1
    assert len(capsys.readouterr().out.splitlines()) == 2


def test_cmd_await_prints_completed_receipts_before_timeout(tmp_path, capsys):
    missing_id = dispatch_protocol.new_request_id()
    completed_id = dispatch_protocol.new_request_id()
    completed = {"request_id": completed_id, "status": "completed"}
    dispatch_protocol.write_receipt(tmp_path, completed)
    args = argparse.Namespace(
        job_ids=[missing_id, completed_id], timeout=0, json=True
    )

    assert dispatch_cli.cmd_await(args, {}, tmp_path) == 2
    captured = capsys.readouterr()
    assert [json.loads(line) for line in captured.out.splitlines()] == [completed]
    assert missing_id in captured.err
    assert completed_id not in captured.err


def test_cmd_job_status_has_stable_success_and_unknown_exits(tmp_path, capsys):
    known = dispatch_protocol.new_request_id()
    dispatch_protocol.write_receipt(
        tmp_path, {"request_id": known, "status": "completed"}
    )
    args = argparse.Namespace(job_action="status", job_id=known, json=True)
    assert dispatch_cli.cmd_job(args, {}, tmp_path) == dispatch_cli.EXIT_SUCCESS
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == 1 and payload["ok"] is True
    assert payload["data"]["state"] == "completed"

    unknown = dispatch_protocol.new_request_id()
    args = argparse.Namespace(job_action="status", job_id=unknown, json=False)
    assert dispatch_cli.cmd_job(args, {}, tmp_path) == dispatch_cli.EXIT_UNKNOWN
    assert capsys.readouterr().out.strip().endswith("unknown")
    args.json = True
    assert dispatch_cli.cmd_job(args, {}, tmp_path) == dispatch_cli.EXIT_UNKNOWN
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["error"]["code"] == "unknown-job"
    assert payload["data"]["state"] == "unknown"


def test_cmd_job_cancel_maps_unknown_and_errors_to_stable_exits(
    tmp_path, monkeypatch, capsys
):
    settings = _settings(tmp_path)
    target = dispatch_protocol.new_request_id()
    monkeypatch.setattr(dispatch_cli.time, "time", lambda: NOW)

    def unknown_receipt(root, request_id, *, wait_timeout):
        del root, wait_timeout
        return {"request_id": request_id, "status": "completed", "result": "unknown"}

    monkeypatch.setattr(dispatch_client, "wait_for_receipt", unknown_receipt)
    args = argparse.Namespace(
        job_action="cancel", job_id=target, queue_timeout=60, wait_timeout=60,
        json=True,
    )
    assert dispatch_cli.cmd_job(args, settings, tmp_path) == dispatch_cli.EXIT_UNKNOWN
    assert json.loads(capsys.readouterr().out)["result"] == "unknown"


def test_cmd_job_cancel_attributes_remote_controller_to_primary(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    settings["pool"] = {"primary": "seat2"}
    monkeypatch.setenv("CODEX_THREAD_ID", "remote-thread")
    monkeypatch.setenv("CODEX_HOME", "/codex/home")
    captured = {}

    def submit(*_args, **kwargs):
        captured.update(kwargs)
        return "b" * 32

    monkeypatch.setattr(dispatch_client, "submit_cancel", submit)
    monkeypatch.setattr(
        dispatch_client,
        "wait_for_receipt",
        lambda *_args, **_kwargs: {
            "request_id": "b" * 32,
            "status": "completed",
            "result": "already_terminal",
        },
    )
    args = argparse.Namespace(
        job_action="cancel",
        job_id="a" * 32,
        queue_timeout=60,
        wait_timeout=60,
        json=True,
    )
    assert dispatch_cli.cmd_job(args, settings, tmp_path) == 0
    assert (captured["origin_head"], captured["origin_seat"]) == (
        "codex",
        "seat2",
    )


def test_tty_delegate_progress_is_transition_only_and_stdout_receipt_once(
    tmp_path, monkeypatch, capsys
):
    settings = _settings(tmp_path)
    monkeypatch.setattr(dispatch_cli.time, "time", lambda: NOW)
    monkeypatch.setattr(dispatch_cli.sys.stderr, "isatty", lambda: True)

    def fake_wait(_root, request_id, *, wait_timeout, on_transition):
        assert wait_timeout == 1800
        on_transition(request_id, {"state": "waiting"})
        on_transition(request_id, {"state": "waiting_capacity"})
        on_transition(request_id, {"state": "waiting_capacity"})
        on_transition(
            request_id,
            {"state": "running", "provider": "codex", "seat": "seat3"},
        )
        on_transition(
            request_id,
            {"state": "running", "provider": "codex", "seat": "seat3"},
        )
        receipt = {"request_id": request_id, "status": "completed"}
        on_transition(request_id, {"state": "completed", "receipt": receipt})
        return receipt

    monkeypatch.setattr(dispatch_client, "wait_for_receipt", fake_wait)
    assert dispatch_cli.cmd_delegate(_delegate_args(), settings, tmp_path) == 0
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 1
    assert captured.out.strip().endswith("completed")
    assert [line.split(maxsplit=2)[2] for line in captured.err.splitlines()] == [
        "submitted · waiting",
        "waiting_capacity",
        "running · codex/seat3",
        "completed",
    ]


def test_json_and_detached_delegate_keep_stdout_and_stderr_machine_stable(
    tmp_path, monkeypatch, capsys
):
    settings = _settings(tmp_path)
    monkeypatch.setattr(dispatch_cli.time, "time", lambda: NOW)
    monkeypatch.setattr(dispatch_cli.sys.stderr, "isatty", lambda: True)

    def fake_wait(_root, request_id, *, wait_timeout):
        assert wait_timeout == 1800
        return {"request_id": request_id, "status": "completed"}

    monkeypatch.setattr(dispatch_client, "wait_for_receipt", fake_wait)
    assert dispatch_cli.cmd_delegate(
        _delegate_args(json=True), settings, tmp_path
    ) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "completed"
    assert captured.err == ""

    assert dispatch_cli.cmd_delegate(
        _delegate_args(detach=True), settings, tmp_path
    ) == 0
    captured = capsys.readouterr()
    assert re.fullmatch(r"[0-9a-f]{32}\n", captured.out)
    assert captured.err == ""


def test_head_context_enables_progress_when_command_stderr_is_piped(
    monkeypatch,
):
    monkeypatch.setenv("CODEX_SEATS_HEAD", "claude")
    monkeypatch.setattr(dispatch_cli.sys.stderr, "isatty", lambda: False)
    assert dispatch_cli._progress(False).enabled is True
    assert dispatch_cli._progress(True).enabled is False


def test_job_list_json_and_human_output_are_sanitized_and_stable(
    tmp_path, monkeypatch, capsys
):
    jobs = [
        {
            "request_id": "a" * 32,
            "state": "running",
            "requested_provider": "auto",
            "provider": "codex",
            "seat": "seat4",
            "kind": "review",
            "size": "small",
            "elapsed_seconds": 9,
            "label": "bounded label",
        }
    ]
    monkeypatch.setattr(dispatch_client, "live_jobs", lambda *_args: jobs)
    json_args = argparse.Namespace(job_action="list", json=True)
    assert dispatch_cli.cmd_job(json_args, {}, tmp_path) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema": 1, "ok": True, "data": {"jobs": jobs},
    }

    human_args = argparse.Namespace(job_action="list", json=False)
    assert dispatch_cli.cmd_job(human_args, {}, tmp_path) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == "aaaaaaaa running codex/seat4 review/small 9s bounded label\n"


def test_job_list_watch_is_tty_only_redraws_transitions_and_rejects_json(
    tmp_path, monkeypatch, capsys
):
    settings = _settings(tmp_path)
    snapshots = [
        [],
        [
            {
                "request_id": "b" * 32,
                "state": "running",
                "requested_provider": "claude",
                "provider": "claude",
                "seat": None,
                "kind": "review",
                "size": "small",
                "elapsed_seconds": 2,
                "label": "review patch",
            }
        ],
    ]
    monkeypatch.setattr(
        dispatch_client, "watch_live_jobs", lambda *_args: iter(snapshots)
    )
    monkeypatch.setattr(dispatch_cli.sys.stdout, "isatty", lambda: True)
    args = argparse.Namespace(job_action="list", json=False, watch=True)
    assert dispatch_cli.cmd_job(args, settings, tmp_path) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("BACKGROUND WORK") == 2
    assert "none" in captured.out
    assert "bbbbbbbb running claude review/small 2s review patch" in captured.out

    args.json = True
    assert dispatch_cli.cmd_job(args, settings, tmp_path) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "dispatch-error" and "cannot be combined" in error["message"]

    args.json = False
    monkeypatch.setattr(dispatch_cli.sys.stdout, "isatty", lambda: False)
    assert dispatch_cli.cmd_job(args, settings, tmp_path) == 2
    assert "interactive terminal" in capsys.readouterr().err


def test_watch_rows_are_clipped_to_the_current_terminal_width():
    jobs = [
        {
            "request_id": "a" * 32,
            "state": "running",
            "provider": "codex",
            "seat": "seat4",
            "kind": "review",
            "size": "small",
            "elapsed_seconds": 9,
            "label": "界" * 80,
        }
    ]
    line = dispatch_cli._job_lines(jobs, width=24)[0]
    from codex_seats.display import _display_width

    assert _display_width(line) <= 24
    assert line.endswith("…")
