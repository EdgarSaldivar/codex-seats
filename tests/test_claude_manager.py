from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from codex_seats import state
from codex_seats.claude_manager import (
    CLAUDE_SUBSCRIPTION_TIERS,
    ClaudeAccountManager,
    normalize_subscription_tier,
    resolve_subscription_tier,
)
from codex_seats.seat_manager import (
    ManagerClosedError,
    ManagerEligibilityError,
    ManagerError,
    StalePreviewError,
)
from codex_seats.state import Ledger, load_settings, default_settings

NOW = 1_788_454_400.0
GUARD_VARS = (
    "CODEX_CONTROL_SEAT",
    "CODEX_SEATS_HEAD",
    "CODEX_SEATS_MANAGED_WORKER",
    "CODEX_THREAD_ID",
    "CODEX_HOME",
    "CODEX_SESSION_ID",
)


def _human_terminal(monkeypatch) -> None:
    for name in GUARD_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)


def _write_settings(root: Path, **claude) -> dict:
    settings = default_settings()
    settings["providers"]["claude"].update(claude)
    settings["future"] = {"keep": [1, 2]}
    root.mkdir(parents=True, exist_ok=True)
    (root / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
    (root / "settings.json").chmod(0o600)
    return settings


def _usage_cache(root: Path) -> None:
    (root / "claude-usage.json").write_text(json.dumps({
        "schema": 2,
        "updated_at": NOW - 60,
        "five_hour": {"used_percentage": 30, "resets_at": NOW + 3600, "observed_at": NOW - 60},
        "seven_day": {"used_percentage": 55, "resets_at": NOW + 86400, "observed_at": NOW - 60},
    }))


def _settings_text(root: Path) -> str:
    return (root / "settings.json").read_text()


# -- tier resolution --------------------------------------------------------
@pytest.mark.parametrize("raw", ["Max 5x", "max 5x", "MAX5X", " max  5x "])
def test_tier_labels_normalize_to_display_form(raw: str) -> None:
    assert normalize_subscription_tier(raw) == "Max 5x"
    assert normalize_subscription_tier(None) is None


@pytest.mark.parametrize("raw", ["Max 10x", "", "Ultra", 5, True, "x" * 200])
def test_unknown_or_malformed_tier_labels_are_rejected(raw) -> None:
    with pytest.raises(ValueError, match="subscription_tier"):
        normalize_subscription_tier(raw)


def test_provider_detected_tier_wins_but_configured_label_is_preserved() -> None:
    fallback = resolve_subscription_tier("Max 5x")
    assert fallback["label"] == "Max 5x" and fallback["source"] == "configured"
    assert fallback["detection"]["available"] is False

    detected = resolve_subscription_tier("Max 5x", "max 20x")
    assert detected["label"] == "Max 20x" and detected["source"] == "provider"
    assert detected["configured"] == "Max 5x" and detected["mismatch"] is True

    # Malformed provider metadata never displaces the operator fallback.
    ignored = resolve_subscription_tier("Max 5x", "enterprise-ish")
    assert ignored["label"] == "Max 5x" and ignored["source"] == "configured"

    unset = resolve_subscription_tier(None)
    assert unset["label"] is None and unset["source"] == "unknown"


# -- read-only report -------------------------------------------------------
def test_report_when_off_does_not_read_usage_cache_or_write(tmp_path: Path, monkeypatch) -> None:
    _write_settings(tmp_path)
    _usage_cache(tmp_path)
    monkeypatch.setattr(
        state, "load_claude_rate_limits",
        lambda *a, **k: pytest.fail("hard-off report read the usage cache"),
    )
    before = sorted(path.name for path in tmp_path.iterdir())

    report = ClaudeAccountManager(tmp_path, clock=lambda: NOW).report()

    assert report["configured_mode"] == "off" and report["mode"] == "off"
    assert report["usage"]["read"] is False
    assert report["headroom"] == {"small_min_pct": 10, "big_min_pct": 15}
    assert report["subscription_tier"]["source"] == "unknown"
    assert report["status"] is None and "pool snapshot" in report["status_error"]
    assert sorted(path.name for path in tmp_path.iterdir()) == before


def test_report_exposes_mode_status_floors_tier_and_usage(tmp_path: Path) -> None:
    _write_settings(
        tmp_path, mode="opportunistic", subscription_tier="Max 5x",
        small_task_min_headroom_pct=12, big_task_min_headroom_pct=25,
    )
    _usage_cache(tmp_path)
    with Ledger(tmp_path) as data:
        data["providers"]["claude"]["status"] = "cooldown"
        data["providers"]["claude"]["last_error"] = "raw provider text"

    report = ClaudeAccountManager(tmp_path, clock=lambda: NOW).report()

    assert report["mode"] == "opportunistic" and report["mode_reason"] is None
    assert report["status"]["status"] == "cooldown"
    assert "last_error" not in report["status"]
    assert report["headroom"] == {"small_min_pct": 12, "big_min_pct": 25}
    assert report["subscription_tier"]["label"] == "Max 5x"
    assert report["subscription_tier"]["source"] == "configured"
    assert report["usage"]["pct_left"] == 45
    assert report["usage"]["fresh"] is True
    assert report["settings_digest"] == state_digest(tmp_path)


def test_report_raises_on_invalid_settings(tmp_path: Path) -> None:
    _write_settings(tmp_path)
    (tmp_path / "settings.json").write_text('{"schema":3,"providers":{"claude":{"mode":"always"}}}')
    with pytest.raises(ManagerError):
        ClaudeAccountManager(tmp_path).report()


def state_digest(root: Path) -> str:
    import hashlib

    return hashlib.sha256((root / "settings.json").read_bytes()).hexdigest()


# -- preview / commit -------------------------------------------------------
def test_preview_writes_nothing_and_commit_applies_pinned_edit(tmp_path: Path, monkeypatch) -> None:
    _human_terminal(monkeypatch)
    _write_settings(tmp_path)
    manager = ClaudeAccountManager(tmp_path, clock=lambda: NOW)
    original = _settings_text(tmp_path)

    preview = manager.preview(
        subscription_tier="max 5x", small_task_min_headroom_pct=8, big_task_min_headroom_pct=20,
    )
    assert _settings_text(tmp_path) == original
    assert preview.changed and not preview.enabling
    assert preview.expected_digest == state_digest(tmp_path)
    assert preview.after["subscription_tier"] == "Max 5x"
    with pytest.raises(TypeError):
        preview.changes["subscription_tier"] = "Pro"  # type: ignore[index]

    result = manager.commit(preview, from_current_user_request=True)

    assert result.changed and result.before_digest == preview.expected_digest
    assert result.after_digest == state_digest(tmp_path)
    written = json.loads(_settings_text(tmp_path))
    claude = written["providers"]["claude"]
    assert claude["subscription_tier"] == "Max 5x"
    assert (claude["small_task_min_headroom_pct"], claude["big_task_min_headroom_pct"]) == (8, 20)
    assert claude["mode"] == "off"
    assert written["future"] == {"keep": [1, 2]}
    assert list(tmp_path.glob("settings.json.backup-sha256-*"))
    settings, _ = load_settings(tmp_path)
    assert settings["providers"]["claude"]["subscription_tier"] == "Max 5x"


def test_mode_edit_marks_enabling_and_warns_when_expired(tmp_path: Path, monkeypatch) -> None:
    _human_terminal(monkeypatch)
    _write_settings(tmp_path, expires_at="2020-01-01T00:00:00Z")
    manager = ClaudeAccountManager(tmp_path)

    preview = manager.preview_mode("review_only")
    assert preview.enabling
    assert any("effectively off" in warning for warning in preview.warnings)
    manager.commit(preview, from_current_user_request=True)
    claude = json.loads(_settings_text(tmp_path))["providers"]["claude"]
    assert claude["mode"] == "review_only"
    assert claude["expires_at"] == "2020-01-01T00:00:00Z"


def test_unrelated_claude_edits_remain_possible_with_legacy_inverted_floors(
    tmp_path: Path,
) -> None:
    _write_settings(
        tmp_path, mode="opportunistic",
        small_task_min_headroom_pct=60, big_task_min_headroom_pct=20,
    )
    manager = ClaudeAccountManager(tmp_path)
    assert manager.preview_mode("off").changed
    assert manager.preview_subscription_tier("Max 5x").changed
    with pytest.raises(ManagerError, match="must not exceed"):
        manager.preview_headroom(small=61)


def test_turning_claude_off_warns_when_a_job_is_in_flight(tmp_path: Path) -> None:
    _write_settings(tmp_path, mode="opportunistic")
    with Ledger(tmp_path) as data:
        data["providers"]["claude"]["in_flight"] = {"label": "active job"}
    preview = ClaudeAccountManager(tmp_path).preview_mode("off")
    assert any("in flight" in warning for warning in preview.warnings)


def test_commit_rejects_stale_preview_without_writing(tmp_path: Path, monkeypatch) -> None:
    _human_terminal(monkeypatch)
    _write_settings(tmp_path)
    manager = ClaudeAccountManager(tmp_path)
    preview = manager.preview_subscription_tier("Max 5x")

    state.set_claude_mode(tmp_path, "manual_only")
    concurrent = _settings_text(tmp_path)

    with pytest.raises(StalePreviewError):
        manager.commit(preview, from_current_user_request=True)
    assert _settings_text(tmp_path) == concurrent


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"small_task_min_headroom_pct": 101}, "percentage"),
        ({"big_task_min_headroom_pct": -1}, "percentage"),
        ({"small_task_min_headroom_pct": True}, "percentage"),
        ({"small_task_min_headroom_pct": float("nan")}, "percentage"),
        ({"big_task_min_headroom_pct": "20"}, "percentage"),
        ({"small_task_min_headroom_pct": 30}, "must not exceed"),
        ({"small_task_min_headroom_pct": 40, "big_task_min_headroom_pct": 20}, "must not exceed"),
        ({"mode": "always"}, "mode must be one of"),
        ({"subscription_tier": "Max 10x"}, "subscription_tier"),
        ({"command": "/tmp/evil"}, "unsupported"),
        ({}, "non-empty"),
    ],
)
def test_invalid_edits_are_rejected_at_preview(tmp_path: Path, changes, message) -> None:
    _write_settings(tmp_path)
    with pytest.raises(ManagerError, match=message):
        ClaudeAccountManager(tmp_path).preview(**changes)


def test_commit_requires_confirmation_eligibility_and_open_manager(
    tmp_path: Path, monkeypatch
) -> None:
    _human_terminal(monkeypatch)
    _write_settings(tmp_path)
    manager = ClaudeAccountManager(tmp_path)
    preview = manager.preview_headroom(small=5)
    original = _settings_text(tmp_path)

    with pytest.raises(ManagerError, match="explicit confirmation"):
        manager.commit(preview, from_current_user_request=False)

    monkeypatch.setenv("CODEX_SEATS_MANAGED_WORKER", "1")
    with pytest.raises(ManagerEligibilityError):
        manager.commit(preview, from_current_user_request=True)
    monkeypatch.delenv("CODEX_SEATS_MANAGED_WORKER")

    monkeypatch.setenv("CLAUDE_LEAD_WORKER", "1")
    with pytest.raises(ManagerEligibilityError):
        manager.commit(preview, from_current_user_request=True)
    monkeypatch.delenv("CLAUDE_LEAD_WORKER")

    manager.close()
    with pytest.raises(ManagerClosedError):
        manager.commit(preview, from_current_user_request=True)
    assert _settings_text(tmp_path) == original


def test_missing_settings_are_never_created(tmp_path: Path) -> None:
    with pytest.raises(ManagerError, match="settings missing"):
        ClaudeAccountManager(tmp_path).preview_subscription_tier("Pro")
    assert not (tmp_path / "settings.json").exists()


# -- settings validation ----------------------------------------------------
def test_settings_tier_validation_is_shape_only(tmp_path: Path) -> None:
    # A well-formed label from a newer build stays loadable; reports ignore it.
    _write_settings(tmp_path, mode="manual_only", subscription_tier="Max 50x")
    settings, warnings = load_settings(tmp_path)
    assert settings["providers"]["claude"]["mode"] == "manual_only"
    assert not any("hard-off" in warning for warning in warnings)
    tier = ClaudeAccountManager(tmp_path).report()["subscription_tier"]
    assert tier["source"] == "unknown"

    for bad in (5, "", " Max 5x", "Max\n5x", "x" * 41):
        _write_settings(tmp_path, mode="manual_only", subscription_tier=bad)
        _settings, warnings = load_settings(tmp_path)
        assert any("subscription_tier" in warning for warning in warnings), bad


def test_known_tiers_fit_settings_shape() -> None:
    for tier in CLAUDE_SUBSCRIPTION_TIERS:
        assert len(tier) <= state.MAX_CLAUDE_SUBSCRIPTION_TIER_LENGTH
        assert normalize_subscription_tier(tier) == tier
