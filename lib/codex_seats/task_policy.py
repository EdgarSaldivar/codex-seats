"""Pure task-policy resolution shared by every routing entry point.

``size`` answers how much capacity a task is expected to burn.  ``capability``
answers which provider model rung it needs.  They are deliberately independent:
overriding one must never silently change the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .efforts import EFFORTS


TASK_KINDS = (
    "implementation",
    "ui_ux",
    "mechanical",
    "review",
    "audit",
    "debug",
    "security",
    "test",
    "docs",
    "planning",
    "research",
    "deep_research",
)
# Every policy kind is a typed delegation kind.  Heads classify work and submit the
# kind; provider/model selection remains runner-owned and is never carried in the
# untrusted dispatch envelope.
DELEGATE_KINDS = TASK_KINDS
SIZES = ("small", "big")
CAPABILITY_TIERS = ("routine", "complex", "frontier")
AUTO = "auto"
REQUESTED_SIZES = (AUTO, *SIZES)
REQUESTED_CAPABILITIES = (AUTO, *CAPABILITY_TIERS)
RISK_FLAGS = (
    "api",
    "auth",
    "concurrency",
    "migration",
    "multi_repo",
    "permissions",
    "planning",
    "security",
    "ui",
)
TASK_SHAPES = ("execute", "investigate", "design")
VALIDATION_LEVELS = ("strong", "weak")
UNSPECIFIED_VALIDATION = "unspecified"
DEFAULT_TASK_SHAPE = "execute"
DEFAULT_VALIDATION = UNSPECIFIED_VALIDATION

# Provider execution remains finite even for the largest accepted estimate.  Keep this
# no larger than the dispatch protocol's existing 24-hour request-lifetime ceiling.
MAX_RUN_TIMEOUT_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class TaskProfile:
    kind: str
    task_shape: str
    validation: str
    size: str
    capability: str
    effort: str | None
    estimated_minutes: float
    parallel_safe: bool
    scope_hints: tuple[str, ...]
    risk_flags: tuple[str, ...]
    override_reason: str | None
    requested_size: str
    requested_capability: str
    preferred_provider: str
    claude_eligible: bool
    codex_model: str


# Compatibility name for callers introduced before the full profile landed.
TaskPolicy = TaskProfile


DEFAULT_SIZE_BY_KIND = {
    "implementation": "big",
    "ui_ux": "big",
    "planning": "big",
    "audit": "big",
    "security": "big",
    "mechanical": "small",
    "review": "small",
    "debug": "small",
    "test": "small",
    "docs": "small",
    "research": "small",
    "deep_research": "big",
}
DEFAULT_CAPABILITY_BY_KIND = {
    "implementation": "complex",
    "ui_ux": "frontier",
    "planning": "frontier",
    "audit": "frontier",
    "security": "frontier",
    "mechanical": "routine",
    "review": "complex",
    "debug": "complex",
    "test": "routine",
    "docs": "routine",
    # Bounded lookup and synthesis should not pay the frontier premium by default.
    # Open-ended, multi-source, or high-uncertainty investigation is a distinct kind.
    "research": "complex",
    "deep_research": "frontier",
}
DEFAULT_EFFORT_BY_KIND = {
    "mechanical": "low",
    "docs": "low",
    "test": "medium",
    "debug": "high",
    "implementation": "high",
    "ui_ux": "high",
    "review": "medium",
    "audit": "high",
    "security": "max",
    "planning": "high",
    "research": "medium",
    "deep_research": "high",
}
DEFAULT_PROVIDER_BY_KIND = {
    "implementation": "codex",
    "ui_ux": "codex",
    "mechanical": "codex",
    "debug": "codex",
    "test": "codex",
    "docs": "codex",
    "review": "claude",
    "audit": "claude",
    "security": "claude",
    "planning": "claude",
    "research": "claude",
    "deep_research": "claude",
}
DEFAULT_CLAUDE_ELIGIBLE_KINDS = TASK_KINDS
DEFAULT_CLAUDE_FRONTIER_KINDS = (
    "security",
    "audit",
    "ui_ux",
    "planning",
    "deep_research",
)

DEFAULT_CLAUDE_MODEL_ROLES = {
    "routine": "claude-sonnet-5",
    "complex": "claude-opus-5-5",
    "frontier": "claude-fable-5-1",
}

_CLAUDE_CURRENT_MODEL_ALIASES = {
    "sonnet": DEFAULT_CLAUDE_MODEL_ROLES["routine"],
    "opus": DEFAULT_CLAUDE_MODEL_ROLES["complex"],
    "fable": DEFAULT_CLAUDE_MODEL_ROLES["frontier"],
    "claude-fable-5": DEFAULT_CLAUDE_MODEL_ROLES["frontier"],
    "claude-opus-5": DEFAULT_CLAUDE_MODEL_ROLES["complex"],
    "claude-opus-4-8": DEFAULT_CLAUDE_MODEL_ROLES["complex"],
}


def current_claude_role_model(model: str) -> str:
    """Resolve legacy role aliases to the current pinned model ID.

    Older Fable 5, Opus 5, and Opus 4.8 selections advance to current role IDs.
    """
    return _CLAUDE_CURRENT_MODEL_ALIASES.get(model.lower(), model)


DEFAULT_CODEX_MODEL_ROLES = {
    "routine": "gpt-5.6-terra",
    "complex": "gpt-6-sol",
    "frontier": "gpt-6-astra",
}

_PREVIOUS_CODEX_MODEL_ROLES = {
    "complex": "gpt-5.6-sol",
}


def current_codex_role_model(role: str, model: str) -> str:
    """Upgrade a previous default only in the role where it was the default."""
    if _PREVIOUS_CODEX_MODEL_ROLES.get(role) == model:
        return DEFAULT_CODEX_MODEL_ROLES[role]
    return model

# These high-volume classes have a more useful default than their broad capability
# rung. A risk/capability promotion still wins, so a security-sensitive docs change is
# sent to Astra rather than being pinned to Terra.
DEFAULT_CODEX_MODEL_BY_KIND = {
    "mechanical": "gpt-5.6-luna",
    "docs": "gpt-5.6-terra",
    "test": "gpt-5.6-terra",
    "ui_ux": "gpt-6-astra",
}

AUTHORING_KINDS = frozenset(
    {
        "implementation",
        "ui_ux",
        "mechanical",
        "debug",
        "test",
        "docs",
        "planning",
        "research",
        "deep_research",
    }
)


def resolve_task_policy(
    settings: dict[str, Any],
    kind: str,
    *,
    size: str | None = None,
    capability: str | None = None,
    task_shape: str | None = None,
    validation: str | None = None,
    effort: str | None = None,
    estimated_minutes: float | None = None,
    parallel_safe: bool = False,
    scope_hints: tuple[str, ...] | list[str] = (),
    risk_flags: tuple[str, ...] | list[str] = (),
    override_reason: str | None = None,
    require_override_reason: bool = False,
) -> TaskProfile:
    """Resolve a task without I/O or mutable state.

    Settings remain operator-tunable. Invalid/missing individual overrides fall
    back to the conservative built-in policy instead of widening eligibility.
    """
    execution = settings.get("execution", {}) if isinstance(settings, dict) else {}
    providers = settings.get("providers", {}) if isinstance(settings, dict) else {}
    claude = providers.get("claude", {}) if isinstance(providers, dict) else {}
    if task_shape is not None and task_shape not in TASK_SHAPES:
        raise ValueError(f"task_shape must be one of {sorted(TASK_SHAPES)}")
    if validation is not None and validation not in (
        *VALIDATION_LEVELS,
        UNSPECIFIED_VALIDATION,
    ):
        raise ValueError(
            "validation must be one of "
            f"{sorted((*VALIDATION_LEVELS, UNSPECIFIED_VALIDATION))}"
        )
    resolved_task_shape = task_shape or DEFAULT_TASK_SHAPE
    resolved_validation = validation or DEFAULT_VALIDATION

    configured_sizes = execution.get("default_size_by_kind")
    configured_size = (
        configured_sizes.get(kind) if isinstance(configured_sizes, dict) else None
    )
    requested_size = size if size in (*SIZES, AUTO) else AUTO
    resolved_size = requested_size if requested_size in SIZES else (
        configured_size if configured_size in SIZES else "small"
    )

    configured_capability = execution.get("capability_tier_by_kind", {}).get(kind)
    frontier_kinds = claude.get("frontier_kinds", [])
    requested_capability = (
        capability if capability in (*CAPABILITY_TIERS, AUTO) else AUTO
    )
    resolved_capability = (
        requested_capability
        if requested_capability in CAPABILITY_TIERS
        else (
        "frontier"
        if isinstance(frontier_kinds, list) and kind in frontier_kinds
        else (
            configured_capability
            if configured_capability in CAPABILITY_TIERS
            else DEFAULT_CAPABILITY_BY_KIND.get(kind, "routine")
        )
        )
    )
    normalized_risks = tuple(
        sorted(
            {
                value
                for value in risk_flags
                if isinstance(value, str) and value in RISK_FLAGS
            }
        )
    )
    # Risk sets a conservative automatic estimate, but must not overwrite an
    # explicitly scoped capacity estimate. Capability promotion still applies.
    if normalized_risks and requested_size not in SIZES:
        resolved_size = "big"
    if {"security", "permissions", "auth", "ui", "planning"} & set(
        normalized_risks
    ):
        resolved_capability = "frontier"
    elif {"api", "concurrency", "migration", "multi_repo"} & set(normalized_risks):
        if resolved_capability == "routine":
            resolved_capability = "complex"

    if (
        isinstance(estimated_minutes, (int, float))
        and not isinstance(estimated_minutes, bool)
        and float(estimated_minutes) > 0
    ):
        resolved_minutes = float(estimated_minutes)
    else:
        resolved_minutes = 5.0 if resolved_size == "small" else 20.0
    configured_default_size = (
        configured_size
        if configured_size in SIZES
        else DEFAULT_SIZE_BY_KIND.get(kind, "small")
    )
    configured_default_capability = (
        configured_capability
        if configured_capability in CAPABILITY_TIERS
        else DEFAULT_CAPABILITY_BY_KIND.get(kind, "routine")
    )
    explicit_override = (
        requested_size in SIZES and requested_size != configured_default_size
    ) or (
        requested_size == "small" and bool(normalized_risks)
    ) or (
        requested_capability in CAPABILITY_TIERS
        and requested_capability != configured_default_capability
    )
    normalized_reason = (
        override_reason.strip()
        if isinstance(override_reason, str) and override_reason.strip()
        else None
    )
    if require_override_reason and explicit_override and normalized_reason is None:
        raise ValueError("explicit size/capability override requires override_reason")

    configured_efforts = execution.get("effort_by_kind")
    configured_effort = (
        configured_efforts.get(kind)
        if isinstance(configured_efforts, dict)
        else None
    )
    requested_effort = effort if effort in EFFORTS else None
    resolved_effort = requested_effort if requested_effort is not None else (
        configured_effort if configured_effort in EFFORTS else None
    )
    explicit_effort_override = (
        requested_effort is not None and requested_effort != configured_effort
    )
    if (
        require_override_reason
        and explicit_effort_override
        and normalized_reason is None
    ):
        raise ValueError("explicit effort override requires override_reason")

    configured_preferences = execution.get("prefer_provider_by_kind")
    configured_preference = (
        configured_preferences.get(kind)
        if isinstance(configured_preferences, dict)
        else None
    )
    preferred = (
        configured_preference
        if configured_preference in {"codex", "claude"}
        else "codex"
    )
    configured_eligible = claude.get("eligible_kinds")
    eligible = (
        kind in configured_eligible
        if isinstance(configured_eligible, list)
        else False
    )
    codex = providers.get("codex", {}) if isinstance(providers, dict) else {}
    role_models = codex.get("model_roles") if isinstance(codex, dict) else None
    kind_models = codex.get("model_by_kind") if isinstance(codex, dict) else None
    role_model = (
        role_models.get(resolved_capability)
        if isinstance(role_models, dict)
        else None
    )
    if not isinstance(role_model, str) or not role_model:
        role_model = DEFAULT_CODEX_MODEL_ROLES[resolved_capability]
    kind_model = kind_models.get(kind) if isinstance(kind_models, dict) else None
    if not isinstance(kind_model, str) or not kind_model:
        kind_model = DEFAULT_CODEX_MODEL_BY_KIND.get(kind)
    # A promotion above the kind's normal capability must promote its model too.
    normal_capability = (
        configured_capability
        if configured_capability in CAPABILITY_TIERS
        else DEFAULT_CAPABILITY_BY_KIND.get(kind, "routine")
    )
    codex_model = (
        kind_model
        if kind_model is not None and resolved_capability == normal_capability
        else role_model
    )

    return TaskProfile(
        kind=kind,
        task_shape=resolved_task_shape,
        validation=resolved_validation,
        size=resolved_size,
        capability=resolved_capability,
        effort=resolved_effort,
        estimated_minutes=resolved_minutes,
        parallel_safe=bool(parallel_safe),
        scope_hints=tuple(
            value for value in scope_hints if isinstance(value, str) and value
        ),
        risk_flags=normalized_risks,
        override_reason=normalized_reason,
        requested_size=requested_size,
        requested_capability=requested_capability,
        preferred_provider=preferred,
        claude_eligible=eligible,
        codex_model=codex_model,
    )


def claude_provider_eligible(
    policy: TaskProfile,
    mode: str,
    *,
    capacity_available: bool,
    provider_available: bool = True,
) -> bool:
    """Return whether an automatic route may currently use Claude."""
    if mode in {"off", "manual_only"} or not policy.claude_eligible:
        return False
    if mode == "review_only" and policy.kind not in {"review", "audit", "security"}:
        return False
    return capacity_available and provider_available


def timeout_seconds(profile: TaskProfile) -> int:
    """Bound one managed work unit."""
    return int(
        max(600, min(MAX_RUN_TIMEOUT_SECONDS, profile.estimated_minutes * 120))
    )
