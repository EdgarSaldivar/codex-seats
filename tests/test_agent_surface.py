"""Agent-facing CLI: capacity model, JSON envelope, light management, guards."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from codex_seats import cli, pool_view, seats_cli
from codex_seats.agent_guide import AGENT_GUIDE
from codex_seats.seat_manager import ActivationChecks, SeatManager
from codex_seats.state import iso
from registry_fixture import registry_settings, seat_entry, write_settings

CONTEXT_VARS = (
    "CODEX_SEATS_MANAGED_WORKER",
    "CLAUDE_LEAD_WORKER",
    "CODEX_CONTROL_SEAT",
    "CODEX_SEATS_HEAD",
    "CODEX_THREAD_ID",
    "CODEX_TURN_ID",
    "CODEX_HOME",
    "CODEX_SESSION_ID",
)
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def agent_shell(monkeypatch):
    """A non-interactive head shell: the context agents run commands from."""
    for name in CONTEXT_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CODEX_SEATS_HEAD", "claude")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)


def _runtime(observed: float, pct: float, *, claim: dict | None = None) -> dict:
    return {
        "status": "ok",
        "pct_left": pct,
        "probed_at": iso(observed),
        "usage_updated_at": iso(observed),
        "usage_observed_at": observed,
        "last_probe_limits": {
            "5h": {"pct": pct, "window_minutes": 300, "resets_at": observed + 1_800},
            "weekly": {"pct": 90, "window_minutes": 10_080, "resets_at": observed + 86_400},
        },
        "active_window_observation": {
            "source": "account",
            "complete": True,
            "observed_at": observed,
            "observed_windows": ["5h", "weekly"],
            "active_windows": ["5h", "weekly"],
        },
        "in_flight": claim,
    }


def _pool(root: Path) -> dict:
    """seat1 fits both sizes, seat2 is busy, seat3 lacks auth, seat4 is inactive."""
    settings = registry_settings(3)
    settings["pool"]["seats"]["seat1"]["label"] = "Edgar Pro"
    settings["pool"]["seats"]["seat4"] = seat_entry("Spare", lifecycle="inactive")
    write_settings(root, settings)
    observed = time.time()
    for seat_id in ("seat1", "seat2", "seat3", "seat4"):
        home = root / seat_id
        home.mkdir(mode=0o700)
        if seat_id != "seat3":
            (home / "auth.json").write_text("present")
            (home / "auth.json").chmod(0o600)
    claim = {"job_id": "live", "dispatcher_pid": os.getpid(), "kind": "review", "label": "audit X"}
    (root / "seats.json").write_text(json.dumps({
        "seats": {
            "seat1": _runtime(observed, 80),
            "seat2": _runtime(observed, 70, claim=claim),
            "seat3": _runtime(observed, 60),
            "seat4": _runtime(observed, 95),
        },
        "jobs": [],
    }))
    (root / ".ledger.lock").write_bytes(b"")
    return settings


def _snapshot_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in sorted(root.iterdir())
        if path.is_file()
    }


def test_collect_is_one_read_only_model_of_every_provider(tmp_path):
    settings = _pool(tmp_path)
    before = _snapshot_bytes(tmp_path)

    view = pool_view.collect(tmp_path, settings)

    assert _snapshot_bytes(tmp_path) == before
    entries = {entry["seat"]: entry for entry in view["entries"]}
    assert list(entries) == ["seat1", "seat2", "seat3", "seat4", "claude"]
    one = entries["seat1"]
    assert one["provider"] == "codex" and one["label"] == "Edgar Pro"
    assert one["role"] == "primary" and one["lifecycle"] == "active"
    assert one["status"] == "ok" and one["fits"] == ["small", "big"]
    assert one["ready"] and not one["busy"] and one["fresh"]
    assert one["left"] == {"5h": 80.0, "7d": 90.0}
    assert [window["name"] for window in one["windows"]] == ["5h", "7d"]
    assert one["windows"][0]["minutes"] == 300
    busy = entries["seat2"]
    assert busy["busy"] and not busy["ready"]
    assert busy["task"] == {"label": "audit X", "kind": "review", "job_id": "live"}
    assert busy["fits"], "fit is a capacity verdict independent of busy"
    assert entries["seat3"]["status"] == "needs-login" and entries["seat3"]["fits"] == []
    assert entries["seat4"]["lifecycle"] == "inactive"
    assert entries["seat4"]["reason"] == "seat is inactive"
    claude = entries["claude"]
    assert claude["lifecycle"] == "off" and claude["fits"] == [] and claude["windows"] == []

    pool = view["pool"]
    assert (pool["ready"], pool["total"]) == (1, 3)
    assert pool["running"] == 1 and pool["queued"] == 0
    assert pool["next"]["small"] == {"provider": "codex", "seat": "seat1", "label": "Edgar Pro"}
    assert pool["next"]["big"]["seat"] == "seat1"
    codes = {(warning["seat"], warning["code"]) for warning in pool["warnings"]}
    assert ("seat3", "needs-login") in codes
    assert "seat1" in pool_view.render_text(view)


def test_next_seat_matches_pick_seat(tmp_path, capsys, monkeypatch):
    settings = _pool(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    view = pool_view.collect(tmp_path, settings)
    assert cli.main(["pick-seat", "--size", "small"]) == 0
    assert capsys.readouterr().out.strip() == view["pool"]["next"]["small"]["seat"]


def test_capacity_json_envelope_and_text(tmp_path, capsys, monkeypatch):
    _pool(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    assert cli.main(["capacity", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == 1 and payload["ok"] is True
    assert set(payload["data"]) == {"pool", "entries", "jobs"}
    entry = payload["data"]["entries"][0]
    assert {
        "provider", "seat", "label", "role", "lifecycle", "status", "busy", "task",
        "windows", "left", "fits", "ready", "banked_resets", "observed_at",
    } <= set(entry)

    assert cli.main(["capacity"]) == 0
    text = capsys.readouterr().out
    assert text.startswith("Ready 1/3")
    assert "ROLE" in text and "seat1 Edgar Pro (Plus 1x)" in text
    assert "○ idle" in text and "× needs sign-in" in text and "– paused" in text
    assert "\x1b" not in text  # captured output is never coloured


def test_capacity_reports_unreadable_pool_in_the_envelope(tmp_path, capsys, monkeypatch):
    write_settings(tmp_path, registry_settings(1))
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    assert cli.main(["capacity", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["error"]["code"] == "pool-unreadable"


def test_pick_seat_json_success_and_exhausted(tmp_path, capsys, monkeypatch):
    _pool(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    assert cli.main(["pick-seat", "--size", "big", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] and payload["data"]["seat"] == "seat1"
    assert payload["data"]["label"] == "Edgar Pro"

    with open(tmp_path / "seats.json") as handle:
        ledger = json.load(handle)
    ledger["seats"]["seat1"]["in_flight"] = {"job_id": "x", "dispatcher_pid": os.getpid()}
    (tmp_path / "seats.json").write_text(json.dumps(ledger))
    assert cli.main(["pick-seat", "--size", "small"]) == 3
    assert capsys.readouterr().out == "-\n"
    assert cli.main(["pick-seat", "--size", "small", "--json"]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["error"]["code"] == "exhausted"
    assert payload["data"]["seat"] is None


def test_head_pauses_and_resumes_a_seat_without_a_terminal(tmp_path, capsys, monkeypatch):
    _pool(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    path = tmp_path / "settings.json"
    before = path.read_bytes()

    assert cli.main(["seats", "pause", "seat3", "--dry-run", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["ok"] and plan["planned"] and plan["mode"] == "deactivate"
    assert plan["lifecycle"] == {"before": "active", "after": "inactive"}
    assert path.read_bytes() == before

    assert cli.main(["seats", "pause", "--seat", "3", "--json"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["ok"] and not applied["planned"] and applied["changed"]
    assert json.loads(path.read_text())["pool"]["seats"]["seat3"]["lifecycle"] == "inactive"

    def passing(self, preview, cancel_event):
        del cancel_event
        return ActivationChecks(
            seat_id=preview.seat_id, home=tmp_path / preview.seat_id, passed=True,
            verified_login=True, successful_probe=True, canceled=False,
            detail="test", expected_digest=preview.expected_digest,
        )

    monkeypatch.setattr(SeatManager, "run_activation_checks", passing)
    assert cli.main(["seats", "resume", "seat3"]) == 0
    assert "APPLIED resume seat3: inactive -> active" in capsys.readouterr().out
    assert json.loads(path.read_text())["pool"]["seats"]["seat3"]["lifecycle"] == "active"


def test_pausing_a_busy_seat_drains_it(tmp_path, capsys, monkeypatch):
    _pool(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    assert cli.main(["seats", "pause", "seat2", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["busy"] and report["mode"] == "drain"
    assert report["lifecycle"]["after"] == "draining"
    assert "seats pause seat2" in report["next_step"]


def test_resume_refuses_when_activation_checks_fail(tmp_path, capsys, monkeypatch):
    _pool(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)

    def failing(self, preview, cancel_event):
        del cancel_event
        return ActivationChecks(
            seat_id=preview.seat_id, home=None, passed=False, verified_login=False,
            successful_probe=False, canceled=False, detail="not signed in",
            expected_digest=preview.expected_digest,
        )

    monkeypatch.setattr(SeatManager, "run_activation_checks", failing)
    assert cli.main(["seats", "resume", "seat4", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and "not signed in" in payload["error"]["message"]


def test_refresh_uses_the_refresher_paths(tmp_path, capsys, monkeypatch):
    _pool(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    from codex_seats import watch

    calls: list[str] = []

    def selected(self, seat_id):
        calls.append(seat_id)
        self._record_selected(seat_id, f"{seat_id} account -> ok", success=True, advance=True)
        return "ok"

    def whole(self):
        calls.append("all")
        with self._state_lock:
            self.last_result, self.last_succeeded = "shared refresh already in progress", None

    monkeypatch.setattr(watch.WatchRefresher, "run_selected_attempt", selected)
    monkeypatch.setattr(watch.WatchRefresher, "run_attempt", whole)
    assert cli.main(["seats", "refresh", "1", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] and report["status"] == "ok" and report["seat_id"] == "seat1"
    assert cli.main(["seats", "refresh", "--all", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["error"]["code"] == "refresh-contended"
    assert calls == ["seat1", "all"]
    assert cli.main(["seats", "refresh"]) == 2
    assert "needs SEAT" in capsys.readouterr().err


@pytest.mark.parametrize("worker", ["CODEX_SEATS_MANAGED_WORKER", "CLAUDE_LEAD_WORKER"])
@pytest.mark.parametrize(
    "command",
    [
        ["seats", "pause", "seat1"],
        ["seats", "resume", "seat4"],
        ["seats", "refresh", "seat1"],
        ["seats", "set", "seat1", "--label", "Nope"],
    ],
)
def test_managed_workers_are_still_refused(tmp_path, capsys, monkeypatch, worker, command):
    _pool(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setenv(worker, "1")
    from codex_seats import watch

    monkeypatch.setattr(
        watch.WatchRefresher, "__init__",
        lambda *_a, **_k: pytest.fail("refresher built for a managed worker"),
    )
    before = _snapshot_bytes(tmp_path)
    assert cli.main([*command, "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and "managed workers" in payload["error"]["message"]
    assert _snapshot_bytes(tmp_path) == before


def test_human_only_commands_are_unchanged(tmp_path, capsys, monkeypatch):
    _pool(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.delenv("CODEX_SEATS_HEAD")
    before = _snapshot_bytes(tmp_path)
    for command in ("retire", "remove"):
        assert cli.main(["seats", command, "seat4", "--from-current-user-request"]) == 2
        assert "interactive trusted human terminal" in capsys.readouterr().err
    monkeypatch.setattr(
        cli, "prepare_banked_reset_redemption",
        lambda *_a: {"credit": {"fingerprint": "f"}, "available_count": 1,
                     "provider_identity": json.dumps({"email": "x"})},
    )
    monkeypatch.setattr(
        cli, "redeem_banked_reset", lambda *_a, **_k: pytest.fail("redeemed without a human")
    )
    assert cli.main(["redeem-reset", "--seat", "seat1"]) == 3
    assert "human terminal confirmation" in capsys.readouterr().err
    assert cli.main(["redeem-reset", "seat1", "--seat", "seat2"]) == 2
    assert _snapshot_bytes(tmp_path) == before


def test_policy_apply_works_without_a_terminal(tmp_path, capsys, monkeypatch):
    settings = _pool(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    assert cli.main(["config", "policy", "set", "--set", "review.mode", "advisory", "--apply"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] and report["applied"]
    assert cli.cmd_config(
        cli.build_parser().parse_args(["config", "policy", "show"]), settings
    ) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["ok"] and shown["data"]["review.mode"] == "off"
    assert cli.main(["config", "policy", "show"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["review.mode"] == "advisory"


def test_provider_claude_set_preview_apply_and_worker_refusal(tmp_path, capsys, monkeypatch):
    _pool(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    path = tmp_path / "settings.json"
    before = path.read_bytes()
    assert cli.main(["provider", "claude", "set", "--mode", "opportunistic", "--dry-run", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["ok"] and plan["data"]["planned"] and plan["data"]["changed"]
    assert path.read_bytes() == before

    assert cli.main([
        "provider", "claude", "set", "--mode", "opportunistic", "--tier", "max 20x",
        "--small-headroom", "12", "--big-headroom", "25", "--active-usage-refresh", "off",
        "--json",
    ]) == 0
    applied = json.loads(capsys.readouterr().out)["data"]
    assert applied["after"]["mode"] == "opportunistic"
    assert applied["after"]["subscription_tier"] == "Max 20x"
    claude = json.loads(path.read_text())["providers"]["claude"]
    assert (claude["small_task_min_headroom_pct"], claude["big_task_min_headroom_pct"]) == (12, 25)
    assert claude["active_usage_refresh"] is False

    assert cli.main(["provider", "claude", "set", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "refused"
    monkeypatch.setenv("CODEX_SEATS_MANAGED_WORKER", "1")
    current = path.read_bytes()
    assert cli.main(["provider", "claude", "set", "--mode", "off", "--json"]) == 2
    assert "managed workers" in json.loads(capsys.readouterr().out)["error"]["message"]
    assert path.read_bytes() == current


def test_read_commands_share_the_envelope(tmp_path, capsys, monkeypatch):
    _pool(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    assert cli.main(["provider", "status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["ok"] and status["data"]["mode"] == "off"
    assert cli.main(["resets", "--json"]) == 0
    resets = json.loads(capsys.readouterr().out)
    assert resets["schema"] == 1 and resets["ok"] and isinstance(resets["data"], dict)
    assert cli.main(["seats", "list", "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["ok"] and listing["schema"] == 1 and listing["seats"]
    assert cli.main(["seats", "show", "--seat", "seat1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["seat"]["id"] == "seat1"
    assert cli.main(["seats", "show", "1", "--seat", "2", "--json"]) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "refused" and "disagree" in error["message"]


def test_seat_alias_is_accepted_where_a_seat_is_chosen():
    parser = cli.build_parser()
    assert parser.parse_args(["review", "--seat", "2", "--diff-file", "x"]).codex_seat
    assert parser.parse_args(["redeem-reset", "--seat", "1"]).seat_option == "1"
    for command in ("show", "pause", "resume", "refresh", "drain", "set-role"):
        tail = ["worker"] if command == "set-role" else []
        args = parser.parse_args(["seats", command, "--seat", "seat1", *tail])
        seats_cli.normalize_plan_args(args)
        assert args.selector == "seat1"


def test_agent_guide_is_printed_and_documented(capsys):
    assert cli.main(["help", "agents"]) == 0
    assert capsys.readouterr().out == AGENT_GUIDE
    guide = (REPO / "docs" / "agents.md").read_text()
    for command in (
        "capacity --json", "pick-seat --size", "seats refresh --all", "delegate --detach",
        "await", "seats pause", "seats resume", "provider claude set",
        "config policy set", "--override-reason",
    ):
        assert command in AGENT_GUIDE and command in guide
    assert "docs/agents.md" in (REPO / "AGENTS.md").read_text()


def test_dash_manage_opens_for_a_human_in_a_head_pane(monkeypatch):
    import argparse

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "codex_version", lambda: (None, None))
    opened: list[bool] = []
    monkeypatch.setattr(
        cli, "_run_interactive_dash",
        lambda *_args, direct, **_kwargs: opened.append(direct) or 0,
    )
    args = argparse.Namespace(
        no_color=True, watch=False, manage=True, interval=5, probe_interval=300,
        no_active_probe=True, forensic=False,
    )
    assert cli.cmd_dash(args, registry_settings(1), []) == 0
    assert opened == [True]
    monkeypatch.setenv("CODEX_SEATS_MANAGED_WORKER", "1")
    assert cli.cmd_dash(args, registry_settings(1), []) == 2
    assert opened == [True]


# -- two roles, primary drains last -----------------------------------------
def test_pick_seat_spends_workers_before_the_primary():
    from codex_seats.seat_selection import pick_seat

    settings = registry_settings(2)
    observed = time.time()
    # The primary scores better (more headroom, nearer reset) but a fitting
    # worker still goes first.
    seats = {"seat1": _runtime(observed, 95), "seat2": _runtime(observed, 40)}
    assert pick_seat(seats, settings, "small") == "seat2"
    assert pick_seat(seats, settings, "big") == "seat2"
    seats["seat2"]["in_flight"] = {"job_id": "x", "dispatcher_pid": os.getpid()}
    assert pick_seat(seats, settings, "small") == "seat1"
    seats["seat2"] = _runtime(observed, 1)
    assert pick_seat(seats, settings, "small") == "seat1"


def test_capacity_marks_the_primary_used_last(tmp_path):
    settings = _pool(tmp_path)
    entries = {entry["seat"]: entry for entry in pool_view.collect(tmp_path, settings)["entries"]}
    assert entries["seat1"]["used_last"] is True
    assert entries["seat2"]["used_last"] is False and entries["seat2"]["role"] == "worker"
    assert {entry["role"] for entry in entries.values()} <= {"primary", "worker", None}


def test_legacy_deputy_loads_silently_and_is_dropped_on_write(tmp_path):
    from codex_seats.seat_admin import set_seat
    from codex_seats.seat_registry import derive_roles
    from codex_seats.state import load_settings

    settings = registry_settings(3)
    settings["pool"]["seats"]["seat3"]["lifecycle"] = "retired"
    settings["pool"]["deputy"] = "seat3"  # legacy, pointing at a retired seat
    write_settings(tmp_path, settings)
    loaded, warnings = load_settings(tmp_path)
    assert warnings == []
    assert "deputy" not in loaded["pool"]
    assert "deputy" not in derive_roles(loaded).values()
    assert derive_roles(settings) == {"seat1": "primary", "seat2": "worker"}

    set_seat(tmp_path, "seat2", label="Renamed")
    written = json.loads((tmp_path / "settings.json").read_text())
    assert "deputy" not in written["pool"]
    assert written["pool"]["seats"]["seat2"]["label"] == "Renamed"


def test_set_role_deputy_is_rejected_and_only_the_primary_is_guarded(
    tmp_path, capsys, monkeypatch
):
    _pool(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    before = (tmp_path / "settings.json").read_bytes()
    assert cli.main(["seats", "set-role", "seat2", "deputy"]) == 2
    assert "deputy role was removed; seats are primary or worker" in capsys.readouterr().err
    assert (tmp_path / "settings.json").read_bytes() == before
    assert cli.main(["seats", "pause", "seat1", "--json"]) == 2
    assert "make another seat primary first" in json.loads(capsys.readouterr().out)["error"]["message"]
    assert cli.main(["seats", "set-role", "seat3", "primary"]) == 0
    capsys.readouterr()
    assert cli.main(["seats", "pause", "seat1", "--json"]) == 0


def test_legacy_pool_keys_are_silent_and_preserved_on_write(tmp_path):
    from codex_seats.seat_admin import set_seat
    from codex_seats.state import load_settings

    settings = registry_settings(2)
    settings["pool"]["primary_wait_for_eligible_nonprimary"] = True
    settings["pool"]["banked_reset_standard_expiry_days"] = 30
    settings["ui"] = {"theme": "dark"}
    write_settings(tmp_path, settings)
    _loaded, warnings = load_settings(tmp_path)
    assert warnings == []
    set_seat(tmp_path, "seat2", label="Renamed")
    written = json.loads((tmp_path / "settings.json").read_text())
    assert written["pool"]["primary_wait_for_eligible_nonprimary"] is True
    assert written["pool"]["banked_reset_standard_expiry_days"] == 30
    assert written["ui"] == {"theme": "dark"}


def test_capacity_and_dashboard_header_share_one_ready_rule(tmp_path):
    from codex_seats.display import dashboard

    settings = _pool(tmp_path)
    strip = pool_view.summary(pool_view.collect(tmp_path, settings))
    header = dashboard(tmp_path, settings, [], color=False, width=140)
    assert f"Ready {strip['ready']}/{strip['total']}" in header
    assert f"Running {strip['running']}" in header
    assert set(strip) == set(pool_view.SUMMARY_KEYS)
