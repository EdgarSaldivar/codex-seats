"""Local availability and advisory claims used by routing and execution.

Only the controller-authored probe executes. No harness discovery, credentials,
model switching, host wakeup, provider ledger, or job queue is involved here.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import stat
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from . import bounded_process, state
from .seat_registry import LOCAL_ID_RE, LOCAL_MODEL_PROFILE_RE, validate_local_registries

STATES = frozenset({"asleep", "gaming", "loading", "ready", "busy", "degraded"})
CACHE_TTL_SECONDS = 15
MAX_PROBE_BYTES = 4096
_MODES = ("off", "shadow", "canary", "opportunistic")
PROBE_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}


def effective_mode(settings, seat):
    """The global mode is a ceiling; missing/invalid modes fail closed."""
    global_mode = settings.get("providers", {}).get("local", {}).get("mode", "off")
    mode = seat.get("mode", "off")
    if global_mode not in _MODES or mode not in _MODES:
        return "off"
    return _MODES[min(_MODES.index(global_mode), _MODES.index(mode))]


def capability_matches(seat, task_shape, validation):
    capabilities = seat.get("capabilities", {})
    return (task_shape in capabilities.get("task_shapes", [])
            and validation in capabilities.get("validation", []))


def _fresh(timestamp, now):
    return (type(timestamp) in {int, float} and math.isfinite(timestamp)
            and 0 <= now - timestamp <= CACHE_TTL_SECONDS)


def eligibility(settings, seat_id, availability, *, task_shape=None, validation=None, now=None):
    """Pure capacity decision. Non-ready states never enter a waiting queue."""
    now = time.time() if now is None else now
    seat = settings.get("local_seats", {}).get(seat_id, {})
    host = settings.get("local_hosts", {}).get(seat.get("host"), {})
    mode = effective_mode(settings, seat)
    result = {"seat": seat_id, "host": seat.get("host"),
              "resource_group": seat.get("resource_group"), "effective_mode": mode,
              "eligible": False, "state": availability.get("state", "degraded")}
    reason = None
    if not seat:
        reason = "unknown_seat"
    elif mode == "off":
        reason = "mode_off"
    elif seat.get("lifecycle") != "active" or host.get("lifecycle") != "active":
        reason = "lifecycle_ineligible"
    elif mode == "shadow":
        reason = "shadow_only"
    elif task_shape is not None or validation is not None:
        if not capability_matches(seat, task_shape, validation):
            reason = "capability_mismatch"
    if reason is None:
        if availability.get("host") != seat["host"]:
            reason = "host_mismatch"
        elif availability.get("reason") in {"availability_missing_or_invalid", "availability_stale"}:
            reason = availability["reason"]
        elif availability.get("config_digest") != _host_digest(host):
            reason = "configuration_changed"
        elif not _fresh(availability.get("observed_at"), now):
            reason = "availability_stale"
        elif availability.get("state") != "ready":
            reason = availability.get("reason", "host_not_ready")
        elif availability.get("model_profile") not in (None, seat["model_profile"]):
            reason = "model_profile_mismatch"
    result.update(eligible=reason is None, reason=reason or "ready")
    return result


def _host_digest(host):
    return hashlib.sha256(json.dumps(host, sort_keys=True).encode()).hexdigest()


def _unavailable(host_id, reason):
    return {"host": host_id, "state": "degraded", "reason": reason}


def parse_probe(payload, *, now):
    """Accept one small object, rejecting duplicate keys and stale probe timestamps."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate probe field")
            result[key] = value
        return result

    if not isinstance(payload, bytes) or len(payload) > MAX_PROBE_BYTES:
        raise ValueError("probe output too large")
    value = json.loads(payload.decode("utf-8"), object_pairs_hook=unique)
    if not isinstance(value, dict) or set(value) - {"state", "detail", "model_profile", "observed_at"}:
        raise ValueError("invalid probe fields")
    if not isinstance(value.get("state"), str) or value["state"] not in STATES:
        raise ValueError("invalid probe state")
    if "detail" in value:
        detail = value["detail"]
        if (not isinstance(detail, str) or len(detail) > 512
                or any(ord(c) < 32 or ord(c) == 127 for c in detail)):
            raise ValueError("invalid probe detail")
    if "model_profile" in value:
        profile = value["model_profile"]
        if (
            not isinstance(profile, str)
            or LOCAL_MODEL_PROFILE_RE.fullmatch(profile) is None
        ):
            raise ValueError("invalid probe model_profile")
    if "observed_at" in value and not _fresh(value["observed_at"], now):
        raise ValueError("stale probe data")
    value.setdefault("observed_at", now)
    return value


def _check_fd(fd, *, directory=False):
    info = os.fstat(fd)
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not correct_type or info.st_uid != os.getuid() or info.st_mode & 0o077
            or (not directory and info.st_nlink != 1)):
        raise ValueError("local runtime must contain owner-only directories and regular files")


@contextmanager
def _runtime(root: Path, *, create=False):
    """Anchor all runtime operations to verified, no-follow directory descriptors."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    root_fd = os.open(root, flags)
    runtime_fd = None
    try:
        info = os.fstat(root_fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("unsafe local control root")
        if create:
            try:
                os.mkdir("local-runtime", 0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
        runtime_fd = os.open("local-runtime", flags, dir_fd=root_fd)
        _check_fd(runtime_fd, directory=True)
        yield runtime_fd
    finally:
        if runtime_fd is not None:
            os.close(runtime_fd)
        os.close(root_fd)


def _open_file(directory, name, *, create=False):
    flags = os.O_RDWR if create else os.O_RDONLY
    flags |= os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT
    fd = os.open(name, flags, 0o600, dir_fd=directory)
    try:
        _check_fd(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _cache_name(host_id):
    if not isinstance(host_id, str) or LOCAL_ID_RE.fullmatch(host_id) is None:
        raise ValueError("invalid local host id")
    return f"availability-{host_id}.json"


def _write_cache(root, host_id, value):
    name = _cache_name(host_id)
    with _runtime(root, create=True) as directory:
        temporary = f".probe-{uuid.uuid4().hex}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(json.dumps(value).encode())
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def _host_gate(settings, host_id):
    # This check must precede validation, command lookup, and runtime filesystem I/O.
    if settings.get("providers", {}).get("local", {}).get("mode", "off") not in _MODES[1:]:
        return "mode_off"
    host = settings.get("local_hosts", {}).get(host_id)
    if host is None:
        return "unknown_host"
    if host.get("lifecycle") != "active":
        return "lifecycle_ineligible"
    if not any(seat.get("host") == host_id and seat.get("lifecycle") == "active"
               and effective_mode(settings, seat) != "off"
               for seat in settings.get("local_seats", {}).values()):
        return "no_enabled_seats"
    return None


def cached_availability(root, settings, host_id, *, now=None):
    reason = _host_gate(settings, host_id)
    if reason:
        return _unavailable(host_id, reason)
    now = time.time() if now is None else now
    try:
        with _runtime(root) as directory:
            fd = _open_file(directory, _cache_name(host_id))
            with os.fdopen(fd, "rb") as handle:
                payload = handle.read(MAX_PROBE_BYTES * 2 + 1)
        if len(payload) > MAX_PROBE_BYTES * 2:
            raise ValueError("cache too large")
        value = json.loads(payload)
        if not isinstance(value, dict) or value.get("host") != host_id:
            raise ValueError("invalid cache host")
        if "observed_at" not in value or type(value["observed_at"]) not in {int, float}:
            raise ValueError("cache missing or invalid timestamp")
        if not _fresh(value["observed_at"], now):
            return _unavailable(host_id, "availability_stale")
        if "config_digest" not in value:
            raise ValueError("cache missing configuration digest")
        if value["config_digest"] != _host_digest(settings["local_hosts"][host_id]):
            return _unavailable(host_id, "configuration_changed")
        probe = {key: value[key] for key in ("state", "detail", "model_profile", "observed_at") if key in value}
        parse_probe(json.dumps(probe).encode(), now=now)
        return value
    except (OSError, ValueError, TypeError, RecursionError):
        return _unavailable(host_id, "availability_missing_or_invalid")


def probe_host(root, settings, host_id, *, clock=time.time):
    reason = _host_gate(settings, host_id)
    if reason:
        return _unavailable(host_id, reason)
    try:
        validate_local_registries(settings)
    except ValueError:
        return _unavailable(host_id, "invalid_configuration")
    host = settings["local_hosts"][host_id]
    probe = host.get("availability_probe")
    if not probe or not host.get("base_url"):
        return _unavailable(host_id, "probe_not_configured")
    value = _unavailable(host_id, "probe_failed")
    started = clock()
    try:
        result = bounded_process.run(probe["argv"], timeout=probe["timeout_seconds"],
                                     cwd="/", env=dict(PROBE_ENV), max_output_bytes=MAX_PROBE_BYTES)
        if result.returncode:
            value["reason"] = "probe_exit_nonzero"
        else:
            finished = clock()
            candidate = parse_probe(result.stdout, now=finished)
            # An unstamped probe cannot extend freshness by spending time running.
            candidate["observed_at"] = min(started, candidate["observed_at"])
            if not _fresh(candidate["observed_at"], finished):
                raise ValueError("stale probe data")
            value = candidate
            value.update(host=host_id, reason="ready" if value["state"] == "ready" else "host_not_ready")
    except bounded_process.ProcessOutputLimitError:
        value["reason"] = "probe_output_limit"
    except bounded_process.ProcessTimeoutError:
        value["reason"] = "probe_timeout"
    except bounded_process.ProcessError:
        value["reason"] = "probe_failed"
    except (ValueError, UnicodeError, RecursionError):
        value["reason"] = "probe_invalid_output"
    value.setdefault("observed_at", started)
    value["config_digest"] = _host_digest(host)
    try:
        _write_cache(root, host_id, value)
    except (OSError, ValueError):
        return _unavailable(host_id, "unsafe_or_unwritable_cache")
    return value


def observe_host(root, settings, host_id):
    """Reuse one fresh observation, actively probing only on a cache miss/stale value."""
    cached = cached_availability(root, settings, host_id)
    if cached.get("reason") not in {"availability_missing_or_invalid", "availability_stale",
                                    "configuration_changed"}:
        return cached
    return probe_host(root, settings, host_id)


def status(root, settings, *, now=None):
    hosts = {host_id: cached_availability(root, settings, host_id, now=now)
             for host_id in settings.get("local_hosts", {})}
    return {"global_mode": settings.get("providers", {}).get("local", {}).get("mode", "off"),
            "hosts": hosts,
            "seats": [eligibility(settings, seat_id, hosts.get(seat["host"], {}), now=now)
                      for seat_id, seat in settings.get("local_seats", {}).items()]}


def _slot(directory, prefix, count):
    # One bounded scan, never polling. Lock files are permanent inode anchors;
    # metadata and old PID files never confer ownership or consume capacity.
    for index in range(count):
        name = f"{prefix}-{index}.lock"
        fd = _open_file(directory, name, create=True)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            continue
        except BaseException:
            os.close(fd)
            raise
        return fd
    return None


def _current_settings(root, supplied):
    """Re-read settings under the shared mutation lock.

    Every settings write (including seat retirement) holds this lock from its
    re-read through its atomic replace, so this read sees either the complete
    old or the complete new document. With no installed settings file the
    caller's snapshot stays authoritative, as in ``provider_local``.
    """
    root = Path(root)
    with state.settings_lock(root):
        if not state.settings_path(root).exists():
            return supplied
        current, _warnings = state.load_settings(root)
    return current


def _revalidate(root, settings, seat_id, availability, *, task_shape, validation, now):
    """Reason the held slots may no longer be used, or None.

    Runs only after the slots are held. A retirement that checked the slots
    before we took them has already committed (we wait on its lock), so the
    re-read shows it; one that runs after sees our held slot and refuses.
    """
    current = _current_settings(root, settings)
    decision = eligibility(current, seat_id, availability, task_shape=task_shape,
                           validation=validation, now=now)
    if not decision["eligible"]:
        return decision["reason"]
    locked = settings["local_seats"][seat_id]
    seat = current["local_seats"][seat_id]
    # The slots we hold are keyed by the seat's placement at first check.
    if (seat["host"], seat["resource_group"]) != (locked["host"], locked["resource_group"]):
        return "configuration_changed"
    return None


@contextmanager
def claim(root, settings, seat_id, availability, *, task_shape, validation, now=None):
    """Hold both seat and physical-group slots until context exit; never queue.

    Eligibility is decided twice: once on the caller's snapshot, and again on
    the current settings after both slots are held, so a seat retired, turned
    off or deactivated meanwhile is backed out before any work starts.
    """
    decision = eligibility(settings, seat_id, availability, task_shape=task_shape,
                           validation=validation, now=now)
    result = dict(decision, acquired=False)
    if not decision["eligible"]:
        yield result
        return
    with ExitStack() as cleanup:
        try:
            validate_local_registries(settings)
            seat = settings["local_seats"][seat_id]
            group = settings["local_hosts"][seat["host"]]["resource_groups"][seat["resource_group"]]
            group_key = hashlib.sha256(json.dumps([seat["host"], seat["resource_group"]]).encode()).hexdigest()
            with _runtime(root, create=True) as directory:
                for prefix, count in (
                    (f"seat-{seat_id}", seat["max_concurrency"]),
                    (f"group-{group_key}", group["max_concurrency"]),
                ):
                    fd = _slot(directory, prefix, count)
                    if fd is None:
                        result.update(eligible=False, reason="capacity_claimed")
                        break
                    cleanup.callback(os.close, fd)
                else:
                    reason = _revalidate(root, settings, seat_id, availability,
                                         task_shape=task_shape, validation=validation, now=now)
                    if reason is None:
                        result.update(acquired=True, reason="claimed")
                    else:
                        result.update(eligible=False, reason=reason)
        except (OSError, ValueError):
            result.update(eligible=False, reason="unsafe_claim_runtime")
        if not result["acquired"]:
            cleanup.close()
        yield result
