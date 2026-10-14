from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

from codex_seats import cli
from codex_seats.state import iso
from registry_fixture import registry_settings, write_settings


def test_pick_seat_uses_read_only_snapshot_without_persisting(tmp_path, monkeypatch, capsys):
    root = tmp_path / "pool"
    root.mkdir()
    seat_home = root / "seat3"
    seat_home.mkdir()
    (seat_home / "auth.json").write_text("present")
    state_path = root / "seats.json"
    lock_path = root / ".ledger.lock"
    observed = time.time()
    original = json.dumps(
        {
            "seats": {
                "seat3": {
                    "status": "ok",
                    "pct_left": 90,
                    "probed_at": iso(observed),
                    "usage_updated_at": iso(observed),
                    "usage_observed_at": observed,
                    "last_probe_limits": {
                        "weekly": {
                            "pct": 90,
                            "window_minutes": 10_080,
                            "resets_at": observed + 3_600,
                        }
                    },
                    "active_window_observation": {
                        "source": "account",
                        "complete": True,
                        "observed_at": observed,
                        "observed_windows": ["weekly"],
                        "active_windows": ["weekly"],
                    },
                }
            },
            "jobs": [],
        },
        separators=(",", ":"),
    ).encode()
    settings = write_settings(root, registry_settings())
    state_path.write_bytes(original)
    lock_path.write_bytes(b"lock-sentinel")
    state_path.chmod(0o444)
    lock_path.chmod(0o444)
    seat_home.chmod(0o555)
    root.chmod(0o555)
    monkeypatch.setattr(cli, "ROOT", root)

    try:
        result = cli.cmd_pick_seat(
            SimpleNamespace(kind="review", size="small"), settings
        )
    finally:
        root.chmod(0o755)
        seat_home.chmod(0o755)

    assert result == 0
    assert capsys.readouterr().out == "seat3\n"
    assert state_path.read_bytes() == original
    assert lock_path.read_bytes() == b"lock-sentinel"
    assert stat_mode(state_path) == 0o444
    assert stat_mode(lock_path) == 0o444


def stat_mode(path):
    return os.stat(path).st_mode & 0o777


def test_seat_selectors_accept_only_stable_ids_and_numeric_shorthand(capsys):
    settings = registry_settings()
    for selector, resolved in {"1": "seat1", "seat5": "seat5", " 3 ": "seat3"}.items():
        assert cli._selected_seat(selector, settings) == resolved
    for selector in ("P", "D", "W1", "W3", "M1", "M2", "W", "0", "01", "seat6"):
        assert cli._selected_seat(selector, settings) is None
    assert capsys.readouterr().err == ""
