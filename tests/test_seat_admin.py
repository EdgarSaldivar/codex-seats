from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from unittest import mock

import pytest

from codex_seats.seat_admin import (
    AUDIT_FILENAME,
    SeatAdminError,
    activate_seat,
    add_seat,
    deactivate_seat,
    drain_seat,
    remove_seat,
    retire_seat,
    set_role,
    set_seat,
)
from codex_seats.state import default_settings
from codex_seats.seat_registry import SeatRegistryError, validate_schema3


NOW = 1_788_454_400.0


def _write_settings(root: Path, settings: dict | None = None) -> str:
    root.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(settings or default_settings(), indent=2) + "\n"
    (root / "settings.json").write_text(payload)
    (root / "settings.json").chmod(0o600)
    return payload


def _add(root: Path, label: str = "Standard") -> str:
    return add_seat(root, label=label, _clock=lambda: NOW).seat_id


def _activate_first(root: Path, seat_id: str) -> None:
    (root / seat_id).mkdir(mode=0o700, exist_ok=True)
    activate_seat(
        root,
        seat_id,
        verified_login=True,
        successful_probe=True,
        as_primary=True,
    )


def test_add_dry_run_is_exact_and_write_ahead_is_sanitized(tmp_path: Path) -> None:
    before = _write_settings(tmp_path)

    preview = add_seat(
        tmp_path, label="Standard", dry_run=True, _clock=lambda: NOW
    )
    assert preview.seat_id == "seat1"
    assert preview.before == before
    assert json.loads(preview.after)["pool"]["seats"]["seat1"]["lifecycle"] == "inactive"
    assert (tmp_path / "settings.json").read_text() == before
    assert not (tmp_path / "seats.json").exists()
    assert not (tmp_path / AUDIT_FILENAME).exists()

    applied = add_seat(tmp_path, label="Standard", _clock=lambda: NOW)
    assert applied.after == preview.after
    assert applied.backup_path is not None
    assert applied.backup_path.read_text() == before
    assert json.loads((tmp_path / "seats.json").read_text())["seats"]["seat1"]
    audit_text = (tmp_path / AUDIT_FILENAME).read_text()
    records = [json.loads(line) for line in audit_text.splitlines()]
    assert [record["event"] for record in records] == ["allocation", "mutation"]
    assert all(set(record) <= {
        "schema", "event", "action", "seat_id", "at",
        "before_sha256", "after_sha256",
    } for record in records)
    assert "Standard" not in audit_text


def test_add_expected_seat_id_rejects_stale_allocation_without_writes(
    tmp_path: Path,
) -> None:
    before = _write_settings(tmp_path)
    preview = add_seat(tmp_path, label="Planned", dry_run=True)
    assert preview.seat_id == "seat1"

    # A separately reserved identity is allocation evidence even though the
    # settings digest remains unchanged.
    (tmp_path / "seat1").mkdir()
    with pytest.raises(SeatAdminError, match="allocation changed"):
        add_seat(
            tmp_path,
            label="Planned",
            expected_digest=preview.digest,
            expected_seat_id=preview.seat_id,
        )

    assert (tmp_path / "settings.json").read_text() == before
    assert not (tmp_path / "seats.json").exists()
    assert not (tmp_path / AUDIT_FILENAME).exists()


def test_dry_run_never_creates_locks_or_repairs_a_stale_claim(tmp_path: Path) -> None:
    settings = default_settings()
    settings["pool"]["seats"] = {
        "seat1": {
            "label": "Only",
            "lifecycle": "active",
            "home": None,
            "capacity_profile": "personal-plus",
            "relative_capacity": None,
            "capacity_source": "unverified",
            "capacity_verified_at": None,
        }
    }
    settings["pool"]["primary"] = "seat1"
    _write_settings(tmp_path, settings)
    ledger = {
        "schema": 6,
        "seats": {
            "seat1": {
                "in_flight": {
                    "job_id": "stale",
                    "dispatcher_pid": 0,
                    "child_pid": 0,
                }
            }
        },
        "jobs": [],
    }
    (tmp_path / "seats.json").write_text(json.dumps(ledger))
    before = {
        path.name: path.read_bytes()
        for path in tmp_path.iterdir()
        if path.is_file()
    }

    result = deactivate_seat(
        tmp_path, "seat1", allow_empty=True, dry_run=True
    )

    assert result.changed is True
    assert json.loads(result.after)["pool"]["seats"]["seat1"]["lifecycle"] == "inactive"
    assert {
        path.name: path.read_bytes()
        for path in tmp_path.iterdir()
        if path.is_file()
    } == before
    assert not (tmp_path / ".settings.lock").exists()
    assert not (tmp_path / ".ledger.lock").exists()


def test_older_settings_schema_refuses_every_lifecycle_mutation(tmp_path: Path) -> None:
    _write_settings(tmp_path, {"schema": 2, "pool": {"primary": "seat1"}})
    with pytest.raises(SeatAdminError, match="only schema 3"):
        add_seat(tmp_path, label="nope")


def test_first_activation_evidence_home_and_as_primary_guards(tmp_path: Path) -> None:
    _write_settings(tmp_path)
    seat_id = _add(tmp_path)
    with pytest.raises(SeatAdminError, match="verified-login"):
        activate_seat(
            tmp_path,
            seat_id,
            verified_login=False,
            successful_probe=True,
            as_primary=True,
        )
    with pytest.raises(SeatAdminError, match="successful-probe"):
        activate_seat(
            tmp_path,
            seat_id,
            verified_login=True,
            successful_probe=False,
            as_primary=True,
        )
    with pytest.raises(SeatAdminError, match="unavailable"):
        activate_seat(
            tmp_path,
            seat_id,
            verified_login=True,
            successful_probe=True,
            as_primary=True,
        )
    (tmp_path / seat_id).mkdir(mode=0o700)
    with pytest.raises(SeatAdminError, match="as_primary"):
        activate_seat(
            tmp_path,
            seat_id,
            verified_login=True,
            successful_probe=True,
        )
    result = activate_seat(
        tmp_path,
        seat_id,
        verified_login=True,
        successful_probe=True,
        as_primary=True,
    )
    current = json.loads(result.after)
    assert current["pool"]["primary"] == seat_id
    assert current["pool"]["seats"][seat_id]["lifecycle"] == "active"
    assert result.warnings == (
        "seat1 has unverified capacity; routing will conservatively use 1x",
    )


def test_existing_parent_symlink_aliases_collide_but_nonexistent_homes_plan(
    tmp_path: Path,
) -> None:
    settings = default_settings()
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    alias_parent = tmp_path / "alias"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    entry = {
        "label": "Seat",
        "lifecycle": "inactive",
        "home": str(real_parent / "future"),
        "capacity_profile": "personal-plus",
        "relative_capacity": None,
        "capacity_source": "unverified",
        "capacity_verified_at": None,
    }
    settings["pool"]["seats"] = {
        "seat1": entry,
        "seat2": {**entry, "home": str(alias_parent / "future")},
    }
    with pytest.raises(SeatRegistryError, match="canonical home collision"):
        validate_schema3(settings, tmp_path)

    settings["pool"]["seats"]["seat2"]["home"] = str(alias_parent / "other")
    validate_schema3(settings, tmp_path)


def test_activation_revalidates_parent_alias_identity_under_locks(tmp_path: Path) -> None:
    settings = default_settings()
    active_parent = tmp_path / "active-parent"
    candidate_parent = tmp_path / "candidate-parent"
    active_parent.mkdir()
    candidate_parent.mkdir()
    active_home = active_parent / "home"
    candidate_home = candidate_parent / "home"
    active_home.mkdir(mode=0o700)
    candidate_home.mkdir(mode=0o700)
    entry = {
        "label": "Seat",
        "lifecycle": "active",
        "home": str(active_home),
        "capacity_profile": "personal-plus",
        "relative_capacity": None,
        "capacity_source": "unverified",
        "capacity_verified_at": None,
    }
    settings["pool"]["seats"] = {
        "seat1": entry,
        "seat2": {
            **entry,
            "lifecycle": "inactive",
            "home": str(candidate_home),
        },
    }
    settings["pool"]["primary"] = "seat1"
    _write_settings(tmp_path, settings)

    alias_now = False
    realpath = os.path.realpath

    def switched_realpath(path: str | os.PathLike[str]) -> str:
        if alias_now and Path(path) == candidate_home:
            return realpath(active_home)
        return realpath(path)

    def replace_parent(phase: str) -> None:
        nonlocal alias_now
        if phase != "after_ledger_lock_before_claim_validation":
            return
        alias_now = True

    with (
        pytest.raises(SeatAdminError, match="aliases active seat1"),
        mock.patch(
            "codex_seats.seat_admin.os.path.realpath", side_effect=switched_realpath
        ),
    ):
        activate_seat(
            tmp_path,
            "seat2",
            verified_login=True,
            successful_probe=True,
            hook=replace_parent,
        )
    current = json.loads((tmp_path / "settings.json").read_text())
    assert current["pool"]["seats"]["seat2"]["lifecycle"] == "inactive"


def test_fields_roles_and_lifecycle_rules(tmp_path: Path) -> None:
    _write_settings(tmp_path)
    one = _add(tmp_path, "One")
    two = _add(tmp_path, "Two")
    _activate_first(tmp_path, one)
    (tmp_path / two).mkdir(mode=0o700)
    activate_seat(
        tmp_path,
        two,
        verified_login=True,
        successful_probe=True,
    )

    set_role(tmp_path, two, "deputy")
    with pytest.raises(SeatAdminError, match="designated deputy"):
        drain_seat(tmp_path, two)
    set_role(tmp_path, two, "worker")
    set_role(tmp_path, two, "primary")
    current = json.loads((tmp_path / "settings.json").read_text())
    assert current["pool"]["primary"] == two
    assert current["pool"]["deputy"] is None
    with pytest.raises(SeatAdminError, match="replacement"):
        set_role(tmp_path, two, "worker")

    set_role(tmp_path, one, "primary")
    drain_seat(tmp_path, two)
    deactivate_seat(tmp_path, two)
    set_seat(
        tmp_path,
        two,
        label="Changed",
        home=None,
        profile="business-premium",
        relative_capacity=5,
        capacity_source="openai-help-center",
        _clock=lambda: NOW,
    )
    retire_seat(tmp_path, two)
    assert remove_seat(tmp_path, two).changed is False
    current = json.loads((tmp_path / "settings.json").read_text())
    entry = current["pool"]["seats"][two]
    assert entry["lifecycle"] == "retired"
    assert entry["relative_capacity"] == 5.0
    assert entry["capacity_verified_at"] == "2026-09-03T16:53:20Z"
    assert (tmp_path / two).is_dir()


def test_capacity_and_home_claim_guards(tmp_path: Path) -> None:
    _write_settings(tmp_path)
    seat_id = _add(tmp_path)
    with pytest.raises(SeatAdminError, match="verified capacity_source"):
        set_seat(tmp_path, seat_id, relative_capacity=5)
    set_seat(
        tmp_path,
        seat_id,
        relative_capacity=5,
        capacity_source="operator-measured",
    )
    _activate_first(tmp_path, seat_id)
    with pytest.raises(SeatAdminError, match="only while.*inactive"):
        set_seat(tmp_path, seat_id, home=str(tmp_path / "elsewhere"))

    ledger = json.loads((tmp_path / "seats.json").read_text())
    ledger["seats"][seat_id]["in_flight"] = {
        "job_id": "live",
        "dispatcher_pid": os.getpid(),
    }
    (tmp_path / "seats.json").write_text(json.dumps(ledger))
    with pytest.raises(SeatAdminError, match="live worker claim"):
        set_seat(tmp_path, seat_id, profile="business-standard")
    with pytest.raises(SeatAdminError, match="live worker claim"):
        deactivate_seat(tmp_path, seat_id, allow_empty=True)


def test_policy_preview_apply_preserves_other_seats_and_omitted_floors(
    tmp_path: Path,
) -> None:
    _write_settings(tmp_path)
    one = _add(tmp_path, "One")
    two = _add(tmp_path, "Two")
    settings_path = tmp_path / "settings.json"
    settings = json.loads(settings_path.read_text())
    settings["pool"]["small_task_min_headroom_pct"] = 11
    settings["pool"]["big_task_min_headroom_pct"] = 31
    settings["pool"]["reserves_pct"] = {two: 7}
    settings["pool"]["task_min_headroom_pct_by_seat"] = {
        two: {"small": 8, "big": 28}
    }
    settings["future"] = {"preserved": True}
    _write_settings(tmp_path, settings)
    before = settings_path.read_bytes()

    preview = set_seat(
        tmp_path,
        one,
        reserve_pct=4,
        small_task_min_headroom_pct=6,
        dry_run=True,
    )

    assert settings_path.read_bytes() == before
    planned = json.loads(preview.after)
    assert planned["pool"]["reserves_pct"] == {two: 7, one: 4.0}
    assert planned["pool"]["task_min_headroom_pct_by_seat"] == {
        two: {"small": 8, "big": 28},
        one: {"small": 6.0, "big": 31},
    }
    assert planned["pool"]["small_task_min_headroom_pct"] == 11
    assert planned["pool"]["big_task_min_headroom_pct"] == 31
    assert planned["future"] == {"preserved": True}

    applied = set_seat(
        tmp_path,
        one,
        reserve_pct=4,
        small_task_min_headroom_pct=6,
    )
    assert applied.after == preview.after
    assert settings_path.read_text() == preview.after
    records = [
        json.loads(line)
        for line in (tmp_path / AUDIT_FILENAME).read_text().splitlines()
    ]
    assert records[-1]["event"] == "mutation"
    assert records[-1]["action"] == "set"
    assert records[-1]["seat_id"] == one

    before_reserve_only = settings_path.read_text()
    set_seat(tmp_path, one, reserve_pct=5)
    reserve_only = json.loads(settings_path.read_text())
    assert reserve_only["pool"]["task_min_headroom_pct_by_seat"][one] == {
        "small": 6.0,
        "big": 31,
    }
    assert reserve_only["pool"]["task_min_headroom_pct_by_seat"][two] == {
        "small": 8,
        "big": 28,
    }
    assert before_reserve_only != settings_path.read_text()


def test_policy_update_accepts_sparse_migrated_settings(tmp_path: Path) -> None:
    _write_settings(tmp_path)
    seat_id = _add(tmp_path)
    path = tmp_path / "settings.json"
    settings = json.loads(path.read_text())
    for key in (
        "reserves_pct", "task_min_headroom_pct_by_seat",
        "small_task_min_headroom_pct", "big_task_min_headroom_pct",
    ):
        settings["pool"].pop(key, None)
    _write_settings(tmp_path, settings)
    before = path.read_bytes()
    preview = set_seat(tmp_path, seat_id, reserve_pct=6,
                       small_task_min_headroom_pct=1, dry_run=True)
    assert path.read_bytes() == before
    applied = set_seat(tmp_path, seat_id, reserve_pct=6,
                       small_task_min_headroom_pct=1)
    assert applied.after == preview.after
    result = json.loads(applied.after)["pool"]
    assert result["reserves_pct"] == {seat_id: 6}
    assert result["task_min_headroom_pct_by_seat"] == {
        seat_id: {"small": 1, "big": 30}
    }
    assert "small_task_min_headroom_pct" not in result
    assert "big_task_min_headroom_pct" not in result


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reserve_pct", True),
        ("reserve_pct", float("nan")),
        ("reserve_pct", float("inf")),
        ("reserve_pct", -0.1),
        ("reserve_pct", 100.1),
        ("small_task_min_headroom_pct", False),
        ("small_task_min_headroom_pct", float("-inf")),
        ("big_task_min_headroom_pct", 101),
    ],
)
def test_policy_percentages_reject_boolean_nonfinite_and_out_of_range(
    tmp_path: Path, field: str, value: object
) -> None:
    _write_settings(tmp_path)
    seat_id = _add(tmp_path)
    before = (tmp_path / "settings.json").read_bytes()

    with pytest.raises(SeatAdminError, match="finite number from 0 to 100"):
        set_seat(tmp_path, seat_id, **{field: value})

    assert (tmp_path / "settings.json").read_bytes() == before


def test_idempotent_field_retry_does_not_restamp_or_require_idle(tmp_path: Path) -> None:
    _write_settings(tmp_path)
    seat_id = _add(tmp_path)
    set_seat(
        tmp_path,
        seat_id,
        profile="business-premium",
        relative_capacity=5,
        capacity_source="openai-help-center",
        _clock=lambda: NOW,
    )
    _activate_first(tmp_path, seat_id)
    ledger = json.loads((tmp_path / "seats.json").read_text())
    ledger["seats"][seat_id]["in_flight"] = {
        "job_id": "live",
        "dispatcher_pid": os.getpid(),
    }
    (tmp_path / "seats.json").write_text(json.dumps(ledger))
    before = (tmp_path / "settings.json").read_text()
    result = set_seat(
        tmp_path,
        seat_id,
        home=None,
        profile="business-premium",
        relative_capacity=5.0,
        capacity_source="openai-help-center",
        _clock=lambda: NOW + 100,
    )
    assert result.changed is False
    assert result.before == result.after == before
    entry = json.loads(result.after)["pool"]["seats"][seat_id]
    assert entry["capacity_verified_at"] == "2026-09-03T16:53:20Z"


def test_final_seat_allow_empty_and_idempotency(tmp_path: Path) -> None:
    _write_settings(tmp_path)
    seat_id = _add(tmp_path)
    _activate_first(tmp_path, seat_id)
    with pytest.raises(SeatAdminError, match="allow_empty"):
        drain_seat(tmp_path, seat_id)
    first = drain_seat(tmp_path, seat_id, allow_empty=True)
    path = tmp_path / "settings.json"
    inode = path.stat().st_ino
    second = drain_seat(tmp_path, seat_id)
    assert first.changed is True
    assert second.changed is False
    assert path.stat().st_ino == inode
    current = json.loads(path.read_text())
    assert current["pool"]["primary"] is None
    assert current["pool"]["deputy"] is None
    deactivate_seat(tmp_path, seat_id)


def test_allocation_scans_ledger_archive_audit_and_orphan(tmp_path: Path) -> None:
    _write_settings(tmp_path)
    (tmp_path / "seat9").mkdir()
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "jobs-2020-01.jsonl").write_text(
        json.dumps({"origin_seat": "seat12"}) + "\n"
    )
    (tmp_path / AUDIT_FILENAME).write_text(
        json.dumps({"event": "allocation", "seat_id": "seat14"}) + "\n"
    )
    (tmp_path / AUDIT_FILENAME).chmod(0o600)
    (tmp_path / "seats.json").write_text(
        json.dumps({
            "schema": 6,
            "seats": {"seat10": {}},
            "jobs": [{"seat": "seat13"}],
        })
    )

    assert add_seat(tmp_path, label="Next", dry_run=True).seat_id == "seat15"


def test_crash_after_allocation_burns_id_and_materializes_ledger(tmp_path: Path) -> None:
    before = _write_settings(tmp_path)

    def crash(phase: str) -> None:
        if phase == "after_allocation_before_settings_replace":
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError, match="crash"):
        add_seat(tmp_path, label="Lost", hook=crash, _clock=lambda: NOW)
    assert (tmp_path / "settings.json").read_text() == before
    ledger = json.loads((tmp_path / "seats.json").read_text())
    assert "seat1" in ledger["seats"]
    assert "seat1" in ledger["seat_admin_allocations"]
    assert add_seat(tmp_path, label="Next", dry_run=True).seat_id == "seat2"


def test_crash_before_allocation_does_not_burn_id(tmp_path: Path) -> None:
    before = _write_settings(tmp_path)

    def crash(phase: str) -> None:
        if phase == "after_ledger_lock_before_claim_validation":
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError, match="crash"):
        add_seat(tmp_path, label="Never allocated", hook=crash)
    assert (tmp_path / "settings.json").read_text() == before
    assert not (tmp_path / AUDIT_FILENAME).exists()
    assert add_seat(tmp_path, label="Next", dry_run=True).seat_id == "seat1"


@pytest.mark.parametrize(
    "phase",
    ["after_backup_before_settings_replace", "before_settings_replace"],
)
def test_crash_after_backup_preserves_settings_and_backup(
    tmp_path: Path, phase: str
) -> None:
    before = _write_settings(tmp_path)
    seat_id = _add(tmp_path)
    original = (tmp_path / "settings.json").read_text()

    def crash(current: str) -> None:
        if current == phase:
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError, match="crash"):
        set_seat(tmp_path, seat_id, label="Changed", hook=crash)
    assert (tmp_path / "settings.json").read_text() == original
    backup = tmp_path / f"settings.json.backup-sha256-{hashlib.sha256(original.encode()).hexdigest()}"
    assert backup.read_text() == original
    assert before != original


def test_restore_cannot_reuse_retired_allocation(tmp_path: Path) -> None:
    empty = _write_settings(tmp_path)
    seat_id = _add(tmp_path)
    retire_seat(tmp_path, seat_id)
    (tmp_path / "settings.json").write_text(empty)
    (tmp_path / "settings.json").chmod(0o600)
    assert add_seat(tmp_path, label="After restore", dry_run=True).seat_id == "seat2"


def test_crash_after_replace_leaves_valid_retriable_state(tmp_path: Path) -> None:
    _write_settings(tmp_path)
    seat_id = _add(tmp_path)

    def crash(phase: str) -> None:
        if phase == "after_settings_replace":
            raise RuntimeError("crash")

    with pytest.raises(RuntimeError, match="crash"):
        set_seat(tmp_path, seat_id, label="Published", hook=crash)
    current = json.loads((tmp_path / "settings.json").read_text())
    assert current["pool"]["seats"][seat_id]["label"] == "Published"
    assert set_seat(tmp_path, seat_id, label="Published").changed is False


def test_stale_claim_is_cleared_before_idle_transition(tmp_path: Path) -> None:
    _write_settings(tmp_path)
    seat_id = _add(tmp_path)
    _activate_first(tmp_path, seat_id)
    ledger = json.loads((tmp_path / "seats.json").read_text())
    ledger["seats"][seat_id]["in_flight"] = {
        "job_id": "stale",
        "dispatcher_pid": 0,
        "child_pid": 0,
    }
    (tmp_path / "seats.json").write_text(json.dumps(ledger))
    deactivate_seat(tmp_path, seat_id, allow_empty=True)
    persisted = json.loads((tmp_path / "seats.json").read_text())
    assert persisted["seats"][seat_id]["in_flight"] is None


def test_hook_order_and_settings_replace_remain_inside_ledger_lock(tmp_path: Path) -> None:
    _write_settings(tmp_path)
    phases: list[str] = []
    result = add_seat(tmp_path, label="One", hook=phases.append)
    assert result.changed
    assert phases == [
        "after_mutation_reload_before_ledger_lock",
        "after_ledger_lock_before_claim_validation",
        "after_allocation_before_settings_replace",
        "after_backup_before_settings_replace",
        "before_settings_replace",
        "after_settings_replace",
    ]


def test_drain_wait_hook_is_lock_free_and_revalidates_before_deactivate(
    tmp_path: Path,
) -> None:
    _write_settings(tmp_path)
    seat_id = _add(tmp_path)
    _activate_first(tmp_path, seat_id)
    ledger = json.loads((tmp_path / "seats.json").read_text())
    ledger["seats"][seat_id]["in_flight"] = {
        "job_id": "live",
        "dispatcher_pid": os.getpid(),
    }
    (tmp_path / "seats.json").write_text(json.dumps(ledger))
    waited = False

    def hook(phase: str) -> None:
        nonlocal waited
        if phase != "drain_wait_without_locks":
            return
        # A complete settings->ledger mutation here would deadlock if drain
        # retained either lock at its wait boundary.
        set_seat(tmp_path, seat_id, label="Changed while waiting")
        current = json.loads((tmp_path / "seats.json").read_text())
        current["seats"][seat_id]["in_flight"] = None
        (tmp_path / "seats.json").write_text(json.dumps(current))
        waited = True

    result = drain_seat(
        tmp_path,
        seat_id,
        wait=True,
        allow_empty=True,
        wait_timeout=0,
        hook=hook,
    )
    assert waited
    assert result.action == "drain"
    current = json.loads(result.after)
    assert current["pool"]["seats"][seat_id]["lifecycle"] == "inactive"
    assert current["pool"]["seats"][seat_id]["label"] == "Changed while waiting"


def test_drain_wait_timeout_leaves_seat_draining(tmp_path: Path) -> None:
    _write_settings(tmp_path)
    seat_id = _add(tmp_path)
    _activate_first(tmp_path, seat_id)
    ledger = json.loads((tmp_path / "seats.json").read_text())
    ledger["seats"][seat_id]["in_flight"] = {
        "job_id": "live",
        "dispatcher_pid": os.getpid(),
    }
    (tmp_path / "seats.json").write_text(json.dumps(ledger))
    with pytest.raises(SeatAdminError, match="remains draining"):
        drain_seat(
            tmp_path,
            seat_id,
            wait=True,
            allow_empty=True,
            wait_timeout=0,
        )
    current = json.loads((tmp_path / "settings.json").read_text())
    assert current["pool"]["seats"][seat_id]["lifecycle"] == "draining"
    assert current["pool"]["primary"] is None


def test_drain_wait_dry_run_shows_eventual_inactive_state(tmp_path: Path) -> None:
    before = _write_settings(tmp_path)
    seat_id = _add(tmp_path)
    _activate_first(tmp_path, seat_id)
    current_before = (tmp_path / "settings.json").read_text()
    result = drain_seat(
        tmp_path,
        seat_id,
        wait=True,
        allow_empty=True,
        dry_run=True,
    )
    assert json.loads(result.after)["pool"]["seats"][seat_id]["lifecycle"] == "inactive"
    assert (tmp_path / "settings.json").read_text() == current_before
    assert current_before != before
