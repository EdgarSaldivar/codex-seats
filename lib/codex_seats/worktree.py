"""Marker-owned throwaway Git worktrees used for managed execution isolation."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import internal_git
from . import worktree_layout


GIT_TIMEOUT_SECONDS = 120.0
MARKER_NAME = "codex-seats-owner.json"
MARKER_SCHEMA = 1
MAX_MARKER_BYTES = 4096


class WorktreeError(RuntimeError):
    """A worktree operation was unsafe or failed."""


def _git(
    repo: str | Path,
    *args: str,
    check: bool = True,
    overrides: internal_git.GitOverrides | None = None,
) -> internal_git.GitResult:
    try:
        result = internal_git.run(
            repo,
            args,
            timeout=GIT_TIMEOUT_SECONDS,
            overrides=overrides,
        )
    except internal_git.GitError as exc:
        raise WorktreeError(str(exc)) from exc
    if check and result.returncode:
        detail = (result.stderr or result.stdout).decode("utf-8", "replace")
        if detail.endswith("\n"):
            detail = detail[:-1]
        raise WorktreeError(
            f"git {' '.join(args)} failed: {detail or f'exit {result.returncode}'}"
        )
    return result


def _line(result: internal_git.GitResult, label: str) -> str:
    try:
        return internal_git.line(result.stdout, label=label)
    except ValueError as exc:
        raise WorktreeError(str(exc)) from exc


def _safe_name(branch: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in branch) or "worktree"


def _canonical_existing(path: str | Path) -> Path:
    """Resolve an existing path without forcing a sandbox walk above the cwd."""
    absolute = Path(path).expanduser().absolute()
    try:
        relative = absolute.relative_to(Path.cwd())
    except ValueError:
        candidate = absolute
    else:
        candidate = relative
    return candidate.resolve(strict=True)


def _canonical_marker(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_marker(admin: Path, value: dict[str, Any]) -> None:
    data = _canonical_marker(value)
    if len(data) > MAX_MARKER_BYTES:
        raise WorktreeError("worktree ownership marker exceeds its size limit")
    marker = admin / MARKER_NAME
    temporary = admin / f".{MARKER_NAME}.{os.getpid()}.{os.urandom(8).hex()}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    published = False
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise WorktreeError("ownership marker write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        # link(2) is an atomic no-clobber publication.  The temporary name is then
        # removed so the published marker has exactly one link.
        os.link(temporary, marker, follow_symlinks=False)
        published = True
        temporary.unlink()
        _fsync_directory(admin)
    except Exception:
        temporary.unlink(missing_ok=True)
        if published:
            marker.unlink(missing_ok=True)
        raise


def _read_marker(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise WorktreeError(f"ownership marker is missing or unreadable: {exc}") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_size > MAX_MARKER_BYTES
    ):
        raise WorktreeError("ownership marker is not an owner-only single-link regular file")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
    except OSError as exc:
        raise WorktreeError("ownership marker changed while it was being opened") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size != before.st_size
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise WorktreeError("ownership marker changed while it was being opened")
        chunks: list[bytes] = []
        remaining = MAX_MARKER_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 4096))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        raise WorktreeError("ownership marker changed while it was being read") from exc
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    if len(data) > MAX_MARKER_BYTES:
        raise WorktreeError("ownership marker exceeds its size limit")
    try:
        value = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WorktreeError("ownership marker is not valid JSON") from exc
    if not isinstance(value, dict) or data != _canonical_marker(value):
        raise WorktreeError("ownership marker is not canonical JSON")
    return value, data


def _registrations(repo: Path) -> list[dict[str, str]]:
    raw = _git(repo, "worktree", "list", "--porcelain", "-z").stdout
    registrations: list[dict[str, str]] = []
    for encoded_record in raw.split(b"\0\0"):
        if not encoded_record:
            continue
        record: dict[str, str] = {}
        for encoded_field in encoded_record.split(b"\0"):
            key, separator, value = encoded_field.partition(b" ")
            record[key.decode("ascii", "strict")] = (
                value.decode("utf-8", "surrogateescape") if separator else ""
            )
        registrations.append(record)
    return registrations


def _registration(repo: Path, path: Path) -> dict[str, str] | None:
    matches = [
        record
        for record in _registrations(repo)
        if record.get("worktree") == os.fspath(path)
    ]
    if len(matches) > 1:
        raise WorktreeError("Git registered the worktree path more than once")
    return matches[0] if matches else None


def _validate_explicit_parent(path: Path) -> Path:
    expanded = path.expanduser()
    absolute = expanded if expanded.is_absolute() else Path.cwd() / expanded
    try:
        parent = _canonical_existing(absolute.parent)
        info = absolute.parent.lstat()
    except (OSError, RuntimeError) as exc:
        raise WorktreeError(f"worktree parent is unavailable: {absolute.parent}") from exc
    if (
        parent != absolute.parent.absolute()
        or not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise WorktreeError("worktree parent must be a canonical owner-controlled directory")
    return parent / absolute.name


@dataclass
class Worktree:
    repo: Path
    path: Path
    branch: str
    base: str
    link: tuple[str, ...] = ()
    _ownership: dict[str, Any] | None = field(default=None, repr=False, compare=False)
    _temporary_parent: Path | None = field(default=None, repr=False, compare=False)
    _removed: bool = field(default=False, init=False, repr=False, compare=False)

    def _full_change(self, *, name_only: bool) -> str:
        # Managed parents are outside future broad temp/cache grants. Keeping this
        # controller-only scratch index beside the checkout also prevents a peer with
        # broad OS-temp access from racing ChangeSet capture.
        index_parent = self._temporary_parent or self.path.parent
        handle, index_name = tempfile.mkstemp(
            prefix=".codex-seat-wt-index-", dir=index_parent
        )
        os.close(handle)
        os.unlink(index_name)
        index = Path(index_name).absolute()
        overrides = internal_git.GitOverrides(index_file=index)
        try:
            _git(self.path, "read-tree", self.base, overrides=overrides)
            _git(self.path, "add", "-A", "--", ".", overrides=overrides)
            if self.link:
                _git(self.path, "reset", "-q", "--", *self.link, overrides=overrides)
            args = [
                "diff",
                "--no-ext-diff",
                "--cached",
                "--binary",
                "--full-index",
                "--find-renames",
            ]
            if name_only:
                args.append("--name-only")
            args.append(self.base)
            return _git(self.path, *args, overrides=overrides).stdout.decode(
                "utf-8", "surrogateescape"
            )
        finally:
            index.unlink(missing_ok=True)

    def diff(self) -> str:
        return self._full_change(name_only=False)

    def changed_files(self) -> list[str]:
        out = self._full_change(name_only=True)
        return sorted({line for line in out.splitlines() if line})

    def restore_paths(self, paths: tuple[str, ...] | list[str]) -> list[str]:
        tampered: list[str] = []
        for entry in paths:
            target = self.path / entry
            at_base = (
                _git(self.path, "cat-file", "-e", f"{self.base}:{entry}", check=False).returncode
                == 0
            )
            if at_base:
                changed = (
                    _git(
                        self.path,
                        "diff",
                        "--no-ext-diff",
                        "--quiet",
                        self.base,
                        "--",
                        entry,
                        check=False,
                    ).returncode
                    != 0
                )
                if changed:
                    tampered.append(entry)
                    _git(self.path, "checkout", self.base, "--", entry)
                others = _git(
                    self.path,
                    "ls-files",
                    "--others",
                    "--exclude-standard",
                    "--",
                    entry,
                    check=False,
                )
                untracked = [line for line in others.stdout.decode().splitlines() if line]
                if untracked:
                    if entry not in tampered:
                        tampered.append(entry)
                    for relative in untracked:
                        (self.path / relative).unlink(missing_ok=True)
            elif target.exists() or target.is_symlink():
                tampered.append(entry)
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
        return tampered

    def _verify_ownership(self) -> tuple[Path, dict[str, Any]]:
        if self._ownership is None:
            raise WorktreeError("refusing to remove a reconstructed or legacy worktree")
        expected = self._ownership
        required = {
            "schema",
            "source_repo",
            "source_git_common_dir",
            "worktree_path",
            "branch",
            "base_commit",
            "admin_dir",
        }
        if "managed_parent" in expected:
            required.add("managed_parent")
        if set(expected) != required or expected.get("schema") != MARKER_SCHEMA:
            raise WorktreeError("worktree ownership schema is invalid")
        try:
            source = _canonical_existing(self.repo)
            checkout = _canonical_existing(self.path)
        except (OSError, RuntimeError) as exc:
            raise WorktreeError("owned worktree path or source repository is unavailable") from exc
        branch_ref = f"refs/heads/{self.branch}"
        if (
            source != self.repo.expanduser().absolute()
            or checkout != self.path.expanduser().absolute()
            or expected.get("source_repo") != os.fspath(source)
            or expected.get("worktree_path") != os.fspath(checkout)
            or expected.get("branch") != branch_ref
            or expected.get("base_commit") != self.base
        ):
            raise WorktreeError("worktree object does not match its ownership marker")
        if "managed_parent" in expected and (
            expected["managed_parent"] != str(checkout.parent)
            or self._temporary_parent != checkout.parent
            or worktree_layout.managed_job_parent(checkout) != checkout.parent
        ):
            raise WorktreeError("managed parent does not match its ownership marker")
        top = Path(
            _line(_git(source, "rev-parse", "--show-toplevel"), "repository root")
        )
        top = _canonical_existing(top)
        if top != source:
            raise WorktreeError("source repository identity changed")
        common = Path(
            _line(_git(source, "rev-parse", "--path-format=absolute", "--git-common-dir"), "Git common directory")
        )
        common = _canonical_existing(common)
        if expected.get("source_git_common_dir") != os.fspath(common):
            raise WorktreeError("source repository Git identity changed")
        admin_text = _line(
            _git(checkout, "rev-parse", "--absolute-git-dir"), "worktree admin directory"
        )
        admin = _canonical_existing(admin_text)
        if admin != Path(admin_text).absolute() or expected.get("admin_dir") != os.fspath(admin):
            raise WorktreeError("worktree admin directory does not match ownership")
        marker, _data = _read_marker(admin / MARKER_NAME)
        if marker != expected:
            raise WorktreeError("ownership marker contents changed")
        registration = _registration(source, checkout)
        if registration is None:
            raise WorktreeError("owned worktree is not registered with Git")
        if registration.get("branch") != branch_ref:
            raise WorktreeError("registered worktree branch does not match ownership")
        symbolic = _line(
            _git(checkout, "symbolic-ref", "-q", "HEAD"), "worktree branch"
        )
        if symbolic != branch_ref:
            raise WorktreeError("checked-out worktree branch does not match ownership")
        return admin, marker

    def remove(self) -> None:
        if self._removed:
            return
        admin, _marker = self._verify_ownership()
        source = _canonical_existing(self.repo)
        checkout = _canonical_existing(self.path)
        _git(source, "worktree", "remove", "--force", os.fspath(checkout))
        if _registration(source, checkout) is not None:
            raise WorktreeError("Git reported removal but the worktree remains registered")
        if checkout.exists() or checkout.is_symlink() or admin.exists():
            raise WorktreeError("Git reported removal but owned worktree artifacts remain")
        _git(source, "branch", "-D", "--", self.branch)
        branch = _git(source, "show-ref", "--verify", "--quiet", f"refs/heads/{self.branch}", check=False)
        if branch.returncode == 0:
            raise WorktreeError("Git reported branch deletion but the owned branch remains")
        if self._temporary_parent is not None:
            try:
                self._temporary_parent.rmdir()
            except OSError:
                pass
        self._removed = True

    def __enter__(self) -> Worktree:
        return self

    def __exit__(self, *exc: object) -> None:
        self.remove()


def _cleanup_created(repo: Path, path: Path, branch: str, temporary_parent: Path | None) -> None:
    failures: list[str] = []
    registration = _registration(repo, path)
    if registration is not None:
        try:
            _git(repo, "worktree", "remove", "--force", os.fspath(path))
        except WorktreeError as exc:
            failures.append(str(exc))
    if _registration(repo, path) is None:
        branch_result = _git(
            repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False
        )
        if branch_result.returncode == 0:
            try:
                _git(repo, "branch", "-D", "--", branch)
            except WorktreeError as exc:
                failures.append(str(exc))
    if temporary_parent is not None:
        try:
            temporary_parent.rmdir()
        except OSError:
            pass
    if failures:
        raise WorktreeError("transactional worktree cleanup failed: " + "; ".join(failures))


def create(
    repo: str | Path,
    branch: str,
    *,
    path: str | Path | None = None,
    base: str = "HEAD",
    link: tuple[str, ...] | list[str] = (),
    _managed_parent: Path | None = None,
) -> Worktree:
    if not isinstance(branch, str) or not branch or "\x00" in branch:
        raise WorktreeError("worktree branch must be a non-empty NUL-free string")
    try:
        source = _canonical_existing(repo)
    except (OSError, RuntimeError) as exc:
        raise WorktreeError(f"repository is unavailable: {repo}") from exc
    inside = _git(source, "rev-parse", "--is-inside-work-tree", check=False)
    if inside.returncode or _line(inside, "repository status") != "true":
        raise WorktreeError(f"{source} is not a git repository")
    top = _canonical_existing(
        Path(_line(_git(source, "rev-parse", "--show-toplevel"), "repository root"))
    )
    if top != source:
        raise WorktreeError("source must be the exact Git worktree root")

    _git(source, "check-ref-format", "--branch", branch)
    branch_ref = f"refs/heads/{branch}"
    if _git(source, "show-ref", "--verify", "--quiet", branch_ref, check=False).returncode == 0:
        raise WorktreeError(f"worktree branch already exists: {branch}")
    base_sha = _line(
        _git(source, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"),
        "base commit",
    )
    source_common = _canonical_existing(
        Path(
            _line(
                _git(source, "rev-parse", "--path-format=absolute", "--git-common-dir"),
                "Git common directory",
            )
        )
    )

    temporary_parent: Path | None = None
    if path is None:
        temporary_parent = _canonical_existing(
            tempfile.mkdtemp(prefix="codex-seat-wt-")
        )
        target = temporary_parent / _safe_name(branch)
    else:
        target = _validate_explicit_parent(Path(path))
    if target.exists() or target.is_symlink():
        if temporary_parent is not None:
            temporary_parent.rmdir()
        raise WorktreeError(f"worktree path already exists: {target}")

    branch_created = False
    added = False
    try:
        # Create the exact branch as its own successful step.  From this point on the
        # invocation can prove it owns that ref and may transactionally delete it;
        # a competing creator instead makes this command fail before ownership.
        _git(source, "branch", "--no-track", "--", branch, base_sha)
        branch_created = True
        result = _git(
            source,
            "worktree",
            "add",
            "--no-checkout",
            "--",
            os.fspath(target),
            branch,
            check=False,
        )
        if result.returncode:
            try:
                _cleanup_created(source, target.absolute(), branch, temporary_parent)
            except WorktreeError as cleanup:
                raise WorktreeError(
                    "worktree add failed and transactional cleanup failed: "
                    f"{cleanup}"
                )
            detail = (result.stderr or result.stdout).decode("utf-8", "replace")
            if detail.endswith("\n"):
                detail = detail[:-1]
            raise WorktreeError(f"git worktree add failed: {detail or result.returncode}")
        added = True
        checkout = _canonical_existing(target)
        admin_text = _line(
            _git(checkout, "rev-parse", "--absolute-git-dir"), "worktree admin directory"
        )
        admin = _canonical_existing(admin_text)
        if admin != Path(admin_text).absolute():
            raise WorktreeError("Git returned a non-canonical worktree admin directory")
        common = source_common
        ownership: dict[str, Any] = {
            "schema": MARKER_SCHEMA,
            "source_repo": os.fspath(source),
            "source_git_common_dir": os.fspath(common),
            "worktree_path": os.fspath(checkout),
            "branch": branch_ref,
            "base_commit": base_sha,
            "admin_dir": os.fspath(admin),
        }
        if _managed_parent is not None:
            if _managed_parent != checkout.parent:
                raise WorktreeError("managed allocation does not contain its checkout")
            ownership["managed_parent"] = str(_managed_parent)
        _write_marker(admin, ownership)
        tree = Worktree(
            source,
            checkout,
            branch,
            base_sha,
            tuple(link),
            ownership,
            _managed_parent or temporary_parent,
        )
        try:
            _git(checkout, "reset", "--hard", base_sha)
            _link_paths(tree, tree.link)
        except Exception:
            tree.remove()
            raise
        return tree
    except Exception as exc:
        if added or branch_created:
            try:
                _cleanup_created(source, target.absolute(), branch, temporary_parent)
            except WorktreeError as cleanup:
                raise WorktreeError(f"worktree creation failed: {exc}; {cleanup}") from exc
        elif temporary_parent is not None:
            try:
                temporary_parent.rmdir()
            except OSError:
                pass
        if isinstance(exc, WorktreeError):
            raise
        raise WorktreeError(f"worktree creation failed: {exc}") from exc


def create_managed(
    pool_root: str | Path,
    repo: str | Path,
    branch: str,
    *,
    base: str = "HEAD",
    link: tuple[str, ...] | list[str] = (),
) -> Worktree:
    """Create one disposable checkout in the controller's protected pool layout."""
    try:
        placement = worktree_layout.allocate_managed_placement(pool_root)
    except worktree_layout.WorktreeLayoutError as exc:
        raise WorktreeError(str(exc)) from exc
    try:
        tree = create(
            repo, branch, path=placement.checkout, base=base, link=link,
            _managed_parent=placement.job_parent,
        )
    except Exception as exc:
        try:
            placement.discard_empty()
        except worktree_layout.WorktreeLayoutError as cleanup:
            raise WorktreeError(
                f"managed worktree creation failed: {exc}; "
                f"placement cleanup failed: {cleanup}"
            ) from exc
        raise
    tree._temporary_parent = placement.job_parent
    return tree


def open_owned(repo: str | Path, path: str | Path, branch: str) -> Worktree:
    """Open a marker-owned worktree for an explicit cleanup request."""
    source = _canonical_existing(repo)
    checkout = _canonical_existing(path)
    admin_text = _line(
        _git(checkout, "rev-parse", "--absolute-git-dir"), "worktree admin directory"
    )
    admin = _canonical_existing(admin_text)
    marker, _data = _read_marker(admin / MARKER_NAME)
    tree = Worktree(
        source,
        checkout,
        branch,
        str(marker.get("base_commit", "")),
        (),
        marker,
        Path(marker["managed_parent"]) if isinstance(marker.get("managed_parent"), str) else None,
    )
    tree._verify_ownership()
    return tree


def _link_paths(tree: Worktree, link: tuple[str, ...] | list[str]) -> None:
    for entry in link:
        source = (tree.repo / entry).resolve()
        if not source.exists():
            continue
        target = tree.path / entry
        if target.exists() or target.is_symlink():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(source, target)
