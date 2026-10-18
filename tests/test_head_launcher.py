"""Tests for the head launcher's command/env construction."""

from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path
import socket
import tempfile

import pytest
from codex_seats import head_launcher as hl
from codex_seats.bounded_process import ProcessResult
from codex_seats.cli import build_parser
from registry_fixture import registry_settings

SETTINGS = registry_settings(primary_home="/Users/example/.codex")


def test_head_policy_renders_modes_and_inline_threshold():
    off = hl.render_head_policy({})
    assert "above 5 minutes" in off
    assert "oppose" not in off.lower()
    assert "runs an automatic" not in off.lower()
    assert "governs automatic goal supervision only" in off
    assert "explicitly requested (default strategy: single)" in off
    always = hl.render_head_policy({
        "execution": {"delegation_policy": "always", "inline_max_minutes": 12},
        "review": {"mode": "blocking", "strategy": "cross"},
    }, codex=True)
    assert "Delegate every implementation unit" in always
    assert "runs an automatic cross review that is blocking" in always
    assert "`delegate`, `await`, and ChangeSet apply never review" in always
    manual = hl.render_head_policy({"execution": {"delegation_policy": "manual"}})
    assert "when the user requests it" in manual


@pytest.mark.parametrize("codex", [False, True])
@pytest.mark.parametrize("delegation", ["auto", "always", "manual"])
def test_review_off_does_not_require_another_family(codex, delegation):
    policy = hl.render_head_policy({
        "execution": {"delegation_policy": delegation},
        "review": {"mode": "off", "strategy": "cross"},
    }, codex=codex).lower()
    assert "review family must" not in policy
    assert "oppose" not in policy
    assert "automatic cross" not in policy
    assert "explicit goal review command is a blocking gate" in policy


SYNTHETIC_HEAD_PROFILE = """
default_permissions = ":danger-full-access"

[permissions.head]
description = "Synthetic contained head"

[permissions.head.filesystem]
":root" = "read"
"/synthetic/project" = "write"
"""


def _head_home(home, config=SYNTHETIC_HEAD_PROFILE):
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    if config is not None:
        (home / "config.toml").write_text(config)
    return home


def _home_settings(home, **extra):
    return {**registry_settings(primary_home=home), **extra}


def test_configured_safe_head_and_explicit_safe_override(tmp_path):
    home = _head_home(tmp_path / "primary")
    settings = _home_settings(home, execution={"head_permissions": "safe"})
    argv, env = hl.codex_head_command(tmp_path, settings, binary="/bin/codex")
    assert 'default_permissions="head"' in argv
    assert env["CODEX_SEATS_HEAD_MODE"] == "safe"
    full = _home_settings(home, execution={"head_permissions": "full-access"})
    argv, env = hl.codex_head_command(tmp_path, full, binary="/bin/codex", safe=True)
    assert 'default_permissions="head"' in argv
    assert env["CODEX_SEATS_HEAD_MODE"] == "safe"


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        (None, "does not exist"),
        ("default_permissions = [", "invalid TOML"),
        ('default_permissions = ":danger-full-access"\n', "no \\[permissions.head\\] profile"),
        ("[permissions]\nhead = \"contained\"\n", "no \\[permissions.head\\] profile"),
        ("[permissions.head]\ndescription = \"empty\"\n", "no filesystem rules"),
        ("[permissions.head.filesystem]\n", "no filesystem rules"),
    ],
)
@pytest.mark.parametrize("explicit", [False, True])
def test_safe_head_requires_usable_head_profile(tmp_path, config, reason, explicit):
    home = _head_home(tmp_path / "primary", config)
    (home / "auth.json").write_text("synthetic-not-read")
    (home / "auth.json").chmod(0)
    settings = _home_settings(
        home, execution={"head_permissions": "full-access" if explicit else "safe"}
    )
    with pytest.raises(ValueError, match=reason) as raised:
        hl.codex_head_command(tmp_path, settings, safe=explicit, binary="/bin/codex")
    assert "full-access" in str(raised.value)
    assert "synthetic-not-read" not in str(raised.value)


def test_full_access_head_does_not_require_head_profile(tmp_path):
    home = _head_home(tmp_path / "primary", None)
    argv, env = hl.codex_head_command(tmp_path, _home_settings(home), binary="/bin/codex")
    assert 'default_permissions=":danger-full-access"' in argv
    assert env["CODEX_SEATS_HEAD_MODE"] == "full-access"


def test_explicit_seat_launches_without_a_configured_primary(tmp_path, monkeypatch):
    settings = registry_settings(["seat3"], primary=None)
    events = []
    monkeypatch.setattr(hl.workers, "resolve_executable", lambda *_args: "/bin/codex")
    monkeypatch.setattr(hl, "launch", lambda argv, env: events.append((argv, env)))
    args = SimpleNamespace(head_kind="codex", seat=3, cd=None, safe=False)

    assert hl.cmd_head(args, settings, tmp_path) == 0
    [(argv, env)] = events
    assert env["CODEX_CONTROL_SEAT"] == "seat3"
    assert "--remote" not in argv


def test_cmd_head_reports_missing_safe_profile_without_launch(tmp_path, monkeypatch, capsys):
    home = _head_home(tmp_path / "primary", None)
    monkeypatch.setattr(hl.workers, "resolve_executable", lambda *_args: "/bin/codex")
    monkeypatch.setattr(hl, "launch", lambda *_args: pytest.fail("must not launch"))
    args = SimpleNamespace(head_kind="codex", seat=None, cd=None, safe=True)
    assert hl.cmd_head(args, _home_settings(home), tmp_path) == 2
    assert "cannot launch a safe Codex head" in capsys.readouterr().err


def _process_result(payload, *, returncode=0, stderr=b""):
    stdout = json.dumps(payload).encode() if payload is not None else b""
    return ProcessResult(("codex",), returncode, stdout, stderr, 123)


@pytest.fixture
def shared_head_home():
    # Keep Unix socket paths below macOS's length limit.
    with tempfile.TemporaryDirectory(prefix="cxh-", dir="/tmp") as directory:
        home = Path(directory)
        control = home / "app-server-control"
        control.mkdir()
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(control / "app-server-control.sock"))
            yield home


@pytest.mark.parametrize("safe", [False, True])
def test_primary_head_attaches_to_remote_server_with_permissions_and_cwd(
    shared_head_home, monkeypatch, safe
):
    settings = registry_settings(primary_home=str(shared_head_home))
    _head_home(shared_head_home)
    monkeypatch.chdir(shared_head_home)
    argv, env = hl.codex_head_command("/root", settings, safe=safe, binary="/bin/codex")

    assert argv[:3] == ["/bin/codex", "--remote", "unix://"]
    assert argv[-2:] == ["-C", str(shared_head_home.resolve())]
    assert argv[argv.index("--ask-for-approval") + 1] == "on-request"
    assert 'approvals_reviewer="user"' in argv
    profile = "head" if safe else ":danger-full-access"
    assert f'default_permissions="{profile}"' in argv
    assert env["CODEX_CONTROL_SEAT"] == "seat1"


def test_shared_head_honors_explicit_repo_and_keeps_other_seats_separate(
    shared_head_home, monkeypatch
):
    settings = registry_settings(primary_home=str(shared_head_home))
    argv, _ = hl.codex_head_command("/root", settings, repo="/project", binary="/bin/codex")
    assert argv[-2:] == ["-C", "/project"]
    # Even a socket in a non-primary home must not change controller attribution.
    monkeypatch.setattr(hl, "runtime_seat_home", lambda *args: shared_head_home)
    argv, env = hl.codex_head_command(
        shared_head_home.parent, settings, seat=2, binary="/bin/codex"
    )
    assert "--remote" not in argv
    assert env["CODEX_CONTROL_SEAT"] == "seat2"


def test_primary_head_without_daemon_keeps_embedded_launch(tmp_path):
    settings = registry_settings(primary_home=str(tmp_path))
    argv, _ = hl.codex_head_command("/root", settings, binary="/bin/codex")
    assert "--remote" not in argv
    assert 'default_permissions=":danger-full-access"' in argv


def test_codex_daemon_versions_uses_official_json_report(tmp_path):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _process_result(
            {
                "status": "running",
                "cliVersion": "0.155.1",
                "managedCodexVersion": "0.155.1",
                "appServerVersion": "0.153.4",
                "ignored": "bounded result",
            }
        )

    versions = hl.codex_daemon_versions("/bin/codex", tmp_path, run=run)

    assert versions == {
        "status": "running",
        "cliVersion": "0.155.1",
        "managedCodexVersion": "0.155.1",
        "appServerVersion": "0.153.4",
    }
    assert calls[0][0] == ["/bin/codex", "app-server", "daemon", "version"]
    assert calls[0][1]["env"]["CODEX_HOME"] == str(tmp_path)


def _daemon_report(status="running", server="0.155.1"):
    return {
        "status": status,
        "cliVersion": "0.155.1",
        "managedCodexVersion": "0.155.1",
        "appServerVersion": server,
    }


def test_current_codex_daemon_passes_read_only_check(tmp_path):
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return _process_result(_daemon_report())

    versions = hl.check_codex_daemon("/bin/codex", tmp_path, run=run)

    assert versions["appServerVersion"] == "0.155.1"
    assert calls == [["/bin/codex", "app-server", "daemon", "version"]]


@pytest.mark.parametrize(
    ("report", "state"),
    [
        (_daemon_report(server="0.153.4"), "is stale"),
        (_daemon_report(status="stopped"), "not running"),
    ],
)
def test_stale_or_stopped_daemon_is_refused_without_update(tmp_path, report, state):
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return _process_result(report)

    with pytest.raises(hl.CodexDaemonError, match=state) as raised:
        hl.check_codex_daemon("/bin/codex", tmp_path, run=run)

    message = str(raised.value)
    assert "does not update or restart" in message
    assert "`codex app-server daemon update`" in message
    assert calls == [["/bin/codex", "app-server", "daemon", "version"]]
    assert not hasattr(hl, "reconcile_codex_daemon")
    assert not hasattr(hl, "CODEX_DAEMON_UPDATE_TIMEOUT_SECONDS")


def test_primary_shared_head_checks_daemon_before_launch(
    shared_head_home, monkeypatch
):
    settings = registry_settings(primary_home=str(shared_head_home))
    events = []

    monkeypatch.setattr(hl.workers, "resolve_executable", lambda *_args: "/bin/codex")
    monkeypatch.setattr(
        hl,
        "check_codex_daemon",
        lambda binary, home: events.append(("check", binary, home)),
    )
    monkeypatch.setattr(
        hl,
        "launch",
        lambda argv, env: events.append(("launch", argv, env)),
    )

    args = SimpleNamespace(head_kind="codex", seat=None, cd=None, safe=False)

    assert hl.cmd_head(args, settings, shared_head_home.parent) == 0
    assert events[0] == ("check", "/bin/codex", shared_head_home)
    assert events[1][0] == "launch"
    assert "--remote" in events[1][1]


def test_stale_shared_daemon_blocks_launch_and_never_runs_update(
    shared_head_home, monkeypatch, capsys
):
    settings = registry_settings(primary_home=str(shared_head_home))
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return _process_result(_daemon_report(server="0.153.4"))

    monkeypatch.setattr(hl.workers, "resolve_executable", lambda *_args: "/bin/codex")
    versions = hl.codex_daemon_versions
    monkeypatch.setattr(
        hl, "codex_daemon_versions",
        lambda binary, home, **_kwargs: versions(binary, home, run=run),
    )
    monkeypatch.setattr(hl, "launch", lambda *_args: pytest.fail("must not launch"))
    args = SimpleNamespace(head_kind="codex", seat=None, cd=None, safe=False)

    assert hl.cmd_head(args, settings, shared_head_home.parent) == 2
    assert "is stale" in capsys.readouterr().err
    assert all(argv[-1] != "update" for argv in calls)


def test_head_parser_has_no_manual_daemon_reconciliation_flags():
    args = build_parser().parse_args(["head", "codex"])

    assert not hasattr(args, "reconcile_daemon")
    assert not hasattr(args, "from_current_user_request")

    with pytest.raises(SystemExit):
        build_parser().parse_args(["head", "codex", "--reconcile-daemon"])


def _full_access_command(*, seat=None, repo=None):
    return hl.codex_head_command(
        "/root",
        SETTINGS,
        seat=seat,
        repo=repo,
        binary="/bin/codex",
    )


def test_codex_head_defaults_to_official_full_access_on_primary_identity():
    argv, env = _full_access_command()

    assert argv[:5] == [
        "/bin/codex",
        "--ask-for-approval",
        "on-request",
        "-c",
        'approvals_reviewer="user"',
    ]
    assert 'default_permissions=":danger-full-access"' in argv
    assert not any(value.startswith("permissions.operator=") for value in argv)
    instruction = next(value for value in argv if value.startswith("developer_instructions="))
    assert "codex-seat delegate" in instruction
    assert "final acceptor" in instruction
    assert "deep_research" in instruction
    assert "--tier routine|complex|frontier" in instruction
    assert "--effort low|medium|high|xhigh|max|ultra" in instruction
    assert "--override-reason" in instruction
    assert env["CODEX_HOME"] == "/Users/example/.codex"
    assert env["CODEX_CONTROL_SEAT"] == "seat1"
    assert env["CODEX_SEATS_HEAD"] == "codex"
    assert env["CODEX_SEATS_HEAD_MODE"] == "full-access"


def test_codex_full_access_head_can_select_another_seat():
    argv, env = _full_access_command(seat=2)

    assert argv[:5] == [
        "/bin/codex",
        "--ask-for-approval",
        "on-request",
        "-c",
        'approvals_reviewer="user"',
    ]
    assert 'default_permissions=":danger-full-access"' in argv
    assert 'default_permissions="head"' not in argv
    assert env["CODEX_HOME"] == "/root/seat2"
    assert env["CODEX_CONTROL_SEAT"] == "seat2"
    assert env["CODEX_SEATS_HEAD"] == "codex"
    assert env["CODEX_SEATS_HEAD_MODE"] == "full-access"


def test_safe_head_pins_contained_profile_with_interactive_approvals(tmp_path):
    argv, env = hl.codex_head_command(
        "/root",
        _home_settings(_head_home(tmp_path)),
        safe=True,
        binary="/bin/codex",
    )

    assert argv[:5] == [
        "/bin/codex",
        "--ask-for-approval",
        "on-request",
        "-c",
        'approvals_reviewer="user"',
    ]
    assert 'default_permissions="head"' in argv
    instruction = next(value for value in argv if value.startswith("developer_instructions="))
    assert "ui_ux" in instruction
    assert "codex-seat await" in instruction
    assert env["CODEX_SEATS_HEAD_MODE"] == "safe"


def test_codex_head_opens_repo_without_bypassing_sandbox():
    argv, _ = _full_access_command(repo="~/proj")

    assert "-C" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert 'default_permissions=":danger-full-access"' in argv


def test_claude_head_writes_walled_settings_and_sets_cwd(tmp_path):
    import json

    argv, env, cwd = hl.claude_head_command(tmp_path, SETTINGS, repo="/Users/example/proj", binary="/bin/claude")
    assert argv[0] == "/bin/claude" and "--settings" in argv
    assert "--append-system-prompt" in argv
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert "codex-seat delegate" in prompt
    assert "smaller work may be done inline" in prompt
    assert "deep_research" in prompt
    assert "--override-reason" in prompt
    assert env["CODEX_SEATS_HEAD"] == "claude"
    assert env["CODEX_SEATS_LAUNCHER_RELEASE_ID"] == "unpinned"
    assert env["CODEX_SEATS_LAUNCHER_SETTINGS_MIN"] == "3"
    assert env["CODEX_SEATS_LAUNCHER_SETTINGS_MAX"] == "3"
    diagnostics = json.loads(env["CODEX_SEATS_LAUNCHER_DIAGNOSTICS"])
    assert diagnostics["reader_kind"] == "head-launcher"
    assert diagnostics["release"] == {"id": None, "source": "unpinned"}
    assert cwd == "/Users/example/proj"
    doc = json.loads((tmp_path / ".claude-head-settings.json").read_text())
    deny = doc["permissions"]["deny"]
    assert any("auth.json" in rule for rule in deny)   # credentials walled off
    assert any(".ssh" in rule for rule in deny)
    assert doc["permissions"]["defaultMode"] == "acceptEdits"  # repo edits don't prompt
    pool_settings = "/" + str(tmp_path / "settings.json")
    assert f"Edit({pool_settings})" in deny
    assert f"Read({pool_settings})" not in deny  # cached controller evidence stays readable
    assert "Bash(kubectl *)" not in deny


def test_claude_head_no_repo_has_no_cwd(tmp_path):
    _, _, cwd = hl.claude_head_command(tmp_path, SETTINGS, binary="/bin/claude")
    assert cwd is None


def test_head_parser_accepts_numeric_codex_seat():
    args = build_parser().parse_args(
        ["head", "codex", "--safe", "--seat", "2", "--cd", "/repo"]
    )
    assert args.seat == "2"
    assert args.cd == "/repo"
    assert args.safe is True


def test_head_parser_rejects_removed_unsafe_direct():
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["head", "codex", "--safe", "--unsafe-direct"]
        )


@pytest.mark.parametrize("argv", [["run", "work"], ["queue"], ["probe", "--drain"]])
def test_removed_mutable_queue_cli_surfaces_are_absent(argv):
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)


def test_claude_head_rejects_codex_seat_selector(capsys):
    args = SimpleNamespace(
        head_kind="claude",
        seat=2,
        cd=None,
        safe=False,
    )
    assert hl.cmd_head(args, SETTINGS, "/root") == 2
    assert "--seat is only valid for a Codex head" in capsys.readouterr().err


def test_claude_head_rejects_safe_mode(capsys):
    args = SimpleNamespace(
        head_kind="claude",
        seat=None,
        cd=None,
        safe=True,
    )

    assert hl.cmd_head(args, SETTINGS, "/root") == 2
    assert "only valid for a Codex head" in capsys.readouterr().err
