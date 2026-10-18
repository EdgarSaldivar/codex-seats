from __future__ import annotations

import copy
import time

import pytest

from codex_seats.capacity import (
    add_capacity_sample,
    available_baseline_units,
    capacity_task_fits,
    capacity_window_signature,
    learning_key,
    raise_risk_floor,
    seat_capacity_report,
    usable_baseline_units,
)
from codex_seats.seat_registry import (
    BUILTIN_CAPACITY_PROFILES,
    capacity_fingerprint,
)
from codex_seats.seat_selection import pick_seat
from codex_seats.state import (
    new_seat,
    default_settings,
    seat_task_min_headroom,
)


def _entry(profile: str, multiplier: float | None) -> dict:
    verified = multiplier is not None
    return {
        "label": profile,
        "lifecycle": "active",
        "home": None,
        "capacity_profile": profile,
        "relative_capacity": multiplier,
        "capacity_source": "openai-help-center" if verified else "unverified",
        "capacity_verified_at": "2026-09-03T00:00:00Z" if verified else None,
    }


def _settings() -> dict:
    settings = default_settings()
    settings["pool"]["seats"] = {
        "seat1": _entry("business-premium", 5.0),
        "seat2": _entry("business-standard", 1.0),
    }
    settings["pool"]["primary"] = "seat1"
    return settings


def _seat(pct: float, *, five_hour: float | None = None) -> dict:
    current = time.time()
    seat = new_seat()
    windows = {"weekly": {"pct": pct, "window_minutes": 10_080}}
    if five_hour is not None:
        windows["5h"] = {"pct": five_hour, "window_minutes": 300}
    seat.update(
        {
            "status": "ok",
            "pct_left": min(pct, five_hour) if five_hour is not None else pct,
            "usage_updated_at": "2026-09-03T00:00:00Z",
            "active_window_observation": {
                "source": "account",
                "complete": True,
                "observed_at": current,
            },
            "last_probe_limits": windows,
        }
    )
    return seat


def test_versioned_builtins_and_unknown_capacity_are_conservative():
    assert BUILTIN_CAPACITY_PROFILES["personal-free"]["relative_capacity"] is None
    assert BUILTIN_CAPACITY_PROFILES["personal-go"]["relative_capacity"] is None
    assert BUILTIN_CAPACITY_PROFILES["personal-plus"]["relative_capacity"] == 1.0
    assert BUILTIN_CAPACITY_PROFILES["personal-pro-5x"]["relative_capacity"] == 5.0
    assert BUILTIN_CAPACITY_PROFILES["personal-pro-20x"]["relative_capacity"] == 20.0
    assert BUILTIN_CAPACITY_PROFILES["business-standard"] == {
        "version": 1,
        "relative_capacity": 1.0,
        "expects_five_hour_window": True,
    }
    assert BUILTIN_CAPACITY_PROFILES["business-premium"] == {
        "version": 1,
        "relative_capacity": 5.0,
        "expects_five_hour_window": False,
    }
    unknown = _entry("operator-unknown", None)
    assert capacity_fingerprint(unknown) == "operator-unknown@1"
    close_a = _entry("operator-measured", 1.0000001)
    close_b = _entry("operator-measured", 1.0000002)
    assert capacity_fingerprint(close_a) != capacity_fingerprint(close_b)
    assert available_baseline_units(50, 5) == 250
    assert usable_baseline_units(10, 20, 5) == 0


def test_one_x_rolling_seat_drains_before_premium_and_reports_baseline_units():
    settings = _settings()
    seats = {"seat1": _seat(50), "seat2": _seat(100)}

    assert pick_seat(seats, settings, "small") == "seat2"
    report = seat_capacity_report(
        "seat1", seats["seat1"], settings, current=time.time()
    )
    assert report["capacity_profile"] == "business-premium"
    assert report["capacity_multiplier"] == 5
    assert report["capacity_fingerprint"] == "business-premium@5"
    assert report["binding_window"]["available_baseline_units"] == 250
    assert report["binding_window"]["usable_baseline_units"] == 250


def test_capacity_class_and_claude_size_floors_match_policy():
    settings = _settings()
    assert seat_task_min_headroom(settings, "seat2", "small") == 5
    assert seat_task_min_headroom(settings, "seat2", "big") == 10
    assert seat_task_min_headroom(settings, "seat1", "small") == 10
    assert seat_task_min_headroom(settings, "seat1", "big") == 20
    assert settings["providers"]["claude"]["small_task_min_headroom_pct"] == 10
    assert settings["providers"]["claude"]["big_task_min_headroom_pct"] == 15


def test_idle_premium_primary_dispatches_but_active_head_is_excluded():
    settings = _settings()
    seats = {"seat1": _seat(80), "seat2": _seat(100)}
    seats["seat2"]["in_flight"] = {"job_id": "busy"}

    assert pick_seat(seats, settings, "small") == "seat1"
    seats["seat2"]["in_flight"] = None
    assert pick_seat(seats, settings, "small", {"seat1"}) == "seat2"


def test_premium_fallback_is_converted_to_native_percent():
    settings = _settings()
    seat = _seat(50)
    details: dict = {}

    fits, _reason = capacity_task_fits(
        seat,
        "big",
        current=time.time(),
        stale_seconds=3600,
        settings=settings,
        seat_id="seat1",
        details=details,
    )

    assert fits
    assert details["predicted_burn_pct"] == 4
    assert details["predicted_native_burn_pct"] == 4
    assert details["predicted_baseline_burn_units"] == 20


def test_profile_fingerprints_partition_learning_and_changes_make_history_inert():
    settings = _settings()
    seat = _seat(40)
    signature = capacity_window_signature(seat, current=time.time())
    assert signature is not None
    standard = capacity_fingerprint(settings["pool"]["seats"]["seat2"])
    premium = capacity_fingerprint(settings["pool"]["seats"]["seat1"])
    standard_key = learning_key(
        "codex",
        signature,
        "gpt-5.6-sol",
        "small",
        "high",
        capacity_fingerprint=standard,
    )
    premium_key = learning_key(
        "codex",
        signature,
        "gpt-5.6-sol",
        "small",
        "high",
        capacity_fingerprint=premium,
    )
    assert standard_key != premium_key

    learning: dict = {}
    for _ in range(8):
        add_capacity_sample(
            learning,
            provider="codex",
            cadence=signature,
            model="gpt-5.6-sol",
            size="small",
            effort="high",
            burn_pct=99,
            capacity_fingerprint=premium,
            seat_id="seat1",
        )
    raise_risk_floor(
        learning,
        premium_key,
        99,
        None,
        capacity_fingerprint=premium,
        seat_id="seat1",
    )
    assert learning[premium_key]["capacity_fingerprint"] == premium
    assert learning[premium_key]["seat_id"] == "seat1"
    assert learning[premium_key]["risk_floor_pct"] == 99

    changed = copy.deepcopy(settings)
    changed["pool"]["seats"]["seat1"]["relative_capacity"] = 4.0
    details: dict = {}
    fits, _reason = capacity_task_fits(
        seat,
        "small",
        current=time.time(),
        stale_seconds=3600,
        settings=changed,
        learning=learning,
        model="gpt-5.6-sol",
        effort="high",
        seat_id="seat1",
        details=details,
    )
    assert fits
    assert premium_key in learning
    assert details["capacity_fingerprint"] == "business-premium@4"
    assert details["prediction_source"] == "fallback burn"
    assert details["risk_floor_pct"] == 0


def test_missing_premium_five_hour_is_valid_but_zero_windows_fail_closed():
    settings = _settings()
    premium = _seat(40)
    assert capacity_task_fits(
        premium,
        "small",
        current=time.time(),
        stale_seconds=3600,
        settings=settings,
        seat_id="seat1",
    )[0]


def test_legacy_reserve_is_ignored_but_too_large_job_still_fails_fit():
    settings = _settings()
    settings["pool"]["reserves_pct"]["seat2"] = 10
    settings["pool"]["task_min_headroom_pct_by_seat"]["seat2"] = {
        "small": 5,
        "big": 10,
    }
    seat = _seat(13, five_hour=100)
    current = time.time()
    details: dict = {}

    ordinary, _ = capacity_task_fits(
        seat,
        "small",
        current=current,
        stale_seconds=3600,
        settings=settings,
        seat_id="seat2",
    )
    compatibility, _ = capacity_task_fits(
        seat,
        "small",
        current=current,
        stale_seconds=3600,
        settings=settings,
        seat_id="seat2",
        details=details,
        spend_local_reserve=True,
    )

    assert ordinary is True
    assert compatibility is True
    assert details["configured_reserve_pct"] == 0
    assert details["reserve_pct"] == 0
    assert details["reserve_spent_for_job"] is False
    assert details["predicted_native_burn_pct"] == 5
    assert {item["window"]: item["headroom_pct"] for item in details["requirements"]} == {
        "5h": 100,
        "weekly": 13,
    }
    assert all(item["required_pct"] == 10 for item in details["requirements"])
    report = seat_capacity_report("seat2", seat, settings, current=current)
    assert report["reserve_pct"] == 0
    assert report["usable_headroom_pct"] == 13
    assert report["binding_usable_baseline_units"] == 13

    too_large = _seat(9, five_hour=100)
    assert capacity_task_fits(
        too_large,
        "small",
        current=current,
        stale_seconds=3600,
        settings=settings,
        seat_id="seat2",
    )[0] is False

    stale = copy.deepcopy(seat)
    stale["active_window_observation"]["observed_at"] = current - 7200
    assert capacity_task_fits(
        stale,
        "small",
        current=current,
        stale_seconds=3600,
        settings=settings,
        seat_id="seat2",
        spend_local_reserve=True,
        require_fresh_known=True,
    )[0] is False

    floor_only_settings = copy.deepcopy(settings)
    floor_only_settings["execution"]["capacity"]["fallback_burn_pct"]["small"] = 0
    floor_only_settings["execution"]["capacity"]["margin_pct"] = 0
    below_floor = _seat(4, five_hour=100)
    assert capacity_task_fits(
        below_floor,
        "small",
        current=time.time(),
        stale_seconds=3600,
        settings=floor_only_settings,
        seat_id="seat2",
        spend_local_reserve=True,
        require_fresh_known=True,
    )[0] is False

    signature = capacity_window_signature(seat, current=current)
    fingerprint = capacity_fingerprint(settings["pool"]["seats"]["seat2"])
    risk_key = learning_key(
        "codex",
        signature,
        "gpt-6-sol",
        "small",
        "default",
        capacity_fingerprint=fingerprint,
    )
    assert capacity_task_fits(
        seat,
        "small",
        current=current,
        stale_seconds=3600,
        settings=settings,
        learning={
            risk_key: {
                "samples": [],
                "risk_floor_pct": 14,
                "risk_until_reset": None,
            }
        },
        seat_id="seat2",
        spend_local_reserve=True,
        require_fresh_known=True,
    )[0] is False

    empty = _seat(40)
    empty["last_probe_limits"] = {}
    fits, reason = capacity_task_fits(
        empty,
        "small",
        current=time.time(),
        stale_seconds=3600,
        settings=settings,
        seat_id="seat1",
    )
    assert not fits
    assert "no active quota windows" in reason

    standard = _seat(100, five_hour=9)
    assert not capacity_task_fits(
        standard,
        "small",
        current=time.time(),
        stale_seconds=3600,
        settings=settings,
        seat_id="seat2",
    )[0]


@pytest.mark.parametrize("balance", [None, True, "13", float("inf"), float("nan"), 101, -1, 0])
def test_reserve_spending_rejects_invalid_reported_active_window(balance):
    settings = _settings()
    seat = _seat(13, five_hour=100)
    seat["last_probe_limits"]["weekly"]["pct"] = balance
    assert capacity_task_fits(
        seat, "small", current=time.time(), stale_seconds=3600,
        settings=settings, seat_id="seat2", spend_local_reserve=True,
        require_fresh_known=True,
    )[0] is False


def test_reserve_spending_ignores_expired_window_but_keeps_premium_weekly_only():
    settings = _settings()
    seat = _seat(13)
    seat["last_probe_limits"]["5h"] = {"pct": None, "resets_at": time.time() - 60}
    assert capacity_task_fits(
        seat, "small", current=time.time(), stale_seconds=3600,
        settings=settings, seat_id="seat1", spend_local_reserve=True,
        require_fresh_known=True,
    )[0] is True
