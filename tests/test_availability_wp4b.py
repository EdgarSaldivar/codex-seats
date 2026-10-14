from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from codex_seats.availability import (
    HeadOrigin,
    OriginError,
    resolve_schema3_origin,
    schema3_concurrency_report,
)
from codex_seats import dispatch_admission, dispatch_protocol, dispatch_runner, seats_cli
from codex_seats.capacity import (
    add_capacity_sample,
    empirical_burn_ratio_report,
    learning_key,
)
from codex_seats.seat_registry import capacity_fingerprint
from codex_seats.state import (
    has_auth,
    new_provider,
    new_seat,
    default_settings,
)


NOW = 1_800_000_000.0


def _entry(index: int, lifecycle: str = "active") -> dict:
    premium = index % 2 == 1
    return {
        "label": f"Seat {index}",
        "lifecycle": lifecycle,
        "home": None,
        "capacity_profile": "business-premium" if premium else "business-standard",
        "relative_capacity": 5.0 if premium else 1.0,
        "capacity_source": "openai-help-center",
        "capacity_verified_at": "2026-09-03T00:00:00Z",
    }


def _runtime(*, status: str = "ok", pct: float = 100.0) -> dict:
    runtime = new_seat()
    runtime.update(
        {
            "status": status,
            "active_window_observation": {
                "source": "account",
                "complete": True,
                "observed_at": NOW,
            },
            "last_probe_limits": {
                "weekly": {
                    "pct": pct,
                    "resets_at": NOW + 3600,
                    "window_minutes": 10_080,
                }
            },
        }
    )
    return runtime


def _pool(count: int) -> tuple[dict, dict]:
    settings = default_settings()
    settings["pool"]["seats"] = {
        f"seat{index}": _entry(index) for index in range(1, count + 1)
    }
    if count:
        settings["pool"]["primary"] = "seat1"
    ledger = {
        "seats": {f"seat{index}": _runtime() for index in range(1, count + 1)},
        "providers": {"claude": new_provider()},
        "capacity_learning": {},
    }
    ledger["providers"]["claude"]["status"] = "off"
    return settings, ledger


def _report(
    settings: dict,
    ledger: dict,
    origins: list[HeadOrigin],
    **kwargs,
) -> dict:
    return schema3_concurrency_report(
        settings,
        ledger,
        origins,
        current=NOW,
        authenticated_codex=settings["pool"]["seats"],
        **kwargs,
    )


@pytest.mark.parametrize(
    "count,codex_slots,claude_head_ceiling",
    [(0, 0, 0), (1, 0, 1), (2, 1, 2), (5, 4, 3), (6, 5, 3), (9, 8, 3)],
)
def test_dynamic_counts_head_exclusion_and_absolute_ceiling(
    count: int, codex_slots: int, claude_head_ceiling: int
) -> None:
    settings, ledger = _pool(count)
    origins = [HeadOrigin("claude")]
    if count:
        origins.append(HeadOrigin("codex", "seat1"))
    report = _report(settings, ledger, origins)
    assert report["safety_ceiling"] == 3
    assert report["origins"][0]["head"] == "claude"
    assert report["origins"][0]["pool_ceiling"] == claude_head_ceiling
    if count:
        codex = report["origins"][1]
        assert codex["codex_worker_slots"] == codex_slots
        assert codex["head_worker_fallback_slots"] == 1
        assert codex["pool_ceiling"] == min(3, codex_slots + 1)


def test_head_worker_fallback_can_be_disabled_without_reclassifying_the_head() -> None:
    settings, ledger = _pool(2)
    settings["execution"]["head_worker_fallback"] = False
    report = _report(settings, ledger, [HeadOrigin("codex", "seat1")])
    origin = report["origins"][0]
    assert origin["codex_worker_identities"] == ["seat2"]
    assert origin["head_worker_fallback_slots"] == 0
    assert origin["pool_ceiling"] == 1


def test_busy_and_lifecycle_rules_are_fail_closed() -> None:
    settings, ledger = _pool(5)
    settings["pool"]["seats"]["seat2"]["lifecycle"] = "draining"
    settings["pool"]["seats"]["seat3"]["lifecycle"] = "draining"
    settings["pool"]["seats"]["seat4"]["lifecycle"] = "inactive"
    settings["pool"]["seats"]["seat5"]["lifecycle"] = "retired"
    ledger["seats"]["seat2"].update(
        {"status": "error", "last_probe_limits": {}, "in_flight": {"job_id": "live"}}
    )
    ledger["seats"]["seat3"]["in_flight"] = {"job_id": "stale"}
    report = _report(
        settings,
        ledger,
        [HeadOrigin("claude")],
        codex_live_claims={"seat2"},
    )
    assert report["codex"]["worker_identities"] == ["seat1", "seat2"]
    states = {item["id"]: item["state"] for item in report["codex"]["identities"]}
    assert states == {
        "seat1": "idle",
        "seat2": "busy",
        "seat3": "draining",
        "seat4": "inactive",
        "seat5": "retired",
    }


def test_unhealthy_unauthenticated_and_unknown_capacity_do_not_create_slots() -> None:
    settings, ledger = _pool(3)
    ledger["seats"]["seat1"]["status"] = "needs-login"
    ledger["seats"]["seat2"]["last_probe_limits"] = {}
    report = schema3_concurrency_report(
        settings,
        ledger,
        [HeadOrigin("claude")],
        current=NOW,
        authenticated_codex={"seat1", "seat2"},
    )
    assert report["codex"]["worker_identities"] == []


def test_legacy_reserve_authority_does_not_change_target_capacity() -> None:
    settings, ledger = _pool(3)
    settings["pool"]["reserves_pct"]["seat2"] = 10
    settings["pool"]["task_min_headroom_pct_by_seat"]["seat2"] = {
        "small": 5,
        "big": 10,
    }
    ledger["seats"]["seat2"] = _runtime(pct=13)
    ledger["seats"]["seat2"]["last_probe_limits"]["weekly"]["resets_at"] = (
        NOW + 7 * 24 * 3600
    )
    ledger["seats"]["seat1"]["status"] = "needs-login"
    settings["pool"]["seats"]["seat3"]["lifecycle"] = "inactive"

    ordinary = _report(settings, ledger, [HeadOrigin("claude")])
    authorized = _report(
        settings,
        ledger,
        [HeadOrigin("claude")],
        reserve_spend_seats={"seat2"},
    )

    assert ordinary["codex"]["worker_identities"] == ["seat2"]
    assert authorized["codex"]["worker_identities"] == ordinary["codex"]["worker_identities"]
    states = {item["id"]: item for item in authorized["codex"]["identities"]}
    assert states["seat1"]["eligible"] is False
    assert states["seat3"]["state"] == "inactive"

    unauthenticated = schema3_concurrency_report(
        settings,
        ledger,
        [HeadOrigin("claude")],
        current=NOW,
        authenticated_codex=set(),
        reserve_spend_seats={"seat2"},
    )
    assert unauthenticated["codex"]["worker_identities"] == []

    inactive_target = copy.deepcopy(settings)
    inactive_target["pool"]["seats"]["seat2"]["lifecycle"] = "inactive"
    lifecycle_blocked = _report(
        inactive_target,
        ledger,
        [HeadOrigin("claude")],
        reserve_spend_seats={"seat2", "seat99"},
    )
    assert lifecycle_blocked["codex"]["worker_identities"] == []


def _claude_limits() -> dict:
    return {
        "updated_at": NOW,
        "five_hour": {"remaining_percentage": 100.0},
        "seven_day": {"remaining_percentage": 100.0},
    }


def test_claude_off_eligible_and_busy_identity_cases() -> None:
    settings, ledger = _pool(1)
    codex_head = [HeadOrigin("codex", "seat1")]
    off = _report(settings, ledger, codex_head, claude_limits=_claude_limits())
    assert off["claude"]["slots"] == 0

    settings["providers"]["claude"]["mode"] = "opportunistic"
    ledger["providers"]["claude"]["status"] = "ok"
    eligible = _report(
        settings, ledger, codex_head, claude_limits=_claude_limits()
    )
    assert eligible["claude"]["slots"] == 1
    assert eligible["origins"][0]["claude_worker_slots"] == 1

    claude_head = _report(
        settings, ledger, [HeadOrigin("claude")], claude_limits=_claude_limits()
    )
    assert claude_head["origins"][0]["claude_worker_slots"] == 0
    assert claude_head["origins"][0]["codex_worker_slots"] == 1

    settings["providers"]["claude"]["mode"] = "off"
    ledger["providers"]["claude"].update(
        {"status": "error", "in_flight": {"job_id": "live"}}
    )
    busy = _report(
        settings,
        ledger,
        codex_head,
        claude_live_claim=True,
        claude_limits={},
    )
    assert busy["claude"]["slots"] == 1
    assert busy["claude"]["state"] == "busy"


def test_mixed_origins_take_deterministic_conservative_minimum() -> None:
    settings, ledger = _pool(2)
    report = _report(
        settings,
        ledger,
        [HeadOrigin("codex", "seat2"), HeadOrigin("claude"), HeadOrigin("codex", "seat1")],
    )
    assert [(item["head"], item["seat"]) for item in report["origins"]] == [
        ("claude", None),
        ("codex", "seat1"),
        ("codex", "seat2"),
    ]
    assert [item["pool_ceiling"] for item in report["origins"]] == [2, 2, 2]
    assert report["current_pool_ceiling"] == 2
    assert report["conservative_mixed_origin_ceiling"] == 2
    assert report["default_primary_codex_head_ceiling"] == 2
    assert report["claude_head_ceiling"] == 2


@pytest.mark.parametrize(
    "origin,message",
    [
        (HeadOrigin("codex", "seat9"), "not a registered identity"),
        (HeadOrigin("codex", "seat2"), "not an active identity"),
        (HeadOrigin("claude", "seat1"), "must not name a Codex seat"),
    ],
)
def test_schema3_origin_validation_rejects_invalid_current_identity(
    origin: HeadOrigin, message: str
) -> None:
    settings, _ledger = _pool(2)
    settings["pool"]["seats"]["seat2"]["lifecycle"] = "inactive"
    with pytest.raises(OriginError, match=message):
        resolve_schema3_origin(settings, origin)
    assert resolve_schema3_origin(settings, HeadOrigin("codex")) == HeadOrigin(
        "codex", "seat1"
    )


def test_invalid_or_non_schema3_state_is_zero_not_legacy_capacity() -> None:
    assert schema3_concurrency_report({}, {}, [HeadOrigin("claude")], current=NOW)[
        "current_pool_ceiling"
    ] == 0
    settings, _ledger = _pool(2)
    assert schema3_concurrency_report(
        settings, {"seats": []}, [HeadOrigin("claude")], current=NOW
    )["current_pool_ceiling"] == 0


def test_parallel_cycle_reloads_on_disk_schema3_and_reads_current_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_disk, snapshot = _pool(2)
    supplied, _old_snapshot = _pool(6)
    (tmp_path / "settings.json").write_text(json.dumps(on_disk))
    (tmp_path / "settings.json").chmod(0o600)
    policy = dispatch_admission.AdmissionFrame(
        request_id="a" * 32,
        repo="/repo",
        serial=False,
        origin_head="codex",
        origin_seat="seat1",
    )
    prepared = dispatch_runner._Prepared(
        tmp_path / "unused.ready",
        {"request_id": policy.request_id},
        policy,
        (0.0, policy.request_id),
    )
    observed: dict = {"ledger_reads": 0}

    def read_snapshot(_root: Path) -> dict:
        observed["ledger_reads"] += 1
        return snapshot

    def select(candidates, active, *, max_active, candidate_max_active):
        observed["max_active"] = max_active
        observed["candidate_max_active"] = candidate_max_active
        observed["candidate_count"] = len(tuple(candidates))
        return ()

    monkeypatch.setattr(dispatch_runner, "read_pool_snapshot", read_snapshot)
    monkeypatch.setattr(dispatch_runner, "has_auth", lambda *_: True)
    monkeypatch.setattr(dispatch_runner, "load_claude_rate_limits", lambda *_a, **_k: {})
    monkeypatch.setattr(dispatch_runner, "_process_cancellations", lambda *_a, **_k: [])
    monkeypatch.setattr(
        dispatch_runner, "_prepare_ready", lambda *_a, **_k: ([prepared], [])
    )
    monkeypatch.setattr(dispatch_runner, "_active_policies", lambda *_a, **_k: [])
    monkeypatch.setattr(dispatch_runner.admission, "select_admissible", select)

    outcomes, children = dispatch_runner.dispatch_parallel_once(
        tmp_path, supplied, now_fn=lambda: NOW
    )
    assert outcomes == [] and children == set()
    assert observed == {
        "ledger_reads": 1,
        "max_active": 3,
        "candidate_max_active": {policy.request_id: 2},
        "candidate_count": 1,
    }


def _dispatch_task(*, origin: dict, request_id: str) -> dict:
    return {
        "schema": 1,
        "release_id": "a" * 64,
        "protocol_version": dispatch_protocol.PROTOCOL_VERSION,
        "protocol_min": dispatch_protocol.MIN_PROTOCOL_VERSION,
        "protocol_max": dispatch_protocol.MAX_PROTOCOL_VERSION,
        "request_id": request_id,
        "operation": "task",
        "repo_id": "repo",
        "prompt": "bounded task",
        "provider": "codex",
        "kind": "implementation",
        "size": "big",
        "estimated_minutes": 10,
        "parallel_safe": True,
        "scope_hints": [request_id],
        "origin": origin,
        "created_at": NOW - 10,
        "expires_at": NOW + 600,
    }


def test_invalid_origin_is_terminal_while_seatless_sibling_uses_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, snapshot = _pool(2)
    repo = tmp_path / "repo"
    repo.mkdir()
    settings["projects"] = {"repo": {"path": str(repo)}}
    (tmp_path / "settings.json").write_text(json.dumps(settings))
    (tmp_path / "settings.json").chmod(0o600)
    for seat_id in ("seat1", "seat2"):
        home = tmp_path / seat_id
        home.mkdir(mode=0o700)
        (home / "auth.json").write_text("opaque credential fixture")
        (home / "auth.json").chmod(0o600)

    invalid = _dispatch_task(
        origin={"head": "codex", "seat": "seat9"}, request_id="8" * 32
    )
    valid = _dispatch_task(origin={"head": "codex"}, request_id="9" * 32)
    dispatch_protocol.write_request(tmp_path, invalid)
    dispatch_protocol.write_request(tmp_path, valid)
    observed: dict = {}

    def spawn(_root, cycle_settings, claimed, frame, policy, **_kwargs):
        resolved = dispatch_runner._resolve_task_origin(frame, cycle_settings)
        internal = dispatch_runner._internal_request(resolved, repo, cycle_settings)
        observed.update(
            policy_origin=(policy.origin_head, policy.origin_seat),
            excluded=internal.get("_excluded_seats"),
            claimed=claimed.name,
        )
        return 4242

    monkeypatch.setattr(dispatch_runner, "read_pool_snapshot", lambda _root: snapshot)
    monkeypatch.setattr(dispatch_runner, "load_claude_rate_limits", lambda *_a, **_k: {})
    monkeypatch.setattr(dispatch_runner, "_spawn_claimed", spawn)

    outcomes, children = dispatch_runner.dispatch_parallel_once(
        tmp_path, settings, now_fn=lambda: NOW
    )

    assert (invalid["request_id"], "rejected") in outcomes
    assert (valid["request_id"], "spawned") in outcomes
    assert children == {4242}
    assert observed["policy_origin"] == ("codex", "seat1")
    assert observed["excluded"] == ["seat1"]
    receipt = dispatch_protocol.read_receipt(tmp_path, invalid["request_id"])
    assert receipt["status"] == "rejected"
    assert "not a registered identity" in receipt["reason"]


def test_real_queue_reserve_authority_does_not_preempt_ordinary_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, snapshot = _pool(2)
    repo = tmp_path / "repo"
    repo.mkdir()
    settings["projects"] = {"repo": {"path": str(repo)}}
    settings["providers"]["claude"]["mode"] = "off"
    settings["pool"]["reserves_pct"]["seat2"] = 10
    snapshot["seats"]["seat2"] = _runtime(pct=13)
    snapshot["seats"]["seat2"]["last_probe_limits"].update({
        "5h": {"pct": 100, "window_minutes": 300, "resets_at": NOW + 18000},
        "weekly": {"pct": 13, "window_minutes": 10080, "resets_at": NOW + 604800},
    })
    (tmp_path / "settings.json").write_text(json.dumps(settings))
    (tmp_path / "settings.json").chmod(0o600)
    home = tmp_path / "seat2"
    home.mkdir(mode=0o700)
    (home / "auth.json").write_text("opaque credential fixture")
    (home / "auth.json").chmod(0o600)
    ordinary = _dispatch_task(origin={"head": "codex", "seat": "seat1"}, request_id="1" * 32)
    ordinary.update(size="small", override_reason="bounded small fixture")
    authorized = copy.deepcopy(ordinary)
    authorized.update(
        request_id="2" * 32, scope_hints=["authorized"],
        reserve_spend_authority={"version": 1, "seat": "seat2", "source": "current_user_request"},
    )
    for frame in (ordinary, authorized):
        dispatch_protocol.write_request(tmp_path, frame)
    spawned = []

    def spawn(_root, current_settings, _claimed, frame, _policy, **_kwargs):
        spawned.append(dispatch_runner._internal_request(frame, repo, current_settings))
        return 4242

    monkeypatch.setattr(dispatch_runner, "read_pool_snapshot", lambda _root: snapshot)
    monkeypatch.setattr(dispatch_runner, "_spawn_claimed", spawn)
    outcomes, children = dispatch_runner.dispatch_parallel_once(tmp_path, settings, now_fn=lambda: NOW)
    assert outcomes == [(ordinary["request_id"], "spawned")]
    assert children == {4242}
    assert len(spawned) == 1 and "seat" not in spawned[0]
    assert spawned[0]["_excluded_seats"] == ["seat1"]
    assert "allow_reserve" not in spawned[0]
    assert "_reserve_spend_authority" not in spawned[0]
    paths = dispatch_protocol.queue_paths(tmp_path)
    assert (paths["inbox"] / f"{authorized['request_id']}.ready").exists()


def test_empirical_ratio_requires_comparable_mature_buckets_and_never_mutates() -> None:
    settings, _ledger = _pool(2)
    standard = capacity_fingerprint(settings["pool"]["seats"]["seat2"])
    premium = capacity_fingerprint(settings["pool"]["seats"]["seat1"])
    learning: dict = {}

    def samples(fingerprint: str, burn: float, *, model: str = "gpt-5.6-sol", count: int = 8) -> None:
        for _ in range(count):
            add_capacity_sample(
                learning,
                provider="codex",
                cadence="windows:weekly",
                model=model,
                size="small",
                effort="high",
                burn_pct=burn,
                capacity_fingerprint=fingerprint,
            )

    samples(standard, 10, count=7)
    samples(premium, 2)
    assert empirical_burn_ratio_report(settings, learning) == []
    samples(standard, 10, model="different")
    assert empirical_burn_ratio_report(settings, learning) == []
    samples(standard, 10, count=1)
    before_settings = copy.deepcopy(settings)
    before_learning = copy.deepcopy(learning)
    report = empirical_burn_ratio_report(settings, learning)
    assert len(report) == 1
    assert report[0]["observed_standard_to_premium_ratio"] == 5
    assert report[0]["configured_standard_to_premium_ratio"] == 5
    assert report[0]["standard_samples"] == report[0]["premium_samples"] == 8
    assert not report[0]["material_disagreement"]
    assert settings == before_settings
    assert learning == before_learning


def test_empirical_ratio_warns_only_after_sustained_material_disagreement() -> None:
    settings, _ledger = _pool(2)
    standard = capacity_fingerprint(settings["pool"]["seats"]["seat2"])
    premium = capacity_fingerprint(settings["pool"]["seats"]["seat1"])
    learning: dict = {}
    for fingerprint, burn in ((standard, 10.0), (premium, 5.0)):
        key = learning_key(
            "codex",
            "windows:weekly",
            "gpt-5.6-sol",
            "small",
            "high",
            capacity_fingerprint=fingerprint,
        )
        learning[key] = {
            "capacity_fingerprint": fingerprint,
            "samples": [burn] * 8,
        }
    report = empirical_burn_ratio_report(settings, learning)
    assert len(report) == 1
    assert report[0]["material_disagreement"]


def test_retired_profiles_do_not_generate_current_empirical_ratio_drift() -> None:
    settings, _ledger = _pool(2)
    settings["pool"]["seats"]["seat1"]["lifecycle"] = "retired"
    settings["pool"]["primary"] = "seat2"
    standard = capacity_fingerprint(settings["pool"]["seats"]["seat2"])
    premium = capacity_fingerprint(settings["pool"]["seats"]["seat1"])
    learning = {}
    for fingerprint, burn in ((standard, 10.0), (premium, 5.0)):
        learning[
            learning_key(
                "codex",
                "windows:weekly",
                "gpt-5.6-sol",
                "small",
                "high",
                capacity_fingerprint=fingerprint,
            )
        ] = {"capacity_fingerprint": fingerprint, "samples": [burn] * 8}
    assert empirical_burn_ratio_report(settings, learning) == []


def test_doctor_exposes_shared_ceiling_and_ratio_warning_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, ledger = _pool(2)
    standard = capacity_fingerprint(settings["pool"]["seats"]["seat2"])
    premium = capacity_fingerprint(settings["pool"]["seats"]["seat1"])
    for fingerprint, burn in ((standard, 10.0), (premium, 5.0)):
        key = learning_key(
            "codex",
            "windows:weekly",
            "gpt-5.6-sol",
            "small",
            "high",
            capacity_fingerprint=fingerprint,
        )
        ledger["capacity_learning"][key] = {
            "capacity_fingerprint": fingerprint,
            "samples": [burn] * 8,
        }
    before_settings = copy.deepcopy(settings)
    before_ledger = copy.deepcopy(ledger)
    monkeypatch.setattr(seats_cli, "_safe_ledger", lambda _root: ledger)
    monkeypatch.setattr(
        seats_cli,
        "_codex_login_support",
        lambda **_kwargs: ("/codex", seats_cli.SUPPORTED_CODEX_VERSION),
    )
    monkeypatch.setattr(seats_cli, "load_claude_rate_limits", lambda _root: {})

    report = seats_cli.seats_doctor_report(tmp_path, settings, include_all=True)
    assert report["head_capacity"]["safety_ceiling"] == 3
    assert "current_pool_ceiling" in report["head_capacity"]
    assert report["empirical_burn_ratios"][0]["material_disagreement"]
    assert "empirical-capacity-ratio-drift" in {
        issue["code"] for issue in report["issues"]
    }
    assert settings == before_settings
    assert ledger == before_ledger


def test_doctor_head_capacity_is_global_when_selector_filtered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, ledger = _pool(2)
    for seat_id in ("seat1", "seat2"):
        home = tmp_path / seat_id
        home.mkdir(mode=0o700)
        (home / "auth.json").write_text("unread fixture")
        (home / "auth.json").chmod(0o600)
    monkeypatch.setattr(seats_cli, "_safe_ledger", lambda _root: ledger)
    monkeypatch.setattr(
        seats_cli,
        "_codex_login_support",
        lambda **_kwargs: ("/codex", seats_cli.SUPPORTED_CODEX_VERSION),
    )
    monkeypatch.setattr(seats_cli, "_login_status", lambda *_args: True)
    monkeypatch.setattr(seats_cli, "load_claude_rate_limits", lambda _root: {})
    monkeypatch.setattr(seats_cli.time, "time", lambda: NOW)

    global_report = seats_cli.seats_doctor_report(tmp_path, settings)
    filtered_report = seats_cli.seats_doctor_report(
        tmp_path, settings, selector="seat1"
    )

    assert filtered_report["head_capacity"] == global_report["head_capacity"]
    capacity = global_report["head_capacity"]
    assert capacity["current_pool_ceiling"] == 2
    assert capacity["default_primary_codex_head_ceiling"] == 2
    assert capacity["codex_head_ceiling"] == 2
    assert capacity["claude_head_ceiling"] == 2


def test_auth_admission_uses_owner_controlled_file_metadata_only(tmp_path: Path) -> None:
    settings, _ledger = _pool(1)
    home = tmp_path / "seat1"
    home.mkdir(mode=0o700)
    auth = home / "auth.json"
    auth.write_text("contents must never be read")
    auth.chmod(0o000)
    assert has_auth(tmp_path, "seat1", settings)

    auth.chmod(0o666)
    assert not has_auth(tmp_path, "seat1", settings)
    assert not seats_cli._auth_file_present(home)
    auth.unlink()
    target = tmp_path / "target"
    target.write_text("not credentials")
    auth.symlink_to(target)
    assert not has_auth(tmp_path, "seat1", settings)
    assert not seats_cli._auth_file_present(home)


def test_doctor_uses_canonical_close_multiplier_fingerprint() -> None:
    entry = _entry(2)
    entry["capacity_profile"] = "operator-measured"
    entry["relative_capacity"] = 1.0000001
    fingerprint = capacity_fingerprint(entry)
    learning = {
        "bucket": {
            "seat_id": "seat2",
            "capacity_fingerprint": fingerprint,
            "samples": [1.0] * 8,
        }
    }
    assert seats_cli._fingerprint_issues(learning, "seat2", entry) == []
