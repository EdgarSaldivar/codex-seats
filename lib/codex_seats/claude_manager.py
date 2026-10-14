"""Guarded singleton Claude account view and settings editor.

Claude is one subscription account behind ``providers.claude``; this module
keeps that singleton architecture. It is the Claude counterpart of
:mod:`local_manager` and shares the :mod:`seat_manager` error types and write
contract.

Design contract
---------------
* **Safe reads.** :meth:`ClaudeAccountManager.report` returns the configured and
  effective mode, the provider status from the read-only pool snapshot, the
  small/big minimum headroom floors, the subscription tier with its source, and
  (only when Claude is not effectively off) the passively cached usage windows.
  It never opens credentials, runs the ``claude`` binary, probes, or writes.
* **Tier source.** No documented, provider-owned, nonsecret field carries the
  subscription plan: the status-line JSON documents only ``rate_limits`` windows
  and ``claude auth status`` documents no output fields while requiring the
  credentialed client. :func:`resolve_subscription_tier` therefore prefers a
  validated provider-detected label only when a caller supplies one and
  otherwise reports the operator-configured fallback label with
  ``source="configured"`` (or ``"unknown"`` when unset).
* **Immutable previews.** :class:`ClaudeAccountPreview` freezes the requested
  changes, the before/after ``providers.claude`` entries and the full
  settings-byte ``expected_digest``. :meth:`ClaudeAccountManager.commit` replays
  the edit inside the settings lock only while that digest is still current;
  otherwise it raises :class:`StalePreviewError` with nothing written.
  Unrelated settings are preserved by the shared transaction.

Public API
----------
* ``ClaudeAccountManager(root, *, clock=time.time)``
* ``manager.report(now=None)``
* ``manager.preview(**changes) -> ClaudeAccountPreview`` and the ``preview_*``
  conveniences for mode, headroom floors and the subscription tier label
* ``manager.commit(preview, *, from_current_user_request) -> ClaudeAccountMutationResult``
* ``manager.close()``
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from . import state
from .seat_manager import (
    ManagerClosedError,
    ManagerEligibilityError,
    ManagerError,
    StalePreviewError,
    _freeze,
    _thaw,
    ensure_management_eligible,
)

# Known consumer/organization plan labels, in display form.
CLAUDE_SUBSCRIPTION_TIERS = (
    "Pro", "Max 5x", "Max 20x", "Team", "Team Premium", "Enterprise",
)
_TIER_BY_KEY = {"".join(tier.lower().split()): tier for tier in CLAUDE_SUBSCRIPTION_TIERS}

EDITABLE_FIELDS = frozenset({
    "mode", "small_task_min_headroom_pct", "big_task_min_headroom_pct", "subscription_tier", "active_usage_refresh",
})

TIER_DETECTION = {
    "available": False,
    "reason": (
        "no documented provider-owned nonsecret plan field: the status-line JSON "
        "exposes only rate_limits windows, and `claude auth status` documents no "
        "output fields and would run the credentialed client"
    ),
}


def normalize_subscription_tier(value: Any) -> str | None:
    """Return the canonical label for ``value``; ``None`` clears the label.

    Matching ignores case and whitespace (``"max 5x"``/``"MAX5X"`` -> ``"Max 5x"``).
    Anything outside :data:`CLAUDE_SUBSCRIPTION_TIERS` is rejected.
    """
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > state.MAX_CLAUDE_SUBSCRIPTION_TIER_LENGTH:
        raise ValueError("subscription_tier must be a known plan label or null")
    tier = _TIER_BY_KEY.get("".join(value.lower().split()))
    if tier is None:
        raise ValueError(
            "subscription_tier must be one of " + ", ".join(CLAUDE_SUBSCRIPTION_TIERS) + " or null"
        )
    return tier


def resolve_subscription_tier(configured: Any, detected: Any = None) -> dict[str, Any]:
    """Pick the reported tier: a valid provider-detected label, else the fallback.

    The configured label is always echoed so a later auto-detected value never
    hides what the operator declared. Unknown or malformed inputs are ignored
    rather than displayed.
    """
    def known(value: Any) -> str | None:
        try:
            return normalize_subscription_tier(value)
        except ValueError:
            return None

    configured_label, detected_label = known(configured), known(detected)
    if detected_label is not None:
        label, source = detected_label, "provider"
    elif configured_label is not None:
        label, source = configured_label, "configured"
    else:
        label, source = None, "unknown"
    return {
        "label": label,
        "source": source,
        "configured": configured_label,
        "detected": detected_label,
        "mismatch": (
            configured_label is not None and detected_label is not None
            and configured_label != detected_label
        ),
        "detection": dict(TIER_DETECTION),
    }


def _percentage(value: Any, name: str) -> float | int:
    if (
        not isinstance(value, (int, float)) or isinstance(value, bool)
        or not math.isfinite(value) or not 0 <= value <= 100
    ):
        raise ValueError(f"{name} must be a percentage from 0 to 100")
    return value


def _digest(before: str | None) -> str:
    return hashlib.sha256((before or "").encode()).hexdigest()


def _effective(document: Mapping[str, Any]) -> dict[str, Any]:
    return state._merge(state.default_settings(), dict(document))


def _claude_entry(document: Mapping[str, Any]) -> dict[str, Any]:
    return deepcopy(_effective(document)["providers"]["claude"])


def _apply(document: dict[str, Any], changes: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Apply validated ``changes`` to ``document``; return (enabling, warnings)."""
    if not isinstance(changes, Mapping) or not changes:
        raise ValueError("changes must be a non-empty object")
    unknown = sorted(set(changes) - EDITABLE_FIELDS)
    if unknown:
        raise ValueError(f"unsupported Claude account field(s): {', '.join(unknown)}")
    before = _claude_entry(document)
    updates: dict[str, Any] = {}
    if "mode" in changes:
        mode = changes["mode"]
        if not isinstance(mode, str) or mode not in state.VALID_CLAUDE_MODES:
            raise ValueError(
                "mode must be one of " + ", ".join(sorted(state.VALID_CLAUDE_MODES))
            )
        updates["mode"] = mode
    for key in ("small_task_min_headroom_pct", "big_task_min_headroom_pct"):
        if key in changes:
            updates[key] = _percentage(changes[key], key)
    if "subscription_tier" in changes:
        updates["subscription_tier"] = normalize_subscription_tier(changes["subscription_tier"])
    if "active_usage_refresh" in changes:
        if type(changes["active_usage_refresh"]) is not bool:
            raise ValueError("active_usage_refresh must be boolean")
        updates["active_usage_refresh"] = changes["active_usage_refresh"]
    small = updates.get("small_task_min_headroom_pct", before.get("small_task_min_headroom_pct"))
    big = updates.get("big_task_min_headroom_pct", before.get("big_task_min_headroom_pct"))
    if (
        {"small_task_min_headroom_pct", "big_task_min_headroom_pct"} & changes.keys()
        and isinstance(small, (int, float))
        and isinstance(big, (int, float))
        and small > big
    ):
        raise ValueError(
            "small_task_min_headroom_pct must not exceed big_task_min_headroom_pct"
        )

    providers = document.setdefault("providers", {})
    if not isinstance(providers, dict):
        raise ValueError("providers must be an object")
    claude = providers.setdefault("claude", {})
    if not isinstance(claude, dict):
        raise ValueError("providers.claude must be an object")
    claude.update(updates)

    warnings: list[str] = []
    new_mode = updates.get("mode", before.get("mode"))
    enabling = new_mode != "off" and new_mode != before.get("mode")
    if new_mode != "off":
        effective, reason = state.effective_claude_mode(_effective(document))
        if effective == "off" and reason:
            warnings.append(
                f"Claude stays effectively off ({reason}); clear providers.claude.expires_at "
                "through `codex-seat provider set` to enable it"
            )
    return enabling, warnings


@dataclass(frozen=True)
class ClaudeAccountPreview:
    """Immutable Claude settings edit bound to a full settings digest."""

    changes: Mapping[str, Any]
    changed: bool
    enabling: bool  # the write turns Claude on or switches to another on-mode
    before: Mapping[str, Any]  # effective providers.claude entry
    after: Mapping[str, Any]
    warnings: tuple[str, ...]
    expected_digest: str

    def __post_init__(self) -> None:
        for name in ("changes", "before", "after"):
            object.__setattr__(self, name, _freeze(getattr(self, name)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "changes": _thaw(self.changes),
            "changed": self.changed,
            "enabling": self.enabling,
            "before": _public_config(_thaw(self.before)),
            "after": _public_config(_thaw(self.after)),
            "warnings": list(self.warnings),
            "expected_digest": self.expected_digest,
        }


@dataclass(frozen=True)
class ClaudeAccountMutationResult:
    """Outcome of one committed Claude settings write."""

    changed: bool
    enabling: bool
    before_digest: str
    after_digest: str
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self) | {"warnings": list(self.warnings)}


def _public_config(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "mode": entry.get("mode"),
        "expires_at": entry.get("expires_at"),
        "small_task_min_headroom_pct": entry.get("small_task_min_headroom_pct"),
        "big_task_min_headroom_pct": entry.get("big_task_min_headroom_pct"),
        "subscription_tier": entry.get("subscription_tier"),
        "active_usage_refresh": entry.get("active_usage_refresh"),
    }


def _provider_status(root: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        snapshot = state.read_pool_snapshot(root)
    except (state.PoolSnapshotError, OSError, ValueError) as exc:
        return None, str(exc)
    provider = snapshot["providers"]["claude"]
    models = provider.get("models")
    capped = sorted(
        name for name, model in (models.items() if isinstance(models, dict) else ())
        if isinstance(model, dict) and model.get("status") == "capped"
    )
    return {
        "status": provider.get("status"),
        "status_until": provider.get("status_until"),
        "busy": bool(provider.get("in_flight")),
        "last_job_at": provider.get("last_job_at"),
        "capped_models": capped,
    }, None


def _usage(root: Path, settings: dict[str, Any], mode: str, current: float) -> dict[str, Any]:
    if mode == "off":
        # Preserve the hard-off guarantee: the cache is not even read.
        return {"read": False, "reason": "Claude is effectively off; usage cache not read"}
    limits = state.load_claude_rate_limits(root, at=current)
    windows = {
        name: {
            "remaining_percentage": limits[name]["remaining_percentage"],
            "resets_at": limits[name].get("resets_at"),
        }
        for name in ("five_hour", "seven_day")
        if isinstance(limits.get(name), dict)
    }
    remaining = [window["remaining_percentage"] for window in windows.values()]
    return {
        "read": True,
        "windows": windows,
        "pct_left": min(remaining) if len(remaining) == 2 else None,
        "fresh": state.claude_usage_fresh(settings, limits, at=current),
        "age_seconds": state.claude_usage_age_seconds(limits, at=current),
    }


class ClaudeAccountManager:
    """Serialize guarded singleton Claude account management for one session."""

    def __init__(self, root: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self._clock = clock
        self._lock = threading.RLock()
        self._closed = False

    # -- inspection ---------------------------------------------------------
    def report(self, *, now: float | None = None) -> dict[str, Any]:
        """Read-only account view from settings, the pool snapshot and usage cache.

        Invalid settings raise :class:`ManagerError` rather than silently
        reporting defaults. A missing or unreadable pool snapshot leaves
        ``status`` as ``None`` with ``status_error`` set.
        """
        document, before, warnings = self._read()
        settings = _effective(document)
        claude = settings["providers"]["claude"]
        current = self._clock() if now is None else now
        mode, reason = state.effective_claude_mode(settings, at=current)
        status, status_error = _provider_status(self.root)
        return {
            "account": "claude",
            "configured_mode": claude.get("mode"),
            "active_usage_refresh": claude.get("active_usage_refresh"),
            "mode": mode,
            "mode_reason": reason,
            "expires_at": claude.get("expires_at"),
            "status": status,
            "status_error": status_error,
            "headroom": {
                "small_min_pct": claude.get("small_task_min_headroom_pct"),
                "big_min_pct": claude.get("big_task_min_headroom_pct"),
            },
            "subscription_tier": resolve_subscription_tier(claude.get("subscription_tier")),
            "usage": _usage(self.root, settings, mode, current),
            "warnings": list(warnings),
            "settings_digest": _digest(before),
        }

    # -- previews -----------------------------------------------------------
    def preview(self, **changes: Any) -> ClaudeAccountPreview:
        """Return an immutable, digest-pinned preview; nothing is written."""
        document, before, _warnings = self._read()
        updated = deepcopy(document)
        try:
            enabling, warnings = _apply(updated, changes)
            canonical, _ = state._validated_settings_document(updated, root=self.root)
        except (ValueError, KeyError, TypeError) as exc:
            raise ManagerError(str(exc)) from exc
        before_entry, after_entry = _claude_entry(document), _claude_entry(canonical)
        if changes.get("mode") == "off" and before_entry.get("mode") != "off":
            status, _error = _provider_status(self.root)
            if status is not None and status.get("busy"):
                warnings.append(
                    "A Claude job is in flight; off stops new routing and lets it finish"
                )
        return ClaudeAccountPreview(
            changes=changes,
            changed=before_entry != after_entry,
            enabling=enabling,
            before=before_entry,
            after=after_entry,
            warnings=tuple(warnings),
            expected_digest=_digest(before),
        )

    def preview_mode(self, mode: str) -> ClaudeAccountPreview:
        return self.preview(mode=mode)

    def preview_headroom(
        self, *, small: float | None = None, big: float | None = None
    ) -> ClaudeAccountPreview:
        changes: dict[str, Any] = {}
        if small is not None:
            changes["small_task_min_headroom_pct"] = small
        if big is not None:
            changes["big_task_min_headroom_pct"] = big
        return self.preview(**changes)

    def preview_subscription_tier(self, label: str | None) -> ClaudeAccountPreview:
        return self.preview(subscription_tier=label)

    # -- commit -------------------------------------------------------------
    def commit(
        self, preview: ClaudeAccountPreview, *, from_current_user_request: bool
    ) -> ClaudeAccountMutationResult:
        """Apply a previewed edit with the explicit confirmation marker.

        Refused after close, for controller/head and managed-worker contexts
        (even on a PTY), without a real TTY, and without
        ``from_current_user_request=True``. The edit is re-applied inside the
        settings lock only if the file still matches ``preview.expected_digest``.
        """
        with self._lock:
            if self._closed:
                raise ManagerClosedError(
                    "the Claude account manager has closed and cannot mutate settings"
                )
            if from_current_user_request is not True:
                raise ManagerError(
                    "Claude account edits require explicit confirmation of the current request"
                )
            try:
                ensure_management_eligible()
            except ManagerEligibilityError as exc:
                raise ManagerEligibilityError(f"Claude account edit refused: {exc}") from exc
            changes = _thaw(preview.changes)
            try:
                with state.settings_transaction(self.root, require_existing=True) as transaction:
                    before_digest = _digest(transaction.before)
                    if before_digest != preview.expected_digest:
                        raise StalePreviewError(
                            "settings changed since this Claude edit was previewed; "
                            "re-preview before applying"
                        )
                    enabling, warnings = _apply(transaction.settings, changes)
            except ManagerError:
                raise
            except (ValueError, KeyError, TypeError, OSError) as exc:
                raise ManagerError(str(exc)) from exc
            return ClaudeAccountMutationResult(
                changed=transaction.changed,
                enabling=enabling,
                before_digest=before_digest,
                after_digest=_digest(transaction.after),
                warnings=tuple(warnings),
            )

    def close(self) -> None:
        with self._lock:
            self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    # -- internals ----------------------------------------------------------
    def _read(self) -> tuple[dict[str, Any], str | None, list[str]]:
        try:
            document, before, _payload, warnings = state._load_settings_for_mutation(
                self.root, require_existing=True
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ManagerError(str(exc)) from exc
        return document, before, warnings
