from __future__ import annotations

import copy
import time
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

import pytest

from codex_seats.account import AccountReadError, refresh_account_usage
from codex_seats.provider_codex import run_codex
from codex_seats.state import (
    Ledger,
    atomic_write_json,
    read_pool_snapshot,
    default_settings,
)
from codex_seats.telemetry import refresh_passive_usage
from codex_seats.worker_config import sync_codex_config


def _entry(
    seat_id: str,
    lifecycle: str = "active",
    *,
    home: Path | None = None,
) -> dict:
    return {
        "label": seat_id,
        "lifecycle": lifecycle,
        "home": str(home) if home is not None else None,
        "capacity_profile": "personal-plus",
        "relative_capacity": None,
        "capacity_source": "unverified",
        "capacity_verified_at": None,
    }


def _settings(
    lifecycles: dict[str, str],
    *,
    homes: dict[str, Path] | None = None,
) -> dict:
    settings = default_settings()
    homes = homes or {}
    settings["pool"]["seats"] = {
        seat_id: _entry(seat_id, lifecycle, home=homes.get(seat_id))
        for seat_id, lifecycle in lifecycles.items()
    }
    active = [
        seat_id for seat_id, lifecycle in lifecycles.items() if lifecycle == "active"
    ]
    settings["pool"]["primary"] = active[0] if active else None
    return settings


def _write_settings(root: Path, settings: dict) -> None:
    atomic_write_json(root / "settings.json", settings)


def _healthy(seat: dict) -> None:
    observed = time.time()
    seat.update(
        {
            "status": "ok",
            "pct_left": 100,
            "probed_at": "2026-09-03T00:00:00Z",
            "usage_updated_at": "2026-09-03T00:00:00Z",
            "last_probe_limits": {
                "weekly": {
                    "pct": 100,
                    "window_minutes": 10080,
                    "resets_at": observed + 86400,
                }
            },
            "active_window_observation": {
                "source": "account",
                "complete": True,
                "observed_at": observed,
                "observed_windows": ["weekly"],
                "active_windows": ["weekly"],
            },
        }
    )


@pytest.mark.parametrize("count", [0, 1, 2, 5, 7])
def test_schema3_registry_materializes_exact_dynamic_counts(
    tmp_path: Path, count: int
) -> None:
    ids = [f"seat{number}" for number in range(1, count + 1)]
    settings = _settings({seat_id: "active" for seat_id in ids})
    _write_settings(tmp_path, settings)

    with Ledger(tmp_path) as data:
        assert list(data["seats"]) == ids
    assert list(read_pool_snapshot(tmp_path)["seats"]) == ids


def test_historical_ledger_seats_survive_without_becoming_operational(
    tmp_path: Path,
) -> None:
    settings = _settings({"seat2": "active", "seat6": "inactive"})
    _write_settings(tmp_path, settings)
    atomic_write_json(
        tmp_path / "seats.json",
        {
            "schema": 6,
            "seats": {"seat2": {}, "seat4": {"historical": True}},
            "jobs": [{"seat": "seat4", "status": "completed"}],
        },
    )

    with Ledger(tmp_path) as data:
        assert set(data["seats"]) == {"seat2", "seat4", "seat6"}
        assert data["seats"]["seat4"] == {"historical": True}
        assert "account_window_observation" in data["seats"]["seat2"]
        assert "account_window_observation" in data["seats"]["seat6"]
    snapshot = read_pool_snapshot(tmp_path)
    assert snapshot["seats"]["seat4"] == {"historical": True}
    assert snapshot["jobs"] == [{"seat": "seat4", "status": "completed"}]


def test_account_defaults_and_explicit_inactive_probe_follow_lifecycle(
    tmp_path: Path,
) -> None:
    custom = tmp_path / "custom-draining-home"
    settings = _settings(
        {
            "seat1": "active",
            "seat2": "draining",
            "seat3": "inactive",
            "seat4": "retired",
        },
        homes={"seat2": custom},
    )
    _write_settings(tmp_path, settings)
    for home in (tmp_path / "seat1", custom, tmp_path / "seat3"):
        home.mkdir(parents=True)
        (home / "auth.json").write_text("{}")
    payload = {
        "rateLimits": {
            "primary": {
                "usedPercent": 5,
                "windowDurationMins": 10080,
                "resetsAt": time.time() + 86400,
            }
        }
    }
    homes: list[Path] = []

    def read(_codex, home, _scratch, **_kwargs):
        homes.append(home)
        return payload

    with (
        mock.patch("codex_seats.workers.resolve_executable", return_value="/codex"),
        mock.patch("codex_seats.account.read_account_rate_limits", side_effect=read),
    ):
        assert len(refresh_account_usage(tmp_path, settings)) == 2
        assert set(homes) == {tmp_path / "seat1", custom}
        homes.clear()
        refresh_account_usage(tmp_path, settings, ["seat3"])
        assert homes == [tmp_path / "seat3"]
        with pytest.raises(AccountReadError, match="retired"):
            refresh_account_usage(tmp_path, settings, ["seat4"])
        with pytest.raises(AccountReadError, match="not registered"):
            refresh_account_usage(tmp_path, settings, ["seat99"])


def test_passive_telemetry_defaults_to_active_and_draining_custom_homes(
    tmp_path: Path,
) -> None:
    homes = {
        "seat1": tmp_path / "custom-active",
        "seat2": tmp_path / "custom-draining",
        "seat3": tmp_path / "custom-inactive",
    }
    settings = _settings(
        {"seat1": "active", "seat2": "draining", "seat3": "inactive"},
        homes=homes,
    )
    _write_settings(tmp_path, settings)
    with Ledger(tmp_path) as data:
        visited: list[Path] = []

        def rollouts(home: Path, *_args, **_kwargs):
            visited.append(home)
            return []

        with mock.patch("codex_seats.telemetry._rollouts", side_effect=rollouts):
            refresh_passive_usage(data, tmp_path, settings, force_discover=True)
    assert visited == [homes["seat1"], homes["seat2"]]


def test_config_sync_uses_custom_target_and_refuses_non_operational_targets(
    tmp_path: Path,
) -> None:
    source = tmp_path / "custom-primary"
    target = tmp_path / "custom-seat6"
    source.mkdir()
    (source / "config.toml").write_text('model = "test"\n')
    settings = _settings(
        {"seat1": "active", "seat6": "active", "seat7": "inactive"},
        homes={"seat1": source, "seat6": target},
    )

    assert sync_codex_config(tmp_path, settings, "seat6")
    assert (target / "config.toml").read_text() == 'model = "test"\n'
    assert not (tmp_path / "seat6").exists()
    with pytest.raises(ValueError, match="lifecycle=inactive"):
        sync_codex_config(tmp_path, settings, "seat7")
    with pytest.raises(ValueError, match="not registered"):
        sync_codex_config(tmp_path, settings, "seat8")


@pytest.mark.parametrize("lifecycle", ["draining", "inactive", "retired"])
def test_forced_codex_claim_rejects_every_non_active_lifecycle(
    tmp_path: Path, lifecycle: str
) -> None:
    settings = _settings({"seat1": "active", "seat6": lifecycle})
    _write_settings(tmp_path, settings)
    with Ledger(tmp_path) as data:
        _healthy(data["seats"]["seat6"])

    result = run_codex(
        tmp_path,
        settings,
        {"prompt": "bounded", "seat": 6, "no_probe": True},
    )

    assert result.status == "unavailable"
    assert lifecycle in (result.message or "")
    with Ledger(tmp_path) as data:
        assert data["seats"]["seat6"].get("in_flight") is None


def test_forced_codex_claim_rejects_unregistered_without_ghost_creation(
    tmp_path: Path,
) -> None:
    settings = _settings({"seat1": "active"})
    _write_settings(tmp_path, settings)
    with Ledger(tmp_path):
        pass

    result = run_codex(
        tmp_path,
        settings,
        {"prompt": "bounded", "seat": 6, "no_probe": True},
    )

    assert result.status == "unavailable"
    assert "not registered" in (result.message or "")
    with Ledger(tmp_path) as data:
        assert "seat6" not in data["seats"]


def test_automatic_codex_claim_uses_dynamic_seat_and_honors_head_exclusion(
    tmp_path: Path,
) -> None:
    settings = _settings({"seat1": "active", "seat6": "active"})
    _write_settings(tmp_path, settings)
    for seat_id in ("seat1", "seat6"):
        home = tmp_path / seat_id
        home.mkdir()
        (home / "auth.json").write_text("{}")
        (home / "config.toml").write_text("")
    with Ledger(tmp_path) as data:
        _healthy(data["seats"]["seat1"])
        _healthy(data["seats"]["seat6"])

    with (
        mock.patch("codex_seats.workers.resolve_executable", return_value="/codex"),
        mock.patch("codex_seats.workers.PowerLease", return_value=nullcontext()),
        mock.patch(
            "codex_seats.workers._execute",
            return_value=("completed", "", 0, 1),
        ),
    ):
        result = run_codex(
            tmp_path,
            settings,
            {
                "prompt": "bounded",
                "kind": "mechanical",
                "no_probe": True,
                "cd": str(tmp_path),
                "_excluded_seats": ["seat1"],
            },
        )

    assert result.status == "completed"
    assert result.worker == "seat6"
    with Ledger(tmp_path) as data:
        assert data["jobs"][-1]["seat"] == "seat6"


@pytest.mark.parametrize("new_lifecycle", ["draining", "inactive"])
def test_codex_claim_barrier_reloads_lifecycle_before_publication(
    tmp_path: Path, new_lifecycle: str
) -> None:
    supplied = _settings({"seat1": "active", "seat6": "active"})
    _write_settings(tmp_path, supplied)
    with Ledger(tmp_path) as data:
        _healthy(data["seats"]["seat6"])
    current = copy.deepcopy(supplied)
    current["pool"]["seats"]["seat6"]["lifecycle"] = new_lifecycle

    def barrier(phase: str) -> None:
        if phase == "after_provider_ledger_lock_before_settings_reload":
            _write_settings(tmp_path, current)

    result = run_codex(
        tmp_path,
        supplied,
        {
            "prompt": "bounded",
            "seat": 6,
            "no_probe": True,
            "_claim_hook": barrier,
        },
    )

    assert result.status == "unavailable"
    assert new_lifecycle in (result.message or "")
    with Ledger(tmp_path) as data:
        assert data["seats"]["seat6"].get("in_flight") is None


def test_automatic_codex_claim_barrier_excludes_newly_draining_seat(
    tmp_path: Path,
) -> None:
    supplied = _settings({"seat1": "active", "seat6": "active"})
    _write_settings(tmp_path, supplied)
    with Ledger(tmp_path) as data:
        _healthy(data["seats"]["seat1"])
        _healthy(data["seats"]["seat6"])
    current = copy.deepcopy(supplied)
    current["pool"]["seats"]["seat6"]["lifecycle"] = "draining"

    def barrier(phase: str) -> None:
        if phase == "after_provider_ledger_lock_before_settings_reload":
            _write_settings(tmp_path, current)

    result = run_codex(
        tmp_path,
        supplied,
        {
            "prompt": "bounded",
            "kind": "mechanical",
            "no_probe": True,
            "_excluded_seats": ["seat1"],
            "_claim_hook": barrier,
        },
    )

    assert result.status == "unavailable"
    with Ledger(tmp_path) as data:
        assert data["seats"]["seat6"].get("in_flight") is None
