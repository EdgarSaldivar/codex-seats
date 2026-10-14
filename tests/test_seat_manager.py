from __future__ import annotations

import io
import json
import os
import threading
from pathlib import Path

import pytest

from codex_seats import seat_manager
from codex_seats.account import refresh_account_usage
from codex_seats.seat_admin import (
    activate_seat,
    add_seat,
    set_seat,
    settings_digest,
)
from codex_seats.seat_manager import (
    ActivationChecks,
    ManagerClosedError,
    ManagerEligibilityError,
    SeatManager,
    StalePreviewError,
    ensure_management_eligible,
)
from codex_seats.runtime_readers import SettingsReader, settings_identity
from codex_seats.seat_registry import CAPACITY_PLAN_PRESETS
from codex_seats.state import Ledger, load_settings, default_settings


NOW = 1_788_454_400.0
GUARD_VARS = (
    "CODEX_CONTROL_SEAT",
    "CODEX_SEATS_HEAD",
    "CODEX_SEATS_MANAGED_WORKER",
    "CODEX_THREAD_ID",
    "CODEX_HOME",
)


class _Tty(io.StringIO):
    def isatty(self) -> bool:  # noqa: D401 - trivial
        return True


def _clean_env(monkeypatch) -> None:
    for name in GUARD_VARS:
        monkeypatch.delenv(name, raising=False)


def _write_settings(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "settings.json").write_text(
        json.dumps(default_settings(), indent=2) + "\n"
    )
    (root / "settings.json").chmod(0o600)


def _add(root: Path, label: str) -> str:
    return add_seat(root, label=label, _clock=lambda: NOW).seat_id


def _activate(root: Path, seat_id: str, *, as_primary: bool) -> None:
    (root / seat_id).mkdir(mode=0o700, exist_ok=True)
    activate_seat(
        root,
        seat_id,
        verified_login=True,
        successful_probe=True,
        as_primary=as_primary,
    )


def _busy(root: Path, seat_id: str) -> None:
    with Ledger(root) as data:
        data["seats"].setdefault(seat_id, {})["in_flight"] = {
            "job_id": "live",
            "dispatcher_pid": os.getpid(),
        }


def _idle(root: Path, seat_id: str) -> None:
    with Ledger(root) as data:
        data["seats"].setdefault(seat_id, {})["in_flight"] = None


def _manager(root: Path) -> SeatManager:
    settings, _ = load_settings(root)
    return SeatManager(root, settings, clock=lambda: NOW)


# -- eligibility ------------------------------------------------------------
def test_eligibility_requires_real_tty(monkeypatch) -> None:
    _clean_env(monkeypatch)
    with pytest.raises(ManagerEligibilityError, match="terminal"):
        ensure_management_eligible(
            stdin=io.StringIO(), stdout=io.StringIO(), env={}
        )


def test_eligibility_rejects_managed_worker(monkeypatch) -> None:
    with pytest.raises(ManagerEligibilityError, match="managed worker"):
        ensure_management_eligible(
            stdin=_Tty(), stdout=_Tty(), env={"CODEX_SEATS_MANAGED_WORKER": "1"}
        )


def test_eligibility_rejects_head(monkeypatch) -> None:
    with pytest.raises(ManagerEligibilityError, match="controller/head"):
        ensure_management_eligible(
            stdin=_Tty(), stdout=_Tty(), env={"CODEX_SEATS_HEAD": "claude"}
        )


def test_eligibility_allows_real_terminal_outside_excluded_contexts() -> None:
    ensure_management_eligible(stdin=_Tty(), stdout=_Tty(), env={})


# -- inspection -------------------------------------------------------------
def test_list_report_includes_inactive_seats(tmp_path: Path) -> None:
    _write_settings(tmp_path)
    _add(tmp_path, "Inactive")
    manager = _manager(tmp_path)
    report = manager.list_report()
    ids = [seat["id"] for seat in report["seats"]]
    assert "seat1" in ids


def test_add_and_remove_use_guarded_dynamic_transactions(
    tmp_path: Path, monkeypatch
) -> None:
    _clean_env(monkeypatch)
    _write_settings(tmp_path)
    manager = _manager(tmp_path)

    # More than five identities are accepted and allocation never reuses a
    # retired stable ID.
    for number in range(1, 7):
        preview = manager.preview_add(label=f"Seat {number}")
        assert preview.seat_id == f"seat{number}"
        result = manager.commit(preview)
        assert result.seat_id == preview.seat_id
    remove = manager.preview_remove("seat6")
    assert remove.action == "retire"
    assert "preserves the seat home" in " ".join(remove.warnings)
    manager.commit(remove)

    next_preview = manager.preview_add(label="After retirement")
    assert next_preview.seat_id == "seat7"
    current = json.loads((tmp_path / "settings.json").read_text())
    assert current["pool"]["seats"]["seat6"]["lifecycle"] == "retired"


def test_add_commit_rejects_allocation_evidence_created_after_preview(
    tmp_path: Path, monkeypatch
) -> None:
    _clean_env(monkeypatch)
    _write_settings(tmp_path)
    manager = _manager(tmp_path)
    preview = manager.preview_add(label="Planned")
    before = (tmp_path / "settings.json").read_text()

    (tmp_path / preview.seat_id).mkdir()
    with pytest.raises(StalePreviewError, match="allocation changed"):
        manager.commit(preview)

    assert (tmp_path / "settings.json").read_text() == before


def test_remove_explains_inactive_idle_and_role_prerequisites(
    tmp_path: Path, monkeypatch
) -> None:
    _clean_env(monkeypatch)
    seat1, seat2 = _two_active(tmp_path)
    manager = _manager(tmp_path)

    with pytest.raises(seat_manager.ManagerError, match="already inactive"):
        manager.preview_remove(seat2)

    _idle(tmp_path, seat2)
    manager.commit_pause(manager.preview_pause(seat2))
    _busy(tmp_path, seat2)
    with pytest.raises(seat_manager.ManagerError, match="wait until it is idle"):
        manager.preview_remove(seat2)

    _idle(tmp_path, seat2)
    manager.reload()
    preview = manager.preview_remove(seat2)
    assert preview.seat_id == seat2
    assert seat1 != seat2


def test_identity_is_available_only_through_manager_identity_view(
    tmp_path: Path, monkeypatch
) -> None:
    _write_settings(tmp_path)
    seat_id = _add(tmp_path, "Identity")
    home = tmp_path / seat_id
    home.mkdir(mode=0o700)
    (home / "auth.json").write_text("mock-auth")
    settings, _warnings = load_settings(tmp_path)
    payload = {
        "rateLimits": None,
        "accountId": "provider-account-1",
        "_codex_seats_manager_identity": {
            "available": True,
            "account": {"account": {
                "type": "chatgpt", "email": "manager-only@example.test",
                "planType": "business",
            }},
        },
    }
    monkeypatch.setattr("codex_seats.workers.resolve_executable", lambda _name: "/fake/codex")
    monkeypatch.setattr(
        "codex_seats.account.read_account_rate_limits", lambda *_a, **_k: payload
    )
    refresh_account_usage(tmp_path, settings, [seat_id])
    manager = SeatManager(tmp_path, settings)
    identity = manager.identity_report(seat_id)
    assert identity["email"] == "manager-only@example.test"
    assert identity["account_id"] == "provider-account-1"
    assert identity["observed_plan_type"] == "business"
    assert "manager-only@example.test" not in repr(manager.list_report())
    assert "manager-only@example.test" not in repr(manager.detail_report(seat_id))
    from codex_seats import seats_cli
    monkeypatch.setattr(seats_cli, "_codex_login_support", lambda **_k: (None, None))
    doctor = seats_cli.seats_doctor_report(tmp_path, settings, include_all=True)
    assert "manager-only@example.test" not in repr(doctor)


# -- preview / commit -------------------------------------------------------
def test_preview_then_commit_set_label(tmp_path: Path, monkeypatch) -> None:
    _clean_env(monkeypatch)
    _write_settings(tmp_path)
    seat_id = _add(tmp_path, "Old")
    manager = _manager(tmp_path)

    preview = manager.preview_set(seat_id, label="New")
    assert preview.expected_digest == settings_digest(tmp_path)
    result = manager.commit(preview)

    assert result.changed
    current = json.loads((tmp_path / "settings.json").read_text())
    assert current["pool"]["seats"][seat_id]["label"] == "New"


def test_preview_options_are_immutable_and_detached(tmp_path: Path) -> None:
    _write_settings(tmp_path)
    seat_id = _add(tmp_path, "Old")
    manager = _manager(tmp_path)
    requested = {"label": "Frozen"}

    preview = manager.preview("set", seat_id, **requested)
    requested["label"] = "Mutated"

    assert preview.options["label"] == "Frozen"
    with pytest.raises(TypeError):
        preview.options["label"] = "Impossible"  # type: ignore[index]


def test_commit_rejects_stale_preview(tmp_path: Path, monkeypatch) -> None:
    _clean_env(monkeypatch)
    _write_settings(tmp_path)
    seat_id = _add(tmp_path, "Old")
    manager = _manager(tmp_path)
    preview = manager.preview_set(seat_id, label="Planned")

    # A change lands out-of-band after the preview.
    set_seat(tmp_path, seat_id, label="Concurrent")
    before = (tmp_path / "settings.json").read_text()

    with pytest.raises(StalePreviewError):
        manager.commit(preview)
    assert (tmp_path / "settings.json").read_text() == before


def test_commit_rejected_in_managed_worker_context(
    tmp_path: Path, monkeypatch
) -> None:
    _clean_env(monkeypatch)
    _write_settings(tmp_path)
    seat_id = _add(tmp_path, "Old")
    manager = _manager(tmp_path)
    preview = manager.preview_set(seat_id, label="New")

    monkeypatch.setenv("CODEX_SEATS_MANAGED_WORKER", "1")
    with pytest.raises(ManagerEligibilityError):
        manager.commit(preview)
    current = json.loads((tmp_path / "settings.json").read_text())
    assert current["pool"]["seats"][seat_id]["label"] == "Old"


def test_set_role_preview_and_commit(tmp_path: Path, monkeypatch) -> None:
    _clean_env(monkeypatch)
    _write_settings(tmp_path)
    seat1 = _add(tmp_path, "Primary")
    _activate(tmp_path, seat1, as_primary=True)
    seat2 = _add(tmp_path, "Worker")
    _activate(tmp_path, seat2, as_primary=False)
    manager = _manager(tmp_path)

    preview = manager.preview_set_role(seat2, "deputy")
    manager.commit(preview)

    current = json.loads((tmp_path / "settings.json").read_text())
    assert current["pool"]["deputy"] == seat2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("label", "Renamed"),
        ("reserve_pct", 7.5),
        ("small_task_min_headroom_pct", 8.0),
        ("big_task_min_headroom_pct", 21.0),
        ("profile", "operator-profile"),
    ],
)
def test_every_supported_seat_form_uses_core_preview_validation(
    tmp_path: Path, field: str, value: object
) -> None:
    _write_settings(tmp_path)
    seat_id = _add(tmp_path, "Seat")
    manager = _manager(tmp_path)

    preview = manager.preview_set(seat_id, **{field: value})

    assert preview.options[field] == value
    assert preview.before != preview.after
    assert f'"{seat_id}"' in preview.after


def test_capacity_form_values_are_validated_and_previewed_atomically(
    tmp_path: Path,
) -> None:
    _write_settings(tmp_path)
    seat_id = _add(tmp_path, "Seat")
    manager = _manager(tmp_path)

    preview = manager.preview_set(
        seat_id,
        profile="operator-profile",
        relative_capacity=2.5,
        capacity_source="operator-measured",
    )

    assert preview.options == {
        "profile": "operator-profile",
        "relative_capacity": 2.5,
        "capacity_source": "operator-measured",
    }
    assert preview.before != preview.after


@pytest.mark.parametrize(
    "profile",
    [
        "personal-free", "personal-go", "personal-plus", "personal-pro-5x",
        "personal-pro-20x", "business-standard", "business-premium",
    ],
)
def test_personal_and_business_plan_presets_use_real_core_preview(
    tmp_path: Path, profile: str,
) -> None:
    _write_settings(tmp_path)
    seat_id = _add(tmp_path, "Seat")
    manager = _manager(tmp_path)
    preset = next(item for item in CAPACITY_PLAN_PRESETS if item["profile"] == profile)

    preview = manager.preview_set(
        seat_id,
        profile=preset["profile"],
        relative_capacity=preset["relative_capacity"],
        capacity_source=preset["capacity_source"],
    )

    assert preview.options["profile"] == profile
    if profile in {"personal-free", "personal-go"}:
        assert preview.options["relative_capacity"] is None
        assert preview.options["capacity_source"] == "unverified"
    else:
        assert preview.options["relative_capacity"] in {1.0, 5.0, 20.0}
        assert preview.options["capacity_source"] == "openai-published-baseline"


# -- pause / pending drains -------------------------------------------------
def _two_active(tmp_path: Path) -> tuple[str, str]:
    _write_settings(tmp_path)
    seat1 = _add(tmp_path, "Primary")
    _activate(tmp_path, seat1, as_primary=True)
    seat2 = _add(tmp_path, "Worker")
    _activate(tmp_path, seat2, as_primary=False)
    return seat1, seat2


def test_pause_idle_seat_previews_deactivate(tmp_path: Path, monkeypatch) -> None:
    _clean_env(monkeypatch)
    _seat1, seat2 = _two_active(tmp_path)
    _idle(tmp_path, seat2)
    manager = _manager(tmp_path)

    plan = manager.preview_pause(seat2)
    assert plan.busy is False
    assert plan.mode == "deactivate"
    manager.commit_pause(plan)

    current = json.loads((tmp_path / "settings.json").read_text())
    assert current["pool"]["seats"][seat2]["lifecycle"] == "inactive"


def test_pause_busy_seat_drains_and_reconciles_to_idle(
    tmp_path: Path, monkeypatch
) -> None:
    _clean_env(monkeypatch)
    _seat1, seat2 = _two_active(tmp_path)
    _busy(tmp_path, seat2)
    manager = _manager(tmp_path)

    plan = manager.preview_pause(seat2)
    assert plan.busy is True
    assert plan.mode == "drain"
    outcome = manager.commit_pause(plan)
    assert outcome.pending_drain is not None
    assert outcome.pending_drain.state == "draining"

    current = json.loads((tmp_path / "settings.json").read_text())
    assert current["pool"]["seats"][seat2]["lifecycle"] == "draining"

    # Still busy: reconcile does not deactivate.
    pending = manager.reconcile_pending_drains()
    assert pending[0].state == "draining"

    # Now idle: reconcile completes through the guarded action layer.
    _idle(tmp_path, seat2)
    pending = manager.reconcile_pending_drains()
    assert pending[0].state == "deactivated"
    current = json.loads((tmp_path / "settings.json").read_text())
    assert current["pool"]["seats"][seat2]["lifecycle"] == "inactive"


def test_reconcile_does_not_overwrite_concurrent_reactivation(
    tmp_path: Path, monkeypatch
) -> None:
    _clean_env(monkeypatch)
    _seat1, seat2 = _two_active(tmp_path)
    _busy(tmp_path, seat2)
    manager = _manager(tmp_path)
    manager.commit_pause(manager.preview_pause(seat2))
    _idle(tmp_path, seat2)

    from codex_seats import seat_admin

    real_deactivate = seat_admin.deactivate_seat

    def reactivate_before_commit(root, selector, **kwargs):
        activate_seat(
            root,
            selector,
            verified_login=True,
            successful_probe=True,
        )
        return real_deactivate(root, selector, **kwargs)

    monkeypatch.setattr(seat_admin, "deactivate_seat", reactivate_before_commit)
    manager.reconcile_pending_drains()

    current = json.loads((tmp_path / "settings.json").read_text())
    assert current["pool"]["seats"][seat2]["lifecycle"] == "active"
    assert "deferred" in manager.pending_drains()[0].detail


def test_reconcile_runs_mutation_guard_before_completion(
    tmp_path: Path, monkeypatch
) -> None:
    _clean_env(monkeypatch)
    _seat1, seat2 = _two_active(tmp_path)
    _busy(tmp_path, seat2)
    manager = _manager(tmp_path)
    manager.commit_pause(manager.preview_pause(seat2))
    _idle(tmp_path, seat2)
    monkeypatch.setenv("CODEX_SEATS_MANAGED_WORKER", "1")

    with pytest.raises(ManagerEligibilityError):
        manager.reconcile_pending_drains()
    current = json.loads((tmp_path / "settings.json").read_text())
    assert current["pool"]["seats"][seat2]["lifecycle"] == "draining"


def test_cancel_pending_drain_leaves_seat_draining(
    tmp_path: Path, monkeypatch
) -> None:
    _clean_env(monkeypatch)
    _seat1, seat2 = _two_active(tmp_path)
    _busy(tmp_path, seat2)
    manager = _manager(tmp_path)
    manager.commit_pause(manager.preview_pause(seat2))

    canceled = manager.cancel_pending_drain(seat2)
    assert canceled.state == "canceled"

    # Even when idle, a canceled drain is not auto-completed.
    _idle(tmp_path, seat2)
    pending = manager.reconcile_pending_drains()
    assert pending[0].state == "canceled"
    current = json.loads((tmp_path / "settings.json").read_text())
    assert current["pool"]["seats"][seat2]["lifecycle"] == "draining"


def test_no_mutation_after_close(tmp_path: Path, monkeypatch) -> None:
    _clean_env(monkeypatch)
    _seat1, seat2 = _two_active(tmp_path)
    _busy(tmp_path, seat2)
    manager = _manager(tmp_path)
    manager.commit_pause(manager.preview_pause(seat2))

    still_draining = manager.close()
    assert [p.seat_id for p in still_draining] == [seat2]
    assert manager.closed is True

    # Idle now, but the closed manager must not complete the drain.
    _idle(tmp_path, seat2)
    pending = manager.reconcile_pending_drains()
    assert pending[0].state == "draining"
    current = json.loads((tmp_path / "settings.json").read_text())
    assert current["pool"]["seats"][seat2]["lifecycle"] == "draining"

    # A confirmed action cannot commit after close.
    _write_settings_unchanged = _manager(tmp_path)  # fresh view to preview from
    preview = _write_settings_unchanged.preview_set(seat2, label="Nope")
    with pytest.raises(ManagerClosedError):
        manager.commit(preview)


# -- activation (nonblocking, cancelable) -----------------------------------
def test_activation_checks_cancel_before_login(tmp_path: Path, monkeypatch) -> None:
    _clean_env(monkeypatch)
    _write_settings(tmp_path)
    seat_id = _add(tmp_path, "Inactive")
    (tmp_path / seat_id).mkdir(mode=0o700, exist_ok=True)
    manager = _manager(tmp_path)

    cancel = threading.Event()
    cancel.set()
    preview = manager.preview_reactivate(seat_id, as_primary=True)
    checks = manager.run_activation_checks(preview, cancel)
    assert checks.canceled is True
    assert checks.passed is False


def test_activation_checks_failed_login_leaves_inactive(
    tmp_path: Path, monkeypatch
) -> None:
    _clean_env(monkeypatch)
    _write_settings(tmp_path)
    seat_id = _add(tmp_path, "Inactive")
    (tmp_path / seat_id).mkdir(mode=0o700, exist_ok=True)
    manager = _manager(tmp_path)

    from codex_seats import seats_cli

    monkeypatch.setattr(
        seats_cli, "_codex_login_support", lambda *, exact_version: ("codex", "v")
    )
    monkeypatch.setattr(seats_cli, "_login_status", lambda _codex, _home: False)

    preview = manager.preview_reactivate(seat_id, as_primary=True)
    checks = manager.run_activation_checks(preview, threading.Event())
    assert checks.passed is False
    assert "login status" in checks.detail

    with pytest.raises(seat_manager.ManagerError):
        manager.commit_activation(preview, checks)
    current = json.loads((tmp_path / "settings.json").read_text())
    assert current["pool"]["seats"][seat_id]["lifecycle"] == "inactive"


def test_commit_activation_activates_with_confirmed_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    _clean_env(monkeypatch)
    _write_settings(tmp_path)
    seat_id = _add(tmp_path, "First")
    home = tmp_path / seat_id
    home.mkdir(mode=0o700, exist_ok=True)
    manager = _manager(tmp_path)

    preview = manager.preview_reactivate(seat_id, as_primary=True)
    checks = ActivationChecks(
        seat_id=seat_id,
        home=home,
        passed=True,
        verified_login=True,
        successful_probe=True,
        canceled=False,
        detail="confirmed in test",
        expected_digest=preview.expected_digest,
        as_primary=True,
    )
    result = manager.commit_activation(preview, checks)
    assert result.changed
    current = json.loads((tmp_path / "settings.json").read_text())
    assert current["pool"]["seats"][seat_id]["lifecycle"] == "active"
    assert current["pool"]["primary"] == seat_id


def test_activation_checks_are_bound_to_original_preview(
    tmp_path: Path, monkeypatch
) -> None:
    _clean_env(monkeypatch)
    _write_settings(tmp_path)
    seat_id = _add(tmp_path, "Inactive")
    (tmp_path / seat_id).mkdir(mode=0o700, exist_ok=True)
    manager = _manager(tmp_path)
    preview = manager.preview_reactivate(seat_id, as_primary=True)
    set_seat(tmp_path, seat_id, label="Changed during confirmation")

    with pytest.raises(StalePreviewError):
        manager.run_activation_checks(preview, threading.Event())


def test_activation_fails_if_settings_change_during_provider_probe(
    tmp_path: Path, monkeypatch
) -> None:
    _clean_env(monkeypatch)
    _write_settings(tmp_path)
    seat_id = _add(tmp_path, "Inactive")
    (tmp_path / seat_id).mkdir(mode=0o700, exist_ok=True)
    manager = _manager(tmp_path)
    preview = manager.preview_reactivate(seat_id, as_primary=True)
    from codex_seats import seats_cli

    monkeypatch.setattr(
        seats_cli, "_codex_login_support", lambda *, exact_version: ("codex", "v")
    )
    monkeypatch.setattr(seats_cli, "_login_status", lambda *_args: True)

    def probe_then_change(*_args) -> bool:
        set_seat(tmp_path, seat_id, label="Changed during provider I/O")
        return True

    monkeypatch.setattr(manager, "_cancelable_probe", probe_then_change)
    checks = manager.run_activation_checks(preview, threading.Event())

    assert checks.passed is False
    assert "changed during" in checks.detail
    current = json.loads((tmp_path / "settings.json").read_text())
    assert current["pool"]["seats"][seat_id]["lifecycle"] == "inactive"


def test_activation_refresh_lock_contention_fails_without_provider_calls(
    tmp_path: Path, monkeypatch
) -> None:
    _clean_env(monkeypatch)
    _write_settings(tmp_path)
    seat_id = _add(tmp_path, "Inactive")
    (tmp_path / seat_id).mkdir(mode=0o700, exist_ok=True)
    manager = _manager(tmp_path)
    preview = manager.preview_reactivate(seat_id, as_primary=True)
    calls: list[str] = []
    from codex_seats import watch

    monkeypatch.setattr(watch, "acquire_refresh_lock", lambda *_a, **_k: None)
    monkeypatch.setattr(manager, "_cancelable_probe", lambda *_a: calls.append("probe"))
    checks = manager.run_activation_checks(preview, threading.Event())

    assert checks.passed is False
    assert "shared refresh" in checks.detail
    assert calls == []


def test_commit_activation_rejects_checks_for_different_preview(
    tmp_path: Path, monkeypatch
) -> None:
    _clean_env(monkeypatch)
    _write_settings(tmp_path)
    seat_id = _add(tmp_path, "Inactive")
    home = tmp_path / seat_id
    home.mkdir(mode=0o700, exist_ok=True)
    manager = _manager(tmp_path)
    preview = manager.preview_reactivate(seat_id, as_primary=True)
    checks = ActivationChecks(
        seat_id=seat_id,
        home=home,
        passed=True,
        verified_login=True,
        successful_probe=True,
        canceled=False,
        detail="mismatched semantics",
        expected_digest=preview.expected_digest,
        as_primary=False,
    )

    with pytest.raises(StalePreviewError):
        manager.commit_activation(preview, checks)


# -- login snapshot and reader publication ---------------------------------
def test_login_plan_rejects_home_change_after_confirmation(
    tmp_path: Path, monkeypatch
) -> None:
    _clean_env(monkeypatch)
    _write_settings(tmp_path)
    seat_id = _add(tmp_path, "Login")
    manager = _manager(tmp_path)
    plan = manager.preview_login(seat_id)

    replacement = tmp_path / "replacement-home"
    set_seat(tmp_path, seat_id, home=str(replacement))

    with pytest.raises(StalePreviewError, match="settings/home changed"):
        manager.validate_login_plan(plan)


def test_login_validation_returns_original_confirmed_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    _clean_env(monkeypatch)
    _write_settings(tmp_path)
    seat_id = _add(tmp_path, "Login")
    manager = _manager(tmp_path)
    plan = manager.preview_login(seat_id)

    confirmed = manager.validate_login_plan(plan)

    assert confirmed == manager.settings
    assert confirmed is not manager.settings
    assert confirmed["pool"]["seats"][seat_id]["home"] is None


def test_manager_reload_publishes_fresh_settings_reader_identity(
    tmp_path: Path, monkeypatch
) -> None:
    _clean_env(monkeypatch)
    _write_settings(tmp_path)
    seat_id = _add(tmp_path, "Before")

    with SettingsReader(tmp_path, "dashboard-watch") as reader:
        settings, _warnings = reader.load(load_settings)
        before = dict(reader.metadata["observed_settings"])
        manager = SeatManager(
            tmp_path,
            settings,
            settings_loader=lambda: reader.load(load_settings),
        )
        manager.commit(manager.preview_set(seat_id, label="After"))

        assert reader.metadata["observed_settings"] == settings_identity(
            manager.settings
        )
        assert reader.metadata["observed_settings"] != before


# -- local hosts and seats (LocalSeatManager) --------------------------------
import copy  # noqa: E402
import sys  # noqa: E402
from contextlib import contextmanager  # noqa: E402

from codex_seats import local_cli, local_seats, state  # noqa: E402
from codex_seats.local_manager import (  # noqa: E402
    LocalActionPreview,
    LocalMutationResult,
    LocalSeatManager,
)
from codex_seats.watch import LocalProbeSnapshot  # noqa: E402


def _local_settings(root: Path) -> dict:
    settings = default_settings()
    settings["providers"]["local"]["mode"] = "opportunistic"
    settings["local_hosts"] = {"studio": {
        "label": "Studio", "lifecycle": "active",
        "resource_groups": {"gpu0": {"max_concurrency": 1}},
        "base_url": "http://127.0.0.1:8080/v1",
        "availability_probe": {"argv": [sys.executable, "-c", 'print(\'{"state":"ready"}\')'],
                               "timeout_seconds": 2},
    }}
    seat = {"label": "Coder", "lifecycle": "active", "mode": "opportunistic", "host": "studio",
            "resource_group": "gpu0", "model_profile": "coder", "harness": "qwen",
            "capabilities": {"task_shapes": ["execute"], "validation": ["strong"]},
            "max_concurrency": 1, "runtime_fingerprint": "sha256:" + "a" * 64}
    settings["local_seats"] = {"small": seat, "large": copy.deepcopy(seat)}
    settings["future"] = {"keep": [1, 2]}
    root.mkdir(parents=True, exist_ok=True)
    (root / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
    (root / "settings.json").chmod(0o600)
    return settings


def _human_terminal(monkeypatch) -> None:
    """A real human TTY in a plain shell (the only context the manager may write from)."""
    _clean_env(monkeypatch)
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)


class _FakeRefresher:
    def __init__(self, *, closed: bool = False) -> None:
        self.requests: list[str] = []
        self.settings = None
        self.closed = closed
        self.snapshot = LocalProbeSnapshot(
            enabled=True, periodic=True, probing=None, pending=(),
            hosts={"studio": {"state": "ready", "reason": "ready", "source": "periodic"}},
            generation=3, last_error=None,
        )

    def request_local_probe(self, host_id: str) -> None:
        if self.closed:
            raise RuntimeError("local probe service is closed")
        self.requests.append(host_id)

    def local_snapshot(self) -> LocalProbeSnapshot:
        return self.snapshot


def _forbid_probes(monkeypatch) -> None:
    monkeypatch.setattr(local_seats, "probe_host", lambda *a, **k: pytest.fail("manager ran a probe"))


def test_local_report_is_structured_and_read_only(tmp_path: Path, monkeypatch) -> None:
    _forbid_probes(monkeypatch)
    _local_settings(tmp_path)
    manager = LocalSeatManager(tmp_path, clock=lambda: NOW)
    report = manager.report()
    assert report["global_mode"] == "opportunistic"
    assert report["provider"]["command"] == "qwen"
    host = report["hosts"]["studio"]
    assert host["config"]["label"] == "Studio"
    assert host["availability"]["reason"] == "availability_missing_or_invalid"
    assert host["seats"] == ["large", "small"]
    assert host["probe"] is None and report["probe"] is None
    seat = report["seats"]["small"]
    assert seat["config"]["label"] == "Coder"
    assert seat["effective_mode"] == "opportunistic"
    assert seat["eligibility"]["resource_group"] == "gpu0"
    assert seat["eligibility"]["reason"] == "availability_missing_or_invalid"
    assert report["settings_digest"] == local_cli.settings_digest(tmp_path)
    assert [w for w in report["warnings"] if "'future' was preserved" in w] == report["warnings"]
    assert manager.host_report("studio") == host
    with pytest.raises(seat_manager.ManagerError, match="unknown local host"):
        manager.host_report("other")
    with pytest.raises(seat_manager.ManagerError, match="unknown local seat"):
        manager.seat_report("other")
    assert not (tmp_path / "local-runtime").exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["settings.json"]

    refreshed = LocalSeatManager(tmp_path, refresher=_FakeRefresher(), clock=lambda: NOW)
    report = refreshed.report()
    assert report["probe"]["generation"] == 3
    assert report["hosts"]["studio"]["probe"]["state"] == "ready"
    assert refreshed.local_snapshot().hosts["studio"]["reason"] == "ready"

    (tmp_path / "settings.json").write_text('{"schema":3,"local_hosts":null}')
    with pytest.raises(seat_manager.ManagerError):
        manager.report()


def test_local_report_status_rows_share_dashboard_derivation(tmp_path: Path, monkeypatch) -> None:
    """Gated, expired, fresh, working and configuration-changed caches map to
    the same off/stale/available/working/unknown rows the dashboard shows."""
    from codex_seats import dispatch_client

    _forbid_probes(monkeypatch)
    settings = _local_settings(tmp_path)
    settings["local_seats"]["large"]["mode"] = "off"
    (tmp_path / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
    runtime = tmp_path / "local-runtime"
    runtime.mkdir(mode=0o700)
    digest = local_seats._host_digest(settings["local_hosts"]["studio"])

    def cache(observed_at: float, *, config_digest: str = digest, **extra) -> None:
        path = runtime / "availability-studio.json"
        path.write_text(json.dumps({
            "host": "studio", "state": "ready", "reason": "ready", "detail": "GPU idle",
            "observed_at": observed_at, "config_digest": config_digest, **extra,
        }))
        path.chmod(0o600)

    manager = LocalSeatManager(tmp_path, clock=lambda: NOW)

    # Expired cache: stale with the last known state and its age (not degraded).
    cache(NOW - 120)
    report = manager.report(background_jobs=[])
    host = report["hosts"]["studio"]
    assert host["availability"]["reason"] == "availability_stale"
    assert host["status"]["key"] == "stale" and host["status"]["state"] == "~ stale"
    assert host["status"]["last_state"] == "ready" and host["status"]["detail"] == "last ready"
    assert host["status"]["age"] == 120.0 and host["status"]["age_text"] == "2m"
    assert host["status"]["observed_at"] == NOW - 120 and host["status"]["stale"] is True
    small = report["seats"]["small"]["status"]
    assert small["key"] == "stale" and small["age_text"] == "2m" and small["last_state"] == "ready"
    assert report["seats"]["small"]["eligibility"]["reason"] == "availability_stale"
    # A gated (seat mode off) seat is off, yet keeps the host's observation age.
    large = report["seats"]["large"]["status"]
    assert large["key"] == "off" and large["detail"] == "seat mode off"
    assert large["age_text"] == "2m" and large["observed_at"] == NOW - 120
    assert report["jobs_error"] is None

    # Working is asserted only for a running local job attributed to the seat.
    jobs = [
        {"state": "running", "provider": "local", "seat": "small", "label": "live\x1b[31m"},
        {"state": "queued", "provider": "local", "seat": "large", "label": "queued"},
        {"state": "running", "provider": "codex", "seat": "seat1", "label": "frontier"},
    ]
    working = manager.report(background_jobs=jobs)
    assert working["seats"]["small"]["status"]["key"] == "working"
    assert working["seats"]["small"]["status"]["detail"] == "live[31m"
    assert working["seats"]["large"]["status"]["key"] == "off"
    assert working["hosts"]["studio"]["status"]["key"] == "working"
    assert working["hosts"]["studio"]["status"]["working_seats"] == ["small"]
    assert working["hosts"]["studio"]["status"]["detail"] == "small: live[31m"

    # By default the validated live-job snapshot is read; a dispatch failure is
    # reported and simply yields no working attribution.
    seen: list[tuple] = []
    monkeypatch.setattr(
        dispatch_client, "live_jobs", lambda root, loaded, *, now=None: seen.append((root, now)) or jobs
    )
    assert manager.report()["seats"]["small"]["status"]["key"] == "working"
    assert seen == [(tmp_path, NOW)]

    def broken(root, loaded, *, now=None):
        raise dispatch_client.dispatch_protocol.DispatchError("queue unreadable")

    monkeypatch.setattr(dispatch_client, "live_jobs", broken)
    failed = manager.report()
    assert failed["jobs_error"] == "queue unreadable"
    assert failed["seats"]["small"]["status"]["key"] == "stale"

    # Fresh cache: available with the served model and probe detail.
    cache(NOW - 3, model_profile="coder")
    fresh = manager.report(background_jobs=[])
    assert fresh["hosts"]["studio"]["status"]["key"] == "ready"
    assert fresh["hosts"]["studio"]["status"]["state"] == "○ available"
    assert fresh["hosts"]["studio"]["status"]["detail"] == "GPU idle"
    assert fresh["hosts"]["studio"]["status"]["age_text"] == "3s"
    assert fresh["hosts"]["studio"]["status"]["model_profile"] == "coder"
    assert fresh["seats"]["small"]["status"]["key"] == "ready"
    assert fresh["seats"]["small"]["eligibility"]["eligible"] is True

    # A known configuration_changed result is unknown, never a probe failure.
    cache(NOW - 3, config_digest="0" * 64)
    changed = manager.report(background_jobs=[])
    assert changed["hosts"]["studio"]["availability"]["reason"] == "configuration_changed"
    assert changed["hosts"]["studio"]["status"]["key"] == "unknown"
    assert changed["hosts"]["studio"]["status"]["detail"] == "configuration changed"
    assert changed["hosts"]["studio"]["status"]["observed_at"] is None
    assert changed["seats"]["small"]["status"]["key"] == "unknown"
    assert changed["seats"]["small"]["eligibility"]["reason"] == "configuration_changed"
    assert "degraded" not in json.dumps([changed["hosts"]["studio"]["status"], changed["seats"]["small"]["status"]])

    # Global off: every row is off and the runtime cache is never touched.
    settings["providers"]["local"]["mode"] = "off"
    (tmp_path / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
    monkeypatch.setattr(
        local_seats, "_runtime", lambda *a, **k: pytest.fail("global off touched the runtime cache")
    )
    off = manager.report(background_jobs=[])
    assert off["global_mode"] == "off"
    assert off["hosts"]["studio"]["status"]["key"] == "off"
    assert off["hosts"]["studio"]["status"]["detail"] == "global mode off"
    assert off["hosts"]["studio"]["status"]["observed_at"] is None
    assert off["hosts"]["studio"]["availability"]["reason"] == "mode_off"
    assert {seat["status"]["key"] for seat in off["seats"].values()} == {"off"}
    assert off["seats"]["small"]["status"]["detail"] == "global mode off"
    assert sorted(path.name for path in runtime.iterdir()) == ["availability-studio.json"]


def test_local_preview_commit_transaction_preserves_unrelated_settings(
    tmp_path: Path, monkeypatch
) -> None:
    _human_terminal(monkeypatch)
    _forbid_probes(monkeypatch)
    _local_settings(tmp_path)
    path = tmp_path / "settings.json"
    original = path.read_bytes()
    refresher = _FakeRefresher()
    manager = LocalSeatManager(tmp_path, refresher=refresher, clock=lambda: NOW)
    changes = {"label": "Renamed"}

    preview = manager.preview_edit_seat("small", changes)
    changes["label"] = "Mutated after preview"
    assert isinstance(preview, LocalActionPreview)
    assert (preview.action, preview.kind, preview.target_id) == ("edit-seat", "seat", "small")
    assert preview.changed and not preview.enabling
    assert preview.entry_before["label"] == "Coder"
    assert preview.entry_after["label"] == "Renamed"
    assert preview.expected_digest == local_cli.settings_digest(tmp_path)
    assert preview.as_dict()["options"] == {"changes": {"label": "Renamed"}}
    with pytest.raises(TypeError):
        preview.options["changes"]["label"] = "Impossible"  # type: ignore[index]
    assert path.read_bytes() == original

    result = manager.commit(preview, from_current_user_request=True)
    assert isinstance(result, LocalMutationResult)
    assert result.changed and not result.enabling
    assert result.before_digest == preview.expected_digest
    assert result.after_digest == local_cli.settings_digest(tmp_path) != result.before_digest
    current = json.loads(path.read_text())
    assert current["future"] == {"keep": [1, 2]}
    assert current["local_seats"]["small"]["label"] == "Renamed"
    assert current["local_seats"]["large"]["label"] == "Coder"
    assert current["pool"] == default_settings()["pool"]
    assert manager.settings["local_seats"]["small"]["label"] == "Renamed"
    assert refresher.settings is manager.settings

    # Committing the same preview again is stale: the bytes it pinned are gone.
    with pytest.raises(StalePreviewError):
        manager.commit(preview, from_current_user_request=True)


def test_local_commit_requires_confirmation_marker(tmp_path: Path, monkeypatch) -> None:
    _human_terminal(monkeypatch)
    _local_settings(tmp_path)
    path = tmp_path / "settings.json"
    original = path.read_bytes()
    manager = LocalSeatManager(tmp_path, clock=lambda: NOW)
    preview = manager.preview_seat_mode("small", "off")
    with pytest.raises(seat_manager.ManagerError, match="explicit confirmation"):
        manager.commit(preview, from_current_user_request=False)
    with pytest.raises(seat_manager.ManagerError, match="explicit confirmation"):
        manager.commit(preview, from_current_user_request="yes")  # type: ignore[arg-type]
    assert path.read_bytes() == original
    with pytest.raises(seat_manager.ManagerError, match="not a local management action"):
        manager.preview("probe", "studio")
    with pytest.raises(seat_manager.ManagerError, match="mode must be one of"):
        manager.preview_seat_mode("small", "loud")


def test_local_commit_rejects_stale_preview(tmp_path: Path, monkeypatch) -> None:
    _human_terminal(monkeypatch)
    _local_settings(tmp_path)
    path = tmp_path / "settings.json"
    manager = LocalSeatManager(tmp_path, clock=lambda: NOW)
    preview = manager.preview_edit_seat("small", {"label": "Planned"})

    # A change lands out-of-band after the preview (through the ordinary CLI path).
    local_cli.apply_action(tmp_path, "set-seat-mode", "large", mode="off", expected_digest=None)
    before = path.read_bytes()

    with pytest.raises(StalePreviewError, match="re-preview"):
        manager.commit(preview, from_current_user_request=True)
    assert path.read_bytes() == before
    assert json.loads(before.decode())["local_seats"]["small"]["label"] == "Coder"


def test_local_commit_digest_is_checked_inside_the_settings_lock(
    tmp_path: Path, monkeypatch
) -> None:
    _human_terminal(monkeypatch)
    _local_settings(tmp_path)
    path = tmp_path / "settings.json"
    manager = LocalSeatManager(tmp_path, clock=lambda: NOW)
    preview = manager.preview_global_mode("off")
    real_transaction = state.settings_transaction

    @contextmanager
    def concurrent_writer(root, **kwargs):
        # Slips in after the manager's friendly pre-check but before the lock.
        with real_transaction(root, **kwargs) as transaction:
            transaction.settings["future"] = "concurrent edit"
        with real_transaction(root, **kwargs) as transaction:
            yield transaction

    monkeypatch.setattr(state, "settings_transaction", concurrent_writer)
    with pytest.raises(StalePreviewError, match="digest changed"):
        manager.commit(preview, from_current_user_request=True)
    current = json.loads(path.read_text())
    assert current["future"] == "concurrent edit"
    assert current["providers"]["local"]["mode"] == "opportunistic"


def test_local_manager_refuses_excluded_contexts_even_with_pty(
    tmp_path: Path, monkeypatch
) -> None:
    _human_terminal(monkeypatch)
    _forbid_probes(monkeypatch)
    _local_settings(tmp_path)
    path = tmp_path / "settings.json"
    original = path.read_bytes()
    refresher = _FakeRefresher()
    manager = LocalSeatManager(tmp_path, refresher=refresher, clock=lambda: NOW)
    preview = manager.preview_seat_mode("small", "off")
    contexts = (
        {"CODEX_CONTROL_SEAT": "seat1"},
        {"CODEX_SEATS_HEAD": "claude"},
        {"CODEX_SEATS_HEAD": "codex"},
        {"CODEX_THREAD_ID": "t1", "CODEX_HOME": str(tmp_path)},
        {"CODEX_SEATS_MANAGED_WORKER": "1"},
    )
    for env in contexts:
        with monkeypatch.context() as patch:
            for name, value in env.items():
                patch.setenv(name, value)
            with pytest.raises(ManagerEligibilityError):
                manager.commit(preview, from_current_user_request=True)
            with pytest.raises(ManagerEligibilityError):
                manager.request_probe("studio")
    with monkeypatch.context() as patch:
        patch.setattr(sys.stdin, "isatty", lambda: False)
        with pytest.raises(ManagerEligibilityError, match="terminal"):
            manager.commit(preview, from_current_user_request=True)
        with pytest.raises(ManagerEligibilityError, match="terminal"):
            manager.request_probe("studio")
    # Defense in depth: even with the manager's own pre-check bypassed, the
    # write layer's distinct manager authority still refuses excluded contexts.
    with monkeypatch.context() as patch:
        patch.setattr(manager, "_authorize", lambda action: None)
        patch.setenv("CODEX_CONTROL_SEAT", "seat1")
        with pytest.raises(ManagerEligibilityError, match="controller/head"):
            manager.commit(preview, from_current_user_request=True)
    assert refresher.requests == []
    assert path.read_bytes() == original
    # Reads stay available in every context; they never write or probe.
    with monkeypatch.context() as patch:
        patch.setenv("CODEX_SEATS_MANAGED_WORKER", "1")
        assert manager.report()["global_mode"] == "opportunistic"
    # And the eligible human terminal succeeds.
    assert manager.commit(preview, from_current_user_request=True).changed
    assert json.loads(path.read_text())["local_seats"]["small"]["mode"] == "off"


def test_local_lifecycle_previews_and_commits(tmp_path: Path, monkeypatch) -> None:
    _human_terminal(monkeypatch)
    _local_settings(tmp_path)
    path = tmp_path / "settings.json"
    manager = LocalSeatManager(tmp_path, clock=lambda: NOW)

    with pytest.raises(seat_manager.ManagerError, match="non-active local host"):
        manager.preview_host_lifecycle("studio", "inactive")
    for seat_id in ("small", "large"):
        preview = manager.preview_seat_lifecycle(seat_id, "inactive")
        assert preview.kind == "seat" and not preview.enabling
        assert preview.entry_after["lifecycle"] == "inactive"
        assert preview.entry_after["mode"] == "off"
        manager.commit(preview, from_current_user_request=True)
    with pytest.raises(seat_manager.ManagerError, match="mode off unless lifecycle is active"):
        manager.preview_seat_mode("small", "canary")
    with pytest.raises(seat_manager.ManagerError, match="transition"):
        manager.preview_edit_seat("small", {"lifecycle": "draining"})

    preview = manager.preview_host_lifecycle("studio", "inactive")
    assert preview.kind == "host" and preview.entry_after["lifecycle"] == "inactive"
    manager.commit(preview, from_current_user_request=True)
    retire = manager.preview_host_lifecycle("studio", "retired")
    manager.commit(retire, from_current_user_request=True)
    with pytest.raises(seat_manager.ManagerError, match="transition"):
        manager.preview_host_lifecycle("studio", "active")
    current = json.loads(path.read_text())
    assert current["local_hosts"]["studio"]["lifecycle"] == "retired"
    assert {seat["mode"] for seat in current["local_seats"].values()} == {"off"}
    assert current["future"] == {"keep": [1, 2]}
    report = manager.report()
    assert report["hosts"]["studio"]["availability"]["reason"] == "lifecycle_ineligible"
    assert report["seats"]["small"]["effective_mode"] == "off"


def test_local_remove_seat_retires_a_tombstone_behind_every_guard(
    tmp_path: Path, monkeypatch
) -> None:
    import fcntl
    import hashlib
    import os

    from codex_seats import dispatch_client

    _human_terminal(monkeypatch)
    _forbid_probes(monkeypatch)
    settings = _local_settings(tmp_path)
    path = tmp_path / "settings.json"
    manager = LocalSeatManager(tmp_path, clock=lambda: NOW)
    # Real dispatch snapshots never attribute a job to a local seat, so the
    # guard must not depend on them; held local execution slots are the signal.
    monkeypatch.setattr(
        dispatch_client, "live_jobs",
        lambda *args, **kwargs: pytest.fail("local removal consulted dispatch live jobs"),
    )

    # Only an inactive, off seat may be removed.
    with pytest.raises(seat_manager.ManagerError, match="lifecycle inactive and mode off"):
        manager.preview_remove_seat("small")
    with pytest.raises(seat_manager.ManagerError, match="unknown local id"):
        manager.preview_remove_seat("missing")
    with pytest.raises(seat_manager.ManagerError, match="takes no options"):
        manager.preview("remove-seat", "small", mode="off")
    manager.commit(manager.preview_seat_lifecycle("small", "inactive"), from_current_user_request=True)
    with pytest.raises(seat_manager.ManagerError, match="lifecycle inactive and mode off"):
        manager.preview_remove_seat("large")

    runtime = tmp_path / "local-runtime"
    runtime.mkdir(mode=0o700)
    slot = runtime / "seat-small-0.lock"
    slot.touch(mode=0o600)
    (runtime / "seat-large-0.lock").touch(mode=0o600)

    @contextmanager
    def held(path):
        holder = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(holder)

    # A local execution holding the seat's slot blocks every retirement path;
    # another seat's held slot does not.
    with held(slot):
        with pytest.raises(seat_manager.ManagerError, match="running local job"):
            manager.preview_remove_seat("small")
        with pytest.raises(seat_manager.ManagerError, match="running local job"):
            manager.preview_seat_lifecycle("small", "retired")
        with pytest.raises(seat_manager.ManagerError, match="running local job"):
            manager.preview_edit_seat("small", {"lifecycle": "retired"})
    with held(runtime / "seat-large-0.lock"):
        preview = manager.preview_remove_seat("small")
    assert (preview.action, preview.kind, preview.target_id) == ("remove-seat", "seat", "small")
    assert preview.changed and not preview.enabling
    assert preview.as_dict()["options"] == {}
    assert preview.entry_before["lifecycle"] == "inactive"
    assert preview.entry_after == settings["local_seats"]["small"] | {"lifecycle": "retired", "mode": "off"}
    assert any("retired in place" in warning for warning in preview.warnings)

    # The guard runs again on the locked re-read: an execution that started
    # after the preview refuses the commit with nothing written.
    before = path.read_bytes()
    with held(slot):
        with pytest.raises(seat_manager.ManagerError, match="running local job"):
            manager.commit(preview, from_current_user_request=True)
    assert path.read_bytes() == before

    # A slot lock vanishing mid-scan, or an unsafe runtime, fails closed.
    real_open = local_seats._open_file

    def vanishing(directory, name, **kwargs):
        if name == slot.name:
            os.unlink(name, dir_fd=directory)
        return real_open(directory, name, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(local_seats, "_open_file", vanishing)
        with pytest.raises(seat_manager.ManagerError, match="cannot verify .*disappeared"):
            manager.commit(preview, from_current_user_request=True)
    slot.touch(mode=0o600)
    runtime.chmod(0o755)
    try:
        with pytest.raises(seat_manager.ManagerError, match="cannot verify"):
            manager.commit(preview, from_current_user_request=True)
    finally:
        runtime.chmod(0o700)
    assert path.read_bytes() == before

    # Existing manager authority guards still apply.
    with pytest.raises(seat_manager.ManagerError, match="explicit confirmation"):
        manager.commit(preview, from_current_user_request=False)
    with monkeypatch.context() as patch:
        patch.setenv("CODEX_CONTROL_SEAT", "seat1")
        with pytest.raises(ManagerEligibilityError):
            manager.commit(preview, from_current_user_request=True)
    assert path.read_bytes() == before

    result = manager.commit(preview, from_current_user_request=True)
    assert result.changed and (result.action, result.kind) == ("remove-seat", "seat")
    assert result.before_digest == preview.expected_digest
    current = json.loads(path.read_text())
    assert current["local_seats"]["small"] == preview.entry_after
    assert current["local_seats"]["large"] == settings["local_seats"]["large"]
    assert current["future"] == {"keep": [1, 2]}
    backup = tmp_path / f"settings.json.backup-sha256-{hashlib.sha256(before).hexdigest()}"
    assert backup.read_bytes() == before
    assert sorted(p.name for p in runtime.iterdir()) == ["seat-large-0.lock", "seat-small-0.lock"]

    # The tombstone is terminal, stays reported, and a repeat is a no-op.
    report = manager.report(background_jobs=[])
    assert report["seats"]["small"]["config"]["lifecycle"] == "retired"
    assert report["seats"]["small"]["effective_mode"] == "off"
    assert not manager.preview_remove_seat("small").changed
    with pytest.raises(seat_manager.ManagerError, match="transition"):
        manager.preview_seat_lifecycle("small", "active")
    with pytest.raises(StalePreviewError):
        manager.commit(preview, from_current_user_request=True)


def test_local_add_host_seat_and_global_mode(tmp_path: Path, monkeypatch) -> None:
    _human_terminal(monkeypatch)
    settings = _local_settings(tmp_path)
    path = tmp_path / "settings.json"
    manager = LocalSeatManager(tmp_path, clock=lambda: NOW)

    host = settings["local_hosts"]["studio"] | {"label": "Other"}
    preview = manager.preview_add_host("other", host)
    assert preview.kind == "host" and preview.entry_before is None
    assert preview.entry_after == host
    with pytest.raises(seat_manager.ManagerError, match="already exists"):
        manager.preview_add_host("studio", host)
    manager.commit(preview, from_current_user_request=True)

    seat = settings["local_seats"]["small"] | {"host": "other"}
    preview = manager.preview_add_seat("third", seat)
    assert preview.enabling  # the new seat is enabled from the start
    manager.commit(preview, from_current_user_request=True)
    with pytest.raises(seat_manager.ManagerError, match="unsupported local edit field"):
        manager.preview_edit_host("other", {"token": "secret"})
    with pytest.raises(seat_manager.ManagerError):
        manager.preview_add_seat("fourth", seat | {"host": "missing"})

    preview = manager.preview_global_mode("off")
    assert (preview.kind, preview.target_id) == ("global", None)
    assert preview.entry_after["mode"] == "off" and not preview.enabling
    manager.commit(preview, from_current_user_request=True)
    assert manager.report()["global_mode"] == "off"
    assert manager.preview_global_mode("canary").enabling
    current = json.loads(path.read_text())
    assert current["local_hosts"]["other"]["label"] == "Other"
    assert current["local_seats"]["third"]["host"] == "other"
    assert current["future"] == {"keep": [1, 2]}


def test_local_probe_requests_go_through_refresher(tmp_path: Path, monkeypatch) -> None:
    _human_terminal(monkeypatch)
    _forbid_probes(monkeypatch)
    _local_settings(tmp_path)
    refresher = _FakeRefresher()
    manager = LocalSeatManager(tmp_path, refresher=refresher, clock=lambda: NOW)

    assert manager.request_probe("studio") == "studio"
    assert manager.request_probe("studio") == "studio"
    assert refresher.requests == ["studio", "studio"]
    with pytest.raises(seat_manager.ManagerError, match="unknown local host"):
        manager.request_probe("other")
    assert manager.local_snapshot() is refresher.snapshot
    assert manager.report()["probe"]["hosts"]["studio"]["state"] == "ready"

    manager.replace_refresher(_FakeRefresher(closed=True))
    with pytest.raises(seat_manager.ManagerError, match="closed"):
        manager.request_probe("studio")
    manager.replace_refresher(None)
    with pytest.raises(seat_manager.ManagerError, match="no active refresher"):
        manager.request_probe("studio")
    assert manager.local_snapshot() is None
    assert refresher.requests == ["studio", "studio"]
    assert not (tmp_path / "local-runtime").exists()


def test_seat_manager_shares_local_manager_and_closes_it(tmp_path: Path, monkeypatch) -> None:
    _human_terminal(monkeypatch)
    settings = _local_settings(tmp_path)
    refresher = _FakeRefresher()
    manager = SeatManager(tmp_path, settings, refresher=refresher, clock=lambda: NOW)

    local = manager.local_manager()
    assert isinstance(local, LocalSeatManager)
    assert manager.local_manager() is local
    assert local.refresher is refresher and local.root == tmp_path
    replacement = _FakeRefresher()
    manager.replace_refresher(replacement)
    assert local.refresher is replacement
    assert local.request_probe("studio") == "studio"
    assert replacement.requests == ["studio"]

    preview = local.preview_seat_mode("small", "off")
    local.commit(preview, from_current_user_request=True)
    assert manager.settings["local_seats"]["small"]["mode"] == "off"
    assert replacement.settings["local_seats"]["small"]["mode"] == "off"
    preview = local.preview_seat_mode("small", "opportunistic")
    manager.close()
    assert local.closed
    with pytest.raises(ManagerClosedError):
        local.commit(preview, from_current_user_request=True)
    with pytest.raises(ManagerClosedError):
        local.request_probe("studio")
    with pytest.raises(ManagerClosedError):
        manager.local_manager()
    assert json.loads((tmp_path / "settings.json").read_text())["local_seats"]["small"]["mode"] == "off"
