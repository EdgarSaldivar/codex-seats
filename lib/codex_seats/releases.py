"""Content-addressed releases and new-work pointer management.

This module deliberately has no import-time connection to the live runtime.  It
builds releases from a clean Git checkout, verifies them from bytes on disk, and
switches only the ``current`` symlink.  In particular, preflight never refreshes
the ledger and no operation signals or terminates a process.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from . import internal_git
from .dispatch_protocol import PROTOCOL_VERSION as DISPATCH_PROTOCOL


MANIFEST_SCHEMA = 1
MANIFEST_NAME = "manifest.json"
HASH_PREFIX = "sha256:"
RELEASE_ID_RE = re.compile(r"\A[0-9a-f]{64}\Z")
MODULE_BASENAME_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\.py\Z")
BOOTSTRAP_SHIM_VERSION = 1
GIT_TIMEOUT_SECONDS = 120.0
TERMINAL_STATES = frozenset(
    {
        "done",
        "failed",
        "paused",
        "died",
        "cancelled",
        "canceled",
        "completed",
        "blocked",
    }
)

# These are the on-disk/protocol versions understood by this runtime.  A release
# states an inclusive compatibility range for every one; unknown or missing
# protocols fail closed during activation.
CURRENT_PROTOCOLS: dict[str, int] = {
    "release": MANIFEST_SCHEMA,
    "settings": 3,
    "ledger": 6,
    "dispatch": DISPATCH_PROTOCOL,
}
DEFAULT_COMPATIBILITY: dict[str, dict[str, int]] = {
    name: {"min": version, "max": version}
    for name, version in CURRENT_PROTOCOLS.items()
}


class ReleaseError(RuntimeError):
    """A release is malformed, unsafe, incompatible, or cannot be published."""


def validate_release_id(value: Any) -> str:
    if not isinstance(value, str) or not RELEASE_ID_RE.fullmatch(value):
        raise ReleaseError(
            "release_id must be exactly 64 lowercase hexadecimal characters"
        )
    return value


@dataclass(frozen=True)
class PreflightReport:
    bootstrap: bool
    blockers: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.blockers

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "bootstrap": self.bootstrap,
            "blockers": list(self.blockers),
        }


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(data: bytes) -> str:
    return HASH_PREFIX + hashlib.sha256(data).hexdigest()


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    """Return the bare digest used as the immutable release directory name."""
    return hashlib.sha256(_canonical_bytes(manifest)).hexdigest()


def _git(source: Path, *args: str, text: bool = True) -> str | bytes:
    try:
        result = internal_git.run(
            source,
            args,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except internal_git.GitTimeoutError as exc:
        raise ReleaseError(
            f"git {' '.join(args)} timed out after {GIT_TIMEOUT_SECONDS:g}s"
        ) from exc
    except internal_git.GitError as exc:
        raise ReleaseError(f"cannot run git: {exc}") from exc
    if result.returncode:
        stderr = result.stderr.decode("utf-8", "replace")
        if stderr.endswith("\n"):
            stderr = stderr[:-1]
        raise ReleaseError(f"git {' '.join(args)} failed: {stderr or 'unknown error'}")
    return result.stdout.decode("utf-8", "surrogateescape") if text else result.stdout


def _git_line(source: Path, *args: str, label: str) -> str:
    output = str(_git(source, *args))
    if output.endswith("\n"):
        output = output[:-1]
    if "\n" in output or "\x00" in output:
        raise ReleaseError(f"Git returned malformed {label}")
    return output


def _clean_source(source: str | Path) -> tuple[Path, str, str]:
    path = Path(source).expanduser().resolve()
    if not path.is_dir():
        raise ReleaseError(f"source is not a directory: {path}")
    top = Path(_git_line(path, "rev-parse", "--show-toplevel", label="repository root")).resolve()
    if top != path:
        raise ReleaseError(f"source must be the Git repository root: {top}")
    dirty = _git(
        path,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=none",
        text=False,
    )
    if dirty:
        raise ReleaseError("source checkout is not clean")
    commit = _git_line(path, "rev-parse", "HEAD^{commit}", label="commit id")
    tree = _git_line(path, "rev-parse", f"{commit}^{{tree}}", label="tree id")
    tree_entries = _git(path, "ls-tree", "-r", "-z", commit, text=False)
    if any(
        entry.startswith(b"160000 ") for entry in tree_entries.split(b"\0") if entry
    ):
        raise ReleaseError("Git submodules are not supported in immutable releases")
    return path, commit, tree


def _safe_member_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ReleaseError(f"unsafe archive path: {name!r}")
    if path == PurePosixPath(MANIFEST_NAME):
        raise ReleaseError(f"source reserves the path {MANIFEST_NAME!r}")
    return path


def _safe_link_target(member_path: PurePosixPath, target: str) -> None:
    target_path = PurePosixPath(target)
    if target_path.is_absolute():
        raise ReleaseError(f"absolute symlink target in {member_path}")
    depth = len(member_path.parent.parts)
    for part in target_path.parts:
        if part == "..":
            depth -= 1
            if depth < 0:
                raise ReleaseError(f"escaping symlink target in {member_path}")
        elif part not in ("", "."):
            depth += 1


def _extract_git_archive(source: Path, destination: Path, commit: str) -> None:
    # Archive the inspected commit, not a second lookup of HEAD.  A concurrent
    # commit after inspection therefore cannot break the manifest's source bind.
    archive = _git(source, "archive", "--format=tar", commit, text=False)
    try:
        tar = tarfile.open(fileobj=io.BytesIO(archive), mode="r:")
    except tarfile.TarError as exc:
        raise ReleaseError(f"invalid Git archive: {exc}") from exc
    with tar:
        for member in tar.getmembers():
            relative = _safe_member_path(member.name.rstrip("/"))
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(mode=0o755, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            if member.isreg():
                source_file = tar.extractfile(member)
                if source_file is None:
                    raise ReleaseError(f"cannot read archive member {member.name!r}")
                with open(target, "xb") as handle:
                    shutil.copyfileobj(source_file, handle)
                os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
            elif member.issym():
                _safe_link_target(relative, member.linkname)
                os.symlink(member.linkname, target)
            else:
                raise ReleaseError(f"unsupported archive member type: {member.name!r}")


def _file_record(base: Path, path: Path) -> dict[str, Any]:
    relative = path.relative_to(base).as_posix()
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path)
        _safe_link_target(PurePosixPath(relative), target)
        data = target.encode("utf-8")
        return {
            "path": relative,
            "type": "symlink",
            "mode": "120000",
            "size": len(data),
            "sha256": _sha256(data),
        }
    if not stat.S_ISREG(info.st_mode):
        raise ReleaseError(f"release contains unsupported file type: {relative}")
    data = path.read_bytes()
    return {
        "path": relative,
        "type": "file",
        "mode": "100755" if info.st_mode & 0o111 else "100644",
        "size": len(data),
        "sha256": _sha256(data),
    }


def _content_records(base: Path) -> list[dict[str, Any]]:
    records = [
        _file_record(base, path)
        for path in base.rglob("*")
        if (path.is_symlink() or not path.is_dir())
        and path.relative_to(base).as_posix() != MANIFEST_NAME
    ]
    return sorted(records, key=lambda record: record["path"])


def _normalize_compatibility(
    compatibility: Mapping[str, Any] | None,
) -> dict[str, dict[str, int]]:
    raw = DEFAULT_COMPATIBILITY if compatibility is None else compatibility
    normalized: dict[str, dict[str, int]] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name:
            raise ReleaseError("protocol names must be non-empty strings")
        if isinstance(value, int):
            minimum = maximum = value
        elif isinstance(value, Mapping):
            minimum, maximum = value.get("min"), value.get("max")
        else:
            raise ReleaseError(f"invalid compatibility range for {name}")
        if (
            not isinstance(minimum, int)
            or isinstance(minimum, bool)
            or not isinstance(maximum, int)
            or isinstance(maximum, bool)
            or minimum < 1
            or maximum < minimum
        ):
            raise ReleaseError(f"invalid compatibility range for {name}")
        normalized[name] = {"min": minimum, "max": maximum}
    return dict(sorted(normalized.items()))


def _make_writable(path: Path) -> None:
    if not path.exists():
        return
    for child in path.rglob("*"):
        if child.is_dir() and not child.is_symlink():
            os.chmod(child, 0o700)
        elif not child.is_symlink():
            os.chmod(child, 0o600)
    os.chmod(path, 0o700)


def _remove_tree(path: Path) -> None:
    if path.exists():
        _make_writable(path)
        shutil.rmtree(path)


def _freeze_tree(path: Path) -> None:
    for child in path.rglob("*"):
        if child.is_symlink():
            continue
        if child.is_dir():
            os.chmod(child, 0o555)
        else:
            os.chmod(child, 0o555 if child.stat().st_mode & 0o111 else 0o444)
    os.chmod(path, 0o555)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("write returned no progress")
        view = view[written:]


def _fsync_tree(path: Path) -> None:
    """Persist regular release bytes and directories before publication."""
    directories = [path]
    for child in path.rglob("*"):
        if child.is_symlink():
            continue
        if child.is_dir():
            directories.append(child)
        elif child.is_file():
            descriptor = os.open(child, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for directory in sorted(
        directories, key=lambda item: len(item.parts), reverse=True
    ):
        _fsync_directory(directory)


def stage_release(
    root: str | Path,
    source: str | Path,
    *,
    protocol_compatibility: Mapping[str, Any] | None = None,
    inspect_source: Callable[[str | Path], tuple[Path, str, str]] = _clean_source,
    build_source: Callable[[Path, Path, str], None] = _extract_git_archive,
) -> str:
    """Build a clean Git ``HEAD`` and atomically publish its immutable release."""
    root_path = Path(root).expanduser()
    source_path, commit, tree = inspect_source(source)
    compatibility = _normalize_compatibility(protocol_compatibility)
    releases = root_path / "releases"
    releases.mkdir(mode=0o700, parents=True, exist_ok=True)
    releases_info = releases.lstat()
    if (
        stat.S_ISLNK(releases_info.st_mode)
        or not stat.S_ISDIR(releases_info.st_mode)
        or releases_info.st_uid != os.getuid()
    ):
        raise ReleaseError("releases must be an owner-controlled real directory")
    _fsync_directory(root_path)
    temporary = Path(tempfile.mkdtemp(prefix=".stage-", dir=str(releases)))
    try:
        build_source(source_path, temporary, commit)
        files = _content_records(temporary)
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "source": {
                "commit": commit,
                "tree": tree,
                "content_sha256": _sha256(_canonical_bytes(files)),
            },
            "protocol_compatibility": compatibility,
            "files": files,
        }
        digest = manifest_hash(manifest)
        manifest_payload = (
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        manifest_path = temporary / MANIFEST_NAME
        descriptor = os.open(
            manifest_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            _write_all(descriptor, manifest_payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _freeze_tree(temporary)
        _fsync_tree(temporary)
        destination = releases / digest
        if destination.is_dir() and not destination.is_symlink():
            verify_release(root_path, digest)
            _remove_tree(temporary)
            return digest
        try:
            os.rename(temporary, destination)
            _fsync_directory(releases)
        except OSError:
            if destination.is_symlink() or not destination.is_dir():
                raise
            # A racing/idempotent stage is acceptable only when the already
            # published bytes independently verify under the same digest.
            verify_release(root_path, digest)
            _remove_tree(temporary)
        return digest
    except Exception:
        _remove_tree(temporary)
        raise


def _validate_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ReleaseError("manifest must be a JSON object")
    if set(manifest) != {"schema", "source", "protocol_compatibility", "files"}:
        raise ReleaseError("manifest has missing or unknown fields")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ReleaseError(f"unsupported manifest schema: {manifest.get('schema')!r}")
    source = manifest.get("source")
    if not isinstance(source, dict) or set(source) != {
        "commit",
        "tree",
        "content_sha256",
    }:
        raise ReleaseError("manifest source binding is malformed")
    for key in ("commit", "tree"):
        value = source.get(key)
        if not isinstance(value, str) or len(value) not in (40, 64):
            raise ReleaseError(f"manifest source.{key} is malformed")
        try:
            int(value, 16)
        except ValueError as exc:
            raise ReleaseError(f"manifest source.{key} is malformed") from exc
    content_hash = source.get("content_sha256")
    if not isinstance(content_hash, str) or len(content_hash) != len(HASH_PREFIX) + 64:
        raise ReleaseError("manifest source.content_sha256 is malformed")
    compatibility = manifest.get("protocol_compatibility")
    if _normalize_compatibility(compatibility) != compatibility:
        raise ReleaseError("manifest protocol compatibility is not canonical")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ReleaseError("manifest files must be an array")
    prior = ""
    for record in files:
        if not isinstance(record, dict) or set(record) != {
            "path",
            "type",
            "mode",
            "size",
            "sha256",
        }:
            raise ReleaseError("manifest file record is malformed")
        path = record.get("path")
        if not isinstance(path, str):
            raise ReleaseError("manifest file path is malformed")
        _safe_member_path(path)
        if path <= prior:
            raise ReleaseError("manifest file records are not uniquely sorted")
        prior = path
        if record.get("type") not in ("file", "symlink"):
            raise ReleaseError(f"manifest file type is malformed: {path}")
        if record.get("mode") not in ("100644", "100755", "120000"):
            raise ReleaseError(f"manifest file mode is malformed: {path}")
        if not isinstance(record.get("size"), int) or record["size"] < 0:
            raise ReleaseError(f"manifest file size is malformed: {path}")
        digest = record.get("sha256")
        if not isinstance(digest, str) or len(digest) != len(HASH_PREFIX) + 64:
            raise ReleaseError(f"manifest file hash is malformed: {path}")
    return manifest


def _pointer_hash(root: Path, name: str) -> str | None:
    pointer = root / name
    if not pointer.exists() and not pointer.is_symlink():
        return None
    if not pointer.is_symlink():
        raise ReleaseError(f"{pointer} must be a symlink")
    target = os.readlink(pointer)
    parsed = PurePosixPath(target)
    if len(parsed.parts) != 2 or parsed.parts[0] != "releases":
        raise ReleaseError(f"{pointer} has an unsafe target")
    digest = parsed.parts[1]
    if len(digest) != 64:
        raise ReleaseError(f"{pointer} target is not a release hash")
    try:
        int(digest, 16)
    except ValueError as exc:
        raise ReleaseError(f"{pointer} target is not a release hash") from exc
    return digest


def _resolve_release(root: Path, release: str | None) -> tuple[str, Path]:
    selector = release or "current"
    if selector in ("current", "previous"):
        digest = _pointer_hash(root, selector)
        if digest is None:
            raise ReleaseError(f"no {selector} release")
    else:
        digest = selector
        if len(digest) != 64:
            raise ReleaseError("release must be a 64-character manifest hash")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ReleaseError(
                "release must be a lowercase hexadecimal manifest hash"
            ) from exc
        if digest.lower() != digest:
            raise ReleaseError("release must be a lowercase hexadecimal manifest hash")
    return digest, root / "releases" / digest


def verify_release(root: str | Path, release: str | None = None) -> dict[str, Any]:
    """Verify the directory name, canonical manifest, and exact content set."""
    root_path = Path(root).expanduser()
    digest, directory = _resolve_release(root_path, release)
    if not directory.is_dir() or directory.is_symlink():
        raise ReleaseError(f"release does not exist: {digest}")
    manifest_path = directory / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ReleaseError(f"release {digest} has no regular {MANIFEST_NAME}")
    try:
        manifest = _validate_manifest(
            json.loads(manifest_path.read_text(encoding="utf-8"))
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"cannot read release manifest: {exc}") from exc
    if manifest_hash(manifest) != digest:
        raise ReleaseError(f"release directory does not match manifest hash: {digest}")
    actual = _content_records(directory)
    if actual != manifest["files"]:
        raise ReleaseError(f"release content verification failed: {digest}")
    if _sha256(_canonical_bytes(actual)) != manifest["source"]["content_sha256"]:
        raise ReleaseError(f"release aggregate content hash failed: {digest}")
    if stat.S_IMODE(directory.stat().st_mode) != 0o555:
        raise ReleaseError(f"release directory is not immutable: {digest}")
    for path in directory.rglob("*"):
        if path.is_symlink():
            continue
        expected_mode = 0o555 if path.is_dir() or path.stat().st_mode & 0o111 else 0o444
        if stat.S_IMODE(path.stat().st_mode) != expected_mode:
            relative = path.relative_to(directory).as_posix()
            raise ReleaseError(f"release path is not immutable: {relative}")
    return manifest


def release_path(root: str | Path, release_id: str) -> Path:
    """Return one structurally safe release selected by a bare content hash."""
    root_path = Path(root).expanduser().absolute()
    validate_release_id(release_id)
    try:
        root_info = root_path.lstat()
        releases_info = (root_path / "releases").lstat()
    except OSError as exc:
        raise ReleaseError(f"release root is unavailable: {exc}") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ReleaseError("runtime root must be a real directory")
    if stat.S_ISLNK(releases_info.st_mode) or not stat.S_ISDIR(releases_info.st_mode):
        raise ReleaseError("releases must be a real directory")
    _digest, directory = _resolve_release(root_path, release_id)
    if directory.parent != root_path / "releases" or directory.name != release_id:
        raise ReleaseError("release directory is not a direct content-hash child")
    try:
        directory_info = directory.lstat()
    except OSError as exc:
        raise ReleaseError(f"release does not exist: {release_id}") from exc
    if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode):
        raise ReleaseError(f"release does not exist: {release_id}")
    return directory


def current_release_id(root: str | Path) -> str:
    """Resolve ``current`` once into an immutable, independently verified id."""
    root_path = Path(root).expanduser().absolute()
    release_id = _pointer_hash(root_path, "current")
    if release_id is None:
        raise ReleaseError("no current release")
    release_path(root_path, release_id)
    return release_id


def loaded_release_id(root: str | Path, module_file: str | Path) -> str | None:
    """Identify the release that loaded a module without trusting environment input.

    A pinned long-running process keeps submitting work to its own release after
    ``current`` changes.  A newly started stable CLI is loaded from the new current
    release, so the same rule naturally assigns new work to the new release.
    """
    root_path = Path(root).expanduser().absolute()
    path = Path(module_file).absolute()
    try:
        relative = path.relative_to(root_path / "releases")
    except ValueError:
        return None
    if len(relative.parts) < 4 or relative.parts[1:3] != ("lib", "codex_seats"):
        return None
    release_id = validate_release_id(relative.parts[0])
    release_path(root_path, release_id)
    return release_id


def release_lib_path(
    root: str | Path, release_id: str, *, module: str | None = None
) -> Path:
    """Return a verified release lib path, rejecting symlinked code components."""
    release = release_path(root, release_id)
    lib = release / "lib"
    package = lib / "codex_seats"
    for path, label in ((lib, "release lib"), (package, "release package")):
        try:
            info = path.lstat()
        except OSError as exc:
            raise ReleaseError(f"{label} is unavailable: {exc}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ReleaseError(f"{label} must be a real directory")
    if module is not None:
        if not re.fullmatch(r"codex_seats(?:\.[A-Za-z_][A-Za-z0-9_]*)*", module):
            raise ReleaseError("release module must be inside codex_seats")
        target = lib.joinpath(*module.split(".")).with_suffix(".py")
        if not target.is_file() or target.is_symlink():
            raise ReleaseError(f"release module is unavailable: {module}")
    return lib


_RUN_MODULE = (
    "import runpy,sys;"
    "sys.path.insert(0,sys.argv.pop(1));"
    "runpy.run_module(sys.argv.pop(1),run_name='__main__')"
)


def release_module_argv(
    root: str | Path,
    release_id: str,
    module: str,
    *arguments: str,
    python: str | Path | None = None,
) -> list[str]:
    """Build an isolated interpreter command for a release-specific module."""
    lib = release_lib_path(root, release_id, module=module)
    return [
        str(python or sys.executable),
        "-I",
        "-c",
        _RUN_MODULE,
        str(lib),
        module,
        *arguments,
    ]


def release_environment(
    root: str | Path,
    release_id: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a child environment pinned to verified code and shared runtime data."""
    release_id = validate_release_id(release_id)
    release_path(root, release_id)
    result = dict(os.environ if environ is None else environ)
    result.pop("PYTHONHOME", None)
    result.pop("PYTHONPATH", None)
    result["CODEX_SEATS_ROOT"] = str(Path(root).expanduser().absolute())
    result["CODEX_SEATS_RELEASE_ID"] = release_id
    return result


def protocol_issues(
    manifest: Mapping[str, Any],
    *,
    current_protocols: Mapping[str, int] = CURRENT_PROTOCOLS,
) -> list[str]:
    compatibility = manifest.get("protocol_compatibility")
    if not isinstance(compatibility, Mapping):
        return ["manifest has no protocol compatibility map"]
    issues: list[str] = []
    for name, version in current_protocols.items():
        supported = compatibility.get(name)
        if not isinstance(supported, Mapping):
            issues.append(f"release does not declare compatibility with {name}")
            continue
        minimum, maximum = supported.get("min"), supported.get("max")
        if not isinstance(minimum, int) or not isinstance(maximum, int):
            issues.append(f"release has an invalid {name} compatibility range")
        elif not minimum <= version <= maximum:
            issues.append(
                f"{name} protocol {version} is outside release range {minimum}..{maximum}"
            )
    unknown = sorted(set(compatibility) - set(current_protocols))
    issues.extend(
        f"runtime does not understand required protocol {name}" for name in unknown
    )
    return issues


def _pid_active(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH


def _active_pid_blockers(
    payload: Mapping[str, Any],
    pid_is_active: Callable[[int], bool],
) -> list[str]:
    blockers: list[str] = []
    collections = (
        ("goal", payload.get("goal_queue")),
        ("campaign", payload.get("campaign_queue")),
        ("campaign", payload.get("campaigns")),
        ("campaign", payload.get("runs")),
    )
    for kind, entries in collections:
        values = entries.values() if isinstance(entries, Mapping) else entries
        if not isinstance(values, (list, tuple)) and not hasattr(values, "__iter__"):
            continue
        for index, entry in enumerate(values):
            if not isinstance(entry, Mapping) or entry.get("status") in TERMINAL_STATES:
                continue
            pid = entry.get("pid")
            if (
                isinstance(pid, int)
                and not isinstance(pid, bool)
                and pid > 0
                and pid_is_active(pid)
            ):
                identity = (
                    entry.get("id")
                    or entry.get("goal_id")
                    or entry.get("run_id")
                    or index
                )
                blockers.append(f"active {kind} pid {pid} ({identity})")
    for key in ("goal_pid", "campaign_pid"):
        pid = payload.get(key)
        if (
            isinstance(pid, int)
            and not isinstance(pid, bool)
            and pid > 0
            and pid_is_active(pid)
        ):
            blockers.append(f"active {key.replace('_', ' ')} {pid}")
    return blockers


def _runner_lock_active(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return True
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return True
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def preflight(
    root: str | Path,
    *,
    bootstrap: bool | None = None,
    pid_is_active: Callable[[int], bool] = _pid_active,
) -> PreflightReport:
    """Read runtime state without repairing, locking, creating, or deleting it."""
    root_path = Path(root).expanduser()
    is_bootstrap = (
        _pointer_hash(root_path, "current") is None if bootstrap is None else bootstrap
    )
    if not is_bootstrap:
        return PreflightReport(bootstrap=False, blockers=())

    blockers: list[str] = []
    ledger_path = root_path / "seats.json"
    if ledger_path.is_symlink():
        blockers.append(
            "cannot safely inspect tracked runtime state: seats.json is a symlink"
        )
    elif ledger_path.exists():
        try:
            payload = json.loads(ledger_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("top level is not an object")
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            blockers.append(f"cannot safely inspect tracked runtime state: {exc}")
        else:
            seats = payload.get("seats")
            if isinstance(seats, Mapping):
                for seat_id, seat in seats.items():
                    if isinstance(seat, Mapping) and seat.get("in_flight"):
                        blockers.append(f"tracked seat claim: {seat_id}")
            providers = payload.get("providers")
            if isinstance(providers, Mapping):
                for provider_id, provider in providers.items():
                    if isinstance(provider, Mapping) and provider.get("in_flight"):
                        blockers.append(f"tracked provider claim: {provider_id}")
            blockers.extend(_active_pid_blockers(payload, pid_is_active))

    dispatch = root_path / "dispatch"
    for queue_name in ("inbox", "processing"):
        queue = dispatch / queue_name
        try:
            if queue.is_symlink():
                blockers.append(
                    f"cannot safely inspect dispatch {queue_name}: path is a symlink"
                )
            elif queue.is_dir() and any(queue.iterdir()):
                blockers.append(f"dispatch {queue_name} contains frame(s)")
        except OSError as exc:
            blockers.append(f"cannot safely inspect dispatch {queue_name}: {exc}")
    if _runner_lock_active(dispatch / "runner.lock"):
        blockers.append("dispatch runner lock is active")
    return PreflightReport(bootstrap=True, blockers=tuple(blockers))


def _atomic_pointer(root: Path, name: str, digest: str) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = root / f".{name}.{os.getpid()}.{os.urandom(6).hex()}"
    try:
        os.symlink(f"releases/{digest}", temporary)
        os.replace(temporary, root / name)
        directory_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def module_regressions(root: str | Path, digest: str) -> tuple[str, ...]:
    """Python modules the ACTIVE release ships that ``digest`` does not.

    Content attestation (``verify_release``) proves a release is internally intact, and
    ``preflight`` proves the runtime is safe to switch — neither notices that the
    candidate is missing half the code. Staging from the wrong source tree therefore
    produced a release that verified, preflighted, and activated cleanly while silently
    dropping seven modules, ``releases.py`` among them: the very code needed to roll the
    mistake back. Feature parity is a different property from integrity, so it gets its
    own check.

    Additions are fine — that is what a new release is for. Only DISAPPEARANCES are
    reported. Empty when there is no active release to compare against.
    """
    root_path = Path(root).expanduser()
    current = _pointer_hash(root_path, "current")
    if current is None or current == digest:
        return ()

    def modules(release_id: str) -> set[str]:
        package = root_path / "releases" / release_id / "lib" / "codex_seats"
        if not package.is_dir():
            return set()
        return {p.name for p in package.glob("*.py")}

    return tuple(sorted(modules(current) - modules(digest)))


def normalize_module_removals(values: Any) -> tuple[str, ...]:
    """Validate repeatable package basenames without accepting path syntax."""
    if values is None:
        return ()
    if isinstance(values, str):
        values = [values]
    try:
        names = list(values)
    except TypeError as exc:
        raise ReleaseError("module removal authorizations must be module basenames") from exc
    normalized: list[str] = []
    for value in names:
        if not isinstance(value, str) or not MODULE_BASENAME_RE.fullmatch(value):
            raise ReleaseError(
                f"invalid module removal {value!r}; use one package basename such as "
                "head_permissions.py"
            )
        if Path(value).name != value:
            raise ReleaseError(f"invalid module removal path: {value!r}")
        if value in normalized:
            raise ReleaseError(f"duplicate module removal authorization: {value}")
        normalized.append(value)
    return tuple(normalized)


def validate_module_removals(
    root: str | Path, digest: str, allowed: Any = ()
) -> tuple[str, ...]:
    """Require authorization to equal the candidate's exact missing-module set."""
    requested = set(normalize_module_removals(allowed))
    missing = set(module_regressions(root, digest))
    unapproved = sorted(missing - requested)
    unnecessary = sorted(requested - missing)
    if unapproved or unnecessary:
        details: list[str] = []
        if unapproved:
            details.append("unapproved missing modules: " + ", ".join(unapproved))
        if unnecessary:
            details.append("authorized modules are not missing: " + ", ".join(unnecessary))
        raise ReleaseError("module removal authorization refused: " + "; ".join(details))
    return tuple(sorted(missing))


def activate(
    root: str | Path,
    release: str,
    *,
    confirm_heads_stopped: bool = False,
    allow_module_removals: Any = (),
) -> dict[str, Any]:
    """Verify/preflight and atomically point new work at ``release``."""
    root_path = Path(root).expanduser()
    digest, _directory = _resolve_release(root_path, release)
    manifest = verify_release(root_path, digest)
    issues = protocol_issues(manifest)
    if issues:
        raise ReleaseError("incompatible release: " + "; ".join(issues))
    validate_module_removals(root_path, digest, allow_module_removals)
    report = preflight(root_path)
    if not report.ok:
        raise ReleaseError("activation preflight failed: " + "; ".join(report.blockers))
    if report.bootstrap and not confirm_heads_stopped:
        raise ReleaseError(
            "bootstrap activation requires --confirm-heads-stopped after a trusted "
            "operator verifies every preexisting interactive head is stopped"
        )
    old = _pointer_hash(root_path, "current")
    if old == digest:
        return {
            "current": digest,
            "previous": _pointer_hash(root_path, "previous"),
            "changed": False,
        }
    if old is not None:
        _atomic_pointer(root_path, "previous", old)
    _atomic_pointer(root_path, "current", digest)
    return {"current": digest, "previous": old, "changed": True}


def rollback(root: str | Path) -> dict[str, Any]:
    """Swap current/previous pointers; already-running work is untouched."""
    root_path = Path(root).expanduser()
    current = _pointer_hash(root_path, "current")
    previous = _pointer_hash(root_path, "previous")
    if current is None or previous is None:
        raise ReleaseError("rollback requires both current and previous releases")
    manifest = verify_release(root_path, previous)
    issues = protocol_issues(manifest)
    if issues:
        raise ReleaseError("incompatible rollback release: " + "; ".join(issues))
    _atomic_pointer(root_path, "previous", current)
    _atomic_pointer(root_path, "current", previous)
    return {"current": previous, "previous": current, "changed": True}


def status(root: str | Path) -> dict[str, Any]:
    """Return release state without creating or mutating runtime paths."""
    root_path = Path(root).expanduser()
    current = _pointer_hash(root_path, "current")
    previous = _pointer_hash(root_path, "previous")
    releases_path = root_path / "releases"
    published: list[str] = []
    if releases_path.is_dir():
        published = sorted(
            child.name
            for child in releases_path.iterdir()
            if child.is_dir()
            and not child.is_symlink()
            and len(child.name) == 64
            and all(character in "0123456789abcdef" for character in child.name)
        )
    return {
        "current": current,
        "previous": previous,
        "releases": published,
        "bootstrap": current is None,
    }


def _bootstrap_sources(root: Path, release_id: str) -> dict[str, bytes]:
    verify_release(root, release_id)
    release = release_path(root, release_id)
    result: dict[str, bytes] = {}
    for name in ("codex-seat", "worker-hook"):
        source = release / "bootstrap" / name
        try:
            info = source.lstat()
        except OSError as exc:
            raise ReleaseError(
                f"release bootstrap source is unavailable: {source}"
            ) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ReleaseError(
                f"release bootstrap source must be a regular file: {source}"
            )
        payload = source.read_bytes()
        if not payload.startswith(b"#!/usr/bin/env python3\n"):
            raise ReleaseError(
                f"release bootstrap source has no trusted shebang: {source}"
            )
        result[name] = payload
    return result


def preflight_bootstrap_shims(
    root: str | Path,
    *,
    confirm_heads_stopped: bool = False,
) -> dict[str, Any]:
    """Plan an explicit stable-shim install without writing any file."""
    root_path = Path(root).expanduser().absolute()
    install_path = root_path
    if not install_path.is_dir() or install_path.is_symlink():
        raise ReleaseError(f"shim target must be a real directory: {install_path}")
    release_id = current_release_id(root_path)
    sources = _bootstrap_sources(root_path, release_id)
    targets: dict[str, dict[str, Any]] = {}
    first_bootstrap = False
    for name, source in sources.items():
        target = install_path / name
        if target.is_symlink():
            raise ReleaseError(
                f"refusing to replace symlink bootstrap target: {target}"
            )
        try:
            info = target.lstat()
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise ReleaseError(
                f"cannot inspect bootstrap target {target}: {exc}"
            ) from exc
        else:
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ReleaseError(
                    f"bootstrap target must be an owner-controlled regular file: {target}"
                )
            existing = target.read_bytes()
        current = existing == source
        stable = bool(
            existing and b"codex-seats immutable bootstrap shim v" in existing[:256]
        )
        first_bootstrap = first_bootstrap or (not current and not stable)
        targets[name] = {
            "path": str(target),
            "current": current,
            "stable": stable,
        }
    blockers: list[str] = []
    if first_bootstrap:
        report = preflight(root_path, bootstrap=True)
        blockers.extend(report.blockers)
        if not confirm_heads_stopped:
            blockers.append(
                "trusted-operator --confirm-heads-stopped confirmation that every "
                "preexisting interactive head is stopped is required"
            )
    if blockers:
        raise ReleaseError("shim install preflight failed: " + "; ".join(blockers))
    return {
        "root": str(root_path),
        "target_dir": str(install_path),
        "release_id": release_id,
        "first_bootstrap": first_bootstrap,
        "targets": targets,
    }


def install_bootstrap_shims(
    root: str | Path,
    *,
    dry_run: bool = False,
    confirm_heads_stopped: bool = False,
) -> dict[str, Any]:
    """Atomically install stable shims after explicit drained-bootstrap approval."""
    plan = preflight_bootstrap_shims(
        root,
        confirm_heads_stopped=confirm_heads_stopped,
    )
    plan["dry_run"] = bool(dry_run)
    plan["written"] = []
    if dry_run:
        return plan
    root_path = Path(plan["root"])
    sources = _bootstrap_sources(root_path, plan["release_id"])
    for name, source in sources.items():
        if plan["targets"][name]["current"]:
            continue
        target = Path(plan["targets"][name]["path"])
        temporary = (
            target.parent / f".{target.name}.{os.getpid()}.{os.urandom(6).hex()}"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(temporary, flags, 0o755)
            try:
                os.fchmod(descriptor, 0o755)
                _write_all(descriptor, source)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, target)
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
        plan["written"].append(str(target))
    return plan


def _protocol_arguments(values: list[str]) -> Mapping[str, Any] | None:
    if not values:
        return None
    result = {
        name: {"min": bounds["min"], "max": bounds["max"]}
        for name, bounds in DEFAULT_COMPATIBILITY.items()
    }
    supplied: set[str] = set()
    for value in values:
        try:
            name, raw_range = value.split("=", 1)
            if ":" in raw_range:
                raw_minimum, raw_maximum = raw_range.split(":", 1)
            else:
                raw_minimum = raw_maximum = raw_range
            minimum, maximum = int(raw_minimum), int(raw_maximum)
        except (TypeError, ValueError) as exc:
            raise ReleaseError(
                f"protocol must be NAME=VERSION or NAME=MIN:MAX: {value!r}"
            ) from exc
        if name in supplied:
            raise ReleaseError(f"protocol was specified more than once: {name}")
        supplied.add(name)
        result[name] = {"min": minimum, "max": maximum}
    return result


def register(subparsers: Any) -> None:
    """Register the ``codex-seat release`` command family."""
    release = subparsers.add_parser(
        "release", help="stage and manage immutable content-addressed releases"
    )
    commands = release.add_subparsers(dest="release_action", required=True)

    stage_parser = commands.add_parser("stage", help="stage a clean Git source")
    stage_parser.add_argument("source", help="clean Git repository root to snapshot")
    stage_parser.add_argument(
        "--protocol",
        action="append",
        default=[],
        metavar="NAME=MIN:MAX",
        help="override a protocol compatibility range (repeatable)",
    )

    verify_parser = commands.add_parser(
        "verify", help="verify immutable release contents"
    )
    verify_parser.add_argument("release", nargs="?", default="current")

    preflight_parser = commands.add_parser(
        "preflight", help="read-only activation safety check"
    )
    preflight_parser.add_argument(
        "--bootstrap",
        action="store_const",
        const=True,
        default=None,
        help="force bootstrap checks even when current already exists",
    )

    commands.add_parser("status", help="show current, previous, and staged releases")
    activate_parser = commands.add_parser(
        "activate", help="verify and switch new work to a release"
    )
    activate_parser.add_argument("release", help="64-character manifest hash")
    activate_parser.add_argument(
        "--confirm-heads-stopped",
        action="store_true",
        help=(
            "trusted-operator confirmation that every preexisting interactive head "
            "is stopped; required for the first bootstrap activation"
        ),
    )
    activate_parser.add_argument(
        "--allow-module-removal",
        action="append",
        default=[],
        metavar="NAME",
        help="authorize one exact missing package basename (repeatable)",
    )
    shims_parser = commands.add_parser(
        "install-shims",
        help="explicitly install stable release-resolving bootstrap shims",
    )
    shims_parser.add_argument("--dry-run", action="store_true")
    shims_parser.add_argument(
        "--confirm-heads-stopped",
        action="store_true",
        help=(
            "trusted-operator confirmation that every preexisting interactive head "
            "is stopped; required for the first shim bootstrap"
        ),
    )
    commands.add_parser("rollback", help="switch new work back to the previous release")
    upgrade_parser = commands.add_parser(
        "upgrade",
        help="transactionally stage, activate, and reconcile a clean Git source",
    )
    upgrade_parser.add_argument(
        "source", help="clean Git repository root to upgrade from"
    )
    upgrade_parser.add_argument(
        "--confirm-heads-stopped",
        action="store_true",
        help=(
            "trusted-operator confirmation that every preexisting interactive head "
            "is stopped; required for a first bootstrap"
        ),
    )
    upgrade_parser.add_argument(
        "--allow-module-removal",
        action="append",
        default=[],
        metavar="NAME",
        help="authorize one exact missing package basename (repeatable)",
    )
    commands.add_parser(
        "upgrade-status", help="read-only status for an incomplete upgrade transaction"
    )
    commands.add_parser(
        "upgrade-recover", help="explicitly restore an incomplete upgrade transaction"
    )
    for parser in commands.choices.values():
        parser.set_defaults(handler="release")


def cmd_release(args: Any, root: str | Path) -> int:
    """CLI adapter; all behavior remains in the testable functions above."""
    try:
        if args.release_action == "stage":
            digest = stage_release(
                root,
                args.source,
                protocol_compatibility=_protocol_arguments(args.protocol),
            )
            result: Any = {
                "release": digest,
                "path": str(Path(root) / "releases" / digest),
            }
        elif args.release_action == "verify":
            manifest = verify_release(root, args.release)
            digest, _ = _resolve_release(Path(root), args.release)
            result = {
                "ok": True,
                "release": digest,
                "source": manifest["source"],
                "protocol_compatibility": manifest["protocol_compatibility"],
            }
        elif args.release_action == "preflight":
            report = preflight(root, bootstrap=args.bootstrap)
            print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
            return 0 if report.ok else 1
        elif args.release_action == "status":
            result = status(root)
        elif args.release_action == "activate":
            result = activate(
                root,
                args.release,
                confirm_heads_stopped=args.confirm_heads_stopped,
                allow_module_removals=getattr(args, "allow_module_removal", []),
            )
        elif args.release_action == "install-shims":
            result = install_bootstrap_shims(
                root,
                dry_run=args.dry_run,
                confirm_heads_stopped=args.confirm_heads_stopped,
            )
        elif args.release_action == "rollback":
            result = rollback(root)
        elif args.release_action == "upgrade-status":
            from .upgrade import upgrade_status

            result = upgrade_status(root)
        elif args.release_action in ("upgrade", "upgrade-recover"):
            from .upgrade import UpgradeError, recover, upgrade

            try:
                if args.release_action == "upgrade":
                    result = upgrade(
                        root,
                        args.source,
                        confirm_heads_stopped=args.confirm_heads_stopped,
                        allow_module_removals=args.allow_module_removal,
                    )
                else:
                    result = recover(root)
            except (UpgradeError, ReleaseError, RuntimeError) as exc:
                failure = getattr(exc, "result", None) or {
                    "ok": False,
                    "action": args.release_action,
                    "phase": (
                        "pre-activation"
                        if args.release_action == "upgrade"
                        else "recovery"
                    ),
                    "error": str(exc),
                }
                print(json.dumps(failure, indent=2, sort_keys=True), file=sys.stderr)
                return 1
        else:
            raise ReleaseError(f"unknown release action: {args.release_action}")
    except (ReleaseError, RuntimeError) as exc:
        print(f"release {args.release_action} refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0
