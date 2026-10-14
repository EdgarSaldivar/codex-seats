"""Conservative, byte-preserving updates to the primary Codex config."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path

import tomllib

FULL_ACCESS = ":danger-full-access"
_DEFAULT_ASSIGNMENT = re.compile(
    rb"^(?P<prefix>[ \t]*(?:default_permissions|\"default_permissions\"|"
    rb"'default_permissions')[ \t]*=[ \t]*)"
    rb"(?P<value>\"(?:\\.|[^\"\\\r\n])*\"|'[^'\r\n]*'|[^#\r\n]*?)"
    rb"(?P<suffix>[ \t]*(?:#[^\r\n]*)?)(?P<newline>\r?\n|$)",
    re.MULTILINE,
)


class PrimaryConfigError(ValueError):
    """The primary config cannot be updated without guessing or losing data."""


def full_access_payload(payload: bytes) -> bytes:
    """Return *payload* with one top-level full-access default.

    Existing bytes are retained exactly except for the value token of an ordinary,
    uniquely located ``default_permissions`` assignment. A missing assignment is prepended.
    TOML that is malformed or uses a valid but non-localizable spelling is refused.
    """
    try:
        text = payload.decode("utf-8")
        config = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PrimaryConfigError(f"primary config is malformed: {exc}") from exc

    current = config.get("default_permissions")
    if current is not None and not isinstance(current, str):
        raise PrimaryConfigError("top-level default_permissions must be a string")
    conflicts = [
        name
        for name in ("sandbox_mode", "sandbox_workspace_write")
        if name in config
    ]
    if conflicts:
        raise PrimaryConfigError(
            "permission profiles cannot be combined with legacy sandbox config: "
            + ", ".join(conflicts)
        )

    if current is None:
        only_crlf = b"\r\n" in payload and b"\n" not in payload.replace(b"\r\n", b"")
        newline = b"\r\n" if only_crlf else b"\n"
        updated = b'default_permissions = ":danger-full-access"' + newline + payload
    else:
        matches = list(_DEFAULT_ASSIGNMENT.finditer(payload))
        expected = dict(config)
        expected["default_permissions"] = FULL_ACCESS
        candidates: list[bytes] = []
        for match in matches:
            candidate = (
                payload[: match.start("value")]
                + b'":danger-full-access"'
                + payload[match.end("value") :]
            )
            try:
                parsed_candidate = tomllib.loads(candidate.decode("utf-8"))
            except (UnicodeDecodeError, tomllib.TOMLDecodeError):
                continue
            if parsed_candidate == expected:
                candidates.append(candidate)
        if len(candidates) != 1:
            raise PrimaryConfigError(
                "top-level default_permissions assignment is ambiguous or unsupported"
            )
        updated = candidates[0]

    try:
        parsed = tomllib.loads(updated.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:  # defensive postcondition
        raise PrimaryConfigError(f"updated primary config would be malformed: {exc}") from exc
    if parsed.get("default_permissions") != FULL_ACCESS:
        raise PrimaryConfigError("could not set top-level default_permissions unambiguously")
    return updated


def set_primary_full_access(path: str | Path) -> bool:
    """Atomically set ``path`` to the official full-access default.

    Returns whether bytes changed. Managed workers are refused before the path is touched.
    Existing file permissions are preserved; a new config is owner-readable/writable only.
    """
    if os.environ.get("CODEX_SEATS_MANAGED_WORKER") == "1":
        raise PrimaryConfigError("managed workers cannot change the primary Codex config")

    target = Path(path).expanduser()
    if target.is_symlink():
        raise PrimaryConfigError("primary config must not be a symbolic link")
    try:
        info = target.stat()
    except FileNotFoundError:
        payload = b""
        mode = 0o600
    except OSError as exc:
        raise PrimaryConfigError(f"cannot inspect primary config: {exc}") from exc
    else:
        if not stat.S_ISREG(info.st_mode):
            raise PrimaryConfigError("primary config must be a regular file")
        mode = stat.S_IMODE(info.st_mode)
        try:
            payload = target.read_bytes()
        except OSError as exc:
            raise PrimaryConfigError(f"cannot read primary config: {exc}") from exc

    updated = full_access_payload(payload)
    if updated == payload:
        return False

    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return True
