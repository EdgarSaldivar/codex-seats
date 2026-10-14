"""Synthetic Claude sidecar and terminal redemption flows."""
import threading
import json
import shlex
import sys
from pathlib import Path
from unittest import mock

from codex_seats import bounded_process
from codex_seats.claude_manager import ClaudeAccountManager
from codex_seats.claude_usage_sidecar import ClaudeUsageSidecar
from codex_seats.seat_manager_tui import ManagerUI
from codex_seats.watch import WatchRefresher
from registry_fixture import registry_settings, write_settings
from test_seat_manager_tui import _FakeManager


def test_claude_sidecar_off_missing_tmux_and_fake_usage(tmp_path, monkeypatch):
    settings = registry_settings(["seat1"], deputy=None)
    cancel = threading.Event()
    calls = []
    monkeypatch.setattr("codex_seats.claude_usage_sidecar._tmux",
                        lambda *args, **_kwargs: calls.append(args) or
                        bounded_process.ProcessResult(tuple(args), 0, b"", b"", 1))
    sidecar = ClaudeUsageSidecar(tmp_path, settings, cancel)
    assert sidecar.start()[0] is False
    assert calls == []
    settings["providers"]["claude"]["mode"] = "manual_only"
    cancel.set()
    assert sidecar.start() == (False, "canceled")
    assert calls == []
    cancel.clear()
    monkeypatch.setattr("codex_seats.claude_usage_sidecar.shutil.which", lambda _name: None)
    assert sidecar.start() == (False, "tmux is unavailable")
    assert calls == []

    monkeypatch.setattr("codex_seats.claude_usage_sidecar.shutil.which", lambda name: f"/fake/{name}")
    monkeypatch.setattr("codex_seats.claude_usage_sidecar._trusted_claude_cwd", lambda *_a: tmp_path)
    monkeypatch.setattr(sidecar, "_write_settings", lambda: (tmp_path / "settings", tmp_path / "mcp"))
    monkeypatch.setattr(sidecar, "_pane_dead", lambda: None)
    pane = iter(["Claude Code ❯", "Current session Current week resets", "Current session Current week resets"])
    monkeypatch.setattr(sidecar, "_capture", lambda: next(pane, "Current session Current week resets"))
    versions = iter([{"five_hour": 1, "seven_day": 1}, {"five_hour": 2, "seven_day": 2}])
    monkeypatch.setattr("codex_seats.claude_usage_sidecar._usage_window_versions", lambda _root: next(versions, {"five_hour": 2, "seven_day": 2}))
    clock = [0.0]
    sidecar.monotonic = lambda: clock[0]
    sidecar.sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    assert sidecar.probe() == (True, "ok")
    assert any(args[:4] == ("send-keys", "-t", sidecar.session, "-l") and args[-1] == "/usage" for args in calls)
    sidecar.close()
    assert any(args[0] == "kill-session" for args in calls)


def test_claude_sidecar_settings_are_isolated_and_cleaned(tmp_path):
    sidecar = ClaudeUsageSidecar(tmp_path, registry_settings(["seat1"], deputy=None), threading.Event())
    settings_path, mcp_path = sidecar._write_settings()
    config = json.loads(settings_path.read_text())
    assert sys.executable in config["statusLine"]["command"]
    assert config["permissions"]["defaultMode"] == "dontAsk"
    assert json.loads(mcp_path.read_text()) == {"mcpServers": {}}
    directory = sidecar.directory
    sidecar.close()
    assert not directory.exists()


def test_claude_sidecar_child_env_is_allowlisted_and_failure_cleans_up(tmp_path, monkeypatch):
    settings = registry_settings(["seat1"], deputy=None)
    settings["providers"]["claude"]["mode"] = "manual_only"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-secret")
    monkeypatch.setenv("CODEX_SESSION_ID", "synthetic-session")
    monkeypatch.setenv("MCP_ACCESS_TOKEN", "synthetic-mcp")
    monkeypatch.setattr("codex_seats.claude_usage_sidecar.shutil.which", lambda name: f"/fake/{name}")
    monkeypatch.setattr("codex_seats.claude_usage_sidecar._trusted_claude_cwd", lambda *_a: tmp_path)
    calls = []

    def tmux(*args, **_kwargs):
        calls.append(args)
        code = 1 if args[0] == "send-keys" and args[-1] == "Enter" else 0
        return bounded_process.ProcessResult(args, code, b"", b"synthetic failure", 1)

    monkeypatch.setattr("codex_seats.claude_usage_sidecar._tmux", tmux)
    sidecar = ClaudeUsageSidecar(tmp_path, settings, threading.Event())
    assert sidecar.start()[0] is False
    launch = next(args for args in calls if args[0] == "send-keys" and args[-1] == "Enter")
    argv = shlex.split(launch[-2])
    assert argv[:2] == ["env", "-i"]
    child_vars = {item.split("=", 1)[0]: item.split("=", 1)[1]
                  for item in argv[2:] if "=" in item and not item.startswith("--")}
    assert child_vars["CODEX_SEATS_ROOT"] == str(tmp_path)
    assert child_vars["HOME"]
    assert "ANTHROPIC_API_KEY" not in child_vars
    assert "CODEX_SESSION_ID" not in child_vars
    assert "MCP_ACCESS_TOKEN" not in child_vars
    assert sidecar.directory is None
    assert any(args[0] == "kill-session" for args in calls)


def test_watch_claude_gate_and_shared_lock(tmp_path):
    settings = registry_settings(["seat1"], deputy=None)
    settings["providers"]["claude"]["mode"] = "manual_only"
    sidecar = mock.Mock()
    sidecar.probe.return_value = (False, "synthetic usage failure")
    refresh = WatchRefresher(tmp_path, settings, 300,
                             account_refresh=lambda *_a, **_k: ["seat1 account -> ok"],
                             claude_sidecar=sidecar)
    refresh.run_attempt()
    sidecar.probe.assert_called_once()
    assert refresh.snapshot().last_succeeded is False
    assert "Claude failed" in refresh.snapshot().last_result
    settings["providers"]["claude"]["active_usage_refresh"] = False
    refresh.run_attempt()
    sidecar.probe.assert_called_once()
    assert refresh.snapshot().last_succeeded is True
    settings["providers"]["claude"]["active_usage_refresh"] = True
    settings["providers"]["claude"]["mode"] = "off"
    refresh.run_attempt()
    sidecar.probe.assert_called_once()


def test_claude_refresh_setting_previews_in_existing_account(tmp_path):
    settings = registry_settings(["seat1"], deputy=None)
    write_settings(tmp_path, settings)
    manager = ClaudeAccountManager(tmp_path)
    assert manager.report()["active_usage_refresh"] is True
    preview = manager.preview(active_usage_refresh=False)
    assert preview.after["active_usage_refresh"] is False


def test_tui_preview_cancel_confirm_poll_is_background(tmp_path, monkeypatch):
    manager = _FakeManager()
    manager.root = Path(tmp_path)
    ui = ManagerUI(manager)
    ui.selected_id = "seat1"
    ui.view = "actions"
    preview = {"seat": "seat1", "home": "/fake/seat1", "provider_identity": '{"email":"one@example.test"}',
               "available_count": 1, "credit": {"fingerprint": "abc", "title": "One reset",
               "granted_at": 1, "expires_at": 2}}
    calls = []
    monkeypatch.setattr("codex_seats.seat_manager_tui.prepare_banked_reset_redemption",
                        lambda *_a, **_k: calls.append("preview") or preview)
    monkeypatch.setattr("codex_seats.seat_manager_tui.redeem_banked_reset",
                        lambda *_a, **_k: calls.append("redeem") or {"outcome": "reset", "refresh_error": None})
    assert any(action == "redeem-reset" for _label, action in ui._actions())
    ui._choose_action("redeem-reset")
    assert ui.view == "busy"
    ui.reset_thread.join(2)
    assert ui.poll() and ui.view == "confirm"
    assert "One reset" in "\n".join(ui._preview_material())
    ui.handle("esc")
    assert calls == ["preview"] and ui.reset_preview is None
    ui._choose_action("redeem-reset")
    ui.reset_thread.join(2)
    ui.poll()
    ui.handle("enter")
    assert ui.view == "busy"
    ui.reset_thread.join(2)
    assert ui.poll() and ui.view == "message"
    assert calls == ["preview", "preview", "redeem"]
