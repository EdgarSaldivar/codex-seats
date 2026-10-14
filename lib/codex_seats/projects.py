"""Safe mutation commands for the registered-project allowlist.

Project registration is part of the controller trust boundary: dispatch accepts only a
project id found in ``settings.projects`` and resolves it to the configured absolute path.
This module keeps operators out of hand-edited JSON while preserving that explicit boundary.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import internal_git
from .state import (
    SettingsMutationError,
    default_settings,
    settings_path,
    settings_transaction,
)


PROJECT_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
class ProjectRegistryError(ValueError):
    """An operator-safe project registry validation failure."""


def validate_project_id(value: str, *, label: str = "project id") -> str:
    if not PROJECT_ID.fullmatch(value):
        raise ProjectRegistryError(
            f"{label} must be 1-64 lowercase letters, digits, '.', '_' or '-', "
            "and must start and end with a letter or digit"
        )
    return value


def canonical_git_root(value: str | Path) -> Path:
    requested = Path(value).expanduser()
    try:
        resolved = requested.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectRegistryError(f"project path does not exist: {requested}") from exc
    if not resolved.is_dir():
        raise ProjectRegistryError(f"project path is not a directory: {resolved}")
    try:
        result = internal_git.run(
            resolved,
            ("rev-parse", "--show-toplevel"),
            timeout=10.0,
        )
    except internal_git.GitError as exc:
        raise ProjectRegistryError(
            f"could not inspect Git repository at {resolved}: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", "replace").splitlines()
        suffix = f" ({detail[-1]})" if detail else ""
        raise ProjectRegistryError(
            f"project path is not a Git repository: {resolved}{suffix}"
        )
    try:
        git_root = Path(
            internal_git.line(result.stdout, label="repository root")
        ).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectRegistryError(
            f"Git returned an invalid repository root for {resolved}"
        ) from exc
    if git_root != resolved:
        raise ProjectRegistryError(
            f"project path must be the Git repository root: {resolved} (root is {git_root})"
        )
    return resolved


def _read_raw_settings(root: Path) -> dict[str, Any]:
    path = settings_path(root)
    if not path.exists():
        return default_settings()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectRegistryError(
            f"cannot read valid JSON from {path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise ProjectRegistryError(f"settings top level must be an object: {path}")
    projects = raw.get("projects")
    if projects is None:
        raw["projects"] = {}
    elif not isinstance(projects, dict):
        raise ProjectRegistryError("settings.projects must be an object")
    return raw


@contextmanager
def locked_settings(root: Path) -> Iterator[dict[str, Any]]:
    """Compatibility wrapper over the shared secure settings transaction."""
    try:
        with settings_transaction(root) as transaction:
            yield transaction.settings
    except SettingsMutationError as exc:
        raise ProjectRegistryError(str(exc)) from exc


def _projects(settings: dict[str, Any]) -> dict[str, Any]:
    projects = settings.setdefault("projects", {})
    if not isinstance(projects, dict):
        raise ProjectRegistryError("settings.projects must be an object")
    return projects


def _entry_path(project_id: str, entry: Any) -> Path | None:
    # Template-only comment keys are not registry entries.
    if project_id.startswith("_"):
        return None
    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
        raise ProjectRegistryError(
            f"registered project {project_id!r} has no string path"
        )
    try:
        return Path(entry["path"]).expanduser().resolve(strict=False)
    except RuntimeError as exc:
        raise ProjectRegistryError(
            f"registered project {project_id!r} has an invalid path"
        ) from exc


def _ensure_unique_path(
    projects: dict[str, Any], path: Path, *, excluding: str | None = None
) -> None:
    for existing_id, entry in projects.items():
        if existing_id == excluding:
            continue
        existing_path = _entry_path(existing_id, entry)
        if existing_path == path:
            raise ProjectRegistryError(
                f"project path is already registered as {existing_id!r}: {path}; "
                "use 'projects rename' instead"
            )


def add_project(
    root: Path,
    project_id: str,
    path_value: str | Path,
) -> tuple[str, Path]:
    project_id = validate_project_id(project_id)
    path = canonical_git_root(path_value)
    with locked_settings(root) as settings:
        projects = _projects(settings)
        if project_id in projects:
            existing_path = _entry_path(project_id, projects[project_id])
            if existing_path == path:
                return "unchanged", path
            raise ProjectRegistryError(
                f"project id {project_id!r} is already registered at {existing_path}"
            )
        _ensure_unique_path(projects, path)
        projects[project_id] = {"path": str(path)}
    return "added", path


def rename_project(
    root: Path,
    old_id: str,
    new_id: str,
    path_value: str | Path | None = None,
) -> tuple[str, Path]:
    if not old_id:
        raise ProjectRegistryError("old project id must not be empty")
    new_id = validate_project_id(new_id, label="new project id")
    with locked_settings(root) as settings:
        projects = _projects(settings)
        if old_id not in projects:
            raise ProjectRegistryError(f"project id is not registered: {old_id!r}")
        if new_id != old_id and new_id in projects:
            raise ProjectRegistryError(
                f"new project id is already registered: {new_id!r}"
            )
        old_entry = projects[old_id]
        if not isinstance(old_entry, dict):
            raise ProjectRegistryError(
                f"registered project {old_id!r} must be an object"
            )
        if path_value is None:
            configured_path = old_entry.get("path")
            if not isinstance(configured_path, str):
                raise ProjectRegistryError(
                    f"registered project {old_id!r} has no string path"
                )
            path = canonical_git_root(configured_path)
        else:
            path = canonical_git_root(path_value)
        _ensure_unique_path(projects, path, excluding=old_id)

        replacement = dict(old_entry)
        replacement["path"] = str(path)
        reordered: dict[str, Any] = {}
        for project_id, entry in projects.items():
            if project_id == old_id:
                reordered[new_id] = replacement
            else:
                reordered[project_id] = entry
        settings["projects"] = reordered
    return ("updated" if old_id == new_id else "renamed"), path


def list_projects(root: Path) -> list[tuple[str, str]]:
    settings = _read_raw_settings(root)
    rows: list[tuple[str, str]] = []
    for project_id, entry in _projects(settings).items():
        path = _entry_path(project_id, entry)
        if path is not None:
            rows.append((project_id, str(path)))
    return sorted(rows)


def cmd_projects(args: argparse.Namespace, root: Path) -> int:
    try:
        if args.projects_action == "add":
            status, path = add_project(root, args.project_id, args.path)
            verb = "already registered" if status == "unchanged" else "registered"
            print(f"{args.project_id}: {verb} at {path}")
            return 0
        if args.projects_action == "rename":
            status, path = rename_project(root, args.old_id, args.new_id, args.path)
            verb = "updated" if status == "updated" else f"renamed from {args.old_id}"
            print(f"{args.new_id}: {verb} at {path}")
            return 0
        if args.projects_action == "list":
            for project_id, path in list_projects(root):
                print(f"{project_id}\t{path}")
            return 0
        raise ProjectRegistryError(f"unknown projects action: {args.projects_action}")
    except ProjectRegistryError as exc:
        print(f"codex-seat projects: {exc}", file=sys.stderr)
        return 2
