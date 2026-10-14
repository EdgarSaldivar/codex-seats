"""Policy defaults, validation, and digest-pinned settings updates."""

import json
import os
from unittest import mock

import pytest

from codex_seats.policy_settings import (
    PolicyError, apply_policy, normalize_policy_value, policy_report, preview_policy,
)
from codex_seats.state import default_settings


def test_defaults_and_validation():
    assert policy_report({}) == {
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
    assert normalize_policy_value("execution.inline_max_minutes", "12") == 12
    assert normalize_policy_value("execution.head_worker_fallback", False) is False
    for key, value in (
        ("review.mode", "required"),
        ("review.strategy", "both"),
        ("execution.inline_max_minutes", 0),
        ("execution.inline_max_minutes", 241),
        ("execution.inline_max_minutes", True),
        ("execution.head_worker_fallback", "false"),
        ("pool.probe_mode", "structured"),
        ("execution.worker_access", "trusted-development"),
        ("ui.theme", "purple"),
        ("ui.density", True),
        ("ui.accent", "blue"),
    ):
        with pytest.raises(PolicyError):
            normalize_policy_value(key, value)


def test_preview_apply_preserves_unknown_metadata_and_rejects_stale_or_worker(tmp_path):
    root = tmp_path
    settings = default_settings()
    settings["future_metadata"] = {"kept": True}
    path = root / "settings.json"
    path.write_text(json.dumps(settings) + "\n")
    preview = preview_policy(root, {"review.mode": "advisory"})
    assert json.loads(preview.before)["future_metadata"] == {"kept": True}
    assert json.loads(preview.after)["review"]["mode"] == "advisory"
    with mock.patch.dict(os.environ, {"CODEX_SEATS_MANAGED_WORKER": "1"}):
        with pytest.raises(PolicyError, match="managed workers"):
            apply_policy(root, preview)
    assert path.read_text() == preview.before
    with mock.patch.dict(os.environ, {"CODEX_SEATS_MANAGED_WORKER": "0"}):
        assert apply_policy(root, preview)
        assert json.loads(path.read_text())["future_metadata"] == {"kept": True}
        with pytest.raises(PolicyError, match="stale"):
            apply_policy(root, preview)


def test_preview_reloads_and_rejects_malformed_policy(tmp_path):
    root = tmp_path
    path = root / "settings.json"
    settings = default_settings()
    settings["review"]["mode"] = "blocking"
    path.write_text(json.dumps(settings))
    assert policy_report(json.loads(preview_policy(root, {}).before))["review.mode"] == "blocking"
    settings["review"]["mode"] = "mandatory"
    path.write_text(json.dumps(settings))
    with pytest.raises(ValueError, match="review.mode"):
        preview_policy(root, {})


def test_every_preference_previews_typed_value_without_losing_metadata(tmp_path):
    settings = default_settings()
    settings["future_metadata"] = {"nested": [1, 2]}
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings) + "\n")
    values = {
        "review.mode": "blocking",
        "review.strategy": "cross",
        "execution.delegation_policy": "manual",
        "execution.inline_max_minutes": "240",
        "execution.head_permissions": "safe",
        "execution.head_worker_fallback": False,
        "ui.theme": "dark",
        "ui.density": "compact",
        "ui.accent": "indigo",
    }
    preview = preview_policy(tmp_path, values)
    updated = json.loads(preview.after)
    assert updated["future_metadata"] == settings["future_metadata"]
    assert policy_report(updated) == {**values, "execution.inline_max_minutes": 240}
    assert path.read_text() == preview.before
