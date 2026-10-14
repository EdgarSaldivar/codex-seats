"""Tests for the head-side dispatch queue client."""

from __future__ import annotations

import os
import threading
import time

import pytest

from codex_seats import dispatch_client as client
from codex_seats import dispatch_protocol as protocol
from codex_seats.state import Ledger
from registry_fixture import registry_settings, write_settings


NOW = 1_000.0
RELEASE_ID = "a" * 64


def _settings(tmp_path):
    release = tmp_path / "releases" / RELEASE_ID / "lib" / "codex_seats"
    release.mkdir(parents=True, exist_ok=True)
    (tmp_path / "current").symlink_to(f"releases/{RELEASE_ID}")
    settings = registry_settings()
    settings["projects"] = {"sample-project": {"path": str(tmp_path)}}
    return write_settings(tmp_path, settings)


def test_submit_task_writes_valid_request(tmp_path):
    settings = _settings(tmp_path)
    request_id = client.submit_task(
        tmp_path,
        settings,
        repo_id="sample-project",
        prompt="Implement the bounded change.",
        origin_head="codex",
        origin_seat="seat3",
        queue_timeout_seconds=120,
        now=NOW,
    )

    assert len(request_id) == 32
    assert int(request_id, 16) >= 0
    ready = protocol.queue_paths(tmp_path)["inbox"] / f"{request_id}.ready"
    request = protocol.read_request_secure(ready)
    protocol.validate_request(request, settings, now=NOW)
    assert request == {
        "schema": protocol.SCHEMA_VERSION,
        "release_id": RELEASE_ID,
        "protocol_version": protocol.PROTOCOL_VERSION,
        "protocol_min": protocol.PROTOCOL_VERSION,
        "protocol_max": protocol.PROTOCOL_VERSION,
        "request_id": request_id,
        "operation": "task",
        "repo_id": "sample-project",
        "prompt": "Implement the bounded change.",
        "provider": "auto",
            "kind": "implementation",
            "size": "auto",
            "capability_tier": "auto",
            "estimated_minutes": 20.0,
            "parallel_safe": False,
        "scope_hints": [],
        "risk_flags": [],
        "read_only": False,
        "origin": {"head": "codex", "seat": "seat3"},
        "created_at": NOW,
        "expires_at": NOW + 120,
    }


def test_client_has_no_goal_submission_surface():
    assert not hasattr(client, "submit_approved_goal")


def test_submit_task_carries_typed_reserve_authority_only_when_confirmed(tmp_path):
    settings = _settings(tmp_path)
    request_id = client.submit_task(
        tmp_path,
        settings,
        repo_id="sample-project",
        prompt="Use the remaining real capacity.",
        origin_head="claude",
        provider="codex",
        spend_reserve_seat="seat2",
        from_current_user_request=True,
        now=NOW,
    )
    ready = protocol.queue_paths(tmp_path)["inbox"] / f"{request_id}.ready"
    request = protocol.read_request_secure(ready)
    assert request["protocol_version"] == protocol.PROTOCOL_VERSION
    assert request["protocol_min"] == request["protocol_max"] == protocol.PROTOCOL_VERSION
    assert request["reserve_spend_authority"] == {
        "version": 1,
        "seat": "seat2",
        "source": "current_user_request",
    }
    assert "allow_reserve" not in request


def test_submit_task_carries_audited_effort_override(tmp_path):
    settings = _settings(tmp_path)
    request_id = client.submit_task(
        tmp_path,
        settings,
        repo_id="sample-project",
        prompt="Review the high-stakes interaction.",
        origin_head="codex",
        kind="ui_ux",
        effort="max",
        override_reason="checkout interaction needs deeper reasoning",
        now=NOW,
    )
    ready = protocol.queue_paths(tmp_path)["inbox"] / f"{request_id}.ready"
    request = protocol.read_request_secure(ready)
    assert request["protocol_version"] == protocol.PROTOCOL_VERSION
    assert request["protocol_min"] == request["protocol_max"] == protocol.PROTOCOL_VERSION
    assert request["effort"] == "max"
    assert request["override_reason"] == "checkout interaction needs deeper reasoning"


def test_submit_task_rejects_unreasoned_effort_override_without_writing(tmp_path):
    settings = _settings(tmp_path)
    with pytest.raises(protocol.DispatchError, match="effort override requires override_reason"):
        client.submit_task(
            tmp_path,
            settings,
            repo_id="sample-project",
            prompt="Spend more reasoning.",
            origin_head="codex",
            effort="max",
            now=NOW,
        )
    assert not protocol.queue_paths(tmp_path)["inbox"].exists()


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"spend_reserve_seat": "seat2"}, "requires --from-current"),
        ({"from_current_user_request": True}, "requires --spend-reserve"),
        (
            {
                "spend_reserve_seat": "seat2",
                "from_current_user_request": True,
                "provider": "auto",
            },
            "requires --provider codex",
        ),
        (
            {
                "spend_reserve_seat": "seat0",
                "from_current_user_request": True,
                "provider": "codex",
            },
            "must be seatN",
        ),
    ],
)
def test_submit_task_rejects_unconfirmed_or_non_codex_reserve_spending(
    tmp_path, kwargs, match
):
    with pytest.raises(protocol.DispatchError, match=match):
        client.submit_task(
            tmp_path,
            _settings(tmp_path),
            repo_id="sample-project",
            prompt="do work",
            origin_head="claude",
            now=NOW,
            **kwargs,
        )


def test_submit_task_carries_only_named_mcp_grants(tmp_path):
    settings = _settings(tmp_path)
    request_id = client.submit_task(
        tmp_path,
        settings,
        repo_id="sample-project",
        prompt="Use the visual tool.",
        origin_head="codex",
        mcp=["visual"],
        now=NOW,
    )
    ready = protocol.queue_paths(tmp_path)["inbox"] / f"{request_id}.ready"
    request = protocol.read_request_secure(ready)
    assert request["mcp"] == ["visual"]
    assert request["protocol_version"] == protocol.PROTOCOL_VERSION
    assert "command" not in request
    assert "env" not in request


def test_submit_cancel_writes_only_typed_control_identity(tmp_path):
    settings = _settings(tmp_path)
    target = protocol.new_request_id()
    request_id = client.submit_cancel(
        tmp_path,
        settings,
        target_request_id=target,
        origin_head="codex",
        origin_seat="seat2",
        now=NOW,
    )
    ready = protocol.queue_paths(tmp_path)["inbox"] / f"{request_id}.ready"
    request = protocol.read_request_secure(ready)
    protocol.validate_request(request, settings, now=NOW)
    assert set(request) == {
        "schema", "release_id", "protocol_version", "protocol_min", "protocol_max",
        "request_id", "operation", "target_request_id", "origin", "created_at",
        "expires_at",
    }
    assert request["target_request_id"] == target


def test_invalid_submission_writes_nothing(tmp_path):
    settings = _settings(tmp_path)

    with pytest.raises(protocol.DispatchError, match="registered project"):
        client.submit_task(
            tmp_path,
            settings,
            repo_id="not-registered",
            prompt="Do not queue this.",
            origin_head="codex",
            now=NOW,
        )

    assert not protocol.queue_paths(tmp_path)["inbox"].exists()


def test_wait_for_receipt_returns_existing_receipt(tmp_path):
    request_id = protocol.new_request_id()
    expected = {"request_id": request_id, "status": "completed"}
    protocol.write_receipt(tmp_path, expected)

    assert client.wait_for_receipt(tmp_path, request_id, wait_timeout=1) == expected


def test_wait_for_receipt_times_out_with_injected_clock(tmp_path):
    ticks = iter((10.0, 12.0))

    with pytest.raises(TimeoutError):
        client.wait_for_receipt(
            tmp_path,
            protocol.new_request_id(),
            wait_timeout=1,
            time_fn=lambda: next(ticks),
        )


def test_wait_for_receipts_returns_all_receipts(tmp_path):
    first_id = protocol.new_request_id()
    second_id = protocol.new_request_id()
    first = {"request_id": first_id, "status": "completed"}
    second = {"request_id": second_id, "status": "failed"}
    protocol.write_receipt(tmp_path, first)
    protocol.write_receipt(tmp_path, second)

    assert client.wait_for_receipts(
        tmp_path, [first_id, second_id], wait_timeout=1
    ) == {first_id: first, second_id: second}


def test_wait_for_receipts_timeout_names_only_missing_ids(tmp_path):
    present_id = protocol.new_request_id()
    missing_id = protocol.new_request_id()
    protocol.write_receipt(
        tmp_path, {"request_id": present_id, "status": "completed"}
    )
    ticks = iter((10.0, 12.0))

    with pytest.raises(client.ReceiptTimeout) as error:
        client.wait_for_receipts(
            tmp_path,
            [present_id, missing_id],
            wait_timeout=1,
            time_fn=lambda: next(ticks),
        )

    assert missing_id in str(error.value)
    assert present_id not in str(error.value)
    assert error.value.receipts == {
        present_id: {"request_id": present_id, "status": "completed"}
    }
    assert error.value.pending == (missing_id,)


def test_job_status_derives_waiting_capacity_running_terminal_and_unknown(tmp_path):
    waiting = protocol.new_request_id()
    waiting_frame = {
        "request_id": waiting,
        "operation": "task",
    }
    protocol.write_request(tmp_path, waiting_frame)
    assert client.job_status(tmp_path, waiting)["state"] == "waiting"

    paths = protocol.queue_paths(tmp_path)
    from codex_seats import dispatch_lease as leases

    leases.write_atomic(
        paths["processing"] / f".{waiting}.retry.json",
        {
            "request_id": waiting,
            "request_hash": protocol.content_hash(waiting_frame),
            "not_before": NOW + 10,
        },
    )
    assert client.job_status(tmp_path, waiting)["state"] == "waiting_capacity"

    running = protocol.new_request_id()
    frame = {"request_id": running, "operation": "task"}
    protocol.write_request(tmp_path, frame)
    claimed = protocol.claim(tmp_path, paths["inbox"] / f"{running}.ready")
    assert claimed.exists()
    leases.write_atomic(
        leases.lease_path(paths["processing"], running),
        {
            "request_id": running,
            "request_hash": protocol.content_hash(frame),
            "job_pid": os.getpid(),
        },
    )
    assert client.job_status(tmp_path, running)["state"] == "running"

    protocol.write_receipt(
        tmp_path, {"request_id": running, "request_hash": "x", "status": "cancelled"}
    )
    assert client.job_status(tmp_path, running)["state"] == "cancelled"
    assert client.job_status(tmp_path, protocol.new_request_id())["state"] == "unknown"


def test_job_status_tolerates_a_claim_disappearing_during_secure_open(
    tmp_path, monkeypatch
):
    request_id = protocol.new_request_id()
    protocol.write_request(
        tmp_path, {"request_id": request_id, "operation": "task"}
    )
    original = protocol.read_request_secure

    def racing_read(path, **kwargs):
        if str(path).endswith(".claimed"):
            try:
                raise FileNotFoundError(path)
            except FileNotFoundError as cause:
                raise protocol.DispatchError("claim moved") from cause
        return original(path, **kwargs)

    monkeypatch.setattr(protocol, "read_request_secure", racing_read)
    assert client.job_status(tmp_path, request_id)["state"] == "waiting"


def test_tail_job_is_bounded_exact_and_refuses_runner_log_symlink(tmp_path):
    paths = protocol.ensure_queue(tmp_path)
    request_id = protocol.new_request_id()
    other_id = protocol.new_request_id()
    log = paths["logs"] / "runner.log"
    log.write_text(
        f"1 completed {other_id}\n"
        f"2 started {request_id}\n"
        f"3 completed {request_id}\n"
    )
    result = client.tail_job(tmp_path, request_id, lines=1)
    assert result["lines"] == [f"3 completed {request_id}"]

    log.unlink()
    outside = tmp_path / "outside.log"
    outside.write_text(f"secret {request_id}\n")
    log.symlink_to(outside)
    with pytest.raises(protocol.DispatchError, match="runner log"):
        client.tail_job(tmp_path, request_id)


def test_production_event_backend_receives_receipt_without_poll_sleeper(tmp_path):
    protocol.ensure_queue(tmp_path)
    request_id = protocol.new_request_id()
    expected = {"request_id": request_id, "status": "completed"}

    def publish():
        time.sleep(0.03)
        protocol.write_receipt(tmp_path, expected)

    thread = threading.Thread(target=publish)
    thread.start()
    try:
        assert client.receipt_wait_backend() in {"kqueue", "inotify"}
        assert client.wait_for_receipt(
            tmp_path,
            request_id,
            wait_timeout=2,
            sleeper=lambda *_args: (_ for _ in ()).throw(
                AssertionError("production backend called poll sleeper")
            ),
        ) == expected
    finally:
        thread.join()


def test_wait_arms_before_recheck_to_prevent_lost_wakeup(tmp_path, monkeypatch):
    protocol.ensure_queue(tmp_path)
    request_id = protocol.new_request_id()
    expected = {"request_id": request_id, "status": "completed"}

    class ArmedWriter:
        def __init__(self, *_args, **_kwargs):
            protocol.write_receipt(tmp_path, expected)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def wait(self, _timeout):
            raise AssertionError("receipt written while arming must be found by recheck")

    monkeypatch.setattr(client.dispatch_events, "QueueEventWaiter", ArmedWriter)
    assert client.wait_for_receipt(tmp_path, request_id, wait_timeout=1) == expected


def test_event_backend_timeout_multiple_receipts_and_unrelated_coalescing(tmp_path):
    paths = protocol.ensure_queue(tmp_path)
    with pytest.raises(TimeoutError):
        client.wait_for_receipt(
            tmp_path, protocol.new_request_id(), wait_timeout=0.02
        )

    request_ids = [protocol.new_request_id() for _ in range(2)]
    expected = {
        request_id: {"request_id": request_id, "status": "completed"}
        for request_id in request_ids
    }

    def publish():
        time.sleep(0.03)
        # Several irrelevant queue changes are coalesced before the receipts arrive.
        for index in range(5):
            artifact = paths["inbox"] / f".unrelated-{index}"
            artifact.write_text("x")
            artifact.unlink()
        for receipt in expected.values():
            protocol.write_receipt(tmp_path, receipt)

    thread = threading.Thread(target=publish)
    thread.start()
    try:
        assert client.wait_for_receipts(
            tmp_path, request_ids, wait_timeout=2
        ) == expected
    finally:
        thread.join()


def test_event_wait_rejects_symlinked_and_non_owner_only_dispatch_dirs(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (tmp_path / "dispatch").symlink_to(outside, target_is_directory=True)
    with pytest.raises(protocol.DispatchError, match="dispatch directory"):
        client.wait_for_receipt(tmp_path, protocol.new_request_id(), wait_timeout=0)

    (tmp_path / "dispatch").unlink()
    paths = protocol.ensure_queue(tmp_path)
    paths["receipts"].chmod(0o755)
    with pytest.raises(protocol.DispatchError, match="owner-only"):
        client.wait_for_receipt(tmp_path, protocol.new_request_id(), wait_timeout=0)


def test_inotify_watch_invalidation_fails_closed_but_overflow_allows_rescan():
    events = client.dispatch_events
    invalidated = events._IN_EVENT_HEADER.pack(1, events._IN_IGNORED, 0, 0)
    with pytest.raises(protocol.DispatchError, match="replaced or revoked"):
        events.QueueEventWaiter._validate_inotify_events(invalidated)

    overflow = events._IN_EVENT_HEADER.pack(-1, events._IN_Q_OVERFLOW, 0, 0)
    events.QueueEventWaiter._validate_inotify_events(overflow)


def test_production_watch_fails_fast_when_receipts_directory_is_replaced(tmp_path):
    paths = protocol.ensure_queue(tmp_path)
    request_id = protocol.new_request_id()

    def replace_directory():
        time.sleep(0.03)
        paths["receipts"].rename(paths["base"] / "receipts-old")
        paths["receipts"].mkdir(mode=0o700)

    thread = threading.Thread(target=replace_directory)
    thread.start()
    try:
        with pytest.raises(protocol.DispatchError, match="replaced or revoked"):
            client.wait_for_receipt(tmp_path, request_id, wait_timeout=2)
    finally:
        thread.join()


def test_sustained_unrelated_event_flood_cannot_extend_receipt_timeout(tmp_path):
    paths = protocol.ensure_queue(tmp_path)
    request_id = protocol.new_request_id()
    stop = threading.Event()

    def flood():
        index = 0
        while not stop.is_set():
            artifact = paths["inbox"] / f".flood-{index % 8}"
            artifact.write_text("x")
            artifact.unlink(missing_ok=True)
            index += 1

    thread = threading.Thread(target=flood)
    thread.start()
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            client.wait_for_receipt(tmp_path, request_id, wait_timeout=0.05)
        assert time.monotonic() - started < 1
    finally:
        stop.set()
        thread.join()


def test_live_jobs_filters_artifacts_sanitizes_and_joins_running_ledger(tmp_path):
    settings = _settings(tmp_path)
    queued = client.submit_task(
        tmp_path,
        settings,
        repo_id="sample-project",
        prompt=(
            "Queue\x1b[31m first line token=super-sensitive-value /private/path\n"
            "SECRET SECOND LINE"
        ),
        origin_head="claude",
        provider="auto",
        kind="review",
        size="small",
        now=NOW,
    )
    running = client.submit_task(
        tmp_path,
        settings,
        repo_id="sample-project",
        prompt="Run the focused checks",
        origin_head="codex",
        origin_seat="seat2",
        provider="codex",
        now=NOW + 1,
    )
    paths = protocol.queue_paths(tmp_path)
    queued_frame = protocol.read_request_secure(paths["inbox"] / f"{queued}.ready")
    running_frame = protocol.read_request_secure(paths["inbox"] / f"{running}.ready")
    protocol.claim(tmp_path, paths["inbox"] / f"{running}.ready")
    from codex_seats import dispatch_lease as lease_api

    lease_api.write_atomic(
        lease_api.lease_path(paths["processing"], running),
        {
            "request_id": running,
            "request_hash": protocol.content_hash(running_frame),
            "job_pid": os.getpid(),
            "started_at": NOW + 2,
        },
    )
    lease_api.write_atomic(
        paths["processing"] / f".{queued}.retry.json",
        {
            "request_id": queued,
            "request_hash": protocol.content_hash(queued_frame),
            "not_before": NOW + 30,
        },
    )
    with Ledger(tmp_path) as data:
        data["seats"]["seat3"]["in_flight"] = {
            "dispatcher_pid": os.getpid(),
            "kind": "implementation",
            "label": running,
        }
    (paths["inbox"] / "not-valid.ready").write_text('{"prompt":"SECRET"}')
    (paths["inbox"] / f"{protocol.new_request_id()}.ready").symlink_to(
        paths["inbox"] / f"{queued}.ready"
    )

    jobs = client.live_jobs(tmp_path, settings, now=NOW + 12)

    assert [job["request_id"] for job in jobs] == [queued, running]
    queued_job, running_job = jobs
    assert queued_job["label"] == "Queue first line [redacted] [path]"
    assert queued_job["state"] == "waiting_capacity"
    assert queued_job["retry"] is True
    assert queued_job["not_before"] == NOW + 30
    assert "SECRET" not in str(jobs)
    assert "/private/path" not in str(jobs)
    assert queued_job["origin"] == {"head": "claude", "seat": None}
    assert running_job["state"] == "running"
    assert (running_job["provider"], running_job["seat"]) == ("codex", "seat3")
    assert running_job["started_at"] == NOW + 2
    assert running_job["elapsed_seconds"] == 10


def test_live_jobs_rejects_weakened_or_symlinked_queue(tmp_path):
    settings = _settings(tmp_path)
    paths = protocol.ensure_queue(tmp_path)
    paths["processing"].chmod(0o750)
    with pytest.raises(protocol.DispatchError, match="owner-only"):
        client.live_jobs(tmp_path, settings)


def test_live_jobs_marks_expired_queue_and_dead_lease_without_claiming_running(
    tmp_path,
):
    settings = _settings(tmp_path)
    expired = client.submit_task(
        tmp_path,
        settings,
        repo_id="sample-project",
        prompt="Expired task",
        origin_head="codex",
        queue_timeout_seconds=1,
        now=NOW,
    )
    recovering = client.submit_task(
        tmp_path,
        settings,
        repo_id="sample-project",
        prompt="Recover task",
        origin_head="codex",
        now=NOW,
    )
    paths = protocol.queue_paths(tmp_path)
    frame = protocol.read_request_secure(paths["inbox"] / f"{recovering}.ready")
    protocol.claim(tmp_path, paths["inbox"] / f"{recovering}.ready")
    from codex_seats import dispatch_lease as lease_api

    lease_api.write_atomic(
        lease_api.lease_path(paths["processing"], recovering),
        {
            "request_id": recovering,
            "request_hash": protocol.content_hash(frame),
            "job_pid": 2**30,
            "started_at": NOW + 1,
        },
    )

    jobs = client.live_jobs(tmp_path, settings, now=NOW + 2)
    assert {job["request_id"]: job["state"] for job in jobs} == {
        expired: "expired",
        recovering: "recovering",
    }
    assert client.background_counts(jobs) == {
        "queued": 0,
        "running": 0,
        "capacity": 0,
        "recovering": 1,
        "expired": 1,
    }


def test_watch_live_jobs_arms_before_initial_snapshot_and_yields_transition(
    tmp_path,
):
    settings = _settings(tmp_path)
    protocol.ensure_queue(tmp_path)
    armed = False
    waits = 0

    class Waiter:
        def __init__(self, *_args, **_kwargs):
            nonlocal armed
            armed = True

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def wait(self, _timeout):
            nonlocal waits
            waits += 1

    stream = client.watch_live_jobs(
        tmp_path,
        settings,
        now_fn=lambda: NOW,
        waiter_factory=Waiter,
    )
    try:
        assert next(stream) == []
        assert armed is True
        request_id = client.submit_task(
            tmp_path,
            settings,
            repo_id="sample-project",
            prompt="Visible task",
            origin_head="codex",
            now=NOW,
        )
        changed = next(stream)
        assert waits == 1
        assert [job["request_id"] for job in changed] == [request_id]
    finally:
        stream.close()
