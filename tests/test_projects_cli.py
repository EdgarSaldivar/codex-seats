from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from codex_seats import cli
from codex_seats.projects import (
    ProjectRegistryError,
    add_project,
    locked_settings,
    rename_project,
)


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path.resolve()


def _write_settings(root: Path, projects: dict[str, object] | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "settings.json").write_text(
        json.dumps(
            {
                "schema": 3,
                "custom": {"preserved": True},
                "projects": projects or {},
            }
        )
        + "\n"
    )


def _read_settings(root: Path) -> dict[str, object]:
    return json.loads((root / "settings.json").read_text())


def test_add_registers_canonical_git_root_and_preserves_settings(
    tmp_path: Path,
) -> None:
    pool = tmp_path / "pool"
    repo = _git_repo(tmp_path / "repo")
    _write_settings(pool)

    status, path = add_project(pool, "example-project", repo)

    assert status == "added"
    assert path == repo
    settings = _read_settings(pool)
    assert settings["custom"] == {"preserved": True}
    assert settings["projects"] == {
        "example-project": {"path": str(repo)}
    }
    assert stat.S_IMODE((pool / "settings.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((pool / ".settings.lock").stat().st_mode) == 0o600
    assert not list(pool.glob(".settings.json.*"))


def test_add_is_idempotent_for_same_id_and_path_without_overwriting_metadata(
    tmp_path: Path,
) -> None:
    pool = tmp_path / "pool"
    repo = _git_repo(tmp_path / "repo")
    entry = {
        "example-project": {
            "path": str(repo),
            "protected_branches": ["trunk"],
            "review_tier": "strict",
        }
    }
    _write_settings(pool, entry)

    status, _ = add_project(pool, "example-project", repo)

    assert status == "unchanged"
    assert _read_settings(pool)["projects"] == entry


def test_add_rejects_id_and_path_collisions(tmp_path: Path) -> None:
    pool = tmp_path / "pool"
    first = _git_repo(tmp_path / "first")
    second = _git_repo(tmp_path / "second")
    _write_settings(pool, {"existing": {"path": str(first)}})

    with pytest.raises(ProjectRegistryError, match="already registered at"):
        add_project(pool, "existing", second)
    with pytest.raises(ProjectRegistryError, match="already registered as 'existing'"):
        add_project(pool, "different", first)

    assert list(_read_settings(pool)["projects"]) == ["existing"]


@pytest.mark.parametrize(
    "project_id", ["", "Uppercase", "-leading", "trailing-", "has space"]
)
def test_add_rejects_unsafe_project_ids(tmp_path: Path, project_id: str) -> None:
    pool = tmp_path / "pool"
    repo = _git_repo(tmp_path / "repo")
    _write_settings(pool)

    with pytest.raises(ProjectRegistryError, match="project id must"):
        add_project(pool, project_id, repo)


def test_add_rejects_missing_non_git_and_nested_paths(tmp_path: Path) -> None:
    pool = tmp_path / "pool"
    _write_settings(pool)
    plain = tmp_path / "plain"
    plain.mkdir()
    repo = _git_repo(tmp_path / "repo")
    nested = repo / "nested"
    nested.mkdir()

    with pytest.raises(ProjectRegistryError, match="does not exist"):
        add_project(pool, "missing", tmp_path / "missing")
    with pytest.raises(ProjectRegistryError, match="not a Git repository"):
        add_project(pool, "plain", plain)
    with pytest.raises(ProjectRegistryError, match="must be the Git repository root"):
        add_project(pool, "nested", nested)


def test_rename_updates_id_and_moved_path_while_preserving_metadata(
    tmp_path: Path,
) -> None:
    pool = tmp_path / "pool"
    moved_repo = _git_repo(tmp_path / "new-name")
    old_path = tmp_path / "old-name"
    _write_settings(
        pool,
        {
            "old-id": {
                "path": str(old_path),
                "protected_branches": ["main"],
                "review_tier": "strict",
            },
            "other": {"path": str(_git_repo(tmp_path / "other"))},
        },
    )

    status, path = rename_project(pool, "old-id", "new-id", moved_repo)

    assert status == "renamed"
    assert path == moved_repo
    projects = _read_settings(pool)["projects"]
    assert list(projects) == ["new-id", "other"]
    assert projects["new-id"] == {
        "path": str(moved_repo),
        "protected_branches": ["main"],
        "review_tier": "strict",
    }


def test_rename_can_update_only_the_path_and_rejects_collisions(tmp_path: Path) -> None:
    pool = tmp_path / "pool"
    first = _git_repo(tmp_path / "first")
    second = _git_repo(tmp_path / "second")
    _write_settings(
        pool,
        {
            "first": {"path": str(first)},
            "second": {"path": str(second)},
        },
    )

    status, path = rename_project(pool, "first", "first", first)
    assert status == "updated"
    assert path == first
    with pytest.raises(
        ProjectRegistryError, match="new project id is already registered"
    ):
        rename_project(pool, "first", "second")
    with pytest.raises(ProjectRegistryError, match="already registered as 'second'"):
        rename_project(pool, "first", "renamed", second)


def test_locked_settings_blocks_another_process_and_persists_valid_json(
    tmp_path: Path,
) -> None:
    pool = tmp_path / "pool"
    _write_settings(pool)
    contender = (
        "import fcntl, os, sys; "
        "fd=os.open(sys.argv[1], os.O_RDWR); "
        "\ntry: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)"
        "\nexcept BlockingIOError: sys.exit(23)"
        "\nsys.exit(0)"
    )

    with locked_settings(pool) as settings:
        result = subprocess.run(
            [sys.executable, "-c", contender, str(pool / ".settings.lock")],
            check=False,
        )
        assert result.returncode == 23
        settings["projects"]["inside-lock"] = {"path": str(tmp_path)}

    assert _read_settings(pool)["projects"]["inside-lock"] == {"path": str(tmp_path)}


def test_cli_parser_and_command_wiring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    pool = tmp_path / "pool"
    repo = _git_repo(tmp_path / "repo")
    _write_settings(pool)
    monkeypatch.setattr(cli, "ROOT", pool)

    parsed = cli.build_parser().parse_args(
        [
            "projects",
            "add",
            "sample",
            str(repo),
        ]
    )
    assert parsed.handler == "projects"
    assert parsed.projects_action == "add"
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["projects", "add", "sample", str(repo), "--protected-branch", "trunk"]
        )

    assert cli.main(["projects", "add", "sample", str(repo)]) == 0
    assert "sample: registered" in capsys.readouterr().out
    assert cli.main(["projects", "list"]) == 0
    assert f"sample\t{repo}" in capsys.readouterr().out


def test_removed_protected_branch_flag_is_absent() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["projects", "add", "sample", "/repo", "--protected-branch", "main"]
        )


@pytest.mark.parametrize("command", ["approve-prod", "check-prod"])
def test_legacy_production_lease_commands_are_absent(command, capsys) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([command])
    assert "invalid choice" in capsys.readouterr().err


def test_cli_returns_usage_error_without_mutating_invalid_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    pool = tmp_path / "pool"
    pool.mkdir()
    settings_path = pool / "settings.json"
    settings_path.write_text("{broken")
    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setattr(cli, "ROOT", pool)

    assert cli.main(["projects", "add", "sample", str(repo)]) == 2
    assert "cannot read valid JSON" in capsys.readouterr().err
    assert settings_path.read_text() == "{broken"
