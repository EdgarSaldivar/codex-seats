from __future__ import annotations

import json
import time

import pytest

from codex_seats.display import dashboard, segment
from codex_seats.display_status import resets_status_json, status_json
from codex_seats.seats_cli import seats_list_report
from codex_seats.state import Ledger, iso, new_seat, default_settings


def _entry(number: int, *, lifecycle: str = "active") -> dict:
    premium = number == 1
    return {
        "label": "Premium" if premium else f"Standard-{number}",
        "lifecycle": lifecycle,
        "home": None,
        "capacity_profile": "business-premium" if premium else "business-standard",
        "relative_capacity": 5.0 if premium else 1.0,
        "capacity_source": "openai-help-center",
        "capacity_verified_at": "2026-09-03T00:00:00Z",
    }


def _runtime(number: int, now: float, *, pct: float = 80) -> dict:
    seat = new_seat()
    limits = {
        "weekly": {
            "pct": pct,
            "resets_at": now + 7 * 86400,
            "window_minutes": 10080,
        }
    }
    if number != 1:
        limits["5h"] = {
            "pct": pct + 10,
            "resets_at": now + 5 * 3600,
            "window_minutes": 300,
        }
    seat.update(
        {
            "status": "ok",
            "pct_left": pct,
            "probed_at": iso(now),
            "usage_updated_at": iso(now),
            "usage_source": "account",
            "last_probe_limits": limits,
            "active_window_observation": {
                "source": "account",
                "complete": True,
                "observed_at": now,
                "observed_windows": list(limits),
                "active_windows": list(limits),
            },
        }
    )
    return seat


def _pool(tmp_path, count: int, *, now: float) -> dict:
    settings = default_settings()
    settings["pool"]["seats"] = {
        f"seat{number}": _entry(number) for number in range(1, count + 1)
    }
    settings["pool"]["primary"] = "seat1" if count else None
    (tmp_path / "settings.json").write_text(json.dumps(settings))
    for number in range(1, count + 1):
        home = tmp_path / f"seat{number}"
        home.mkdir()
        (home / "auth.json").write_text("{}")
    with Ledger(tmp_path) as data:
        for number in range(1, count + 1):
            data["seats"][f"seat{number}"] = _runtime(number, now)
    return settings


@pytest.mark.parametrize(
    ("count", "codex_head", "claude_head", "mixed"),
    [(0, 0, 0, 0), (1, 1, 1, 1), (2, 2, 2, 2), (5, 3, 3, 3), (7, 3, 3, 3)],
)
def test_schema3_pool_summary_cardinality_goldens(
    tmp_path, count, codex_head, claude_head, mixed
):
    now = time.time()
    settings = _pool(tmp_path, count, now=now)

    result = json.loads(status_json(tmp_path, settings))
    summary = result["pool_summary"]

    assert list(result["seats"]) == [f"seat{number}" for number in range(1, count + 1)]
    assert (summary["healthy"], summary["active"]) == (count, count)
    assert summary["lifecycle"] == {
        "active": count,
        "draining": 0,
        "inactive": 0,
        "retired": 0,
    }
    assert summary["global_safety_ceiling"] == 3
    assert summary["default_primary_codex_head_ceiling"] == codex_head
    assert summary["current_codex_head_ceiling"] == codex_head
    assert summary["claude_head_ceiling"] == claude_head
    assert summary["conservative_mixed_origin_ceiling"] == mixed
    assert summary["effective_worker_slots"] == {"codex": count, "claude": 0}


def test_schema3_capacity_fields_and_profile_window_semantics(tmp_path):
    now = time.time()
    settings = _pool(tmp_path, 2, now=now)
    with Ledger(tmp_path) as data:
        # Invert the documented window shapes to make both drift paths visible.
        data["seats"]["seat1"]["last_probe_limits"]["5h"] = {
            "pct": 70,
            "resets_at": now + 3600,
            "window_minutes": 300,
        }
        data["seats"]["seat2"]["last_probe_limits"].pop("5h")

    result = json.loads(status_json(tmp_path, settings))
    premium = result["seats"]["seat1"]
    standard = result["seats"]["seat2"]

    for seat in (premium, standard):
        assert seat["healthy"] is True
        assert seat["routable"] is True
        assert seat["capacity_source"] == "openai-help-center"
        assert seat["capacity_verified_at"] == "2026-09-03T00:00:00Z"
        assert seat["capacity_fingerprint"]
        assert seat["active_capacity_windows"]
        assert seat["binding_available_baseline_units"] is not None
        assert seat["binding_usable_baseline_units"] is not None
        assert seat["unverified"] is False
    assert premium["declared_relative_multiplier"] == 5
    assert premium["effective_relative_multiplier"] == 5
    assert premium["profile_window_drift"] is True
    assert "Premium" in premium["profile_window_drift_reason"]
    assert standard["profile_window_drift"] is True
    assert "Standard" in standard["profile_window_drift_reason"]

    with Ledger(tmp_path) as data:
        data["seats"]["seat1"]["last_probe_limits"] = {}
    zero = json.loads(status_json(tmp_path, settings))["seats"]["seat1"]
    assert zero["zero_authoritative_windows"] is True
    assert zero["healthy"] is False
    assert zero["routable"] is False
    assert zero["profile_window_drift"] is False


def test_lifecycle_all_view_and_narrow_operator_outputs(tmp_path):
    now = time.time()
    settings = _pool(tmp_path, 2, now=now)
    settings["pool"]["seats"]["seat3"] = _entry(3, lifecycle="inactive")
    settings["pool"]["seats"]["seat4"] = _entry(4, lifecycle="retired")
    (tmp_path / "settings.json").write_text(json.dumps(settings))
    with Ledger(tmp_path):
        pass

    ordinary = seats_list_report(tmp_path, settings)
    all_view = seats_list_report(tmp_path, settings, include_all=True)
    assert [seat["id"] for seat in ordinary["seats"]] == ["seat1", "seat2", "seat3"]
    assert [seat["id"] for seat in all_view["seats"]] == [
        "seat1",
        "seat2",
        "seat3",
        "seat4",
    ]
    assert ordinary["pool_summary"]["lifecycle"]["retired"] == 1
    assert ordinary["pool_summary"]["healthy"] == 2
    assert ordinary["pool_summary"]["effective_worker_slots"]["codex"] == 2

    narrow = dashboard(tmp_path, settings, [], color=False, width=50, height=12, at=now)
    assert all(len(line) <= 50 for line in narrow.splitlines())
    assert "Ready 2/2" in narrow
    assert "P Premium Premium 5x" in narrow
    tmux = segment(tmp_path, settings, plain=True)
    assert "H:2/2" in tmux and "Premium(seat1)@5x" in tmux
    resets = json.loads(resets_status_json(tmp_path, settings))
    assert resets["pool_summary"]["lifecycle"]["retired"] == 1
    assert resets["codex"]["seat1"]["binding_available_baseline_units"] == 400
