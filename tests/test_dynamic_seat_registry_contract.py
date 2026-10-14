"""Cross-cutting contract guards for the dynamic seat registry.

Settings have one format: an explicit schema-3 registry. These guards pin the
ledger boundary, lifecycle-aware enumeration, the sanitized two-seat golden,
and the absence of any fixed-topology reference in production source.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from unittest import mock

from codex_seats.account import refresh_account_usage
from codex_seats.display import dashboard, segment
from codex_seats.display_status import resets_report, status_json
from codex_seats.seat_selection import pick_seat
from codex_seats.state import (
    Ledger,
    iso,
    new_seat,
    refresh_transients,
    default_settings,
)
from registry_fixture import registry_settings, write_settings


REPOSITORY = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "dynamic_seat_registry"


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def _healthy_seat(pct_left: float, observed_at: float) -> dict:
    seat = new_seat()
    seat.update(
        {
            "status": "ok",
            "type": "window",
            "pct_left": pct_left,
            "probed_at": iso(observed_at),
            "usage_updated_at": iso(observed_at),
            "usage_source": "account",
            "last_probe_limits": {
                "weekly": {
                    "pct": pct_left,
                    "resets_at": observed_at + 7 * 86400,
                    "window_minutes": 10080,
                }
            },
            "active_window_observation": {
                "source": "account",
                "complete": True,
                "observed_at": observed_at,
                "observed_windows": ["weekly"],
                "active_windows": ["weekly"],
            },
        }
    )
    return seat


def test_ledger_schema6_fixture_materializes_registered_seats_and_preserves_history(
    tmp_path,
):
    """Guard the schema-6 input boundary: only registered seats materialize."""
    fixture = _fixture("ledger-schema6.json")
    (tmp_path / "seats.json").write_text(json.dumps(fixture))
    registered = ["seat1", "seat2", "seat10"]
    write_settings(tmp_path, registry_settings(registered))

    with Ledger(tmp_path) as data:
        assert data["schema"] == 6
        assert list(data["seats"]) == registered
        assert data["seats"]["seat1"]["pct_left"] == 64
        assert data["jobs"] == fixture["jobs"]
        assert data["capacity_learning"] == fixture["capacity_learning"]
        # Force a normal write to prove the serialized contract remains schema 6.
        data["fixture_characterized"] = True

    persisted = json.loads((tmp_path / "seats.json").read_text())
    assert persisted["schema"] == 6
    assert set(persisted["seats"]) == set(registered)


def test_empty_registry_ledger_materializes_no_seats(tmp_path):
    with Ledger(tmp_path) as data:
        assert data["seats"] == {}


def test_default_account_refresh_enumerates_operational_seats_and_probes_authenticated(
    tmp_path,
):
    """Default fan-out covers active and draining seats, never inactive ones."""
    settings = write_settings(tmp_path, registry_settings(["seat1", "seat2", "seat7", "seat11"]))
    settings["pool"]["seats"]["seat7"]["lifecycle"] = "draining"
    settings["pool"]["seats"]["seat11"]["lifecycle"] = "inactive"
    write_settings(tmp_path, settings)
    for seat_id in ("seat1", "seat2"):
        home = tmp_path / seat_id
        home.mkdir()
        (home / "auth.json").write_text("{}")
    observed = time.time()
    payload = {
        "rateLimits": {
            "primary": {
                "usedPercent": 10,
                "windowDurationMins": 10080,
                "resetsAt": observed + 86400,
            }
        },
    }

    with (
        mock.patch("codex_seats.workers.resolve_executable", return_value="/fake/codex"),
        mock.patch(
            "codex_seats.account.read_account_rate_limits", return_value=payload
        ) as read_account,
    ):
        messages = refresh_account_usage(tmp_path, settings)

    assert [message.split()[0] for message in messages] == ["seat1", "seat2", "seat7"]
    assert sorted(call.args[1].name for call in read_account.call_args_list) == [
        "seat1",
        "seat2",
    ]
    assert "needs-login" in messages[2]


def test_unquarantined_auth_file_can_revive_and_route_a_seat(tmp_path):
    """An unquarantined seat whose auth file returns becomes routable again."""
    settings = registry_settings()
    home = tmp_path / "seat3"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    observed = time.time()
    seats = {seat_id: new_seat() for seat_id in settings["pool"]["seats"]}
    seats["seat3"] = _healthy_seat(90, observed)
    seats["seat3"]["status"] = "needs-login"
    prior_usage = seats["seat3"]["usage_updated_at"]
    data = {"seats": seats, "providers": {"claude": {"status": "off"}}}

    refresh_transients(data, tmp_path, settings)

    assert data["seats"]["seat3"]["status"] == "ok"
    assert data["seats"]["seat3"]["usage_updated_at"] == prior_usage
    assert pick_seat(data["seats"], settings, "big") == "seat3"


def test_sanitized_two_seat_target_fixture_is_enforced_golden(tmp_path):
    """Enforce the sanitized two-seat golden against real operator surfaces."""
    target = _fixture("two-seat-target.json")
    fixture_settings = target["settings"]
    settings = default_settings()
    settings["pool"].update(fixture_settings["pool"])
    write_settings(tmp_path, settings)
    seats = settings["pool"]["seats"]

    assert settings["schema"] == 3
    assert list(seats) == ["seat1", "seat2"]
    assert settings["pool"]["primary"] == "seat1"
    assert settings["pool"]["deputy"] == "seat2"
    assert [seats[seat_id]["relative_capacity"] for seat_id in seats] == [5.0, 1.0]
    observed = time.time()
    for seat_id in seats:
        home = tmp_path / seat_id
        home.mkdir()
        (home / "auth.json").write_text("{}")
    with Ledger(tmp_path) as data:
        data["seats"]["seat1"] = _healthy_seat(80, observed)
        data["seats"]["seat2"] = _healthy_seat(80, observed)
        data["seats"]["seat2"]["last_probe_limits"]["5h"] = {
            "pct": 90,
            "resets_at": observed + 5 * 3600,
            "window_minutes": 300,
        }
    machine = json.loads(status_json(tmp_path, settings))
    summary = machine["pool_summary"]
    expected = target["status"]
    assert {"healthy": summary["healthy"], "active": summary["active"]} == expected[
        "counts"
    ]
    assert {
        "codex_primary_head": summary["head_capacity"]["default_primary_codex_head"][
            "codex_worker_slots"
        ],
        "claude_head": summary["head_capacity"]["claude_head"]["codex_worker_slots"],
    } == expected["effective_codex_worker_slots"]
    assert {
        "global_safety": summary["global_safety_ceiling"],
        "default_primary_codex_head": summary[
            "default_primary_codex_head_ceiling"
        ],
        "claude_head": summary["claude_head_ceiling"],
        "conservative_mixed_origin": summary[
            "conservative_mixed_origin_ceiling"
        ],
    } == expected["ceilings"]
    for seat_id, golden in expected["seats"].items():
        actual = machine["seats"][seat_id]
        assert actual["label"] == golden["label"]
        assert actual["role"] == golden["role"]
        assert actual["declared_relative_multiplier"] == golden["relative_capacity"]
        assert actual["binding_available_baseline_units"] == golden[
            "binding_available_baseline_units"
        ]
        assert actual["binding_usable_baseline_units"] == golden[
            "binding_usable_baseline_units"
        ]
        assert actual["profile_window_drift"] is golden["profile_window_drift"]
    rendered = dashboard(tmp_path, settings, [], color=False, width=160, at=observed)
    tmux = segment(tmp_path, settings, plain=True)
    resets = resets_report(tmp_path, settings)
    for text in target["dashboard_contains"]:
        assert text in rendered
    for text in target["segment_contains"]:
        assert text in tmux
    for text in target["resets_contains"]:
        assert text in resets
    serialized = json.dumps(target).lower()
    assert "/users/" not in serialized
    assert not re.search(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", serialized)
    assert '"token"' not in serialized
    assert '"email"' not in serialized


def _production_python_files() -> list[Path]:
    return [
        path
        for directory in ("lib", "scripts", "bootstrap")
        for path in sorted((REPOSITORY / directory).rglob("*.py"))
    ]


def _occurrence_inventory(pattern: str) -> dict[str, int]:
    found = {}
    for path in _production_python_files():
        count = path.read_text().count(pattern)
        if count:
            found[str(path.relative_to(REPOSITORY))] = count
    return found


def _fixed_five_wording_inventory() -> dict[str, int]:
    wording = re.compile(
        r"(?:five|5)[- ](?:Codex )?(?:seats?|accounts?)|all (?:five|5)\b|"
        r"seat1\.\.seat5|/5\b|1\.\.5\b|W1\|W2\|W3",
        re.IGNORECASE,
    )
    candidates = [REPOSITORY / "README.md", REPOSITORY / "codex-seat"]
    for directory, suffixes in (
        ("docs", {".md"}),
        ("templates", None),
        ("lib", {".py"}),
        ("scripts", {".py", ".sh"}),
        ("bootstrap", {".py", ".sh"}),
    ):
        for path in sorted((REPOSITORY / directory).rglob("*")):
            if path.is_file() and (suffixes is None or path.suffix in suffixes):
                candidates.append(path)
    found = {}
    for path in candidates:
        count = sum(bool(wording.search(line)) for line in path.read_text().splitlines())
        if count:
            found[str(path.relative_to(REPOSITORY))] = count
    return dict(sorted(found.items()))


def test_production_source_has_no_fixed_topology_or_role_alias_reference():
    """Seats are registered identities; no fixed list, alias, or migration remains."""
    for pattern in (
        "SEAT_IDS",
        "LEGACY_IDS",
        "range(1, 6)",
        "selector_labels",
        "migrate_schema",
        "legacy-unclassified",
    ):
        assert _occurrence_inventory(pattern) == {}, pattern
    assert _fixed_five_wording_inventory() == {}
