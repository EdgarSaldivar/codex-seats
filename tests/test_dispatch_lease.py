from __future__ import annotations

import json
import os

from codex_seats import dispatch_admission as da
from codex_seats import dispatch_lease as dl
from codex_seats import dispatch_client
from codex_seats import dispatch_protocol


def test_lease_is_atomic_owner_only_and_payload_cannot_override_policy(tmp_path):
    frame = {
        "schema": 1,
        "request_id": "a" * 32,
        "release_id": "release-7",
        "repo": "/evil",
        "serial": False,
        "job_pid": 999,
    }
    policy = da.AdmissionFrame(
        request_id="a" * 32,
        repo="/registered/repo",
        scopes=("src/a",),
        parallel_safe=True,
        serial=True,
        identities=frozenset({("goal_id", "goal-1")}),
        origin_head="codex",
        origin_seat="seat2",
    )
    lease = dl.build_lease(
        frame, policy, coordinator_pid=10, job_pid=20, now=123.0
    )
    path = dl.lease_path(tmp_path, policy.request_id)
    dl.write_atomic(path, lease)
    stored = dl.read(path)
    assert stored["repo"] == "/registered/repo"
    assert stored["serial"] is True
    assert stored["job_pid"] == 20
    assert stored["release_id"] == "release-7"
    assert stored["schema"] == 1
    assert stored["origin"] == {"head": "codex", "seat": "seat2"}
    restored = dl.admission_from_lease(stored)
    assert restored is not None
    assert (restored.origin_head, restored.origin_seat) == ("codex", "seat2")
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_heartbeat_matches_job_owner_and_preserves_policy(tmp_path):
    policy = da.AdmissionFrame("b" * 32, "/repo", ("src",), True, False)
    lease = dl.build_lease(
        {"schema": 1}, policy, coordinator_pid=10, job_pid=20, now=1.0
    )
    path = dl.lease_path(tmp_path, policy.request_id)
    dl.write_atomic(path, lease)
    assert not dl.heartbeat(path, request_id=policy.request_id, job_pid=21, now=2.0)
    assert dl.heartbeat(path, request_id=policy.request_id, job_pid=20, now=3.0)
    stored = dl.read(path)
    assert stored["heartbeat_at"] == 3.0
    assert stored["repo"] == "/repo"


def test_lease_additively_carries_canonical_settings_reader_metadata(tmp_path):
    from codex_seats import runtime_readers
    from codex_seats.state import default_settings

    metadata = runtime_readers.build_metadata(
        "dispatch-job",
        default_settings(),
        root=tmp_path,
        pid=20,
        process_identity="4" * 32,
        process_started_at=1.0,
        observed_at=1.0,
        heartbeat_at=1.0,
        release={"id": "a" * 64, "source": "immutable"},
    )
    policy = da.AdmissionFrame("d" * 32, "/repo", (), True, False)
    lease = dl.build_lease(
        {"schema": 1},
        policy,
        coordinator_pid=10,
        job_pid=20,
        now=1.0,
        settings_reader=metadata,
    )
    path = dl.lease_path(tmp_path, policy.request_id)
    dl.write_atomic(path, lease)

    assert dl.heartbeat(path, request_id=policy.request_id, job_pid=20, now=3.0)
    stored = dl.read(path)
    assert stored["settings_reader"]["reader_kind"] == "dispatch-job"
    assert stored["settings_reader"]["observed_settings"]["schema"] == 3
    assert stored["settings_reader"]["supported_settings"] == {"min": 3, "max": 3}
    assert stored["settings_reader"]["heartbeat_at"] == 3.0

    processing = tmp_path / "processing"
    processing.mkdir()
    frame = {
        "schema": 1,
        "request_id": policy.request_id,
        "release_id": "a" * 64,
    }
    claimed = processing / f"{policy.request_id}.claimed"
    claimed.write_text(json.dumps(frame))
    # job_status resolves the standard dispatch layout.
    dispatch_root = tmp_path / "status-root"
    paths = dispatch_protocol.ensure_queue(dispatch_root)
    status_claimed = paths["processing"] / claimed.name
    status_claimed.write_text(json.dumps(frame))
    status_lease = dict(stored)
    status_lease["request_hash"] = dispatch_protocol.content_hash(frame)
    dl.write_atomic(dl.lease_path(paths["processing"], policy.request_id), status_lease)
    status = dispatch_client.job_status(dispatch_root, policy.request_id)
    assert status["settings_reader"]["reader_kind"] == "dispatch-job"


def test_old_lease_without_origin_remains_fail_closed_for_schema3_capacity(tmp_path):
    from codex_seats.availability import HeadOrigin, schema3_concurrency_report
    from codex_seats.state import new_provider, new_seat, default_settings

    lease = {
        "request_id": "c" * 32,
        "repo": "/repo",
        "scopes": ["src"],
        "serial": False,
        "parallel_safe": True,
        "identities": [],
    }
    restored = dl.admission_from_lease(lease)
    assert restored is not None
    assert (restored.origin_head, restored.origin_seat) == (None, None)

    settings = default_settings()
    settings["pool"]["seats"] = {
        "seat1": {
            "label": "One",
            "lifecycle": "active",
            "home": None,
            "capacity_profile": "business-standard",
            "relative_capacity": 1.0,
            "capacity_source": "unverified",
            "capacity_verified_at": None,
        }
    }
    settings["pool"]["primary"] = "seat1"
    runtime = new_seat()
    runtime.update(
        {
            "status": "ok",
            "active_window_observation": {
                "source": "account",
                "complete": True,
                "observed_at": 100.0,
            },
            "last_probe_limits": {
                "weekly": {"pct": 100.0, "resets_at": 200.0, "window_minutes": 10080}
            },
        }
    )
    report = schema3_concurrency_report(
        settings,
        {"seats": {"seat1": runtime}, "providers": {"claude": new_provider()}},
        [HeadOrigin(restored.origin_head or "", restored.origin_seat)],
        current=100.0,
        authenticated_codex={"seat1"},
    )
    assert report["conservative_mixed_origin_ceiling"] == 0
