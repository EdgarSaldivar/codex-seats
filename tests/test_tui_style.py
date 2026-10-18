"""The shared terminal design system: colours, bars, chips, labels and times."""
from datetime import datetime

import pytest

from codex_seats import tui_style
from codex_seats.tui_style import (
    ANSI,
    TMUX,
    ago,
    age,
    clip,
    clock,
    countdown,
    day,
    display_width,
    duration,
    human,
    paint,
    render_parts,
    stamp,
    status_chip,
    strip_ansi,
    until,
    usage_bar,
    usage_role,
    window_label,
)


def _text(parts):
    return "".join(text for text, _role in parts)


@pytest.mark.parametrize(
    ("pct", "role"),
    [(0, "bad"), (19, "bad"), (19.9, "bad"), (20, "warn"), (49, "warn"), (50, "ok"), (100, "ok")],
)
def test_usage_thresholds_at_their_boundaries(pct, role):
    assert usage_role(pct) == role


def test_stale_or_unknown_usage_is_muted_whatever_its_value():
    assert usage_role(5, fresh=False) == "muted"
    assert usage_role(90, fresh=False) == "muted"
    assert usage_role(None) == "muted"
    assert {role for _text, role in usage_bar(90, 10, fresh=False)} <= {"muted", "track"}


@pytest.mark.parametrize(
    ("pct", "bar"),
    [
        (64, "██████▍░░░"),
        (92, "█████████▎"),
        (42, "████▎░░░░░"),
        (7, "▊░░░░░░░░░"),
        (68, "██████▊░░░"),
        (71, "███████▏░░"),
        (40, "████░░░░░░"),
        (0, "░░░░░░░░░░"),
        (100, "██████████"),
    ],
)
def test_bar_uses_eighth_block_partials_like_the_mockups(pct, bar):
    parts = usage_bar(pct, 10)
    assert _text(parts) == f"{bar} {pct:>3}%"
    assert display_width(_text(parts)) == 15


def test_bar_width_is_constant_and_unknown_is_a_dash():
    for cells in (4, 5, 6, 10, 12):
        for pct in (None, 0, 3, 49.5, 99.9, 100, 140, -5):
            assert display_width(_text(usage_bar(pct, cells))) == cells + 5
            assert display_width(_text(usage_bar(pct, cells, percent=False))) == cells
    assert _text(usage_bar(None, 5)).startswith("—")
    fill, track, percent = usage_bar(30, 10)
    assert fill[1] == percent[1] == "warn" and track[1] == "track"


def test_paint_is_explicit_and_stripping_restores_the_plain_text():
    assert paint("x", "ok", False) == "x"
    assert paint("x", None, True) == "x"
    painted = paint("x", "bad", True)
    assert painted == f"{ANSI['bad']}x{ANSI['reset']}"
    assert strip_ansi(painted) == "x"
    assert ANSI["selected"] == "\033[1;48;2;68;71;90m"
    assert ANSI["muted"] != "\033[38;2;98;114;164m"  # lightened for contrast
    for name in ("green", "cyan", "amber", "red", "gray", "purple"):
        assert TMUX[name].startswith("#") and name in ANSI


def test_render_parts_clips_to_width_and_colour_only_adds_escapes():
    parts = [("hello ", "ok"), ("界" * 10, "bad")]
    plain = render_parts(parts, 12, False)
    assert display_width(plain) <= 12 and plain.endswith("…")
    assert strip_ansi(render_parts(parts, 12, True)) == plain
    assert clip("a\nb\x1b[31m", 10) == "a b[31m"


@pytest.mark.parametrize(
    ("kind", "state", "ctx", "expected"),
    [
        ("codex", "ok", {"task": "Refactor dispatch"}, ("●", "Refactor dispatch", "live")),
        ("codex", "ok", {}, ("○", "idle", "ok")),
        ("codex", "ok", {"exhausted": "7d"}, ("◐", "capped 7d", "bad")),
        ("codex", "capped", {}, ("◐", "capped", "bad")),
        ("codex", "ok", {"stale": True}, ("◐", "stale", "warn")),
        ("codex", "needs-login", {}, ("×", "needs sign-in", "bad")),
        ("codex", "ok", {"auth_failure": True}, ("×", "needs sign-in", "bad")),
        ("codex", "unavailable", {}, ("×", "error", "bad")),
        ("codex", "ok", {"lifecycle": "inactive"}, ("–", "paused", "muted")),
        ("codex", "ok", {"lifecycle": "retired"}, ("–", "retired", "muted")),
        ("codex", "ok", {"lifecycle": "draining"}, ("◐", "draining", "warn")),
        ("claude", "ok", {"mode": "off"}, ("–", "off", "muted")),
        ("claude", "needs-auth", {}, ("×", "needs sign-in", "bad")),
        ("claude", "needs-login", {}, ("×", "needs sign-in", "bad")),
        ("claude", "missing", {}, ("×", "not installed", "bad")),
        ("claude", "cooldown", {}, ("◐", "cooldown", "warn")),
        ("claude", "ok", {"task": "review"}, ("●", "review", "live")),
        ("local", "working", {"task": "build"}, ("●", "build", "live")),
        ("local", "ready", {}, ("○", "ready", "ok")),
        ("local", "stale", {"age": "2.8h"}, ("◐", "stale 2.8h", "warn")),
        ("local", "model_mismatch", {}, ("×", "model mismatch", "bad")),
        ("local", "degraded", {}, ("×", "degraded", "bad")),
        ("local", "off", {}, ("–", "off", "muted")),
        ("work", "waiting_capacity", {}, ("◐", "Waiting for capacity", "warn")),
        ("lifecycle", "inactive", {}, ("–", "paused", "muted")),
    ],
)
def test_status_chip_vocabulary(kind, state, ctx, expected):
    chip = status_chip(kind, state, **ctx)
    assert tuple(chip) == expected
    assert chip.text == f"{expected[0]} {expected[1]}"
    assert chip.role in ANSI


def test_status_chip_never_raises_on_unknown_states():
    for kind in ("codex", "claude", "local", "work", "lifecycle", "other"):
        for state in (None, "", "brand_new_state"):
            chip = status_chip(kind, state)
            assert chip.glyph in {"●", "○", "◐", "×", "–"}


@pytest.mark.parametrize(
    ("field", "value", "label"),
    [
        ("claude_mode", "off", "Off"),
        ("claude_mode", "manual_only", "Manual only"),
        ("claude_mode", "review_only", "Reviews only"),
        ("claude_mode", "opportunistic", "Opportunistic"),
        ("role", "primary", "Primary"),
        ("role", "worker", "Worker"),
        ("role", "deputy", "Worker"),
        ("role", "D", "Worker"),
        ("review.mode", "advisory", "Advisory"),
        ("review.strategy", "cross", "Cross-provider"),
        ("execution.delegation_policy", "auto", "Automatic"),
        ("execution.head_permissions", "full-access", "Full access"),
        ("execution.head_permissions", "safe", "Safe"),
        ("capacity_profile", "business-premium", "Premium 5x"),
        ("capacity_profile", "personal-pro-20x", "Pro 20x"),
        ("capacity_profile", "unverified", "Unverified"),
        ("plan_type", "self_serve_business_prolite", "Business"),
        ("plan_type", "team", "Business"),
        ("plan_type", "plus", "Plus"),
        ("bank_status", "fresh", "Current"),
        ("window", "five_hour", "5-hour"),
        ("window", "primary", "5-hour"),
        ("window", 300, "5-hour"),
        ("window", "seven_day", "7-day"),
        ("window", 10080, "7-day"),
        ("reason", "lifecycle_ineligible", "Paused"),
        ("reason", "mode_off", "Off"),
        ("lifecycle", "inactive", "Paused"),
        ("enabled", True, "Enabled"),
        ("enabled", False, "Disabled"),
        ("anything", None, "—"),
        ("claude_mode", "brand_new_mode", "Brand new mode"),
        ("no_such_field", "some-raw_value", "Some raw value"),
    ],
)
def test_human_labels(field, value, label):
    assert human(field, value) == label


def test_window_label_derives_from_minutes_before_names():
    assert window_label("secondary", 300) == "5-hour"
    assert window_label("weekly", short=True) == "7d"
    assert window_label("five_hour", short=True) == "5h"
    assert window_label("monthly") == "30-day"
    assert window_label("odd_window") == "Odd window"


def test_relative_times():
    now = 1_000_000.0
    assert countdown(now + 2 * 3600 + 15 * 60, now) == "2h15m"
    assert countdown(now + 6 * 86400 + 4 * 3600, now) == "6d04h"
    assert countdown(now + 6 * 86400 + 4 * 3600, now, compact=True) == "6d"
    assert countdown(now + 45, now) == "1m"
    assert countdown(None, now) == "?"
    assert until(now + 2 * 3600 + 15 * 60, now) == "in 2h15m"
    assert until(now + 6 * 86400 + 4 * 3600, now) == "in 6d04h"
    assert until(now + 45, now) == "in 45s"
    assert until(now - 5, now) == "now"
    assert until("soon", now) == "—"
    assert ago(now - 12, now) == "12s ago"
    assert ago(now - 180, now) == "3m ago"
    assert ago(now - 2.8 * 3600, now) == "2.8h ago"
    assert ago(now - 3 * 86400 - 5, now) == "3d ago"
    assert ago(None, now) == "never"
    assert age(None) == "never"
    assert duration(65) == "1m05s" and duration(9) == "9s" and duration(7500) == "2h05m"


def test_absolute_times():
    now = datetime(2026, 10, 18, 9, 0).timestamp()
    same_year = datetime(2026, 10, 18, 14, 0).timestamp()
    last_year = datetime(2025, 10, 18, 14, 0).timestamp()
    assert stamp(same_year, now) == "Oct 18 14:00"
    assert stamp(last_year, now) == "Oct 18 2025"
    assert day(datetime(2026, 11, 2, 8, 0).timestamp(), now) == "Nov 2"
    assert day(last_year, now) == "Oct 18 2025"
    assert clock(datetime(2026, 10, 18, 6, 12).timestamp(), now) == "06:12"
    assert clock(datetime(2026, 10, 17, 6, 12).timestamp(), now) == "Oct 17"
    assert clock(0, now) == "--:--"
    assert stamp(None) == "unknown"


def test_width_helpers_are_the_terminal_ones():
    from codex_seats import terminal

    assert tui_style.display_width is terminal.display_width
    assert tui_style.fit_text is terminal.fit_text
    assert tui_style.wrap_lines is terminal.wrap_lines
    assert tui_style.clean_text is terminal.clean_text
