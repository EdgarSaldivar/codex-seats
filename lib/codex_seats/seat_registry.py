"""Pure helpers for the schema-3 dynamic seat registry.

This module deliberately performs no filesystem reads or writes.  It owns the
portable registry contract while lifecycle commands and provider probes remain
in their respective operational layers.
"""

from __future__ import annotations

import math
import os
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping


SEAT_ID_RE = re.compile(r"^seat([1-9][0-9]*)$")
CAPACITY_PROFILE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
UTC_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"
)
LIFECYCLES = frozenset({"active", "draining", "inactive", "retired"})
CAPACITY_SOURCES = frozenset(
    {
        "unverified",
        "openai-help-center",
        "openai-published-baseline",
        "workspace-ui",
        "contract",
        "operator-measured",
    }
)
BUILTIN_CAPACITY_PROFILES = {
    "personal-free": {
        "version": 1,
        "relative_capacity": None,
        "expects_five_hour_window": None,
    },
    "personal-go": {
        "version": 1,
        "relative_capacity": None,
        "expects_five_hour_window": None,
    },
    "personal-plus": {
        "version": 1,
        "relative_capacity": 1.0,
        "expects_five_hour_window": None,
    },
    "personal-pro-5x": {
        "version": 1,
        "relative_capacity": 5.0,
        "expects_five_hour_window": None,
    },
    "personal-pro-20x": {
        "version": 1,
        "relative_capacity": 20.0,
        "expects_five_hour_window": None,
    },
    "business-standard": {
        "version": 1,
        "relative_capacity": 1.0,
        "expects_five_hour_window": True,
    },
    "business-premium": {
        "version": 1,
        "relative_capacity": 5.0,
        "expects_five_hour_window": False,
    },
}
CAPACITY_PLAN_PRESETS = (
    {
        "label": "Free (capacity unverified)",
        "profile": "personal-free",
        "relative_capacity": None,
        "capacity_source": "unverified",
    },
    {
        "label": "Go (capacity unverified)",
        "profile": "personal-go",
        "relative_capacity": None,
        "capacity_source": "unverified",
    },
    {
        "label": "Plus 1x (baseline)",
        "profile": "personal-plus",
        "relative_capacity": 1.0,
        "capacity_source": "openai-published-baseline",
    },
    {
        "label": "Pro 5x (baseline)",
        "profile": "personal-pro-5x",
        "relative_capacity": 5.0,
        "capacity_source": "openai-published-baseline",
    },
    {
        "label": "Pro 20x (baseline)",
        "profile": "personal-pro-20x",
        "relative_capacity": 20.0,
        "capacity_source": "openai-published-baseline",
    },
    {
        "label": "Standard 1x (baseline)",
        "profile": "business-standard",
        "relative_capacity": 1.0,
        "capacity_source": "openai-published-baseline",
    },
    {
        "label": "Premium 5x (estimate)",
        "profile": "business-premium",
        "relative_capacity": 5.0,
        "capacity_source": "openai-published-baseline",
    },
)
BUILTIN_CAPACITIES = {
    profile: definition["relative_capacity"]
    for profile, definition in BUILTIN_CAPACITY_PROFILES.items()
}
# Local seats are deliberately a separate registry from account-backed
# ``pool.seats``.  A host owns physical resource groups (for example one GPU),
# and every local seat names one of those groups so model profiles sharing the
# same device cannot later be mistaken for independent capacity.
LOCAL_MODES = frozenset({"off", "shadow", "canary", "opportunistic"})
LOCAL_TASK_SHAPES = frozenset({"execute", "investigate", "design"})
LOCAL_VALIDATION_LEVELS = frozenset({"strong", "weak"})
LOCAL_ID_RE = CAPACITY_PROFILE_RE
LOCAL_MODEL_PROFILE_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$"
)
RUNTIME_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_LOCAL_CONCURRENCY = 64
MIN_LOCAL_CONTEXT_WINDOW_SIZE = 16_384
MAX_LOCAL_CONTEXT_WINDOW_SIZE = 1_048_576

_LOCAL_HOST_FIELDS = frozenset({"label", "lifecycle", "resource_groups"})
_LOCAL_RESOURCE_GROUP_FIELDS = frozenset({"max_concurrency"})
_LOCAL_SEAT_FIELDS = frozenset(
    {
        "label",
        "lifecycle",
        "mode",
        "host",
        "resource_group",
        "model_profile",
        "harness",
        "capabilities",
        "max_concurrency",
        "runtime_fingerprint",
    }
)
_LOCAL_SEAT_OPTIONAL_FIELDS = frozenset({"context_window_size"})
_LOCAL_CAPABILITY_FIELDS = frozenset({"task_shapes", "validation"})

_SEAT_FIELDS = frozenset(
    {
        "label",
        "lifecycle",
        "home",
        "capacity_profile",
        "relative_capacity",
        "capacity_source",
        "capacity_verified_at",
    }
)
_SENSITIVE_KEYS = frozenset(
    {
        "auth",
        "auth_payload",
        "api_key",
        "credential",
        "credentials",
        "email",
        "member",
        "member_id",
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "workspace_member",
        "workspace_member_id",
    }
)
_TRANSITIONS = {
    "active": frozenset({"draining", "inactive"}),
    "draining": frozenset({"active", "inactive"}),
    "inactive": frozenset({"active", "retired"}),
    "retired": frozenset(),
}


class SeatRegistryError(ValueError):
    """A registry value violates the stable schema-3 contract."""


def seat_number(seat_id: str) -> int:
    """Return the numeric identity component, rejecting aliases and leading zeroes."""
    if not isinstance(seat_id, str):
        raise SeatRegistryError("seat id must be a string matching seatN")
    match = SEAT_ID_RE.fullmatch(seat_id)
    if match is None:
        raise SeatRegistryError(
            f"invalid seat id {seat_id!r}; expected seatN with N >= 1 and no leading zeroes"
        )
    return int(match.group(1))


def validate_seat_id(seat_id: str) -> str:
    seat_number(seat_id)
    return seat_id


def is_seat_id(value: Any) -> bool:
    return isinstance(value, str) and SEAT_ID_RE.fullmatch(value) is not None


def sort_seat_ids(seat_ids: Iterable[str]) -> list[str]:
    """Sort strict IDs numerically (seat2 before seat10), validating every value."""
    values = list(seat_ids)
    return sorted(values, key=seat_number)


def validate_lifecycle(value: Any, *, path: str = "lifecycle") -> str:
    if not isinstance(value, str) or value not in LIFECYCLES:
        raise SeatRegistryError(
            f"{path} must be one of {', '.join(sorted(LIFECYCLES))}"
        )
    return value


def validate_lifecycle_transition(current: str, target: str) -> None:
    validate_lifecycle(current, path="current lifecycle")
    validate_lifecycle(target, path="target lifecycle")
    if current == target:
        return
    if target not in _TRANSITIONS[current]:
        raise SeatRegistryError(f"lifecycle transition {current} -> {target} is not allowed")


def validate_capacity_profile(value: Any, *, path: str = "capacity_profile") -> str:
    if not isinstance(value, str) or CAPACITY_PROFILE_RE.fullmatch(value) is None:
        raise SeatRegistryError(
            f"{path} must be a lowercase slug of 1-64 letters, digits, or hyphens"
        )
    return value


def validate_capacity_source(value: Any, *, path: str = "capacity_source") -> str:
    if value not in CAPACITY_SOURCES:
        raise SeatRegistryError(
            f"{path} must be one of {', '.join(sorted(CAPACITY_SOURCES))}"
        )
    return value


def effective_relative_capacity(entry: Mapping[str, Any]) -> float:
    """Return the conservative routing multiplier for a validated seat entry."""
    capacity = entry.get("relative_capacity")
    if capacity is None:
        return 1.0
    if (
        not isinstance(capacity, (int, float, Decimal))
        or isinstance(capacity, bool)
    ):
        raise SeatRegistryError(
            "relative_capacity must be null or a finite number from 0.1 to 1000"
        )
    try:
        numeric = float(capacity)
    except (OverflowError, ValueError) as exc:
        raise SeatRegistryError(
            "relative_capacity must be null or a finite number from 0.1 to 1000"
        ) from exc
    if not math.isfinite(numeric) or not 0.1 <= numeric <= 1000:
        raise SeatRegistryError(
            "relative_capacity must be null or a finite number from 0.1 to 1000"
        )
    return numeric


def capacity_fingerprint(entry: Mapping[str, Any]) -> str:
    """Bind learned capacity evidence to the declared profile and multiplier.

    The multiplier is deliberately taken from the seat record rather than
    inferred from a built-in profile.  That keeps operator changes explicit and
    makes unknown/null capacity conservatively distinct at its effective 1x.
    """
    profile = validate_capacity_profile(entry.get("capacity_profile"))
    multiplier = effective_relative_capacity(entry)
    # ``:g`` defaults to six significant digits, which can collapse two valid
    # nearby multipliers into one learning identity.  ``repr`` is Python's
    # shortest round-trippable float spelling; trim only the cosmetic integer
    # suffix so stable built-ins remain ``@1`` and ``@5``.
    canonical_multiplier = repr(multiplier)
    if canonical_multiplier.endswith(".0"):
        canonical_multiplier = canonical_multiplier[:-2]
    return f"{profile}@{canonical_multiplier}"


def _validate_timestamp(value: Any, path: str) -> None:
    if not isinstance(value, str) or UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise SeatRegistryError(f"{path} must be a UTC ISO-8601 timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise SeatRegistryError(f"{path} must be a valid UTC ISO-8601 timestamp") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise SeatRegistryError(f"{path} must be a UTC ISO-8601 timestamp")


def _canonical_home(path: Path) -> str:
    # realpath resolves every existing parent while preserving a deterministic
    # suffix for a not-yet-created inactive home.
    return os.path.normcase(os.path.realpath(os.path.normpath(str(path))))


def _existing_home_identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_dev, info.st_ino


def resolve_home(pool_root: Path, seat_id: str, entry: Mapping[str, Any]) -> Path:
    """Resolve configured/null home semantics without touching the filesystem."""
    validate_seat_id(seat_id)
    configured = entry.get("home")
    if configured is None:
        return Path(os.path.normpath(str(pool_root / seat_id)))
    if (
        not isinstance(configured, str)
        or not configured
        or any(ord(character) < 32 or ord(character) == 127 for character in configured)
    ):
        raise SeatRegistryError(f"pool.seats.{seat_id}.home must be an absolute path or null")
    path = Path(configured)
    if not path.is_absolute():
        raise SeatRegistryError(f"pool.seats.{seat_id}.home must be an absolute path or null")
    return Path(os.path.normpath(str(path)))


def registry_entries(settings: Mapping[str, Any]) -> Mapping[str, Any]:
    pool = settings.get("pool")
    if not isinstance(pool, Mapping):
        raise SeatRegistryError("pool must be an object")
    seats = pool.get("seats")
    if not isinstance(seats, Mapping):
        raise SeatRegistryError("pool.seats must be an object")
    return seats


def registered_seat_ids(
    settings: Mapping[str, Any], *, lifecycles: Iterable[str] | None = None
) -> list[str]:
    seats = registry_entries(settings)
    selected = None if lifecycles is None else frozenset(lifecycles)
    if selected is not None:
        for lifecycle in selected:
            validate_lifecycle(lifecycle)
    result = []
    for seat_id in sort_seat_ids(seats):
        entry = seats[seat_id]
        if not isinstance(entry, Mapping):
            raise SeatRegistryError(f"pool.seats.{seat_id} must be an object")
        lifecycle = validate_lifecycle(
            entry.get("lifecycle"), path=f"pool.seats.{seat_id}.lifecycle"
        )
        if selected is None or lifecycle in selected:
            result.append(seat_id)
    return result


def active_seat_ids(settings: Mapping[str, Any]) -> list[str]:
    return registered_seat_ids(settings, lifecycles=("active",))


def derive_roles(settings: Mapping[str, Any]) -> dict[str, str]:
    """Derive roles for active seats; inactive/draining/retired seats have no role."""
    pool = settings.get("pool", {})
    primary, deputy = pool.get("primary"), pool.get("deputy")
    roles: dict[str, str] = {}
    for seat_id in active_seat_ids(settings):
        if seat_id == primary:
            roles[seat_id] = "primary"
        elif seat_id == deputy:
            roles[seat_id] = "deputy"
        else:
            roles[seat_id] = "worker"
    return roles


def resolve_readonly_selector(selector: str, settings: Mapping[str, Any]) -> str:
    """Resolve a strict seatN identity or its numeric shorthand to a registered seat."""
    if not isinstance(selector, str) or not selector:
        raise SeatRegistryError("seat selector must be non-empty")
    if selector.isascii() and selector.isdigit() and not selector.startswith("0"):
        candidate = f"seat{selector}"
    else:
        candidate = selector
    if not is_seat_id(candidate) or candidate not in registered_seat_ids(settings):
        raise SeatRegistryError(f"unknown seat selector {selector!r}")
    return candidate


def _check_sensitive_keys(value: Any, path: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS:
                raise SeatRegistryError(
                    f"{path}.{key} is forbidden; settings must not contain secrets or member identifiers"
                )
            _check_sensitive_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_sensitive_keys(item, f"{path}[{index}]")


def _validate_local_id(value: Any, path: str) -> str:
    if not isinstance(value, str) or LOCAL_ID_RE.fullmatch(value) is None:
        raise SeatRegistryError(
            f"{path} must be a lowercase slug of 1-64 letters, digits, or hyphens"
        )
    return value


def _validate_local_label(value: Any, path: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise SeatRegistryError(
            f"{path} must be 1-128 visible characters without surrounding whitespace"
        )


def _validate_local_concurrency(value: Any, path: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= MAX_LOCAL_CONCURRENCY
    ):
        raise SeatRegistryError(
            f"{path} must be an integer from 1 to {MAX_LOCAL_CONCURRENCY}"
        )
    return value


def _require_exact_fields(
    value: Mapping[str, Any], fields: frozenset[str], path: str
) -> None:
    missing = sorted(fields - set(value), key=str)
    extra = sorted(set(value) - fields, key=str)
    if missing:
        raise SeatRegistryError(f"{path} is missing required fields: {', '.join(missing)}")
    if extra:
        raise SeatRegistryError(f"{path} has unknown fields: {', '.join(extra)}")


def validate_local_registries(settings: Mapping[str, Any]) -> None:
    """Validate the additive local-host/local-seat settings contract.

    Local seats never change the meaning of account-backed ``pool.seats``.  This
    performs no networking or provider discovery.
    """
    from urllib.parse import urlsplit

    provider = settings.get("providers", {}).get("local", {})
    if not isinstance(provider, Mapping):
        raise SeatRegistryError("providers.local must be an object")
    _check_sensitive_keys(provider, "providers.local")
    if set(provider) - {"mode", "command", "max_turns", "repair_budget"}:
        raise SeatRegistryError("providers.local has unknown fields")
    if not isinstance(provider.get("mode", "off"), str) or provider.get("mode", "off") not in LOCAL_MODES:
        raise SeatRegistryError("providers.local.mode is invalid")
    command = provider.get("command", "qwen")
    if (not isinstance(command, str) or not command or len(command) > 1024
            or any(c.isspace() or ord(c) < 32 for c in command)):
        raise SeatRegistryError("providers.local.command must be one executable path or name")
    for key, default, lower, upper in (("max_turns", 100, 1, 100), ("repair_budget", 2, 0, 10)):
        value = provider.get(key, default)
        if type(value) is not int or not lower <= value <= upper:
            raise SeatRegistryError(f"providers.local.{key} must be {lower}..{upper}")
    hosts = settings.get("local_hosts", {})
    seats = settings.get("local_seats", {})
    if not isinstance(hosts, Mapping):
        raise SeatRegistryError("local_hosts must be an object")
    if not isinstance(seats, Mapping):
        raise SeatRegistryError("local_seats must be an object")
    _check_sensitive_keys(hosts, "local_hosts")
    _check_sensitive_keys(seats, "local_seats")

    host_groups: dict[str, Mapping[str, Any]] = {}
    host_lifecycles: dict[str, str] = {}
    for host_id, host in hosts.items():
        _validate_local_id(host_id, "local host id")
        path = f"local_hosts.{host_id}"
        if not isinstance(host, Mapping):
            raise SeatRegistryError(f"{path} must be an object")
        optional = {"base_url", "lease_url", "availability_probe"}
        _require_exact_fields(host, _LOCAL_HOST_FIELDS | (optional & set(host)), path)
        for url_field in ("base_url", "lease_url"):
            if url_field not in host:
                continue
            url = host[url_field]
            if (not isinstance(url, str) or len(url) > 2048
                    or any(c.isspace() or ord(c) < 32 for c in url)
                    or any(c in url for c in ("?", "#", "\\"))):
                raise SeatRegistryError(f"{path}.{url_field} is invalid")
            try:
                parsed = urlsplit(url)
                valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                         and parsed.username is None and parsed.password is None
                         and (parsed.port is None or parsed.port > 0)
                         and re.fullmatch(r"[A-Za-z0-9.:[\]-]+", parsed.hostname)
                         and not parsed.netloc.endswith(":"))
            except ValueError:
                valid = False
            if not valid:
                raise SeatRegistryError(
                    f"{path}.{url_field} must be an HTTP(S) URL without credentials"
                )
        if "availability_probe" in host:
            probe = host["availability_probe"]
            if not isinstance(probe, Mapping):
                raise SeatRegistryError(f"{path}.availability_probe must be an object")
            _require_exact_fields(probe, frozenset({"argv", "timeout_seconds"}), path)
            argv = probe["argv"]
            if (not isinstance(argv, list) or not 1 <= len(argv) <= 32
                    or any(not isinstance(arg, str) or not arg or len(arg) > 2048
                           or any(ord(c) < 32 for c in arg) for arg in argv)):
                raise SeatRegistryError(f"{path}.availability_probe.argv must be direct argv")
            # A probe is a controller-authored executable, never a shell program.
            if argv[0].rsplit("/", 1)[-1] in {"sh", "bash", "zsh", "dash", "fish", "ksh", "env"}:
                raise SeatRegistryError(f"{path}.availability_probe must not invoke a shell")
            if any(c.isspace() for c in argv[0]):
                raise SeatRegistryError(f"{path}.availability_probe must use a direct executable")
            timeout = probe["timeout_seconds"]
            if type(timeout) not in {int, float} or not 0.1 <= timeout <= 30:
                raise SeatRegistryError(f"{path}.availability_probe timeout must be 0.1..30 seconds")
        _validate_local_label(host.get("label"), f"{path}.label")
        host_lifecycles[host_id] = validate_lifecycle(
            host.get("lifecycle"), path=f"{path}.lifecycle"
        )
        groups = host.get("resource_groups")
        if not isinstance(groups, Mapping) or not groups:
            raise SeatRegistryError(f"{path}.resource_groups must be a non-empty object")
        for group_id, group in groups.items():
            _validate_local_id(group_id, f"{path}.resource group id")
            group_path = f"{path}.resource_groups.{group_id}"
            if not isinstance(group, Mapping):
                raise SeatRegistryError(f"{group_path} must be an object")
            _require_exact_fields(group, _LOCAL_RESOURCE_GROUP_FIELDS, group_path)
            _validate_local_concurrency(
                group.get("max_concurrency"), f"{group_path}.max_concurrency"
            )
        host_groups[host_id] = groups

    for local_seat_id, seat in seats.items():
        _validate_local_id(local_seat_id, "local seat id")
        path = f"local_seats.{local_seat_id}"
        if not isinstance(seat, Mapping):
            raise SeatRegistryError(f"{path} must be an object")
        _require_exact_fields(
            seat, _LOCAL_SEAT_FIELDS | (_LOCAL_SEAT_OPTIONAL_FIELDS & set(seat)), path
        )
        _validate_local_label(seat.get("label"), f"{path}.label")
        lifecycle = validate_lifecycle(
            seat.get("lifecycle"), path=f"{path}.lifecycle"
        )
        mode = seat.get("mode")
        if not isinstance(mode, str) or mode not in LOCAL_MODES:
            raise SeatRegistryError(
                f"{path}.mode must be one of {', '.join(sorted(LOCAL_MODES))}"
            )
        if lifecycle != "active" and mode != "off":
            raise SeatRegistryError(f"{path} must use mode off unless lifecycle is active")

        host_id = seat.get("host")
        if not isinstance(host_id, str) or host_id not in host_groups:
            raise SeatRegistryError(f"{path}.host must name a configured local host")
        if lifecycle == "active" and host_lifecycles[host_id] != "active":
            raise SeatRegistryError(f"{path} cannot be active on a non-active local host")
        group_id = seat.get("resource_group")
        groups = host_groups[host_id]
        if not isinstance(group_id, str) or group_id not in groups:
            raise SeatRegistryError(
                f"{path}.resource_group must name a resource group on local host {host_id}"
            )

        model_profile = seat.get("model_profile")
        if (
            not isinstance(model_profile, str)
            or LOCAL_MODEL_PROFILE_RE.fullmatch(model_profile) is None
        ):
            raise SeatRegistryError(
                f"{path}.model_profile must be a lowercase model identifier"
            )
        if "context_window_size" in seat:
            context_window_size = seat["context_window_size"]
            if (type(context_window_size) is not int
                    or not MIN_LOCAL_CONTEXT_WINDOW_SIZE <= context_window_size <= MAX_LOCAL_CONTEXT_WINDOW_SIZE):
                raise SeatRegistryError(
                    f"{path}.context_window_size must be an integer from "
                    f"{MIN_LOCAL_CONTEXT_WINDOW_SIZE} to {MAX_LOCAL_CONTEXT_WINDOW_SIZE}"
                )
        _validate_local_id(seat.get("harness"), f"{path}.harness")
        seat_concurrency = _validate_local_concurrency(
            seat.get("max_concurrency"), f"{path}.max_concurrency"
        )
        group_concurrency = groups[group_id]["max_concurrency"]
        if seat_concurrency > group_concurrency:
            raise SeatRegistryError(
                f"{path}.max_concurrency must not exceed its resource group maximum"
            )

        capabilities = seat.get("capabilities")
        if not isinstance(capabilities, Mapping):
            raise SeatRegistryError(f"{path}.capabilities must be an object")
        _require_exact_fields(capabilities, _LOCAL_CAPABILITY_FIELDS, f"{path}.capabilities")
        for field, allowed in (
            ("task_shapes", LOCAL_TASK_SHAPES),
            ("validation", LOCAL_VALIDATION_LEVELS),
        ):
            values = capabilities.get(field)
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(value, str) or value not in allowed for value in values)
                or len(values) != len(set(values))
            ):
                raise SeatRegistryError(
                    f"{path}.capabilities.{field} must be a non-empty unique list from "
                    f"{', '.join(sorted(allowed))}"
                )

        fingerprint = seat.get("runtime_fingerprint")
        if (
            not isinstance(fingerprint, str)
            or RUNTIME_FINGERPRINT_RE.fullmatch(fingerprint) is None
        ):
            raise SeatRegistryError(
                f"{path}.runtime_fingerprint must be sha256 followed by 64 lowercase hex characters"
            )


def _validate_entry(seat_id: str, entry: Any) -> None:
    path = f"pool.seats.{seat_id}"
    if not isinstance(entry, Mapping):
        raise SeatRegistryError(f"{path} must be an object")
    missing = sorted(_SEAT_FIELDS - set(entry))
    if missing:
        raise SeatRegistryError(f"{path} is missing required fields: {', '.join(missing)}")
    if "id" in entry or "seat_id" in entry:
        raise SeatRegistryError(f"{path} must not duplicate or rename its immutable seat id")
    _check_sensitive_keys(entry, path)

    label = entry.get("label")
    if (
        not isinstance(label, str)
        or not label
        or label != label.strip()
        or len(label) > 128
        or "@" in label
        or any(ord(character) < 32 or ord(character) == 127 for character in label)
    ):
        raise SeatRegistryError(
            f"{path}.label must be 1-128 visible characters without surrounding whitespace"
        )
    validate_lifecycle(entry.get("lifecycle"), path=f"{path}.lifecycle")
    validate_capacity_profile(
        entry.get("capacity_profile"), path=f"{path}.capacity_profile"
    )
    source = validate_capacity_source(
        entry.get("capacity_source"), path=f"{path}.capacity_source"
    )
    capacity = entry.get("relative_capacity")
    if capacity is not None:
        try:
            effective_relative_capacity(entry)
        except SeatRegistryError as exc:
            raise SeatRegistryError(
                f"{path}.relative_capacity must be null or a finite number from 0.1 to 1000"
            ) from exc
    verified_at = entry.get("capacity_verified_at")
    if source == "unverified":
        if capacity is not None or verified_at is not None:
            raise SeatRegistryError(
                f"{path} unverified capacity requires null relative_capacity and capacity_verified_at"
            )
    else:
        if capacity is None or verified_at is None:
            raise SeatRegistryError(
                f"{path} verified capacity requires relative_capacity and capacity_verified_at"
            )
        _validate_timestamp(verified_at, f"{path}.capacity_verified_at")


def validate_schema3(settings: Mapping[str, Any], pool_root: Path | None = None) -> None:
    """Validate schema-3 topology and filesystem-canonical home uniqueness."""
    if settings.get("schema") != 3:
        raise SeatRegistryError("settings schema must be 3")
    _check_sensitive_keys(settings, "settings")
    validate_local_registries(settings)
    pool = settings.get("pool")
    if not isinstance(pool, Mapping):
        raise SeatRegistryError("pool must be an object")
    if "workers" in pool or "primary_home" in pool:
        raise SeatRegistryError(
            "pool.workers and pool.primary_home are not settings; register seats in pool.seats"
        )
    seats = registry_entries(settings)
    for seat_id, entry in seats.items():
        validate_seat_id(seat_id)
        _validate_entry(seat_id, entry)

    active = set(active_seat_ids(settings))
    primary, deputy = pool.get("primary"), pool.get("deputy")
    if primary is None:
        if active:
            raise SeatRegistryError("pool.primary may be null only when there are no active seats")
    elif primary not in active:
        raise SeatRegistryError("pool.primary must name an active registered seat")
    if deputy is not None:
        if deputy not in active:
            raise SeatRegistryError("pool.deputy must be null or name an active registered seat")
        if deputy == primary:
            raise SeatRegistryError("pool.deputy must be distinct from pool.primary")

    root = pool_root if pool_root is not None else Path("/__codex_seats_pool__")
    homes: dict[str, str] = {}
    identities: dict[tuple[int, int], str] = {}
    for seat_id in registered_seat_ids(settings):
        resolved_home = resolve_home(root, seat_id, seats[seat_id])
        home = _canonical_home(resolved_home)
        if home in homes:
            raise SeatRegistryError(
                f"canonical home collision between {homes[home]} and {seat_id}: {home}"
            )
        homes[home] = seat_id
        identity = _existing_home_identity(resolved_home)
        if identity is not None:
            if identity in identities:
                raise SeatRegistryError(
                    "filesystem home identity collision between "
                    f"{identities[identity]} and {seat_id}: {home}"
                )
            identities[identity] = seat_id
