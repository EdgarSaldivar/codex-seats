"""Offline guards for policy writes, head launch, and packaged usage helper."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from codex_seats import cli, watch
from codex_seats.seat_manager import ManagerEligibilityError
from codex_seats.state import default_settings


def test_policy_apply_requires_management_eligibility_and_preserves_bytes(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(default_settings(), indent=2) + "\n")
    before = path.read_bytes()
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    args = argparse.Namespace(
        config_command="policy", policy_action="set", policy_change=["review.mode", "blocking"],
        apply=True,
    )
    with mock.patch("codex_seats.seat_manager.ensure_management_eligible", side_effect=ManagerEligibilityError("real human terminal required")) as eligible:
        assert cli.cmd_config(args, default_settings()) == 2
    eligible.assert_called_once_with()
    assert path.read_bytes() == before
    args.apply = False
    assert cli.cmd_config(args, default_settings()) == 0
    assert path.read_bytes() == before


@pytest.mark.parametrize("family", ["codex", "claude"])
def test_invalid_settings_prevent_head_launcher_exec(tmp_path, monkeypatch, family):
    path = tmp_path / "settings.json"
    path.write_bytes(b"{invalid json\n")
    before = path.read_bytes()
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    with mock.patch("codex_seats.head_launcher.cmd_head") as launch:
        assert cli.main(["head", family]) == 2
    launch.assert_not_called()
    assert path.read_bytes() == before


def test_installed_statusline_helper_runs_on_stdlib_python(tmp_path, monkeypatch):
    package_helper = Path(watch.__file__).with_name("claude_statusline.py")
    assert package_helper.is_file()
    python = Path("/usr/bin/python3") if Path("/usr/bin/python3").is_file() else Path(sys.executable)
    env = {**os.environ, "CODEX_SEATS_ROOT": str(tmp_path)}
    result = subprocess.run([str(python), str(package_helper)], input=json.dumps({
        "model": {"display_name": "Claude"},
        "rate_limits": {"five_hour": {"used_percentage": 25, "resets_at": 9999999999}},
    }), text=True, capture_output=True, env=env, check=True, timeout=15)
    assert "5h 75% left" in result.stdout
    assert json.loads((tmp_path / "claude-usage.json").read_text())["five_hour"]["used_percentage"] == 25


def test_statusline_compatibility_entrypoint_in_pool_root(tmp_path):
    source = Path(watch.__file__).resolve().parents[2]
    wrapper = tmp_path / "claude-statusline.py"
    shutil.copyfile(source / "claude-statusline.py", wrapper)
    (tmp_path / "current").symlink_to(source, target_is_directory=True)
    result = subprocess.run(
        [sys.executable, str(wrapper)], input="{}", text=True,
        capture_output=True, check=True, timeout=15,
        env={**os.environ, "CODEX_SEATS_ROOT": str(tmp_path)},
    )
    assert result.stdout.strip() == "[Claude]"
