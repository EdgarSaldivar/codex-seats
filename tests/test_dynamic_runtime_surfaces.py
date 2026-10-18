from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest import mock

import pytest

from codex_seats import bounded_process, cli, dispatch_client, head_launcher
from codex_seats.display import dashboard, segment
from codex_seats.display_status import resets_report, resets_status_json, status_json
from codex_seats.state import Ledger, new_seat, default_settings
from codex_seats.watch import WatchRefresher


def _entry(lifecycle: str = "active", *, home: str | None = None) -> dict:
    return {
        "label": lifecycle.title(),
        "lifecycle": lifecycle,
        "home": home,
        "capacity_profile": "unverified",
        "relative_capacity": None,
        "capacity_source": "unverified",
        "capacity_verified_at": None,
    }


def _settings(root: Path, seats: dict[str, str], *, primary: str | None = None) -> dict:
    settings = default_settings()
    settings["pool"]["seats"] = {
        seat_id: _entry(lifecycle) for seat_id, lifecycle in seats.items()
    }
    active = [seat_id for seat_id, lifecycle in seats.items() if lifecycle == "active"]
    chosen_primary = primary if primary is not None else (active[0] if active else None)
    settings["pool"]["primary"] = chosen_primary
    (root / "settings.json").write_text(json.dumps(settings))
    with Ledger(root):
        pass
    return settings


@pytest.mark.parametrize("count", [0, 1, 2, 5, 7])
def test_status_enumerates_exact_registry_cardinality_without_ghosts(tmp_path, count):
    ids = [f"seat{number}" for number in range(1, count + 1)]
    settings = _settings(tmp_path, {seat_id: "active" for seat_id in ids})

    result = json.loads(status_json(tmp_path, settings))

    assert list(result["seats"]) == ids
    assert result["seat_counts"]["active"] == count
    assert result["seat_counts"]["healthy"] <= count
    assert result["runtime_reader"]["reader_kind"] == "status-command"
    assert result["runtime_reader"]["supported_settings"] == {"min": 3, "max": 3}
    assert result["runtime_reader"]["observed_settings"]["schema"] == 3
    assert result["settings_readers"] == []


def test_selectors_resolve_only_stable_ids_and_numeric_shorthand(capsys, tmp_path):
    settings = _settings(
        tmp_path,
        {"seat2": "active", "seat6": "active", "seat10": "active"},
        primary="seat2",
    )

    assert cli._selected_seat("10", settings) == "seat10"
    assert cli._selected_seat(10, settings) == "seat10"
    assert cli._selected_seat("seat6", settings) == "seat6"
    for alias in ("P", "D", "W1", "W", "m1", "seat3"):
        assert cli._selected_seat(alias, settings) is None
    assert cli._numeric_selected_seat("10", settings) == 10
    with pytest.raises(ValueError, match="unknown seat selector"):
        cli._numeric_selected_seat("P", settings)
    assert capsys.readouterr().err == ""


def test_lifecycle_views_hide_retired_and_do_not_count_inactive_as_capacity(tmp_path):
    settings = _settings(
        tmp_path,
        {
            "seat1": "active",
            "seat2": "draining",
            "seat3": "inactive",
            "seat10": "retired",
        },
    )
    with Ledger(tmp_path) as data:
        data["seats"]["seat99"] = new_seat()

    machine = json.loads(status_json(tmp_path, settings))
    rendered = dashboard(tmp_path, settings, [], color=False, width=220)
    resets = resets_report(tmp_path, settings)
    reset_json = json.loads(resets_status_json(tmp_path, settings))
    tmux = segment(tmp_path, settings, plain=True)

    assert list(machine["seats"]) == ["seat1", "seat2", "seat3"]
    assert machine["seats"]["seat2"]["lifecycle"] == "draining"
    assert machine["seats"]["seat3"]["role"] is None
    assert machine["seats"]["seat2"]["fits_small"] is False
    assert machine["seats"]["seat3"]["fits_small"] is False
    assert reset_json["codex"]["seat3"]["lifecycle"] == "inactive"
    assert machine["seat_counts"] == {
        "healthy": 0,
        "active": 1,
        "draining": 1,
        "inactive": 1,
        "retired": 1,
    }
    assert "Active Unverified" in rendered
    assert "Draining Unverified" in rendered
    assert "Inactive Unverified" not in rendered
    assert "seat1" in resets and "seat2" in resets and "seat3" in resets
    for text in (rendered, resets):
        assert "seat10" not in text and "seat99" not in text
    assert "[draining]" in resets and "[paused]" in resets
    assert "seat2[draining]" in tmux
    assert "seat3" not in tmux and "seat10" not in tmux and "seat99" not in tmux


def test_empty_registry_head_fails_clearly_and_custom_seat10_home_is_used(tmp_path):
    empty = _settings(tmp_path, {})
    with pytest.raises(ValueError, match="no primary"):
        head_launcher.codex_head_command(tmp_path, empty, binary="/bin/codex")

    custom = tmp_path / "custom-home"
    settings = _settings(tmp_path, {"seat10": "active"}, primary="seat10")
    settings["pool"]["seats"]["seat10"]["home"] = str(custom)
    (tmp_path / "settings.json").write_text(json.dumps(settings))
    _argv, environment = head_launcher.codex_head_command(
        tmp_path, settings, seat=10, binary="/bin/codex"
    )
    assert environment["CODEX_HOME"] == str(custom)
    assert environment["CODEX_CONTROL_SEAT"] == "seat10"


def test_dispatch_claim_join_uses_only_registered_operational_identities(tmp_path):
    settings = _settings(
        tmp_path,
        {
            "seat2": "draining",
            "seat3": "inactive",
            "seat6": "active",
            "seat10": "retired",
        },
        primary="seat6",
    )
    labels = {
        "seat2": "2" * 32,
        "seat3": "3" * 32,
        "seat6": "6" * 32,
        "seat10": "a" * 32,
        "seat99": "9" * 32,
    }
    with Ledger(tmp_path) as data:
        data["seats"]["seat99"] = new_seat()
        for seat_id, label in labels.items():
            data["seats"][seat_id]["in_flight"] = {
                "dispatcher_pid": 1,
                "label": label,
            }

    assignments = dispatch_client._actual_assignments(tmp_path, settings)

    assert assignments == {
        labels["seat2"]: ("codex", "seat2"),
        labels["seat6"]: ("codex", "seat6"),
    }


def test_dispatch_claim_join_reloads_registry_instead_of_using_stale_caller_settings(
    tmp_path,
):
    supplied = _settings(
        tmp_path,
        {"seat2": "active", "seat6": "active"},
        primary="seat2",
    )
    with Ledger(tmp_path) as data:
        for seat_id in ("seat2", "seat6"):
            data["seats"][seat_id]["in_flight"] = {
                "dispatcher_pid": 1,
                "label": seat_id[-1] * 32,
            }

    current = json.loads(json.dumps(supplied))
    current["pool"]["seats"]["seat6"]["lifecycle"] = "inactive"
    (tmp_path / "settings.json").write_text(json.dumps(current))

    assignments = dispatch_client._actual_assignments(tmp_path, supplied)

    assert assignments == {"2" * 32: ("codex", "seat2")}


def test_watch_uses_active_and_draining_denominator_including_empty(tmp_path):
    for seats, expected in [({}, "Codex 0/0"), ({"seat6": "active", "seat10": "draining"}, "Codex 2/2")]:
        root = tmp_path / ("empty" if not seats else "two")
        root.mkdir()
        settings = _settings(root, seats)
        captured = []

        def refresh(_root, _settings, targets, **_kwargs):
            captured.append(list(targets))
            return [f"{seat_id} account -> ok" for seat_id in targets]

        refresher = WatchRefresher(
            root,
            settings,
            300,
            account_refresh=refresh,
        )
        refresher.run_attempt()
        assert captured == [list(seats)]
        assert expected in refresher.snapshot().last_result
        assert refresher.snapshot().last_succeeded is True


def test_login_status_discards_all_provider_output_and_uses_operational_targets(
    tmp_path, monkeypatch, capsys
):
    settings = _settings(
        tmp_path,
        {"seat1": "active", "seat2": "draining", "seat3": "inactive", "seat4": "retired"},
    )
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    results = iter(
        [
            bounded_process.ProcessResult(("codex",), 0, b"user@example.test member-123 oauth", b"", 1),
            bounded_process.ProcessResult(("codex",), 1, b"", b"refresh-token=secret", 1),
        ]
    )
    with (
        mock.patch("codex_seats.cli.resolve_executable", return_value="/bin/codex"),
        mock.patch("codex_seats.cli.bounded_process.run", side_effect=lambda *_a, **_k: next(results)),
    ):
        assert cli.cmd_logins(argparse.Namespace(seat=None), settings) == 1

    output = capsys.readouterr().out
    assert "seat1: authenticated" in output
    assert "seat2: not-confirmed" in output
    assert "seat3" not in output and "seat4" not in output
    for forbidden in ("user@example.test", "member-123", "oauth", "refresh-token", "secret"):
        assert forbidden not in output
    assert (
        "codex-seat seats login seat2 --from-current-user-request" in output
    )
    assert str(tmp_path) not in output


def test_probe_and_config_sync_defaults_target_active_and_draining(tmp_path, monkeypatch):
    settings = _settings(
        tmp_path,
        {"seat1": "active", "seat6": "draining", "seat7": "inactive", "seat8": "retired"},
    )
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    with mock.patch("codex_seats.cli.probe_seats", return_value=[]) as probe:
        assert cli.cmd_probe(argparse.Namespace(seat=None), settings) == 0
    probe.assert_called_once_with(tmp_path, settings, ["seat1", "seat6"])

    with mock.patch(
        "codex_seats.cli.refresh_account_usage", return_value=[]
    ) as inspect_inactive:
        assert cli.cmd_probe(argparse.Namespace(seat="seat7"), settings) == 0
    inspect_inactive.assert_called_once_with(tmp_path, settings, ["seat7"])

    with mock.patch("codex_seats.cli.sync_codex_config", return_value=False) as sync:
        assert cli.cmd_config(
            argparse.Namespace(
                config_command=None,
                apply=False,
                from_current_user_request=False,
                set_primary_full_access=False,
                sync_workers=True,
                seat=None,
            ),
            settings,
        ) == 0
    assert [call.args[2] for call in sync.call_args_list] == ["seat1", "seat6"]
