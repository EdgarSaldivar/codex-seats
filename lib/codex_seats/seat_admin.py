"""Lifecycle-safe, non-secret mutations for schema-3 Codex seats.

This module is intentionally independent of argparse and provider commands.  A
caller supplies activation evidence after performing login and capacity probes;
the administration layer never opens an auth file or invokes a provider.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import stat
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from .seat_registry import (
    SeatRegistryError,
    active_seat_ids,
    resolve_home,
    seat_number,
    validate_capacity_profile,
    validate_capacity_source,
    validate_schema3,
    validate_seat_id,
)
from .state import (
    Ledger,
    SettingsMutationError,
    _assert_settings_source_unchanged,
    _fsync_directory,
    _load_settings_for_mutation,
    _read_settings_payload,
    _secure_settings_backup,
    _validated_settings_document,
    atomic_write_json,
    claim_alive,
    iso,
    new_seat,
    settings_lock,
    settings_path,
    task_min_headroom,
)


AUDIT_FILENAME = "seat-admin-audit.jsonl"
MUTATION_HOOKS = frozenset(
    {
        "after_mutation_reload_before_ledger_lock",
        "after_ledger_lock_before_claim_validation",
        "after_allocation_before_settings_replace",
        "after_backup_before_settings_replace",
        "before_settings_replace",
        "after_settings_replace",
        "drain_wait_without_locks",
    }
)

SeatAdminHook = Callable[[str], None]
Clock = Callable[[], float]
_UNSET = object()
_SEAT_TEXT_RE = re.compile(r"(?<![a-zA-Z0-9])seat([1-9][0-9]*)(?![a-zA-Z0-9])")


class SeatAdminError(ValueError):
    """A requested seat mutation is unsafe or violates lifecycle policy."""


@dataclass(frozen=True)
class SeatMutationResult:
    """Exact, deterministic result shared by previews, dry runs, and writes."""

    action: str
    seat_id: str
    changed: bool
    before: str
    after: str
    warnings: tuple[str, ...] = ()
    backup_path: Path | None = None
    # SHA-256 of the exact settings bytes this result was computed against. A
    # preview publishes it so a later apply can pin the same on-disk revision
    # (``expected_digest``); it is None only for callers that synthesize a
    # result without reading settings (e.g. a login placeholder).
    digest: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "seat_id": self.seat_id,
            "changed": self.changed,
            "before": self.before,
            "after": self.after,
            "warnings": list(self.warnings),
            "backup_path": (
                str(self.backup_path) if self.backup_path is not None else None
            ),
            "digest": self.digest,
        }


def _call_hook(hook: SeatAdminHook | None, phase: str) -> None:
    if hook is not None:
        hook(phase)


def _timestamp(clock: Clock) -> str:
    return iso(clock())


def _mutation_selector(selector: str, seats: Mapping[str, Any]) -> str:
    if not isinstance(selector, str) or not selector:
        raise SeatAdminError("mutation seat selector must be a seatN id or numeric shorthand")
    if selector.isascii() and selector.isdigit() and not selector.startswith("0"):
        candidate = f"seat{selector}"
    else:
        candidate = selector
    try:
        validate_seat_id(candidate)
    except SeatRegistryError as exc:
        raise SeatAdminError(
            "mutation seat selector must be a seatN id or numeric shorthand"
        ) from exc
    if candidate not in seats:
        raise SeatAdminError(f"unknown registered seat {candidate}")
    return candidate


def _hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _audit_path(root: Path) -> Path:
    return root / AUDIT_FILENAME


def _append_audit(root: Path, record: Mapping[str, Any]) -> None:
    """Append one fixed-shape sanitized record and durably flush it."""
    allowed = {
        "schema",
        "event",
        "action",
        "seat_id",
        "at",
        "before_sha256",
        "after_sha256",
    }
    if set(record) - allowed:
        raise SeatAdminError("seat-admin audit record contains unsupported fields")
    payload = (json.dumps(dict(record), sort_keys=True, separators=(",", ":")) + "\n").encode()
    path = _audit_path(root)
    flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise SeatAdminError(f"cannot append seat-admin audit: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        ):
            raise SeatAdminError(
                "seat-admin audit must be an owner-controlled, singly linked regular file"
            )
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(root)


def _ids_in_text(text: str) -> set[str]:
    return {f"seat{match.group(1)}" for match in _SEAT_TEXT_RE.finditer(text)}


def _ids_in_allocation_evidence(path: Path) -> set[str]:
    """Stream strict IDs from regular allocation history without following links."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise SeatAdminError(
                f"allocation evidence must be an owner-controlled, singly linked "
                f"regular file: {path}"
            )
        found: set[str] = set()
        carry = ""
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            text = carry + chunk.decode("utf-8", errors="ignore")
            found.update(_ids_in_text(text))
            # Keep enough overlap for every seat number Python can safely parse;
            # a token split at the read boundary must never become reusable.
            carry = text[-8192:]
        found.update(_ids_in_text(carry))
        return found
    finally:
        os.close(descriptor)


def _ever_used_ids(
    root: Path, settings: Mapping[str, Any], ledger: Mapping[str, Any]
) -> set[str]:
    """Collect rollback-immune allocation evidence without opening auth files."""
    used = set(settings.get("pool", {}).get("seats", {}))
    seats = ledger.get("seats", {})
    if isinstance(seats, Mapping):
        used.update(key for key in seats if isinstance(key, str))

    # Live job attribution may use seat, seat_id, origin_seat, or later additive
    # names.  Scanning serialized job objects is conservative and cannot expose
    # credentials because the ledger job history is already sanitized state.
    jobs = ledger.get("jobs", [])
    if isinstance(jobs, list):
        used.update(_ids_in_text(json.dumps(jobs, sort_keys=True, default=str)))

    audit = _audit_path(root)
    try:
        used.update(_ids_in_allocation_evidence(audit))
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise SeatAdminError(f"cannot safely read allocation audit: {exc}") from exc

    archive = root / "archive"
    try:
        archive_info = archive.lstat()
        if not stat.S_ISDIR(archive_info.st_mode) or stat.S_ISLNK(archive_info.st_mode):
            raise SeatAdminError(
                "job archive must be a real directory before allocating a seat"
            )
        archived_jobs = sorted(archive.glob("jobs-*.jsonl"))
    except FileNotFoundError:
        archived_jobs = []
    except OSError as exc:
        raise SeatAdminError(f"cannot safely inspect archived jobs: {exc}") from exc
    for path in archived_jobs:
        try:
            if path.is_file() and not path.is_symlink():
                used.update(_ids_in_allocation_evidence(path))
            else:
                raise SeatAdminError(
                    f"archived job attribution must be a regular file: {path}"
                )
        except OSError as exc:
            raise SeatAdminError(f"cannot safely read archived jobs: {exc}") from exc

    # Any exact seatN entry at the pool root is allocation evidence, including
    # an unsafe symlink or an inaccessible directory.  It is never traversed.
    try:
        children = list(root.iterdir())
    except OSError:
        children = []
    for child in children:
        try:
            validate_seat_id(child.name)
        except SeatRegistryError:
            continue
        used.add(child.name)

    valid: set[str] = set()
    for seat_id in used:
        try:
            validate_seat_id(seat_id)
        except SeatRegistryError:
            continue
        valid.add(seat_id)
    return valid


def next_seat_id(
    root: Path, settings: Mapping[str, Any], ledger: Mapping[str, Any]
) -> str:
    """Return max(ever-used seat number)+1 for a locked snapshot."""
    used = _ever_used_ids(root, settings, ledger)
    number = max((seat_number(seat_id) for seat_id in used), default=0) + 1
    return f"seat{number}"


def settings_digest(root: Path) -> str:
    """Return the SHA-256 of the exact on-disk settings bytes.

    This matches the digest a mutation computes inside the settings lock, so a
    caller may snapshot it advisorily (outside the lock) and pass it back as
    ``expected_digest`` for a race-safe apply. It is not itself a lock.
    """
    try:
        payload = _read_settings_payload(settings_path(Path(root)))
    except SettingsMutationError as exc:
        raise SeatAdminError(str(exc)) from exc
    return _hash(payload)


def _read_ledger_for_preview(root: Path) -> dict[str, Any]:
    """Read one coherent raw ledger snapshot without creating locks or state.

    Ledger writers publish with an atomic replacement. Pinning the opened inode
    and rejecting a concurrent path replacement therefore gives previews a
    complete old-or-new document without taking the write-oriented ``Ledger``
    context (which may repair state on exit).
    """
    path = root / "seats.json"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return {"schema": 6, "seats": {}, "jobs": []}
    except OSError as exc:
        raise SeatAdminError(f"cannot safely read ledger preview: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise SeatAdminError(
                "ledger preview must be an owner-controlled, singly linked regular file"
            )
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                raw = json.load(handle)
                after = os.fstat(handle.fileno())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SeatAdminError(f"cannot read valid ledger JSON for preview: {exc}") from exc
        stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable):
            raise SeatAdminError("ledger changed while its preview was read")
        try:
            current = path.lstat()
        except OSError as exc:
            raise SeatAdminError("ledger changed while its preview was read") from exc
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise SeatAdminError("ledger changed while its preview was read")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(raw, dict):
        raise SeatAdminError("ledger preview top level must be an object")
    seats = raw.setdefault("seats", {})
    jobs = raw.setdefault("jobs", [])
    if not isinstance(seats, dict) or not isinstance(jobs, list):
        raise SeatAdminError("ledger preview seats/jobs have invalid types")
    return raw


def _refresh_target_claim(data: dict[str, Any], seat_id: str) -> dict[str, Any]:
    seats = data.setdefault("seats", {})
    state = seats.setdefault(seat_id, new_seat())
    if not isinstance(state, dict):
        raise SeatAdminError(f"ledger seat {seat_id} must be an object")
    claim = state.get("in_flight")
    if isinstance(claim, dict) and not claim_alive(claim):
        state["in_flight"] = None
    return state


def _require_idle(data: dict[str, Any], seat_id: str) -> None:
    if _refresh_target_claim(data, seat_id).get("in_flight"):
        raise SeatAdminError(f"{seat_id} has a live worker claim; wait until it is idle")


def _verify_activation_home(
    root: Path,
    seat_id: str,
    entry: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> None:
    home = resolve_home(root, seat_id, entry)
    try:
        info = home.lstat()
    except OSError as exc:
        raise SeatAdminError(f"activation home for {seat_id} is unavailable: {home}") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise SeatAdminError(
            f"activation home for {seat_id} must be an owner-controlled, non-symlink directory"
        )
    actual = Path(os.path.realpath(home))
    try:
        actual_info = home.lstat()
    except OSError as exc:
        raise SeatAdminError(
            f"activation home for {seat_id} changed during validation: {home}"
        ) from exc
    if (
        Path(os.path.realpath(home)) != actual
        or (actual_info.st_dev, actual_info.st_ino) != (info.st_dev, info.st_ino)
    ):
        raise SeatAdminError(
            f"activation home for {seat_id} changed during validation: {home}"
        )
    for other_id in active_seat_ids(settings):
        if other_id == seat_id:
            continue
        other_entry = settings["pool"]["seats"][other_id]
        other_home = resolve_home(root, other_id, other_entry)
        try:
            other_actual = Path(os.path.realpath(other_home))
            other_info = other_home.stat()
        except OSError as exc:
            raise SeatAdminError(
                f"active home for {other_id} is unavailable during activation"
            ) from exc
        if Path(os.path.realpath(other_home)) != other_actual:
            raise SeatAdminError(
                f"active home for {other_id} changed during activation"
            )
        if actual == other_actual or (actual_info.st_dev, actual_info.st_ino) == (
            other_info.st_dev,
            other_info.st_ino,
        ):
            raise SeatAdminError(
                f"activation home for {seat_id} aliases active {other_id}: {actual}"
            )


def _role_guard(pool: dict[str, Any], seat_id: str, *, allow_empty: bool) -> None:
    active = active_seat_ids({"schema": 3, "pool": pool})
    designated = [role for role in ("primary", "deputy") if pool.get(role) == seat_id]
    final_primary = active == [seat_id] and pool.get("primary") == seat_id
    if final_primary and not allow_empty:
        raise SeatAdminError(
            f"{seat_id} is the final active primary; explicit allow_empty is required"
        )
    if designated and not (allow_empty and final_primary):
        roles = " and ".join(designated)
        raise SeatAdminError(
            f"{seat_id} is the designated {roles}; assign or clear that role first"
        )
    if allow_empty and not final_primary:
        raise SeatAdminError("allow_empty applies only to the final active primary")
    if final_primary:
        pool["primary"] = None
        pool["deputy"] = None


def _set_fields(
    entry: dict[str, Any],
    pool: dict[str, Any],
    seat_id: str,
    *,
    label: Any,
    home: Any,
    profile: Any,
    relative_capacity: Any,
    capacity_source: Any,
    reserve_pct: Any,
    small_task_min_headroom_pct: Any,
    big_task_min_headroom_pct: Any,
    clock: Clock,
) -> bool:
    capacity_requested = any(
        value is not _UNSET for value in (profile, relative_capacity, capacity_source)
    )
    if label is not _UNSET:
        entry["label"] = label
    if home is not _UNSET:
        if entry.get("home") != home and entry.get("lifecycle") != "inactive":
            raise SeatAdminError("home may be changed only while the seat is inactive")
        entry["home"] = home
    capacity_before = (
        entry.get("capacity_profile"),
        entry.get("relative_capacity"),
        entry.get("capacity_source"),
    )
    if profile is not _UNSET:
        entry["capacity_profile"] = validate_capacity_profile(profile)
    if capacity_source is not _UNSET:
        entry["capacity_source"] = validate_capacity_source(capacity_source)
    if relative_capacity is not _UNSET:
        if relative_capacity is not None:
            if isinstance(relative_capacity, bool) or not isinstance(
                relative_capacity, (int, float, Decimal)
            ):
                raise SeatAdminError(
                    "relative_capacity must be null or a finite number from 0.1 to 1000"
                )
            numeric = float(relative_capacity)
            if not math.isfinite(numeric) or not 0.1 <= numeric <= 1000:
                raise SeatAdminError(
                    "relative_capacity must be null or a finite number from 0.1 to 1000"
                )
            relative_capacity = numeric
        entry["relative_capacity"] = relative_capacity

    if capacity_requested:
        source = entry.get("capacity_source")
        capacity = entry.get("relative_capacity")
        if source == "unverified":
            if capacity_source is not _UNSET:
                entry["relative_capacity"] = None
                entry["capacity_verified_at"] = None
            elif capacity is not None:
                raise SeatAdminError(
                    "a non-null relative capacity requires a verified capacity_source"
                )
        else:
            if capacity is None:
                raise SeatAdminError(
                    "a verified capacity_source requires a non-null relative capacity"
                )
    capacity_after = (
        entry.get("capacity_profile"),
        entry.get("relative_capacity"),
        entry.get("capacity_source"),
    )
    capacity_changed = capacity_before != capacity_after
    if capacity_changed and entry.get("capacity_source") != "unverified":
        entry["capacity_verified_at"] = _timestamp(clock)

    def percentage(value: Any, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise SeatAdminError(f"{name} must be a finite number from 0 to 100")
        numeric = float(value)
        if not math.isfinite(numeric) or not 0 <= numeric <= 100:
            raise SeatAdminError(f"{name} must be a finite number from 0 to 100")
        return numeric

    if reserve_pct is not _UNSET:
        pool.setdefault("reserves_pct", {})[seat_id] = percentage(reserve_pct, "reserve_pct")

    floor_requested = any(
        value is not _UNSET
        for value in (small_task_min_headroom_pct, big_task_min_headroom_pct)
    )
    if floor_requested:
        overrides = pool.setdefault("task_min_headroom_pct_by_seat", {})
        existing = overrides.get(seat_id)
        if isinstance(existing, Mapping):
            floor = dict(existing)
        else:
            floor = {
                "small": task_min_headroom({"pool": pool}, "small"),
                "big": task_min_headroom({"pool": pool}, "big"),
            }
        if small_task_min_headroom_pct is not _UNSET:
            floor["small"] = percentage(
                small_task_min_headroom_pct, "small_task_min_headroom_pct"
            )
        if big_task_min_headroom_pct is not _UNSET:
            floor["big"] = percentage(
                big_task_min_headroom_pct, "big_task_min_headroom_pct"
            )
        overrides[seat_id] = floor
    return capacity_changed


def _apply_action(
    root: Path,
    settings: dict[str, Any],
    ledger: dict[str, Any],
    *,
    action: str,
    selector: str | None,
    options: Mapping[str, Any],
    clock: Clock,
) -> tuple[str, bool, list[str]]:
    pool = settings["pool"]
    seats = pool["seats"]
    warnings: list[str] = []
    requires_idle = False

    if action == "add":
        seat_id = str(options["allocated_seat_id"])
        profile = options.get("profile", _UNSET)
        if profile is _UNSET or profile is None:
            profile = "unverified"
        validate_capacity_profile(profile)
        entry = {
            "label": options["label"],
            "lifecycle": "inactive",
            "home": options.get("home", None),
            "capacity_profile": profile,
            "relative_capacity": None,
            "capacity_source": "unverified",
            "capacity_verified_at": None,
        }
        seats[seat_id] = entry
        return seat_id, False, warnings

    assert selector is not None
    seat_id = _mutation_selector(selector, seats)
    entry = seats[seat_id]
    if not isinstance(entry, dict):
        raise SeatAdminError(f"registry seat {seat_id} must be an object")

    if action == "set":
        requires_idle = _set_fields(
            entry,
            pool,
            seat_id,
            label=options.get("label", _UNSET),
            home=options.get("home", _UNSET),
            profile=options.get("profile", _UNSET),
            relative_capacity=options.get("relative_capacity", _UNSET),
            capacity_source=options.get("capacity_source", _UNSET),
            reserve_pct=options.get("reserve_pct", _UNSET),
            small_task_min_headroom_pct=options.get(
                "small_task_min_headroom_pct", _UNSET
            ),
            big_task_min_headroom_pct=options.get(
                "big_task_min_headroom_pct", _UNSET
            ),
            clock=clock,
        )
        if requires_idle:
            _require_idle(ledger, seat_id)
    elif action == "set-role":
        role = options.get("role")
        if role not in {"primary", "deputy", "worker"}:
            raise SeatAdminError("role must be primary, deputy, or worker")
        if entry.get("lifecycle") != "active":
            raise SeatAdminError("roles may be assigned only to active seats")
        if role == "primary":
            pool["primary"] = seat_id
            if pool.get("deputy") == seat_id:
                pool["deputy"] = None
        elif role == "deputy":
            if pool.get("primary") == seat_id:
                raise SeatAdminError(
                    "the primary cannot also be deputy; assign a replacement primary first"
                )
            pool["deputy"] = seat_id
        elif pool.get("primary") == seat_id:
            raise SeatAdminError(
                "the current primary cannot become a worker while active seats remain; "
                "assign its replacement instead"
            )
        elif pool.get("deputy") == seat_id:
            pool["deputy"] = None
    elif action == "activate":
        if entry.get("lifecycle") == "active":
            if options.get("as_primary") and pool.get("primary") != seat_id:
                pool["primary"] = seat_id
                if pool.get("deputy") == seat_id:
                    pool["deputy"] = None
            return seat_id, False, warnings
        if entry.get("lifecycle") not in {"inactive", "draining"}:
            raise SeatAdminError(
                f"cannot activate {seat_id} from lifecycle {entry.get('lifecycle')}"
            )
        if options.get("verified_login") is not True:
            raise SeatAdminError("activation requires explicit verified-login evidence")
        if options.get("successful_probe") is not True:
            raise SeatAdminError("activation requires explicit successful-probe evidence")
        verified_home = options.get("verified_home")
        if verified_home is not None:
            current_home = resolve_home(root, seat_id, entry)
            if current_home != Path(verified_home):
                raise SeatAdminError(
                    "activation evidence no longer matches the registered seat home; "
                    "repeat login-status and capacity checks"
                )
        _require_idle(ledger, seat_id)
        _verify_activation_home(root, seat_id, entry, settings)
        existing_active = active_seat_ids(settings)
        if not existing_active and options.get("as_primary") is not True:
            raise SeatAdminError(
                "activating the first seat requires explicit as_primary semantics"
            )
        entry["lifecycle"] = "active"
        if options.get("as_primary"):
            pool["primary"] = seat_id
            if pool.get("deputy") == seat_id:
                pool["deputy"] = None
        if entry.get("relative_capacity") is None:
            warnings.append(
                f"{seat_id} has unverified capacity; routing will conservatively use 1x"
            )
    elif action == "drain":
        if entry.get("lifecycle") == "draining":
            if options.get("wait_preview"):
                entry["lifecycle"] = "inactive"
            return seat_id, False, warnings
        if entry.get("lifecycle") != "active":
            raise SeatAdminError(
                f"cannot drain {seat_id} from lifecycle {entry.get('lifecycle')}"
            )
        _role_guard(pool, seat_id, allow_empty=bool(options.get("allow_empty")))
        entry["lifecycle"] = "draining"
        if options.get("wait_preview"):
            entry["lifecycle"] = "inactive"
        warnings.append(
            "lifecycle changes are prospective and do not kill or log out interactive sessions"
        )
    elif action == "deactivate":
        if entry.get("lifecycle") == "inactive":
            return seat_id, False, warnings
        if entry.get("lifecycle") not in {"active", "draining"}:
            raise SeatAdminError(
                f"cannot deactivate {seat_id} from lifecycle {entry.get('lifecycle')}"
            )
        _require_idle(ledger, seat_id)
        if entry.get("lifecycle") == "active":
            _role_guard(pool, seat_id, allow_empty=bool(options.get("allow_empty")))
        elif options.get("allow_empty"):
            raise SeatAdminError("allow_empty is unnecessary for a draining seat")
        entry["lifecycle"] = "inactive"
        warnings.append(
            "lifecycle changes are prospective and do not kill or log out interactive sessions"
        )
    elif action == "retire":
        if entry.get("lifecycle") == "retired":
            return seat_id, False, warnings
        if entry.get("lifecycle") != "inactive":
            raise SeatAdminError("retirement requires an already inactive seat")
        if pool.get("primary") == seat_id or pool.get("deputy") == seat_id:
            raise SeatAdminError("retirement requires a non-primary, non-deputy seat")
        _require_idle(ledger, seat_id)
        entry["lifecycle"] = "retired"
        warnings.append(
            "retirement preserves the seat home, credentials, and historical attribution"
        )
    else:
        raise SeatAdminError(f"unknown seat mutation action {action!r}")
    return seat_id, requires_idle, warnings


def _preview_mutation(
    root: Path,
    *,
    action: str,
    selector: str | None,
    clock: Clock,
    options: Mapping[str, Any],
) -> SeatMutationResult:
    """Compute an exact point-in-time preview without writing any pool file."""
    settings, before, payload, load_warnings = _load_settings_for_mutation(
        root, require_existing=True
    )
    validate_schema3(settings, root)
    assert before is not None and payload is not None
    ledger = _read_ledger_for_preview(root)
    mutation_options = dict(options)
    if action == "add":
        mutation_options["allocated_seat_id"] = next_seat_id(root, settings, ledger)
    candidate = copy.deepcopy(settings)
    seat_id, _requires_idle, action_warnings = _apply_action(
        root,
        candidate,
        ledger,
        action=action,
        selector=selector,
        options=mutation_options,
        clock=clock,
    )
    canonical, final_warnings = _validated_settings_document(candidate, root=root)
    validate_schema3(canonical, root)
    changed = canonical != settings
    return SeatMutationResult(
        action=action,
        seat_id=seat_id,
        changed=changed,
        before=before,
        after=json.dumps(canonical, indent=2) + "\n" if changed else before,
        warnings=tuple(
            dict.fromkeys([*load_warnings, *action_warnings, *final_warnings])
        ),
        digest=_hash(payload),
    )


def _mutate(
    root: Path,
    *,
    action: str,
    selector: str | None = None,
    dry_run: bool = False,
    hook: SeatAdminHook | None = None,
    clock: Clock = time.time,
    expected_digest: str | None = None,
    **options: Any,
) -> SeatMutationResult:
    root = Path(root)
    try:
        if dry_run:
            return _preview_mutation(
                root,
                action=action,
                selector=selector,
                clock=clock,
                options=options,
            )
        with settings_lock(root):
            settings, before, payload, load_warnings = _load_settings_for_mutation(
                root, require_existing=True
            )
            validate_schema3(settings, root)
            assert before is not None and payload is not None
            # Bind an optional preview to the exact settings bytes it was built
            # against. The comparison runs while the settings lock is held and
            # before any ledger/allocation effect, so a concurrent settings
            # change since the preview aborts cleanly with nothing written.
            if expected_digest is not None and _hash(payload) != expected_digest:
                raise SeatAdminError(
                    "settings changed since this action was previewed; re-preview "
                    "before applying"
                )
            _call_hook(hook, "after_mutation_reload_before_ledger_lock")

            ledger_manager = Ledger(root)
            with ledger_manager as ledger:
                _call_hook(hook, "after_ledger_lock_before_claim_validation")
                mutation_options = dict(options)
                allocated = action == "add"
                if allocated:
                    mutation_options["allocated_seat_id"] = next_seat_id(
                        root, settings, ledger
                    )
                    expected_seat_id = mutation_options.pop(
                        "expected_seat_id", None
                    )
                    if (
                        expected_seat_id is not None
                        and mutation_options["allocated_seat_id"] != expected_seat_id
                    ):
                        raise SeatAdminError(
                            "seat allocation changed since this action was previewed; "
                            "re-preview before applying"
                        )
                candidate = copy.deepcopy(settings)
                seat_id, _requires_idle, action_warnings = _apply_action(
                    root,
                    candidate,
                    ledger,
                    action=action,
                    selector=selector,
                    options=mutation_options,
                    clock=clock,
                )
                canonical, final_warnings = _validated_settings_document(
                    candidate, root=root
                )
                validate_schema3(canonical, root)
                logical_changed = canonical != settings
                before_hash = _hash(payload)
                if not logical_changed:
                    return SeatMutationResult(
                        action=action,
                        seat_id=seat_id,
                        changed=False,
                        before=before,
                        after=before,
                        warnings=tuple(
                            dict.fromkeys([*load_warnings, *action_warnings, *final_warnings])
                        ),
                        digest=before_hash,
                    )

                after = json.dumps(canonical, indent=2) + "\n"
                warnings = tuple(
                    dict.fromkeys([*load_warnings, *action_warnings, *final_warnings])
                )
                at = _timestamp(clock)
                if allocated:
                    runtime = ledger.setdefault("seats", {}).setdefault(
                        seat_id, new_seat()
                    )
                    if not isinstance(runtime, dict):
                        raise SeatAdminError(f"ledger seat {seat_id} must be an object")
                    runtime["seat_admin_allocated_at"] = at
                    allocations = ledger.setdefault("seat_admin_allocations", {})
                    if not isinstance(allocations, dict):
                        raise SeatAdminError("ledger seat_admin_allocations must be an object")
                    allocations[seat_id] = {"allocated_at": at}
                    _append_audit(
                        root,
                        {
                            "schema": 1,
                            "event": "allocation",
                            "action": "add",
                            "seat_id": seat_id,
                            "at": at,
                            "before_sha256": before_hash,
                        },
                    )
                    ledger_manager.flush()
                    _call_hook(hook, "after_allocation_before_settings_replace")

                backup = _secure_settings_backup(root, payload)
                _call_hook(hook, "after_backup_before_settings_replace")

                def before_replace() -> None:
                    _call_hook(hook, "before_settings_replace")
                    _assert_settings_source_unchanged(settings_path(root), payload)

                atomic_write_json(
                    settings_path(root),
                    canonical,
                    mode=0o600,
                    _before_replace=before_replace,
                )
                _call_hook(hook, "after_settings_replace")
                after_hash = _hash(after.encode())
                _append_audit(
                    root,
                    {
                        "schema": 1,
                        "event": "mutation",
                        "action": action,
                        "seat_id": seat_id,
                        "at": at,
                        "before_sha256": before_hash,
                        "after_sha256": after_hash,
                    },
                )
                return SeatMutationResult(
                    action=action,
                    seat_id=seat_id,
                    changed=True,
                    before=before,
                    after=after,
                    warnings=warnings,
                    backup_path=backup,
                    digest=before_hash,
                )
    except SeatAdminError:
        raise
    except (SeatRegistryError, SettingsMutationError, KeyError, TypeError) as exc:
        raise SeatAdminError(str(exc)) from exc


def add_seat(
    root: Path,
    *,
    label: str,
    home: str | None = None,
    profile: str | None = None,
    dry_run: bool = False,
    hook: SeatAdminHook | None = None,
    expected_digest: str | None = None,
    expected_seat_id: str | None = None,
    _clock: Clock = time.time,
) -> SeatMutationResult:
    """Allocate a new inactive seat without reusing a historical identity.

    ``expected_seat_id`` optionally binds a prior dry-run to the allocation
    evidence re-read under both transaction locks, complementing the settings
    digest guard when a caller offers an exact preview/confirm workflow.
    """
    return _mutate(
        root,
        action="add",
        label=label,
        home=home,
        profile=profile,
        dry_run=dry_run,
        hook=hook,
        expected_digest=expected_digest,
        expected_seat_id=expected_seat_id,
        clock=_clock,
    )


def set_seat(
    root: Path,
    selector: str,
    *,
    label: Any = _UNSET,
    home: Any = _UNSET,
    profile: Any = _UNSET,
    relative_capacity: Any = _UNSET,
    capacity_source: Any = _UNSET,
    reserve_pct: Any = _UNSET,
    small_task_min_headroom_pct: Any = _UNSET,
    big_task_min_headroom_pct: Any = _UNSET,
    dry_run: bool = False,
    hook: SeatAdminHook | None = None,
    expected_digest: str | None = None,
    _clock: Clock = time.time,
) -> SeatMutationResult:
    if all(
        value is _UNSET
        for value in (
            label,
            home,
            profile,
            relative_capacity,
            capacity_source,
            reserve_pct,
            small_task_min_headroom_pct,
            big_task_min_headroom_pct,
        )
    ):
        raise SeatAdminError("set requires at least one field")
    return _mutate(
        root,
        action="set",
        selector=selector,
        label=label,
        home=home,
        profile=profile,
        relative_capacity=relative_capacity,
        capacity_source=capacity_source,
        reserve_pct=reserve_pct,
        small_task_min_headroom_pct=small_task_min_headroom_pct,
        big_task_min_headroom_pct=big_task_min_headroom_pct,
        dry_run=dry_run,
        hook=hook,
        expected_digest=expected_digest,
        clock=_clock,
    )


def set_role(
    root: Path,
    selector: str,
    role: str,
    *,
    dry_run: bool = False,
    hook: SeatAdminHook | None = None,
    expected_digest: str | None = None,
) -> SeatMutationResult:
    return _mutate(
        root,
        action="set-role",
        selector=selector,
        role=role,
        dry_run=dry_run,
        hook=hook,
        expected_digest=expected_digest,
    )


def activate_seat(
    root: Path,
    selector: str,
    *,
    verified_login: bool,
    successful_probe: bool,
    verified_home: Path | str | None = None,
    as_primary: bool = False,
    dry_run: bool = False,
    hook: SeatAdminHook | None = None,
    expected_digest: str | None = None,
) -> SeatMutationResult:
    return _mutate(
        root,
        action="activate",
        selector=selector,
        verified_login=verified_login,
        successful_probe=successful_probe,
        verified_home=verified_home,
        as_primary=as_primary,
        dry_run=dry_run,
        hook=hook,
        expected_digest=expected_digest,
    )


def drain_seat(
    root: Path,
    selector: str,
    *,
    wait: bool = False,
    allow_empty: bool = False,
    wait_timeout: float | None = None,
    poll_interval: float = 0.1,
    dry_run: bool = False,
    hook: SeatAdminHook | None = None,
    expected_digest: str | None = None,
    _sleep: Callable[[float], None] = time.sleep,
    _monotonic: Callable[[], float] = time.monotonic,
) -> SeatMutationResult:
    if wait_timeout is not None and wait_timeout < 0:
        raise SeatAdminError("wait_timeout must be non-negative or null")
    if poll_interval <= 0:
        raise SeatAdminError("poll_interval must be positive")
    result = _mutate(
        root,
        action="drain",
        selector=selector,
        allow_empty=allow_empty,
        wait_preview=wait and dry_run,
        dry_run=dry_run,
        hook=hook,
        expected_digest=expected_digest,
    )
    if wait and not dry_run:
        _call_hook(hook, "drain_wait_without_locks")
        started = _monotonic()
        while True:
            ledger_manager = Ledger(Path(root))
            with ledger_manager as data:
                idle = not _refresh_target_claim(data, result.seat_id).get(
                    "in_flight"
                )
            if idle:
                break
            if wait_timeout is not None and _monotonic() - started >= wait_timeout:
                raise SeatAdminError(
                    f"timed out waiting for {result.seat_id} to become idle; "
                    "the seat remains draining"
                )
            _sleep(poll_interval)
        completed = deactivate_seat(root, result.seat_id, hook=hook)
        return SeatMutationResult(
            action="drain",
            seat_id=result.seat_id,
            changed=result.changed or completed.changed,
            before=result.before,
            after=completed.after,
            warnings=tuple(dict.fromkeys([*result.warnings, *completed.warnings])),
            backup_path=completed.backup_path or result.backup_path,
        )
    return result


def deactivate_seat(
    root: Path,
    selector: str,
    *,
    allow_empty: bool = False,
    dry_run: bool = False,
    hook: SeatAdminHook | None = None,
    expected_digest: str | None = None,
) -> SeatMutationResult:
    return _mutate(
        root,
        action="deactivate",
        selector=selector,
        allow_empty=allow_empty,
        dry_run=dry_run,
        hook=hook,
        expected_digest=expected_digest,
    )


def retire_seat(
    root: Path,
    selector: str,
    *,
    dry_run: bool = False,
    hook: SeatAdminHook | None = None,
    expected_digest: str | None = None,
) -> SeatMutationResult:
    return _mutate(
        root,
        action="retire",
        selector=selector,
        dry_run=dry_run,
        hook=hook,
        expected_digest=expected_digest,
    )


def remove_seat(*args: Any, **kwargs: Any) -> SeatMutationResult:
    """Discoverability alias for :func:`retire_seat`; homes are never removed."""
    return retire_seat(*args, **kwargs)


def plan_mutation(
    root: Path, action: str, selector: str | None = None, **options: Any
) -> SeatMutationResult:
    """Return the exact dry-run result for one supported lifecycle action."""
    normalized = "retire" if action == "remove" else action
    return _mutate(
        root,
        action=normalized,
        selector=selector,
        dry_run=True,
        **options,
    )


# Concise domain aliases for callers which already operate in the seats namespace.
activate = activate_seat
add = add_seat
deactivate = deactivate_seat
drain = drain_seat
remove = remove_seat
retire = retire_seat
set_fields = set_seat
