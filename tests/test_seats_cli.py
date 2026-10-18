from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_seats import cli, seats_cli
from codex_seats.seat_admin import (
    activate_seat,
    add_seat,
    deactivate_seat,
    retire_seat,
)
from codex_seats.state import default_settings


def _write_settings(root: Path, settings: dict | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "settings.json"
    path.write_text(json.dumps(settings or default_settings(), indent=2) + "\n")
    path.chmod(0o600)


def _clear_context(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CODEX_SEATS_MANAGED_WORKER",
        "CODEX_CONTROL_SEAT",
        "CODEX_SEATS_HEAD",
        "CODEX_THREAD_ID",
        "CODEX_TURN_ID",
        "CODEX_HOME",
    ):
        monkeypatch.delenv(name, raising=False)


def _result(code: int = 0, stdout: bytes = b"") -> SimpleNamespace:
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=b"")


def test_parser_registers_complete_seats_family() -> None:
    parser = cli.build_parser()
    commands = {
        "list": ["--json"],
        "show": ["seat1", "--json"],
        "doctor": ["--all", "--json"],
        "add": ["--label", "One", "--dry-run"],
        "set": ["1", "--label", "One", "--dry-run"],
        "set-role": ["seat1", "worker", "--dry-run"],
        "activate": ["seat1", "--as-primary", "--dry-run"],
        "drain": ["seat1", "--wait", "--allow-empty", "--dry-run"],
        "deactivate": ["seat1", "--allow-empty", "--dry-run"],
        "retire": ["seat1", "--from-current-user-request", "--dry-run"],
        "remove": ["seat1", "--from-current-user-request", "--dry-run"],
        "login": ["seat1", "--from-current-user-request", "--dry-run"],
    }
    for command, tail in commands.items():
        parsed = parser.parse_args(["seats", command, *tail])
        assert parsed.handler == "seats"
    for command in commands.keys() - {"list", "show", "doctor"}:
        parsed = parser.parse_args(["seats", "plan", command, *commands[command]])
        assert parsed.plan_action == command
    before_action = parser.parse_args(
        ["seats", "plan", "--json", "add", "--label", "One"]
    )
    assert before_action.json is True
    policy = parser.parse_args(
        [
            "seats",
            "plan",
            "set",
            "seat1",
            "--reserve-pct",
            "4",
            "--small-task-min-headroom-pct",
            "6",
            "--big-task-min-headroom-pct",
            "18",
        ]
    )
    assert policy.reserve_pct == 4
    assert policy.small_task_min_headroom_pct == 6
    assert policy.big_task_min_headroom_pct == 18


def test_list_show_zero_one_two_retired_by_stable_id_or_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_context(monkeypatch)
    _write_settings(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    assert cli.main(["seats", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["seats"] == []

    one = add_seat(tmp_path, label="Primary").seat_id
    two = add_seat(tmp_path, label="Worker").seat_id
    for seat_id in (one, two):
        (tmp_path / seat_id).mkdir(mode=0o700)
    activate_seat(
        tmp_path,
        one,
        verified_login=True,
        successful_probe=True,
        as_primary=True,
    )
    activate_seat(tmp_path, two, verified_login=True, successful_probe=True)
    deactivate_seat(tmp_path, two)
    retire_seat(tmp_path, two)

    assert cli.main(["seats", "show", "seat1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["seat"]["id"] == "seat1"
    assert cli.main(["seats", "show", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["seat"]["id"] == "seat1"
    assert cli.main(["seats", "list", "--json"]) == 0
    assert [seat["id"] for seat in json.loads(capsys.readouterr().out)["seats"]] == [
        "seat1"
    ]
    assert cli.main(["seats", "list", "--all", "--json"]) == 0
    assert [seat["id"] for seat in json.loads(capsys.readouterr().out)["seats"]] == [
        "seat1",
        "seat2",
    ]


def test_plan_and_dry_run_share_exact_registry_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_context(monkeypatch)
    _write_settings(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    before = (tmp_path / "settings.json").read_bytes()

    assert cli.main(["seats", "plan", "add", "--label", "One", "--json"]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert cli.main(["seats", "add", "--label", "One", "--dry-run", "--json"]) == 0
    dry_run = json.loads(capsys.readouterr().out)

    assert planned["registry"] == dry_run["registry"]
    assert planned["registry"]["diff"].startswith("--- settings.before")
    assert planned["blockers"] == []
    assert (tmp_path / "settings.json").read_bytes() == before


def test_capacity_profile_preview_normalizes_reserve_and_floors_without_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_context(monkeypatch)
    _write_settings(tmp_path)
    add_seat(tmp_path, label="One")
    settings = json.loads((tmp_path / "settings.json").read_text())
    settings["pool"]["reserves_pct"] = {"seat1": 20}
    settings["pool"]["task_min_headroom_pct_by_seat"] = {
        "seat1": {"small": 12, "big": 34}
    }
    _write_settings(tmp_path, settings)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(
        seats_cli,
        "_codex_login_support",
        lambda **_: pytest.fail("preview must not invoke provider support checks"),
    )
    before = {
        path.name: path.read_bytes()
        for path in tmp_path.iterdir()
        if path.is_file()
    }

    assert (
        cli.main(
            [
                "seats",
                "plan",
                "set",
                "seat1",
                "--profile",
                "business-premium",
                "--relative-capacity",
                "5",
                "--capacity-source",
                "openai-help-center",
                "--reserve-pct",
                "4",
                "--small-task-min-headroom-pct",
                "6",
                "--big-task-min-headroom-pct",
                "18",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    impact = report["capacity_policy_impact"]
    assert impact["changed"] is True
    assert impact["before"]["reserve"] == {
        "native_percent": 20.0,
        "baseline_units": 20.0,
    }
    assert impact["after"]["reserve"] == {
        "native_percent": 4.0,
        "baseline_units": 20.0,
    }
    assert impact["after"]["task_floors"]["small"]["baseline_units"] == 30.0
    assert impact["after"]["task_floors"]["big"]["baseline_units"] == 90.0
    assert '"reserves_pct"' in report["registry"]["diff"]
    assert '"task_min_headroom_pct_by_seat"' in report["registry"]["diff"]
    assert report["provider_checks"] == {
        "performed": False,
        "deferred": ["post-apply-authoritative-capacity-probe"],
    }
    assert any("after apply" in warning for warning in report["warnings"])

    assert (
        cli.main(
            [
                "seats",
                "plan",
                "set",
                "seat1",
                "--profile",
                "business-premium",
                "--relative-capacity",
                "5",
                "--capacity-source",
                "openai-help-center",
                "--reserve-pct",
                "4",
                "--small-task-min-headroom-pct",
                "6",
                "--big-task-min-headroom-pct",
                "18",
            ]
        )
        == 0
    )
    human = capsys.readouterr().out
    assert "CAPACITY POLICY: unverified 1x -> business-premium 5x" in human
    assert "before: reserve 20%=20 baseline units" in human
    assert "after: reserve 4%=20 baseline units" in human
    assert "floors small 6%=30, big 18%=90" in human
    assert (
        "PROVIDER CHECKS: not run during planning; required: "
        "post-apply-authoritative-capacity-probe"
    ) in human
    assert {
        path.name: path.read_bytes()
        for path in tmp_path.iterdir()
        if path.is_file()
    } == before


def test_blocked_plan_is_structured_and_does_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_context(monkeypatch)
    _write_settings(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    before = (tmp_path / "settings.json").read_bytes()
    assert cli.main(["seats", "plan", "set", "seat1", "--label", "X", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["command"] == "plan"
    assert report["blockers"]
    assert report["registry"]["before"] == report["registry"]["after"]
    assert (tmp_path / "settings.json").read_bytes() == before


def test_mutations_apply_all_lifecycle_and_field_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_context(monkeypatch)
    _write_settings(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(seats_cli, "_codex_login_support", lambda **_: ("/codex", "codex-cli 0.153.1"))
    monkeypatch.setattr(seats_cli, "_login_status", lambda *_: True)
    monkeypatch.setattr(seats_cli, "_fresh_activation_probe", lambda *_: True)

    assert cli.main(["seats", "add", "--label", "One", "--json"]) == 0
    capsys.readouterr()
    (tmp_path / "seat1").mkdir(mode=0o700)
    assert (
        cli.main(
            [
                "seats",
                "set",
                "1",
                "--label",
                "Changed",
                "--profile",
                "business-standard",
                "--relative-capacity",
                "1",
                "--capacity-source",
                "openai-help-center",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert cli.main(["seats", "activate", "seat1", "--as-primary", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["seats", "set-role", "seat1", "primary", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["seats", "drain", "seat1", "--allow-empty", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["seats", "deactivate", "seat1", "--json"]) == 0
    capsys.readouterr()

    monkeypatch.setattr(seats_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(seats_cli.sys.stdout, "isatty", lambda: True)
    assert (
        cli.main(
            [
                "seats",
                "retire",
                "seat1",
                "--from-current-user-request",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["registry"]["after"]["seats"]["seat1"]["lifecycle"] == "retired"
    assert "do not alter OpenAI workspace membership" in report["notice"]

    assert cli.main(["seats", "add", "--label", "Two", "--json"]) == 0
    capsys.readouterr()
    assert (
        cli.main(
            [
                "seats",
                "remove",
                "seat2",
                "--from-current-user-request",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["action"] == "remove"
    assert report["canonical_action"] == "retire"
    assert report["registry"]["after"]["seats"]["seat2"]["lifecycle"] == "retired"


@pytest.mark.parametrize(
    "environment",
    [{"CODEX_SEATS_MANAGED_WORKER": "1"}, {"CLAUDE_LEAD_WORKER": "1"}],
)
def test_every_write_refuses_managed_workers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    environment: dict[str, str],
) -> None:
    _clear_context(monkeypatch)
    _write_settings(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    before = (tmp_path / "settings.json").read_bytes()
    assert cli.main(["seats", "add", "--label", "Nope"]) == 2
    assert "managed workers" in capsys.readouterr().err
    assert (tmp_path / "settings.json").read_bytes() == before


@pytest.mark.parametrize(
    "environment",
    [
        {"CODEX_SEATS_HEAD": "claude"},
        {"CODEX_SEATS_HEAD": "codex"},
        {"CODEX_CONTROL_SEAT": "seat1"},
        {"CODEX_THREAD_ID": "thread", "CODEX_HOME": "/controlled"},
    ],
)
def test_heads_and_controllers_may_make_light_registry_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    environment: dict[str, str],
) -> None:
    _clear_context(monkeypatch)
    _write_settings(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    assert cli.main(["seats", "add", "--label", "Agent added", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True and report["seat_id"] == "seat1"
    # Retirement stays with a human outside head/controller contexts.
    assert cli.main(["seats", "retire", "seat1", "--from-current-user-request"]) == 2
    assert "controller/head" in capsys.readouterr().err


@pytest.mark.parametrize(
    "tail",
    [
        ["add", "--label", "Nope"],
        ["set", "seat1", "--label", "Nope"],
        ["set-role", "seat1", "worker"],
        ["activate", "seat1", "--as-primary"],
        ["drain", "seat1"],
        ["deactivate", "seat1"],
        ["retire", "seat1", "--from-current-user-request"],
        ["remove", "seat1", "--from-current-user-request"],
        ["login", "seat1", "--from-current-user-request"],
    ],
)
def test_managed_worker_refusal_precedes_every_mutation_and_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tail: list[str],
) -> None:
    _clear_context(monkeypatch)
    monkeypatch.setenv("CODEX_SEATS_MANAGED_WORKER", "1")
    _write_settings(tmp_path)
    add_seat(tmp_path, label="One")
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(
        seats_cli,
        "_codex_login_support",
        lambda **_: pytest.fail("provider command ran before authorization"),
    )
    before = (tmp_path / "settings.json").read_bytes()
    assert cli.main(["seats", *tail]) == 2
    assert "managed workers" in capsys.readouterr().err
    assert (tmp_path / "settings.json").read_bytes() == before


def test_role_aliases_are_refused_for_reads_and_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_context(monkeypatch)
    settings = default_settings()
    settings["pool"]["seats"] = {
        "seat1": {
            "label": "One",
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
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    assert cli.main(["seats", "show", "P", "--json"]) == 2
    assert "unknown seat selector" in capsys.readouterr().out
    assert cli.main(["seats", "set", "P", "--label", "Nope"]) == 2
    assert "stable seatN" in capsys.readouterr().err


@pytest.mark.parametrize("version", ["0.153.1", "0.154.0", "0.154.0-alpha.1"])
def test_login_uses_exact_inherited_stdio_argv_and_resolved_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    version: str,
) -> None:
    _clear_context(monkeypatch)
    _write_settings(tmp_path)
    add_seat(tmp_path, label="One")
    (tmp_path / "seat1").mkdir(mode=0o700)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(seats_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(seats_cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(seats_cli, "resolve_executable", lambda _: "/bin/codex")
    calls: list[tuple[list[str], dict]] = []

    def run(argv: list[str], **kwargs):
        calls.append((argv, kwargs))
        if argv[-1] == "--version":
            return _result(stdout=f"codex-cli {version}\n".encode())
        return _result()

    monkeypatch.setattr(seats_cli.bounded_process, "run", run)
    assert (
        cli.main(
            [
                "seats",
                "login",
                "1",
                "--from-current-user-request",
            ]
        )
        == 0
    )
    assert [call[0] for call in calls] == [
        ["/bin/codex", "--version"],
        ["/bin/codex", "login", "--help"],
        ["/bin/codex", "login"],
    ]
    login_kwargs = calls[-1][1]
    assert login_kwargs["capture_output"] is False
    assert login_kwargs["env"]["CODEX_HOME"] == str(tmp_path / "seat1")
    assert "input_bytes" not in login_kwargs
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("version_code", "help_code", "error"),
    [(1, 0, "version check failed"), (0, 1, "does not support `codex login`")],
)
def test_login_preflight_failure_does_not_launch_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    version_code: int, help_code: int, error: str,
) -> None:
    _write_settings(tmp_path)
    add_seat(tmp_path, label="One")
    (tmp_path / "seat1").mkdir(mode=0o700)
    settings = json.loads((tmp_path / "settings.json").read_text())
    monkeypatch.setattr(seats_cli, "resolve_executable", lambda _: "/bin/codex")

    def run(argv, **kwargs):
        if argv == ["/bin/codex", "--version"]:
            return _result(version_code, b"codex-cli 0.154.0\n")
        if argv == ["/bin/codex", "login", "--help"]:
            return _result(help_code)
        pytest.fail("interactive login launched after failed preflight")

    monkeypatch.setattr(seats_cli.bounded_process, "run", run)
    with pytest.raises(seats_cli.SeatsCLIError, match=error):
        seats_cli._run_login(tmp_path, settings, "seat1")


def test_login_and_retire_require_attestation_and_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_context(monkeypatch)
    _write_settings(tmp_path)
    add_seat(tmp_path, label="One")
    (tmp_path / "seat1").mkdir(mode=0o700)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(seats_cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(seats_cli.sys.stdout, "isatty", lambda: False)
    assert cli.main(["seats", "login", "seat1"]) == 2
    assert "from-current-user-request" in capsys.readouterr().err
    assert (
        cli.main(
            ["seats", "remove", "seat1", "--from-current-user-request"]
        )
        == 2
    )
    assert "trusted human terminal" in capsys.readouterr().err


def test_activate_requires_supported_cli_login_status_and_fresh_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_context(monkeypatch)
    _write_settings(tmp_path)
    add_seat(tmp_path, label="One")
    (tmp_path / "seat1").mkdir(mode=0o700)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    evidence: list[str] = []
    monkeypatch.setattr(
        seats_cli,
        "_codex_login_support",
        lambda **kwargs: evidence.append("version+support") or ("/codex", "0.153.1"),
    )
    monkeypatch.setattr(
        seats_cli,
        "_login_status",
        lambda *_: evidence.append("login-status") or True,
    )
    monkeypatch.setattr(
        seats_cli,
        "_fresh_activation_probe",
        lambda *_: evidence.append("fresh-probe") or True,
    )
    assert cli.main(["seats", "activate", "seat1", "--as-primary", "--json"]) == 0
    assert evidence == ["version+support", "login-status", "fresh-probe"]


def test_activate_refuses_unsafe_home_before_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_context(monkeypatch)
    _write_settings(tmp_path)
    add_seat(tmp_path, label="One")
    (tmp_path / "seat1").mkdir(mode=0o777)
    (tmp_path / "seat1").chmod(0o777)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(
        seats_cli,
        "_codex_login_support",
        lambda **_: ("/codex", "0.153.1"),
    )
    monkeypatch.setattr(
        seats_cli,
        "_login_status",
        lambda *_: pytest.fail("login status ran against an unsafe home"),
    )

    assert cli.main(["seats", "activate", "seat1", "--as-primary"]) == 2
    assert "owner-controlled" in capsys.readouterr().err


def test_activate_binds_evidence_to_the_registered_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_context(monkeypatch)
    settings = default_settings()
    first_home = tmp_path / "first-home"
    second_home = tmp_path / "second-home"
    first_home.mkdir(mode=0o700)
    second_home.mkdir(mode=0o700)
    _write_settings(tmp_path, settings)
    add_seat(tmp_path, label="One", home=str(first_home))
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(
        seats_cli,
        "_codex_login_support",
        lambda **_: ("/codex", "0.153.1"),
    )
    monkeypatch.setattr(seats_cli, "_login_status", lambda *_: True)

    def probe_then_move_home(root, _settings, _seat_id):
        document = json.loads((root / "settings.json").read_text())
        document["pool"]["seats"]["seat1"]["home"] = str(second_home)
        (root / "settings.json").write_text(json.dumps(document, indent=2) + "\n")
        (root / "settings.json").chmod(0o600)
        return True

    monkeypatch.setattr(seats_cli, "_fresh_activation_probe", probe_then_move_home)

    assert cli.main(["seats", "activate", "seat1", "--as-primary"]) == 2
    assert "evidence no longer matches" in capsys.readouterr().err
    document = json.loads((tmp_path / "settings.json").read_text())
    assert document["pool"]["seats"]["seat1"]["lifecycle"] == "inactive"


def test_fresh_activation_probe_requires_new_authoritative_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_settings(tmp_path)
    add_seat(tmp_path, label="One")
    monkeypatch.setattr(seats_cli.time, "time", lambda: 100.0)

    def refresh(root, _settings, targets):
        assert targets == ["seat1"]
        path = root / "seats.json"
        ledger = json.loads(path.read_text())
        ledger["seats"]["seat1"].update(
            {
                "last_probe_limits": {"weekly": {"pct": 75}},
                "active_window_observation": {
                    "source": "account",
                    "observed_at": 100.0,
                },
                "account_window_observation": {
                    "last_success_observed_at": 100.0,
                },
            }
        )
        path.write_text(json.dumps(ledger))
        return ["seat1 account -> refreshed"]

    monkeypatch.setattr(seats_cli, "refresh_account_usage", refresh)
    assert seats_cli._fresh_activation_probe(tmp_path, default_settings(), "seat1")

    monkeypatch.setattr(
        seats_cli,
        "refresh_account_usage",
        lambda *_: ["seat1 account -> unavailable"],
    )
    assert not seats_cli._fresh_activation_probe(
        tmp_path, default_settings(), "seat1"
    )


def test_login_status_uses_exit_code_and_discards_identity_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _result(stdout=b"private-member@example.test\n")

    monkeypatch.setattr(seats_cli.bounded_process, "run", run)
    assert seats_cli._login_status("/codex", tmp_path)
    assert calls[0][0] == ["/codex", "login", "status"]
    assert calls[0][1]["env"]["CODEX_HOME"] == str(tmp_path)
    assert "private-member" not in capsys.readouterr().out


def test_doctor_covers_orphan_retained_auth_stale_claim_drift_and_no_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_context(monkeypatch)
    _write_settings(tmp_path)
    seat_id = add_seat(tmp_path, label="One").seat_id
    home = tmp_path / seat_id
    home.mkdir(mode=0o700)
    (home / "auth.json").write_text("email@example.test member-123 refresh-secret")
    (tmp_path / "seat9").mkdir()
    ledger_path = tmp_path / "seats.json"
    ledger = json.loads(ledger_path.read_text())
    ledger["seats"][seat_id].update(
        {
            "email": "email@example.test",
            "auth": "refresh-secret",
            "in_flight": {"job_id": "old", "dispatcher_pid": 0},
            "observed_plan": "business-premium",
        }
    )
    ledger_path.write_text(json.dumps(ledger))
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(
        seats_cli,
        "_codex_login_support",
        lambda **_: ("/codex", "codex-cli 0.153.1"),
    )
    monkeypatch.setattr(seats_cli, "_login_status", lambda *_: True)
    assert cli.main(["seats", "doctor", "--all", "--json"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    codes = {issue["code"] for issue in report["issues"]}
    assert {"orphan-seat-home", "retained-auth", "stale-claim"} <= codes
    assert "configured-observed-tier-drift" in codes
    assert report["orphans"] == ["seat9"]
    assert len(report["workspace_owner_follow_ups"]) == 2
    assert "email@example.test" not in output
    assert "member-123" not in output
    assert "refresh-secret" not in output


def test_older_settings_schema_refuses_read_and_write_management(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_context(monkeypatch)
    _write_settings(tmp_path, {"schema": 2, "pool": {"primary": "seat1"}})
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    assert cli.main(["seats", "list"]) == 2
    assert "schema 3" in capsys.readouterr().err
    assert cli.main(["seats", "add", "--label", "Nope"]) == 2
    assert "schema 3" in capsys.readouterr().err
