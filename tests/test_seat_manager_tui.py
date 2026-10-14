from __future__ import annotations

import argparse
import io
import json
import os
import pty
import re
import select
import signal
import subprocess
import sys
import termios
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from codex_seats import cli
from codex_seats.seat_admin import add_seat, set_seat
from codex_seats.seat_manager import ActionPreview, LoginPlan, SeatManager
from codex_seats.seat_manager_tui import (
    ADD_FIELDS,
    CAPACITY_CHOICES,
    FIELDS,
    GLOBAL_LABELS,
    KEY_BACKTAB,
    KEY_TAB,
    ManagerUI,
    UIExit,
    INVENTORY_GLOBAL_KEY,
)
from codex_seats.policy_settings import DEFAULTS
from codex_seats.terminal import (
    ALT_OFF,
    ALT_ON,
    TerminalSession,
    clean_text,
    decode_key,
    display_width,
    fit_text,
)
from codex_seats.state import (
    Ledger,
    load_settings,
    default_settings,
)


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class _FakeManager:
    def __init__(self) -> None:
        self.settings = default_settings()
        self.settings["pool"]["seats"] = {
            "seat2": {
                "label": "Second",
                "lifecycle": "inactive",
                "home": None,
                "capacity_profile": "unverified",
                "relative_capacity": None,
                "capacity_source": "unverified",
                "capacity_verified_at": None,
            },
            "seat1": {
                "label": "First\x1b[2J界",
                "lifecycle": "active",
                "home": None,
                "capacity_profile": "business-standard",
                "relative_capacity": 1.0,
                "capacity_source": "operator",
                "capacity_verified_at": None,
            },
        }
        self.settings["pool"]["primary"] = "seat1"
        self.pending: list[SimpleNamespace] = []
        self.identity = {
            "status": "unavailable",
            "email": None,
            "observed_plan_type": None,
            "account_id": None,
            "observed_at": None,
            "reason": "identity has not been refreshed",
        }

    def list_report(self, **_kwargs):
        return {
            "seats": [
                {
                    "id": seat_id,
                    "role": "primary" if seat_id == "seat1" else None,
                    "lifecycle": entry["lifecycle"],
                    "provider": {
                        "health": "ok",
                        "freshness": "fresh",
                        "binding_window": None,
                    },
                    "claim": {"live": seat_id == "seat1"},
                    "capacity": {
                        "profile": entry["capacity_profile"],
                        "relative_capacity": entry["relative_capacity"],
                        "effective_relative_capacity": 1.0,
                        "source": entry["capacity_source"],
                        "active_windows": [],
                        "observed_plan_type": None,
                        "credits": None,
                    },
                }
                for seat_id, entry in reversed(self.settings["pool"]["seats"].items())
            ]
        }

    def refresh_state(self):
        return None

    def identity_report(self, _selector):
        return dict(self.identity)

    def pending_drains(self):
        return tuple(self.pending)

    def reconcile_pending_drains(self):
        return tuple(self.pending)

    def preview_add(self, **_options):
        return replace(_preview(), action="add", seat_id="seat3")

    def preview_remove(self, seat_id):
        return replace(_preview(), action="retire", seat_id=seat_id)


def _frontier_ui(manager) -> ManagerUI:
    """Direct management lands on the inventory; one Tab reaches frontier seats."""
    ui = ManagerUI(manager, direct=True)  # type: ignore[arg-type]
    assert ui.screen == "inventory" and ui.view == "list"
    ui.handle(KEY_TAB)
    assert ui.screen == "seats" and ui.view == "list"
    return ui


@pytest.mark.parametrize("schema", [2, 3])
def test_global_settings_inventory_zero_seats_choice_cancel_and_stale(
    tmp_path, monkeypatch, schema
) -> None:
    settings = default_settings() if schema == 3 else default_settings()
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings) + "\n")
    path.chmod(0o600)
    with Ledger(tmp_path):
        pass
    manager = SeatManager(tmp_path, load_settings(tmp_path)[0])
    ui = ManagerUI(manager, direct=True)
    assert INVENTORY_GLOBAL_KEY in [row.key for row in ui._inventory_rows()]
    ui.handle("g")
    assert ui.view == "global-settings"
    from codex_seats.seat_manager_tui import GLOBAL_LABELS
    assert all(GLOBAL_LABELS[field] in ui.render(100, 24, "") for field in DEFAULTS)
    ui.handle("enter")
    assert ui.view == "global-choice"
    ui.handle("down")
    ui.handle("enter")
    assert ui.view == "confirm"
    material = "\n".join(ui._preview_material())
    assert "--- settings.before" in material
    assert "+++ settings.after" in material
    assert "BEFORE settings.before" not in material
    before = path.read_text()
    ui.handle("esc")
    assert path.read_text() == before
    ui.handle("enter")
    ui.handle("down")
    ui.handle("enter")
    assert ui.view == "confirm"
    # An independent settings update invalidates the digest-pinned preview.
    changed = json.loads(path.read_text())
    changed["future_metadata"] = {"revision": 2}
    path.write_text(json.dumps(changed) + "\n")
    monkeypatch.setenv("CODEX_SEATS_MANAGED_WORKER", "0")
    monkeypatch.setattr("codex_seats.seat_manager.ensure_management_eligible", lambda: None)
    ui.handle("y")
    assert ui.view == "message" and "stale" in ui.message
    assert json.loads(path.read_text())["future_metadata"] == {"revision": 2}


def test_global_settings_offer_six_controls_without_probe_mode() -> None:
    assert tuple(GLOBAL_LABELS) == tuple(DEFAULTS)
    assert set(DEFAULTS) == {
        "review.mode", "review.strategy", "execution.delegation_policy",
        "execution.inline_max_minutes", "execution.head_permissions",
        "execution.head_worker_fallback", "ui.theme", "ui.density", "ui.accent",
    }
    assert {key: DEFAULTS[key] for key in ("ui.theme", "ui.density", "ui.accent")} == {
        "ui.theme": "system", "ui.density": "comfortable", "ui.accent": "teal",
    }
    assert DEFAULTS["review.mode"] == "off"
    assert DEFAULTS["execution.inline_max_minutes"] == 5
    assert DEFAULTS["execution.head_worker_fallback"] is True
    assert "pool.probe_mode" not in DEFAULTS


def test_global_settings_number_and_boolean_commit_reload(tmp_path, monkeypatch) -> None:
    settings = default_settings()
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings) + "\n")
    path.chmod(0o600)
    with Ledger(tmp_path):
        pass
    manager = SeatManager(tmp_path, load_settings(tmp_path)[0], refresher=SimpleNamespace(settings=None))
    ui = ManagerUI(manager, direct=True)
    monkeypatch.setenv("CODEX_SEATS_MANAGED_WORKER", "0")
    monkeypatch.setattr("codex_seats.seat_manager.ensure_management_eligible", lambda: None)
    ui.handle("g")
    ui.global_index = tuple(DEFAULTS).index("execution.inline_max_minutes")
    ui.handle("enter")
    assert ui.view == "global-input"
    ui.input_buffer = "12"
    ui.handle("enter")
    ui.handle("y")
    assert manager.settings["execution"]["inline_max_minutes"] == 12
    assert manager.refresher.settings is manager.settings
    ui.handle("enter")
    ui.global_index = tuple(DEFAULTS).index("execution.head_worker_fallback")
    ui.handle("enter")
    ui.handle("down")
    ui.handle("enter")
    ui.handle("y")
    assert manager.settings["execution"]["head_worker_fallback"] is False
    ui.handle("enter")
    ui.global_index = tuple(DEFAULTS).index("ui.theme")
    ui.handle("enter")
    ui.handle("down")
    ui.handle("enter")
    ui.handle("y")
    assert manager.settings["ui"]["theme"] != "system"


def _preview(lines: int = 30) -> ActionPreview:
    before = json.dumps({"before": list(range(lines))}, indent=2) + "\n"
    after = json.dumps({"after": list(range(lines))}, indent=2) + "\n"
    return ActionPreview(
        action="set",
        seat_id="seat1",
        changed=True,
        before=before,
        after=after,
        warnings=(),
        expected_digest="digest",
        options=MappingProxyType({"label": "new"}),
    )


def test_key_decoder_and_unicode_width_are_terminal_safe() -> None:
    assert decode_key(b"\x1b[A") == "up"
    assert decode_key(b"\x1b") == "esc"
    assert decode_key(b"\x04") == "eof"
    assert decode_key("界".encode()) == "界"
    assert clean_text("label\x1b[2J\n") == "label?[2J?"
    assert display_width("a界e\u0301") == 4
    assert display_width(fit_text("a界", 4)) == 4
    assert "\x1b" not in fit_text("bad\x1b[H", 20)


def test_overview_preserves_dashboard_colors_and_columns() -> None:
    ui = ManagerUI(None)
    dashboard = "\x1b[1;36mCXWATCH\x1b[0m\n\x1b[32mseat1\x1b[0m  50%    idle"
    frame = ui.render(30, 8, dashboard)
    assert frame.splitlines()[:2] == dashboard.splitlines()
    assert "?[" not in frame
    assert re.sub(r"\x1b\[[0-9;]*m", "", frame).endswith("m manage · ? help · q quit")


def test_colored_dashboard_survives_interactive_wrapper(tmp_path) -> None:
    import re
    from codex_seats.display import dashboard

    with Ledger(tmp_path):
        pass
    styled = dashboard(
        tmp_path, default_settings(), [], color=True, width=80, height=23
    )
    frame = ManagerUI(None).render(80, 24, styled)
    assert "\x1b[" in styled
    assert frame.splitlines()[:-1] == styled.splitlines()
    visible = re.sub(r"\x1b\[[0-9;]*m", "", frame)
    assert "?[" not in visible
    assert all(display_width(line) <= 80 for line in visible.splitlines())


def test_long_label_does_not_hide_state_or_narrow_screen_exit_keys() -> None:
    manager = _FakeManager()
    manager.settings["pool"]["seats"]["seat1"]["label"] = "Long name " * 20
    frame = _frontier_ui(manager).render(40, 12, "")
    assert "seat1   active" in frame
    assert frame.splitlines()[-1].endswith("Esc/q")


def test_selection_is_stable_id_based_across_report_reordering() -> None:
    manager = _FakeManager()
    ui = _frontier_ui(manager)
    ui.render(80, 20, "overview")
    assert ui.selected_id == "seat1"

    ui.handle("down")
    assert ui.selected_id == "seat2"
    manager.settings["pool"]["seats"] = dict(
        reversed(manager.settings["pool"]["seats"].items())
    )
    rendered = ui.render(80, 20, "overview")

    assert ui.selected_id == "seat2"
    assert "> seat2" in rendered
    assert "\x1b" not in rendered


def test_main_screen_add_is_discoverable_for_empty_and_large_registries() -> None:
    manager = _FakeManager()
    manager.settings["pool"]["seats"] = {}
    empty = _frontier_ui(manager).render(60, 10, "")
    assert "No seats registered" in empty
    assert "a Add seat" in empty

    manager = _FakeManager()
    template = dict(manager.settings["pool"]["seats"]["seat2"])
    manager.settings["pool"]["seats"] = {
        f"seat{number}": {**template, "label": f"Seat {number}"}
        for number in range(1, 8)
    }
    large = _frontier_ui(manager).render(60, 10, "")
    assert "SEATS 7" in large
    assert "a Add seat" in large


def test_add_form_previews_confirms_and_selects_new_inactive_seat() -> None:
    manager = _FakeManager()
    commits = []
    manager.commit = lambda preview: (  # type: ignore[attr-defined]
        commits.append(preview) or SimpleNamespace(seat_id="seat3", changed=True)
    )
    ui = _frontier_ui(manager)

    ui.handle("a")
    assert ui.view == "add-form"
    assert ADD_FIELDS[-1].option == "submit"
    ui.add_field_index = len(ADD_FIELDS) - 1
    ui.handle("enter")
    assert ui.view == "confirm"
    assert ui.preview is not None and ui.preview.action == "add"
    assert "Action: add  Seat: seat3" in ui.render(80, 20, "")
    ui.handle("esc")
    assert ui.view == "add-form"
    assert commits == []
    ui.add_field_index = len(ADD_FIELDS) - 1
    ui.handle("enter")
    ui.handle("enter")

    assert commits and commits[0].seat_id == "seat3"
    assert ui.selected_id == "seat3"
    assert "configure settings" in ui.message
    assert "sign in" in ui.message
    assert "Reactivate" in ui.message


def test_add_form_plan_preset_and_printable_q_are_usable() -> None:
    ui = _frontier_ui(_FakeManager())
    ui.handle("a")
    ui.add_field_index = 0
    ui.handle("enter")
    ui.input_buffer = "Ac"
    assert ui.handle("q") is None
    assert ui.input_buffer == "Acq"
    ui.handle("enter")
    assert ui.add_buffers["label"] == "Acq"

    ui.add_field_index = 2
    ui.handle("enter")
    assert ui.view == "add-presets"
    ui.add_preset_index = 3
    chosen = ui.render(72, 10, "")
    assert "PLAN PRESET" in chosen
    ui.handle("enter")
    assert ui.add_buffers["profile"] == "personal-plus"


def test_narrow_add_form_keeps_values_and_editable_input_visible() -> None:
    ui = _frontier_ui(_FakeManager())
    ui.handle("a")
    frame = ui.render(24, 6, "")
    assert "Label: New seat" in frame
    assert "Home: <default>" in frame
    assert "Plan: unverified" in frame
    assert "Esc cancel" in frame.splitlines()[-1]
    ui.handle("enter")
    ui.input_buffer = "New account"
    frame = ui.render(24, 6, "")
    assert "> New account" in frame
    assert "Esc cancel" in frame.splitlines()[-1]
    assert len(frame.splitlines()) <= 6
    assert all(display_width(line) <= 24 for line in frame.splitlines())


def test_retired_seats_are_hidden_until_history_toggle_and_remove_is_labeled() -> None:
    manager = _FakeManager()
    manager.settings["pool"]["seats"]["seat2"]["lifecycle"] = "retired"
    ui = _frontier_ui(manager)
    ordinary = ui.render(100, 20, "")
    assert "SEATS 1" in ordinary
    assert "seat2" not in ordinary

    ui.handle("h")
    history = ui.render(100, 20, "")
    assert "SEATS 2" in history and "seat2" in history

    ui.show_history = False
    ui.selected_id = "seat1"
    ui.view = "actions"
    actions = ui.render(100, 20, "")
    assert "Remove seat (requires inactive, idle, non-primary/deputy)" in actions


def test_complete_preview_pages_instead_of_clipping_material() -> None:
    ui = ManagerUI(_FakeManager(), direct=True)  # type: ignore[arg-type]
    ui.selected_id = "seat1"
    ui.view = "confirm"
    ui.confirm_kind = "settings"
    ui.preview = _preview(50)

    first = ui.render(50, 8, "overview")
    assert "/" in first
    for _ in range(30):
        ui.handle("pagedown")
        last = ui.render(50, 8, "overview")

    assert f"/{len(ui._preview_material())}" not in first or last != first
    assert "Enter/y confirm" in last


def test_short_terminal_renders_bounded_resize_message() -> None:
    ui = ManagerUI(_FakeManager(), direct=True)  # type: ignore[arg-type]
    frame = ui.render(20, 4, "overview")
    assert len(frame.splitlines()) <= 4
    assert "Terminal too small" in frame


def test_short_terminal_blocks_hidden_confirmation_until_resized() -> None:
    manager = _FakeManager()
    commits: list[ActionPreview] = []
    manager.commit = lambda preview: (  # type: ignore[attr-defined]
        commits.append(preview) or SimpleNamespace(changed=True)
    )
    ui = ManagerUI(manager, direct=True)  # type: ignore[arg-type]
    ui.selected_id = "seat1"
    ui.view = "confirm"
    ui.confirm_kind = "settings"
    ui.preview = _preview()

    ui.render(20, 4, "overview")
    assert ui.handle("enter") is None
    assert commits == []
    assert ui.view == "confirm"

    ui.render(80, 8, "overview")
    ui.handle("enter")
    assert commits == [ui.preview]


def test_action_and_settings_windows_keep_dynamic_selection_visible() -> None:
    manager = _FakeManager()
    ui = ManagerUI(manager, direct=True)  # type: ignore[arg-type]
    ui.selected_id = "seat2"
    ui.view = "actions"
    ui.menu_index = 2  # login in the inactive-seat action set
    manager.settings["pool"]["seats"]["seat2"]["lifecycle"] = "retired"
    actions = ui.render(60, 6, "overview")
    assert ui.menu_index == 0
    assert "AVAILABLE ACTIONS" in actions

    ui.view = "settings"
    ui.field_index = len(FIELDS) - 1
    fields = ui.render(60, 6, "overview")
    assert "> configured plan and capacity" in fields


def test_selected_detail_can_be_paged_without_losing_selected_row() -> None:
    ui = _frontier_ui(_FakeManager())
    ui.render(60, 7, "overview")
    first = ui.render(60, 7, "overview")
    ui.handle("pagedown")
    second = ui.render(60, 7, "overview")

    assert "> seat1" in first and "> seat1" in second
    assert first != second


def test_missing_plan_windows_and_credits_are_explicitly_unavailable() -> None:
    ui = _frontier_ui(_FakeManager())
    seen = ""
    for _ in range(12):
        seen += "\n" + ui.render(56, 8, "overview")
        ui.handle("pagedown")
    assert "Observed account:" in seen and "Unavailable" in seen
    assert "Observed usage:" in seen
    assert "Windows:" in seen
    assert "Workspace credits:" in seen
    assert "Earned resets" not in seen
    assert "unlimited" not in seen.lower()


def test_identity_usage_and_credits_are_sanitized_and_scrollable() -> None:
    manager = _FakeManager()
    manager.identity = {
        "status": "fresh",
        "email": "seat@example.test\x1b[2J",
        "observed_plan_type": "business",
        "account_id": "account-456",
        "observed_at": time.time(),
        "reason": None,
    }
    manager.list_report = lambda **_kwargs: {
        "seats": [
            {
                "id": "seat1",
                "role": "primary",
                "lifecycle": "active",
                "provider": {"health": "ok", "observed_at": time.time()},
                "claim": {"live": False},
                "capacity": {
                    "profile": "business-standard",
                    "effective_relative_capacity": 1.0,
                    "relative_capacity": 1.0,
                    "source": "openai-published-baseline",
                    "observed_plan_type": "business",
                    "credits": {
                        "has_credits": True,
                        "unlimited": False,
                        "balance": "42.50",
                    },
                    "active_windows": [
                        {
                            "name": "5h",
                            "remaining_pct": 75,
                            "window_minutes": 300,
                            "resets_at": time.time() + 3600,
                        }
                    ],
                },
            }
        ]
    }
    ui = _frontier_ui(manager)
    frame = ui.render(48, 8, "overview")
    assert "\x1b" not in frame
    assert "> seat1" in frame
    seen = frame
    for _ in range(20):
        ui.handle("pagedown")
        frame = ui.render(48, 8, "overview")
        assert "> seat1" in frame
        assert len(frame.splitlines()) <= 8
        seen += "\n" + frame
    compact = re.sub(r" +", " ", seen)
    assert "Email: seat@example.test?[2J" in compact
    assert "Configured:" in seen
    assert "Observed account: business" in compact
    assert "5h: 75% remaining" in compact
    assert "Workspace balance: 42.50" in compact
    assert "Earned resets" not in compact
    assert "Workspace ID:" not in seen
    assert "Workspace:" not in seen
    assert "Account ID: account-456" in compact


def _plain_frame(frame: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", frame)


def test_management_layout_is_two_column_when_wide_and_stacked_when_narrow() -> None:
    ui = _frontier_ui(_FakeManager())

    wide = ui.render(100, 30, "overview")
    assert "── SEATS 2" in wide
    assert "│ ── SELECTED SEAT1" in wide
    assert all(name in wide for name in ("ACCOUNT", "PLAN", "USAGE", "CREDITS"))
    assert len(wide.splitlines()) == 30
    assert all(display_width(line) <= 100 for line in wide.splitlines())

    ui.detail_scroll = 0
    narrow = ui.render(60, 20, "overview")
    assert "── SEATS 2" in narrow
    assert "── SELECTED SEAT1" in narrow
    assert " │ " not in narrow
    assert len(narrow.splitlines()) == 20
    assert all(display_width(line) <= 60 for line in narrow.splitlines())


def test_management_color_highlight_and_untrusted_text_are_kept_separate() -> None:
    manager = _FakeManager()
    manager.identity["email"] = "evil\x1b[31m@example.test界"
    manager.identity["status"] = "fresh"
    manager.settings["pool"]["seats"]["seat1"]["label"] = "n\x1b]0;x\x07界"
    ui = _frontier_ui(manager)
    styled_overview = "\x1b[38;2;139;233;253moverview\x1b[0m"

    frame = ui.render(100, 30, styled_overview)
    plain = _plain_frame(frame)
    assert "\x1b[1;48;2;68;71;90m" in frame
    assert "\x1b[38;2;139;233;253m" in frame
    assert "\x1b" not in plain
    assert "evil?[31m@example.test界" in plain
    assert "n?]0;x?界" in plain
    assert all(display_width(line) <= 100 for line in plain.splitlines())

    unstyled = ui.render(100, 30, "overview")
    assert "\x1b" not in unstyled
    assert "> seat1" in unstyled


def test_minimum_supported_frame_keeps_selection_detail_and_exit_visible() -> None:
    frame = _frontier_ui(_FakeManager()).render(24, 6, "overview")
    assert len(frame.splitlines()) == 6
    assert "> seat1" in frame
    assert "SELECTED" in frame
    assert "a Add" in frame.splitlines()[-1]
    assert "h all" in frame.splitlines()[-1]
    assert "q quit" in frame.splitlines()[-1]
    assert all(display_width(line) <= 24 for line in frame.splitlines())


def test_confirmation_warning_is_sanitized_styled_and_reachable_by_paging() -> None:
    ui = ManagerUI(_FakeManager(), direct=True)  # type: ignore[arg-type]
    ui.selected_id = "seat1"
    ui.view = "confirm"
    ui.confirm_kind = "settings"
    ui.preview = replace(
        _preview(45),
        warnings=("Do not miss this warning \x1b[2J界",),
    )
    overview = "\x1b[32moverview\x1b[0m"
    pages: list[str] = []
    for _ in range(20):
        frame = ui.render(54, 9, overview)
        pages.append(_plain_frame(frame))
        assert all(display_width(line) <= 54 for line in pages[-1].splitlines())
        old_scroll = ui.scroll
        ui.handle("pagedown")
        ui.render(54, 9, overview)
        if ui.scroll == old_scroll:
            break
    seen = "\n".join(pages)
    assert "WARNING: Do not miss this warning ?[2J界" in seen
    assert "settings.before" in seen
    assert "settings.after" in seen
    assert "Enter/y confirm" in seen
    assert "\x1b" not in seen


@pytest.mark.parametrize(
    ("view", "title", "section", "footer_text"),
    [
        ("actions", "SEAT ACTIONS", "AVAILABLE ACTIONS", "Enter"),
        ("settings", "SEAT SETTINGS", "SUPPORTED FIELDS", "Enter edit"),
        ("capacity-presets", "CONFIGURED PLAN", "PRESETS", "Enter preview"),
    ],
)
def test_menu_screens_share_header_divider_selection_and_footer(
    view: str, title: str, section: str, footer_text: str
) -> None:
    manager = _FakeManager()
    manager.settings["pool"]["seats"]["seat1"]["label"] = (
        "Very long 界 setting \x1b[2J" * 8
    )
    ui = ManagerUI(manager, direct=True)  # type: ignore[arg-type]
    ui.selected_id = "seat1"
    ui.view = view
    overview = "\x1b[32moverview\x1b[0m"

    frame = ui.render(60, 10, overview)
    plain = _plain_frame(frame)
    assert title in plain.splitlines()[0]
    assert section in plain.splitlines()[1]
    assert footer_text in plain.splitlines()[-1]
    assert "\x1b[1;48;2;68;71;90m" in frame
    assert "\x1b" not in plain
    assert all(display_width(line) <= 60 for line in plain.splitlines())


def test_advanced_capacity_form_keeps_all_fields_at_minimum_height() -> None:
    ui = ManagerUI(_FakeManager(), direct=True)  # type: ignore[arg-type]
    ui.selected_id = "seat1"
    ui.view = "capacity-input"
    ui.capacity_buffers = {
        "profile": "personal-pro-20x",
        "relative_capacity": "20",
        "capacity_source": "operator-confirmed-界\x1b[2J",
    }
    frame = ui.render(60, 6, "overview")
    assert "profile:" in frame
    assert "relative capacity" in frame
    assert "source:" in frame
    assert "?[2J" in frame
    assert "Enter preview" in frame.splitlines()[-1]
    assert len(frame.splitlines()) == 6


def test_capacity_presets_cover_personal_business_and_keep_advanced_controls() -> None:
    manager = _FakeManager()
    calls = []
    manager.preview_set = lambda seat, **values: (  # type: ignore[attr-defined]
        calls.append((seat, values)) or _preview()
    )
    ui = ManagerUI(manager, direct=True)  # type: ignore[arg-type]
    ui.selected_id = "seat1"
    ui.view = "settings"
    ui.field_index = len(FIELDS) - 1
    ui.handle("enter")
    assert ui.view == "capacity-presets"
    labels = [choice["label"] for choice in CAPACITY_CHOICES]
    assert any("Plus 1x" in label for label in labels)
    assert any("Pro 20x" in label for label in labels)
    assert any("Standard 1x" in label for label in labels)
    assert any("Premium 5x (estimate)" in label for label in labels)

    ui.capacity_preset_index = next(
        index
        for index, choice in enumerate(CAPACITY_CHOICES)
        if choice.get("profile") == "personal-pro-20x"
    )
    ui.handle("enter")
    assert calls[-1][1] == {
        "profile": "personal-pro-20x",
        "relative_capacity": 20.0,
        "capacity_source": "openai-published-baseline",
    }

    ui.view = "capacity-presets"
    ui.capacity_preset_index = len(CAPACITY_CHOICES) - 1
    short_frame = ui.render(52, 6, "overview")
    assert "> Advanced / custom values" in short_frame
    ui.handle("enter")
    assert ui.view == "capacity-input"
    assert set(ui.capacity_buffers) == {
        "profile",
        "relative_capacity",
        "capacity_source",
    }


def test_quit_discloses_that_pending_drain_remains_draining() -> None:
    manager = _FakeManager()
    manager.pending = [SimpleNamespace(seat_id="seat2", state="draining")]
    ui = ManagerUI(manager, direct=True)  # type: ignore[arg-type]

    assert ui.handle("q") is None
    assert ui.view == "quit-confirm"
    frame = ui.render(80, 12, "overview")
    assert "remain draining" in frame
    assert ui.handle("enter") == UIExit("quit")


def test_quit_during_completed_activation_cancels_before_commit() -> None:
    manager = _FakeManager()
    manager.pending = [SimpleNamespace(seat_id="seat2", state="draining")]
    commits: list[object] = []
    manager.commit_activation = lambda *_args: commits.append(object())  # type: ignore[attr-defined]
    ui = ManagerUI(manager, direct=True)  # type: ignore[arg-type]
    ui.selected_id = "seat1"
    ui.view = "busy"
    ui.preview = _preview()
    ui.activation_thread = SimpleNamespace(is_alive=lambda: False)  # type: ignore[assignment]
    ui.activation_result = SimpleNamespace(
        passed=True, seat_id="seat1", detail="checks completed"
    )

    assert ui.handle("q") is None
    assert ui.view == "quit-confirm"
    assert ui.activation_cancel.is_set()
    ui.handle("esc")
    assert ui.view == "busy"
    ui.poll()

    assert commits == []
    assert "canceled" in ui.message.lower()


def test_terminal_session_real_pty_restores_termios_and_alternate_screen() -> None:
    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)
    stdin = os.fdopen(os.dup(slave), "rb", buffering=0)
    stdout = os.fdopen(os.dup(slave), "w", buffering=1)
    result: list[str | None] = []

    def run() -> None:
        with TerminalSession(stdin, stdout) as terminal:
            result.append(terminal.read_key(2))

    thread = threading.Thread(target=run)
    thread.start()
    assert select.select([master], [], [], 2)[0]
    output = os.read(master, 4096)
    os.write(master, b"\x1b[B")
    thread.join(timeout=3)
    assert not thread.is_alive()
    time.sleep(0.02)
    if select.select([master], [], [], 1)[0]:
        output += os.read(master, 4096)

    assert result == ["down"]
    assert ALT_ON.encode() in output
    assert ALT_OFF.encode() in output
    assert b"\x1b[?25l" in output
    assert b"\x1b[?25h" in output
    restored = termios.tcgetattr(slave)
    # macOS reports the kernel-maintained PENDIN bit after tcsetattr even when
    # every user-controlled input/output/control/local setting was restored.
    restored[3] &= ~getattr(termios, "PENDIN", 0)
    original[3] &= ~getattr(termios, "PENDIN", 0)
    assert restored == original
    stdin.close()
    stdout.close()
    os.close(master)
    os.close(slave)


def _write_schema2(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "settings.json"
    path.write_text(json.dumps(default_settings(), indent=2) + "\n")
    path.chmod(0o600)
    with Ledger(root):
        pass


def _write_schema3(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "settings.json"
    path.write_text(json.dumps(default_settings(), indent=2) + "\n")
    path.chmod(0o600)
    with Ledger(root):
        pass
    add_seat(root, label="First")
    add_seat(root, label="Second")


def _pty_cli(root: Path, *arguments: str, excluded_marker: str | None = None):
    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)
    environment = os.environ.copy()
    for name in (
        "CODEX_CONTROL_SEAT",
        "CODEX_SEATS_HEAD",
        "CODEX_SEATS_MANAGED_WORKER",
        "CODEX_THREAD_ID",
        "CODEX_HOME",
    ):
        environment.pop(name, None)
    if excluded_marker == "managed":
        environment["CODEX_SEATS_MANAGED_WORKER"] = "1"
    elif excluded_marker == "head":
        environment["CODEX_SEATS_HEAD"] = "codex"
    environment["CODEX_SEATS_ROOT"] = str(root)
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "lib")
    process = subprocess.Popen(
        [sys.executable, "-m", "codex_seats.cli", *arguments],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=environment,
        close_fds=True,
    )
    return process, master, slave, original


def _read_until(master: int, marker: bytes, timeout: float = 8) -> bytes:
    output = b""
    deadline = time.monotonic() + timeout
    while marker not in output and time.monotonic() < deadline:
        if select.select([master], [], [], 0.1)[0]:
            try:
                output += os.read(master, 65536)
            except OSError:
                break
    return output


def _wait_and_drain(
    process: subprocess.Popen, master: int, timeout: float = 10
) -> bytes:
    output = b""
    deadline = time.monotonic() + timeout
    while process.poll() is None and time.monotonic() < deadline:
        if select.select([master], [], [], 0.05)[0]:
            try:
                output += os.read(master, 65536)
            except OSError:
                break
    if process.poll() is None:
        raise subprocess.TimeoutExpired(process.args, timeout)
    return output


@pytest.mark.parametrize("ending", ["q", "ctrl-c", "eof", "sigterm", "sighup"])
def test_real_pty_watch_restores_terminal_on_quit_and_signal(
    tmp_path: Path, ending: str
) -> None:
    _write_schema2(tmp_path)
    process, master, slave, original = _pty_cli(
        tmp_path,
        "dash",
        "--watch",
        "--no-active-probe",
        "--interval",
        "0.05",
    )
    output = _read_until(master, ALT_ON.encode())
    assert ALT_ON.encode() in output
    if ending == "q":
        os.write(master, b"q")
    elif ending == "ctrl-c":
        # This subprocess is attached to a PTY but is not its controlling
        # foreground process group; deliver the same SIGINT a real Ctrl-C does.
        os.kill(process.pid, signal.SIGINT)
    elif ending == "eof":
        os.write(master, b"\x04")
    elif ending == "sighup":
        os.kill(process.pid, signal.SIGHUP)
    else:
        os.kill(process.pid, signal.SIGTERM)
    output += _wait_and_drain(process, master)
    output += _read_until(master, ALT_OFF.encode(), timeout=2)

    assert process.returncode == 0
    assert ALT_OFF.encode() in output
    restored = termios.tcgetattr(slave)
    restored[3] &= ~getattr(termios, "PENDIN", 0)
    original[3] &= ~getattr(termios, "PENDIN", 0)
    assert restored == original
    os.close(master)
    os.close(slave)


def test_real_pty_manage_selects_stable_id_and_opens_actions(tmp_path: Path) -> None:
    _write_schema3(tmp_path)
    process, master, slave, _original = _pty_cli(
        tmp_path,
        "dash",
        "--manage",
        "--no-active-probe",
        "--interval",
        "0.05",
    )
    output = _read_until(master, b"all registered identities")
    output += _read_until(master, b"seat2")
    assert b"INVENTORY" in output and b"seat1" in output and b"seat2" in output
    os.write(master, b"j\r")
    output += _read_until(master, b"ACTIONS")
    assert b"SEAT ACTIONS" in output and b"seat2" in output
    os.write(master, b"q")
    output += _wait_and_drain(process, master)

    assert process.returncode == 0
    assert ALT_OFF.encode() in output
    os.close(master)
    os.close(slave)


@pytest.mark.parametrize(
    ("excluded_marker", "message"),
    [("managed", b"managed workers"), ("head", b"controller/head")],
)
def test_manage_is_rejected_in_excluded_context_even_through_real_pty(
    tmp_path: Path, excluded_marker: str, message: bytes
) -> None:
    _write_schema2(tmp_path)
    process, master, slave, original = _pty_cli(
        tmp_path,
        "dash",
        "--manage",
        "--no-active-probe",
        excluded_marker=excluded_marker,
    )
    process.wait(timeout=10)
    output = _read_until(master, message, timeout=2)

    assert process.returncode == 2
    assert message in output
    assert ALT_ON.encode() not in output
    assert not (tmp_path / "readers").exists()
    assert termios.tcgetattr(slave) == original
    os.close(master)
    os.close(slave)


def test_explicit_manage_rejects_non_tty_without_rendering(monkeypatch) -> None:
    output = io.StringIO()
    error = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stderr", error)
    monkeypatch.setattr(cli, "codex_version", lambda: (None, None))
    args = argparse.Namespace(
        no_color=True,
        watch=False,
        manage=True,
        interval=5,
        probe_interval=300,
        no_active_probe=True,
        forensic=False,
    )

    assert cli.cmd_dash(args, default_settings(), []) == 2
    assert output.getvalue() == ""
    assert "real stdin/stdout terminal" in error.getvalue()


def test_mocked_login_handoff_quiesces_then_recreates_watcher(
    tmp_path: Path, monkeypatch
) -> None:
    events: list[str] = []
    settings = default_settings()
    current = {"value": settings}

    class FakeRefresher:
        count = 0

        def __init__(self, *_args, **kwargs):
            self.number = FakeRefresher.count
            FakeRefresher.count += 1
            self.settings = settings
            events.append(f"new:{self.number}:periodic={kwargs['periodic']}")

        def start(self):
            events.append(f"start:{self.number}")

        def stop(self, *, timeout):
            events.append(f"stop:{self.number}:{timeout:g}")
            return True

        def snapshot(self):
            return None

        def selected_snapshot(self):
            return None

    plan = LoginPlan(
        seat_id="seat1",
        home=tmp_path / "seat1",
        expected_digest="test-digest",
        settings=settings,
    )
    outcomes = iter([UIExit("login", "seat1", plan), UIExit("quit")])

    def fake_run_ui(_ui, **kwargs):
        kwargs["on_first_render"]()
        return next(outcomes)

    class Reader:
        def heartbeat(self):
            events.append("heartbeat")

        def load(self, _loader):
            events.append("reload")
            return current["value"], []

    args = argparse.Namespace(
        interval=0.1,
        probe_interval=300,
        no_active_probe=True,
        forensic=False,
        _settings_reader=Reader(),
    )
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr("codex_seats.watch.WatchRefresher", FakeRefresher)
    monkeypatch.setattr("codex_seats.terminal.run_ui", fake_run_ui)
    monkeypatch.setattr(
        "codex_seats.seat_manager.ensure_management_eligible", lambda: None
    )
    monkeypatch.setattr(
        "codex_seats.seat_manager.SeatManager.validate_login_plan",
        lambda _self, confirmed: confirmed.confirmed_settings(),
    )
    monkeypatch.setattr(
        "codex_seats.seats_cli._mutation_guard", lambda: events.append("guard")
    )
    monkeypatch.setattr(
        "codex_seats.seats_cli._trusted_terminal", lambda *_a: events.append("tty")
    )
    monkeypatch.setattr(
        "codex_seats.seats_cli._run_login",
        lambda *_a: events.append("login") or 0,
    )
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr(sys, "stdout", _Tty())

    assert (
        cli._run_interactive_dash(
            args, settings, [], version_warning=None, color=False, direct=True
        )
        == 0
    )

    assert events.index("stop:0:10") < events.index("login")
    assert events.index("login") < events.index("new:1:periodic=False")
    assert "reload" in events
    assert "start:1" in events


def test_login_handoff_rejects_home_change_during_quiescence(
    tmp_path: Path, monkeypatch
) -> None:
    for name in (
        "CODEX_CONTROL_SEAT",
        "CODEX_SEATS_HEAD",
        "CODEX_SEATS_MANAGED_WORKER",
        "CODEX_THREAD_ID",
        "CODEX_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    _write_schema3(tmp_path)
    settings, _warnings = load_settings(tmp_path)
    plan = SeatManager(tmp_path, settings).preview_login("seat1")
    events: list[str] = []

    class RacingRefresher:
        count = 0

        def __init__(self, *_args, **_kwargs):
            self.number = RacingRefresher.count
            RacingRefresher.count += 1
            self.settings = settings

        def start(self):
            events.append(f"start:{self.number}")

        def stop(self, *, timeout):
            events.append(f"stop:{self.number}:{timeout:g}")
            if self.number == 0:
                set_seat(
                    tmp_path,
                    "seat1",
                    home=str(tmp_path / "replacement-home"),
                )
            return True

        def snapshot(self):
            return None

        def selected_snapshot(self):
            return None

    outcomes = iter([UIExit("login", "seat1", plan), UIExit("quit")])

    def fake_run_ui(_ui, **kwargs):
        kwargs["on_first_render"]()
        return next(outcomes)

    args = argparse.Namespace(
        interval=0.1,
        probe_interval=300,
        no_active_probe=True,
        forensic=False,
    )
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr("codex_seats.watch.WatchRefresher", RacingRefresher)
    monkeypatch.setattr("codex_seats.terminal.run_ui", fake_run_ui)
    monkeypatch.setattr(
        "codex_seats.seat_manager.ensure_management_eligible", lambda: None
    )
    monkeypatch.setattr(
        "codex_seats.seats_cli._run_login",
        lambda *_args: events.append("login"),
    )
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr(sys, "stdout", _Tty())

    assert (
        cli._run_interactive_dash(
            args, settings, [], version_warning=None, color=False, direct=True
        )
        == 0
    )
    assert "login" not in events
    assert events.index("stop:0:10") < events.index("start:1")


# -- local hosts and seats ---------------------------------------------------
import copy  # noqa: E402
from dataclasses import replace as _replace  # noqa: E402

from codex_seats import dispatch_client, local_cli, local_seats  # noqa: E402
from codex_seats import state as state_module  # noqa: E402
from codex_seats.claude_manager import ClaudeAccountManager  # noqa: E402
from codex_seats.dispatch_protocol import DispatchError  # noqa: E402
from codex_seats.local_manager import LocalSeatManager  # noqa: E402
from codex_seats.seat_manager_tui import (  # noqa: E402
    CLAUDE_MODE_ORDER,
    CLAUDE_TIER_CLEAR_LABEL,
    HOST_FORM_FIELDS,
    LOCAL_MODE_ORDER,
    SCREENS,
    SEAT_FORM_FIELDS,
)
from codex_seats.watch import LocalProbeSnapshot  # noqa: E402

LOCAL_GUARD_VARS = (
    "CODEX_CONTROL_SEAT",
    "CODEX_SEATS_HEAD",
    "CODEX_SEATS_MANAGED_WORKER",
    "CODEX_THREAD_ID",
    "CODEX_HOME",
    "CODEX_SESSION_ID",
)


def _local_settings(*, schema: int = 3, mode: str = "opportunistic") -> dict:
    settings = default_settings() if schema == 3 else default_settings()
    settings["providers"]["local"]["mode"] = mode
    settings["local_hosts"] = {
        "studio": {
            "label": "Studio",
            "lifecycle": "active",
            "resource_groups": {"gpu0": {"max_concurrency": 1}},
            "base_url": "http://127.0.0.1:8080/v1",
            "lease_url": "http://127.0.0.1:8002",
            "availability_probe": {
                "argv": [sys.executable, "-c", 'print(\'{"state":"ready"}\')'],
                "timeout_seconds": 2,
            },
        }
    }
    seat = {
        "label": "Coder",
        "lifecycle": "active",
        "mode": "opportunistic",
        "host": "studio",
        "resource_group": "gpu0",
        "model_profile": "coder",
        "harness": "qwen",
        "capabilities": {"task_shapes": ["execute"], "validation": ["strong"]},
        "max_concurrency": 1,
        "runtime_fingerprint": "sha256:" + "a" * 64,
    }
    settings["local_seats"] = {"small": seat, "large": copy.deepcopy(seat)}
    return settings


def _write_local(root: Path, settings: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "settings.json"
    path.write_text(json.dumps(settings, indent=2) + "\n")
    path.chmod(0o600)


def _human_terminal(monkeypatch) -> None:
    """A real human TTY outside controller/head/worker contexts."""
    for name in LOCAL_GUARD_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)


class _LocalRefresher:
    def __init__(self, *, closed: bool = False, enabled: bool = True) -> None:
        self.requests: list[str] = []
        self.settings = None
        self.closed = closed
        self.snapshot = LocalProbeSnapshot(
            enabled=enabled,
            periodic=False,
            probing=None,
            pending=(),
            hosts={},
            generation=1,
            last_error=None,
        )

    def request_local_probe(self, host_id: str) -> None:
        if self.closed:
            raise RuntimeError("local probe service is closed")
        self.requests.append(host_id)
        self.snapshot = _replace(
            self.snapshot,
            pending=(*self.snapshot.pending, host_id),
            generation=self.snapshot.generation + 1,
        )

    def local_snapshot(self) -> LocalProbeSnapshot:
        return self.snapshot


class _LocalHostManager(_FakeManager):
    """Frontier fake plus real, digest-pinned local and Claude managers."""

    def __init__(self, root: Path, refresher, *, settings: dict | None = None) -> None:
        super().__init__()
        if settings is not None:
            self.settings = settings
        self.root = root
        self.local = LocalSeatManager(root, refresher=refresher)
        self.claude = ClaudeAccountManager(root)

    def local_manager(self):
        return self.local

    def claude_manager(self):
        return self.claude

    def reload(self):
        self.settings, _warnings = state_module.load_settings(self.root)
        return self.settings


def _local_ui(
    tmp_path: Path,
    *,
    settings: dict | None = None,
    refresher=None,
) -> tuple[ManagerUI, _LocalHostManager, _LocalRefresher | None]:
    settings = settings if settings is not None else _local_settings()
    _write_local(tmp_path, settings)
    refresher = _LocalRefresher() if refresher is None else refresher
    manager = _LocalHostManager(
        tmp_path,
        None if refresher == "none" else refresher,
        settings=settings if settings.get("schema") != 3 else None,
    )
    ui = ManagerUI(manager, direct=True)  # type: ignore[arg-type]
    # Direct management lands on the inventory; these flows start from the
    # frontier screen so one Tab reaches Local Hosts as before.
    ui.handle(KEY_TAB)
    assert ui.screen == "seats"
    return ui, manager, (None if refresher == "none" else refresher)


def _type(ui: ManagerUI, text: str) -> None:
    for character in text:
        ui.handle(character)


def _set_field(ui: ManagerUI, fields, option: str, text: str) -> None:
    """Select a form field by option, replace its text, and accept."""
    index = next(i for i, field in enumerate(ui.local_form_fields) if field.option == option)
    ui.local_form_index = index
    ui.handle("enter")
    assert ui.view == "local-form-input"
    while ui.input_buffer:
        ui.handle("backspace")
    _type(ui, text)
    ui.handle("enter")
    assert ui.view == "local-form"


def _choose(ui: ManagerUI, value) -> None:
    assert ui.view == "local-choice"
    ui.choice_index = next(
        index for index, (_label, item) in enumerate(ui.choice_items) if item == value
    )
    ui.handle("enter")


def _pages(ui: ManagerUI, width: int, height: int) -> str:
    seen = ""
    ui.detail_scroll = 0
    for _ in range(40):
        frame = ui.render(width, height, "")
        assert len(frame.splitlines()) <= height
        assert all(display_width(line) <= width for line in frame.splitlines())
        seen += "\n" + frame
        before = ui.detail_scroll
        ui.handle("pagedown")
        ui.render(width, height, "")
        if ui.detail_scroll == before:
            break
    return re.sub(r" +", " ", seen)


def _forbid_probes(monkeypatch) -> None:
    monkeypatch.setattr(
        local_seats, "probe_host", lambda *a, **k: pytest.fail("UI ran a probe inline")
    )


def test_tab_switches_screens_and_keeps_stable_selection_per_screen(tmp_path) -> None:
    ui, _manager, _refresher = _local_ui(tmp_path)
    assert SCREENS == ("inventory", "seats", "hosts", "local-seats", "claude")
    frontier = ui.render(100, 24, "")
    assert "SEAT MANAGER" in frontier and "Tab: local hosts" in frontier
    assert "Tab local" in frontier.splitlines()[-1]
    assert "> seat1" in frontier

    ui.handle(KEY_TAB)
    hosts = ui.render(100, 24, "")
    assert ui.screen == "hosts" and "LOCAL HOSTS" in hosts
    assert "> studio" in hosts and "Tab: local seats" in hosts

    ui.handle(KEY_TAB)
    assert ui.screen == "local-seats"
    ui.handle("j")
    assert ui.local_selected_id == "small"
    seats = ui.render(100, 24, "")
    assert "LOCAL SEATS" in seats and "> small" in seats and "large" in seats

    ui.handle(KEY_TAB)
    assert ui.screen == "claude" and ui.view == "list"
    assert "CLAUDE ACCOUNT" in ui.render(100, 24, "")
    ui.handle(KEY_TAB)
    assert ui.screen == "inventory" and ui.view == "list"
    assert "INVENTORY" in ui.render(100, 24, "")
    ui.handle(KEY_TAB)
    assert ui.screen == "seats" and ui.view == "list"
    assert "> seat1" in ui.render(100, 24, "")
    ui.handle(KEY_BACKTAB)
    assert ui.screen == "inventory"
    ui.handle(KEY_BACKTAB)
    ui.handle(KEY_BACKTAB)
    assert ui.screen == "local-seats" and ui.local_selected_id == "small"
    ui.handle("esc")
    assert ui.view == "overview"
    ui.handle("m")
    assert ui.screen == "inventory" and ui.view == "list"


def test_local_lists_and_details_show_config_availability_and_probe_state(
    tmp_path, monkeypatch
) -> None:
    _forbid_probes(monkeypatch)
    refresher = _LocalRefresher()
    refresher.snapshot = _replace(
        refresher.snapshot,
        hosts={
            "studio": {
                "state": "busy",
                "reason": "host_not_ready",
                "source": "requested",
                "finished_at": time.time() - 5,
                "detail": "GPU busy\x1b[2J界",
            }
        },
        last_error="worker hiccup\x1b[31m",
    )
    ui, _manager, _refresher = _local_ui(tmp_path, refresher=refresher)
    ui.handle(KEY_TAB)
    styled = "\x1b[32moverview\x1b[0m"
    wide = ui.render(100, 30, styled)
    plain = _plain_frame(wide)
    assert "\x1b[1;48;2;68;71;90m" in wide and "\x1b" not in plain
    assert all(display_width(line) <= 100 for line in plain.splitlines())
    assert "── HOSTS 1" in plain and "│ ── SELECTED STUDIO" in plain
    compact = re.sub(r" +", " ", plain)
    for fact in (
        "Host: studio · active · Studio",
        "Global mode: opportunistic",
        "Base URL: http://127.0.0.1:8080/v1",
        "Lease URL: http://127.0.0.1:8002",
        "Probe timeout: 2 s",
        "Resource groups: gpu0 ×1",
        "Seats: large, small",
        "Status: ? unknown · no probe observation",
        "Reason: availability_missing_or_invalid",
        "Observed: Not observed — no probe cached yet",
        "Live probe: busy · host_not_ready · requested",
        "5s ago",
        "Probe detail: GPU busy?[2J界",
        "Periodic: off (--no-active-probe)",
        "Probe now still",
        "Probe error: worker hiccup?[31m",
    ):
        assert fact in compact, fact
    # A never-probed host is unknown, never painted as a degraded probe failure.
    assert "> studio active unknown" in compact and "degraded" not in compact

    narrow = _pages(ui, 40, 12)
    assert "> studio" in narrow
    assert "Live probe: busy" in narrow and "Fingerprint" not in narrow

    ui.handle(KEY_TAB)
    seats = _pages(ui, 48, 10)
    assert "> large" in seats
    for fact in (
        "Seat: large · active · Coder",
        "Global: opportunistic",
        "Seat: opportunistic",
        "Effective: opportunistic",
        "Host: studio · active · Studio",
        "Resource group: gpu0 · max 1",
        "Concurrency: 1",
        "Model profile: coder",
        "Harness: qwen",
        "Task shapes: execute",
        "Validation: strong",
        "Fingerprint: sha256:aaaa",
        "Eligible: no",
        "availability_missing_or_invalid",
        "Live probe: busy",
    ):
        assert fact in seats, fact
    assert "\x1b" not in seats


def test_local_empty_registry_keeps_add_and_global_mode_discoverable(tmp_path) -> None:
    settings = _local_settings()
    settings["local_hosts"] = {}
    settings["local_seats"] = {}
    ui, _manager, _refresher = _local_ui(tmp_path, settings=settings)
    ui.handle(KEY_TAB)
    stacked = ui.render(60, 10, "")
    assert "No local hosts registered. Press a to Add host" in stacked
    assert "a Add" in stacked.splitlines()[-1]
    wide = ui.render(100, 12, "")
    assert "No hosts. Press a to Add host." in wide and "── HOSTS 0" in wide
    assert ui.handle("enter") is None and ui.view == "list"
    assert ui.handle("p") is None and ui.view == "list"

    ui.handle("g")
    assert ui.view == "local-choice"
    assert "GLOBAL LOCAL MODE" in ui.render(60, 10, "")
    ui.handle("esc")
    assert ui.view == "list"

    ui.handle(KEY_TAB)
    assert "No local seats registered. Press a to Add seat" in ui.render(60, 10, "")
    ui.handle("a")
    assert ui.view == "local-form"
    host_index = next(i for i, f in enumerate(ui.local_form_fields) if f.option == "host")
    ui.local_form_index = host_index
    ui.handle("enter")
    assert ui.view == "message" and "Add a local host first" in ui.message
    ui.handle("enter")
    assert ui.view == "local-form"


def test_local_off_stale_and_fresh_availability_are_explicit(tmp_path, monkeypatch) -> None:
    _forbid_probes(monkeypatch)
    ui, manager, _refresher = _local_ui(tmp_path, settings=_local_settings(mode="off"))
    ui.handle(KEY_TAB)
    hosts = re.sub(r" +", " ", ui.render(100, 26, ""))
    assert "Global mode: off" in hosts and "Reason: mode_off" in hosts
    assert "> studio active off" in hosts
    assert "Status: · off · global mode off" in hosts
    assert "Observed: Not read — local mode off, cache untouched" in hosts
    assert "degraded" not in hosts
    ui.handle(KEY_TAB)
    seats = _pages(ui, 100, 26)
    assert "Effective: off" in seats and "Eligible: no · mode_off" in seats
    assert "> large active off" in seats
    assert "Status: · off · global mode off" in seats and "degraded" not in seats

    settings = _local_settings()
    _write_local(tmp_path, settings)
    runtime = tmp_path / "local-runtime"
    runtime.mkdir(mode=0o700)
    host_digest = local_seats._host_digest(settings["local_hosts"]["studio"])

    def cache(observed_at: float) -> None:
        path = runtime / "availability-studio.json"
        path.write_text(
            json.dumps(
                {
                    "host": "studio",
                    "state": "ready",
                    "reason": "ready",
                    "observed_at": observed_at,
                    "config_digest": host_digest,
                    "detail": "GPU idle",
                }
            )
        )
        path.chmod(0o600)

    cache(time.time() - 120)
    ui._local_report_cache = None
    stale = _pages(ui, 100, 26)
    assert "Eligible: no · availability_stale" in stale
    # An expired cache entry is stale with its last known state and age, not
    # a degraded probe failure with no observation.
    assert "Status: ~ stale · last ready" in stale
    assert "Observed: 2m ago · expired, last known state ready" in stale
    assert "> large active stale" in stale and "degraded" not in stale
    ui.handle(KEY_BACKTAB)
    stale_hosts = re.sub(r" +", " ", ui.render(100, 26, ""))
    assert "> studio active stale" in stale_hosts
    assert "Status: ~ stale · last ready" in stale_hosts
    assert "Reason: availability_stale" in stale_hosts
    assert "Observed: 2m ago · expired, last known state ready" in stale_hosts
    assert "degraded" not in stale_hosts
    ui.handle(KEY_TAB)

    cache(time.time())
    ui._local_report_cache = None
    fresh = _pages(ui, 100, 26)
    assert "Status: ○ available · GPU idle" in fresh
    assert "Eligible: yes · ready" in fresh
    assert "Observed: 0s ago" in fresh
    assert "> large active ready" in fresh
    ui.handle(KEY_BACKTAB)
    assert "> studio active ready" in re.sub(r" +", " ", ui.render(100, 26, ""))
    assert not manager.local.closed


def _write_cache(root: Path, settings: dict, *, observed_at: float, digest: str | None = None, **extra) -> None:
    runtime = root / "local-runtime"
    runtime.mkdir(mode=0o700, exist_ok=True)
    path = runtime / "availability-studio.json"
    payload = {
        "host": "studio",
        "state": "ready",
        "reason": "ready",
        "observed_at": observed_at,
        "config_digest": digest or local_seats._host_digest(settings["local_hosts"]["studio"]),
        **extra,
    }
    path.write_text(json.dumps(payload))
    path.chmod(0o600)


def test_local_gated_seat_and_global_off_keep_last_observation_and_skip_cache_io(
    tmp_path, monkeypatch
) -> None:
    """A gated seat shows off with the host's last observation age; global off
    is off with no runtime cache I/O even though a stale cache exists."""
    _forbid_probes(monkeypatch)
    settings = _local_settings()
    settings["local_seats"]["large"]["mode"] = "off"
    ui, _manager, _refresher = _local_ui(tmp_path, settings=settings)
    _write_cache(tmp_path, settings, observed_at=time.time() - 300, detail="GPU idle")
    ui.handle(KEY_TAB)
    ui.handle(KEY_TAB)
    ui.local_selected["local-seats"] = "large"
    gated = _pages(ui, 100, 26)
    assert "> large active off" in gated and " small active stale" in gated
    assert "Status: · off · seat mode off" in gated
    assert "Eligible: no · mode_off" in gated
    assert "Observed: 5m ago" in gated and "degraded" not in gated
    ui.handle("j")
    small = _pages(ui, 100, 26)
    assert "Status: ~ stale · last ready" in small
    assert "Observed: 5m ago · expired, last known state ready" in small

    settings["providers"]["local"]["mode"] = "off"
    _write_local(tmp_path, settings)
    ui._local_report_cache = None

    def no_cache_io(*_args, **_kwargs):
        pytest.fail("global off must not touch the local runtime cache")

    monkeypatch.setattr(local_seats, "_runtime", no_cache_io)
    off = _pages(ui, 100, 26)
    assert "Status: · off · global mode off" in off and "Eligible: no · mode_off" in off
    assert "Observed: Not read — local mode off, cache untouched" in off
    assert "stale" not in off and "degraded" not in off
    ui.handle(KEY_BACKTAB)
    hosts = re.sub(r" +", " ", ui.render(100, 26, ""))
    assert "> studio active off" in hosts and "Status: · off · global mode off" in hosts
    assert "Reason: mode_off" in hosts and "degraded" not in hosts


def test_local_probe_states_render_distinctly_in_manager(tmp_path, monkeypatch) -> None:
    _forbid_probes(monkeypatch)
    settings = _local_settings()
    ui, _manager, _refresher = _local_ui(tmp_path, settings=settings)
    ui.handle(KEY_TAB)
    for state, word, label, extra in (
        ("gaming", "gaming", "◐ gaming", {"detail": "GPU busy with a game"}),
        ("busy", "busy", "◐ busy", {}),
        ("loading", "loading", "↻ loading", {}),
        ("asleep", "asleep", "○ asleep", {}),
        ("degraded", "degraded", "× degraded", {"reason": "probe_timeout"}),
        ("ready", "ready", "○ available", {"model_profile": "coder"}),
    ):
        _write_cache(tmp_path, settings, observed_at=time.time() - 2, state=state, **extra)
        ui._local_report_cache = None
        frame = re.sub(r" +", " ", ui.render(100, 26, ""))
        assert f"> studio active {word}" in frame, state
        assert f"Status: {label}" in frame, state
        assert "Observed: 2s ago" in frame, state
        if state == "gaming":
            assert "GPU busy with a game" in frame
        if state == "degraded":
            assert "Status: × degraded · probe_timeout" in frame
        if state == "ready":
            assert "Served model: coder" in frame
    ui.handle(KEY_TAB)
    seats = _pages(ui, 100, 26)
    assert "> large active ready" in seats and "Status: ○ available" in seats

    # A served model that does not match the seat's profile is a mismatch.
    _write_cache(tmp_path, settings, observed_at=time.time() - 2, model_profile="other")
    ui._local_report_cache = None
    mismatch = _pages(ui, 100, 26)
    assert "> large active mismatch" in mismatch
    assert "Status: × model mismatch · serving other" in mismatch


def test_local_working_status_comes_only_from_running_local_jobs(tmp_path, monkeypatch) -> None:
    _forbid_probes(monkeypatch)
    settings = _local_settings()
    ui, _manager, _refresher = _local_ui(tmp_path, settings=settings)
    _write_cache(tmp_path, settings, observed_at=time.time() - 1, detail="GPU idle")
    jobs = [
        {
            "request_id": "a" * 32,
            "state": "running",
            "provider": "local",
            "seat": "small",
            "label": "local live work\x1b[31m",
        },
        {"request_id": "b" * 32, "state": "queued", "provider": "local", "seat": "large"},
        {"request_id": "c" * 32, "state": "running", "provider": "codex", "seat": "seat1"},
    ]
    calls: list[tuple] = []

    def live_jobs(root, loaded, *, now=None):
        calls.append((root, now))
        return jobs

    monkeypatch.setattr(dispatch_client, "live_jobs", live_jobs)
    ui.handle(KEY_TAB)
    hosts = re.sub(r" +", " ", ui.render(100, 26, ""))
    assert calls and calls[0][0] == tmp_path
    assert "> studio active working" in hosts
    # The shared derivation strips control bytes from the job label.
    assert "Status: ● working · small: local live work[31m" in hosts
    assert "\x1b" not in hosts
    ui.handle(KEY_TAB)
    seats = _pages(ui, 100, 26)
    assert "> large active ready" in seats and " small active working" in seats
    ui.handle("j")
    small = _pages(ui, 100, 26)
    assert "Status: ● working · local live work[31m" in small
    assert "Observed: 1s ago" in small and "\x1b" not in small

    # A dispatch read failure never fabricates or hides a state; it is shown.
    def broken(root, loaded, *, now=None):
        raise DispatchError("queue unreadable\x1b[2J")

    monkeypatch.setattr(dispatch_client, "live_jobs", broken)
    ui._local_report_cache = None
    failed = _pages(ui, 100, 26)
    assert "working" not in failed
    assert "Status: ○ available · GPU idle" in failed
    assert "Live jobs: Unreadable · queue unreadable?[2J" in failed


def test_local_configuration_changed_is_unknown_not_a_probe_failure(tmp_path, monkeypatch) -> None:
    _forbid_probes(monkeypatch)
    settings = _local_settings()
    ui, _manager, _refresher = _local_ui(tmp_path, settings=settings)
    _write_cache(tmp_path, settings, observed_at=time.time(), digest="0" * 64)
    ui.handle(KEY_TAB)
    hosts = re.sub(r" +", " ", ui.render(100, 26, ""))
    assert "> studio active unknown" in hosts
    assert "Status: ? unknown · configuration changed" in hosts
    assert "Reason: configuration_changed" in hosts
    assert "Observed: Not observed — configuration changed" in hosts
    assert "degraded" not in hosts
    ui.handle(KEY_TAB)
    seats = _pages(ui, 100, 26)
    assert "> large active unknown" in seats
    assert "Eligible: no · configuration_changed" in seats
    assert "Status: ? unknown · configuration changed" in seats
    assert "degraded" not in seats


def test_probe_now_queues_on_refresher_and_never_runs_inline(tmp_path, monkeypatch) -> None:
    _human_terminal(monkeypatch)
    _forbid_probes(monkeypatch)
    ui, _manager, refresher = _local_ui(tmp_path)
    ui.handle(KEY_TAB)
    assert ui.poll() is True  # first snapshot observation marks the frame dirty
    assert ui.poll() is False

    ui.handle("p")
    assert refresher.requests == ["studio"]
    assert ui.view == "message" and "Probe queued for studio" in ui.message
    assert "refresher" in ui.message
    assert ui.poll() is True  # pending changed on the snapshot
    ui.handle("enter")
    assert ui.view == "list"
    assert "Live probe: Queued" in re.sub(r" +", " ", ui.render(100, 26, ""))

    ui.handle("enter")
    assert ui.view == "local-actions"
    actions = ui.render(80, 12, "")
    assert "> Probe now (read-only, runs on the refresher)" in actions
    ui.handle("enter")
    assert refresher.requests == ["studio", "studio"]
    ui.handle("enter")
    assert ui.view == "local-actions"

    ui.handle("esc")
    ui.handle(KEY_TAB)
    ui.handle("enter")
    ui.menu_index = next(
        i for i, (_label, action) in enumerate(ui._local_actions()) if action == "probe-host"
    )
    ui.handle("enter")
    assert refresher.requests == ["studio", "studio", "studio"]
    assert "Probe queued for studio" in ui.message

    closed_ui, _m, _r = _local_ui(tmp_path, refresher=_LocalRefresher(closed=True))
    closed_ui.handle(KEY_TAB)
    closed_ui.handle("p")
    assert "Cannot probe studio" in closed_ui.message and "closed" in closed_ui.message

    no_refresher_ui, _m, _r = _local_ui(tmp_path, refresher="none")
    no_refresher_ui.handle(KEY_TAB)
    no_refresher_ui.handle("p")
    assert "no active refresher" in no_refresher_ui.message
    no_refresher_ui.handle("enter")
    assert "Unavailable — no probe service" in no_refresher_ui.render(100, 26, "")


def test_global_and_seat_mode_preview_confirm_and_cancel(tmp_path, monkeypatch) -> None:
    _human_terminal(monkeypatch)
    _forbid_probes(monkeypatch)
    ui, _manager, _refresher = _local_ui(tmp_path)
    path = tmp_path / "settings.json"
    original = path.read_bytes()
    ui.handle(KEY_TAB)

    ui.handle("g")
    assert ui.view == "local-choice"
    assert ui.choice_items[ui.choice_index][1] == "opportunistic"
    assert [value for _label, value in ui.choice_items] == list(LOCAL_MODE_ORDER)
    _choose(ui, "shadow")
    assert ui.view == "confirm" and ui.confirm_kind == "local"
    seen = ""
    for _ in range(10):
        seen += "\n" + ui.render(70, 12, "")
        ui.handle("pagedown")
    assert "Local action: set-global-mode  Target: providers.local" in seen
    assert "settings.before" in seen and "settings.after" in seen
    assert '-      "mode": "opportunistic"' in seen
    assert '+      "mode": "shadow"' in seen
    assert "CONFIRM CHANGE  /  providers.local" in seen

    ui.handle("esc")
    assert ui.view == "list" and ui.local_preview is None
    assert path.read_bytes() == original

    ui.handle("g")
    _choose(ui, "shadow")
    ui.handle("enter")
    assert ui.view == "message" and ui.message.startswith("Applied set-global-mode")
    assert json.loads(path.read_text())["providers"]["local"]["mode"] == "shadow"
    assert ui.local_preview is None
    ui.handle("enter")
    assert ui.view == "list"
    assert "global mode shadow" in ui.render(80, 20, "")

    ui.handle(KEY_TAB)
    ui.handle("j")
    assert ui.local_selected_id == "small"
    ui.handle("enter")
    ui.menu_index = 0
    ui.handle("enter")
    assert ui.view == "local-choice" and "SEAT MODE" in ui.render(60, 10, "")
    _choose(ui, "off")
    assert "Local action: set-seat-mode  Target: seat small" in ui.render(70, 12, "")
    ui.handle("y")
    assert ui.message.startswith("Applied set-seat-mode on small")
    current = json.loads(path.read_text())
    assert current["local_seats"]["small"]["mode"] == "off"
    assert current["local_seats"]["large"]["mode"] == "opportunistic"
    ui.handle("enter")
    assert ui.view == "local-actions"
    ui.handle("esc")
    assert "> small      active    off" in ui.render(100, 26, "")


def test_local_lifecycle_actions_offer_transitions_and_surface_core_errors(
    tmp_path, monkeypatch
) -> None:
    _human_terminal(monkeypatch)
    ui, _manager, _refresher = _local_ui(tmp_path)
    path = tmp_path / "settings.json"
    ui.handle(KEY_TAB)
    ui.handle("enter")
    ui.menu_index = 1
    ui.handle("enter")
    assert ui.view == "local-choice" and "HOST LIFECYCLE" in ui.render(60, 10, "")
    assert [value for _label, value in ui.choice_items] == ["active", "draining", "inactive"]
    _choose(ui, "inactive")
    assert ui.view == "message"
    assert "Cannot preview" in ui.message and "non-active local host" in ui.message
    ui.handle("enter")
    assert ui.view == "local-actions"

    ui.handle("esc")
    ui.handle(KEY_TAB)
    for seat_id in ("large", "small"):
        ui.local_selected["local-seats"] = seat_id
        ui.handle("enter")
        ui.menu_index = 1
        ui.handle("enter")
        assert "SEAT LIFECYCLE" in ui.render(60, 10, "")
        _choose(ui, "inactive")
        frame = ui.render(80, 30, "")
        assert '+      "lifecycle": "inactive"' in frame
        assert '+      "mode": "off"' in frame
        ui.handle("y")
        assert ui.message.startswith(f"Applied set-seat-lifecycle on {seat_id}")
        ui.handle("enter")
        ui.handle("esc")
    current = json.loads(path.read_text())
    assert {seat["mode"] for seat in current["local_seats"].values()} == {"off"}

    ui.handle(KEY_BACKTAB)
    ui.handle("enter")
    ui.menu_index = 1
    ui.handle("enter")
    _choose(ui, "inactive")
    ui.handle("y")
    assert ui.message.startswith("Applied set-host-lifecycle on studio")
    ui.handle("enter")
    ui.menu_index = 1
    ui.handle("enter")
    assert [value for _label, value in ui.choice_items] == ["active", "inactive", "retired"]
    _choose(ui, "retired")
    ui.handle("y")
    assert json.loads(path.read_text())["local_hosts"]["studio"]["lifecycle"] == "retired"
    ui.handle("enter")
    ui.menu_index = 1
    ui.handle("enter")
    assert ui.view == "message" and "retired is terminal" in ui.message
    ui.handle("enter")
    ui.handle("esc")
    assert "> studio     retired" in ui.render(100, 26, "")


def test_add_host_form_validates_fields_and_commits_after_confirmation(
    tmp_path, monkeypatch
) -> None:
    _human_terminal(monkeypatch)
    ui, _manager, _refresher = _local_ui(tmp_path)
    path = tmp_path / "settings.json"
    ui.handle(KEY_TAB)
    ui.handle("a")
    assert ui.view == "local-form"
    form = ui.render(80, 14, "")
    assert "ADD LOCAL HOST" in form and "host id: <required>" in form
    assert "Preview Add host" in form
    assert [field.option for field in ui.local_form_fields] == [
        field.option for field in HOST_FORM_FIELDS
    ]

    _set_field(ui, HOST_FORM_FIELDS, "id", "other")
    _set_field(ui, HOST_FORM_FIELDS, "label", "Other box")
    _set_field(ui, HOST_FORM_FIELDS, "base_url", "http://user:pw@10.0.0.5/v1")
    ui.local_form_index = len(ui.local_form_fields) - 1
    ui.handle("enter")
    assert ui.view == "message"
    assert "Cannot preview add-host" in ui.message
    assert "base_url must be an HTTP(S) URL without credentials" in ui.message
    ui.handle("enter")
    assert ui.view == "local-form" and ui.local_form_buffers["id"] == "other"

    _set_field(ui, HOST_FORM_FIELDS, "base_url", "http://10.0.0.5:8000/v1")
    _set_field(ui, HOST_FORM_FIELDS, "resource_groups", "{")
    ui.local_form_index = len(ui.local_form_fields) - 1
    ui.handle("enter")
    assert "resource_groups is not valid JSON" in ui.message
    ui.handle("enter")
    _set_field(ui, HOST_FORM_FIELDS, "resource_groups", '{"gpu0": {"max_concurrency": 2}}')
    _set_field(
        ui,
        HOST_FORM_FIELDS,
        "availability_probe",
        json.dumps({"argv": ["/usr/local/bin/probe", "http://10.0.0.5:8002/status"], "timeout_seconds": 3}),
    )
    lifecycle_index = next(
        i for i, f in enumerate(ui.local_form_fields) if f.option == "lifecycle"
    )
    ui.local_form_index = lifecycle_index
    ui.handle("enter")
    assert ui.view == "local-choice" and "LIFECYCLE" in ui.render(60, 10, "")
    _choose(ui, "active")
    assert ui.view == "local-form" and ui.local_form_buffers["lifecycle"] == "active"

    ui.local_form_index = len(ui.local_form_fields) - 1
    ui.handle("enter")
    assert ui.view == "confirm"
    seen = ""
    for _ in range(12):
        seen += "\n" + ui.render(80, 12, "")
        ui.handle("pagedown")
    assert "Local action: add-host  Target: host other" in seen
    assert '+    "other": {' in seen and '+      "label": "Other box"' in seen
    assert "lease_url" not in seen.split("Local action")[1]
    ui.handle("n")
    assert ui.view == "local-form"
    assert "other" not in json.loads(path.read_text())["local_hosts"]

    ui.local_form_index = len(ui.local_form_fields) - 1
    ui.handle("enter")
    ui.handle("y")
    assert ui.view == "message" and "Added local host other" in ui.message
    ui.handle("enter")
    assert ui.screen == "hosts" and ui.local_selected_id == "other"
    host = json.loads(path.read_text())["local_hosts"]["other"]
    assert host == {
        "label": "Other box",
        "lifecycle": "active",
        "base_url": "http://10.0.0.5:8000/v1",
        "resource_groups": {"gpu0": {"max_concurrency": 2}},
        "availability_probe": {
            "argv": ["/usr/local/bin/probe", "http://10.0.0.5:8002/status"],
            "timeout_seconds": 3,
        },
    }
    assert "> other" in ui.render(100, 26, "")


def test_add_seat_form_uses_host_choices_and_fingerprint_validation(
    tmp_path, monkeypatch
) -> None:
    _human_terminal(monkeypatch)
    ui, _manager, _refresher = _local_ui(tmp_path)
    path = tmp_path / "settings.json"
    ui.handle(KEY_TAB)
    ui.handle("enter")
    ui.menu_index = next(
        i for i, (_l, action) in enumerate(ui._local_actions()) if action == "add-seat-here"
    )
    ui.handle("enter")
    assert ui.view == "local-form" and ui.local_form_action == "add-seat"
    assert ui.local_form_buffers["host"] == "studio"
    assert ui.local_form_buffers["resource_group"] == "gpu0"
    assert [field.option for field in ui.local_form_fields] == [
        field.option for field in SEAT_FORM_FIELDS
    ]
    form = ui.render(80, 16, "")
    assert "ADD LOCAL SEAT" in form and "seat id: <required>" in form
    assert 'capabilities (JSON): {"task_shapes": ["execute"]' in form

    _set_field(ui, SEAT_FORM_FIELDS, "id", "third")
    _set_field(ui, SEAT_FORM_FIELDS, "model_profile", "qwen3.8-27b")
    _set_field(ui, SEAT_FORM_FIELDS, "runtime_fingerprint", "sha256:zz")
    for option, value in (("lifecycle", "active"), ("mode", "canary")):
        ui.local_form_index = next(
            i for i, f in enumerate(ui.local_form_fields) if f.option == option
        )
        ui.handle("enter")
        _choose(ui, value)
    ui.local_form_index = next(
        i for i, f in enumerate(ui.local_form_fields) if f.option == "resource_group"
    )
    ui.handle("enter")
    assert ui.view == "local-choice"
    assert [value for _label, value in ui.choice_items] == ["gpu0"]
    _choose(ui, "gpu0")

    ui.local_form_index = len(ui.local_form_fields) - 1
    ui.handle("enter")
    assert ui.view == "message"
    assert "runtime_fingerprint must be sha256 followed by 64 lowercase hex" in ui.message
    ui.handle("enter")
    _set_field(ui, SEAT_FORM_FIELDS, "runtime_fingerprint", "sha256:" + "b" * 64)
    _set_field(ui, SEAT_FORM_FIELDS, "max_concurrency", "two")
    ui.local_form_index = len(ui.local_form_fields) - 1
    ui.handle("enter")
    assert "max_concurrency must be an integer" in ui.message
    ui.handle("enter")
    _set_field(ui, SEAT_FORM_FIELDS, "max_concurrency", "1")
    _set_field(ui, SEAT_FORM_FIELDS, "context_window_size", "180224")

    ui.local_form_index = len(ui.local_form_fields) - 1
    ui.handle("enter")
    assert ui.view == "confirm"
    frame = ui.render(90, 40, "")
    assert "Local action: add-seat  Target: seat third" in frame
    assert "less restrictive (enabling)" in frame
    assert '+      "mode": "canary"' in frame
    ui.handle("enter")
    assert "Added local seat third" in ui.message
    ui.handle("enter")
    assert ui.screen == "local-seats" and ui.local_selected_id == "third"
    seat = json.loads(path.read_text())["local_seats"]["third"]
    assert seat["model_profile"] == "qwen3.8-27b"
    assert seat["context_window_size"] == 180224
    assert seat["runtime_fingerprint"] == "sha256:" + "b" * 64
    assert seat["capabilities"] == {"task_shapes": ["execute"], "validation": ["strong"]}
    assert seat["lifecycle"] == "active" and seat["mode"] == "canary"
    detail = re.sub(r" +", " ", ui.render(100, 30, ""))
    # The list column is the shared status (never probed → unknown); the mode
    # section carries the effective mode.
    assert "> third active unknown" in detail
    assert "Effective: canary" in detail


def test_edit_forms_send_only_changed_fields_and_clear_optionals(tmp_path, monkeypatch) -> None:
    _human_terminal(monkeypatch)
    ui, manager, _refresher = _local_ui(tmp_path)
    path = tmp_path / "settings.json"
    previews: list[tuple[str, dict]] = []
    real_edit_host = manager.local.preview_edit_host
    real_edit_seat = manager.local.preview_edit_seat
    manager.local.preview_edit_host = lambda host_id, changes: (  # type: ignore[method-assign]
        previews.append(("host", dict(changes))) or real_edit_host(host_id, changes)
    )
    manager.local.preview_edit_seat = lambda seat_id, changes: (  # type: ignore[method-assign]
        previews.append(("seat", dict(changes))) or real_edit_seat(seat_id, changes)
    )
    ui.handle(KEY_TAB)
    ui.handle("enter")
    ui.menu_index = 2
    ui.handle("enter")
    assert ui.view == "local-form" and ui.local_form_action == "edit-host"
    assert "id" not in ui.local_form_buffers and "lifecycle" not in ui.local_form_buffers
    frame = ui.render(90, 14, "")
    assert "EDIT LOCAL HOST  /  studio" in frame and "Preview Edit host" in frame
    assert "label: Studio" in frame and "lease_url (blank = none): http://127.0.0.1:8002" in frame
    ui.local_form_index = len(ui.local_form_fields) - 1
    ui.handle("enter")
    assert ui.view == "message" and "No changes to preview" in ui.message
    ui.handle("enter")

    _set_field(ui, HOST_FORM_FIELDS, "label", "Renamed")
    _set_field(ui, HOST_FORM_FIELDS, "lease_url", "")
    ui.local_form_index = len(ui.local_form_fields) - 1
    ui.handle("enter")
    assert previews[-1] == ("host", {"label": "Renamed", "lease_url": None})
    assert ui.view == "confirm"
    frame = ui.render(90, 40, "")
    assert '-      "lease_url": "http://127.0.0.1:8002"' in frame
    assert '+      "label": "Renamed"' in frame
    ui.handle("y")
    assert ui.message.startswith("Applied edit-host on studio")
    ui.handle("enter")
    assert ui.view == "list"
    host = json.loads(path.read_text())["local_hosts"]["studio"]
    assert host["label"] == "Renamed" and "lease_url" not in host
    assert host["base_url"] == "http://127.0.0.1:8080/v1"

    ui.handle(KEY_TAB)
    ui.handle("enter")
    ui.menu_index = 2
    ui.handle("enter")
    assert ui.local_form_action == "edit-seat"
    assert "mode" not in ui.local_form_buffers
    _set_field(ui, SEAT_FORM_FIELDS, "max_concurrency", "2")
    ui.local_form_index = len(ui.local_form_fields) - 1
    ui.handle("enter")
    assert "must not exceed its resource group maximum" in ui.message
    ui.handle("enter")
    _set_field(ui, SEAT_FORM_FIELDS, "max_concurrency", "1")
    _set_field(ui, SEAT_FORM_FIELDS, "label", "Big coder")
    _set_field(ui, SEAT_FORM_FIELDS, "context_window_size", "32768")
    _set_field(ui, SEAT_FORM_FIELDS, "capabilities", '{"task_shapes": ["execute", "investigate"], "validation": ["strong"]}')
    ui.local_form_index = len(ui.local_form_fields) - 1
    ui.handle("enter")
    assert previews[-1] == (
        "seat",
        {
            "label": "Big coder",
            "context_window_size": 32768,
            "capabilities": {"task_shapes": ["execute", "investigate"], "validation": ["strong"]},
        },
    )
    ui.handle("enter")
    assert ui.message.startswith("Applied edit-seat on large")
    seat = json.loads(path.read_text())["local_seats"]["large"]
    assert seat["label"] == "Big coder" and seat["max_concurrency"] == 1
    assert seat["context_window_size"] == 32768
    assert seat["capabilities"]["task_shapes"] == ["execute", "investigate"]
    assert json.loads(path.read_text())["local_seats"]["small"]["label"] == "Coder"


def test_local_confirm_reports_stale_preview_without_writing(tmp_path, monkeypatch) -> None:
    _human_terminal(monkeypatch)
    ui, _manager, _refresher = _local_ui(tmp_path)
    path = tmp_path / "settings.json"
    ui.handle(KEY_TAB)
    ui.handle("g")
    _choose(ui, "shadow")
    assert ui.view == "confirm"

    local_cli.apply_action(
        tmp_path, "set-seat-mode", "large", mode="off", expected_digest=None
    )
    before = path.read_bytes()
    ui.handle("enter")
    assert ui.view == "message"
    assert "changed since this local action was previewed" in ui.message
    assert "re-preview" in ui.message
    assert path.read_bytes() == before
    assert json.loads(before)["providers"]["local"]["mode"] == "opportunistic"
    ui.handle("enter")
    assert ui.view == "list"
    ui.handle(KEY_TAB)
    assert "> large      active    off" in ui.render(100, 26, "")


@pytest.mark.parametrize(
    "environment",
    [
        {"CODEX_CONTROL_SEAT": "seat1"},
        {"CODEX_SEATS_HEAD": "claude"},
        {"CODEX_SEATS_HEAD": "codex"},
        {"CODEX_SEATS_MANAGED_WORKER": "1"},
    ],
)
def test_local_mutations_and_probes_are_refused_in_excluded_contexts(
    tmp_path, monkeypatch, environment
) -> None:
    _human_terminal(monkeypatch)
    _forbid_probes(monkeypatch)
    ui, _manager, refresher = _local_ui(tmp_path)
    path = tmp_path / "settings.json"
    original = path.read_bytes()
    ui.handle(KEY_TAB)
    ui.handle("g")
    _choose(ui, "shadow")
    assert ui.view == "confirm"
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    ui.handle("enter")
    assert ui.view == "message" and "refused" in ui.message
    assert path.read_bytes() == original
    ui.handle("enter")
    ui.handle("p")
    assert "Cannot probe studio" in ui.message and "refused" in ui.message
    assert refresher.requests == []
    # Reads stay available: the list still renders the registry.
    ui.handle("enter")
    assert "> studio" in ui.render(100, 26, "")


def test_local_writes_require_a_real_terminal(tmp_path, monkeypatch) -> None:
    _human_terminal(monkeypatch)
    ui, _manager, _refresher = _local_ui(tmp_path)
    path = tmp_path / "settings.json"
    original = path.read_bytes()
    ui.handle(KEY_TAB)
    ui.handle("g")
    _choose(ui, "shadow")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    ui.handle("y")
    assert ui.view == "message" and "terminal" in ui.message
    assert path.read_bytes() == original


def test_local_screens_keep_minimum_frame_and_sanitized_highlight(tmp_path) -> None:
    ui, _manager, _refresher = _local_ui(tmp_path)
    styled = "\x1b[32moverview\x1b[0m"
    ui.handle(KEY_TAB)

    def check(frame: str, *, footer: str) -> None:
        plain = _plain_frame(frame)
        assert len(plain.splitlines()) <= 6
        assert all(display_width(line) <= 24 for line in plain.splitlines())
        assert "\x1b" not in plain
        assert footer in plain.splitlines()[-1]

    check(ui.render(24, 6, styled), footer="q quit")
    assert "> studio" in ui.render(24, 6, styled)
    ui.handle("enter")
    check(ui.render(24, 6, styled), footer="Enter")
    ui.handle("enter")  # probe now → message
    check(ui.render(24, 6, styled), footer="Enter/Esc")
    ui.handle("enter")
    ui.menu_index = 1
    ui.handle("enter")
    assert ui.view == "local-choice"
    check(ui.render(24, 6, styled), footer="Esc")
    ui.handle("esc")
    ui.handle("esc")
    ui.handle("a")
    frame = ui.render(24, 6, styled)
    check(frame, footer="Esc cancel")
    assert "\x1b[1;48;2;68;71;90m" in frame
    ui.handle("enter")
    ui.input_buffer = "studio-two\x1b[2J界"
    frame = ui.render(24, 6, styled)
    check(frame, footer="Esc")
    assert "?[2J界" in _plain_frame(frame)

    ui.view = "confirm"
    ui.confirm_kind = "local"
    ui.local_preview = _manager.local.preview_global_mode("shadow")
    frame = ui.render(24, 6, styled)
    check(frame, footer="Enter/y")
    assert "CONFIRM" in frame


def _write_local_schema3(root: Path, *, mode: str) -> None:
    _write_local(root, _local_settings(mode=mode))
    with Ledger(root):
        pass


def test_real_pty_local_hosts_probe_now_works_without_active_probe(tmp_path: Path) -> None:
    _write_local_schema3(tmp_path, mode="opportunistic")
    process, master, slave, original = _pty_cli(
        tmp_path,
        "dash",
        "--manage",
        "--no-active-probe",
        "--interval",
        "0.05",
    )
    output = _read_until(master, b"INVENTORY")
    output += _read_until(master, b"local-seat")
    assert b"studio" in output and b"claude" in output
    os.write(master, b"\t")
    output += _read_until(master, b"SEAT MANAGER")
    os.write(master, b"\t")
    output += _read_until(master, b"LOCAL HOSTS")
    assert b"studio" in output
    os.write(master, b"p")
    output += _read_until(master, b"Probe queued for studio")
    os.write(master, b"\r")
    # The probe runs on the refresher worker even with --no-active-probe;
    # the list detail re-renders with its "requested" result.
    output += _read_until(master, b"requested", timeout=15)
    assert b"requested" in output
    os.write(master, b"\r")
    output += _read_until(master, b"HOST ACTIONS")
    assert b"Probe now" in output
    os.write(master, b"q")
    output += _wait_and_drain(process, master)

    assert process.returncode == 0
    assert ALT_OFF.encode() in output
    restored = termios.tcgetattr(slave)
    restored[3] &= ~getattr(termios, "PENDIN", 0)
    original[3] &= ~getattr(termios, "PENDIN", 0)
    assert restored == original
    cache = json.loads((tmp_path / "local-runtime" / "availability-studio.json").read_text())
    assert cache["state"] == "ready"
    os.close(master)
    os.close(slave)


def test_real_pty_local_global_mode_commits_only_after_confirmation(tmp_path: Path) -> None:
    _write_local_schema3(tmp_path, mode="off")
    path = tmp_path / "settings.json"
    original = path.read_bytes()
    process, master, slave, _original = _pty_cli(
        tmp_path,
        "dash",
        "--manage",
        "--no-active-probe",
        "--interval",
        "0.05",
    )
    output = _read_until(master, b"INVENTORY")
    os.write(master, b"\t")
    output += _read_until(master, b"SEAT MANAGER")
    os.write(master, b"\t")
    output += _read_until(master, b"LOCAL HOSTS")
    os.write(master, b"g")
    output += _read_until(master, b"GLOBAL LOCAL MODE")
    os.write(master, b"j\r")
    output += _read_until(master, b"CONFIRM CHANGE")
    assert b"set-global-mode" in output
    os.write(master, b"n")
    output += _read_until(master, b"LOCAL HOSTS")
    time.sleep(0.2)
    assert path.read_bytes() == original

    os.write(master, b"g")
    output += _read_until(master, b"GLOBAL LOCAL MODE")
    os.write(master, b"j\r")
    output += _read_until(master, b"CONFIRM CHANGE")
    os.write(master, b"y")
    output += _read_until(master, b"Applied set-global-mode")
    assert json.loads(path.read_text())["providers"]["local"]["mode"] == "shadow"
    os.write(master, b"q")
    output += _wait_and_drain(process, master)

    assert process.returncode == 0
    assert ALT_OFF.encode() in output
    os.close(master)
    os.close(slave)


# -- unified inventory, claude account, local removal -------------------------
import subprocess as _subprocess  # noqa: E402


def _forbid_provider_clients(monkeypatch) -> None:
    """No UI path may spawn a provider client (claude/codex binaries)."""
    for name in ("run", "Popen", "check_output", "check_call", "call"):
        monkeypatch.setattr(
            _subprocess, name, lambda *a, **k: pytest.fail("UI spawned a subprocess")
        )


def _inventory_ui(tmp_path: Path, monkeypatch, *, settings: dict | None = None):
    """Direct management with real local and Claude managers on the inventory."""
    _forbid_probes(monkeypatch)
    _forbid_provider_clients(monkeypatch)
    settings = settings if settings is not None else _local_settings()
    _write_local(tmp_path, settings)
    refresher = _LocalRefresher()
    manager = _LocalHostManager(tmp_path, refresher)
    ui = ManagerUI(manager, direct=True)  # type: ignore[arg-type]
    assert ui.screen == "inventory" and ui.view == "list"
    return ui, manager


def _select_inventory(ui: ManagerUI, key: str) -> None:
    ui.render(100, 30, "")
    keys = ui._inventory_keys()
    assert key in keys, keys
    while ui.inventory_selected != key:
        ui.handle("down")


def _return_to_inventory(ui: ManagerUI) -> None:
    """The dashboard's m key always lands on the inventory."""
    while ui.view not in {"list", "overview"}:
        ui.handle("esc")
    ui.handle("esc")
    assert ui.view == "overview"
    ui.handle("m")
    assert ui.screen == "inventory" and ui.view == "list"


def test_inventory_lists_every_provider_and_enter_opens_its_controls(
    tmp_path, monkeypatch
) -> None:
    settings = _local_settings()
    settings["providers"]["claude"]["subscription_tier"] = "Max 5x"
    settings["providers"]["claude"]["small_task_min_headroom_pct"] = 12
    settings["providers"]["claude"]["big_task_min_headroom_pct"] = 25
    ui, _manager = _inventory_ui(tmp_path, monkeypatch, settings=settings)

    wide = ui.render(110, 30, "")
    compact = re.sub(r" +", " ", wide)
    assert "INVENTORY / all seats" in compact
    assert "── INVENTORY 5" in wide
    assert "KIND ID STATE STATUS LABEL" in compact
    assert "> codex seat1 active working First?[2J界 · primary" in compact
    assert "codex seat2 inactive idle Second" in compact
    assert "claude account off none mode off · tier Max" in compact
    # The host is placement for local seats, never a seat row of its own.
    assert "local-host" not in wide and "local-seat studio" not in compact
    assert "local-seat small active" in compact and "Coder · host studio" in compact
    assert "local-seat large active" in compact
    assert compact.count("+ add") == 1
    assert "+ add seat Add seat (Codex, Cla" in compact
    assert "Enter open controls · a Add seat" in wide.splitlines()[-1]
    assert "\x1b" not in wide
    assert all(display_width(line) <= 110 for line in wide.splitlines())
    _select_inventory(ui, "claude:account")
    stacked = re.sub(r" +", " ", ui.render(83, 30, ""))
    assert " │ " not in stacked
    assert "> claude account off none mode off · tier Max 5x (configured)" in stacked
    claude_detail = _pages(ui, 120, 30)
    assert "Small-task minimum: 12% remaining" in claude_detail
    assert "Big-task minimum: 25% remaining" in claude_detail
    _select_inventory(ui, "add:seat")
    stacked = re.sub(r" +", " ", ui.render(83, 30, ""))
    assert "> + add seat Add seat (Codex, Claude, or Local)" in stacked
    prose = " ".join(stacked.split())
    assert "chooses the seat type" in prose and "registered hosts: studio" in prose
    assert "one Claude account" in prose and "second login" in prose

    # Enter on each row lands directly on that item's existing controls, and
    # Escape from those controls returns straight to the inventory with every
    # provider row and the prior selection intact.
    _select_inventory(ui, "local-seat:small")
    assert "SELECTED LOCAL-SEAT:SMALL" in ui.render(110, 30, "")
    ui.handle("enter")
    assert ui.screen == "local-seats" and ui.view == "local-actions"
    assert ui.local_selected_id == "small"
    actions = ui.render(110, 30, "")
    assert "LOCAL SEAT ACTIONS  /  small" in actions
    assert "Remove seat (requires lifecycle inactive and mode off; keeps retired history)" in actions
    assert "Manage this seat's host (Local Hosts)" in actions
    assert "Esc inventory" in actions.splitlines()[-1]
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.view == "list"
    assert ui.inventory_selected == "local-seat:small"
    back = re.sub(r" +", " ", ui.render(110, 30, ""))
    assert "> local-seat small" in back and "codex seat1" in back and "claude account" in back

    # The host stays reachable from the selected local seat's actions.
    ui.handle("enter")
    _choose_action(ui, "open-host")
    assert ui.screen == "hosts" and ui.view == "local-actions"
    assert ui.local_selected_id == "studio"
    assert "HOST ACTIONS  /  studio" in ui.render(110, 30, "")
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.inventory_selected == "local-seat:small"

    _select_inventory(ui, "claude:account")
    detail = re.sub(r" +", " ", ui.render(110, 30, ""))
    assert "SUBSCRIPTION TIER" in detail and "Tier: Max 5x" in detail
    ui.handle("enter")
    assert ui.screen == "claude" and ui.view == "claude-actions"
    menu = ui.render(110, 30, "")
    assert "CLAUDE ACCOUNT ACTIONS" in menu and "Esc inventory" in menu.splitlines()[-1]
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.inventory_selected == "claude:account"

    _select_inventory(ui, "codex:seat2")
    ui.handle("enter")
    assert ui.screen == "seats" and ui.view == "actions" and ui.selected_id == "seat2"
    frame = ui.render(110, 30, "")
    assert "SEAT ACTIONS  /  seat2" in frame and "Esc inventory" in frame.splitlines()[-1]
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.view == "list"
    assert ui.inventory_selected == "codex:seat2"
    back = re.sub(r" +", " ", ui.render(110, 30, ""))
    assert "> codex seat2" in back and "claude account" in back and "local-seat small" in back

    # Exactly one Add seat entry point: Enter on the row or a opens the type menu.
    _select_inventory(ui, "add:seat")
    ui.handle("enter")
    assert ui.screen == "inventory" and ui.view == "local-choice"
    menu = ui.render(80, 12, "")
    assert "ADD SEAT  /  choose the seat type" in menu and "SEAT TYPE" in menu
    assert [value for _label, value in ui.choice_items] == ["add:codex", "add:claude", "add:local"]
    assert "Esc back to inventory" in menu.splitlines()[-1]
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.view == "list"
    assert ui.inventory_selected == "add:seat"

    ui.handle("a")
    assert ui.view == "local-choice"
    _choose(ui, "add:codex")
    assert ui.screen == "seats" and ui.view == "add-form"
    frame = ui.render(80, 12, "")
    assert "ADD SEAT  /  new inactive Codex seat" in frame
    assert "Esc cancel (back to inventory)" in frame.splitlines()[-1]
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.view == "list"

    ui.handle("a")
    _choose(ui, "add:claude")
    assert ui.screen == "claude" and ui.view == "claude-actions"
    assert "Set mode (off / manual_only" in ui.render(100, 12, "")
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.view == "list"

    ui.handle("a")
    _choose(ui, "add:local")
    assert ui.screen == "local-seats" and ui.view == "local-form"
    assert ui.local_form_action == "add-seat"
    assert ui.local_form_buffers["host"] == "studio"
    assert "ADD LOCAL SEAT" in ui.render(110, 30, "")
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.view == "list"


def _choose_action(ui: ManagerUI, action: str) -> None:
    """Pick a local action by its stable id from the open actions menu."""
    assert ui.view == "local-actions"
    ui.menu_index = next(
        index for index, (_label, item) in enumerate(ui._local_actions()) if item == action
    )
    ui.handle("enter")


def test_inventory_shows_one_local_seat_row_for_one_host_and_one_seat(
    tmp_path, monkeypatch
) -> None:
    """The live registry shape: one host, one seat -> one local row, no host row."""
    settings = _local_settings()
    settings["local_seats"] = {"studio-qwen3-8-27b": settings["local_seats"]["small"]}
    settings["local_seats"]["studio-qwen3-8-27b"]["label"] = "Qwen3 8-27B"
    ui, _manager = _inventory_ui(tmp_path, monkeypatch, settings=settings)
    rows = ui._inventory_rows()
    assert [row.kind for row in rows] == ["codex", "codex", "claude", "local-seat", "add", "global"]
    assert [row.key for row in rows][3:] == ["local-seat:studio-qwen3-8-27b", "add:seat", INVENTORY_GLOBAL_KEY]
    assert sum(1 for row in rows if row.kind == "add") == 1
    frame = ui.render(120, 30, "")
    assert "── INVENTORY 4" in frame
    assert "local-host" not in frame and frame.count("+ add") == 1
    # The long local id widens the ID column instead of overflowing it, so the
    # STATE/STATUS columns stay aligned with the header for every row.
    lines = frame.splitlines()
    header_line = next(line for line in lines if "KIND" in line and "STATE" in line)
    seat_line = next(line for line in lines if "studio-qwen3-8-27b" in line)
    assert seat_line.index("active") == header_line.index("STATE")
    assert seat_line.index("unknown") == header_line.index("STATUS")
    stacked = re.sub(r" +", " ", ui.render(83, 30, ""))
    assert "local-seat studio-qwen3-8-27b active unknown Qwen3 8-27B · host studio" in stacked
    assert stacked.count("+ add") == 1
    # The host is still one Tab away and named in the seat's placement detail.
    _select_inventory(ui, "local-seat:studio-qwen3-8-27b")
    assert "Host: studio · active · Studio" in re.sub(r" +", " ", _pages(ui, 120, 30))
    ui.handle(KEY_TAB)
    ui.handle(KEY_TAB)
    assert ui.screen == "hosts" and "> studio" in ui.render(100, 24, "")


def test_add_seat_type_menu_offers_host_first_path_when_no_host_is_registered(
    tmp_path, monkeypatch
) -> None:
    settings = _local_settings()
    settings["local_hosts"] = {}
    settings["local_seats"] = {}
    ui, _manager = _inventory_ui(tmp_path, monkeypatch, settings=settings)
    assert [row.kind for row in ui._inventory_rows()] == ["codex", "codex", "claude", "add", "global"]
    _select_inventory(ui, "add:seat")
    assert "no host is registered yet" in " ".join(ui.render(83, 30, "").split())
    ui.handle("a")
    _choose(ui, "add:local")
    assert ui.screen == "inventory" and ui.view == "local-choice"
    menu = ui.render(90, 12, "")
    assert "ADD LOCAL SEAT  /  no local host is registered yet" in menu
    assert "SEAT TYPE" not in menu and "Esc back to seat type" in menu.splitlines()[-1]
    assert [value for _label, value in ui.choice_items] == ["add:host", "hosts"]
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.view == "local-choice"
    assert ui.choice_purpose == "inventory-add" and ui.choice_index == 2
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.view == "list"

    ui.handle("a")
    _choose(ui, "add:local")
    _choose(ui, "hosts")
    assert ui.screen == "hosts" and ui.view == "list"
    ui.handle("a")
    assert ui.view == "local-form" and not ui.opened_from_inventory
    ui.handle("esc")
    assert ui.screen == "hosts" and ui.view == "list"
    ui.handle("esc")
    assert ui.view == "overview"

    ui.handle("m")
    ui.handle("a")
    _choose(ui, "add:local")
    _choose(ui, "hosts")
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.view == "list"

    ui.handle("a")
    _choose(ui, "add:local")
    _choose(ui, "add:host")
    assert ui.screen == "hosts" and ui.view == "local-form"
    assert ui.local_form_action == "add-host"
    assert "ADD LOCAL HOST" in ui.render(110, 30, "")
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.view == "list"


def test_tab_entered_provider_lists_keep_overview_escape(tmp_path, monkeypatch) -> None:
    ui, _manager = _inventory_ui(tmp_path, monkeypatch)
    ui.handle(KEY_TAB)
    assert ui.screen == "seats" and not ui.opened_from_inventory
    ui.handle("enter")
    assert ui.view == "actions"
    assert "Esc inventory" not in ui.render(100, 24, "").splitlines()[-1]
    ui.handle("esc")
    assert ui.screen == "seats" and ui.view == "list"
    ui.handle("esc")
    assert ui.view == "overview"

    ui.handle("m")
    _select_inventory(ui, "codex:seat1")
    ui.handle("enter")
    assert ui.opened_from_inventory
    # Tab away from controls opened from the inventory clears the return path.
    ui.handle("esc")
    ui.handle(KEY_TAB)
    ui.handle(KEY_TAB)
    assert ui.screen == "hosts" and not ui.opened_from_inventory
    ui.handle("enter")
    ui.handle("esc")
    assert ui.screen == "hosts" and ui.view == "list"
    ui.handle(KEY_TAB)
    ui.handle(KEY_TAB)
    assert ui.screen == "claude" and ui.view == "list"
    ui.handle("esc")
    assert ui.view == "overview"


def test_inventory_entered_provider_list_keeps_new_actions_on_that_list(
    tmp_path, monkeypatch
) -> None:
    ui, _manager = _inventory_ui(tmp_path, monkeypatch)
    ui._show_screen("hosts", from_inventory=True)
    assert ui.opened_from_inventory and ui.view == "list"
    ui.handle("enter")
    assert ui.view == "local-actions" and not ui.opened_from_inventory
    assert "Esc inventory" not in ui.render(100, 24, "").splitlines()[-1]
    ui.handle("esc")
    assert ui.screen == "hosts" and ui.view == "list"


def test_inventory_hides_retired_seats_until_history_and_paces_detail(
    tmp_path, monkeypatch
) -> None:
    settings = _local_settings()
    settings["local_seats"]["large"]["lifecycle"] = "retired"
    settings["local_seats"]["large"]["mode"] = "off"
    ui, manager = _inventory_ui(tmp_path, monkeypatch, settings=settings)
    manager.settings["pool"]["seats"]["seat2"]["lifecycle"] = "retired"

    ordinary = ui.render(100, 24, "")
    # seat1, the Claude account, and local seat small; the host is not a seat.
    assert "── INVENTORY 3" in ordinary
    assert "large" not in ordinary and "seat2" not in ordinary
    ui.handle("h")
    history = ui.render(100, 24, "")
    assert "── INVENTORY 5" in history
    assert "local-seat large      retired" in history and "seat2" in history

    # m from the dashboard lands on the inventory even from another screen.
    ui.handle(KEY_TAB)
    ui.handle(KEY_TAB)
    assert ui.screen == "hosts"
    ui.handle("esc")
    ui.handle("m")
    assert ui.screen == "inventory" and ui.view == "list"
    overview_ui = ManagerUI(manager)  # type: ignore[arg-type]
    assert overview_ui.view == "overview"
    overview_ui.handle("m")
    assert overview_ui.screen == "inventory" and overview_ui.view == "list"

    _select_inventory(ui, "local-seat:small")
    seen = _pages(ui, 60, 12)
    assert "── SELECTED LOCAL-SEAT:SMALL" in seen
    assert "Host: studio" in seen and "Fingerprint:" in seen
    _select_inventory(ui, "codex:seat1")
    seen = _pages(ui, 60, 12)
    assert "Seat: seat1 · active · primary" in seen and "CREDITS" in seen


def test_claude_screen_shows_account_and_edits_through_previews(
    tmp_path, monkeypatch
) -> None:
    _human_terminal(monkeypatch)
    settings = _local_settings()
    settings["providers"]["claude"]["subscription_tier"] = "Max 5x"
    settings["providers"]["claude"]["small_task_min_headroom_pct"] = 12
    settings["providers"]["claude"]["big_task_min_headroom_pct"] = 25
    ui, _manager = _inventory_ui(tmp_path, monkeypatch, settings=settings)
    with Ledger(tmp_path) as data:
        data["providers"]["claude"]["status"] = "cooldown"
        data["providers"]["claude"]["last_error"] = "raw provider text"
    path = tmp_path / "settings.json"
    original = path.read_bytes()
    passive_cache = state_module.load_claude_rate_limits
    monkeypatch.setattr(
        state_module, "load_claude_rate_limits",
        lambda *a, **k: pytest.fail("the UI read the usage cache while Claude is off"),
    )

    # Enter on the inventory row opens the account's controls directly and
    # Escape returns to the inventory; the full-screen account detail is the
    # Claude screen reached by Tab / Shift-Tab.
    _select_inventory(ui, "claude:account")
    ui.handle("enter")
    assert ui.screen == "claude" and ui.view == "claude-actions"
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.inventory_selected == "claude:account"
    ui.handle(KEY_BACKTAB)
    assert ui.screen == "claude" and ui.view == "list"
    seen = re.sub(r" +", " ", _pages(ui, 100, 12))
    assert "CLAUDE ACCOUNT / providers.claude" in seen
    assert "Account: claude · singleton subscription account · off" in seen
    assert "Provider: cooldown" in seen and "raw provider text" not in seen
    assert "Configured: off" in seen and "Effective: off" in seen
    assert "Tier: Max 5x" in seen and "Source: configured · operator-declared label" in seen
    assert "Detection: Unavailable" in seen
    assert "Small-task minimum: 12% remaining" in seen
    assert "Big-task minimum: 25% remaining" in seen
    assert "Usage: Claude is effectively off; usage cache not read" in seen
    assert "Enter edit mode / tier / headroom" in seen

    ui.handle("enter")
    assert ui.view == "claude-actions"
    menu = ui.render(100, 12, "")
    assert "CLAUDE ACCOUNT ACTIONS  /  providers.claude" in menu
    assert "Set mode (off / manual_only / review_only / opportunistic)" in menu
    assert "Set subscription tier label (Pro, Max 5x, Max 20x" in menu
    assert "Set small-task minimum headroom percent" in menu
    assert "Set big-task minimum headroom percent" in menu

    # Mode: preview, cancel without writing, then confirm.
    ui.menu_index = 0
    ui.handle("enter")
    assert ui.view == "local-choice" and "CLAUDE MODE" in ui.render(80, 10, "")
    assert [value for _label, value in ui.choice_items] == list(CLAUDE_MODE_ORDER)
    assert ui.choice_items[ui.choice_index] == ("off (current)", "off")
    _choose(ui, "review_only")
    assert ui.view == "confirm" and ui.confirm_kind == "claude"
    confirm = ""
    for _ in range(8):
        confirm += "\n" + ui.render(80, 12, "")
        ui.handle("pagedown")
    assert "CONFIRM CHANGE  /  providers.claude" in confirm
    assert "Claude account edit: mode = review_only  Target: providers.claude" in confirm
    assert "turns Claude on" in confirm and "no credentials are read" in confirm
    assert "providers.claude.before" in confirm and "providers.claude.after" in confirm
    assert '-  "mode": "off"' in confirm and '+  "mode": "review_only"' in confirm
    ui.handle("esc")
    assert ui.view == "claude-actions" and ui.claude_preview is None
    assert path.read_bytes() == original

    ui.handle("enter")
    _choose(ui, "review_only")
    ui.handle("y")
    # Once Claude is on, the report may read the passive usage cache (a file),
    # never the provider client.
    monkeypatch.setattr(state_module, "load_claude_rate_limits", passive_cache)
    assert ui.view == "message"
    assert ui.message.startswith("Applied Claude account edit (mode = review_only)")
    assert "configured mode now permits routing" in ui.message
    assert _manager.settings["providers"]["claude"]["mode"] == "review_only"
    written = json.loads(path.read_text())
    assert written["providers"]["claude"]["mode"] == "review_only"
    assert written["providers"]["claude"]["subscription_tier"] == "Max 5x"
    assert written["local_hosts"]["studio"]["label"] == "Studio"
    ui.handle("enter")
    assert ui.view == "list" and ui.screen == "claude"
    after = re.sub(r" +", " ", _pages(ui, 100, 12))
    assert "Configured: review_only" in after and "Effective: review_only" in after
    assert "Account: claude · singleton subscription account · review_only" in after

    # Tier label choice offers every known plan plus clearing it.
    ui.handle("enter")
    ui.menu_index = 1
    ui.handle("enter")
    assert "CLAUDE SUBSCRIPTION TIER" in ui.render(80, 12, "")
    labels = [label for label, _value in ui.choice_items]
    assert "Max 5x (current)" in labels and "Max 20x" in labels and "Pro" in labels
    assert ui.choice_items[-1] == (CLAUDE_TIER_CLEAR_LABEL, None)
    _choose(ui, "Max 20x")
    assert "Claude account edit: subscription_tier = Max 20x" in ui.render(80, 12, "")
    ui.handle("enter")
    assert ui.message.startswith("Applied Claude account edit (subscription_tier = Max 20x)")
    assert json.loads(path.read_text())["providers"]["claude"]["subscription_tier"] == "Max 20x"
    ui.handle("enter")
    assert "Tier: Max 20x" in re.sub(r" +", " ", _pages(ui, 100, 12))

    # Headroom floors are typed, validated by the manager, and previewed.
    ui.handle("enter")
    ui.menu_index = 2
    ui.handle("enter")
    assert ui.view == "claude-input" and ui.input_buffer == "12"
    assert ui.handle("q") is None and ui.input_buffer == "12q"
    ui.handle("backspace")
    for _ in range(2):
        ui.handle("backspace")
    _type(ui, "8")
    ui.handle("enter")
    assert ui.view == "confirm"
    assert "small_task_min_headroom_pct = 8" in ui.render(80, 12, "")
    ui.handle("y")
    assert "small_task_min_headroom_pct = 8" in ui.message
    assert json.loads(path.read_text())["providers"]["claude"]["small_task_min_headroom_pct"] == 8
    ui.handle("enter")
    ui.handle("enter")
    ui.menu_index = 3
    ui.handle("enter")
    assert ui.view == "claude-input" and ui.input_buffer == "25"
    ui.input_buffer = "5"
    ui.handle("enter")
    assert ui.view == "message"
    assert "Invalid big-task minimum headroom percent" in ui.message
    assert "must not exceed" in ui.message
    ui.handle("enter")
    assert ui.view == "claude-input"
    ui.input_buffer = "abc"
    ui.handle("enter")
    assert ui.view == "message" and "Invalid big-task" in ui.message
    assert json.loads(path.read_text())["providers"]["claude"]["big_task_min_headroom_pct"] == 25


def test_claude_edits_refuse_stale_previews_and_excluded_contexts(
    tmp_path, monkeypatch
) -> None:
    _human_terminal(monkeypatch)
    ui, _manager = _inventory_ui(tmp_path, monkeypatch)
    path = tmp_path / "settings.json"
    ui.handle(KEY_BACKTAB)
    assert ui.screen == "claude"
    ui.handle("enter")
    ui.menu_index = 0
    ui.handle("enter")
    _choose(ui, "opportunistic")
    assert ui.view == "confirm" and ui.confirm_kind == "claude"

    local_cli.apply_action(
        tmp_path, "set-seat-mode", "large", mode="off", expected_digest=None
    )
    before = path.read_bytes()
    ui.handle("y")
    assert ui.view == "message" and "re-preview" in ui.message
    assert path.read_bytes() == before
    ui.handle("enter")
    assert ui.view == "claude-actions"

    ui.menu_index = 0
    ui.handle("enter")
    _choose(ui, "opportunistic")
    monkeypatch.setenv("CODEX_SEATS_HEAD", "claude")
    ui.handle("enter")
    assert ui.view == "message" and "refused" in ui.message
    assert path.read_bytes() == before
    assert json.loads(before)["providers"]["claude"]["mode"] == "off"


def test_remove_local_seat_requires_inactive_off_and_keeps_retired_history(
    tmp_path, monkeypatch
) -> None:
    _human_terminal(monkeypatch)
    ui, _manager = _inventory_ui(tmp_path, monkeypatch)
    path = tmp_path / "settings.json"
    original = path.read_bytes()

    _select_inventory(ui, "local-seat:small")
    ui.handle("enter")
    actions = ui._local_actions()
    remove_index = next(i for i, (_label, action) in enumerate(actions) if action == "remove-seat")
    ui.menu_index = remove_index
    ui.handle("enter")
    assert ui.view == "message"
    assert ui.message.startswith("Cannot remove local seat small:")
    assert "requires lifecycle inactive and mode off" in ui.message
    assert "retired history" in ui.message
    assert path.read_bytes() == original
    ui.handle("enter")
    assert ui.view == "local-actions"

    local_cli.apply_action(
        tmp_path, "set-seat-lifecycle", "large", lifecycle="inactive", expected_digest=None
    )
    ui._local_report_cache = None
    _return_to_inventory(ui)
    _select_inventory(ui, "local-seat:large")
    ui.handle("enter")
    ui.menu_index = remove_index
    ui.handle("enter")
    assert ui.view == "confirm" and ui.confirm_kind == "local"
    assert ui.local_preview.action == "remove-seat"
    confirm = ""
    for _ in range(10):
        confirm += "\n" + ui.render(90, 12, "")
        ui.handle("pagedown")
    assert "Local action: remove-seat  Target: seat large" in confirm
    assert "retires the seat in place" in confirm
    assert "requires lifecycle inactive and mode off" in confirm
    assert "retired in place; its id, configuration and history are kept" in confirm
    assert '-      "lifecycle": "inactive"' in confirm
    assert '+      "lifecycle": "retired"' in confirm
    ui.handle("esc")
    assert ui.view == "local-actions" and ui.local_preview is None
    assert json.loads(path.read_text())["local_seats"]["large"]["lifecycle"] == "inactive"

    ui.handle("enter")
    ui.handle("y")
    assert ui.view == "message"
    assert ui.message.startswith("Removed local seat large from service")
    assert "retired history" in ui.message and "no longer be reactivated" in ui.message
    written = json.loads(path.read_text())
    assert written["local_seats"]["large"]["lifecycle"] == "retired"
    assert written["local_seats"]["large"]["model_profile"] == "coder"
    assert written["local_seats"]["small"]["lifecycle"] == "active"
    assert written["local_hosts"]["studio"]["label"] == "Studio"
    ui.handle("enter")
    assert ui.view == "list" and ui.screen == "local-seats"
    assert "large      retired" in ui.render(100, 26, "")

    ui.handle("enter")
    ui.menu_index = remove_index
    ui.handle("enter")
    assert ui.view == "message" and "already retired" in ui.message
    ui.handle("enter")
    _return_to_inventory(ui)
    frame = ui.render(100, 26, "")
    assert "large" not in frame
    ui.handle("h")
    assert "local-seat large      retired" in ui.render(100, 26, "")


def test_inventory_and_claude_screens_keep_minimum_frame_and_sanitized_highlight(
    tmp_path, monkeypatch
) -> None:
    ui, manager = _inventory_ui(tmp_path, monkeypatch)
    manager.settings["pool"]["seats"]["seat1"]["label"] = "n\x1b]0;x\x07界"
    styled = "\x1b[32moverview\x1b[0m"

    def check(frame: str, *, footer: str, width: int = 24, height: int = 6) -> None:
        plain = _plain_frame(frame)
        assert len(plain.splitlines()) <= height
        assert all(display_width(line) <= width for line in plain.splitlines())
        assert "\x1b" not in plain
        assert footer in plain.splitlines()[-1]

    frame = ui.render(24, 6, styled)
    check(frame, footer="q quit")
    assert "> codex" in _plain_frame(frame)
    assert "\x1b[1;48;2;68;71;90m" in frame
    stacked = ui.render(70, 20, "")
    assert "n?]0;x?界 · primary" in stacked and "\x1b" not in stacked
    _select_inventory(ui, "local-seat:small")
    frame = ui.render(24, 6, styled)
    check(frame, footer="q quit")
    assert "> local-seat small" in _plain_frame(frame)
    assert "studio" in _plain_frame(_pages(ui, 24, 6))

    stacked = ui.render(60, 20, "")
    assert " │ " not in stacked and "── INVENTORY 5" in stacked
    assert "SELECTED LOCAL-SEAT:SMALL" in stacked
    assert len(stacked.splitlines()) == 20
    two_column = ui.render(100, 30, "")
    assert "│ ── SELECTED LOCAL-SEAT:SMALL" in two_column
    assert len(two_column.splitlines()) == 30
    assert all(display_width(line) <= 100 for line in two_column.splitlines())

    # The single Add seat row, its type menu, and the host-first submenu stay
    # bounded and keep their cancel key visible at the 24×6 minimum.
    _select_inventory(ui, "add:seat")
    frame = ui.render(24, 6, styled)
    check(frame, footer="q quit")
    assert "> + add" in _plain_frame(frame)
    ui.handle("enter")
    assert ui.view == "local-choice"
    frame = ui.render(24, 6, styled)
    check(frame, footer="Esc")
    assert "ADD SEAT" in _plain_frame(frame)
    check(ui.render(64, 8, styled), footer="Esc back to inventory", width=64, height=8)
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.view == "list"
    ui.handle("a")
    check(ui.render(24, 6, styled), footer="Esc")
    ui.handle("esc")

    # Controls opened from the inventory are bounded and name their return.
    _select_inventory(ui, "codex:seat1")
    ui.handle("enter")
    assert ui.view == "actions"
    check(ui.render(24, 6, styled), footer="Esc")
    check(ui.render(60, 8, styled), footer="Esc inventory", width=60, height=8)
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.inventory_selected == "codex:seat1"
    _select_inventory(ui, "local-seat:small")
    ui.handle("enter")
    assert ui.view == "local-actions"
    check(ui.render(24, 6, styled), footer="Esc")
    ui.handle("esc")
    assert ui.screen == "inventory"

    _select_inventory(ui, "claude:account")
    ui.handle("enter")
    assert ui.screen == "claude" and ui.view == "claude-actions"
    check(ui.render(24, 6, styled), footer="Enter")
    assert "CLAUDE" in ui.render(24, 6, styled)
    ui.handle("esc")
    assert ui.screen == "inventory"
    ui.handle(KEY_BACKTAB)
    assert ui.screen == "claude" and ui.view == "list"
    check(ui.render(24, 6, styled), footer="q quit")
    ui.handle("enter")
    check(ui.render(24, 6, styled), footer="Enter")
    ui.menu_index = 2
    ui.handle("enter")
    assert ui.view == "claude-input"
    ui.input_buffer = "12\x1b[2J界"
    frame = ui.render(24, 6, styled)
    check(frame, footer="Esc")
    assert "?[2J界" in _plain_frame(frame)
    ui.handle("esc")
    ui.menu_index = 0
    ui.handle("enter")
    assert ui.view == "local-choice"
    check(ui.render(24, 6, styled), footer="Esc")
    _choose(ui, "opportunistic")
    assert ui.view == "confirm" and ui.confirm_kind == "claude"
    frame = ui.render(24, 6, styled)
    check(frame, footer="Enter/y")
    assert "CONFIRM" in frame
