from __future__ import annotations

import copy
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from .policy_settings import DEFAULTS as POLICY_DEFAULTS

from .efforts import EFFORTS
from .quota_cooldown import quota_cooldown_recovered, quota_evidence_from_snapshot
from .seat_registry import (
    derive_roles as derive_registry_roles,
    effective_relative_capacity,
    registered_seat_ids,
    resolve_home as resolve_registry_home,
    validate_local_registries,
    validate_schema3,
)
from .task_policy import (
    CAPABILITY_TIERS,
    DEFAULT_CAPABILITY_BY_KIND,
    DEFAULT_CLAUDE_ELIGIBLE_KINDS,
    DEFAULT_CLAUDE_FRONTIER_KINDS,
    DEFAULT_CLAUDE_MODEL_ROLES,
    DEFAULT_CODEX_MODEL_BY_KIND,
    DEFAULT_CODEX_MODEL_ROLES,
    DEFAULT_EFFORT_BY_KIND,
    DEFAULT_PROVIDER_BY_KIND,
    DEFAULT_SIZE_BY_KIND,
    current_claude_role_model,
    current_codex_role_model,
    resolve_task_policy,
)

VALID_CLAUDE_MODES = {"off", "manual_only", "review_only", "opportunistic"}


def now() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else now(), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def age_h(value: str | None) -> float | None:
    parsed = parse_iso(value)
    return None if parsed is None else max(0.0, (now() - parsed) / 3600)


SETTINGS_SCHEMA = 3


def default_settings() -> dict[str, Any]:
    """Return the settings for a new installation: an empty seat registry.

    Missing or invalid configuration is hard-off. Seats are registered
    explicitly, and the interactive controller can explicitly select Claude
    manual_only, review_only, or opportunistic.
    """
    return {
        "schema": SETTINGS_SCHEMA,
        "pool": {
            "primary": None,
            "seats": {},
            "reserves_pct": {},
            "reserve_release_hours": 36,
            "probe_stale_seconds": 6 * 3600,
            "watch_probe_seconds": 300,
            "telemetry_scan_seconds": 5,
            "account_probe_timeout_seconds": 30,
            "cap_cooldown_seconds": 5 * 3600,
            "small_task_min_headroom_pct": 5,
            "big_task_min_headroom_pct": 10,
            "task_min_headroom_pct_by_seat": {},
            "task_min_headroom_pct_by_capacity_multiplier": {
                "1": {"small": 5, "big": 10},
                "5": {"small": 10, "big": 20},
            },
        },
        "control": {
            "auto_min_usable_pct": 30,
            "manual_min_usable_pct": 10,
        },
        "execution": {
            "delegation_policy": POLICY_DEFAULTS["execution.delegation_policy"],
            "inline_max_minutes": POLICY_DEFAULTS["execution.inline_max_minutes"],
            "head_permissions": POLICY_DEFAULTS["execution.head_permissions"],
            # Controller-owned worker boundary; restricted until deliberately
            # changed.
            "worker_access": "restricted",
            "parallel_min_minutes": 8,
            "learning_min_samples": 8,
            # Allow an isolated worker on the head identity when ordinary worker
            # capacity is unavailable, independently of the delegation preference.
            "head_worker_fallback": POLICY_DEFAULTS["execution.head_worker_fallback"],
            "capacity": {
                "fallback_burn_pct": {"small": 5, "big": 20},
                "margin_pct": 5,
                "margin_taper_max_hours": 36,
                "margin_taper_window_fraction": 0.2,
                # A failed/partial account sweep retains bounded use of the
                # last-good view for small work, with a larger safety margin.
                "degraded_grace_seconds": 24 * 3600,
                "degraded_extra_margin_pct": 10,
            },
            "default_size_by_kind": dict(DEFAULT_SIZE_BY_KIND),
            "capability_tier_by_kind": dict(DEFAULT_CAPABILITY_BY_KIND),
            "effort_by_kind": dict(DEFAULT_EFFORT_BY_KIND),
            # Which provider `auto` prefers per kind. Build kinds default to the
            # abundant Codex pool (with a Codex->Claude exhaustion fallback); the
            # quality lenses default to Claude (with a Claude->Codex fallback).
            # Tunable here without code changes — the symmetric swap is config-driven.
            "prefer_provider_by_kind": dict(DEFAULT_PROVIDER_BY_KIND),
        },
        "review": {
            "mode": POLICY_DEFAULTS["review.mode"],
            "strategy": POLICY_DEFAULTS["review.strategy"],
        },
        "providers": {
            "local": {
                "mode": "off",
                "command": "qwen",
                "max_turns": 100,
                "repair_budget": 2,
            },
            "claude": {
                "mode": "off",
                "command": "claude",
                "eligible_kinds": list(DEFAULT_CLAUDE_ELIGIBLE_KINDS),
                "cooldown_seconds": 5 * 3600,
                "model": None,
                "model_roles": dict(DEFAULT_CLAUDE_MODEL_ROLES),
                "frontier_kinds": list(DEFAULT_CLAUDE_FRONTIER_KINDS),
                "expires_at": None,
                "usage_stale_seconds": 6 * 3600,
                "active_usage_refresh": True,
                "small_canary_max_minutes": 5,
                "small_task_min_headroom_pct": 10,
                "big_task_min_headroom_pct": 15,
                # Operator-declared fallback plan label (e.g. "Max 5x"). The
                # client exposes no documented nonsecret plan field, so this is
                # display metadata only and never gates routing or admission.
                "subscription_tier": None,
            },
            "codex": {
                # "ultra" (Sol Ultra subagent mode) is account-gated at the API;
                # flip to true once the account is enrolled — until then workers
                # clamp ultra -> max instead of taking a guaranteed 400.
                "supports_ultra": False,
                "model_roles": dict(DEFAULT_CODEX_MODEL_ROLES),
                "model_by_kind": dict(DEFAULT_CODEX_MODEL_BY_KIND),
            },
        },
        "power": {
            "caffeinate": True,
            "require_ac": True,
        },
        "projects": {},
        "local_hosts": {},
        "local_seats": {},
    }


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def settings_path(root: Path) -> Path:
    return root / "settings.json"


MAX_SETTINGS_BYTES = 1024 * 1024
MAX_PROTECTED_BRANCH_PATTERNS = 64
MAX_PROTECTED_BRANCH_PATTERN_LENGTH = 256
MAX_CLAUDE_SUBSCRIPTION_TIER_LENGTH = 40


class SettingsMutationError(ValueError):
    """A settings document cannot be safely mutated."""


@contextmanager
def settings_lock(root: Path) -> Iterator[None]:
    """Take the owner-only lock shared by every settings mutation."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root_stat = root.lstat()
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != os.geteuid()
        or root_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise SettingsMutationError(
            "settings directory must be an owner-controlled, non-symbolic-link directory"
        )
    lock_path = root / ".settings.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    with os.fdopen(descriptor, "a+") as lock_file:
        lock_stat = os.fstat(lock_file.fileno())
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_nlink != 1
            or lock_stat.st_uid != os.geteuid()
            or lock_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise SettingsMutationError(
                "settings lock must be an owner-controlled, singly linked regular file"
            )
        os.fchmod(lock_file.fileno(), 0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            current_lock = lock_path.lstat()
            if (current_lock.st_dev, current_lock.st_ino) != (
                lock_stat.st_dev,
                lock_stat.st_ino,
            ):
                raise SettingsMutationError(
                    "settings lock changed while it was acquired"
                )
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _without_comments(value: Any) -> Any:
    """Copy settings while dropping documented template-only comment metadata."""
    if isinstance(value, dict):
        return {
            key: _without_comments(item)
            for key, item in value.items()
            if key != "_comment"
        }
    if isinstance(value, list):
        return [_without_comments(item) for item in value]
    return copy.deepcopy(value)


# Retired settings that older files may still carry, as dotted paths. They are
# preserved verbatim, never read, and do not warn: ``ui`` held the removed
# browser manager's appearance preferences; the two pool keys belonged to the
# pre-schema-3 "primary waits for a non-primary" flag and an unused bank expiry.
LEGACY_SETTINGS_KEYS = frozenset({
    "ui",
    "pool.primary_wait_for_eligible_nonprimary",
    "pool.banked_reset_standard_expiry_days",
})


def _legacy_keys(section: str = "") -> set[str]:
    """Legacy key names directly under ``section`` ("" is the top level)."""
    prefix = f"{section}." if section else ""
    return {
        path[len(prefix):]
        for path in LEGACY_SETTINGS_KEYS
        if path.startswith(prefix) and "." not in path[len(prefix):]
    }


def _warn_unknown_keys(
    value: Any, allowed: set[str], path: str, warnings: list[str]
) -> None:
    if not isinstance(value, dict):
        return
    for key in sorted(set(value) - allowed):
        name = f"{path}.{key}" if path else key
        warnings.append(
            f"unknown setting {name!r} was preserved; check its spelling or upgrade "
            "Codex Seats if it belongs to a newer schema"
        )


def _canonicalize_settings(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a comment-free schema-3 copy plus model-alias and unknown-key warnings."""
    schema = raw.get("schema")
    if type(schema) is not int or schema != SETTINGS_SCHEMA:
        raise ValueError(
            f"unsupported settings schema {schema!r}; only schema {SETTINGS_SCHEMA} "
            "(the dynamic seat registry) is supported and older settings are not "
            "migrated; replace settings.json with a schema-3 registry (see "
            "templates/settings.example.json)"
        )
    canonical, warnings = _without_comments(raw), []
    pool = canonical.get("pool")
    if isinstance(pool, dict):
        # The deputy role was removed: a legacy pool.deputy is ignored silently
        # and, because it is gone from the canonical document, dropped on the
        # next settings write.
        pool.pop("deputy", None)

    providers = canonical.get("providers")
    claude = providers.get("claude") if isinstance(providers, dict) else None
    model_roles = claude.get("model_roles") if isinstance(claude, dict) else None
    if isinstance(model_roles, dict):
        for role in ("frontier", "complex", "routine"):
            configured = model_roles.get(role)
            if not isinstance(configured, str):
                continue
            current = current_claude_role_model(configured)
            if current != configured:
                model_roles[role] = current
                warnings.append(
                    f"providers.claude.model_roles.{role} upgraded from "
                    f"{configured!r} to {current!r}"
                )

    codex = providers.get("codex") if isinstance(providers, dict) else None
    model_roles = codex.get("model_roles") if isinstance(codex, dict) else None
    if isinstance(model_roles, dict):
        for role in CAPABILITY_TIERS:
            configured = model_roles.get(role)
            if not isinstance(configured, str):
                continue
            current = current_codex_role_model(role, configured)
            if current != configured:
                model_roles[role] = current
                warnings.append(
                    f"providers.codex.model_roles.{role} upgraded from "
                    f"{configured!r} to {current!r}"
                )

    defaults = default_settings()
    _warn_unknown_keys(canonical, set(defaults) | _legacy_keys(), "", warnings)
    for section in ("pool", "control", "providers", "power"):
        _warn_unknown_keys(
            canonical.get(section),
            set(defaults[section]) | _legacy_keys(section),
            section,
            warnings,
        )
    _warn_unknown_keys(canonical.get("review"), set(defaults["review"]), "review", warnings)
    _warn_unknown_keys(
        canonical.get("execution"),
        set(defaults["execution"])
        | {"coordination_overhead_target_pct", "worker_filesystem_policy"},
        "execution",
        warnings,
    )
    execution = canonical.get("execution")
    if isinstance(execution, dict):
        _warn_unknown_keys(
            execution.get("capacity"), set(defaults["execution"]["capacity"]),
            "execution.capacity", warnings,
        )
        capacity = execution.get("capacity")
        if isinstance(capacity, dict):
            _warn_unknown_keys(
                capacity.get("fallback_burn_pct"), {"small", "big"},
                "execution.capacity.fallback_burn_pct", warnings,
            )
    providers = canonical.get("providers")
    if isinstance(providers, dict):
        for provider in ("claude", "codex", "local"):
            _warn_unknown_keys(
                providers.get(provider), set(defaults["providers"][provider]),
                f"providers.{provider}", warnings,
            )
        claude = providers.get("claude")
        if isinstance(claude, dict):
            _warn_unknown_keys(
                claude.get("model_roles"),
                set(defaults["providers"]["claude"]["model_roles"]),
                "providers.claude.model_roles", warnings,
            )
        codex = providers.get("codex")
        if isinstance(codex, dict):
            _warn_unknown_keys(
                codex.get("model_roles"), set(CAPABILITY_TIERS),
                "providers.codex.model_roles", warnings,
            )
            _warn_unknown_keys(
                codex.get("model_by_kind"), set(DEFAULT_SIZE_BY_KIND),
                "providers.codex.model_by_kind", warnings,
            )
    pool = canonical.get("pool")
    if isinstance(pool, dict):
        overrides = pool.get("task_min_headroom_pct_by_seat")
        if isinstance(overrides, dict):
            for seat_id, override in sorted(overrides.items()):
                _warn_unknown_keys(
                    override, {"small", "big"},
                    f"pool.task_min_headroom_pct_by_seat.{seat_id}", warnings,
                )
        multiplier_overrides = pool.get(
            "task_min_headroom_pct_by_capacity_multiplier"
        )
        if isinstance(multiplier_overrides, dict):
            for multiplier, override in sorted(multiplier_overrides.items()):
                _warn_unknown_keys(
                    override, {"small", "big"},
                    "pool.task_min_headroom_pct_by_capacity_multiplier."
                    f"{multiplier}", warnings,
                )
    projects = canonical.get("projects")
    if isinstance(projects, dict):
        for project_id, project in sorted(projects.items()):
            _warn_unknown_keys(
                project, {"path", "hasTrustDialogAccepted", "protected_branches"},
                f"projects.{project_id}", warnings,
            )
    return canonical, warnings


def _percentage(value: Any, path: str) -> None:
    if (
        not isinstance(value, (int, float)) or isinstance(value, bool)
        or not 0 <= value <= 100
    ):
        raise ValueError(f"{path} must be a percentage from 0 to 100")


def _validate_protected_branches(value: Any, path: str) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be an array")
    if len(value) > MAX_PROTECTED_BRANCH_PATTERNS:
        raise ValueError(
            f"{path} must contain at most {MAX_PROTECTED_BRANCH_PATTERNS} patterns"
        )
    seen: set[str] = set()
    for pattern in value:
        if not isinstance(pattern, str):
            raise ValueError(f"{path} must contain branch-pattern strings")
        if not pattern or pattern != pattern.strip():
            raise ValueError(
                f"{path} patterns must be non-empty without surrounding whitespace"
            )
        if len(pattern) > MAX_PROTECTED_BRANCH_PATTERN_LENGTH:
            raise ValueError(
                f"{path} patterns must be at most "
                f"{MAX_PROTECTED_BRANCH_PATTERN_LENGTH} characters"
            )
        if re.search(r"[\x00-\x1f\x7f-\x9f]", pattern):
            raise ValueError(f"{path} patterns must not contain control characters")
        if pattern in seen:
            raise ValueError(f"{path} must not contain duplicate patterns")
        seen.add(pattern)


def _validate_settings(merged: dict[str, Any], root: Path | None = None) -> None:
    from .policy_settings import policy_report

    policy_report(merged)
    if merged.get("schema") != SETTINGS_SCHEMA:
        raise ValueError(f"canonical settings schema must be {SETTINGS_SCHEMA}")
    for section in (
        "pool", "providers", "control", "execution", "review", "power", "projects",
        "local_hosts", "local_seats",
    ):
        if not isinstance(merged.get(section), dict):
            raise ValueError(f"{section} must be an object")

    validate_local_registries(merged)

    pool = merged["pool"]
    validate_schema3(merged, pool_root=root)
    known_seats = set(registered_seat_ids(merged))

    reserves = pool.get("reserves_pct")
    if not isinstance(reserves, dict):
        raise ValueError("pool.reserves_pct must be an object")
    for seat_id, value in reserves.items():
        if seat_id not in known_seats:
            raise ValueError(f"pool.reserves_pct has unknown seat id {seat_id!r}")
        _percentage(value, f"pool.reserves_pct.{seat_id}")
    for key in ("small_task_min_headroom_pct", "big_task_min_headroom_pct"):
        _percentage(pool.get(key), f"pool.{key}")
    overrides = pool.get("task_min_headroom_pct_by_seat")
    if not isinstance(overrides, dict):
        raise ValueError("pool.task_min_headroom_pct_by_seat must be an object")
    for seat_id, override in overrides.items():
        if seat_id not in known_seats:
            raise ValueError(
                "pool.task_min_headroom_pct_by_seat has unknown seat id "
                f"{seat_id!r}"
            )
        if not isinstance(override, dict) or set(override) != {"small", "big"}:
            raise ValueError(
                f"pool.task_min_headroom_pct_by_seat.{seat_id} must define small and big"
            )
        for size in ("small", "big"):
            _percentage(
                override[size],
                f"pool.task_min_headroom_pct_by_seat.{seat_id}.{size}",
            )
    multiplier_overrides = pool.get("task_min_headroom_pct_by_capacity_multiplier")
    if not isinstance(multiplier_overrides, dict):
        raise ValueError(
            "pool.task_min_headroom_pct_by_capacity_multiplier must be an object"
        )
    for multiplier, override in multiplier_overrides.items():
        try:
            numeric_multiplier = float(multiplier)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "pool.task_min_headroom_pct_by_capacity_multiplier keys must be "
                "positive capacity multipliers"
            ) from exc
        if not math.isfinite(numeric_multiplier) or numeric_multiplier <= 0:
            raise ValueError(
                "pool.task_min_headroom_pct_by_capacity_multiplier keys must be "
                "positive capacity multipliers"
            )
        if not isinstance(override, dict) or set(override) != {"small", "big"}:
            raise ValueError(
                "pool.task_min_headroom_pct_by_capacity_multiplier."
                f"{multiplier} must define small and big"
            )
        for size in ("small", "big"):
            _percentage(
                override[size],
                "pool.task_min_headroom_pct_by_capacity_multiplier."
                f"{multiplier}.{size}",
            )
    for key in ("auto_min_usable_pct", "manual_min_usable_pct"):
        _percentage(merged["control"].get(key), f"control.{key}")
    for key in (
        "probe_stale_seconds", "watch_probe_seconds", "telemetry_scan_seconds",
        "account_probe_timeout_seconds", "cap_cooldown_seconds",
    ):
        value = pool.get(key)
        if (
            not isinstance(value, (int, float)) or isinstance(value, bool)
            or value <= 0
        ):
            raise ValueError(f"pool.{key} must be positive")
    release_hours = pool.get("reserve_release_hours")
    if (
        not isinstance(release_hours, (int, float)) or isinstance(release_hours, bool)
        or release_hours < 0
    ):
        raise ValueError("pool.reserve_release_hours must be non-negative")

    execution = merged["execution"]
    from .development_policy import WORKER_ACCESS_MODES

    worker_access = execution.get("worker_access")
    # Guard membership on a string first: a list/dict value is unhashable and
    # would raise TypeError, which load_settings does not catch, instead of
    # failing closed to the restricted default.
    if not isinstance(worker_access, str) or worker_access not in WORKER_ACCESS_MODES:
        raise ValueError(
            "execution.worker_access must be one of "
            + ", ".join(sorted(WORKER_ACCESS_MODES))
        )
    # Compatibility/operator metadata only: validation must not turn this into
    # routing or admission policy.
    if "coordination_overhead_target_pct" in execution:
        _percentage(
            execution["coordination_overhead_target_pct"],
            "execution.coordination_overhead_target_pct",
        )
    parallel_min = execution.get("parallel_min_minutes")
    if (
        not isinstance(parallel_min, (int, float)) or isinstance(parallel_min, bool)
        or parallel_min <= 0
    ):
        raise ValueError("execution.parallel_min_minutes must be positive")
    filesystem_policy = execution.get("worker_filesystem_policy")
    if filesystem_policy is not None and not isinstance(filesystem_policy, dict):
        # Retired route metadata is inert compatibility data. Access resolution
        # rejects any leftover canary grant before worker execution.
        raise ValueError("execution.worker_filesystem_policy must be an object")
    if execution.get("learning_min_samples") != 8:
        raise ValueError("execution.learning_min_samples must be 8")
    if not isinstance(execution.get("head_worker_fallback"), bool):
        raise ValueError("execution.head_worker_fallback must be true or false")
    capacity = execution.get("capacity")
    if not isinstance(capacity, dict):
        raise ValueError("execution.capacity must be an object")
    fallback_burn = capacity.get("fallback_burn_pct")
    if not isinstance(fallback_burn, dict) or set(fallback_burn) != {"small", "big"}:
        raise ValueError(
            "execution.capacity.fallback_burn_pct must define small/big percentages"
        )
    for size, value in fallback_burn.items():
        _percentage(value, f"execution.capacity.fallback_burn_pct.{size}")
    for key in ("margin_pct", "degraded_extra_margin_pct"):
        _percentage(capacity.get(key), f"execution.capacity.{key}")
    for key in ("margin_taper_max_hours", "degraded_grace_seconds"):
        value = capacity.get(key)
        if (
            not isinstance(value, (int, float)) or isinstance(value, bool)
            or value < 0
        ):
            raise ValueError(f"execution.capacity.{key} must be non-negative")
    taper_fraction = capacity.get("margin_taper_window_fraction")
    if (
        not isinstance(taper_fraction, (int, float))
        or isinstance(taper_fraction, bool) or not 0 <= taper_fraction <= 1
    ):
        raise ValueError(
            "execution.capacity.margin_taper_window_fraction must be from 0 to 1"
        )
    sizes = execution.get("default_size_by_kind")
    capabilities = execution.get("capability_tier_by_kind")
    efforts = execution.get("effort_by_kind")
    preferences = execution.get("prefer_provider_by_kind")
    if not isinstance(sizes, dict) or any(value not in {"small", "big"} for value in sizes.values()):
        raise ValueError("execution.default_size_by_kind must map kinds to small|big")
    if not isinstance(capabilities, dict) or any(
        value not in set(CAPABILITY_TIERS) for value in capabilities.values()
    ):
        raise ValueError("execution.capability_tier_by_kind contains an invalid capability")
    if not isinstance(efforts, dict) or any(value not in set(EFFORTS) for value in efforts.values()):
        raise ValueError("execution.effort_by_kind contains an invalid effort")
    if not isinstance(preferences, dict) or any(
        value not in {"codex", "claude"} for value in preferences.values()
    ):
        raise ValueError("execution.prefer_provider_by_kind contains an invalid provider")

    claude = merged["providers"].get("claude")
    codex = merged["providers"].get("codex")
    if not isinstance(claude, dict):
        raise ValueError("providers.claude must be an object")
    if type(claude.get("active_usage_refresh")) is not bool:
        raise ValueError("providers.claude.active_usage_refresh must be boolean")
    if not isinstance(codex, dict):
        raise ValueError("providers.codex must be an object")
    for key in ("small_task_min_headroom_pct", "big_task_min_headroom_pct"):
        _percentage(claude.get(key), f"providers.claude.{key}")
    model_roles = claude.get("model_roles")
    if not isinstance(model_roles, dict) or any(
        not isinstance(model_roles.get(role), str) or not model_roles.get(role)
        for role in ("frontier", "complex", "routine")
    ):
        raise ValueError("providers.claude.model_roles must define frontier/complex/routine")
    if not isinstance(codex.get("supports_ultra"), bool):
        raise ValueError("providers.codex.supports_ultra must be true or false")
    codex_model_roles = codex.get("model_roles")
    if not isinstance(codex_model_roles, dict) or any(
        not isinstance(codex_model_roles.get(role), str)
        or not codex_model_roles.get(role)
        for role in CAPABILITY_TIERS
    ):
        raise ValueError(
            "providers.codex.model_roles must define frontier/complex/routine"
        )
    codex_models_by_kind = codex.get("model_by_kind")
    if not isinstance(codex_models_by_kind, dict) or any(
        not isinstance(model, str) or not model
        for model in codex_models_by_kind.values()
    ):
        raise ValueError("providers.codex.model_by_kind must map kinds to model ids")
    for key in ("command",):
        if not isinstance(claude.get(key), str) or not claude.get(key):
            raise ValueError(f"providers.claude.{key} must be a non-empty string")
    for key in ("eligible_kinds", "frontier_kinds"):
        value = claude.get(key)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"providers.claude.{key} must be an array of strings")
    for key in ("cooldown_seconds", "usage_stale_seconds", "small_canary_max_minutes"):
        value = claude.get(key)
        if (
            not isinstance(value, (int, float)) or isinstance(value, bool)
            or value <= 0
        ):
            raise ValueError(f"providers.claude.{key} must be positive")
    for key in ("model", "expires_at"):
        if claude.get(key) is not None and not isinstance(claude.get(key), str):
            raise ValueError(f"providers.claude.{key} must be a string or null")
    # Shape-only so a label written by a newer build never hard-offs Claude;
    # the interactive manager enforces the known-label set on edits.
    tier = claude.get("subscription_tier")
    if tier is not None and (
        not isinstance(tier, str)
        or not 0 < len(tier) <= MAX_CLAUDE_SUBSCRIPTION_TIER_LENGTH
        or tier != tier.strip()
        or re.search(r"[\x00-\x1f\x7f-\x9f]", tier)
    ):
        raise ValueError(
            "providers.claude.subscription_tier must be null or a short label "
            "without surrounding whitespace or control characters"
        )
    for key in ("caffeinate", "require_ac"):
        if not isinstance(merged["power"].get(key), bool):
            raise ValueError(f"power.{key} must be true or false")
    for project_id, project in merged["projects"].items():
        if not isinstance(project, dict) or not isinstance(project.get("path"), str):
            raise ValueError(f"projects.{project_id} must be an object with a string path")
        # Compatibility/operator metadata only: this is deliberately not push
        # or release enforcement.
        if "protected_branches" in project:
            _validate_protected_branches(
                project["protected_branches"],
                f"projects.{project_id}.protected_branches",
            )


def load_settings(root: Path) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    defaults = default_settings()
    path = settings_path(root)
    if not path.exists():
        warnings.append(
            f"settings missing at {path}; explicit seat onboarding is required and "
            "Claude is hard-off"
        )
        return defaults, warnings
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("top level must be an object")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        warnings.append(
            f"invalid settings ({exc}); no seat capacity was synthesized; "
            "repair settings.json before dispatch"
        )
        return default_settings(), warnings
    try:
        canonical, canonical_warnings = _canonicalize_settings(raw)
        warnings.extend(canonical_warnings)
        merged = _merge(defaults, canonical)
        try:
            _validate_settings(merged, root=root)
        except (KeyError, ValueError) as local_exc:
            # A broken optional local registry must not erase valid frontier-seat
            # topology or silently hard-off Claude. Retry once with only local
            # execution disabled; if anything else is invalid, retain the normal
            # fail-closed whole-document recovery below.
            without_local = copy.deepcopy(merged)
            without_local["local_hosts"] = {}
            without_local["local_seats"] = {}
            local_providers = without_local.get("providers")
            if not isinstance(local_providers, dict):
                raise local_exc
            local_providers["local"] = copy.deepcopy(
                defaults["providers"]["local"]
            )
            try:
                _validate_settings(without_local, root=root)
            except (KeyError, ValueError):
                raise local_exc
            merged = without_local
            warnings.append(
                f"invalid local seat configuration ({local_exc}); local routing forced off"
            )
    except (KeyError, ValueError) as exc:
        warnings.append(
            f"invalid settings shape ({exc}); no Codex seat capacity was synthesized; "
            "repair settings.json before dispatch"
        )
        return default_settings(), warnings
    mode = merged["providers"]["claude"].get("mode")
    if mode not in VALID_CLAUDE_MODES:
        merged["providers"]["claude"]["mode"] = "off"
        warnings.append(f"invalid Claude mode {mode!r}; forced off")
    return merged, warnings


SettingsMutationHook = Callable[[str], None]


@dataclass
class SettingsTransaction:
    """The locked document and observable result of one settings mutation."""

    settings: dict[str, Any]
    before: str | None
    warnings: list[str]
    changed: bool = False
    after: str | None = None
    backup_path: Path | None = None


def _read_settings_payload(path: Path) -> bytes:
    """Read one owner-controlled settings file through a pinned descriptor."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise SettingsMutationError(
            "this platform cannot securely inspect settings.json"
        )
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | nofollow
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise SettingsMutationError(f"settings missing at {path}") from exc
    except OSError as exc:
        raise SettingsMutationError(
            "settings.json must be a regular, non-symbolic-link file"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise SettingsMutationError(
                "settings.json must be a singly linked regular file"
            )
        if before.st_uid != os.geteuid() or before.st_mode & (
            stat.S_IWGRP | stat.S_IWOTH
        ):
            raise SettingsMutationError(
                "settings.json must be owner-controlled and not group/world-writable"
            )
        if before.st_size > MAX_SETTINGS_BYTES:
            raise SettingsMutationError("settings.json exceeds the size limit")
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_SETTINGS_BYTES:
            chunk = os.read(
                descriptor,
                min(16 * 1024, MAX_SETTINGS_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > MAX_SETTINGS_BYTES:
            raise SettingsMutationError("settings.json exceeds the size limit")
        after = os.fstat(descriptor)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(
            getattr(before, field) != getattr(after, field)
            for field in stable_fields
        ) or total != before.st_size:
            raise SettingsMutationError("settings.json changed while it was read")
        try:
            current = path.lstat()
        except OSError as exc:
            raise SettingsMutationError(
                "settings.json changed while it was read"
            ) from exc
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise SettingsMutationError("settings.json changed while it was read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _validated_settings_document(
    raw: dict[str, Any], *, root: Path
) -> tuple[dict[str, Any], list[str]]:
    """Canonicalize and fully validate a schema-3 document."""
    try:
        canonical, warnings = _canonicalize_settings(raw)
        effective = _merge(default_settings(), canonical)
        _validate_settings(effective, root=root)
        mode = effective["providers"]["claude"].get("mode")
        if mode not in VALID_CLAUDE_MODES:
            raise ValueError(
                "providers.claude.mode must be one of "
                + ", ".join(sorted(VALID_CLAUDE_MODES))
            )
    except (KeyError, ValueError) as exc:
        raise SettingsMutationError(f"invalid settings shape: {exc}") from exc
    return canonical, warnings


def _load_settings_for_mutation(
    root: Path, *, require_existing: bool
) -> tuple[dict[str, Any], str | None, bytes | None, list[str]]:
    """Read and validate the current file while the caller holds settings_lock."""
    path = settings_path(root)
    try:
        path.lstat()
    except FileNotFoundError:
        if require_existing:
            raise SettingsMutationError(f"settings missing at {path}")
        raw = default_settings()
        canonical, warnings = _validated_settings_document(raw, root=root)
        return canonical, None, None, warnings
    except OSError as exc:
        raise SettingsMutationError(f"cannot inspect {path}: {exc}") from exc

    payload = _read_settings_payload(path)
    try:
        before = payload.decode("utf-8")
        raw = json.loads(before)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SettingsMutationError(
            f"cannot read valid JSON (UTF-8 required) from {path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise SettingsMutationError("settings top level must be an object")
    canonical, warnings = _validated_settings_document(raw, root=root)
    return canonical, before, payload, warnings


def _secure_settings_backup(root: Path, payload: bytes) -> Path:
    """Create or verify the owner-only backup named by the complete content hash."""
    digest = hashlib.sha256(payload).hexdigest()
    backup = root / f"settings.json.backup-sha256-{digest}"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(backup, flags, 0o600)
    except FileExistsError:
        existing = _read_settings_payload(backup)
        backup_stat = backup.lstat()
        if backup_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise SettingsMutationError(
                f"content-addressed settings backup must be owner-only: {backup}"
            )
        if existing != payload:
            raise SettingsMutationError(
                f"content-addressed settings backup does not match its name: {backup}"
            )
        return backup
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            backup.unlink()
        except OSError:
            pass
        raise
    _fsync_directory(root)
    return backup


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _assert_settings_source_unchanged(path: Path, payload: bytes | None) -> None:
    """Refuse to replace a path changed by a writer that ignored the lock."""
    if payload is None:
        try:
            path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise SettingsMutationError(
                "settings.json changed before atomic replacement"
            ) from exc
        raise SettingsMutationError("settings.json appeared before atomic replacement")
    try:
        current = _read_settings_payload(path)
    except SettingsMutationError:
        raise
    except OSError as exc:
        raise SettingsMutationError(
            "settings.json changed before atomic replacement"
        ) from exc
    if current != payload:
        raise SettingsMutationError("settings.json changed before atomic replacement")


@contextmanager
def settings_transaction(
    root: Path,
    *,
    require_existing: bool = False,
    _hook: SettingsMutationHook | None = None,
) -> Iterator[SettingsTransaction]:
    """Securely perform one settings read-modify-write transaction.

    The settings lock is always acquired before the current file is loaded. The
    yielded document is canonical but retains all unknown fields. A successful
    exit validates it again and, only when bytes change, backs up the prior file
    before an atomic replacement. This helper deliberately knows nothing about
    the provider ledger so callers cannot invert the settings-to-ledger order.

    ``_hook`` exposes stable fault/interleaving points to deterministic tests; it
    is private so production callers cannot turn transaction phases into API.
    """
    with settings_lock(root):
        settings, before, payload, warnings = _load_settings_for_mutation(
            root, require_existing=require_existing
        )
        transaction = SettingsTransaction(settings, before, warnings)
        if _hook is not None:
            _hook("after_reload")
        yield transaction

        canonical, final_warnings = _validated_settings_document(
            transaction.settings, root=root
        )
        transaction.settings = canonical
        for warning in final_warnings:
            if warning not in transaction.warnings:
                transaction.warnings.append(warning)
        after = json.dumps(canonical, indent=2) + "\n"
        transaction.after = after
        encoded = after.encode("utf-8")
        if payload is not None and encoded == payload:
            return

        if payload is not None:
            transaction.backup_path = _secure_settings_backup(root, payload)
            if _hook is not None:
                _hook("after_backup")
        def before_replace() -> None:
            if _hook is not None:
                _hook("before_replace")
            _assert_settings_source_unchanged(settings_path(root), payload)

        atomic_write_json(
            settings_path(root),
            canonical,
            mode=0o600,
            _before_replace=before_replace,
        )
        transaction.changed = True
        if _hook is not None:
            _hook("after_replace")


def atomic_write_json(
    path: Path,
    data: dict[str, Any],
    mode: int = 0o600,
    *,
    _before_replace: Callable[[], None] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if _before_replace is not None:
            _before_replace()
        os.replace(temp_name, path)
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def effective_claude_mode(settings: dict[str, Any], at: float | None = None) -> tuple[str, str | None]:
    config = settings.get("providers", {}).get("claude", {})
    mode = config.get("mode", "off")
    if mode not in VALID_CLAUDE_MODES:
        return "off", "invalid configuration"
    expiry = parse_iso(config.get("expires_at"))
    if expiry is not None and (at if at is not None else now()) >= expiry:
        return "off", f"expired at {config.get('expires_at')}"
    return mode, None


_EXPIRY_UNCHANGED = object()


def set_claude_mode(
    root: Path,
    mode: str,
    *,
    expires_at: str | None | object = _EXPIRY_UNCHANGED,
    _hook: SettingsMutationHook | None = None,
) -> dict[str, Any]:
    if mode not in VALID_CLAUDE_MODES:
        raise ValueError(f"invalid Claude mode: {mode}")
    try:
        with settings_transaction(root, _hook=_hook) as transaction:
            providers = transaction.settings.setdefault("providers", {})
            if not isinstance(providers, dict):
                raise SettingsMutationError("providers must be an object")
            claude = providers.setdefault("claude", {})
            if not isinstance(claude, dict):
                raise SettingsMutationError("providers.claude must be an object")
            claude["mode"] = mode
            if expires_at is not _EXPIRY_UNCHANGED:
                claude["expires_at"] = expires_at
    except SettingsMutationError as exc:
        raise ValueError(f"refusing to overwrite invalid settings: {exc}") from exc
    return transaction.settings


def set_worker_access(root: Path, mode: str) -> dict[str, Any]:
    """Select or roll back worker access through the shared transaction."""
    from .development_policy import WORKER_ACCESS_MODES

    if mode not in WORKER_ACCESS_MODES:
        raise ValueError(f"invalid worker access mode: {mode}")
    try:
        with settings_transaction(root) as transaction:
            execution = transaction.settings.setdefault("execution", {})
            if not isinstance(execution, dict):
                raise SettingsMutationError("execution must be an object")
            execution["worker_access"] = mode
    except SettingsMutationError as exc:
        raise ValueError(f"refusing to overwrite invalid settings: {exc}") from exc
    return transaction.settings


def new_seat() -> dict[str, Any]:
    return {
        "type": "unknown",
        "email": None,
        "status": "needs-login",
        "status_until": None,
        "auth_quarantined_at": None,
        "auth_failure": None,
        "pct_left": None,
        "probed_at": None,
        "spent_since_probe": 0,
        "in_flight": None,
        "last_job_at": None,
        "last_control_at": None,
        "last_probe_limits": {},
        "banked_resets": {"status": "unknown", "available_count": None,
                          "credits": [], "history": [], "attempts": []},
        # Window-level reset tracking survives partial provider snapshots. A
        # later response that reports only one of two rolling windows must not
        # erase the other still-future reset date.
        "usage_reset_windows": {},
        # Describes the observation which produced the currently visible
        # active-window view. Completeness is deliberately independent of the
        # source: rollout telemetry is partial, while a structurally complete
        # account enumeration may authoritatively remove omitted windows.
        "active_window_observation": None,
        # Account sweep health is kept separate from the last-good active
        # usage view so a transient read failure cannot erase usable limits.
        "account_window_observation": {
            "status": None,
            "last_attempt_at": None,
            "last_attempt_observed_at": None,
            "last_attempt_complete": None,
            "last_success_at": None,
            "last_success_observed_at": None,
            "last_complete_at": None,
            "last_complete_observed_at": None,
            "last_failure_at": None,
            "last_failure_observed_at": None,
            "last_error": None,
            "stale_since": None,
        },
        # Provider identity is cached for the interactive manager only. The
        # metadata-only fingerprint suppresses a prior login's identity.
        "manager_identity": {
            "schema": 1,
            "status": "unavailable",
            "email": None,
            "observed_plan_type": None,
            "account_id": None,
            "observed_at": None,
            "last_attempt_at": None,
            "auth_file_fingerprint": None,
            "reason": "identity has not been refreshed",
        },
        # Kept separately so a long-running process with older passive-
        # telemetry code cannot permanently mask a newer structured account
        # read. New readers restore this snapshot when the visible fields have
        # been rolled back to an older observation.
        "account_usage_snapshot": None,
        # Literal optional metadata from the most recent structured provider
        # rate-limit bucket. Neither field participates in admission.
        "account_usage_plan_type": None,
        "account_usage_credits": None,
        "usage_source": None,
        "usage_updated_at": None,
        "telemetry_file": None,
        "telemetry_mtime_ns": None,
        "session_total_tokens": 0,
        "session_cached_tokens": 0,
        "session_output_tokens": 0,
        "usage_cycles": {},
    }


def canonicalize_seat_limit_type(seat: dict[str, Any]) -> bool:
    """Repair cadence-era seat types from the current active limit set.

    Window names and durations are provider data, not seat roles. Historical
    ``usage_cycles`` deliberately remain untouched.
    """
    changed = False
    limits = seat.get("last_probe_limits")
    canonical = "window" if isinstance(limits, dict) and limits else "unknown"
    if seat.get("type") != canonical:
        seat["type"] = canonical
        changed = True
    snapshot = seat.get("account_usage_snapshot")
    if isinstance(snapshot, dict):
        snapshot_limits = snapshot.get("limits")
        snapshot_type = (
            "window" if isinstance(snapshot_limits, dict) and snapshot_limits else "unknown"
        )
        if snapshot.get("type") != snapshot_type:
            snapshot["type"] = snapshot_type
            changed = True
    return changed


def canonicalize_manager_identity(seat: dict[str, Any]) -> bool:
    """Remove retired workspace cache fields and add current plan slots."""
    identity = seat.get("manager_identity")
    if not isinstance(identity, dict):
        return False
    changed = False
    for retired in ("workspace_name", "workspace_id"):
        if retired in identity:
            identity.pop(retired)
            changed = True
    for current in ("observed_plan_type",):
        if current not in identity:
            identity[current] = None
            changed = True
    return changed


def restore_account_usage_snapshot(seat: dict[str, Any]) -> bool:
    """Restore a structured account read masked by an older passive writer."""
    snapshot = seat.get("account_usage_snapshot")
    if not isinstance(snapshot, dict):
        return False
    limits = snapshot.get("limits")
    observed_at = snapshot.get("observed_at")
    if not isinstance(limits, dict) or not isinstance(observed_at, (int, float)):
        return False
    # Compare against usage_updated_at/probed_at (iso), NOT the numeric
    # usage_observed_at: the adversary here is a *legacy* writer that rolls back
    # those visible fields but leaves usage_observed_at stale-high (it predates
    # that field), so the numeric key can't detect the rollback. Subsecond
    # ordering of concurrent *current* writers is handled in _apply_latest.
    snapshot_at = parse_iso(snapshot.get("updated_at"))
    if snapshot_at is None:
        snapshot_at = float(observed_at)
    visible_at = parse_iso(seat.get("usage_updated_at") or seat.get("probed_at"))
    active_observation = seat.get("active_window_observation")
    snapshot_observation = snapshot.get("active_window_observation")
    same_authoritative_observation = (
        isinstance(active_observation, dict)
        and active_observation.get("source") == "account"
        and isinstance(active_observation.get("observed_at"), (int, float))
        and float(active_observation["observed_at"]) == float(observed_at)
    )
    pct_left = snapshot.get("pct_left")
    if pct_left is not None and not isinstance(pct_left, (int, float)):
        return False
    visible_matches = (
        seat.get("last_probe_limits") == limits
        and seat.get("pct_left") == pct_left
        and seat.get("type") == snapshot.get("type")
        and seat.get("usage_reset_windows") == snapshot.get("usage_reset_windows")
        and (
            not isinstance(snapshot_observation, dict)
            or active_observation == snapshot_observation
        )
    )
    if visible_at is not None and snapshot_at <= visible_at and not (
        same_authoritative_observation and not visible_matches
    ):
        return False
    timestamp = snapshot.get("updated_at") or iso(float(observed_at))
    seat["last_probe_limits"] = copy.deepcopy(limits)
    seat["pct_left"] = pct_left
    seat["type"] = "window" if limits else "unknown"
    seat["probed_at"] = timestamp
    seat["usage_updated_at"] = timestamp
    seat["usage_observed_at"] = float(observed_at)
    seat["usage_source"] = "account"
    if isinstance(snapshot_observation, dict):
        seat["active_window_observation"] = copy.deepcopy(snapshot_observation)
    else:
        seat["active_window_observation"] = {
            "source": "account",
            "complete": bool(snapshot.get("complete", False)),
            "observed_at": float(observed_at),
            "updated_at": timestamp,
            "observed_windows": sorted(limits),
            "active_windows": sorted(limits),
        }
    seat["spent_since_probe"] = snapshot.get("spent_since_probe", 0)
    reset_windows = snapshot.get("usage_reset_windows")
    if isinstance(reset_windows, dict):
        seat["usage_reset_windows"] = copy.deepcopy(reset_windows)
    cycles = snapshot.get("usage_cycles")
    if isinstance(cycles, dict):
        seat["usage_cycles"] = copy.deepcopy(cycles)
    if (
        seat.get("status") == "capped"
        and isinstance(pct_left, (int, float))
        and float(pct_left) > 0
    ):
        seat["status"], seat["status_until"] = "ok", None
    canonicalize_seat_limit_type(seat)
    return True


def new_provider() -> dict[str, Any]:
    return {
        "status": "ok",
        "status_until": None,
        "status_before_off": None,
        "cooldown_provenance": None,
        "in_flight": None,
        "last_job_at": None,
        "tokens_7d": 0,
        "last_error": None,
        "models": {},
    }


class PoolSnapshotError(RuntimeError):
    """Raised when the on-disk pool cannot be read as a consistent snapshot."""


def _open_snapshot_file(path: Path, label: str):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise PoolSnapshotError(
            f"cannot read pool snapshot: {label} is missing at {path}"
        ) from exc
    except OSError as exc:
        raise PoolSnapshotError(
            f"cannot open pool snapshot {label} read-only at {path}: {exc}"
        ) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PoolSnapshotError(
                f"cannot read pool snapshot: {label} is not a regular file at {path}"
            )
        if info.st_uid != os.getuid():
            raise PoolSnapshotError(
                f"cannot read pool snapshot: {label} has the wrong owner at {path}"
            )
        return os.fdopen(fd, "rb")
    except Exception:
        os.close(fd)
        raise


def _sanitize_pool_snapshot(
    raw: Any,
    path: Path,
    materialized_seat_ids: list[str],
) -> dict[str, Any]:
    """Validate persisted state and add display defaults to an in-memory copy."""
    if not isinstance(raw, dict):
        raise PoolSnapshotError(f"invalid pool state at {path}: top level must be an object")

    snapshot = copy.deepcopy(raw)
    seats = snapshot.get("seats")
    if seats is None:
        seats = snapshot["seats"] = {}
    if not isinstance(seats, dict):
        raise PoolSnapshotError(f"invalid pool state at {path}: seats must be an object")
    registered = set(materialized_seat_ids)
    for seat_id in materialized_seat_ids:
        seats.setdefault(seat_id, new_seat())
    for seat_id, seat in seats.items():
        if not isinstance(seat_id, str) or not isinstance(seat, dict):
            raise PoolSnapshotError(
                f"invalid pool state at {path}: every seat must be a named object"
            )
        # Unregistered entries are historical attribution, not operational
        # seats. Preserve them byte-for-byte in the in-memory snapshot; only
        # authoritative registry members receive current-reader defaults.
        if seat_id not in registered:
            continue
        for key, value in new_seat().items():
            seat.setdefault(key, copy.deepcopy(value))
        canonicalize_seat_limit_type(seat)
        canonicalize_manager_identity(seat)
        for key in (
            "last_probe_limits",
            "usage_reset_windows",
            "usage_cycles",
            "account_window_observation",
            "banked_resets",
        ):
            if not isinstance(seat.get(key), dict):
                raise PoolSnapshotError(
                    f"invalid pool state at {path}: seats.{seat_id}.{key} must be an object"
                )
        active_observation = seat.get("active_window_observation")
        if active_observation is not None and not isinstance(active_observation, dict):
            raise PoolSnapshotError(
                f"invalid pool state at {path}: "
                f"seats.{seat_id}.active_window_observation must be an object or null"
            )

    for key in ("jobs",):
        value = snapshot.setdefault(key, [])
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise PoolSnapshotError(
                f"invalid pool state at {path}: {key} must be an array of objects"
            )
    capacity_learning = snapshot.setdefault("capacity_learning", {})
    if not isinstance(capacity_learning, dict):
        raise PoolSnapshotError(
            f"invalid pool state at {path}: capacity_learning must be an object"
        )

    providers = snapshot.setdefault("providers", {})
    if not isinstance(providers, dict):
        raise PoolSnapshotError(f"invalid pool state at {path}: providers must be an object")
    claude = providers.setdefault("claude", new_provider())
    if not isinstance(claude, dict):
        raise PoolSnapshotError(
            f"invalid pool state at {path}: providers.claude must be an object"
        )
    for key, value in new_provider().items():
        claude.setdefault(key, copy.deepcopy(value))
    if not isinstance(claude.get("models"), dict):
        raise PoolSnapshotError(
            f"invalid pool state at {path}: providers.claude.models must be an object"
        )
    return snapshot


def read_pool_snapshot(root: Path) -> dict[str, Any]:
    """Read a coherent pool snapshot without creating, repairing, or writing files."""
    path = root / "seats.json"
    lock_path = root / ".ledger.lock"
    lock_fh = _open_snapshot_file(lock_path, "lock file")

    try:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_SH)
        except OSError as exc:
            raise PoolSnapshotError(
                f"cannot take shared pool snapshot lock at {lock_path}: {exc}"
            ) from exc
        try:
            # Settings mutations take settings -> ledger. Once this shared
            # ledger lock is held, an in-flight mutation cannot publish a new
            # registry until this coherent snapshot is complete. Reading the
            # atomically-replaced settings file does not acquire its lock.
            settings, _warnings = load_settings(root)
            materialized_seat_ids = registered_seat_ids(settings)
            try:
                with _open_snapshot_file(path, "state file") as state_fh:
                    raw = json.load(state_fh)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise PoolSnapshotError(
                    f"cannot read consistent pool snapshot at {path}: {exc}"
                ) from exc
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
    finally:
        lock_fh.close()
    return _sanitize_pool_snapshot(raw, path, materialized_seat_ids)


def load_claude_rate_limits(root: Path, at: float | None = None) -> dict[str, Any]:
    """Load passively captured Claude subscription windows, dropping expired data."""
    try:
        payload = json.loads((root / "claude-usage.json").read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    current = now() if at is None else at
    result: dict[str, Any] = {}
    updated_at = payload.get("updated_at")
    updated_at = float(updated_at) if isinstance(updated_at, (int, float)) else None
    observations: list[float] = []
    for name in ("five_hour", "seven_day"):
        raw = payload.get(name)
        if not isinstance(raw, dict):
            continue
        try:
            used = float(raw["used_percentage"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= used <= 100:
            continue
        resets_at = raw.get("resets_at")
        if resets_at is not None:
            try:
                resets_at = float(resets_at)
            except (TypeError, ValueError):
                continue
            if resets_at <= current:
                continue
        result[name] = {
            "used_percentage": used,
            "remaining_percentage": 100 - used,
            "resets_at": resets_at,
        }
        observed_at = raw.get("observed_at", updated_at)
        if isinstance(observed_at, (int, float)):
            observed_at = float(observed_at)
            result[name]["observed_at"] = observed_at
            observations.append(observed_at)
    if not any(name in result for name in ("five_hour", "seven_day")):
        return {}
    if observations:
        # Big Claude work requires both windows to be fresh, so report the age
        # of the oldest retained observation rather than the newest callback.
        result["updated_at"] = min(observations)
    elif updated_at is not None:
        result["updated_at"] = updated_at
    cycles = payload.get("usage_cycles")
    if isinstance(cycles, dict):
        result["usage_cycles"] = copy.deepcopy(cycles)
    return result


def load_claude_quota_evidence(root: Path, at: float | None = None) -> dict[str, Any]:
    """Load strictly validated quota-window evidence for cooldown decisions.

    Unlike ``load_claude_rate_limits`` — which keeps synthesizing per-window
    ``observed_at`` from the snapshot's global ``updated_at`` for ordinary
    usage readers — this path preserves only observation times the persisted
    file genuinely supplied per window, so a global-only or partially refreshed
    snapshot can never vouch for a window it did not actually observe.
    """
    try:
        payload = json.loads((root / "claude-usage.json").read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return quota_evidence_from_snapshot(payload, now() if at is None else at)


def claude_usage_age_seconds(limits: dict[str, Any], at: float | None = None) -> float | None:
    updated_at = limits.get("updated_at")
    if not isinstance(updated_at, (int, float)):
        return None
    return max(0.0, (now() if at is None else at) - float(updated_at))


def claude_usage_fresh(
    settings: dict[str, Any], limits: dict[str, Any], at: float | None = None
) -> bool:
    age = claude_usage_age_seconds(limits, at)
    stale_after = float(
        settings.get("providers", {}).get("claude", {}).get("usage_stale_seconds", 6 * 3600)
    )
    return age is not None and age <= stale_after


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # A restricted Codex tool sandbox can see the ledger but cannot signal
        # its host-side wrapper. EPERM means "not observable", not "dead".
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    except (TypeError, ValueError):
        return False


def claim_alive(claim: dict[str, Any] | None) -> bool:
    if not claim:
        return False
    return pid_alive(claim.get("dispatcher_pid")) or pid_alive(claim.get("child_pid"))


class Ledger:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "seats.json"
        self.lock_path = root / ".ledger.lock"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._fh = open(self.lock_path, "a+")

    def __enter__(self) -> dict[str, Any]:
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        try:
            self._new_file = not self.path.exists()
            if not self._new_file:
                self.data = json.loads(self.path.read_text())
            else:
                self.data = {"seats": {}, "jobs": []}
            self._migrate()
            self._original = json.dumps(self.data, sort_keys=True, separators=(",", ":"))
            return self.data
        except Exception:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            raise

    def _migrate(self) -> None:
        # _original is captured AFTER _migrate, so a repair applied here would
        # otherwise never be seen as dirty and never persist — the on-disk
        # rollback would be re-repaired in memory on every open, forever, and
        # never healed. Track it explicitly so __exit__ writes the fix back.
        self._repaired = self._new_file
        self.data.setdefault("seats", {})
        # The registry is authoritative: only registered seats are materialized,
        # so an empty installation burns no IDs before its first allocation.
        # This is an atomic read only; Ledger must never acquire the settings
        # mutation lock.
        settings, _warnings = load_settings(self.root)
        for seat_id in registered_seat_ids(settings):
            if seat_id not in self.data["seats"]:
                self.data["seats"][seat_id] = new_seat()
                self._repaired = True
            current = self.data["seats"][seat_id]
            for key, value in new_seat().items():
                if key not in current:
                    current[key] = copy.deepcopy(value)
                    self._repaired = True
            # Apply the compatibility guard for every current reader, not only
            # dashboard paths that also scan passive telemetry.
            if restore_account_usage_snapshot(current):
                self._repaired = True
            if canonicalize_seat_limit_type(current):
                self._repaired = True
            if canonicalize_manager_identity(current):
                self._repaired = True
        self.data.setdefault("jobs", [])
        self.data.setdefault("capacity_learning", {})
        self.data.setdefault("providers", {})
        claude = self.data["providers"].setdefault("claude", new_provider())
        for key, value in new_provider().items():
            claude.setdefault(key, copy.deepcopy(value))
        if not isinstance(claude.get("models"), dict):
            claude["models"] = {}
        self.data["schema"] = 6

    def flush(self) -> None:
        """Durably persist the current ledger while retaining its lock.

        Seat allocation uses this write-ahead boundary before settings.json is
        published.  Keeping the exclusive lock held across this flush and the
        later settings replacement preserves the settings->ledger lock order.
        """
        atomic_write_json(self.path, self.data)
        self._original = json.dumps(
            self.data, sort_keys=True, separators=(",", ":")
        )
        self._repaired = False

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            changed = json.dumps(self.data, sort_keys=True, separators=(",", ":")) != self._original
            # A snapshot repair mutates data before _original is captured, so it
            # is invisible to the diff above — force the write so it persists.
            if exc_type is None and (changed or getattr(self, "_repaired", False)):
                atomic_write_json(self.path, self.data)
        finally:
            # A failed atomic write must never strand the process-wide pool.
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()


def runtime_seat_home(root: Path, seat_id: str, settings: dict[str, Any]) -> Path:
    """Return the Codex home actually used for a seat.

    A registered seat may name an explicit home (for example the interactive
    ~/.codex home, so Remote, refresh-token rotation, and terminal orchestration
    remain one identity); otherwise it uses its isolated home below the pool root.
    """
    entry = settings.get("pool", {}).get("seats", {}).get(seat_id)
    if not isinstance(entry, dict):
        raise ValueError(f"seat {seat_id!r} is not registered")
    return resolve_registry_home(root, seat_id, entry)


def has_auth(root: Path, seat_id: str, settings: dict[str, Any]) -> bool:
    """Return whether auth metadata is safe without opening credential contents."""
    try:
        info = (runtime_seat_home(root, seat_id, settings) / "auth.json").lstat()
    except OSError:
        return False
    return bool(
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_uid == os.geteuid()
        and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    )


def refresh_transients(data: dict[str, Any], root: Path, settings: dict[str, Any]) -> None:
    current = now()
    registered = set(registered_seat_ids(settings))
    for seat_id, seat in data["seats"].items():
        if seat.get("status") == "capped" and seat.get("status_until") and current > seat["status_until"]:
            seat["status"], seat["status_until"] = "ok", None
        if seat_id in registered:
            if not has_auth(root, seat_id, settings):
                seat["status"] = "needs-login"
            elif seat.get("status") == "needs-login" and not seat.get("auth_quarantined_at"):
                if seat.get("status_until") and current < seat["status_until"]:
                    seat["status"] = "capped"
                else:
                    seat["status"], seat["status_until"] = "ok", None
        in_flight = seat.get("in_flight")
        if in_flight and not claim_alive(in_flight):
            seat["in_flight"] = None
    provider = data["providers"]["claude"]
    mode, _ = effective_claude_mode(settings, current)
    if mode == "off":
        if provider.get("status") != "off":
            provider["status_before_off"] = provider.get("status")
        provider["status"] = "off"
    elif provider.get("status") == "off":
        previous = provider.get("status_before_off")
        if provider.get("status_until") and current < provider["status_until"]:
            provider["status"] = "cooldown"
        elif previous in {"missing", "needs-auth"}:
            provider["status"] = previous
        else:
            provider["status"], provider["status_until"] = "ok", None
        provider["status_before_off"] = None
    if provider.get("status") == "cooldown":
        if provider.get("status_until") and current > provider["status_until"]:
            provider["status"], provider["status_until"] = "ok", None
            provider["cooldown_provenance"] = None
        elif isinstance(provider.get("cooldown_provenance"), dict) and (
            quota_cooldown_recovered(
                provider["cooldown_provenance"],
                load_claude_quota_evidence(root, current),
                at=current,
                usage_stale_seconds=float(
                    settings.get("providers", {})
                    .get("claude", {})
                    .get("usage_stale_seconds", 6 * 3600)
                ),
            )
        ):
            # Evidence-gated early recovery for a typed provider-wide quota
            # cooldown only. needs-auth/missing/off and per-model caps are
            # never cleared here, and last_error stays as historical evidence.
            provider["status"], provider["status_until"] = "ok", None
            provider["cooldown_provenance"] = None
    models = provider.setdefault("models", {})
    if not isinstance(models, dict):
        models = provider["models"] = {}
    if isinstance(models, dict):
        for model in models.values():
            if not isinstance(model, dict):
                continue
            if model.get("status") == "capped" and model.get("status_until"):
                if current > model["status_until"]:
                    model["status"], model["status_until"] = "ok", None
    in_flight = provider.get("in_flight")
    if in_flight and not claim_alive(in_flight):
        provider["in_flight"] = None


def seat_roles(settings: dict[str, Any]) -> dict[str, str]:
    return derive_registry_roles(settings)


def reserve_for(
    settings: dict[str, Any], seat_id: str, seat: dict[str, Any], reference: datetime | None = None
) -> float:
    """Return the effective capacity reserve for a Codex seat.

    Drain-first routing spends all reported Codex capacity that passes the
    independent fit checks. ``pool.reserves_pct`` and its release setting are
    deliberately retained as inert compatibility configuration, but no longer
    reduce usable headroom or admission capacity.
    """
    del settings, seat_id, seat, reference
    return 0.0


def usable_headroom(
    settings: dict[str, Any],
    seat_id: str,
    seat: dict[str, Any],
    reference: datetime | None = None,
) -> float:
    pct = seat.get("pct_left")
    pct = 50.0 if pct is None else float(pct)
    return pct - reserve_for(settings, seat_id, seat, reference)


def task_min_headroom(settings: dict[str, Any], size: str) -> float:
    key = "big_task_min_headroom_pct" if size == "big" else "small_task_min_headroom_pct"
    fallback = 30 if size == "big" else 10
    return float(settings.get("pool", {}).get(key, fallback))


def seat_task_min_headroom(settings: dict[str, Any], seat_id: str, size: str) -> float:
    pool = settings.get("pool", {})
    override = pool.get("task_min_headroom_pct_by_seat", {}).get(seat_id)
    if isinstance(override, dict):
        value = override.get(size)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    entry = pool.get("seats", {}).get(seat_id)
    multiplier_overrides = pool.get("task_min_headroom_pct_by_capacity_multiplier", {})
    if isinstance(entry, dict) and isinstance(multiplier_overrides, dict):
        multiplier = effective_relative_capacity(entry)
        # JSON object keys are strings. Accept a naturally formatted key ("5")
        # and its exact float spelling ("5.0") for operator convenience.
        keys = (f"{multiplier:g}", str(multiplier))
        class_override = next(
            (
                multiplier_overrides[key]
                for key in keys
                if isinstance(multiplier_overrides.get(key), dict)
            ),
            None,
        )
        if isinstance(class_override, dict):
            value = class_override.get(size)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
    return task_min_headroom(settings, size)


def default_task_size(settings: dict[str, Any], kind: str) -> str:
    return resolve_task_policy(settings, kind).size
