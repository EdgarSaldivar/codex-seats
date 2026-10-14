"""Operator CLI for the schema-3 dynamic seat registry.

Only sanitized registry and health facts cross this module's output boundary.
Authentication files are never opened, and provider command output is never
included in a report.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import stat
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import availability, bounded_process
from .account import banked_reset_report, refresh_account_usage
from .capacity import (
    active_capacity_windows,
    empirical_burn_ratio_report,
    seat_capacity_report,
)
from .operator_reporting import schema3_operator_report
from .controller_context import current_controller
from .seat_admin import (
    SeatAdminError,
    SeatMutationResult,
    _read_ledger_for_preview,
    activate_seat,
    add_seat,
    deactivate_seat,
    drain_seat,
    plan_mutation,
    remove_seat,
    retire_seat,
    set_role,
    set_seat,
)
from .seat_registry import (
    CAPACITY_SOURCES,
    SeatRegistryError,
    capacity_fingerprint,
    derive_roles,
    effective_relative_capacity,
    registered_seat_ids,
    resolve_home,
    resolve_readonly_selector,
    sort_seat_ids,
    validate_schema3,
    validate_seat_id,
)
from .state import claim_alive, has_auth, load_claude_rate_limits, load_settings, parse_iso
from .workers import TESTED_CODEX, resolve_executable


OUTPUT_SCHEMA = 1
SUPPORTED_CODEX_VERSION = TESTED_CODEX
LOGIN_DOCUMENTATION = "https://developers.openai.com/codex/cli/reference"
LOCAL_ONLY_NOTICE = (
    "Local seat lifecycle changes do not alter OpenAI workspace membership or "
    "billing, revoke tokens, kill interactive sessions, or delete seat homes."
)
WORKSPACE_FOLLOW_UPS = (
    "A workspace owner must separately verify membership, paid-seat assignment, "
    "workspace role, and billing in OpenAI administration.",
    "A workspace owner must separately revoke provider access/tokens and perform "
    "local logout when offboarding requires it.",
)
_MUTATIONS = frozenset(
    {"add", "set", "set-role", "activate", "drain", "deactivate", "retire", "remove"}
)


class SeatsCLIError(ValueError):
    """An operator command cannot safely complete."""


def _json_dump(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


def _require_schema3(settings: Mapping[str, Any], root: Path) -> None:
    _current, warnings = load_settings(root)
    invalid = [warning for warning in warnings if warning.startswith("invalid settings")]
    if invalid:
        # Never present the empty fail-closed registry as if it were the pool.
        raise SeatsCLIError(invalid[0])
    try:
        validate_schema3(settings, root)
    except SeatRegistryError as exc:
        raise SeatsCLIError(f"invalid schema-3 seat registry: {exc}") from exc


def _stable_selector(selector: str, settings: Mapping[str, Any]) -> str:
    candidate = f"seat{selector}" if selector.isascii() and selector.isdigit() else selector
    try:
        validate_seat_id(candidate)
    except SeatRegistryError as exc:
        raise SeatsCLIError(
            "mutation seat selector must be a stable seatN id or numeric shorthand"
        ) from exc
    if candidate not in settings["pool"]["seats"]:
        raise SeatsCLIError(f"unknown registered seat {candidate}")
    return candidate


def _safe_ledger(root: Path) -> dict[str, Any]:
    try:
        return _read_ledger_for_preview(root)
    except SeatAdminError as exc:
        raise SeatsCLIError(str(exc)) from exc


def _auth_file_present(home: Path) -> bool:
    """Check only directory metadata; never open or follow an auth file."""
    try:
        info = (home / "auth.json").lstat()
    except OSError:
        return False
    return bool(
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_uid == os.geteuid()
        and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    )


def _claim_summary(raw: Any) -> dict[str, Any]:
    active = isinstance(raw, Mapping) and bool(raw)
    alive = bool(active and claim_alive(dict(raw)))
    return {
        "present": active,
        "live": alive,
        "stale": active and not alive,
        "job_id": raw.get("job_id") if isinstance(raw, Mapping) else None,
        "task_kind": (
            raw.get("task_kind", raw.get("kind"))
            if isinstance(raw, Mapping)
            else None
        ),
    }


def _binding_capacity(entry: Mapping[str, Any], runtime: Mapping[str, Any]) -> dict[str, Any]:
    now = time.time()
    windows = active_capacity_windows(dict(runtime), current=now)
    multiplier = effective_relative_capacity(entry)
    candidates = [
        {
            "name": name,
            "remaining_percent": pct,
            "available_baseline_units": pct * multiplier,
            "resets_at": resets_at,
        }
        for name, pct, resets_at, _minutes in windows
    ]
    binding = (
        min(
            candidates,
            key=lambda item: (item["available_baseline_units"], item["name"]),
        )
        if candidates
        else None
    )
    observed = parse_iso(runtime.get("usage_updated_at") or runtime.get("probed_at"))
    return {
        "authoritative_window_count": len(candidates),
        "observed_at": observed,
        "binding_window": binding,
    }


def _next_action(entry: Mapping[str, Any], runtime: Mapping[str, Any]) -> str:
    lifecycle = entry.get("lifecycle")
    if lifecycle == "active" and runtime.get("status") == "needs-login":
        return "login"
    if lifecycle == "active":
        return "monitor" if runtime.get("in_flight") else "ready"
    if lifecycle == "draining":
        return "wait" if runtime.get("in_flight") else "deactivate"
    if lifecycle == "inactive":
        return "activate-or-retire"
    return "workspace-offboarding-check"


def _seat_record(
    root: Path,
    settings: Mapping[str, Any],
    ledger: Mapping[str, Any],
    seat_id: str,
    *,
    operator: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    entry = settings["pool"]["seats"][seat_id]
    runtime = ledger.get("seats", {}).get(seat_id, {})
    if not isinstance(runtime, Mapping):
        runtime = {}
    roles = derive_roles(settings)
    report = (
        operator.get("seat_capacity", {}).get(seat_id)
        if isinstance(operator, Mapping)
        else None
    )
    if not isinstance(report, Mapping):
        report = seat_capacity_report(
            seat_id,
            dict(runtime),
            dict(settings),
            current=time.time(),
            learning=(
                dict(ledger.get("capacity_learning", {}))
                if isinstance(ledger.get("capacity_learning"), Mapping)
                else {}
            ),
        )
    healthy_ids = operator.get("healthy_seat_ids", []) if operator else []
    return {
        "id": seat_id,
        "banked_resets": banked_reset_report(dict(runtime), dict(settings), root=root, seat_id=seat_id),
        "label": entry["label"],
        "role": roles.get(seat_id),
        "lifecycle": entry["lifecycle"],
        "routable": entry["lifecycle"] == "active" and bool(report["fits_small"]),
        "healthy": seat_id in healthy_ids,
        "home": {
            "configured": entry.get("home") is not None,
            "location": "configured" if entry.get("home") is not None else "pool-root/seat-id",
        },
        "capacity": {
            "profile": entry["capacity_profile"],
            "relative_capacity": entry["relative_capacity"],
            "effective_relative_capacity": effective_relative_capacity(entry),
            "source": entry["capacity_source"],
            "verified_at": entry["capacity_verified_at"],
            "fingerprint": report["capacity_fingerprint"],
            "active_windows": report["capacity_windows"],
            "binding_available_baseline_units": report[
                "binding_available_baseline_units"
            ],
            "binding_usable_baseline_units": report[
                "binding_usable_baseline_units"
            ],
            "unverified": report["unverified"],
            "unverified_reasons": report["unverified_reasons"],
            "zero_authoritative_windows": report["zero_authoritative_windows"],
            "profile_window_drift": report["profile_window_drift"],
            "profile_window_drift_reason": report["profile_window_drift_reason"],
            "observed_plan_type": runtime.get("account_usage_plan_type"),
            "credits": (
                dict(runtime["account_usage_credits"])
                if isinstance(runtime.get("account_usage_credits"), Mapping)
                else None
            ),
        },
        "provider": {
            "health": (
                runtime.get("status")
                if runtime.get("status")
                in {"ok", "needs-login", "capped", "error", "unknown"}
                else "unknown"
            ),
            **_binding_capacity(entry, runtime),
        },
        "claim": _claim_summary(runtime.get("in_flight")),
        "next_action": _next_action(entry, runtime),
    }


def seats_list_report(
    root: Path, settings: Mapping[str, Any], *, include_all: bool = False
) -> dict[str, Any]:
    _require_schema3(settings, root)
    ledger = _safe_ledger(root)
    registered = registered_seat_ids(settings)
    live_codex = [
        seat_id
        for seat_id in registered
        if isinstance(ledger.get("seats", {}).get(seat_id), Mapping)
        and claim_alive(ledger["seats"][seat_id].get("in_flight"))
    ]
    claude = ledger.get("providers", {}).get("claude", {})
    operator = schema3_operator_report(
        settings,
        ledger,
        current=time.time(),
        claude_limits=load_claude_rate_limits(root),
        live_codex_claims=live_codex,
        claude_live_claim=(
            isinstance(claude, Mapping) and claim_alive(claude.get("in_flight"))
        ),
    )
    ids = registered_seat_ids(settings)
    if not include_all:
        ids = [
            seat_id
            for seat_id in ids
            if settings["pool"]["seats"][seat_id]["lifecycle"] != "retired"
        ]
    return {
        "schema": OUTPUT_SCHEMA,
        "command": "list",
        "settings_schema": 3,
        "seats": [
            _seat_record(root, settings, ledger, seat_id, operator=operator)
            for seat_id in ids
        ],
        "pool_summary": {
            key: value for key, value in operator.items() if key != "seat_capacity"
        },
        "notice": LOCAL_ONLY_NOTICE,
    }


def seats_show_report(
    root: Path, settings: Mapping[str, Any], selector: str
) -> dict[str, Any]:
    _require_schema3(settings, root)
    try:
        seat_id = resolve_readonly_selector(selector, settings)
    except SeatRegistryError as exc:
        raise SeatsCLIError(str(exc)) from exc
    return {
        "schema": OUTPUT_SCHEMA,
        "command": "show",
        "settings_schema": 3,
        "seat": _seat_record(root, settings, _safe_ledger(root), seat_id),
        "notice": LOCAL_ONLY_NOTICE,
    }


def _codex_version(result: Any) -> str:
    output = getattr(result, "stdout", b"")
    if isinstance(output, bytes):
        text = output.decode("utf-8", "replace")
    else:
        text = str(output or "")
    rendered = text.strip()
    match = re.fullmatch(
        r"(?:codex-cli\s+)?([0-9]+\.[0-9]+\.[0-9]+)", rendered
    )
    return rendered if match else "unknown"


def _codex_login_support(*, exact_version: bool) -> tuple[str, str]:
    try:
        codex = resolve_executable("codex")
        version_result = bounded_process.run([codex, "--version"], timeout=10)
    except (FileNotFoundError, bounded_process.ProcessError) as exc:
        raise SeatsCLIError(f"Codex CLI unavailable: {exc}") from exc
    version = _codex_version(version_result)
    if version_result.returncode != 0:
        raise SeatsCLIError("Codex CLI version check failed")
    supported = {
        SUPPORTED_CODEX_VERSION,
        f"codex-cli {SUPPORTED_CODEX_VERSION}",
    }
    if exact_version and version not in supported:
        raise SeatsCLIError(
            f"seat activation requires Codex CLI {SUPPORTED_CODEX_VERSION}; installed: "
            f"{version or 'unknown'}"
        )
    try:
        help_result = bounded_process.run([codex, "login", "--help"], timeout=10)
    except bounded_process.ProcessError as exc:
        raise SeatsCLIError(f"cannot verify Codex login command support: {exc}") from exc
    if help_result.returncode != 0:
        raise SeatsCLIError("installed Codex CLI does not support `codex login`")
    return codex, version


def _login_status(codex: str, home: Path) -> bool:
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(home)
    try:
        result = bounded_process.run(
            [codex, "login", "status"], timeout=30, env=environment
        )
    except bounded_process.ProcessError:
        return False
    # The documented contract is the exit status. Output may identify an account
    # or auth method, so it is deliberately discarded.
    return result.returncode == 0


def _fresh_activation_probe(
    root: Path, settings: Mapping[str, Any], seat_id: str
) -> bool:
    started = time.time()
    messages = refresh_account_usage(root, dict(settings), [seat_id])
    if not messages or any(
        "unavailable" in message or "needs-login" in message for message in messages
    ):
        return False
    runtime = _safe_ledger(root).get("seats", {}).get(seat_id, {})
    if not isinstance(runtime, Mapping):
        return False
    observation = runtime.get("account_window_observation")
    success = (
        observation.get("last_success_observed_at")
        if isinstance(observation, Mapping)
        else None
    )
    active = runtime.get("active_window_observation")
    active_at = active.get("observed_at") if isinstance(active, Mapping) else None
    fresh_at = success if isinstance(success, (int, float)) else active_at
    windows = active_capacity_windows(dict(runtime), current=time.time())
    return isinstance(fresh_at, (int, float)) and float(fresh_at) >= started and bool(windows)


def _directory_facts(home: Path) -> dict[str, Any]:
    try:
        info = home.lstat()
    except OSError:
        return {
            "exists": False,
            "directory": False,
            "symlink": False,
            "owner_ok": False,
            "mode_ok": False,
            "mode": None,
        }
    return {
        "exists": True,
        "directory": stat.S_ISDIR(info.st_mode),
        "symlink": stat.S_ISLNK(info.st_mode),
        "owner_ok": info.st_uid == os.geteuid(),
        "mode_ok": not bool(info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)),
        "mode": f"{stat.S_IMODE(info.st_mode):04o}",
    }


def _issue(
    code: str,
    message: str,
    *,
    severity: str = "warning",
    seat_id: str | None = None,
) -> dict[str, Any]:
    return {"code": code, "severity": severity, "seat_id": seat_id, "message": message}


def _observed_profile(runtime: Mapping[str, Any]) -> str | None:
    for key in ("observed_capacity_profile", "observed_plan", "plan_type", "plan"):
        value = runtime.get(key)
        if isinstance(value, str) and value:
            normalized = value.lower().replace("_", "-")
            if normalized.replace("-", "").isalnum() and len(normalized) <= 64:
                return normalized
    return None


def _fingerprint_issues(
    learning: Any, seat_id: str, entry: Mapping[str, Any]
) -> list[dict[str, Any]]:
    if not isinstance(learning, Mapping):
        return []
    current = capacity_fingerprint(entry)
    issues: list[dict[str, Any]] = []
    for key, value in learning.items():
        if not isinstance(value, Mapping) or value.get("seat_id") != seat_id:
            continue
        fingerprint = value.get("capacity_fingerprint")
        if isinstance(fingerprint, str) and fingerprint != current:
            kind = "risk floor" if value.get("risk_floor_pct") else "learning bucket"
            issues.append(
                _issue(
                    "inert-capacity-fingerprint",
                    f"{kind} {key!r} is inert after the capacity fingerprint changed",
                    seat_id=seat_id,
                )
            )
    return issues


def seats_doctor_report(
    root: Path,
    settings: Mapping[str, Any],
    *,
    selector: str | None = None,
    include_all: bool = False,
) -> dict[str, Any]:
    _require_schema3(settings, root)
    ledger = _safe_ledger(root)
    if selector is not None:
        try:
            ids = [resolve_readonly_selector(selector, settings)]
        except SeatRegistryError as exc:
            raise SeatsCLIError(str(exc)) from exc
    else:
        ids = registered_seat_ids(settings)
        if not include_all:
            ids = [
                seat_id
                for seat_id in ids
                if settings["pool"]["seats"][seat_id]["lifecycle"] != "retired"
            ]

    issues: list[dict[str, Any]] = []
    codex: str | None = None
    cli_version: str | None = None
    try:
        codex, cli_version = _codex_login_support(exact_version=False)
    except SeatsCLIError as exc:
        issues.append(_issue("codex-cli", str(exc), severity="error"))
    supported_versions = {
        SUPPORTED_CODEX_VERSION,
        f"codex-cli {SUPPORTED_CODEX_VERSION}",
    }
    if codex and cli_version not in supported_versions:
        issues.append(
            _issue(
                "codex-cli-version",
                f"installed Codex CLI is not the verified {SUPPORTED_CODEX_VERSION} build",
            )
        )

    seat_reports: list[dict[str, Any]] = []
    # Capacity is global even when the diagnostic rows are selector-filtered.
    # Use metadata-only auth facts for every configured identity; provider
    # status/claims in the ledger remain the authoritative health signal.
    authenticated_ids = {
        seat_id
        for seat_id in registered_seat_ids(settings)
        if has_auth(root, seat_id, dict(settings))
    }
    for seat_id in ids:
        entry = settings["pool"]["seats"][seat_id]
        runtime = ledger.get("seats", {}).get(seat_id, {})
        if not isinstance(runtime, Mapping):
            runtime = {}
        home = resolve_home(root, seat_id, entry)
        directory = _directory_facts(home)
        valid_home = all(
            directory[key] for key in ("exists", "directory", "owner_ok", "mode_ok")
        ) and not directory["symlink"]
        if not valid_home:
            issues.append(
                _issue(
                    "unsafe-home",
                    "home must be an owner-controlled, non-symlink directory "
                    "without group/world write access",
                    severity="error" if entry["lifecycle"] == "active" else "warning",
                    seat_id=seat_id,
                )
            )
        auth_file = _auth_file_present(home) if valid_home else False
        authenticated = _login_status(codex, home) if codex and valid_home else False
        if entry["lifecycle"] == "active" and not auth_file:
            issues.append(
                _issue(
                    "active-missing-auth",
                    "active seat has no regular auth file",
                    severity="error",
                    seat_id=seat_id,
                )
            )
        if entry["lifecycle"] in {"inactive", "retired"} and (
            auth_file or authenticated
        ):
            issues.append(
                _issue(
                    "retained-auth",
                    "inactive/retired seat retains local authentication; local "
                    "lifecycle does not revoke it",
                    seat_id=seat_id,
                )
            )
        if entry["lifecycle"] in {"active", "draining"} and not authenticated:
            issues.append(
                _issue(
                    "login-status",
                    "`codex login status` did not confirm authentication",
                    severity="error",
                    seat_id=seat_id,
                )
            )
        binding = _binding_capacity(entry, runtime)
        observed_at = binding["observed_at"]
        stale_seconds = float(settings["pool"].get("probe_stale_seconds", 6 * 3600))
        fresh = observed_at is not None and time.time() - observed_at <= stale_seconds
        if entry["lifecycle"] in {"active", "draining"} and (
            not fresh or binding["authoritative_window_count"] == 0
        ):
            issues.append(
                _issue(
                    "probe-freshness",
                    "active/draining seat needs a fresh successful capacity probe with an authoritative window",
                    severity="error",
                    seat_id=seat_id,
                )
            )
        claim = _claim_summary(runtime.get("in_flight"))
        if claim["stale"]:
            issues.append(
                _issue(
                    "stale-claim",
                    "stale worker claim should be cleared by the locked ledger refresh path",
                    seat_id=seat_id,
                )
            )
        observed_profile = _observed_profile(runtime)
        if observed_profile and observed_profile != entry["capacity_profile"]:
            issues.append(
                _issue(
                    "configured-observed-tier-drift",
                    f"configured profile {entry['capacity_profile']!r} differs from "
                    f"observed provider profile {observed_profile!r}",
                    seat_id=seat_id,
                )
            )
        issues.extend(_fingerprint_issues(ledger.get("capacity_learning"), seat_id, entry))
        capacity_record = _seat_record(root, settings, ledger, seat_id)["capacity"]
        if entry["lifecycle"] in {"active", "draining"} and capacity_record[
            "unverified"
        ]:
            issues.append(
                _issue(
                    "unverified-capacity",
                    "; ".join(capacity_record["unverified_reasons"]),
                    seat_id=seat_id,
                )
            )
        if capacity_record["profile_window_drift"]:
            issues.append(
                _issue(
                    "profile-window-drift",
                    capacity_record["profile_window_drift_reason"],
                    seat_id=seat_id,
                )
            )
        seat_reports.append(
            {
                "id": seat_id,
                "lifecycle": entry["lifecycle"],
                "directory": directory,
                "auth_file_present": auth_file,
                "login_status_authenticated": authenticated,
                "probe_fresh": fresh,
                "authoritative_window_count": binding["authoritative_window_count"],
                "claim": claim,
                "configured_profile": entry["capacity_profile"],
                "observed_profile": observed_profile,
                "capacity": capacity_record,
            }
        )

    registered = set(registered_seat_ids(settings))
    try:
        children = list(root.iterdir())
    except OSError:
        children = []
    orphans: list[str] = []
    for child in children:
        try:
            validate_seat_id(child.name)
        except SeatRegistryError:
            continue
        if child.name not in registered:
            orphans.append(child.name)
    orphans = sort_seat_ids(orphans)
    for seat_id in orphans:
        issues.append(
            _issue(
                "orphan-seat-home",
                "strict seatN entry exists under the pool root but is not "
                "registered; it will not be adopted automatically",
                seat_id=seat_id,
            )
        )

    live_codex = {
        seat_id
        for seat_id in registered
        if isinstance(ledger.get("seats", {}).get(seat_id), Mapping)
        and claim_alive(ledger["seats"][seat_id].get("in_flight"))
    }
    claude = ledger.get("providers", {}).get("claude", {})
    active_origins = [
        availability.HeadOrigin("codex", seat_id)
        for seat_id in sort_seat_ids(
            seat_id
            for seat_id in registered
            if settings["pool"]["seats"][seat_id]["lifecycle"] == "active"
        )
    ]
    active_origins.append(availability.HeadOrigin("claude"))
    head_capacity = availability.schema3_concurrency_report(
        settings,
        ledger,
        active_origins,
        current=time.time(),
        codex_live_claims=live_codex,
        claude_live_claim=(
            isinstance(claude, Mapping) and claim_alive(claude.get("in_flight"))
        ),
        authenticated_codex=authenticated_ids,
        claude_limits=load_claude_rate_limits(root),
    )
    primary = settings["pool"].get("primary")
    primary_capacity = next(
        (
            origin
            for origin in head_capacity["origins"]
            if origin["head"] == "codex" and origin["seat"] == primary
        ),
        None,
    )
    if primary_capacity is not None and primary_capacity["codex_worker_slots"] == 0:
        issues.append(
            _issue(
                "codex-head-zero-workers",
                "the configured Codex head leaves zero currently usable Codex workers",
            )
        )
    claude_capacity = next(
        (origin for origin in head_capacity["origins"] if origin["head"] == "claude"),
        None,
    )
    if claude_capacity is not None and claude_capacity["pool_ceiling"] == 0:
        issues.append(
            _issue(
                "claude-head-zero-workers",
                "the Claude head leaves zero currently usable worker identities",
            )
        )
    # Preserve the original additive doctor keys, but derive them from the
    # shared health/capacity calculation instead of lifecycle counts.
    head_capacity["codex_head_worker_slots"] = (
        primary_capacity["codex_worker_slots"] if primary_capacity is not None else 0
    )
    head_capacity["claude_head_worker_slots"] = (
        claude_capacity["codex_worker_slots"] if claude_capacity is not None else 0
    )
    head_capacity["codex_head_ceiling"] = head_capacity[
        "default_primary_codex_head_ceiling"
    ]

    empirical_ratios = empirical_burn_ratio_report(
        dict(settings),
        dict(ledger.get("capacity_learning", {}))
        if isinstance(ledger.get("capacity_learning"), Mapping)
        else {},
    )
    for ratio in empirical_ratios:
        if ratio["material_disagreement"]:
            issues.append(
                _issue(
                    "empirical-capacity-ratio-drift",
                    "mature matching native-burn samples materially disagree with "
                    "the configured Standard-to-Premium capacity ratio; review only, "
                    "settings were not changed",
                )
            )

    operator = schema3_operator_report(
        settings,
        ledger,
        current=time.time(),
        claude_limits=load_claude_rate_limits(root),
        live_codex_claims=live_codex,
        claude_live_claim=(
            isinstance(claude, Mapping) and claim_alive(claude.get("in_flight"))
        ),
    )
    return {
        "schema": OUTPUT_SCHEMA,
        "command": "doctor",
        "settings_schema": 3,
        "ok": not any(item["severity"] == "error" for item in issues),
        "codex_cli_version": cli_version,
        "codex_cli_contract": {
            "version": SUPPORTED_CODEX_VERSION,
            "login_argv": ["codex", "login"],
            "status_argv": ["codex", "login", "status"],
            "official_documentation": LOGIN_DOCUMENTATION,
        },
        "seats": seat_reports,
        "orphans": orphans,
        "head_capacity": head_capacity,
        "pool_summary": {
            key: value for key, value in operator.items() if key != "seat_capacity"
        },
        "empirical_burn_ratios": empirical_ratios,
        "issues": issues,
        "workspace_owner_follow_ups": list(WORKSPACE_FOLLOW_UPS),
        "notice": LOCAL_ONLY_NOTICE,
    }


def _registry_snapshot(text: str) -> dict[str, Any]:
    document = json.loads(text)
    pool = document["pool"]
    return {
        "primary": pool.get("primary"),
        "deputy": pool.get("deputy"),
        "seats": pool.get("seats", {}),
        "reserves_pct": pool.get("reserves_pct", {}),
        "small_task_min_headroom_pct": pool.get("small_task_min_headroom_pct"),
        "big_task_min_headroom_pct": pool.get("big_task_min_headroom_pct"),
        "task_min_headroom_pct_by_seat": pool.get(
            "task_min_headroom_pct_by_seat", {}
        ),
    }


def _capacity_policy_snapshot(document: Mapping[str, Any], seat_id: str) -> dict[str, Any] | None:
    pool = document.get("pool", {})
    seats = pool.get("seats", {}) if isinstance(pool, Mapping) else {}
    entry = seats.get(seat_id) if isinstance(seats, Mapping) else None
    if not isinstance(entry, Mapping):
        return None
    multiplier = effective_relative_capacity(entry)
    reserves = pool.get("reserves_pct", {})
    reserve = reserves.get(seat_id, 0) if isinstance(reserves, Mapping) else 0
    overrides = pool.get("task_min_headroom_pct_by_seat", {})
    override = overrides.get(seat_id, {}) if isinstance(overrides, Mapping) else {}
    floors: dict[str, dict[str, float]] = {}
    for size, default in (("small", 10), ("big", 30)):
        global_key = f"{size}_task_min_headroom_pct"
        native = (
            override.get(size, pool.get(global_key, default))
            if isinstance(override, Mapping)
            else pool.get(global_key, default)
        )
        native_value = float(native)
        floors[size] = {
            "native_percent": native_value,
            "baseline_units": native_value * multiplier,
        }
    reserve_value = float(reserve)
    return {
        "capacity_profile": entry.get("capacity_profile"),
        "relative_capacity": entry.get("relative_capacity"),
        "effective_relative_capacity": multiplier,
        "reserve": {
            "native_percent": reserve_value,
            "baseline_units": reserve_value * multiplier,
        },
        "task_floors": floors,
    }


def mutation_report(
    result: SeatMutationResult,
    *,
    planned: bool,
    requested_action: str | None = None,
) -> dict[str, Any]:
    before_document = json.loads(result.before)
    after_document = json.loads(result.after)
    before = _registry_snapshot(result.before)
    after = _registry_snapshot(result.after)
    before_text = _json_dump(before).splitlines(keepends=True)
    after_text = _json_dump(after).splitlines(keepends=True)
    diff = "".join(
        difflib.unified_diff(
            before_text,
            after_text,
            fromfile="settings.before",
            tofile="settings.after",
            lineterm="\n",
        )
    )
    action = requested_action or result.action
    capacity_before = _capacity_policy_snapshot(before_document, result.seat_id)
    capacity_after = _capacity_policy_snapshot(after_document, result.seat_id)
    capacity_changed = capacity_before != capacity_after
    deferred_checks = []
    if planned and action in {"activate", "login"}:
        deferred_checks = ["codex-login-status", "authoritative-capacity-probe"]
    elif planned and capacity_changed:
        deferred_checks = ["post-apply-authoritative-capacity-probe"]
    warnings = list(result.warnings)
    if planned and action in {"activate", "login"}:
        warnings.append(
            "planning performs no provider checks; activation apply requires login-status "
            "and an authoritative capacity probe"
        )
    elif planned and capacity_changed:
        warnings.append(
            "planning performs no provider checks; after apply, refresh authoritative "
            "capacity before relying on the changed profile or multiplier"
        )
    return {
        "schema": OUTPUT_SCHEMA,
        "command": "plan" if planned else action,
        "action": action,
        "canonical_action": result.action,
        "seat_id": result.seat_id,
        "planned": planned,
        "changed": result.changed,
        "registry": {"before": before, "after": after, "diff": diff},
        "capacity_policy_impact": {
            "changed": capacity_changed,
            "before": capacity_before,
            "after": capacity_after,
        },
        "provider_checks": {
            "performed": False if planned else None,
            "deferred": deferred_checks,
        },
        "blockers": [],
        "warnings": warnings,
        "notice": LOCAL_ONLY_NOTICE,
    }


def _render_seat(record: Mapping[str, Any]) -> str:
    capacity = record["capacity"]
    binding = record["provider"]["binding_window"]
    binding_text = (
        "no-window"
        if binding is None
        else (
            f"{binding['name']}={binding['remaining_percent']:g}% "
            f"units={capacity['binding_available_baseline_units']:g}/"
            f"{capacity['binding_usable_baseline_units']:g}"
        )
    )
    claim = (
        "live"
        if record["claim"]["live"]
        else ("stale" if record["claim"]["stale"] else "idle")
    )
    role = record["role"] or "-"
    flags = []
    if capacity["unverified"]:
        flags.append("unverified")
    if capacity["profile_window_drift"]:
        flags.append("window-drift")
    flag_text = f" flags={','.join(flags)}" if flags else ""
    return (
        f"{record['id']} {record['label']} role={role} "
        f"lifecycle={record['lifecycle']} profile={capacity['profile']} "
        f"capacity={capacity['effective_relative_capacity']:g}x source={capacity['source']} "
        f"health={record['provider']['health']} binding={binding_text}{flag_text} claim={claim} "
        f"next={record['next_action']}"
    )


def _print_report(report: Mapping[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(_json_dump(report))
        return
    command = report["command"]
    if command == "list":
        summary = report["pool_summary"]
        lifecycle = summary["lifecycle"]
        print(
            f"POOL {summary['healthy']} healthy / {summary['active']} active; "
            f"lifecycle A{lifecycle['active']}/D{lifecycle['draining']}/"
            f"I{lifecycle['inactive']}/R{lifecycle['retired']}; "
            f"ceilings Codex={summary['current_codex_head_ceiling']} "
            f"Claude={summary['claude_head_ceiling']} "
            f"safety={summary['global_safety_ceiling']}"
        )
        for seat in report["seats"]:
            print(_render_seat(seat))
    elif command == "show":
        print(_render_seat(report["seat"]))
    elif command == "doctor":
        summary = report["pool_summary"]
        lifecycle = summary["lifecycle"]
        print(
            f"POOL {summary['healthy']} healthy / {summary['active']} active; "
            f"lifecycle A{lifecycle['active']}/D{lifecycle['draining']}/"
            f"I{lifecycle['inactive']}/R{lifecycle['retired']}"
        )
        for seat in report["seats"]:
            print(
                f"{seat['id']}: login={'ok' if seat['login_status_authenticated'] else 'not-confirmed'} "
                f"probe={'fresh' if seat['probe_fresh'] else 'stale/missing'} "
                f"windows={seat['authoritative_window_count']} "
                f"profile={seat['capacity']['profile']} "
                f"capacity={seat['capacity']['effective_relative_capacity']:g}x "
                f"source={seat['capacity']['source']}"
            )
        capacity = report["head_capacity"]
        print(
            "HEAD CAPACITY: "
            f"safety={capacity['safety_ceiling']} "
            f"current={capacity['current_pool_ceiling']} "
            f"default-codex={capacity['default_primary_codex_head_ceiling']} "
            f"claude={capacity['claude_head_ceiling']} "
            f"conservative-mixed={capacity['conservative_mixed_origin_ceiling']}"
        )
        for issue in report["issues"]:
            target = f" {issue['seat_id']}" if issue["seat_id"] else ""
            print(f"{issue['severity'].upper()}{target}: {issue['message']}")
        for follow_up in report["workspace_owner_follow_ups"]:
            print(f"FOLLOW-UP: {follow_up}")
    else:
        registry = report["registry"]
        print(
            f"{'PLAN' if report['planned'] else 'APPLIED'} {report['action']} "
            f"{report['seat_id']} changed={'yes' if report['changed'] else 'no'}"
        )
        print(registry["diff"] or "(no registry changes)")
        impact = report.get("capacity_policy_impact")
        if isinstance(impact, Mapping) and impact.get("changed"):
            before = impact.get("before")
            after = impact.get("after")
            if isinstance(before, Mapping) and isinstance(after, Mapping):
                print(
                    "CAPACITY POLICY: "
                    f"{before.get('capacity_profile')} "
                    f"{before.get('effective_relative_capacity', 1):g}x -> "
                    f"{after.get('capacity_profile')} "
                    f"{after.get('effective_relative_capacity', 1):g}x"
                )
                for label, snapshot in (("before", before), ("after", after)):
                    reserve = snapshot.get("reserve", {})
                    floors = snapshot.get("task_floors", {})
                    small = floors.get("small", {}) if isinstance(floors, Mapping) else {}
                    big = floors.get("big", {}) if isinstance(floors, Mapping) else {}
                    print(
                        f"  {label}: reserve {reserve.get('native_percent', 0):g}%="
                        f"{reserve.get('baseline_units', 0):g} baseline units; "
                        f"floors small {small.get('native_percent', 0):g}%="
                        f"{small.get('baseline_units', 0):g}, "
                        f"big {big.get('native_percent', 0):g}%="
                        f"{big.get('baseline_units', 0):g}"
                    )
        checks = report.get("provider_checks")
        if isinstance(checks, Mapping) and checks.get("deferred"):
            print(
                "PROVIDER CHECKS: not run during planning; required: "
                + ", ".join(str(item) for item in checks["deferred"])
            )
        for warning in report["warnings"]:
            print(f"WARNING: {warning}")
    print(report["notice"])


def _mutation_guard() -> None:
    context = current_controller()
    if context.managed_worker:
        raise SeatsCLIError("managed workers cannot mutate the seat registry or run seat login")
    declared_head = os.environ.get("CODEX_SEATS_HEAD", "").lower()
    if context.is_controller or declared_head in {"codex", "claude"}:
        raise SeatsCLIError(
            "controller/head contexts cannot mutate the seat registry or run seat login"
        )


def _trusted_terminal(args: argparse.Namespace, action: str) -> None:
    if not getattr(args, "from_current_user_request", False):
        raise SeatsCLIError(f"{action} requires --from-current-user-request")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SeatsCLIError(f"{action} requires an interactive trusted human terminal")


def _core_options(args: argparse.Namespace) -> dict[str, Any]:
    action = args.seats_action
    if action == "add":
        return {"label": args.label, "home": args.home, "profile": args.profile}
    if action == "set":
        options: dict[str, Any] = {}
        for cli_name, core_name in (
            ("label", "label"),
            ("home", "home"),
            ("profile", "profile"),
            ("relative_capacity", "relative_capacity"),
            ("capacity_source", "capacity_source"),
            ("reserve_pct", "reserve_pct"),
            ("small_task_min_headroom_pct", "small_task_min_headroom_pct"),
            ("big_task_min_headroom_pct", "big_task_min_headroom_pct"),
        ):
            value = getattr(args, cli_name, None)
            if value is not None:
                options[core_name] = value
        return options
    if action == "set-role":
        return {"role": args.role}
    if action == "activate":
        return {"as_primary": args.as_primary}
    if action == "drain":
        return {"wait": args.wait, "allow_empty": args.allow_empty}
    if action == "deactivate":
        return {"allow_empty": args.allow_empty}
    return {}


def _plan_core(
    root: Path,
    settings: Mapping[str, Any],
    action: str,
    selector: str | None,
    options: dict[str, Any],
) -> SeatMutationResult:
    normalized = "retire" if action == "remove" else action
    if normalized == "activate":
        options = {**options, "verified_login": True, "successful_probe": True}
    if normalized == "login":
        seat_id = _stable_selector(str(selector), settings)
        text = json.dumps(dict(settings), indent=2) + "\n"
        return SeatMutationResult(
            action="login",
            seat_id=seat_id,
            changed=False,
            before=text,
            after=text,
            warnings=("login would run interactively with the resolved CODEX_HOME",),
        )
    return plan_mutation(root, normalized, selector, **options)


def _blocked_plan_report(
    settings: Mapping[str, Any], action: str, selector: str | None, blocker: str
) -> dict[str, Any]:
    pool = settings["pool"]
    registry = {
        "primary": pool.get("primary"),
        "deputy": pool.get("deputy"),
        "seats": pool.get("seats", {}),
    }
    return {
        "schema": OUTPUT_SCHEMA,
        "command": "plan",
        "action": action,
        "seat_id": selector,
        "planned": True,
        "changed": False,
        "registry": {"before": registry, "after": registry, "diff": ""},
        "blockers": [blocker],
        "warnings": [],
        "notice": LOCAL_ONLY_NOTICE,
    }


def _apply_core(
    root: Path,
    settings: Mapping[str, Any],
    action: str,
    selector: str | None,
    options: dict[str, Any],
) -> SeatMutationResult:
    if action == "add":
        return add_seat(root, **options)
    assert selector is not None
    _stable_selector(selector, settings)
    if action == "set":
        return set_seat(root, selector, **options)
    if action == "set-role":
        return set_role(root, selector, options["role"])
    if action == "activate":
        codex, _version = _codex_login_support(exact_version=True)
        current_settings, _warnings = load_settings(root)
        _require_schema3(current_settings, root)
        seat_id = _stable_selector(selector, current_settings)
        home = resolve_home(
            root, seat_id, current_settings["pool"]["seats"][seat_id]
        )
        facts = _directory_facts(home)
        if not (
            facts["exists"]
            and facts["directory"]
            and not facts["symlink"]
            and facts["owner_ok"]
            and facts["mode_ok"]
        ):
            raise SeatsCLIError(
                f"activation home for {seat_id} must be an owner-controlled, "
                "non-symlink directory"
            )
        verified_login = _login_status(codex, home)
        if not verified_login:
            raise SeatsCLIError(
                f"activation refused: `codex login status` did not authenticate {seat_id}"
            )
        successful_probe = _fresh_activation_probe(root, current_settings, seat_id)
        if not successful_probe:
            raise SeatsCLIError(
                "activation refused: fresh account/capacity probe did not produce an authoritative window"
            )
        return activate_seat(
            root,
            selector,
            verified_login=True,
            successful_probe=True,
            verified_home=home,
            as_primary=options["as_primary"],
        )
    if action == "drain":
        return drain_seat(root, selector, **options)
    if action == "deactivate":
        return deactivate_seat(root, selector, **options)
    if action == "retire":
        return retire_seat(root, selector)
    if action == "remove":
        return remove_seat(root, selector)
    raise SeatsCLIError(f"unknown seat action {action!r}")


def _run_login(root: Path, settings: Mapping[str, Any], selector: str) -> int:
    seat_id = _stable_selector(selector, settings)
    # Interactive login needs command support, not the activation probe's version pin.
    codex, _version = _codex_login_support(exact_version=False)
    home = resolve_home(root, seat_id, settings["pool"]["seats"][seat_id])
    facts = _directory_facts(home)
    if not (
        facts["exists"]
        and facts["directory"]
        and not facts["symlink"]
        and facts["owner_ok"]
        and facts["mode_ok"]
    ):
        raise SeatsCLIError(
            f"login home for {seat_id} must be an owner-controlled, non-symlink directory"
        )
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(home)
    try:
        result = bounded_process.run(
            [codex, "login"],
            timeout=900,
            env=environment,
            capture_output=False,
        )
    except bounded_process.ProcessError as exc:
        raise SeatsCLIError(f"interactive Codex login failed: {exc}") from exc
    if result.returncode:
        raise SeatsCLIError(f"interactive Codex login exited {result.returncode}")
    print(f"{seat_id}: Codex login completed with its resolved CODEX_HOME")
    print(LOCAL_ONLY_NOTICE)
    return 0


def cmd_seats(args: argparse.Namespace, settings: dict[str, Any], root: Path) -> int:
    try:
        _require_schema3(settings, root)
        action = args.seats_action
        if action == "list":
            report = seats_list_report(root, settings, include_all=args.all)
            _print_report(report, as_json=args.json)
            return 0
        if action == "show":
            report = seats_show_report(root, settings, args.selector)
            _print_report(report, as_json=args.json)
            return 0
        if action == "doctor":
            report = seats_doctor_report(
                root,
                settings,
                selector=args.selector,
                include_all=args.all,
            )
            _print_report(report, as_json=args.json)
            return 0 if report["ok"] else 1

        planned = action == "plan" or bool(getattr(args, "dry_run", False))
        source = args.plan_args if action == "plan" else args
        mutation = source.seats_action
        selector = getattr(source, "selector", None)
        options = _core_options(source)
        if planned:
            try:
                result = _plan_core(root, settings, mutation, selector, options)
            except (SeatAdminError, SeatRegistryError, SeatsCLIError, ValueError) as exc:
                report = _blocked_plan_report(settings, mutation, selector, str(exc))
                _print_report(report, as_json=args.json)
                return 1
            _print_report(
                mutation_report(
                    result,
                    planned=True,
                    requested_action=mutation,
                ),
                as_json=args.json,
            )
            return 0

        _mutation_guard()
        if mutation in {"retire", "remove", "login"}:
            _trusted_terminal(source, mutation)
        if mutation == "login":
            return _run_login(root, settings, str(selector))
        result = _apply_core(root, settings, mutation, selector, options)
        _print_report(
            mutation_report(
                result,
                planned=False,
                requested_action=mutation,
            ),
            as_json=args.json,
        )
        return 0
    except (SeatsCLIError, SeatAdminError, SeatRegistryError, OSError, ValueError) as exc:
        payload = {
            "schema": OUTPUT_SCHEMA,
            "command": "seats",
            "ok": False,
            "error": str(exc),
            "notice": LOCAL_ONLY_NOTICE,
        }
        if getattr(args, "json", False):
            print(_json_dump(payload))
        else:
            print(f"codex-seat seats refused: {exc}", file=sys.stderr)
            print(LOCAL_ONLY_NOTICE, file=sys.stderr)
        return 2


def _add_common_mutation_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="render the exact plan without writing",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
    )


def _register_mutation(
    sub: argparse._SubParsersAction[argparse.ArgumentParser], name: str
) -> argparse.ArgumentParser:
    parser = sub.add_parser(
        name,
        help=(
            "preview/apply a local registry mutation; use --dry-run first"
            if name != "login"
            else "run local Codex login from a trusted current-request terminal"
        ),
    )
    if name == "add":
        parser.add_argument("--label", required=True)
        parser.add_argument("--home")
        parser.add_argument("--profile")
    else:
        parser.add_argument("selector")
    if name == "set":
        parser.add_argument("--label")
        parser.add_argument("--home")
        parser.add_argument("--profile")
        parser.add_argument("--relative-capacity", type=float)
        parser.add_argument("--capacity-source", choices=sorted(CAPACITY_SOURCES))
        parser.add_argument("--reserve-pct", type=float)
        parser.add_argument("--small-task-min-headroom-pct", type=float)
        parser.add_argument("--big-task-min-headroom-pct", type=float)
    elif name == "set-role":
        parser.add_argument("role", choices=("primary", "deputy", "worker"))
    elif name == "activate":
        parser.add_argument("--as-primary", action="store_true")
    elif name == "drain":
        parser.add_argument("--wait", action="store_true")
        parser.add_argument("--allow-empty", action="store_true")
    elif name == "deactivate":
        parser.add_argument("--allow-empty", action="store_true")
    if name in {"retire", "remove", "login"}:
        parser.add_argument("--from-current-user-request", action="store_true")
    _add_common_mutation_flags(parser)
    return parser


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    seats = sub.add_parser(
        "seats",
        help="inspect and preview-first manage the local schema-3 seat registry",
        description=(
            "Inspect or manage local seatN identities. These commands do not change "
            "OpenAI workspace billing/membership or revoke credentials. Preview "
            "mutations with `seats plan ACTION ...` or ACTION `--dry-run`; trusted "
            "terminal/current-request guards still apply to protected mutations."
        ),
    )
    seats.set_defaults(json=False)
    seats_sub = seats.add_subparsers(dest="seats_action", required=True)

    list_parser = seats_sub.add_parser("list", help="list non-retired local identities")
    list_parser.add_argument("--all", action="store_true")
    list_parser.add_argument("--json", action="store_true")

    show = seats_sub.add_parser("show", help="show one local identity")
    show.add_argument("selector")
    show.add_argument("--json", action="store_true")

    doctor = seats_sub.add_parser(
        "doctor", help="diagnose registry, capacity, homes, and owner follow-ups"
    )
    doctor.add_argument("selector", nargs="?")
    doctor.add_argument("--all", action="store_true")
    doctor.add_argument("--json", action="store_true")

    for name in sorted(_MUTATIONS | {"login"}):
        _register_mutation(seats_sub, name)

    plan = seats_sub.add_parser("plan", help="render an exact mutation without writing")
    plan.add_argument("--json", action="store_true")
    plan_sub = plan.add_subparsers(dest="plan_action", required=True)
    for name in sorted(_MUTATIONS | {"login"}):
        target = _register_mutation(plan_sub, name)
        target.set_defaults(dry_run=True)

    # Normal commands route directly. Plan wraps its nested namespace after
    # argparse; keeping this normalization here avoids duplicating execution.
    seats.set_defaults(handler="seats")


def normalize_plan_args(args: argparse.Namespace) -> None:
    if (
        getattr(args, "handler", None) != "seats"
        or getattr(args, "seats_action", None) != "plan"
    ):
        return
    nested = argparse.Namespace(**vars(args))
    nested.seats_action = args.plan_action
    nested.json = getattr(args, "json", False)
    args.plan_args = nested
