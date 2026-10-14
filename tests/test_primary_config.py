from __future__ import annotations

import os
import stat
from types import SimpleNamespace

import pytest
from codex_seats import cli
from codex_seats.primary_config import (
    PrimaryConfigError,
    full_access_payload,
    set_primary_full_access,
)


def test_payload_replaces_only_the_top_level_value_bytes():
    source = (
        b'model = "gpt-test"\r\n'
        b'default_permissions = ":workspace"  # keep this comment\r\n'
        b"\r\n[permissions.custom]\r\ndescription = \"keep\"\r\n"
    )

    updated = full_access_payload(source)

    assert updated == source.replace(b'":workspace"', b'":danger-full-access"')


def test_payload_prepends_missing_default_and_preserves_existing_bytes():
    source = b'# original\nmodel = "gpt-test"\n\n[permissions.custom]\nwrite = false\n'

    assert full_access_payload(source) == (
        b'default_permissions = ":danger-full-access"\n' + source
    )


@pytest.mark.parametrize(
    "source",
    [
        b"broken = [\n",
        b'default_permissions = """\n:workspace\n"""\n',
        b'default_permissions = 7\n[permissions.custom]\nwrite = false\n',
    ],
)
def test_payload_refuses_malformed_or_non_localizable_configs(source):
    with pytest.raises(PrimaryConfigError):
        full_access_payload(source)


@pytest.mark.parametrize(
    "source",
    [
        b'sandbox_mode = "workspace-write"\n',
        b'[sandbox_workspace_write]\nnetwork_access = true\n',
        (
            b'default_permissions = "head"\n'
            b'sandbox_mode = "workspace-write"\n'
        ),
    ],
)
def test_payload_refuses_legacy_sandbox_config(source):
    with pytest.raises(PrimaryConfigError, match="cannot be combined"):
        full_access_payload(source)


def test_payload_accepts_a_quoted_top_level_key_among_nested_names():
    source = (
        b'"default_permissions" = ":workspace"\n'
        b"[permissions.custom]\ndefault_permissions = \"nested\"\n"
    )

    assert full_access_payload(source) == source.replace(
        b'":workspace"', b'":danger-full-access"'
    )


def test_atomic_update_preserves_mode_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_SEATS_MANAGED_WORKER", raising=False)
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_bytes(b'model = "keep"\n')
    path.chmod(0o640)

    assert set_primary_full_access(path) is True
    first = path.read_bytes()
    assert first == b'default_permissions = ":danger-full-access"\nmodel = "keep"\n'
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert set_primary_full_access(path) is False
    assert path.read_bytes() == first
    assert not list(path.parent.glob(".config.toml.*"))


def test_managed_worker_is_refused_before_any_config_is_created(tmp_path, monkeypatch):
    path = tmp_path / ".codex" / "config.toml"
    monkeypatch.setenv("CODEX_SEATS_MANAGED_WORKER", "1")

    with pytest.raises(PrimaryConfigError, match="managed workers"):
        set_primary_full_access(path)

    assert not path.parent.exists()


def test_config_cli_updates_primary_dot_codex_without_using_real_home(tmp_path, monkeypatch):
    primary = tmp_path / ".codex"
    primary.mkdir()
    path = primary / "config.toml"
    path.write_text('model = "keep"\n')
    settings = {"pool": {"primary_home": "/different/seat/home"}}
    args = SimpleNamespace(set_primary_full_access=True, sync_workers=False)
    monkeypatch.delenv("CODEX_SEATS_MANAGED_WORKER", raising=False)
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)

    assert cli.cmd_config(args, settings) == 0
    assert path.read_text().startswith('default_permissions = ":danger-full-access"\n')


def test_config_parser_exposes_one_mutually_exclusive_full_access_operation():
    args = cli.build_parser().parse_args(["config", "--set-primary-full-access"])
    assert args.set_primary_full_access is True

    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["config", "--set-primary-full-access", "--sync-workers"]
        )


def test_failed_atomic_replace_leaves_original_and_removes_temporary(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_SEATS_MANAGED_WORKER", raising=False)
    path = tmp_path / "config.toml"
    original = b'default_permissions = ":workspace"\nmodel = "keep"\n'
    path.write_bytes(original)

    def fail_replace(source, target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        set_primary_full_access(path)

    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".config.toml.*"))
