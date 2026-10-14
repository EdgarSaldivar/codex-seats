from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable


SCOPE_TYPES = {"file", "tree"}


def _wildcard(value: str) -> bool:
    return any(marker in value for marker in "*?[")


def _canonical_repository(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _scope_absolute(entry: dict[str, str], repository: Path, *, external: bool = False) -> Path:
    value = Path(os.path.expanduser(entry["path"]))
    if external:
        if not value.is_absolute():
            raise ValueError("external scope paths must be absolute")
    elif value.is_absolute():
        raise ValueError("repository write_scope paths must be relative")
    candidate = value if value.is_absolute() else repository / value
    resolved = candidate.resolve(strict=False)
    if not external:
        try:
            resolved.relative_to(repository)
        except ValueError as exc:
            raise ValueError("scope escapes the canonical repository") from exc
    return resolved


def normalize_scope_entries(
    entries: Any,
    repository: str | Path,
    *,
    legacy: bool = False,
    external: bool = False,
) -> tuple[list[dict[str, str]], list[str]]:
    """Validate scope objects and return stable path/type objects."""
    repo = _canonical_repository(repository)
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        return [], ["scope must be an array"]
    normalized: list[dict[str, str]] = []
    errors: list[str] = []
    seen: dict[Path, str] = {}
    for index, raw in enumerate(entries):
        entry: dict[str, str] | None = None
        if isinstance(raw, dict):
            if set(raw) != {"path", "type"}:
                errors.append(f"scope[{index}] must contain exactly path and type")
                continue
            if not isinstance(raw.get("path"), str) or raw.get("type") not in SCOPE_TYPES:
                errors.append(f"scope[{index}] needs a string path and type file|tree")
                continue
            entry = {"path": raw["path"], "type": raw["type"]}
        elif legacy and isinstance(raw, str):
            if raw.endswith("/**") and not _wildcard(raw[:-3]):
                entry = {"path": raw[:-3] or ".", "type": "tree"}
            elif not _wildcard(raw):
                candidate = Path(raw if Path(raw).is_absolute() else repo / raw)
                entry = {"path": raw, "type": "tree" if candidate.is_dir() else "file"}
            else:
                errors.append(f"scope[{index}] legacy wildcard is not a terminal /** tree")
                continue
        else:
            errors.append(f"scope[{index}] must be a path/type object")
            continue
        path = entry["path"]
        if not path or "\x00" in path or _wildcard(path):
            errors.append(f"scope[{index}] path is empty or contains a wildcard")
            continue
        if entry["type"] == "file" and path in {".", "./"}:
            errors.append(f"scope[{index}] whole repository requires type=tree")
            continue
        if any(part in {"..", ""} for part in Path(path).parts):
            errors.append(f"scope[{index}] contains traversal")
            continue
        try:
            absolute = _scope_absolute(entry, repo, external=external)
        except ValueError as exc:
            errors.append(f"scope[{index}] {exc}")
            continue
        previous = seen.get(absolute)
        if previous and previous != entry["type"]:
            errors.append(f"scope[{index}] declares {absolute} as both file and tree")
            continue
        if previous:
            errors.append(f"scope[{index}] duplicates {absolute}")
            continue
        for existing_path, existing_type in seen.items():
            if existing_type == "tree" and _is_within(absolute, existing_path):
                errors.append(f"scope[{index}] overlaps enclosing tree {existing_path}")
                break
            if entry["type"] == "tree" and _is_within(existing_path, absolute):
                errors.append(f"scope[{index}] overlaps enclosed path {existing_path}")
                break
        else:
            seen[absolute] = entry["type"]
            stable_path = str(absolute) if external else (
                "." if absolute == repo else absolute.relative_to(repo).as_posix()
            )
            normalized.append({"path": stable_path, "type": entry["type"]})
    return normalized, errors


def effective_repository_write_scope(
    declared: Iterable[Any],
    *,
    strict_scope: bool = False,
    parallel_safe: bool = False,
) -> list[Any]:
    """Keep metadata exact while granting balanced serial writers the repo tree."""
    scopes = list(declared)
    if scopes and not strict_scope and not parallel_safe:
        return [{"path": ".", "type": "tree"}]
    return scopes


def resolved_scope_paths(
    entries: Iterable[Any],
    repository: str | Path,
    *,
    legacy: bool = True,
    external: bool = False,
) -> list[tuple[Path, str]]:
    values = list(entries)
    normalized, errors = normalize_scope_entries(
        values, repository, legacy=legacy, external=external
    )
    if errors:
        raise ValueError("; ".join(errors))
    repo = _canonical_repository(repository)
    return [
        (_scope_absolute(entry, repo, external=external), entry["type"])
        for entry in normalized
    ]


def scope_allows(
    target: str | Path,
    entries: Iterable[Any],
    repository: str | Path,
    *,
    legacy: bool = True,
    external: bool = False,
) -> bool:
    repo = _canonical_repository(repository)
    value = Path(target).expanduser()
    absolute = (value if value.is_absolute() else repo / value).resolve(strict=False)
    try:
        scopes = resolved_scope_paths(entries, repo, legacy=legacy, external=external)
    except ValueError:
        return False
    return any(
        absolute == scope or (scope_type == "tree" and _is_within(absolute, scope))
        for scope, scope_type in scopes
    )


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return path != parent
    except ValueError:
        return False


def _toml_key(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def worker_permission_profile(
    name: str,
    repository: str | Path,
    write_scope: Iterable[Any],
    external_scope: Iterable[Any],
    private_temp: str | Path,
    *,
    protected_paths: Iterable[str] = (),
    generic_temp: str | Path | None = None,
    home: str | Path | None = None,
    allow_git_write: bool = False,
    git_metadata: Iterable[str | Path] | None = None,
    control_root: str | Path | None = None,
    settings: dict[str, Any] | None = None,
) -> str:
    repo = _canonical_repository(repository)
    private = Path(private_temp).resolve()
    user_home = Path(home or Path.home()).resolve()
    writes = resolved_scope_paths(write_scope, repo)
    writes += resolved_scope_paths(external_scope, repo, external=True)
    dot_environment = "." + "env"
    # Generic temp and the /tmp aliases stay denied: historical dispatch
    # worktrees lived below generic temp, and workers write only through their
    # declared scopes and private scratch.
    generic = str(Path(generic_temp or tempfile.gettempdir()).resolve())
    from .write_boundary import (
        launch_agent_control_paths,
        git_metadata_paths,
        known_credential_store_paths,
        pool_runtime_paths,
    )

    # Known stores deny the lexical alias (unlink/rename) and the canonical
    # target (actual reads/writes) so a store symlinked into a broad writable
    # root stays protected; wildcard patterns stay lexical.
    credential_denials: dict[str, str | int] = {
        str(store): "deny" for store in known_credential_store_paths(user_home)
    }
    credential_denials |= {
        str(user_home / "**" / "*.p12"): "deny",
        str(user_home / "**" / "*.pfx"): "deny",
        str(user_home / "**" / "*.keychain"): "deny",
        str(user_home / "**" / "*.keychain-db"): "deny",
    }
    filesystem: dict[str, str | int] = {
        "glob_scan_max_depth": 32,
        ":root": "read",
        ":slash_tmp": "deny",
        "/private/tmp": "deny",
        generic: "deny",
        # Keep the assigned repository readable even when a read-only task has
        # no write scope to add this child rule.
        str(repo): "read",
        str(private): "write",
        **credential_denials,
    }
    credential_denials |= {
        str(user_home / "**" / dot_environment): "deny",
        str(user_home / "**" / f"{dot_environment}.*.local"): "deny",
        str(user_home / "**" / f"{dot_environment}.local"): "deny",
        str(user_home / "**" / "*.key"): "deny",
        str(user_home / "**" / "*.pem"): "deny",
    }
    for key, access in credential_denials.items():
        filesystem[key] = access
    for path, _kind in writes:
        filesystem[str(path)] = "write"

    for relative in protected_paths:
        protected = (repo / relative).resolve(strict=False)
        try:
            protected.relative_to(repo)
        except ValueError as exc:
            raise ValueError(f"protected path escapes repository: {relative}") from exc
        filesystem[str(protected)] = "read"
    metadata = git_metadata_paths(repo) if git_metadata is None else git_metadata
    for git_path in metadata:
        # A checkout-wide write grant and the legacy allow_git_write request
        # must both lose to the more-specific Git metadata protection.
        lexical = Path(git_path).expanduser().absolute()
        filesystem[str(lexical)] = "read"
        filesystem[str(lexical.resolve(strict=False))] = "read"
    for control_path in launch_agent_control_paths():
        filesystem[str(control_path)] = "deny"
    for runtime_path in pool_runtime_paths(control_root, settings):
        filesystem[str(runtime_path)] = "deny"
    filesystem.pop(str(private), None)
    filesystem[str(private)] = "write"

    # Repository scopes can sit outside HOME and can grant the whole tree. Keep
    # credential material denied with repository-specific rules emitted after
    # every grant (including protected-path reads). Reinsert duplicate exact
    # keys so their order is also later than all grants.
    repository_secret_patterns = [
        "*.p12",
        "*.pfx",
        "*.keychain",
        "*.keychain-db",
        dot_environment,
        f"{dot_environment}.local",
        f"{dot_environment}.*.local",
        "*.key",
        "*.pem",
    ]
    for pattern in repository_secret_patterns:
        for secret_path in (repo / pattern, repo / "**" / pattern):
            key = str(secret_path)
            filesystem.pop(key, None)
            filesystem[key] = "deny"
    lines = [
        f"default_permissions = {_toml_key(name)}",
        'approval_policy = "never"',
        "",
        f"[permissions.{name}]",
        'description = "Ephemeral least-privilege managed worker profile"',
        "",
        f"[permissions.{name}.filesystem]",
    ]
    for path, access in filesystem.items():
        encoded = access if isinstance(access, int) else _toml_key(access)
        lines.append(f"{_toml_key(path)} = {encoded}")
    lines.extend(
        [
            "",
            f"[permissions.{name}.network]",
            "enabled = true",
            "allow_local_binding = true",
            "",
            f"[permissions.{name}.network.domains]",
            '"*" = "allow"',
            '"localhost" = "allow"',
            '"127.0.0.1" = "allow"',
            '"::1" = "allow"',
            "",
        ]
    )
    return "\n".join(lines)


def _toml_inline(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ",".join(_toml_inline(item) for item in value) + "]"
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(
                f"{_toml_key(str(key))}={_toml_inline(item)}"
                for key, item in value.items()
            )
            + "}"
        )
    raise TypeError(f"unsupported TOML value: {type(value).__name__}")


def worker_profile_overrides(profile_text: str, name: str) -> list[str]:
    import tomllib

    parsed = tomllib.loads(profile_text)
    return [
        f"default_permissions={_toml_inline(name)}",
        f"permissions.{name}={_toml_inline(parsed['permissions'][name])}",
    ]
