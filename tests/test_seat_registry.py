from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_seats import state
from codex_seats.seat_registry import (
    SeatRegistryError,
    derive_roles,
    effective_relative_capacity,
    registered_seat_ids,
    resolve_home,
    resolve_readonly_selector,
    sort_seat_ids,
    validate_lifecycle_transition,
    validate_schema3,
    validate_seat_id,
)


def _entry(*, lifecycle="active", home=None):
    return {
        "label": "Local seat",
        "lifecycle": lifecycle,
        "home": home,
        "capacity_profile": "personal-plus",
        "relative_capacity": None,
        "capacity_source": "unverified",
        "capacity_verified_at": None,
    }


def _registry(count: int):
    settings = state.default_settings()
    settings["pool"]["seats"] = {
        f"seat{number}": _entry() for number in range(1, count + 1)
    }
    settings["pool"]["primary"] = "seat1" if count else None
    return settings


def test_new_install_defaults_require_explicit_onboarding():
    settings = state.default_settings()
    assert settings["schema"] == 3
    assert settings["pool"]["seats"] == {}
    assert settings["pool"]["primary"] is None
    assert "deputy" not in settings["pool"]
    validate_schema3(settings)


def test_strict_ids_and_numeric_sorting_are_unbounded():
    assert sort_seat_ids(["seat10", "seat2", "seat101", "seat1"]) == [
        "seat1",
        "seat2",
        "seat10",
        "seat101",
    ]
    for invalid in ("seat0", "seat01", "Seat1", "seat-1", "1", "seat1x", ""):
        with pytest.raises(SeatRegistryError):
            validate_seat_id(invalid)


@pytest.mark.parametrize("count", [0, 1, 2, 5, 6, 12])
def test_schema3_accepts_arbitrary_registry_counts(count, tmp_path):
    settings = _registry(count)
    validate_schema3(settings, tmp_path)
    assert registered_seat_ids(settings) == [
        f"seat{number}" for number in range(1, count + 1)
    ]


def test_roles_selectors_and_homes_are_derived_without_io(tmp_path):
    settings = _registry(6)
    # A stray legacy deputy never yields a deputy role.
    settings["pool"]["deputy"] = "seat4"
    settings["pool"]["seats"]["seat3"]["lifecycle"] = "inactive"
    settings["pool"]["seats"]["seat6"]["home"] = "/var/local/codex-seat6"

    assert derive_roles(settings) == {
        "seat1": "primary",
        "seat2": "worker",
        "seat4": "worker",
        "seat5": "worker",
        "seat6": "worker",
    }
    assert resolve_readonly_selector("6", settings) == "seat6"
    assert resolve_readonly_selector("seat3", settings) == "seat3"
    for alias in ("P", "D", "W2", "w", "m1", "06", "seat7"):
        with pytest.raises(SeatRegistryError, match="unknown seat selector"):
            resolve_readonly_selector(alias, settings)
    assert resolve_home(tmp_path, "seat2", settings["pool"]["seats"]["seat2"]) == (
        tmp_path / "seat2"
    )
    assert resolve_home(
        tmp_path, "seat6", settings["pool"]["seats"]["seat6"]
    ) == Path("/var/local/codex-seat6")


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"schema": 1},
        {"schema": 2, "pool": {"primary": "seat1", "workers": ["seat3"]}},
        {"schema": "3"},
    ],
)
def test_older_or_missing_schema_is_rejected_without_inventing_a_registry(
    tmp_path, document
):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(document))
    path.chmod(0o600)

    settings, warnings = state.load_settings(tmp_path)

    assert settings["pool"]["seats"] == {}
    assert settings["pool"]["primary"] is None
    assert any("only schema 3" in warning for warning in warnings)
    with pytest.raises(ValueError, match="only schema 3"):
        state.set_claude_mode(tmp_path, "off")
    assert json.loads(path.read_text()) == document


def test_schema3_rejects_fixed_topology_keys(tmp_path):
    settings = _registry(2)
    settings["pool"]["workers"] = ["seat2"]
    with pytest.raises(SeatRegistryError, match="pool.workers"):
        validate_schema3(settings, tmp_path)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda settings: settings["pool"].update(primary=None), "primary"),
        (
            lambda settings: settings["pool"]["seats"].update(seat01=_entry()),
            "invalid seat id",
        ),
        (
            lambda settings: settings["pool"]["seats"]["seat2"].update(home="relative"),
            "absolute path",
        ),
        (
            lambda settings: settings["pool"]["seats"]["seat2"].update(
                capacity_profile="Business-Premium"
            ),
            "lowercase slug",
        ),
        (
            lambda settings: settings["pool"]["seats"]["seat2"].update(email="x@y.test"),
            "forbidden",
        ),
    ],
)
def test_schema3_rejects_invariant_violations(tmp_path, mutate, message):
    settings = _registry(2)
    mutate(settings)
    with pytest.raises(SeatRegistryError, match=message):
        validate_schema3(settings, tmp_path)


def test_capacity_provenance_and_canonical_home_uniqueness(tmp_path):
    settings = _registry(2)
    settings["pool"]["seats"]["seat1"].update(
        capacity_profile="business-premium",
        relative_capacity=5.0,
        capacity_source="openai-help-center",
        capacity_verified_at="2026-09-03T00:00:00Z",
    )
    validate_schema3(settings, tmp_path)
    assert effective_relative_capacity(settings["pool"]["seats"]["seat1"]) == 5
    assert effective_relative_capacity(settings["pool"]["seats"]["seat2"]) == 1

    settings["pool"]["seats"]["seat1"]["relative_capacity"] = 0.01
    with pytest.raises(SeatRegistryError, match="0.1 to 1000"):
        validate_schema3(settings, tmp_path)

    settings = _registry(2)
    settings["pool"]["seats"]["seat1"]["home"] = str(tmp_path / "same")
    settings["pool"]["seats"]["seat2"]["home"] = str(tmp_path / "x" / ".." / "same")
    with pytest.raises(SeatRegistryError, match="home collision"):
        validate_schema3(settings, tmp_path)


def test_lifecycle_transition_table_is_explicit_and_retired_is_terminal():
    validate_lifecycle_transition("active", "draining")
    validate_lifecycle_transition("draining", "inactive")
    validate_lifecycle_transition("inactive", "retired")
    with pytest.raises(SeatRegistryError):
        validate_lifecycle_transition("retired", "inactive")
    with pytest.raises(SeatRegistryError):
        validate_lifecycle_transition("active", "retired")


def test_newer_schema_loads_as_explicit_no_capacity(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"schema": 99}))
    settings, warnings = state.load_settings(tmp_path)
    assert settings["schema"] == 3
    assert settings["pool"]["seats"] == {}
    assert any("no Codex seat capacity was synthesized" in warning for warning in warnings)


def test_failed_atomic_apply_keeps_original_and_recoverable_backup(
    tmp_path, monkeypatch
):
    path = tmp_path / "settings.json"
    original = json.dumps(_registry(1))
    path.write_text(original)
    path.chmod(0o600)

    def fail_write(*args, **kwargs):
        raise OSError("injected replace failure")

    monkeypatch.setattr(state, "atomic_write_json", fail_write)
    with pytest.raises(OSError, match="injected replace failure"):
        state.set_claude_mode(tmp_path, "manual_only")

    assert path.read_text() == original
    backups = list(tmp_path.glob("settings.json.backup-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == original
