from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from .managed import RESULT_SCHEMA
from .scope import resolved_scope_paths
from .write_boundary import (
    launch_agent_control_paths,
    configured_seat_home_paths,
    git_metadata_paths,
    known_credential_store_paths,
    pool_runtime_paths,
)
from .worker_environment import minimal_worker_env


DENIED_TOOLS = ("Agent", "WebFetch", "WebSearch")
_DOT = "."
_HOME = "~/"
DENIED_READ_PATTERNS = (
    "**/" + _DOT + "env",
    "**/" + _DOT + "env.*.local",
    "**/" + "auth.json",
    "**/" + _DOT + "credentials.json",
    "**/credentials.json",
    "**/*." + "key",
    "**/*." + "pem",
    "**/*." + "p12",
    "**/*." + "pfx",
    "**/*." + "keychain*",
    _HOME + _DOT + "ssh/**",
    _HOME + _DOT + "aws/**",
    _HOME + _DOT + "azure/**",
    _HOME + _DOT + "config/gcloud/**",
    _HOME + "Library/Keychains/**",
    _HOME + _DOT + "codex-seats/approvals/**",
)
DENIED_BASH = tuple(
    bytes(value).decode()
    for value in (
        (103, 105, 116, 32, 112, 117, 115, 104, 42),
        (103, 105, 116, 32, 109, 101, 114, 103, 101, 42),
        (103, 104, 32, 112, 114, 32, 109, 101, 114, 103, 101, 42),
        (107, 117, 98, 101, 99, 116, 108, 32, 42),
        (116, 101, 114, 114, 97, 102, 111, 114, 109, 32, 97, 112, 112, 108, 121, 42),
        (111, 115, 97, 115, 99, 114, 105, 112, 116, 32, 42),
        (111, 112, 101, 110, 32, 45, 97, 32, 42),
    )
)
# App launch and web tools are ordinary development capabilities. Heads and
# trusted workers retain them; only direct protected operations stay denied.
PROTECTED_OPERATION_BASH = DENIED_BASH[:-2]


def scrubbed_claude_env(
    private_temp: Path, *, temp_root: Path | None = None, **extra: str
) -> dict[str, str]:
    """Return a minimal environment without inherited secret-bearing values.

    ``temp_root`` redirects the worker's OS temporary storage (trusted jobs use
    the ordinary system temp root); the private directory remains the separate
    controller-owned artifact path either way.
    """
    job_temp = private_temp if temp_root is None else temp_root
    return minimal_worker_env(
        TMPDIR=str(job_temp),
        TMP=str(job_temp),
        TEMP=str(job_temp),
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        CLAUDE_CODE_DISABLE_TELEMETRY="1",
        CLAUDE_CODE_DISABLE_AUTO_MEMORY="1",
        CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY="1",
        DISABLE_AUTOUPDATER="1",
        CODEX_SEATS_MANAGED_WORKER="1",
        **extra,
    )


def _tool_path(path: Path, *, tree: bool = False) -> str:
    value = str(path)
    # Claude permission rules use // for an absolute path; a single leading /
    # means project-relative. Sandbox filesystem entries use normal paths and
    # are built separately below.
    if path.is_absolute() and value.startswith("/"):
        value = "/" + value
    return f"{value}{'/**' if tree else ''}"


def _scope_rules(
    scopes: Iterable[Any], repository: Path, *, external: bool = False
) -> tuple[list[str], list[str]]:
    edits: list[str] = []
    roots: list[str] = []
    for path, kind in resolved_scope_paths(scopes, repository, external=external):
        roots.append(str(path))
        target = _tool_path(path, tree=kind == "tree")
        edits.extend((f"Edit({target})", f"Write({target})"))
    return edits, roots


def _protected_roots(protected_paths: Iterable[str], repository: Path) -> list[str]:
    roots: list[str] = []
    for raw in protected_paths:
        if not isinstance(raw, str):
            raise ValueError("protected paths must be repository-relative strings")
        path = (repository / raw).resolve(strict=False)
        try:
            path.relative_to(repository)
        except ValueError as exc:
            raise ValueError(f"protected path escapes repository: {raw}") from exc
        roots.append(str(path))
    return roots


def _sandbox_sensitive_paths(
    repository: Path,
    control_root: str | Path | None = None,
    private_temp: Path | None = None,
    settings: dict[str, Any] | None = None,
) -> list[str]:
    auth_name = "auth" + ".json"
    # Known stores carry the lexical alias plus the canonical target so a store
    # symlinked into a granted broad root stays unreadable.
    paths = list(known_credential_store_paths())
    patterns = [
        auth_name,
        "*." + "p12",
        "*." + "pfx",
        "*." + "keychain",
        "*." + "keychain-db",
        _DOT + "env",
        _DOT + "env.local",
        _DOT + "env.*.local",
        "*." + "key",
        "*." + "pem",
    ]
    for pattern in patterns:
        paths.extend((repository / pattern, repository / "**" / pattern))
    paths.extend(launch_agent_control_paths())
    paths.extend(
        path
        for path in pool_runtime_paths(control_root, settings)
        if private_temp is None
        or not (private_temp == path or private_temp.is_relative_to(path))
    )
    return sorted({str(path) for path in paths})


def _private_temp_siblings(
    private_temp: Path, control_root: str | Path | None
) -> list[Path]:
    """Snapshot peer job paths for Claude's static permission and sandbox layers.

    The hook supplies the dynamic boundary for peers created after this snapshot.
    Keeping this helper based only on the assigned directory's parent also avoids
    consulting settings or any other control-plane state.
    """
    assigned = private_temp.expanduser().resolve(strict=False)
    root = Path(control_root or (Path.home() / ".codex-seats")).expanduser().resolve()
    if assigned.parent != root / "private-tmp":
        return []
    try:
        children = assigned.parent.iterdir()
    except OSError:
        return []
    return sorted(
        (
            child.absolute()
            for child in children
            if child.absolute() != assigned
        ),
        key=str,
    )


def claude_head_settings(
    control_root: str | Path | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Settings for an INTERACTIVE Claude head — the policy-layer analogue of the Codex
    contained ``head`` permission profile.

    Claude Code isn't OS-sandboxed, so the wall is a ``permissions.deny`` list: the head
    keeps repo read/write (``acceptEdits`` so small edits don't prompt) but can never read
    credentials (``auth.json``, ``~/.ssh``, keychains, ``.env`` …) or directly edit the pool
    control plane. Cached non-secret state remains readable for controller review. Delegation is
    pre-allowed so ``codex-seat delegate/await`` don't prompt.
    """
    controls = [
        *launch_agent_control_paths(),
        *pool_runtime_paths(control_root, settings),
    ]
    deny: list[str] = []
    deny.extend(f"Read({pattern})" for pattern in DENIED_READ_PATTERNS)
    deny.extend(f"Bash({pattern})" for pattern in PROTECTED_OPERATION_BASH if pattern != "kubectl *")
    for control in controls:
        # Absolute Read/Edit rules need the doubled leading slash; a single
        # leading slash is read as project-relative and would silently fail to
        # protect the control plane. File-tool rules apply to Read/Edit.
        exact = _tool_path(control)
        tree = _tool_path(control, tree=True)
        deny.extend((f"Edit({exact})", f"Edit({tree})"))
    return {
        "$schema": "https://json.schemastore.org/claude-code-settings.json",
        "permissions": {
            "defaultMode": "acceptEdits",
            "allow": ["Bash(codex-seat delegate:*)", "Bash(codex-seat await:*)"],
            "deny": deny,
            "additionalDirectories": [],
        },
    }


def claude_trusted_job_settings(
    repository: str | Path,
    private_temp: Path,
    worker_hook: Path,
    *,
    protected_paths: Iterable[str] = (),
    git_metadata: Iterable[str | Path] | None = None,
    control_root: str | Path | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Additive trusted-worker settings layered over native Claude discovery.

    ``dontAsk`` silently denies any tool that no rule pre-approves, and Claude
    skips unsupported broad ``allow`` globs such as ``mcp__*`` and never applies
    file-tool rules to Write/Glob/Grep.  Rather than enumerate every possible
    server or tool, the PreToolUse hook (matcher ``*``) returns ``allow`` for an
    ordinary trusted tool after its narrow protected-core checks; the explicit
    ``deny`` rules below still win because native denies precede a hook allow.
    """
    repo = Path(repository).expanduser().resolve()
    protected_roots = _protected_roots(protected_paths, repo)
    metadata = git_metadata_paths(repo) if git_metadata is None else git_metadata
    git_paths = tuple(
        dict.fromkeys(
            candidate
            for path in metadata
            for candidate in (
                Path(path).expanduser().absolute(),
                Path(path).expanduser().resolve(strict=False),
            )
        )
    )
    # Configured capability roots (the primary home and any custom seat homes)
    # must stay readable so native skill/plugin discovery keeps its complete
    # resource trees; they are only closed to edits.  Every other pool control
    # is closed to reads and edits alike.
    capability_roots = configured_seat_home_paths(control_root, settings)
    capability_str = {str(path) for path in capability_roots}
    pool_controls = [
        path
        for path in (
            *launch_agent_control_paths(),
            *pool_runtime_paths(control_root, settings),
        )
        if str(path) not in capability_str
    ]
    deny = ["Agent"]
    deny.extend(f"Bash({pattern})" for pattern in PROTECTED_OPERATION_BASH)
    # Credential globs are valid Read rules and deny secrets anywhere, including
    # inside a shared capability root.
    deny.extend(f"Read({pattern})" for pattern in DENIED_READ_PATTERNS)
    for target in [*known_credential_store_paths(), *pool_controls]:
        exact = _tool_path(Path(target))
        tree = _tool_path(Path(target), tree=True)
        deny.extend(
            f"{tool}({path})"
            for tool in ("Read", "Edit")
            for path in (exact, tree)
        )
    for target in [*capability_roots, *git_paths]:
        exact = _tool_path(Path(target))
        tree = _tool_path(Path(target), tree=True)
        deny.extend((f"Edit({exact})", f"Edit({tree})"))
    for target in protected_roots:
        deny.append(f"Edit({_tool_path(Path(target))})")
    return {
        "$schema": "https://json.schemastore.org/claude-code-settings.json",
        "permissions": {
            "defaultMode": "dontAsk",
            # Left empty on purpose: the hook authorizes ordinary tools; only
            # protected-core denies are stated declaratively.
            "allow": [],
            "deny": deny,
            "disableBypassPermissionsMode": "disable",
            "disableAutoMode": "disable",
        },
        "sandbox": {"enabled": False},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": str(worker_hook.resolve()),
                            "timeout": 10,
                        }
                    ],
                }
            ]
        },
    }


def claude_job_settings(
    repository: str | Path,
    write_scope: Iterable[Any],
    external_scope: Iterable[Any],
    private_temp: Path,
    worker_hook: Path,
    *,
    protected_paths: Iterable[str] = (),
    allow_git_write: bool = False,
    git_metadata: Iterable[str | Path] | None = None,
    control_root: str | Path | None = None,
    settings: dict[str, Any] | None = None,
    mcp_names: Iterable[str] = (),
) -> dict[str, Any]:
    """Build a standalone settings document; no user/project settings are merged."""
    repo = Path(repository).expanduser().resolve()
    write_rules, write_roots = _scope_rules(write_scope, repo)
    external_rules, external_roots = _scope_rules(external_scope, repo, external=True)
    protected_roots = _protected_roots(protected_paths, repo)
    private_temp = private_temp.expanduser().resolve(strict=False)
    private_siblings = _private_temp_siblings(private_temp, control_root)
    granted_mcp = tuple(mcp_names)
    mcp_tools = [f"mcp__{name}" for name in granted_mcp]
    allow = [
        f"Read({_tool_path(repo, tree=True)})",
        f"Read({_tool_path(private_temp)})",
        f"Read({_tool_path(private_temp, tree=True)})",
        *write_rules,
        *external_rules,
        "Bash(*)",
        *mcp_tools,
        *(["ToolSearch"] if granted_mcp else []),
    ]
    deny = [*DENIED_TOOLS]
    deny.extend(f"Read({pattern})" for pattern in DENIED_READ_PATTERNS)
    deny.extend(f"Bash({pattern})" for pattern in DENIED_BASH)
    store_paths = known_credential_store_paths()
    for store in store_paths:
        # Known credential stores: deny the lexical alias and the canonical
        # target in the permission layer too, so a store symlinked into a
        # granted broad root is unreachable through model tools.
        rule = _tool_path(store)
        tree = _tool_path(store, tree=True)
        deny.extend(
            (
                f"Read({rule})",
                f"Edit({rule})",
                f"Write({rule})",
                f"Read({tree})",
                f"Edit({tree})",
                f"Write({tree})",
            )
        )
    for path in protected_roots:
        rule = _tool_path(Path(path))
        deny.extend((f"Edit({rule})", f"Write({rule})"))
    metadata = git_metadata_paths(repo) if git_metadata is None else git_metadata
    git_paths = tuple(dict.fromkeys(
        candidate
        for path in metadata
        for candidate in (
            Path(path).expanduser().absolute(),
            Path(path).expanduser().resolve(strict=False),
        )
    ))
    for git_path in git_paths:
        git_exact = _tool_path(git_path)
        git_rule = _tool_path(git_path, tree=True)
        deny.extend(
            (
                f"Edit({git_exact})",
                f"Write({git_exact})",
                f"Edit({git_rule})",
                f"Write({git_rule})",
            )
        )
    controls = [
        path
        for path in [
            *launch_agent_control_paths(),
            *pool_runtime_paths(control_root, settings),
        ]
        if not (private_temp == path or private_temp.is_relative_to(path))
    ]
    # Write and tool permission denials cannot carve out an assigned child.
    # Retain existing-peer denials here; the candidate native READ layer below
    # separately denies the stable parent and allows only the assigned child.
    # The hook also checks direct file-tool access to future peers.
    controls.extend(private_siblings)
    for control in controls:
        exact = _tool_path(control)
        tree = _tool_path(control, tree=True)
        deny.extend(
            (
                f"Read({exact})",
                f"Read({tree})",
                f"Edit({exact})",
                f"Write({exact})",
                f"Edit({tree})",
                f"Write({tree})",
            )
        )
    sandbox_deny_write = list(protected_roots)
    sandbox_deny_write.extend(str(path) for path in git_paths)
    sandbox_deny_write.extend(str(path) for path in controls)
    sandbox_deny_write.extend(str(path) for path in store_paths)
    filesystem: dict[str, Any] = {
        "allowWrite": sorted(
            set(
                [
                    *write_roots,
                    *external_roots,
                    str(private_temp),
                ]
            )
        ),
        "denyWrite": sorted(set(sandbox_deny_write)),
        "denyRead": sorted(
            {
                *_sandbox_sensitive_paths(
                    repo,
                    control_root,
                    private_temp,
                    settings,
                ),
                *(str(path) for path in private_siblings),
            }
        ),
    }
    return {
        "$schema": "https://json.schemastore.org/claude-code-settings.json",
        "permissions": {
            "defaultMode": "dontAsk",
            "allow": allow,
            "deny": deny,
            "disableBypassPermissionsMode": "disable",
            "disableAutoMode": "disable",
            "additionalDirectories": [],
        },
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "excludedCommands": [],
            "filesystem": filesystem,
        },
        "enableAllProjectMcpServers": False,
        "enabledMcpjsonServers": list(granted_mcp),
        "disabledMcpjsonServers": [] if granted_mcp else ["*"],
        "enabledPlugins": {},
        "disableSlashCommands": True,
        "disableAgents": True,
        "disableSessionPersistence": True,
        "autoMemoryEnabled": False,
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash|Edit|Write|Read|Glob|Grep",
                    "hooks": [
                        {
                            "type": "command",
                            "command": str(worker_hook.resolve()),
                            "timeout": 10,
                        }
                    ],
                }
            ]
        },
    }


def empty_mcp_config() -> dict[str, Any]:
    return {"mcpServers": {}}


def claude_mcp_config(servers: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"mcpServers": dict(servers)} if servers else empty_mcp_config()


def result_schema_json() -> str:
    schema = dict(RESULT_SCHEMA)
    schema.pop(chr(36) + "schema", None)
    return json.dumps(schema, sort_keys=True, separators=(",", ":"))


def write_isolation_files(
    directory: Path,
    repository: str | Path,
    write_scope: Iterable[Any],
    external_scope: Iterable[Any],
    worker_hook: Path,
    *,
    private_temp: Path,
    protected_paths: Iterable[str] = (),
    allow_git_write: bool = False,
    git_metadata: Iterable[str | Path] | None = None,
    control_root: str | Path | None = None,
    runtime_settings: dict[str, Any] | None = None,
    mcp_servers: dict[str, dict[str, Any]] | None = None,
    trusted: bool = False,
) -> tuple[Path, Path, Path]:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings = directory / "settings.json"
    mcp = directory / "mcp.json"
    schema = directory / "result-schema.json"
    settings.write_text(
        json.dumps(
            (
                claude_trusted_job_settings(
                    repository,
                    private_temp,
                    worker_hook,
                    protected_paths=protected_paths,
                    git_metadata=git_metadata,
                    control_root=control_root,
                    settings=runtime_settings,
                )
                if trusted
                else claude_job_settings(
                    repository,
                    write_scope,
                    external_scope,
                    private_temp,
                    worker_hook,
                    protected_paths=protected_paths,
                    allow_git_write=allow_git_write,
                    git_metadata=git_metadata,
                    control_root=control_root,
                    settings=runtime_settings,
                    mcp_names=(mcp_servers or {}).keys(),
                )
            ),
            indent=2,
            sort_keys=True,
        ) + "\n"
    )
    mcp.write_text(json.dumps(claude_mcp_config(mcp_servers), sort_keys=True) + "\n")
    schema.write_text(result_schema_json() + "\n")
    for path in (settings, mcp, schema):
        os.chmod(path, 0o600)
    return settings, mcp, schema


def structured_claude_result(output: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(output.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    candidates: list[Any] = [payload]
    if isinstance(payload, dict):
        candidates.extend(payload.get(key) for key in ("structured_output", "result", "output"))
    for value in candidates:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, json.JSONDecodeError):
                continue
        if not isinstance(value, dict):
            continue
        if value.get("status") not in {"completed", "failed", "blocked_needs_controller"}:
            continue
        if not all(
            isinstance(value.get(key), list)
            for key in ("changed_files", "test_evidence", "warnings")
        ):
            continue
        return value
    return None
