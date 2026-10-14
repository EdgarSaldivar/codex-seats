"""Controller-selected worker access mode for routed jobs.

Only two meaningful worker access choices exist: ordinary ``restricted`` mode
and controller-selected ``trusted-development`` mode. Selection is controller
configuration only; a routed request can narrow access (read-only, strict
scope, parallel-safe) but can never opt into trusted access.

The retired ``development-v1`` candidate filesystem policy and its native canary
machinery are gone. Existing validated settings may still carry an inert
``execution.worker_filesystem_policy`` object (for example the live install's
``routes.claude.enabled = false`` with no canary); such settings are accepted
without granting any candidate access. A leftover ``routes.*.canary`` grant is
refused explicitly here as retired rather than treated as an ordinary job.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


SETTINGS_KEY = "worker_filesystem_policy"
WORKER_ACCESS_KEY = "worker_access"
WORKER_ACCESS_ENV_KEY = "CODEX_SEATS_WORKER_ACCESS"
RESTRICTED_ACCESS = "restricted"
TRUSTED_DEVELOPMENT_ACCESS = "trusted-development"
WORKER_ACCESS_MODES = frozenset({RESTRICTED_ACCESS, TRUSTED_DEVELOPMENT_ACCESS})


class DevelopmentPolicyError(RuntimeError):
    """The configured worker access setting is invalid or retired."""


@dataclass(frozen=True)
class WorkerAccessDecision:
    """Controller-resolved access mode frozen for one routed job."""

    configured: str
    effective: str
    reason: str

    @property
    def trusted(self) -> bool:
        return self.effective == TRUSTED_DEVELOPMENT_ACCESS


def _has_retired_canary(execution: Mapping[str, Any]) -> bool:
    """True when inert settings still carry a retired filesystem-policy canary."""
    policy = execution.get(SETTINGS_KEY)
    if not isinstance(policy, Mapping):
        return False
    routes = policy.get("routes")
    if not isinstance(routes, Mapping):
        return False
    return any(
        isinstance(route, Mapping) and route.get("canary") is not None
        for route in routes.values()
    )


def resolve_worker_access(
    settings: Any,
    request: Mapping[str, Any] | None = None,
) -> WorkerAccessDecision:
    """Resolve access solely from a controller settings snapshot and strict flags.

    The request can only narrow access.  It cannot opt into trusted access. A
    present (retired) filesystem-policy canary grant refuses execution outright,
    regardless of the configured access mode.
    """
    execution = settings.get("execution", {}) if isinstance(settings, Mapping) else {}
    if not isinstance(execution, Mapping):
        raise DevelopmentPolicyError("execution must be an object")
    if _has_retired_canary(execution):
        raise DevelopmentPolicyError(
            "a retired development-v1 filesystem-policy canary grant is present; "
            "the canary machinery is removed and worker execution is refused"
        )
    configured = execution.get(WORKER_ACCESS_KEY, RESTRICTED_ACCESS)
    # A list/dict value is unhashable: test membership only after confirming it
    # is a string so an unhashable mode fails closed here instead of raising an
    # uncaught TypeError past the callers that expect DevelopmentPolicyError.
    if not isinstance(configured, str) or configured not in WORKER_ACCESS_MODES:
        raise DevelopmentPolicyError(
            f"execution.{WORKER_ACCESS_KEY} must be one of "
            + ", ".join(sorted(WORKER_ACCESS_MODES))
        )
    request = request or {}
    if request.get("sandbox") == "danger-full-access":
        raise DevelopmentPolicyError(
            "managed worker requests cannot select full-access mode"
        )
    strict = (
        request.get("sandbox") == "read-only"
        or request.get("strict_scope") is True
        or request.get("parallel_safe") is True
    )
    if strict:
        return WorkerAccessDecision(configured, RESTRICTED_ACCESS, "explicit strict request")
    return WorkerAccessDecision(configured, configured, "controller setting")
