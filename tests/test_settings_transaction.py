from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from pathlib import Path

import pytest

from codex_seats.projects import add_project
from codex_seats.state import (
    SettingsMutationError,
    default_settings,
    set_claude_mode,
    settings_transaction,
)


def _write_settings(root: Path, value: dict[str, object]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "settings.json"
    path.write_text(json.dumps(value, separators=(",", ":")))
    return path


def test_shared_transaction_validates_schema_three_and_preserves_unknowns(
    tmp_path: Path,
) -> None:
    raw: dict[str, object] = {
        "schema": 3,
        "future": {"preserved": 3},
        "pool": {"primary": None, "seats": {}},
    }
    _write_settings(tmp_path, raw)

    set_claude_mode(tmp_path, "manual_only")

    current = json.loads((tmp_path / "settings.json").read_text())
    assert current["schema"] == 3
    assert current["future"] == {"preserved": 3}
    assert current["providers"]["claude"]["mode"] == "manual_only"


@pytest.mark.parametrize("schema", [None, 1, 2, 4])
def test_shared_transaction_refuses_other_schemas_without_writing(
    tmp_path: Path, schema: object
) -> None:
    raw: dict[str, object] = {"future": {"preserved": True}}
    if schema is not None:
        raw["schema"] = schema
    path = _write_settings(tmp_path, raw)
    before = path.read_bytes()

    with pytest.raises(ValueError, match="only schema 3"):
        set_claude_mode(tmp_path, "manual_only")

    assert path.read_bytes() == before
    assert not list(tmp_path.glob("settings.json.backup-*"))


def test_concurrent_provider_and_project_updates_preserve_each_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = tmp_path / "pool"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        "codex_seats.projects.canonical_git_root", lambda _value: project
    )
    set_claude_mode(pool, "off")
    provider_reloaded = threading.Event()
    release_provider = threading.Event()
    project_attempted = threading.Event()
    errors: list[BaseException] = []

    def provider_hook(phase: str) -> None:
        if phase == "after_reload":
            provider_reloaded.set()
            assert release_provider.wait(5)

    def update_provider() -> None:
        try:
            set_claude_mode(
                pool,
                "opportunistic",
                expires_at="2030-01-01T00:00:00Z",
                _hook=provider_hook,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def update_project() -> None:
        project_attempted.set()
        try:
            add_project(pool, "sample", project)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    provider_thread = threading.Thread(target=update_provider)
    project_thread = threading.Thread(target=update_project)
    provider_thread.start()
    assert provider_reloaded.wait(2)
    project_thread.start()
    assert project_attempted.wait(2)
    project_thread.join(0.05)
    assert project_thread.is_alive(), "project mutation must wait for settings lock"

    release_provider.set()
    provider_thread.join(2)
    project_thread.join(2)

    assert not provider_thread.is_alive()
    assert not project_thread.is_alive()
    assert errors == []
    current = json.loads((pool / "settings.json").read_text())
    assert current["providers"]["claude"]["mode"] == "opportunistic"
    assert current["providers"]["claude"]["expires_at"] == "2030-01-01T00:00:00Z"
    assert current["projects"]["sample"] == {"path": str(project)}


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("symlink", "non-symbolic-link"),
        ("hardlink", "singly linked"),
    ],
)
def test_mutation_rejects_linked_settings_without_touching_target(
    tmp_path: Path, kind: str, message: str
) -> None:
    pool = tmp_path / "pool"
    pool.mkdir()
    target = tmp_path / "target.json"
    original = json.dumps(default_settings())
    target.write_text(original)
    settings = pool / "settings.json"
    if kind == "symlink":
        settings.symlink_to(target)
    else:
        os.link(target, settings)

    with pytest.raises(ValueError, match=message):
        set_claude_mode(pool, "manual_only")

    assert target.read_text() == original
    assert not list(pool.glob("settings.json.backup-*"))


def test_mutation_rejects_invalid_and_non_owner_controlled_files(
    tmp_path: Path,
) -> None:
    invalid_pool = tmp_path / "invalid"
    invalid = _write_settings(invalid_pool, {"schema": 99, "future": True})
    with pytest.raises(ValueError, match="unsupported settings schema 99"):
        set_claude_mode(invalid_pool, "off")
    assert json.loads(invalid.read_text()) == {"schema": 99, "future": True}

    writable_pool = tmp_path / "writable"
    writable = _write_settings(writable_pool, {"schema": 3, "pool": {"seats": {}}})
    writable.chmod(0o666)
    with pytest.raises(ValueError, match="owner-controlled"):
        set_claude_mode(writable_pool, "off")
    assert stat.S_IMODE(writable.stat().st_mode) == 0o666


def test_interrupted_replace_preserves_original_and_content_backup(
    tmp_path: Path, monkeypatch,
) -> None:
    original = '{"schema":3,"future":{"preserved":true}}'
    path = tmp_path / "settings.json"
    path.write_text(original)

    def interrupt(phase: str) -> None:
        if phase == "before_replace":
            raise RuntimeError("injected interruption")

    with pytest.raises(RuntimeError, match="injected interruption"):
        set_claude_mode(tmp_path, "manual_only", _hook=interrupt)

    assert path.read_text() == original
    backups = list(tmp_path.glob("settings.json.backup-sha256-*"))
    assert len(backups) == 1
    assert backups[0].name == (
        "settings.json.backup-sha256-"
        + hashlib.sha256(original.encode()).hexdigest()
    )
    assert backups[0].read_text() == original
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".settings.json.*"))


def test_invalid_candidate_fails_before_backup_or_replace(tmp_path: Path) -> None:
    set_claude_mode(tmp_path, "off")
    original = (tmp_path / "settings.json").read_text()

    with pytest.raises(SettingsMutationError, match="providers must be an object"):
        with settings_transaction(tmp_path) as transaction:
            transaction.settings["providers"] = "invalid"

    assert (tmp_path / "settings.json").read_text() == original
    assert not list(tmp_path.glob("settings.json.backup-*"))


def test_byte_identical_no_op_is_deterministic(tmp_path: Path) -> None:
    first = set_claude_mode(tmp_path, "manual_only")
    path = tmp_path / "settings.json"
    before = path.stat()

    second = set_claude_mode(tmp_path, "manual_only")
    after = path.stat()

    assert second == first
    assert (after.st_dev, after.st_ino, after.st_mtime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_mtime_ns,
    )
    assert not list(tmp_path.glob("settings.json.backup-*"))
