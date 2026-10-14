"""Durable, verified dispatch ChangeSets and their deliberately narrow CLI.

The runner is the only writer of active artifacts.  Readers derive every path from a
validated request id; neither a request nor a manifest may provide a filesystem path.
Applying is an explicit controller action and never commits, merges, pushes, or runs an
acceptance command.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import dispatch_protocol as dp
from . import internal_git

SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
PATCH_NAME = "change.patch"
HASH_PREFIX = "sha256:"
HASH_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
COMMIT_RE = re.compile(r"\A[0-9a-f]{40,64}\Z")
MAX_PATCH_BYTES = 128 * 1024 * 1024
APPLY_LOCK_TIMEOUT = 5.0
GIT_TIMEOUT_SECONDS = 120.0


class ChangeSetError(RuntimeError):
    """A ChangeSet is malformed, unsafe, tampered, or cannot be applied."""


@dataclass(frozen=True)
class VerifiedChangeSet:
    id: str
    state: str
    directory: Path
    manifest: dict[str, Any]
    patch: bytes
    manifest_sha256: str


def paths(root: str | Path) -> dict[str, Path]:
    dispatch = Path(root).expanduser() / "dispatch"
    return {
        "dispatch": dispatch,
        "active": dispatch / "changesets",
        "dropped": dispatch / "dropped",
        "applied": dispatch / "applied",
        "locks": dispatch / "apply-locks",
    }


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mkdir_owner(path: Path) -> None:
    created = not path.exists() and not path.is_symlink()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ChangeSetError(f"control path is a symlink: {path}")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ChangeSetError(f"control path is not an owner directory: {path}")
    os.chmod(path, 0o700)
    if created:
        _fsync_dir(path)
        _fsync_dir(path.parent)


def ensure_storage(root: str | Path) -> dict[str, Path]:
    result = paths(root)
    _mkdir_owner(result["dispatch"])
    for name in ("active", "dropped", "applied", "locks"):
        _mkdir_owner(result[name])
    return result


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(data: bytes) -> str:
    return HASH_PREFIX + hashlib.sha256(data).hexdigest()


def _valid_id(value: Any) -> str:
    if not isinstance(value, str) or not dp.REQUEST_ID_RE.fullmatch(value):
        raise ChangeSetError("ChangeSet id must be a validated dispatch request id")
    return value


def _valid_changed_files(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ChangeSetError("manifest changed_files must be a string list")
    normalized: list[str] = []
    for item in value:
        path = PurePosixPath(item)
        if (
            not item
            or "\x00" in item
            or "\\" in item
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.parts[0] == ".git"
        ):
            raise ChangeSetError(f"unsafe changed file: {item!r}")
        normalized.append(path.as_posix())
    if normalized != sorted(set(normalized)):
        raise ChangeSetError("manifest changed_files must be sorted and unique")
    return normalized


def _validate_manifest(value: Any, change_id: str, patch: bytes) -> dict[str, Any]:
    required = {
        "schema",
        "request_id",
        "request_hash",
        "release_id",
        "protocol_version",
        "repo_id",
        "base_commit",
        "changed_files",
        "terminal_status",
        "patch_size",
        "patch_sha256",
        "created_at",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ChangeSetError("manifest has unknown or missing fields")
    if value.get("schema") != SCHEMA_VERSION:
        raise ChangeSetError(f"manifest schema must be {SCHEMA_VERSION}")
    if value.get("request_id") != change_id:
        raise ChangeSetError("manifest request_id does not match its artifact id")
    if not isinstance(value.get("request_hash"), str) or not HASH_RE.fullmatch(
        value["request_hash"]
    ):
        raise ChangeSetError("manifest request_hash is invalid")
    if not isinstance(value.get("release_id"), str) or not dp.RELEASE_ID_RE.fullmatch(
        value["release_id"]
    ):
        raise ChangeSetError("manifest release_id is invalid")
    if (
        not isinstance(value.get("protocol_version"), int)
        or isinstance(value["protocol_version"], bool)
        or value["protocol_version"] < 1
    ):
        raise ChangeSetError("manifest protocol_version is invalid")
    if not isinstance(value.get("repo_id"), str) or not dp.IDENTITY_RE.fullmatch(
        value["repo_id"]
    ):
        raise ChangeSetError("manifest repo_id is invalid")
    if not isinstance(value.get("base_commit"), str) or not COMMIT_RE.fullmatch(
        value["base_commit"]
    ):
        raise ChangeSetError("manifest base_commit is invalid")
    _valid_changed_files(value.get("changed_files"))
    terminal_status = value.get("terminal_status")
    if (
        not isinstance(terminal_status, str)
        or not terminal_status
        or len(terminal_status.encode("utf-8")) > 256
        or any(ord(character) < 0x20 for character in terminal_status)
    ):
        raise ChangeSetError("manifest terminal_status is invalid")
    if value.get("patch_size") != len(patch) or len(patch) > MAX_PATCH_BYTES:
        raise ChangeSetError("manifest patch_size does not match patch bytes")
    if value.get("patch_sha256") != _sha(patch):
        raise ChangeSetError("manifest patch SHA-256 does not match patch bytes")
    if not isinstance(value.get("created_at"), int) or isinstance(
        value["created_at"], bool
    ):
        raise ChangeSetError("manifest created_at is invalid")
    return dict(value)


def _write_file(path: Path, data: bytes, mode: int = 0o400) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while persisting ChangeSet content")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create(
    root: str | Path,
    *,
    request_id: str,
    request_hash: str,
    release_id: str,
    protocol_version: int,
    repo_id: str,
    base_commit: str,
    changed_files: list[str],
    terminal_status: str,
    patch: bytes,
    now: int | None = None,
) -> VerifiedChangeSet:
    """Atomically publish one immutable runner-owned ChangeSet."""
    change_id = _valid_id(request_id)
    storage = ensure_storage(root)
    destination = storage["active"] / change_id
    if destination.exists() or destination.is_symlink():
        existing = inspect(root, change_id, include_dropped=False)
        expected = {
            "request_hash": request_hash,
            "patch_sha256": _sha(patch),
        }
        if all(existing.manifest.get(key) == value for key, value in expected.items()):
            return existing
        raise ChangeSetError(f"ChangeSet id already exists with different content: {change_id}")
    dropped = storage["dropped"] / change_id
    if dropped.exists() or dropped.is_symlink():
        raise ChangeSetError(f"ChangeSet id is already dropped: {change_id}")
    manifest = {
        "schema": SCHEMA_VERSION,
        "request_id": change_id,
        "request_hash": request_hash,
        "release_id": release_id,
        "protocol_version": protocol_version,
        "repo_id": repo_id,
        "base_commit": base_commit,
        "changed_files": sorted(set(changed_files)),
        "terminal_status": terminal_status,
        "patch_size": len(patch),
        "patch_sha256": _sha(patch),
        "created_at": int(time.time()) if now is None else int(now),
    }
    _validate_manifest(manifest, change_id, patch)
    manifest_bytes = _canonical(manifest)
    temporary = storage["active"] / f".{change_id}.{os.getpid()}.{os.urandom(6).hex()}"
    os.mkdir(temporary, 0o700)
    try:
        _write_file(temporary / PATCH_NAME, patch)
        _write_file(temporary / MANIFEST_NAME, manifest_bytes)
        _fsync_dir(temporary)
        os.chmod(temporary, 0o700)
        os.rename(temporary, destination)
        _fsync_dir(storage["active"])
    except Exception:
        for child in (temporary / PATCH_NAME, temporary / MANIFEST_NAME):
            child.unlink(missing_ok=True)
        try:
            temporary.rmdir()
        except OSError:
            pass
        raise
    return VerifiedChangeSet(
        change_id, "active", destination, manifest, patch, _sha(manifest_bytes)
    )


def _secure_directory(path: Path, *, mode: int = 0o700) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ChangeSetError(f"cannot inspect ChangeSet directory: {exc}") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != mode
    ):
        raise ChangeSetError(f"unsafe ChangeSet directory: {path}")


def _secure_read(path: Path, *, max_bytes: int) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ChangeSetError(f"cannot inspect ChangeSet content: {exc}") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o400
        or info.st_size > max_bytes
    ):
        raise ChangeSetError(f"unsafe ChangeSet content: {path.name}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o400
            or opened.st_size != info.st_size
            or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise ChangeSetError(f"ChangeSet content changed during read: {path.name}")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    if len(data) > max_bytes:
        raise ChangeSetError(f"ChangeSet content exceeds size cap: {path.name}")
    return data


def _container(root: str | Path, name: str, *, required: bool) -> Path | None:
    all_paths = paths(root)
    dispatch = all_paths["dispatch"]
    if not dispatch.exists() and not dispatch.is_symlink():
        if required:
            raise ChangeSetError(f"ChangeSet storage does not exist: {dispatch}")
        return None
    _secure_directory(dispatch, mode=0o700)
    container = all_paths[name]
    if not container.exists() and not container.is_symlink():
        if required:
            raise ChangeSetError(f"ChangeSet storage does not exist: {container}")
        return None
    _secure_directory(container, mode=0o700)
    return container


def _inspect_at(
    container: Path,
    change_id: str,
    state: str,
    *,
    allow_unanchored_receipt: bool = False,
) -> VerifiedChangeSet:
    directory = container / change_id
    _secure_directory(directory)
    entries = {entry.name for entry in directory.iterdir()}
    if entries != {MANIFEST_NAME, PATCH_NAME}:
        raise ChangeSetError("ChangeSet directory has unknown or missing content")
    patch = _secure_read(directory / PATCH_NAME, max_bytes=MAX_PATCH_BYTES)
    manifest_bytes = _secure_read(directory / MANIFEST_NAME, max_bytes=64 * 1024)
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ChangeSetError(f"manifest is not valid JSON: {exc}") from exc
    validated = _validate_manifest(manifest, change_id, patch)
    if manifest_bytes != _canonical(validated):
        raise ChangeSetError("manifest is not canonically encoded")
    manifest_sha256 = _sha(manifest_bytes)
    root = container.parent.parent
    receipt_path = dp.receipt_path(root, change_id)
    if receipt_path.exists() or receipt_path.is_symlink():
        receipt = dp.read_receipt(root, change_id)
        if receipt is None:
            raise ChangeSetError("ChangeSet receipt anchor is unsafe or malformed")
        anchored = (
            receipt.get("changeset_id") == change_id
            and receipt.get("changeset_sha") == manifest_sha256
        )
        if not anchored and not (
            allow_unanchored_receipt
            and receipt.get("changeset_id") is None
            and receipt.get("changeset_sha") is None
        ):
            raise ChangeSetError("manifest SHA-256 does not match its terminal receipt")
    return VerifiedChangeSet(
        change_id, state, directory, validated, patch, manifest_sha256
    )


def inspect(
    root: str | Path, change_id: str, *, include_dropped: bool = True
) -> VerifiedChangeSet:
    change_id = _valid_id(change_id)
    active = _container(root, "active", required=False)
    if active is not None and (
        (active / change_id).exists() or (active / change_id).is_symlink()
    ):
        return _inspect_at(active, change_id, "active")
    if include_dropped:
        dropped = _container(root, "dropped", required=False)
        if dropped is not None and (
            (dropped / change_id).exists() or (dropped / change_id).is_symlink()
        ):
            return _inspect_at(
                dropped, change_id, "dropped", allow_unanchored_receipt=True
            )
    raise ChangeSetError(f"ChangeSet not found: {change_id}")


def list_all(root: str | Path) -> list[VerifiedChangeSet]:
    """Verify and return active and dropped artifacts without creating any path."""
    result: list[VerifiedChangeSet] = []
    for name, state in (("active", "active"), ("dropped", "dropped")):
        container = _container(root, name, required=False)
        if container is None:
            continue
        for entry in sorted(container.iterdir(), key=lambda item: item.name):
            _valid_id(entry.name)
            result.append(
                _inspect_at(
                    container,
                    entry.name,
                    state,
                    allow_unanchored_receipt=state == "dropped",
                )
            )
    return result


def _git(
    repo: Path,
    *args: str,
    input_bytes: bytes | None = None,
    overrides: internal_git.GitOverrides | None = None,
) -> internal_git.GitResult:
    try:
        return internal_git.run(
            repo,
            args,
            timeout=GIT_TIMEOUT_SECONDS,
            input_bytes=input_bytes,
            overrides=overrides,
        )
    except internal_git.GitTimeoutError as exc:
        raise ChangeSetError(
            f"Git {' '.join(args)} timed out after {GIT_TIMEOUT_SECONDS:g}s"
        ) from exc
    except internal_git.GitError as exc:
        raise ChangeSetError(f"cannot run Git: {exc}") from exc


def _git_ok(
    repo: Path,
    *args: str,
    input_bytes: bytes | None = None,
    overrides: internal_git.GitOverrides | None = None,
) -> bytes:
    result = _git(repo, *args, input_bytes=input_bytes, overrides=overrides)
    if result.returncode:
        detail = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
        raise ChangeSetError(f"Git {' '.join(args)} failed: {detail or result.returncode}")
    return result.stdout


def _not_symlink(path: Path, *, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ChangeSetError(f"cannot inspect {label} path {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ChangeSetError(f"{label} path is a symlink: {path}")


def _registered_repo(settings: Mapping[str, Any], repo_id: str) -> Path:
    projects = settings.get("projects")
    entry = projects.get(repo_id) if isinstance(projects, Mapping) else None
    configured = entry.get("path") if isinstance(entry, Mapping) else None
    if not isinstance(configured, str):
        raise ChangeSetError(f"manifest repository is not currently registered: {repo_id}")
    configured_path = Path(configured).expanduser()
    if not configured_path.is_absolute():
        raise ChangeSetError("registered repository path must be absolute")
    requested = configured_path.absolute()
    _not_symlink(requested, label="repository")
    if not requested.is_dir():
        raise ChangeSetError(f"registered repository does not exist: {requested}")
    git_path = requested / ".git"
    if git_path.is_symlink() or not git_path.exists():
        raise ChangeSetError("repository Git control path is missing or symlinked")
    try:
        git_dir_text = internal_git.line(
            _git_ok(requested, "rev-parse", "--absolute-git-dir"),
            label="Git control path",
        )
    except ValueError as exc:
        raise ChangeSetError(str(exc)) from exc
    git_dir = Path(git_dir_text)
    if not git_dir.is_absolute():
        raise ChangeSetError("Git returned a non-absolute control path")
    _not_symlink(git_dir, label="Git control")
    index_path = git_dir / "index"
    if index_path.is_symlink():
        raise ChangeSetError("repository Git index path is symlinked")
    try:
        top = Path(
            internal_git.line(
                _git_ok(requested, "rev-parse", "--show-toplevel"),
                label="repository root",
            )
        )
    except ValueError as exc:
        raise ChangeSetError(str(exc)) from exc
    if top.resolve() != requested.resolve():
        raise ChangeSetError("registered path does not identify the repository root")
    return requested


@contextmanager
def _apply_lock(root: str | Path, repo_id: str) -> Iterator[None]:
    storage = ensure_storage(root)
    safe = hashlib.sha256(repo_id.encode()).hexdigest()
    lock_path = storage["locks"] / f"{safe}.lock"
    descriptor = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(descriptor)
        raise ChangeSetError("repository apply lock is not an owner regular file")
    os.fchmod(descriptor, 0o600)
    deadline = time.monotonic() + APPLY_LOCK_TIMEOUT
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ChangeSetError("timed out waiting for repository apply lock")
                time.sleep(0.05)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _status(repo: Path) -> bytes:
    return _git_ok(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )


@contextmanager
def _temporary_index(
    directory: Path, repo: Path, head: str
) -> Iterator[internal_git.GitOverrides]:
    try:
        objects_text = internal_git.line(
            _git_ok(repo, "rev-parse", "--git-path", "objects"),
            label="Git object path",
        )
    except ValueError as exc:
        raise ChangeSetError(str(exc)) from exc
    source_objects = Path(objects_text)
    if not source_objects.is_absolute():
        source_objects = (repo / source_objects).absolute()
    _not_symlink(source_objects, label="Git object")
    if not source_objects.is_dir():
        raise ChangeSetError("repository Git object path is missing")
    with tempfile.TemporaryDirectory(prefix="changeset-preflight-", dir=directory) as raw:
        temporary = Path(raw)
        private_objects = temporary / "objects"
        private_objects.mkdir(mode=0o700)
        override = internal_git.GitOverrides(
            index_file=temporary / "index",
            object_directory=private_objects,
            alternate_object_directories=(source_objects,),
        )
        _git_ok(repo, "read-tree", head, overrides=override)
        yield override


def _atomic_event(path: Path, value: Mapping[str, Any]) -> None:
    data = _canonical(value)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{os.urandom(6).hex()}"
    _write_file(temporary, data, mode=0o600)
    try:
        os.rename(temporary, path)
        _fsync_dir(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _indexed_changed_files(
    repo: Path,
    head: str,
    *,
    overrides: internal_git.GitOverrides | None = None,
) -> list[str]:
    raw = _git_ok(
        repo,
        "diff",
        "--no-ext-diff",
        "--cached",
        "--name-only",
        "--find-renames",
        "-z",
        head,
        overrides=overrides,
    )
    return sorted(
        item.decode("utf-8", "surrogateescape")
        for item in raw.split(b"\0")
        if item
    )


def apply(root: str | Path, settings: Mapping[str, Any], change_id: str) -> dict[str, Any]:
    if os.environ.get("CODEX_SEATS_MANAGED_WORKER") == "1":
        raise ChangeSetError("managed workers may not apply ChangeSets")
    verified = inspect(root, change_id, include_dropped=False)
    repo_id = verified.manifest["repo_id"]
    repo = _registered_repo(settings, repo_id)
    with _apply_lock(root, repo_id):
        # Re-verify under the mutation lock so a pre-lock replacement cannot win.
        verified = inspect(root, change_id, include_dropped=False)
        if verified.manifest["repo_id"] != repo_id:
            raise ChangeSetError("ChangeSet repository identity changed")
        if _status(repo):
            raise ChangeSetError("repository has staged, unstaged, or untracked user work")
        try:
            head = internal_git.line(
                _git_ok(repo, "rev-parse", "HEAD^{commit}"), label="commit id"
            )
        except ValueError as exc:
            raise ChangeSetError(str(exc)) from exc
        base = verified.manifest["base_commit"]
        exists = _git(repo, "cat-file", "-e", f"{base}^{{commit}}")
        if exists.returncode:
            raise ChangeSetError("ChangeSet base commit is missing")
        ancestor = _git(repo, "merge-base", "--is-ancestor", base, head)
        if ancestor.returncode:
            raise ChangeSetError("ChangeSet base commit is not an ancestor of HEAD")
        expected = verified.manifest["changed_files"]
        storage = paths(root)
        # Apply to a private index first. This validates binary/text bytes and the exact
        # staged path set without touching either the user's index or worktree.
        with _temporary_index(storage["locks"], repo, head) as temporary_env:
            preflight = _git(
                repo,
                "apply",
                "--cached",
                "--binary",
                input_bytes=verified.patch,
                overrides=temporary_env,
            )
            if preflight.returncode:
                detail = (preflight.stderr or preflight.stdout).decode(
                    "utf-8", "replace"
                ).strip()
                raise ChangeSetError(f"ChangeSet does not apply cleanly: {detail}")
            predicted = _indexed_changed_files(
                repo, head, overrides=temporary_env
            )
            if predicted != expected:
                raise ChangeSetError(
                    "staged changed-file set differs from manifest: "
                    f"{predicted!r} != {expected!r}"
                )
        # Recheck after preflight. Git apply without --reject is atomic on failure; never
        # use reset --hard/clean as rollback because concurrent user work is not ours.
        if _status(repo):
            raise ChangeSetError("repository changed while ChangeSet was preflighting")
        applied = _git(
            repo, "apply", "--index", "--binary", input_bytes=verified.patch
        )
        if applied.returncode:
            detail = (applied.stderr or applied.stdout).decode(
                "utf-8", "replace"
            ).strip()
            if _status(repo):
                raise ChangeSetError(
                    "Git apply failed after modifying the repository; preserve the "
                    f"checkout for manual inspection: {detail}"
                )
            raise ChangeSetError(f"ChangeSet apply failed without mutation: {detail}")
        staged = _indexed_changed_files(repo, head)
        if staged != expected:
            raise ChangeSetError(
                "ChangeSet applied but post-apply staged files violated the verified "
                f"preflight invariant: {staged!r} != {expected!r}"
            )
        event = {
            "schema": 1,
            "changeset_id": verified.id,
            "changeset_sha256": verified.manifest_sha256,
            "repo_id": repo_id,
            "pre_apply_head": head,
            "applied_at": int(time.time()),
        }
        try:
            _atomic_event(storage["applied"] / f"{verified.id}.json", event)
        except Exception as exc:
            raise ChangeSetError(
                f"ChangeSet is applied and staged, but its applied event failed: {exc}"
            ) from exc
    return event


def _move_to_dropped(
    storage: dict[str, Path], verified: VerifiedChangeSet
) -> str:
    change_id = verified.id
    destination = storage["dropped"] / change_id
    if destination.exists() or destination.is_symlink():
        existing = _inspect_at(
            storage["dropped"],
            change_id,
            "dropped",
            allow_unanchored_receipt=True,
        )
        if existing.manifest_sha256 == verified.manifest_sha256:
            return "already_dropped"
        raise ChangeSetError("dropped ChangeSet id has conflicting content")
    try:
        os.rename(verified.directory, destination)
    except FileNotFoundError:
        existing = _inspect_at(
            storage["dropped"],
            change_id,
            "dropped",
            allow_unanchored_receipt=True,
        )
        if existing.manifest_sha256 == verified.manifest_sha256:
            return "already_dropped"
        raise ChangeSetError("dropped ChangeSet id has conflicting content")
    _fsync_dir(storage["active"])
    _fsync_dir(storage["dropped"])
    return "dropped"


def abandon_unpublished(
    root: str | Path, change_id: str, manifest_sha256: str
) -> str:
    """Tombstone a verified artifact whose terminal receipt lost a publish race."""
    change_id = _valid_id(change_id)
    storage = ensure_storage(root)
    verified = _inspect_at(
        storage["active"],
        change_id,
        "active",
        allow_unanchored_receipt=True,
    )
    if verified.manifest_sha256 != manifest_sha256:
        raise ChangeSetError("unpublished ChangeSet identity changed")
    return _move_to_dropped(storage, verified)


def drop(root: str | Path, change_id: str) -> str:
    if os.environ.get("CODEX_SEATS_MANAGED_WORKER") == "1":
        raise ChangeSetError("managed workers may not drop ChangeSets")
    change_id = _valid_id(change_id)
    storage = ensure_storage(root)
    active_path = storage["active"] / change_id
    if active_path.exists() or active_path.is_symlink():
        # Explicit drop is also the recovery path for a fully verified artifact whose
        # completion lost the receipt race to cancellation.
        verified = _inspect_at(
            storage["active"],
            change_id,
            "active",
            allow_unanchored_receipt=True,
        )
        return _move_to_dropped(storage, verified)
    dropped_path = storage["dropped"] / change_id
    if dropped_path.exists() or dropped_path.is_symlink():
        _inspect_at(
            storage["dropped"],
            change_id,
            "dropped",
            allow_unanchored_receipt=True,
        )
        return "already_dropped"
    raise ChangeSetError(f"ChangeSet not found: {change_id}")


def register(subparsers) -> None:
    parser = subparsers.add_parser("changeset", help="inspect and explicitly apply worker changes")
    commands = parser.add_subparsers(dest="changeset_action", required=True)
    listing = commands.add_parser("list", help="list verified durable ChangeSets")
    listing.add_argument("--json", action="store_true")
    inspect_p = commands.add_parser("inspect", help="inspect one verified ChangeSet")
    inspect_p.add_argument("id")
    inspect_p.add_argument("--patch", action="store_true")
    inspect_p.add_argument("--json", action="store_true")
    apply_p = commands.add_parser("apply", help="apply one ChangeSet to worktree and index")
    apply_p.add_argument("id")
    apply_p.add_argument("--json", action="store_true")
    drop_p = commands.add_parser("drop", help="move one ChangeSet to recoverable tombstone storage")
    drop_p.add_argument("id")
    drop_p.add_argument("--json", action="store_true")
    parser.set_defaults(handler="changeset")


def _summary(item: VerifiedChangeSet, *, include_patch: bool) -> dict[str, Any]:
    result = {
        "id": item.id,
        "state": item.state,
        "sha256": item.manifest_sha256,
        **item.manifest,
    }
    if include_patch:
        result["patch"] = item.patch.decode("utf-8", "replace")
    return result


def cmd_changeset(
    args: argparse.Namespace, settings: Mapping[str, Any], root: str | Path
) -> int:
    try:
        action = args.changeset_action
        if action == "list":
            rows = [_summary(item, include_patch=False) for item in list_all(root)]
            if args.json:
                print(json.dumps(rows, sort_keys=True))
            else:
                for row in rows:
                    print(
                        f"{row['id']} {row['state']} {row['terminal_status']} "
                        f"repo={row['repo_id']} files={len(row['changed_files'])} "
                        f"sha256={row['sha256']}"
                    )
            return 0
        if action == "inspect":
            item = inspect(root, args.id)
            value = _summary(item, include_patch=args.patch)
            if args.json:
                print(json.dumps(value, sort_keys=True))
            else:
                print(
                    f"{item.id} {item.state} {item.manifest['terminal_status']} "
                    f"repo={item.manifest['repo_id']} base={item.manifest['base_commit']} "
                    f"files={len(item.manifest['changed_files'])} sha256={item.manifest_sha256}"
                )
                if args.patch:
                    sys.stdout.flush()
                    sys.stdout.buffer.write(item.patch)
            return 0
        if action == "apply":
            event = apply(root, settings, args.id)
            print(json.dumps(event, sort_keys=True) if args.json else f"applied changeset={args.id}")
            return 0
        if action == "drop":
            outcome = drop(root, args.id)
            value = {"changeset_id": args.id, "status": outcome}
            print(json.dumps(value, sort_keys=True) if args.json else f"{outcome} changeset={args.id}")
            return 0
        raise ChangeSetError(f"unknown ChangeSet action: {action}")
    except ChangeSetError as exc:
        print(str(exc), file=sys.stderr)
        return 2
