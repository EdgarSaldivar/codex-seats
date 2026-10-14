from __future__ import annotations

from types import SimpleNamespace

from codex_seats import cli
from codex_seats.state import default_settings


def _clear_controller_env(monkeypatch):
    for name in (
        "CODEX_SEATS_MANAGED_WORKER",
        "CODEX_CONTROL_SEAT",
        "CODEX_SEATS_HEAD",
        "CODEX_THREAD_ID",
        "CODEX_TURN_ID",
        "CODEX_HOME",
    ):
        monkeypatch.delenv(name, raising=False)


def test_remote_controller_can_enable_provider_only_with_request_flag(
    tmp_path, monkeypatch
):
    _clear_controller_env(monkeypatch)
    monkeypatch.setenv("CODEX_THREAD_ID", "remote-thread")
    monkeypatch.setenv("CODEX_HOME", "/codex/home")
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    args = SimpleNamespace(
        provider_name="claude",
        mode="manual_only",
        from_current_user_request=False,
        expires_at=None,
        reset_state=False,
        cancel_active=False,
    )
    assert cli.cmd_provider_set(args, default_settings()) == 2
    args.from_current_user_request = True
    assert cli.cmd_provider_set(args, default_settings()) == 0


def test_claude_head_can_enable_provider_without_a_tty(tmp_path, monkeypatch):
    _clear_controller_env(monkeypatch)
    monkeypatch.setenv("CODEX_SEATS_HEAD", "claude")
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    args = SimpleNamespace(
        provider_name="claude",
        mode="manual_only",
        from_current_user_request=True,
        expires_at=None,
        reset_state=False,
        cancel_active=False,
    )
    assert cli.cmd_provider_set(args, default_settings()) == 0


def test_arbitrary_noninteractive_shell_cannot_enable_provider(
    tmp_path, monkeypatch
):
    _clear_controller_env(monkeypatch)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    provider_args = SimpleNamespace(
        provider_name="claude",
        mode="manual_only",
        from_current_user_request=True,
        expires_at=None,
        reset_state=False,
        cancel_active=False,
    )
    assert cli.cmd_provider_set(provider_args, default_settings()) == 2
