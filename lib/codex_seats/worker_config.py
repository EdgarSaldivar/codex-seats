from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterable

from . import bounded_process
from .seat_registry import registered_seat_ids
from .state import runtime_seat_home
from .worker_environment import minimal_worker_env


def _sync_config_file(
    source: Path,
    target: Path,
    payload: bytes | None = None,
) -> bool:
    payload = source.read_bytes() if payload is None else payload
    try:
        if target.read_bytes() == payload:
            return False
    except OSError:
        pass
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, target)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    return True


def _config_segment(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else json.dumps(value)


def _worker_isolation_overrides(home: Path) -> list[str]:
    """Disable every configured MCP/plugin plus native fanout for a worker."""
    import tomllib

    try:
        config = tomllib.loads((home / "config.toml").read_text())
    except (OSError, ValueError):
        config = {}
    overrides = [
        "features.multi_agent=false",
        "features.guardian_approval=false",
        "features.enable_fanout=false",
        "features.apps=false",
        "features.plugins=false",
    ]
    # On codex >= 0.144.5 a
    # `-c mcp_servers.X.enabled=false` override for a server the config does NOT
    # define *creates* an entry with no transport, and config load aborts with
    # "invalid transport". The loop below disables every server that actually
    # exists.
    servers = config.get("mcp_servers", {})
    if isinstance(servers, dict):
        for name in servers:
            overrides.append(f"mcp_servers.{_config_segment(str(name))}.enabled=false")
            overrides.append(f"mcp_servers.{_config_segment(str(name))}.required=false")
    plugins = config.get("plugins", {})
    if isinstance(plugins, dict):
        for name in plugins:
            overrides.append(f"plugins.{_config_segment(str(name))}.enabled=false")
    return overrides


def trusted_worker_overrides() -> list[str]:
    """Keep worker-only authority limits without suppressing development tools."""
    return [
        "features.multi_agent=false",
        "features.guardian_approval=false",
        "features.enable_fanout=false",
    ]


def validate_trusted_codex_config(home: Path) -> None:
    """Refuse legacy sandbox keys that would compete with the built-in mode."""
    import tomllib

    try:
        config = tomllib.loads((home / "config.toml").read_text())
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot load trusted Codex configuration: {exc}") from exc
    legacy = sorted(
        key for key in ("sandbox_mode", "sandbox_workspace_write") if key in config
    )
    if legacy:
        raise ValueError(
            "trusted Codex configuration contains competing legacy sandbox keys: "
            + ", ".join(legacy)
        )


REPO_CONFIG_DIRNAME = ".seats"
WORKER_MCP_CATALOG = Path(REPO_CONFIG_DIRNAME) / "worker-mcp.json"
MAX_WORKER_MCP_CATALOG_BYTES = 64 * 1024
_MCP_NAME_RE = re.compile(r"\A[A-Za-z0-9_.-]{1,128}\Z")
_SECRET_ENV_MARKERS = (
    "API_KEY",
    "AUTH",
    "CREDENTIAL",
    "PASSWORD",
    "SECRET",
    "TOKEN",
)


def _read_worker_mcp_catalog(repository: str | Path) -> tuple[Path, bytes]:
    """Read one catalog through repository-anchored, no-follow descriptors.

    Both directory and file descriptors pin the objects being checked, so a symlinked
    config directory or concurrent rename cannot redirect the subsequent read. The read is
    capped independently of the initial stat and refuses in-place mutation.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory_flag:
        raise ValueError("this platform cannot securely open a worker MCP catalog")
    repository_path = Path(repository).expanduser().resolve()
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | nofollow | directory_flag
    # O_NONBLOCK prevents a FIFO/device planted at the catalog name from wedging the
    # single dispatch runner before fstat can reject its non-regular type.
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | nofollow
    try:
        repository_fd = os.open(repository_path, directory_flags)
    except OSError as exc:
        raise ValueError(
            f"worker MCP repository is unavailable: {repository_path}"
        ) from exc
    catalog_path = repository_path / WORKER_MCP_CATALOG
    unavailable = f"worker MCP catalog is unavailable: {catalog_path}"
    try:
        try:
            config_fd = os.open(
                REPO_CONFIG_DIRNAME, directory_flags, dir_fd=repository_fd
            )
        except FileNotFoundError as exc:
            raise ValueError(unavailable) from exc
        except OSError as exc:
            raise ValueError(
                "worker MCP config directory must be a regular non-symlink directory: "
                f"{repository_path / REPO_CONFIG_DIRNAME}"
            ) from exc
        try:
            try:
                catalog_fd = os.open(
                    WORKER_MCP_CATALOG.name,
                    file_flags,
                    dir_fd=config_fd,
                )
            except FileNotFoundError as exc:
                raise ValueError(unavailable) from exc
            except OSError as exc:
                raise ValueError(
                    "worker MCP catalog must be a regular non-symlink file: "
                    f"{catalog_path}"
                ) from exc
            try:
                before = os.fstat(catalog_fd)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise ValueError(
                        "worker MCP catalog must be a singly linked regular file"
                    )
                if before.st_uid != os.geteuid() or before.st_mode & (
                    stat.S_IWGRP | stat.S_IWOTH
                ):
                    raise ValueError(
                        "worker MCP catalog must be owner-controlled and not group/world-writable"
                    )
                if before.st_size > MAX_WORKER_MCP_CATALOG_BYTES:
                    raise ValueError("worker MCP catalog exceeds the size limit")
                chunks: list[bytes] = []
                total = 0
                while total <= MAX_WORKER_MCP_CATALOG_BYTES:
                    chunk = os.read(
                        catalog_fd,
                        min(16 * 1024, MAX_WORKER_MCP_CATALOG_BYTES + 1 - total),
                    )
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                if total > MAX_WORKER_MCP_CATALOG_BYTES:
                    raise ValueError("worker MCP catalog exceeds the size limit")
                after = os.fstat(catalog_fd)
                stable_fields = (
                    "st_dev",
                    "st_ino",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                )
                if any(
                    getattr(before, field) != getattr(after, field)
                    for field in stable_fields
                ) or total != before.st_size:
                    raise ValueError("worker MCP catalog changed while it was read")
                return catalog_path, b"".join(chunks)
            finally:
                os.close(catalog_fd)
        finally:
            os.close(config_fd)
    finally:
        os.close(repository_fd)


def validated_worker_mcp_specs(
    names: Iterable[str], repository: str | Path
) -> dict[str, dict[str, Any]]:
    """Return strict catalog specs for an explicit worker grant.

    The dispatch frame carries names only. Commands and environment stay in the
    controller-authored repository catalog, and every worker revalidates the committed
    catalog in its isolated worktree before a provider starts.
    """
    requested = tuple(names)
    if not requested:
        return {}
    if len(requested) != len(set(requested)):
        raise ValueError("worker MCP grants must not contain duplicates")
    if any(not isinstance(name, str) or not _MCP_NAME_RE.fullmatch(name) for name in requested):
        raise ValueError("worker MCP names must use 1-128 letters, digits, dot, dash, or underscore")

    try:
        path, payload = _read_worker_mcp_catalog(repository)
        data = json.loads(payload.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError("worker MCP catalog must be UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"worker MCP catalog is malformed: {path}") from exc
    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise ValueError("worker MCP catalog must contain a servers object")
    unknown = sorted(set(requested) - set(servers))
    if unknown:
        raise ValueError("worker MCP grant is not in the repository catalog: " + ", ".join(unknown))

    result: dict[str, dict[str, Any]] = {}
    for name in requested:
        raw = servers[name]
        if not isinstance(raw, dict) or set(raw) - {"command", "args", "env"}:
            raise ValueError(f"worker MCP server {name!r} has an invalid specification")
        command = raw.get("command")
        args = raw.get("args", [])
        env = raw.get("env", {})
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"worker MCP server {name!r} needs a command")
        if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
            raise ValueError(f"worker MCP server {name!r} args must be strings")
        if not isinstance(env, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in env.items()
        ):
            raise ValueError(f"worker MCP server {name!r} env must map strings to strings")
        secret_keys = sorted(
            key
            for key in env
            if any(marker in key.upper() for marker in _SECRET_ENV_MARKERS)
        )
        if secret_keys:
            raise ValueError(
                f"worker MCP server {name!r} may not declare secret-bearing env keys: "
                + ", ".join(secret_keys)
            )
        result[name] = {"command": command, "args": list(args), "env": dict(env)}
    return result


def resolved_worker_mcp_specs(
    names: Iterable[str], repository: str | Path
) -> dict[str, dict[str, Any]]:
    """Validate grants and resolve repo-local command/argument paths."""
    catalog = validated_worker_mcp_specs(names, repository)
    repo = Path(repository).resolve()

    def _resolve(value: str) -> str:
        candidate = repo / value
        return str(candidate) if not os.path.isabs(value) and candidate.exists() else value

    return {
        name: {
            "command": _resolve(str(spec["command"])),
            "args": [_resolve(arg) for arg in spec["args"]],
            "env": dict(spec["env"]),
        }
        for name, spec in catalog.items()
    }


def worker_mcp_overrides(names: Iterable[str], repository: str | Path) -> list[str]:
    """`-c` overrides that ENABLE the allowlisted MCP servers for one worker.

    Each granted server is enabled with ``default_tools_approval_mode="approve"``
    (so tool calls don't stall on approval in a headless run). Unknown, missing, or
    malformed grants fail closed before the provider starts. Relative
    command/args paths resolve against the repository root (the worktree carries
    a full checkout plus the linked venv)."""
    catalog = resolved_worker_mcp_specs(names, repository)

    overrides: list[str] = []
    for name, spec in catalog.items():
        seg = _config_segment(str(name))
        overrides.append(f"mcp_servers.{seg}.command={json.dumps(str(spec['command']))}")
        args = spec.get("args", [])
        if isinstance(args, list):
            overrides.append(f"mcp_servers.{seg}.args={json.dumps(args, separators=(',', ':'))}")
        env = spec.get("env")
        if isinstance(env, dict):
            for key, val in env.items():
                overrides.append(f"mcp_servers.{seg}.env.{_config_segment(str(key))}={json.dumps(val)}")
        overrides.append(f"mcp_servers.{seg}.enabled=true")
        overrides.append(f'mcp_servers.{seg}.default_tools_approval_mode="approve"')
    return overrides


def expected_config_payload(source: Path, source_home: Path, target_home: Path) -> bytes:
    payload = source.read_bytes()
    if source.name == "config.toml" or source.name.endswith(".config.toml"):
        payload = persisted_config_payload(payload)
    if source.name == "config.toml":
        payload = payload.replace(
            f'{source_home}/config.toml:'.encode(),
            f'{target_home}/config.toml:'.encode(),
        )
    return payload


def static_config_payload(payload: bytes) -> bytes:
    """Remove only the path-specific hook trust cache from config bytes."""
    lines = payload.splitlines(keepends=True)
    kept: list[bytes] = []
    in_hook_state = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(b"["):
            in_hook_state = bool(
                re.match(rb"^\[\s*hooks\.state(?:\.|\])", stripped)
            )
        if not in_hook_state:
            kept.append(line)
    return b"".join(kept)


def persisted_config_payload(payload: bytes) -> bytes:
    """Persist controller defaults, including the narrow local MCP approval."""
    import tomllib

    config = tomllib.loads(payload.decode())
    profiles = config.get("permissions")
    if not isinstance(profiles, dict) or not profiles:
        return payload
    default = config.get("default_permissions")
    if default is not None:
        builtins = {":read-only", ":workspace", ":danger-full-access"}
        if not isinstance(default, str) or (
            default not in profiles and default not in builtins
        ):
            raise ValueError("default_permissions must name a defined permissions profile")
        return payload
    if "controller" in profiles:
        default = "controller"
    elif len(profiles) == 1:
        default = str(next(iter(profiles)))
    else:
        raise ValueError("permissions configs with multiple profiles require default_permissions")
    return f'default_permissions = {json.dumps(default)}\n'.encode() + payload


def sync_codex_config(root: Path, settings: dict[str, Any], seat_id: str) -> bool:
    """Atomically mirror primary non-auth config, instructions, and profiles."""
    operational = set(registered_seat_ids(settings, lifecycles=("active", "draining")))
    registered = set(registered_seat_ids(settings))
    if seat_id not in registered:
        raise ValueError(f"seat {seat_id!r} is not registered")
    if seat_id not in operational:
        lifecycle = settings["pool"]["seats"][seat_id]["lifecycle"]
        raise ValueError(
            f"seat {seat_id!r} is not operational (lifecycle={lifecycle})"
        )
    primary = settings["pool"]["primary"]
    if primary is None:
        raise ValueError("cannot synchronize Codex config without an active primary")
    source_home = runtime_seat_home(root, primary, settings)
    target_home = runtime_seat_home(root, seat_id, settings)
    changed = False
    primary_config = source_home / "config.toml"
    if primary_config.exists():
        source_payload = primary_config.read_bytes()
        migrated_payload = persisted_config_payload(source_payload)
        if migrated_payload != source_payload:
            changed = _sync_config_file(
                primary_config, primary_config, migrated_payload
            ) or changed
    if source_home.resolve() == target_home.resolve():
        return changed
    sources = [
        source
        for source in (
            source_home / "config.toml",
            source_home / "AGENTS.md",
            *sorted(source_home.glob("*.config.toml")),
        )
        if source.exists()
    ]
    for source in sources:
        # Hook trust is keyed by the config file that declared the hook.
        # Mirroring ~/.codex verbatim makes every isolated worker see the same
        # known Pixtuoid hook as "new" on each launch.
        payload = expected_config_payload(source, source_home, target_home)
        changed = _sync_config_file(
            source,
            target_home / source.name,
            payload,
        ) or changed
    return changed


def sync_codex_capability_roots(
    root: Path, settings: dict[str, Any], seat_id: str
) -> list[str]:
    """Share complete non-secret skill/plugin trees with an isolated seat.

    A directory symlink preserves scripts, assets and references without copying
    provider homes. Existing seat-local roots are respected as explicit local
    configuration and reported when they differ from the primary root.
    """
    primary = settings["pool"]["primary"]
    if primary is None:
        raise ValueError("cannot synchronize Codex capabilities without an active primary")
    source_home = runtime_seat_home(root, primary, settings)
    target_home = runtime_seat_home(root, seat_id, settings)
    if source_home.resolve() == target_home.resolve():
        return []
    warnings: list[str] = []
    for name in ("skills", "plugins"):
        source = source_home / name
        target = target_home / name
        if not source.is_dir():
            continue
        target_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not target.exists() and not target.is_symlink():
            target.symlink_to(source, target_is_directory=True)
            continue
        if target.resolve(strict=False) != source.resolve(strict=False):
            warnings.append(
                f"Codex {name} remain seat-local at {target}; native discovery may "
                f"differ from the primary capability root {source}"
            )
    return warnings


# ---------------------------------------------------------------------------
# Per-job portability adapter for trusted Claude workers.
#
# The primary Codex configuration already carries the user's usable development
# skills and configured MCP servers. A trusted Claude launch keeps native
# discovery, but Claude never looks at the Codex home. These helpers forward
# just the portable pieces for one job — skill resource trees by directory link
# under a task-owned ``.claude/skills`` root, and standard MCP transports
# into the additive per-job MCP config — without registering anything, copying
# whole homes, or touching the user/seat homes. Anything Claude's format cannot
# represent faithfully (provider-specific options, credential-bearing env) is
# omitted with a precise warning instead of being silently weakened.
# ---------------------------------------------------------------------------

# stdio connection fields Claude's per-job MCP config represents faithfully.
_CLAUDE_MCP_FIELDS = frozenset({"command", "args", "env", "enabled", "url"})


def native_claude_mcp_names(binary: str, repository: Path) -> set[str] | None:
    """Read names through Claude's CLI without opening its credential-bearing home file."""
    try:
        result = bounded_process.run(
            [binary, "mcp", "list"], cwd=repository, timeout=20,
            env=minimal_worker_env(MCP_TIMEOUT="3000"),
        )
    except bounded_process.ProcessError:
        return None
    if result.returncode:
        return None
    output = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout_text(errors="replace"))
    # CLI lines are NAME: transport/status. Never expose addresses or config values.
    names = {match.group(1).strip() for match in re.finditer(r"^([^:\r\n]+):\s", output, re.M)}
    return names if names or "No MCP servers configured" in output else None


def _primary_codex_home(root: Path, settings: dict[str, Any]) -> Path | None:
    """Resolve the primary Codex home without assuming an operational pool."""
    primary = settings.get("pool", {}).get("primary")
    if not primary:
        return None
    try:
        return runtime_seat_home(root, primary, settings)
    except (ValueError, OSError):
        return None


def _primary_codex_config(home: Path | None) -> dict[str, Any]:
    if home is None:
        return {}
    import tomllib

    try:
        config = tomllib.loads((home / "config.toml").read_text())
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def portable_primary_mcp_servers(
    root: Path, settings: dict[str, Any], repository: str | Path
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Forward enabled standard-transport MCP servers from the primary config.

    Explicit disables stay disabled. Unsupported fields omit the entire
    connection, so tool restrictions or environment dependencies cannot be lost.
    Plain stdio and HTTP transports are portable; project-relative stdio paths
    bind to the disposable worktree. Returns ``(servers, warnings)``.
    """
    config = _primary_codex_config(_primary_codex_home(root, settings))
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        return {}, []
    repo = Path(repository).resolve()

    def _bind(value: str) -> str:
        candidate = repo / value
        return str(candidate) if not os.path.isabs(value) and candidate.exists() else value

    forwarded: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for name, raw in servers.items():
        if not isinstance(name, str) or not _MCP_NAME_RE.fullmatch(name):
            continue
        if not isinstance(raw, dict):
            continue
        # Preserve explicit disables (e.g. a disabled desktop connector).
        if raw.get("enabled") is False:
            continue
        extra = sorted(key for key in raw if key not in _CLAUDE_MCP_FIELDS)
        if extra:
            warnings.append(
                f"portable MCP {name!r} cannot faithfully forward fields "
                f"{', '.join(extra)} (including any tool/environment restrictions); omitted"
            )
            continue
        url = raw.get("url")
        if isinstance(url, str) and url.startswith(("https://", "http://")) and not any(
            key in raw for key in ("command", "args", "env")
        ):
            forwarded[name] = {"type": "http", "url": url}
            continue
        command = raw.get("command")
        if raw.get("url") is not None or not isinstance(command, str) or not command.strip():
            warnings.append(
                f"portable MCP {name!r} has an unsupported or ambiguous transport; omitted"
            )
            continue
        env = raw.get("env", {})
        if not isinstance(env, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in env.items()
        ):
            warnings.append(f"portable MCP {name!r} has invalid environment configuration; omitted")
            continue
        secret = sorted(
            key
            for key in env
            if any(marker in key.upper() for marker in _SECRET_ENV_MARKERS)
        )
        if secret:
            warnings.append(
                f"portable MCP {name!r} declares credential-bearing env "
                f"({', '.join(secret)}); omitted to avoid exporting credentials"
            )
            continue
        args = raw.get("args")
        args = [] if args is None else args
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            warnings.append(f"portable MCP {name!r} has invalid arguments; omitted")
            continue
        bound_command = _bind(command)
        if os.path.sep not in command and shutil.which(command) is None and not (
            repo / command
        ).exists():
            warnings.append(
                f"portable MCP {name!r} command {command!r} is not on PATH; the "
                "connection may fail until the dependency is installed"
            )
        forwarded[name] = {
            "command": bound_command,
            "args": [_bind(arg) for arg in args],
            "env": dict(env),
        }
    return forwarded, warnings


def _native_claude_skill_names(repository: str | Path) -> set[str]:
    """Return the names of skills Claude already discovers natively.

    Only directory listings are read (never ``~/.claude.json`` or credentials),
    so a portable skill that collides with a native one can be skipped and the
    native skill preserved.
    """
    names: set[str] = set()
    roots = (
        Path.home() / ".claude" / "skills",
        Path(repository).expanduser() / ".claude" / "skills",
    )
    for base in roots:
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.name.startswith("."):
                names.add(entry.name)
    return names


def link_portable_primary_skills(
    root: Path,
    settings: dict[str, Any],
    repository: str | Path,
    capabilities_root: Path,
) -> tuple[Path | None, list[str]]:
    """Link primary Codex skill trees under a task-owned ``.claude/skills`` root.

    The returned directory is meant for Claude's native ``--add-dir`` skill
    discovery. Each portable skill is exposed by a directory symlink so its
    complete resource tree (instruction, scripts, assets, references) is
    reachable without copying the primary home. Skills whose name collides with
    a native Claude skill are skipped so native discovery wins, and that
    difference is reported. Returns ``(skills_root_or_None, warnings)``.
    """
    home = _primary_codex_home(root, settings)
    if home is None:
        return None, []
    source = home / "skills"
    if not source.is_dir():
        return None, []
    native = _native_claude_skill_names(repository)
    skills_dir = capabilities_root / ".claude" / "skills"
    warnings: list[str] = []
    linked = 0
    try:
        entries = sorted(source.iterdir(), key=lambda item: item.name)
        if (source / ".system").is_dir():
            entries.extend(sorted((source / ".system").iterdir(), key=lambda item: item.name))
    except OSError:
        return None, []
    disabled: set[Path] = set()
    for item in _primary_codex_config(home).get("skills", {}).get("config", []):
        if isinstance(item, dict) and item.get("enabled") is False and isinstance(item.get("path"), str):
            path = Path(item["path"]).expanduser()
            path = path if path.is_absolute() else home / path
            disabled.add((path.parent if path.name == "SKILL.md" else path).resolve())
    for entry in entries:
        if entry.name.startswith(".") or not (entry / "SKILL.md").is_file() or entry.resolve() in disabled:
            continue
        if entry.name in native:
            warnings.append(
                f"portable skill {entry.name!r} is shadowed by a native Claude "
                "skill of the same name; native discovery wins"
            )
            continue
        skills_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        link = skills_dir / entry.name
        if link.exists() or link.is_symlink():
            continue
        link.symlink_to(entry, target_is_directory=entry.is_dir())
        linked += 1
    return (capabilities_root if linked else None), warnings


def create_worker_capabilities_directory(root: Path, job_id: str) -> Path:
    """Create a readable, owner-controlled per-job capabilities root.

    Skill links live here rather than under the launch directory (a pool
    control the trusted settings deny read) so Claude's native discovery and
    ordinary tool reads of skill resources both work. The directory is disposable
    and removed with the job.
    """
    parent = root.expanduser().resolve() / "worker-capabilities"
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = parent.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RuntimeError(
            "worker capabilities root must be an owner-controlled non-symlink directory"
        )
    os.chmod(parent, 0o700)
    prefix = re.sub(r"[^A-Za-z0-9_.-]+", "-", job_id).strip("-") or "job"
    directory = Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=parent))
    os.chmod(directory, 0o700)
    return directory
