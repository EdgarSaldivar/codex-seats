"""Validated orchestration preferences in the existing settings document.

An explicit goal review command is a blocking gate; ``review: none`` skips it.
Only an empty goal review inherits these runtime preferences.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

DEFAULTS: dict[str, Any] = {
    "review.mode": "off",
    "review.strategy": "single",
    "execution.delegation_policy": "auto",
    "execution.inline_max_minutes": 5,
    "execution.head_permissions": "full-access",
    "execution.head_worker_fallback": True,
    "ui.theme": "system",
    "ui.density": "comfortable",
    "ui.accent": "teal",
}
CHOICES = {
    "review.mode": {"off", "advisory", "blocking"},
    "review.strategy": {"single", "cross"},
    "execution.delegation_policy": {"auto", "always", "manual"},
    "execution.head_permissions": {"safe", "full-access"},
    "ui.theme": {"system", "light", "dark"},
    "ui.density": {"comfortable", "compact"},
    "ui.accent": {"teal", "indigo"},
}


class PolicyError(ValueError):
    """Invalid preference or stale settings preview."""


@dataclass(frozen=True)
class PolicyPreview:
    before: str
    after: str
    digest: str
    changes: tuple[tuple[str, Any], ...]


def normalize_policy_value(key: str, value: Any) -> Any:
    if key not in DEFAULTS:
        raise PolicyError(f"unknown policy key {key!r}")
    if key == "execution.inline_max_minutes":
        if isinstance(value, str) and value.isdecimal():
            value = int(value)
        if type(value) is not int or not 1 <= value <= 240:
            raise PolicyError(f"{key} must be an integer from 1 to 240")
    elif isinstance(DEFAULTS[key], bool):
        if type(value) is not bool:
            raise PolicyError(f"{key} must be true or false")
    elif not isinstance(value, str) or value not in CHOICES[key]:
        raise PolicyError(f"{key} must be one of {', '.join(sorted(CHOICES[key]))}")
    return value


def policy_report(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve and validate the effective policy, including absent defaults."""
    result = {}
    for key, default in DEFAULTS.items():
        section, field = key.split(".")
        source = settings.get(section, {})
        if not isinstance(source, Mapping):
            raise PolicyError(f"{section} must be an object")
        result[key] = normalize_policy_value(key, source.get(field, default))
    return result


def _updated(settings: dict[str, Any], changes: Mapping[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(settings)
    for key, value in changes.items():
        normalized = normalize_policy_value(key, value)
        section, field = key.split(".")
        updated.setdefault(section, {})[field] = normalized
    policy_report(updated)
    return updated


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def preview_policy(root: str | Path, changes: Mapping[str, Any]) -> PolicyPreview:
    """Read under the settings lock and pin exact file bytes for later apply."""
    from .state import (
        _read_settings_payload, _validated_settings_document, settings_lock,
        settings_path,
    )

    root = Path(root)
    with settings_lock(root):
        payload = _read_settings_payload(settings_path(root))
        try:
            raw = json.loads(payload)
            if not isinstance(raw, dict):
                raise PolicyError("settings top level must be an object")
            settings, _ = _validated_settings_document(raw, root=root)
        except (ValueError, TypeError) as exc:
            raise PolicyError(f"invalid settings: {exc}") from exc
        updated = _updated(settings, changes)
        _validated_settings_document(updated, root=root)
        return PolicyPreview(
            before=payload.decode("utf-8"),
            after=json.dumps(updated, indent=2) + "\n",
            digest=_digest(payload),
            changes=tuple(sorted(
                (key, normalize_policy_value(key, value))
                for key, value in changes.items()
            )),
        )


def apply_policy(root: str | Path, preview: PolicyPreview) -> bool:
    """Apply a digest-pinned preview through the shared atomic transaction."""
    if os.environ.get("CODEX_SEATS_MANAGED_WORKER") == "1" or os.environ.get("CLAUDE_LEAD_WORKER") == "1":
        raise PolicyError("managed workers cannot mutate settings")
    if not isinstance(preview, PolicyPreview):
        raise PolicyError("a PolicyPreview is required")
    from .state import settings_transaction

    with settings_transaction(Path(root), require_existing=True) as transaction:
        if transaction.before is None or _digest(transaction.before.encode("utf-8")) != preview.digest:
            raise PolicyError("stale policy preview: settings changed; preview again")
        updated = _updated(transaction.settings, dict(preview.changes))
        if json.dumps(updated, indent=2) + "\n" != preview.after:
            raise PolicyError("policy preview does not match current settings")
        transaction.settings = updated
    return transaction.changed
