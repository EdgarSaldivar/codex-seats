from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_seats import releases, runtime_readers as readers
from codex_seats.state import default_settings, load_settings


RELEASE_ID = "a" * 64


def test_process_birth_lookup_uses_utc_across_local_dst_ambiguity(monkeypatch):
    monkeypatch.setenv("TZ", "America/Los_Angeles")

    def run(argv, *, timeout, env):
        assert argv == ["/bin/ps", "-p", "123", "-o", "lstart="]
        assert timeout == 2.0
        assert env["TZ"] == "UTC0" and env["LC_ALL"] == "C"
        return SimpleNamespace(
            returncode=0,
            stdout_text=lambda encoding: "Sun Nov  1 08:30:00 2026\n",
        )

    monkeypatch.setattr(readers.bounded_process, "run", run)
    expected = datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc).timestamp()
    assert readers._pid_started_at(123) == expected


def _metadata(settings: dict, *, heartbeat: float = 100.0) -> dict:
    return readers.build_metadata(
        "dashboard-watch",
        settings,
        root=Path("/unused"),
        pid=123,
        process_identity="1" * 32,
        process_started_at=90.0,
        observed_at=heartbeat,
        heartbeat_at=heartbeat,
        release={"id": RELEASE_ID, "source": "immutable"},
    )


def _write_reader(root: Path, metadata: dict) -> Path:
    path = root / "readers" / f"reader-{metadata['process']['identity']}.json"
    readers._atomic_write(path, metadata)
    return path


def test_release_runtime_and_default_manifest_support_only_settings_schema_three():
    assert releases.CURRENT_PROTOCOLS["settings"] == 3
    assert releases.DEFAULT_COMPATIBILITY["settings"] == {"min": 3, "max": 3}
    assert (readers.SETTINGS_MIN, readers.SETTINGS_MAX) == (3, 3)
    manifest = {"protocol_compatibility": releases.DEFAULT_COMPATIBILITY}
    assert releases.protocol_issues(manifest) == []
    incompatible = {
        "protocol_compatibility": {
            **releases.DEFAULT_COMPATIBILITY,
            "settings": {"min": 1, "max": 2},
        }
    }
    assert releases.protocol_issues(incompatible) == [
        "settings protocol 3 is outside release range 1..2"
    ]


def test_settings_identity_is_deterministic_and_contains_no_settings_payload():
    for schema in (1, 2, None):
        with pytest.raises(readers.ReaderMetadataError, match="must be 3"):
            readers.settings_identity({"schema": schema, "email": "x@example.test"})
    settings = default_settings()
    first = readers.settings_identity(settings)
    reordered = json.loads(json.dumps(settings, sort_keys=True))
    assert readers.settings_identity(reordered) == first
    assert first["registry_sha256"].startswith("sha256:")
    assert "email" not in json.dumps(first)


@pytest.mark.parametrize(
    ("mutate", "alive", "expected"),
    [
        (lambda value: None, True, None),
        (
            lambda value: value["supported_settings"].update(min=1, max=2),
            True,
            "does not support settings schema 3",
        ),
        (
            lambda value: value.update(heartbeat_at=0.0),
            True,
            "stale heartbeat",
        ),
        (
            lambda value: value["observed_settings"].update(
                registry_sha256="sha256:" + "f" * 64
            ),
            True,
            "observed stale settings identity",
        ),
        (lambda value: None, False, None),
    ],
)
def test_reader_inspection_handles_compatible_incompatible_stale_and_dead(
    tmp_path, mutate, alive, expected
):
    settings = default_settings()
    metadata = _metadata(settings)
    mutate(metadata)
    _write_reader(tmp_path, metadata)

    reports, blockers = readers.inspect_readers(
        tmp_path,
        current_identity=readers.settings_identity(settings),
        clock=lambda: 1000.0 if expected == "stale heartbeat" else 100.0,
        pid_alive=lambda _pid: alive,
        pid_started_at=lambda _pid: 90.0 if alive else None,
    )

    assert reports[0]["state"] == ("live" if alive else "dead")
    if expected is None:
        assert blockers == []
    else:
        assert any(expected in blocker for blocker in blockers)


def test_malformed_reader_blocks_even_when_its_process_cannot_be_identified(tmp_path):
    directory = tmp_path / "readers"
    directory.mkdir(mode=0o700)
    path = directory / f"reader-{'2' * 32}.json"
    path.write_text('{"reader_kind":"broken"}')
    os.chmod(path, 0o600)

    reports, blockers = readers.inspect_readers(
        tmp_path, pid_alive=lambda _pid: False
    )

    assert reports[0]["state"] == "malformed"
    assert "malformed settings reader" in blockers[0]


def test_reader_with_reused_live_pid_is_ignored_only_with_positive_birth_evidence(
    tmp_path,
):
    metadata = _metadata(default_settings(), heartbeat=100.0)
    _write_reader(tmp_path, metadata)

    reports, blockers = readers.inspect_readers(
        tmp_path,
        clock=lambda: 1000.0,
        pid_alive=lambda _pid: True,
        pid_started_at=lambda _pid: 500.0,
    )

    assert reports[0]["state"] == "pid-reused"
    assert reports[0]["current_process_started_at"] == 500.0
    assert blockers == []


@pytest.mark.parametrize("birth", [90.0, 101.0, None])
def test_stale_live_reader_blocks_when_pid_matches_or_birth_is_unavailable(
    tmp_path, birth
):
    metadata = _metadata(default_settings(), heartbeat=100.0)
    _write_reader(tmp_path, metadata)

    reports, blockers = readers.inspect_readers(
        tmp_path,
        clock=lambda: 1000.0 + readers.STALE_READER_SECONDS,
        pid_alive=lambda _pid: True,
        pid_started_at=lambda _pid: birth,
    )

    assert reports[0]["state"] == "live"
    assert any("stale heartbeat" in blocker for blocker in blockers)


def test_broken_reader_directory_symlink_blocks(tmp_path):
    (tmp_path / "readers").symlink_to(tmp_path / "missing-reader-directory")

    reports, blockers = readers.inspect_readers(tmp_path)

    assert reports == []
    assert any("cannot safely inspect settings readers" in item for item in blockers)


def test_reader_registers_before_loading_and_writes_owner_only_files(tmp_path):
    settings = default_settings()
    (tmp_path / "settings.json").write_text(json.dumps(settings))
    observed_before_load: list[dict] = []

    with readers.SettingsReader(
        tmp_path,
        "supervisor",
        clock=lambda: 50.0,
        pid=321,
        process_identity="5" * 32,
    ) as reader:
        def loader(root: Path):
            record = json.loads(reader.path.read_text())
            observed_before_load.append(record["observed_settings"])
            return load_settings(root)

        loaded, _warnings = reader.load(loader)

    assert loaded["schema"] == 3
    assert observed_before_load == [
        {"schema": None, "topology": "not-loaded", "registry_sha256": None}
    ]
    assert reader.path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / ".settings-readers.lock").stat().st_mode & 0o777 == 0o600


def test_reader_record_claiming_an_older_settings_schema_is_malformed(tmp_path):
    metadata = _metadata(default_settings())
    metadata["observed_settings"] = {
        "schema": 2,
        "topology": "legacy-schema-1-2-topology",
        "registry_sha256": None,
    }
    _write_reader(tmp_path, metadata)

    reports, blockers = readers.inspect_readers(
        tmp_path,
        clock=lambda: 100.0,
        pid_alive=lambda _pid: True,
        pid_started_at=lambda _pid: 90.0,
    )

    assert reports[0]["state"] == "malformed"
    assert any("malformed settings reader" in blocker for blocker in blockers)
