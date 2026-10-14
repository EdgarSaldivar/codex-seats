from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_seats.seat_admin import (
    SeatAdminError,
    add_seat,
    set_seat,
    settings_digest,
)
from codex_seats.state import default_settings


NOW = 1_788_454_400.0


def _write_settings(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(default_settings(), indent=2) + "\n"
    (root / "settings.json").write_text(payload)
    (root / "settings.json").chmod(0o600)
    return payload


def _add(root: Path, label: str = "Standard") -> str:
    return add_seat(root, label=label, _clock=lambda: NOW).seat_id


def test_preview_publishes_full_settings_byte_digest(tmp_path: Path) -> None:
    _write_settings(tmp_path)
    seat_id = _add(tmp_path)
    preview = set_seat(tmp_path, seat_id, label="Renamed", dry_run=True)
    assert preview.digest is not None
    assert preview.digest == settings_digest(tmp_path)


def test_apply_with_matching_digest_succeeds(tmp_path: Path) -> None:
    _write_settings(tmp_path)
    seat_id = _add(tmp_path)
    preview = set_seat(tmp_path, seat_id, label="Renamed", dry_run=True)
    applied = set_seat(
        tmp_path, seat_id, label="Renamed", expected_digest=preview.digest
    )
    assert applied.changed
    current = json.loads((tmp_path / "settings.json").read_text())
    assert current["pool"]["seats"][seat_id]["label"] == "Renamed"


def test_apply_with_stale_digest_is_rejected_before_any_write(tmp_path: Path) -> None:
    _write_settings(tmp_path)
    seat_id = _add(tmp_path)
    preview = set_seat(tmp_path, seat_id, label="Planned", dry_run=True)

    # A concurrent change since the preview shifts the settings-byte digest.
    set_seat(tmp_path, seat_id, label="Concurrent")
    before = (tmp_path / "settings.json").read_text()

    with pytest.raises(SeatAdminError, match="previewed"):
        set_seat(tmp_path, seat_id, label="Planned", expected_digest=preview.digest)

    # Nothing was written by the rejected apply.
    assert (tmp_path / "settings.json").read_text() == before


def test_legacy_callers_without_digest_remain_compatible(tmp_path: Path) -> None:
    _write_settings(tmp_path)
    seat_id = _add(tmp_path)
    applied = set_seat(tmp_path, seat_id, label="NoDigest")
    assert applied.changed
    assert applied.digest is not None  # write path still reports it
