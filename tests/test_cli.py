"""Terminal policy command parsing and the managed-worker write boundary."""

import json

from codex_seats import cli
from codex_seats.cli import build_parser, cmd_config
from codex_seats.state import default_settings


def test_policy_show_and_set_parser(capsys):
    parser = build_parser()
    show = parser.parse_args(["config", "policy", "show"])
    assert cmd_config(show, default_settings()) == 0
    assert '"review.mode": "off"' in capsys.readouterr().out
    change = parser.parse_args([
        "config", "policy", "set", "--set", "review.mode", "advisory", "--apply",
    ])
    assert change.policy_change == ["review.mode", "advisory"]


def test_managed_worker_cannot_apply_policy(monkeypatch, capsys):
    monkeypatch.setenv("CODEX_SEATS_MANAGED_WORKER", "1")
    args = build_parser().parse_args([
        "config", "policy", "set", "--set", "review.mode", "blocking", "--apply",
    ])
    assert cmd_config(args, default_settings()) == 2
    assert "policy apply refused:" in capsys.readouterr().err


def test_policy_set_previews_without_mutating(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    path = tmp_path / "settings.json"
    original = json.dumps(default_settings()) + "\n"
    path.write_text(original)
    args = build_parser().parse_args([
        "config", "policy", "set", "--set", "review.mode", "advisory",
    ])
    assert cmd_config(args, default_settings()) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["before"]["review.mode"] == "off"
    assert report["after"]["review.mode"] == "advisory"
    assert not report["applied"]
    assert path.read_text() == original
