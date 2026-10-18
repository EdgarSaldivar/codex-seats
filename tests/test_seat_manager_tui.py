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
from codex_seats.seat_manager import (
    ActionPreview,
    LoginPlan,
    PausePlan,
    PauseResult,
    PendingDrain,
    SeatManager,
    StalePreviewError,
)
from codex_seats.seat_manager_tui import (
    ADD_FIELDS,
    CAPACITY_CHOICES,
    FIELDS,
    GLOBAL_KEYS,
    GLOBAL_LABELS,
    KEY_BACKTAB,
    KEY_TAB,
    KEYMAP,
    MARKER,
    ManagerUI,
    UIExit,
    footer_parts,
    keymap_markdown,
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
from codex_seats.tui_style import strip_ansi

SELECTED_BG = "\x1b[1;48;2;68;71;90m"


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Refresher:
    """A refresher double: records refresh requests, reports no snapshot."""

    def __init__(self) -> None:
        self.settings = None
        self.full_requests = 0

    def snapshot(self):
        return None

    def request_full_refresh(self) -> None:
        self.full_requests += 1


class _FakeManager:
    """Registry-only manager double; mutations are recorded, never written."""

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
        self.busy: set[str] = {"seat1"}
        self.calls: list[tuple] = []
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
                    "role": "primary" if self.settings["pool"].get("primary") == seat_id else None,
                    "lifecycle": entry["lifecycle"],
                    "provider": {
                        "health": "ok",
                        "freshness": "fresh",
                        "binding_window": None,
                    },
                    "claim": {"live": seat_id in self.busy},
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

    def global_settings_report(self):
        return dict(DEFAULTS)

    # -- recorded mutations ---------------------------------------------------
    def _lifecycle(self, seat_id: str, lifecycle: str) -> None:
        self.settings["pool"]["seats"][seat_id]["lifecycle"] = lifecycle

    def preview_pause(self, seat_id):
        busy = seat_id in self.busy
        mode = "drain" if busy else "deactivate"
        return PausePlan(seat_id, busy, mode, replace(_preview(), action=mode, seat_id=seat_id))

    def commit_pause(self, plan):
        self.calls.append(("pause", plan.seat_id))
        self._lifecycle(plan.seat_id, "draining" if plan.busy else "inactive")
        pending = (
            PendingDrain(plan.seat_id, time.time(), "draining", "draining; awaiting idle")
            if plan.busy else None
        )
        return PauseResult(plan.seat_id, SimpleNamespace(changed=True), pending)

    def preview_set_role(self, seat_id, role):
        return replace(_preview(), action="set-role", seat_id=seat_id,
                       options=MappingProxyType({"role": role}))

    def preview_set(self, seat_id, **fields):
        return replace(_preview(), action="set", seat_id=seat_id, options=MappingProxyType(fields))

    def preview_reactivate(self, seat_id, *, as_primary=False):
        return replace(_preview(), action="activate", seat_id=seat_id,
                       options=MappingProxyType({"as_primary": as_primary}))

    def run_activation_checks(self, preview, _cancel):
        return SimpleNamespace(passed=True, seat_id=preview.seat_id, detail="checks completed")

    def commit_activation(self, preview, _checks):
        self.calls.append(("activate", preview.seat_id))
        self._lifecycle(preview.seat_id, "active")

    def commit(self, preview):
        self.calls.append((preview.action, preview.seat_id, dict(preview.options)))
        if preview.action == "set-role":
            self.settings["pool"]["primary"] = preview.seat_id
        if preview.action == "retire":
            self._lifecycle(preview.seat_id, "retired")
        return SimpleNamespace(changed=True, seat_id=preview.seat_id)

    def request_refresh(self, seat_id):
        self.calls.append(("refresh", seat_id))
        return seat_id

    def preview_login(self, seat_id):
        return LoginPlan(seat_id, Path("/pool") / seat_id, "digest", self.settings)

    def cancel_pending_drain(self, seat_id):
        self.calls.append(("cancel-drain", seat_id))
        return SimpleNamespace(detail="wait canceled; seat remains draining")


def _frontier_ui(manager, **kwargs) -> ManagerUI:
    """Direct management lands on the inventory; one Tab reaches Codex seats."""
    ui = ManagerUI(manager, direct=True, **kwargs)  # type: ignore[arg-type]
    assert ui.screen == "inventory" and ui.view == "list"
    ui.handle(KEY_TAB)
    assert ui.screen == "seats" and ui.view == "list"
    return ui


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


def _plain_frame(frame: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", frame)


def _squash(frame: str) -> str:
    return re.sub(r" +", " ", frame)


def _fits(frame: str, width: int, height: int) -> None:
    lines = _plain_frame(frame).splitlines()
    assert len(lines) <= height, (len(lines), height)
    assert all(display_width(line) <= width for line in lines), max(
        (display_width(line), line) for line in lines
    )


# -- global settings -----------------------------------------------------------


def _policy_root(tmp_path: Path) -> SeatManager:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(default_settings()) + "\n")
    path.chmod(0o600)
    with Ledger(tmp_path):
        pass
    return SeatManager(
        tmp_path, load_settings(tmp_path)[0], refresher=SimpleNamespace(settings=None)
    )


def test_global_settings_apply_at_once_with_human_labels(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("codex_seats.seat_manager.ensure_not_managed_worker", lambda: None)
    manager = _policy_root(tmp_path)
    path = tmp_path / "settings.json"
    ui = ManagerUI(manager, direct=True)
    # g works on the inventory with no seats at all and pushes onto the stack.
    ui.handle("g")
    assert ui.view == "global-settings"
    frame = ui.render(100, 24, "")
    assert all(GLOBAL_LABELS[field] in frame for field in DEFAULTS)
    assert "Full access" in frame and "full-access" not in frame.split("Setting:")[0]
    assert "probe mode" not in frame.lower()
    ui.handle("enter")
    assert ui.view == "global-choice"
    before = path.read_text()
    ui.handle("esc")
    assert ui.view == "global-settings" and path.read_text() == before
    # Choosing a value applies it immediately: no dialog, one result line.
    ui.handle("enter")
    ui.handle("down")
    ui.handle("enter")
    assert ui.view == "global-settings"
    assert json.loads(path.read_text())["review"]["mode"] != "off"
    assert ui.message.startswith("✓ Goal review mode → ")
    assert ui.message.split("→ ")[1] in {"Advisory", "Blocking"}
    assert ui.message in ui.render(100, 24, "").splitlines()[-1]
    ui.handle("down")
    assert ui.message == ""
    ui.handle("esc")
    assert ui.view == "list" and ui.screen == "inventory"


def test_global_settings_report_a_stale_preview_without_writing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("codex_seats.seat_manager.ensure_not_managed_worker", lambda: None)
    manager = _policy_root(tmp_path)
    path = tmp_path / "settings.json"
    real_preview = manager.preview_global_settings

    def racing_preview(changes):
        preview = real_preview(changes)
        changed = json.loads(path.read_text())
        changed["future_metadata"] = {"revision": 2}
        path.write_text(json.dumps(changed) + "\n")
        return preview

    manager.preview_global_settings = racing_preview  # type: ignore[method-assign]
    ui = ManagerUI(manager, direct=True)
    ui.handle("g")
    ui.handle("enter")
    ui.handle("down")
    ui.handle("enter")
    assert ui.message_kind == "error" and "stale" in ui.message.lower()
    written = json.loads(path.read_text())
    assert written["future_metadata"] == {"revision": 2}
    assert written.get("review", {}).get("mode", "off") == "off"


def test_global_settings_offer_six_controls_without_probe_mode() -> None:
    assert tuple(GLOBAL_LABELS) == tuple(DEFAULTS)
    assert set(DEFAULTS) == {
        "review.mode", "review.strategy", "execution.delegation_policy",
        "execution.inline_max_minutes", "execution.head_permissions",
        "execution.head_worker_fallback",
    }
    assert DEFAULTS["review.mode"] == "off"
    assert DEFAULTS["execution.inline_max_minutes"] == 5
    assert DEFAULTS["execution.head_worker_fallback"] is True
    assert "pool.probe_mode" not in DEFAULTS


def test_global_settings_number_and_boolean_apply_and_reload(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("codex_seats.seat_manager.ensure_not_managed_worker", lambda: None)
    manager = _policy_root(tmp_path)
    ui = ManagerUI(manager, direct=True)
    ui.handle("g")
    ui.global_index = tuple(DEFAULTS).index("execution.inline_max_minutes")
    ui.handle("enter")
    assert ui.view == "global-input"
    # Only digits are typed into the minutes field; q is not a quit here.
    assert ui.handle("q") is None
    ui.input_buffer = ""
    for key in "12":
        ui.handle(key)
    ui.handle("enter")
    assert ui.view == "global-settings"
    assert manager.settings["execution"]["inline_max_minutes"] == 12
    assert manager.refresher.settings is manager.settings
    assert "Inline minutes → 12 min" in ui.message
    ui.global_index = tuple(DEFAULTS).index("execution.head_worker_fallback")
    ui.handle("enter")
    ui.handle("down")
    ui.handle("enter")
    assert manager.settings["execution"]["head_worker_fallback"] is False
    assert "Head fallback → Disabled" in ui.message
    ui.global_index = tuple(DEFAULTS).index("review.strategy")
    ui.handle("enter")
    ui.handle("down")
    ui.handle("enter")
    assert manager.settings["review"]["strategy"] != "single"


# -- keys, keymap and help --------------------------------------------------------


def test_key_decoder_and_unicode_width_are_terminal_safe() -> None:
    assert decode_key(b"\x1b[A") == "up"
    assert decode_key(b"\x1b") == "esc"
    assert decode_key(b"\x1b[Z") == KEY_BACKTAB
    assert decode_key(b"\x04") == "eof"
    assert decode_key("界".encode()) == "界"
    assert clean_text("label\x1b[2J\n") == "label?[2J?"
    assert display_width("a界é") == 4
    assert display_width(fit_text("a界", 4)) == 4
    assert "\x1b" not in fit_text("bad\x1b[H", 20)


def test_footer_is_generated_from_the_keymap_and_fits_by_priority() -> None:
    ui = _frontier_ui(_FakeManager())
    ui.handle(KEY_BACKTAB)
    wide = ui.render(120, 30, "").splitlines()[-1]
    assert wide == (
        "↑↓ select  Enter open  c r e o p l seat actions  a add  / filter  "
        "Tab screens  g settings  ? help  Esc overview"
    )
    narrow = ui.render(84, 30, "").splitlines()[-1]
    assert narrow == "↑↓ select  Enter open  a add  / filter  Tab screens  ? help  Esc overview"
    tiny = ui.render(48, 20, "").splitlines()[-1]
    assert tiny == "↑↓ select  Enter open  ? help  Esc overview"
    # Every entry in a footer comes from a binding of that screen.
    caps = {key.cap for key in (*KEYMAP["inventory"], *GLOBAL_KEYS)}
    parts = footer_parts((*KEYMAP["inventory"], *GLOBAL_KEYS), 200)
    shown = {text for text, role in parts if role == "accent"}
    shorts = {key.short for key in (*KEYMAP["inventory"], *GLOBAL_KEYS)}
    assert {cap for cap in shown if " " not in cap} <= caps | shorts


def test_help_lists_this_screen_and_everywhere_keys_and_returns_to_its_origin() -> None:
    ui = _frontier_ui(_FakeManager())
    ui.handle(KEY_BACKTAB)
    ui.handle("?")
    assert ui.view == "help"
    frame = ui.render(96, 30, "")
    assert frame.splitlines()[0].startswith("Help · Inventory")
    assert "THIS SCREEN" in frame and "EVERYWHERE" in frame
    for key in KEYMAP["inventory"]:
        assert key.help in frame, key.help
    for key in GLOBAL_KEYS:
        assert key.help in frame, key.help
    assert "only Redeem (c) and Remove ask first" in frame
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.view == "list"

    # Help opened from a menu describes the menu and returns to it.
    ui.handle(KEY_TAB)
    ui.handle("enter")
    assert ui.view == "actions"
    ui.handle("?")
    assert ui.render(80, 24, "").splitlines()[0].startswith("Help · Actions")
    ui.handle("?")
    assert ui.view == "actions" and ui.screen == "seats"
    ui.handle("esc")
    assert ui.view == "list" and ui.screen == "seats"
    ui.handle("esc")
    assert ui.view == "overview"


def test_doc_keymap_table_matches_the_generated_keymap() -> None:
    doc = (Path(__file__).parents[1] / "docs" / "terminal-seat-manager.md").read_text()
    start = doc.index("<!-- keymap:start -->") + len("<!-- keymap:start -->")
    end = doc.index("<!-- keymap:end -->")
    assert doc[start:end].strip() == keymap_markdown()


def test_overview_preserves_dashboard_colors_and_uses_the_generated_footer() -> None:
    ui = ManagerUI(None)
    dashboard = "\x1b[1;36mCXWATCH\x1b[0m\n\x1b[32mseat1\x1b[0m  50%    idle"
    frame = ui.render(30, 8, dashboard)
    assert frame.splitlines()[:2] == dashboard.splitlines()
    assert "?[" not in frame
    assert _plain_frame(frame).endswith("m manage  ? help  q quit")
    wide = ManagerUI(None).render(100, 8, dashboard)
    assert _plain_frame(wide).endswith("m manage  R refresh all  g settings  ? help  q quit")


def test_colored_dashboard_survives_interactive_wrapper(tmp_path) -> None:
    from codex_seats.display import dashboard

    with Ledger(tmp_path):
        pass
    styled = dashboard(
        tmp_path, default_settings(), [], color=True, width=80, height=23
    )
    frame = ManagerUI(None).render(80, 24, styled)
    assert "\x1b[" in styled
    assert frame.splitlines()[:-1] == styled.splitlines()
    visible = _plain_frame(frame)
    assert "?[" not in visible
    assert all(display_width(line) <= 80 for line in visible.splitlines())


# -- the seat table -------------------------------------------------------------------


def test_long_label_does_not_hide_status_or_narrow_screen_exit_keys() -> None:
    manager = _FakeManager()
    manager.settings["pool"]["seats"]["seat2"]["label"] = "Long name " * 20
    frame = _frontier_ui(manager).render(40, 20, "")
    assert "– paused" in frame
    assert frame.splitlines()[-1].endswith("Esc overview")
    _fits(frame, 40, 20)


def test_selection_is_stable_id_based_across_report_reordering() -> None:
    manager = _FakeManager()
    ui = _frontier_ui(manager)
    ui.render(80, 20, "")
    assert ui.selected_id == "seat1"

    ui.handle("down")
    assert ui.selected_id == "seat2"
    manager.settings["pool"]["seats"] = dict(
        reversed(manager.settings["pool"]["seats"].items())
    )
    rendered = ui.render(80, 20, "")

    assert ui.selected_id == "seat2"
    assert f"{MARKER} W seat2" in rendered
    assert "\x1b" not in rendered


def test_add_row_is_discoverable_for_empty_and_large_registries() -> None:
    manager = _FakeManager()
    manager.settings["pool"]["seats"] = {}
    empty = _frontier_ui(manager).render(60, 12, "")
    assert "CODEX 0" in empty
    assert "+ Add Codex seat   a" in empty

    manager = _FakeManager()
    template = dict(manager.settings["pool"]["seats"]["seat2"])
    manager.settings["pool"]["seats"] = {
        f"seat{number}": {**template, "label": f"Seat {number}"}
        for number in range(1, 8)
    }
    large = _frontier_ui(manager).render(60, 12, "")
    assert "CODEX 7" in large
    assert "more ↓" in large and "a add" in large.splitlines()[-1]


def test_table_shows_role_plan_bars_reset_and_status_chip(tmp_path) -> None:
    manager = _FakeManager()
    manager.list_report = lambda **_k: {"seats": [
        {
            "id": "seat1", "role": "primary", "lifecycle": "active",
            "provider": {"health": "ok", "observed_at": time.time() - 180},
            "claim": {"live": False},
            "capacity": {
                "profile": "business-standard", "relative_capacity": 1.0,
                "effective_relative_capacity": 1.0, "source": "operator",
                "observed_plan_type": None, "credits": None,
                "active_windows": [
                    {"name": "5h", "remaining_pct": 64, "window_minutes": 300,
                     "resets_at": time.time() + 8100},
                    {"name": "weekly", "remaining_pct": 0, "window_minutes": 10080,
                     "resets_at": time.time() + 532800},
                ],
            },
        },
        {"id": "seat2", "role": None, "lifecycle": "inactive", "provider": {"health": "ok"},
         "claim": {"live": False}, "capacity": {}},
    ]}
    ui = _frontier_ui(manager)
    frame = ui.render(120, 30, "")
    lines = frame.splitlines()
    header = next(line for line in lines if "5H LEFT" in line)
    assert all(column in header for column in ("SEAT", "7D LEFT", "RESET", "STATUS"))
    row = next(line for line in lines if line.startswith(MARKER))
    assert row.startswith(f"{MARKER} P seat1 Standard 1x")
    assert "64%" in row and "0%" in row and "◐ capped 7d" in row
    assert "6d04h" in row or "6d03h" in row
    paused = next(line for line in lines if line.startswith("  W seat2"))
    assert paused.startswith("  W seat2 Unverified") and "– paused" in paused
    assert "CODEX 2 · primary seat1 is used last" in frame
    assert "deputy" not in frame.lower() and " D " not in frame


def test_retired_seats_are_hidden_until_history_toggle() -> None:
    manager = _FakeManager()
    manager.settings["pool"]["seats"]["seat2"]["lifecycle"] = "retired"
    ui = _frontier_ui(manager)
    ordinary = ui.render(100, 20, "")
    assert "CODEX 1" in ordinary
    assert "seat2" not in ordinary

    ui.handle("h")
    history = ui.render(100, 20, "")
    assert "CODEX 2" in history and "seat2" in history and "– retired" in history
    ui.handle("h")
    assert "seat2" not in ui.render(100, 20, "")


def test_filter_narrows_rows_and_esc_clears_it() -> None:
    manager = _FakeManager()
    template = dict(manager.settings["pool"]["seats"]["seat2"])
    manager.settings["pool"]["seats"].update(
        {f"seat{n}": {**template, "label": f"Box {n}"} for n in range(3, 6)}
    )
    ui = _frontier_ui(manager)
    ui.handle("/")
    assert ui.filtering
    for key in "box 4":
        assert ui.handle(key) is None  # q, j, k and friends are typed here
    frame = ui.render(100, 24, "")
    assert frame.splitlines()[-1].startswith("/ box 4")
    assert "seat4" in frame and "seat3" not in frame and "seat1" not in frame
    ui.handle("enter")
    assert not ui.filtering and ui.filter == "box 4"
    assert ui.selected_id == "seat4"
    assert "seat3" not in ui.render(100, 24, "")
    ui.handle("esc")
    assert ui.filter == "" and ui.view == "list"
    assert "seat3" in ui.render(100, 24, "")
    ui.handle("/")
    for key in "nothing":
        ui.handle(key)
    assert "nothing matches the filter" in ui.render(100, 24, "")
    ui.handle("esc")
    assert ui.filter == "" and not ui.filtering


# -- direct keys: reversible actions apply at once ------------------------------------


def test_pause_and_resume_apply_without_a_dialog_and_report_a_result_line() -> None:
    manager = _FakeManager()
    manager.busy = set()
    manager.settings["pool"]["seats"]["seat3"] = dict(
        manager.settings["pool"]["seats"]["seat1"], label="Third", lifecycle="active"
    )
    ui = _frontier_ui(manager)
    while ui.selected_id != "seat3":
        ui.handle("down")
    ui.handle("p")
    assert ui.view == "list"
    assert manager.calls == [("pause", "seat3")]
    assert manager.settings["pool"]["seats"]["seat3"]["lifecycle"] == "inactive"
    assert ui.message == "✓ Paused seat3 · new work skips it · p resumes"
    footer = ui.render(84, 24, "").splitlines()[-1]
    assert footer == "✓ Paused seat3  ·  new work skips it  ·  p resumes"
    # The result line clears on the next key.
    ui.handle("j")
    assert ui.message == ""
    ui.handle("k")
    # Resume runs the sign-in and usage checks off the key loop, then commits.
    ui.handle("p")
    assert ui.view == "busy"
    assert "Checking sign-in and fresh usage for seat3" in ui.render(84, 24, "")
    ui.activation_thread.join(timeout=2)
    assert ui.poll() is True
    assert ui.view == "list"
    assert manager.calls[-1] == ("activate", "seat3")
    assert ui.message.startswith("✓ Resumed seat3")


def test_pausing_a_busy_seat_drains_and_the_menu_can_cancel_the_wait() -> None:
    manager = _FakeManager()
    manager.busy = {"seat3"}
    manager.settings["pool"]["seats"]["seat3"] = dict(
        manager.settings["pool"]["seats"]["seat1"], label="Third", lifecycle="active"
    )
    ui = _frontier_ui(manager)
    while ui.selected_id != "seat3":
        ui.handle("down")
    ui.handle("p")
    assert ui.message.startswith("◐ Draining seat3")
    assert manager.settings["pool"]["seats"]["seat3"]["lifecycle"] == "draining"
    manager.pending = [SimpleNamespace(seat_id="seat3", state="draining")]
    ui.handle("enter")
    assert ui.view == "actions"
    labels = [action.label for action in ui._menu_actions()]
    assert "Cancel this session's drain wait" in labels
    ui.menu_index = labels.index("Cancel this session's drain wait")
    ui.handle("enter")
    assert manager.calls[-1] == ("cancel-drain", "seat3")
    assert "seat3" in ui.message and "draining" in ui.message


def test_refresh_role_and_refresh_all_apply_at_once() -> None:
    manager = _FakeManager()
    manager.refresher = _Refresher()
    manager.settings["pool"]["seats"]["seat2"]["lifecycle"] = "active"
    ui = _frontier_ui(manager)
    ui.handle("down")
    assert ui.selected_id == "seat2"
    ui.handle("r")
    assert manager.calls == [("refresh", "seat2")]
    assert ui.message.startswith("↻ Refreshing seat2")
    ui.handle("o")
    assert manager.calls[-1] == ("set-role", "seat2", {"role": "primary"})
    assert ui.message == "✓ seat2 is now primary · routing uses it last"
    assert ui.view == "list"
    # The new primary cannot be "made primary" again; the reason is shown.
    ui.handle("o")
    assert ui.message_kind == "error" and "already primary" in ui.message
    ui.handle("R")
    assert manager.refresher.full_requests == 1
    assert ui.message.startswith("↻ Refreshing every seat")


def test_disabled_actions_show_their_reason_in_the_detail_and_on_the_key() -> None:
    manager = _FakeManager()
    ui = _frontier_ui(manager)
    assert ui.selected_id == "seat1"
    frame = _squash(ui.render(120, 40, ""))
    assert "p Pause make another seat primary first" in frame
    assert "o Make primary already primary · used last" in frame
    assert "r Refresh usage no refresher in this session" in frame
    ui.handle("p")
    assert manager.calls == []
    assert ui.message == "× Pause: make another seat primary first"
    ui.handle("enter")
    actions = {action.label: action for action in ui._menu_actions()}
    assert actions["Remove seat…"].reason == "make another seat primary first"
    ui.menu_index = list(actions).index("Remove seat…")
    ui.handle("enter")
    assert ui.view == "actions" and ui.message_kind == "error"


def test_sign_in_hands_off_to_the_login_flow_immediately() -> None:
    manager = _FakeManager()
    ui = _frontier_ui(manager)
    outcome = ui.handle("l")
    assert isinstance(outcome, UIExit) and outcome.reason == "login"
    assert outcome.login_seat_id == "seat1" and outcome.login_plan is ui.login_plan


# -- confirmed actions: redeem and remove ------------------------------------------------


def test_remove_requires_the_dialog_and_shows_what_changes() -> None:
    manager = _FakeManager()
    ui = _frontier_ui(manager)
    ui.handle("down")
    assert ui.selected_id == "seat2"
    ui.handle("enter")
    labels = [action.label for action in ui._menu_actions()]
    ui.menu_index = labels.index("Remove seat…")
    ui.handle("enter")
    assert ui.view == "confirm" and ui.confirm_kind == "remove"
    assert manager.calls == []
    frame = ui.render(84, 24, "")
    _fits(frame, 84, 24)
    for text in ("╭─ Confirm", "Remove seat2 from service", "What changes",
                 "Lifecycle", "Paused → Retired", "! A retired seat cannot be reactivated",
                 "Enter Remove", "Esc Cancel", "d Show raw diff"):
        assert text in frame, text
    ui.handle("d")
    raw = ui.render(84, 24, "")
    assert "Raw settings diff" in raw and "--- settings.before" in raw
    ui.handle("esc")
    assert ui.view == "actions" and manager.calls == []
    ui.handle("enter")
    ui.handle("enter")
    assert manager.calls == [("retire", "seat2", {"label": "new"})]
    assert ui.view == "list" and ui.message.startswith("✓ Removed seat2 from service")


def test_remove_dialog_offers_a_fresh_preview_when_settings_changed() -> None:
    manager = _FakeManager()
    previews = []
    manager.preview_remove = lambda seat_id: previews.append(seat_id) or replace(  # type: ignore[method-assign]
        _preview(), action="retire", seat_id=seat_id
    )
    attempts = []

    def stale_once(preview):
        attempts.append(preview)
        if len(attempts) == 1:
            raise StalePreviewError("settings changed since this action was previewed")
        return SimpleNamespace(changed=True, seat_id=preview.seat_id)

    manager.commit = stale_once  # type: ignore[method-assign]
    ui = _frontier_ui(manager)
    ui.handle("down")
    ui._open_remove("seat2")
    ui.handle("enter")
    assert ui.view == "confirm" and ui.confirm_stale
    frame = ui.render(84, 24, "")
    assert "Settings changed since this preview" in frame and "Enter Fresh preview" in frame
    ui.handle("enter")
    assert previews == ["seat2", "seat2"] and not ui.confirm_stale and ui.view == "confirm"
    ui.handle("enter")
    assert len(attempts) == 2 and ui.view == "list"
    assert ui.message.startswith("✓ Removed seat2")


def test_redeem_reads_in_the_background_then_requires_the_dialog(monkeypatch) -> None:
    manager = _FakeManager()
    manager.root = Path("/pool")
    manager.busy = set()
    calls = []

    def prepare(root, settings, seat_id, *, cancel_event):
        calls.append(("prepare", seat_id))
        return {
            "seat": seat_id, "home": "/pool/seat1", "available_count": 2,
            "provider_identity": json.dumps({"email": "ops@example.test\x1b[2J"}),
            "credit": {"title": "Weekly", "fingerprint": "f1", "granted_at": None,
                       "expires_at": None, "expiry_status": "none"},
        }

    def redeem(root, settings, preview, *, current_user_request, cancel_event):
        calls.append(("redeem", preview["seat"], current_user_request))
        return {"outcome": "redeemed", "refresh_error": None}

    monkeypatch.setattr("codex_seats.seat_manager_tui.prepare_banked_reset_redemption", prepare)
    monkeypatch.setattr("codex_seats.seat_manager_tui.redeem_banked_reset", redeem)
    ui = _frontier_ui(manager)
    ui.handle("c")
    assert ui.view == "busy"
    ui.reset_thread.join(timeout=2)
    assert ui.poll() is True
    assert ui.view == "confirm" and ui.confirm_kind == "redeem-reset"
    frame = ui.render(84, 24, "")
    for text in ("Redeem one banked reset on seat1", "ops@example.test?[2J",
                 "Usage windows", "reset immediately", "Banked resets", "2 → 1",
                 "! Spends a provider credit. This cannot be undone.", "Enter Redeem"):
        assert text in frame, text
    assert calls == [("prepare", "seat1")]
    ui.handle("enter")
    ui.reset_thread.join(timeout=2)
    ui.poll()
    assert calls[-1] == ("redeem", "seat1", True)
    assert ui.view == "list" and ui.message.startswith("✓ Reset outcome: redeemed")


def test_dialog_raw_diff_pages_and_keeps_warnings_sanitized() -> None:
    manager = _FakeManager()
    manager.preview_remove = lambda seat_id: replace(  # type: ignore[method-assign]
        _preview(45), action="retire", seat_id=seat_id,
        warnings=("Do not miss this warning \x1b[2J界",),
    )
    ui = _frontier_ui(manager)
    ui._open_remove("seat2")
    ui.handle("d")
    pages: list[str] = []
    for _ in range(40):
        frame = ui.render(54, 14, "\x1b[32moverview\x1b[0m")
        pages.append(_plain_frame(frame))
        _fits(frame, 54, 14)
        before = ui.scroll
        ui.handle("pagedown")
        ui.render(54, 14, "")
        if ui.scroll == before:
            break
    seen = "\n".join(pages)
    assert "! Do not miss this warning ?[2J界" in seen
    assert "settings.before" in seen and "settings.after" in seen
    assert re.search(r"↑↓ \d+-\d+/\d+", seen)
    assert "\x1b" not in seen


def test_short_terminal_blocks_hidden_confirmation_until_resized() -> None:
    manager = _FakeManager()
    ui = _frontier_ui(manager)
    ui._open_remove("seat2")
    ui.render(20, 4, "")
    assert ui.handle("enter") is None
    assert manager.calls == []
    assert ui.view == "confirm"
    ui.render(80, 20, "")
    ui.handle("enter")
    assert manager.calls and manager.calls[0][0] == "retire"


def test_short_terminal_renders_bounded_resize_message() -> None:
    ui = ManagerUI(_FakeManager(), direct=True)  # type: ignore[arg-type]
    frame = ui.render(20, 4, "")
    assert len(frame.splitlines()) <= 4
    assert "Terminal too small" in frame


# -- forms ----------------------------------------------------------------------------------


def test_add_form_adds_at_once_and_selects_the_new_paused_seat() -> None:
    manager = _FakeManager()
    ui = _frontier_ui(manager)
    ui.handle("a")
    assert ui.view == "add-form"
    assert ADD_FIELDS[-1].option == "submit"
    form = ui.render(80, 20, "")
    assert "Add Codex seat" in form and "New seat" in form and "→ Add seat" in form
    ui.handle("esc")
    assert ui.view == "list" and manager.calls == []
    ui.handle("a")
    ui.add_field_index = len(ADD_FIELDS) - 1
    ui.handle("enter")
    assert manager.calls == [("add", "seat3", {"label": "new"})]
    assert ui.view == "list" and ui.selected_id == "seat3"
    assert ui.message == "✓ Added seat3 (paused) · l signs it in · p resumes it"


def test_add_form_plan_preset_and_printable_q_are_usable() -> None:
    ui = _frontier_ui(_FakeManager())
    ui.handle("a")
    ui.add_field_index = 0
    ui.handle("enter")
    assert ui.view == "add-input"
    ui.input_buffer = "Ac"
    assert ui.handle("q") is None
    assert ui.handle("?") is None
    assert ui.input_buffer == "Acq?"
    ui.handle("backspace")
    ui.handle("enter")
    assert ui.view == "add-form" and ui.add_buffers["label"] == "Acq"

    ui.add_field_index = 2
    ui.handle("enter")
    assert ui.view == "add-presets"
    ui.add_preset_index = 3
    assert "Plan · for the new seat" in ui.render(72, 12, "")
    ui.handle("enter")
    assert ui.view == "add-form" and ui.add_buffers["profile"] == "personal-plus"
    assert "Plus" in ui.render(72, 12, "")


def test_narrow_add_form_keeps_values_and_editable_input_visible() -> None:
    ui = _frontier_ui(_FakeManager())
    ui.handle("a")
    frame = ui.render(24, 6, "")
    assert "New seat" in frame
    assert frame.splitlines()[-1].endswith("Esc back")
    _fits(frame, 24, 6)
    ui.handle("enter")
    ui.input_buffer = "New account"
    frame = ui.render(24, 6, "")
    assert "> New account" in frame
    assert "Esc cancel" in frame.splitlines()[-1]
    _fits(frame, 24, 6)


def test_settings_fields_apply_at_once_and_stay_on_the_form() -> None:
    manager = _FakeManager()
    ui = _frontier_ui(manager)
    ui.handle("e")
    assert ui.view == "settings"
    frame = ui.render(80, 20, "")
    assert "Edit plan & headroom · seat1" in frame
    assert "role" not in frame.lower().replace("routing", "")
    ui.field_index = [field.option for field in FIELDS].index("small_task_min_headroom_pct")
    ui.handle("enter")
    assert ui.view == "input"
    ui.input_buffer = "15"
    ui.handle("enter")
    assert ui.view == "settings"
    assert manager.calls == [("set", "seat1", {"small_task_min_headroom_pct": 15.0})]
    assert ui.message == "✓ Saved seat1 · Small-task floor % 15"
    ui.handle("enter")
    ui.input_buffer = "not a number"
    ui.handle("enter")
    assert ui.view == "input" and ui.message.startswith("× Invalid value")
    ui.handle("esc")
    ui.handle("esc")
    assert ui.view == "list"


def test_action_and_settings_windows_keep_dynamic_selection_visible() -> None:
    manager = _FakeManager()
    ui = ManagerUI(manager, direct=True)  # type: ignore[arg-type]
    ui.selected_id = "seat2"
    ui._push("actions")
    ui.menu_index = 5
    manager.settings["pool"]["seats"]["seat2"]["lifecycle"] = "retired"
    actions = ui.render(60, 8, "")
    assert ui.menu_index <= len(ui._menu_actions()) - 1
    assert "Seat actions · seat2" in actions

    ui._push("settings")
    ui.field_index = len(FIELDS) - 1
    fields = ui.render(60, 8, "")
    assert f"{MARKER} Plan & capacity" in fields


def test_advanced_capacity_form_keeps_all_fields_at_minimum_height() -> None:
    ui = ManagerUI(_FakeManager(), direct=True)  # type: ignore[arg-type]
    ui.selected_id = "seat1"
    ui._push("capacity-input")
    ui.capacity_buffers = {
        "profile": "personal-pro-20x",
        "relative_capacity": "20",
        "capacity_source": "operator-confirmed-界\x1b[2J",
    }
    frame = ui.render(60, 8, "")
    assert "profile:" in frame
    assert "relative capacity" in frame
    assert "source:" in frame
    assert "?[2J" in frame
    assert "Enter save" in frame.splitlines()[-1]
    assert len(frame.splitlines()) == 8


def test_capacity_presets_cover_personal_business_and_apply_at_once() -> None:
    manager = _FakeManager()
    ui = _frontier_ui(manager)
    ui.handle("e")
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
    assert manager.calls[-1][2] == {
        "profile": "personal-pro-20x",
        "relative_capacity": 20.0,
        "capacity_source": "openai-published-baseline",
    }
    assert ui.view == "settings" and ui.message.startswith("✓ Saved seat1 · plan")

    ui.handle("enter")
    ui.capacity_preset_index = len(CAPACITY_CHOICES) - 1
    short_frame = ui.render(52, 8, "")
    assert f"{MARKER} Advanced / custom values" in short_frame
    ui.handle("enter")
    assert ui.view == "capacity-input"
    assert set(ui.capacity_buffers) == {"profile", "relative_capacity", "capacity_source"}


# -- detail pane ----------------------------------------------------------------------------


def test_selected_detail_can_be_paged_without_losing_selected_row() -> None:
    ui = _frontier_ui(_FakeManager())
    first = ui.render(60, 16, "")
    ui.handle("pagedown")
    second = ui.render(60, 16, "")
    assert f"{MARKER} P seat1" in first and f"{MARKER} P seat1" in second
    assert first != second
    assert "PgDn more" in first


def test_missing_usage_identity_and_credits_are_explicitly_unavailable() -> None:
    ui = _frontier_ui(_FakeManager())
    seen = "\n".join(ui._plain(line) for line in ui._codex_detail(
        ui._entry("codex", "seat1"), 60, compact=False))
    assert "Usage   not reported yet" in seen
    assert "Email   unavailable · identity has not been refreshed" in seen
    assert "Sign-in not verified yet" in seen
    assert "Credits not reported" in seen
    assert "unlimited" not in seen.lower()


def test_identity_usage_and_credits_are_sanitized_and_scrollable() -> None:
    manager = _FakeManager()
    manager.identity = {
        "status": "fresh",
        "email": "seat@example.test\x1b[2J",
        "observed_plan_type": "business",
        "account_id": "account-456",
        "observed_at": time.time() - 120,
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
                    "credits": {"has_credits": True, "unlimited": False, "balance": "42.50"},
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
    seen = ""
    for _ in range(40):
        frame = ui.render(48, 12, "")
        assert "\x1b" not in frame
        assert f"{MARKER}P seat1" in frame
        _fits(frame, 48, 12)
        seen += "\n" + frame
        ui.handle("pagedown")
    compact = _squash(seen)
    assert "Email seat@example.test?[2J" in compact
    assert "Sign-in ✓ verified 2m ago" in compact
    assert "Plan Standard 1x · observed: Business" in compact
    assert "5-hour" in compact and "75%" in compact
    assert "Credits balance 42.50" in compact
    assert "Account ID account-456" in compact
    assert "Workspace" not in seen


def test_management_layout_is_two_column_when_wide_and_stacked_when_narrow() -> None:
    ui = _frontier_ui(_FakeManager())
    wide = ui.render(120, 36, "")
    assert " │ seat1  Primary · Standard 1x" in wide
    assert all(name in wide for name in ("── USAGE", "── ACCOUNT", "── ACTIONS", "── DETAILS"))
    assert len(wide.splitlines()) == 36
    _fits(wide, 120, 36)

    narrow = ui.render(84, 30, "")
    assert " │ " not in narrow
    assert "── SEAT1 · PRIMARY · STANDARD 1X" in narrow
    assert "c Redeem reset  r Refresh  e Edit  o Role  p Pause  l Sign in" in narrow
    assert len(narrow.splitlines()) == 30
    _fits(narrow, 84, 30)


def test_color_flag_paints_selection_and_keeps_untrusted_text_inert() -> None:
    manager = _FakeManager()
    manager.identity["email"] = "evil\x1b[31m@example.test界"
    manager.identity["status"] = "fresh"
    manager.settings["pool"]["seats"]["seat1"]["label"] = "n\x1b]0;x\x07界"
    ui = _frontier_ui(manager, color=True)
    # Colour comes from the flag, never from sniffing the overview text.
    frame = ui.render(120, 36, "plain overview")
    plain = _plain_frame(frame)
    assert SELECTED_BG in frame
    assert "\x1b" not in plain
    assert "evil?[31m@example.test界" in plain
    assert "n?]0;x?界" in plain
    _fits(frame, 120, 36)

    unstyled = _frontier_ui(manager).render(120, 36, "\x1b[32mstyled overview\x1b[0m")
    assert "\x1b" not in unstyled
    assert f"{MARKER} P seat1" in unstyled
    # The same layout with and without colour: stripping ANSI gives the plain frame.
    assert plain == unstyled


@pytest.mark.parametrize("size", [(140, 40), (120, 36), (84, 30), (60, 20), (48, 20), (24, 6)])
def test_every_view_fits_the_terminal_and_strips_to_the_plain_frame(size) -> None:
    width, height = size

    def views(ui: ManagerUI):
        yield "inventory"
        ui.handle(KEY_TAB)
        yield "seats"
        ui.handle("?")
        yield "help"
        ui.handle("esc")
        ui.handle("enter")
        yield "actions"
        ui.handle("esc")
        ui.handle("e")
        yield "settings"
        ui.handle("enter")
        yield "input"
        ui.handle("esc")
        ui.handle("esc")
        ui.handle("g")
        yield "global"
        ui.handle("esc")
        ui.handle("a")
        yield "add-form"
        ui.handle("esc")
        ui.handle("down")
        ui._open_remove("seat2")
        yield "confirm"
        ui.handle("d")
        yield "raw diff"
        ui.handle("esc")
        ui.handle("/")
        ui.handle("s")
        yield "filter"
        ui.handle("esc")
        ui.handle("p")
        yield "busy"

    plain_ui = ManagerUI(_FakeManager(), direct=True)  # type: ignore[arg-type]
    color_ui = ManagerUI(_FakeManager(), direct=True, color=True)  # type: ignore[arg-type]
    for (name, _), _same in zip(zip(views(plain_ui), views(color_ui)), range(100)):
        plain = plain_ui.render(width, height, "")
        colored = color_ui.render(width, height, "")
        _fits(plain, width, height)
        assert "\x1b" not in plain, name
        assert strip_ansi(colored) == plain, name
    if plain_ui.activation_thread is not None:
        plain_ui.activation_thread.join(timeout=2)
    if color_ui.activation_thread is not None:
        color_ui.activation_thread.join(timeout=2)


def test_minimum_supported_frame_keeps_selection_and_exit_visible() -> None:
    frame = _frontier_ui(_FakeManager()).render(24, 6, "")
    assert len(frame.splitlines()) == 6
    assert f"{MARKER}P seat1" in frame
    assert "Esc" in frame.splitlines()[-1]
    _fits(frame, 24, 6)


# -- quit and background work ------------------------------------------------------------


def test_quit_discloses_that_pending_drain_remains_draining() -> None:
    manager = _FakeManager()
    manager.pending = [SimpleNamespace(seat_id="seat2", state="draining")]
    ui = ManagerUI(manager, direct=True)  # type: ignore[arg-type]

    assert ui.handle("q") is None
    assert ui.view == "quit-confirm"
    frame = _squash(ui.render(80, 16, ""))
    assert "These seats remain" in frame and "seat2" in frame
    assert "Enter Quit, leave draining" in frame
    assert ui.handle("esc") is None and ui.view == "list"
    ui.handle("q")
    assert ui.handle("enter") == UIExit("quit")


def test_quit_during_completed_activation_cancels_before_commit() -> None:
    manager = _FakeManager()
    manager.pending = [SimpleNamespace(seat_id="seat2", state="draining")]
    commits: list[object] = []
    manager.commit_activation = lambda *_args: commits.append(object())  # type: ignore[method-assign]
    ui = ManagerUI(manager, direct=True)  # type: ignore[arg-type]
    ui.selected_id = "seat1"
    ui.confirm_return_view = "list"
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
    output = _read_until(master, b"seat2")
    output += _read_until(master, b"Add seat")
    assert b"Inventory" in output and b"seat1" in output and b"seat2" in output
    os.write(master, b"j\r")
    output += _read_until(master, b"Seat actions")
    assert b"Seat actions" in output and b"seat2" in output
    os.write(master, b"?")
    output += _read_until(master, b"Help")
    os.write(master, b"\x1b")
    output += _read_until(master, b"Remove seat")
    os.write(master, b"q")
    output += _wait_and_drain(process, master)

    assert process.returncode == 0
    assert ALT_OFF.encode() in output
    os.close(master)
    os.close(slave)


@pytest.mark.parametrize(
    ("excluded_marker", "message"),
    [("managed", b"managed workers")],
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
        "codex_seats.seat_manager.ensure_interactive_terminal", lambda: None
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
        "codex_seats.seat_manager.ensure_interactive_terminal", lambda: None
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
    CLAUDE_SETTINGS,
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
    """Every detail page of the selected row, whitespace squashed."""
    seen = ""
    ui.detail_scroll = 0
    for _ in range(60):
        frame = ui.render(width, height, "")
        _fits(frame, width, height)
        seen += "\n" + frame
        before = ui.detail_scroll
        ui.handle("pagedown")
        ui.render(width, height, "")
        if ui.detail_scroll == before:
            break
    ui.detail_scroll = 0
    return _squash(seen)


def _detail(ui: ManagerUI, width: int = 100) -> str:
    """The selected row's full detail as plain text, whitespace squashed."""
    row = ui._selected_row()
    return _squash("\n".join(ui._plain(line) for line in ui._detail(row, width, compact=False)))


def _row_line(frame: str, text: str) -> str:
    return _squash(next(line for line in _plain_frame(frame).splitlines() if text in line))


def _forbid_probes(monkeypatch) -> None:
    monkeypatch.setattr(
        local_seats, "probe_host", lambda *a, **k: pytest.fail("UI ran a probe inline")
    )




def test_host_detail_keeps_status_visible_with_a_long_probe_path(tmp_path) -> None:
    settings = _local_settings()
    long_python = "/" + "/".join(["a-very-long-directory-name"] * 8) + "/bin/python3"
    settings["local_hosts"]["studio"]["availability_probe"]["argv"][0] = long_python
    ui, _manager, _refresher = _local_ui(tmp_path, settings=settings)
    ui.handle(KEY_TAB)
    frame = _plain_frame(ui.render(120, 30, ""))
    probe_lines = [line for line in frame.splitlines() if "python3 -c" in line]
    assert len(probe_lines) == 1 and "a-very-long-directory-name" not in probe_lines[0]
    # Live status sits above the endpoint configuration.
    assert frame.index("AVAILABILITY") < frame.index("ENDPOINT")
    assert "LIVE PROBE" in frame


def test_tab_switches_screens_and_keeps_stable_selection_per_screen(tmp_path) -> None:
    ui, _manager, _refresher = _local_ui(tmp_path)
    assert SCREENS == ("inventory", "seats", "hosts", "local-seats", "claude")
    frontier = ui.render(100, 24, "")
    assert "▸ Codex seats" in frontier and f"{MARKER} P seat1" in frontier

    ui.handle(KEY_TAB)
    hosts = ui.render(100, 24, "")
    assert ui.screen == "hosts" and "▸ Local hosts" in hosts
    assert f"{MARKER} H studio" in hosts

    ui.handle(KEY_TAB)
    assert ui.screen == "local-seats"
    ui.handle("j")
    assert ui.local_selected_id == "small"
    seats = ui.render(100, 24, "")
    assert "▸ Local seats" in seats and f"{MARKER} L coder on studio small" in seats
    assert "coder on studio large" in seats

    ui.handle(KEY_TAB)
    assert ui.screen == "claude" and ui.view == "list"
    assert "▸ Claude" in ui.render(100, 24, "")
    ui.handle(KEY_TAB)
    assert ui.screen == "inventory" and ui.view == "list"
    assert "▸ Inventory" in ui.render(100, 24, "")
    ui.handle(KEY_TAB)
    assert ui.screen == "seats" and ui.view == "list"
    assert f"{MARKER} P seat1" in ui.render(100, 24, "")
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
    ui.color = True
    ui.handle(KEY_TAB)
    wide = ui.render(120, 40, "")
    plain = _plain_frame(wide)
    assert SELECTED_BG in wide and "\x1b" not in plain
    _fits(wide, 120, 40)
    assert "HOSTS 1 · global Opportunistic" in plain and " │ studio  Host · Active" in plain
    ui.color = False
    detail = _detail(ui)
    for fact in (
        "Label Studio",
        "Global Opportunistic",
        "Base URL http://127.0.0.1:8080/v1",
        "Lease URL http://127.0.0.1:8002",
        "Timeout 2 s",
        "Groups gpu0 ×1",
        "Seats large, small",
        "Status ◐ not probed · no probe observation",
        "Reason Not probed yet (availability_missing_or_invalid)",
        "Observed not observed · no probe cached yet",
        "Probe busy · Host not ready · requested · finished 5s ago",
        "GPU busy?[2J界",
        "Periodic off (--no-active-probe) · r still probes now",
        "Error worker hiccup?[31m",
    ):
        assert fact in detail, fact
    # A never-probed host is not painted as a degraded probe failure.
    row = _row_line(plain, "H studio")
    assert "◐ not probed" in row and "degraded" not in plain

    narrow = _pages(ui, 40, 14)
    assert f"{MARKER} H studio" in narrow
    assert "Probe busy" in narrow and "Runtime" not in narrow

    ui.handle(KEY_TAB)
    seats = _pages(ui, 48, 14)
    assert f"{MARKER}L coder on studio large" in seats
    detail = _detail(ui)
    for fact in (
        "coder on studio Local · large",
        "Effective Opportunistic",
        "Global Opportunistic",
        "Seat Opportunistic",
        "Host studio · Active · Studio",
        "Group gpu0 · max 1",
        "Slots 1",
        "Model coder",
        "Harness qwen",
        "Shapes execute",
        "Checks strong",
        "Runtime sha256:aaaa",
        "Eligible no · Not probed yet (availability_missing_or_invalid)",
        "Probe busy",
    ):
        assert fact in detail, fact
    assert "\x1b" not in seats


def test_local_empty_registry_keeps_add_and_global_mode_discoverable(tmp_path) -> None:
    settings = _local_settings()
    settings["local_hosts"] = {}
    settings["local_seats"] = {}
    ui, _manager, _refresher = _local_ui(tmp_path, settings=settings)
    ui.handle(KEY_TAB)
    stacked = ui.render(60, 14, "")
    assert f"{MARKER} + Add local host   a" in stacked
    assert "No local hosts yet · press a to add one" not in stacked or True
    assert "a add" in stacked.splitlines()[-1]
    # p has nothing to act on; the reason is shown and nothing else changes.
    assert ui.handle("p") is None and ui.view == "list" and ui.message_kind == "error"

    ui.handle("L")
    assert ui.view == "local-choice"
    assert "Global local mode" in ui.render(60, 12, "")
    ui.handle("esc")
    assert ui.view == "list"

    ui.handle(KEY_TAB)
    assert "+ Add local seat" in ui.render(60, 14, "")
    ui.handle("a")
    assert ui.view == "local-form"
    host_index = next(i for i, f in enumerate(ui.local_form_fields) if f.option == "host")
    ui.local_form_index = host_index
    ui.handle("enter")
    assert ui.view == "local-form" and "Add a local host first" in ui.message


def test_local_off_stale_and_fresh_availability_are_explicit(tmp_path, monkeypatch) -> None:
    _forbid_probes(monkeypatch)
    ui, manager, _refresher = _local_ui(tmp_path, settings=_local_settings(mode="off"))
    ui.handle(KEY_TAB)
    hosts = _plain_frame(ui.render(120, 30, ""))
    assert "– off" in _row_line(hosts, "H studio")
    detail = _detail(ui)
    assert "Global Off" in detail and "Reason Off (mode_off)" in detail
    assert "Status – off · global mode off" in detail
    assert "Observed not read · local mode is off, cache untouched" in detail
    assert "degraded" not in detail
    ui.handle(KEY_TAB)
    seats = _plain_frame(ui.render(120, 30, ""))
    assert "– off" in _row_line(seats, "studio large")
    detail = _detail(ui)
    assert "Effective Off" in detail and "Eligible no · Off (mode_off)" in detail
    assert "Status – off · global mode off" in detail and "degraded" not in detail

    settings = _local_settings()
    _write_local(tmp_path, settings)
    _write_cache(tmp_path, settings, observed_at=time.time() - 120, detail="GPU idle")
    ui._invalidate()
    stale = _detail(ui)
    assert "Eligible no · Probe is stale (availability_stale)" in stale
    # An expired cache entry is stale with its last known state and age.
    assert "Status ◐ stale 2m · last ready" in stale
    assert "Observed 2m ago · expired, last known state ready" in stale
    assert "◐ stale 2m" in _row_line(ui.render(120, 30, ""), "studio large")
    assert "degraded" not in stale
    ui.handle(KEY_BACKTAB)
    stale_hosts = _detail(ui)
    assert "Status ◐ stale 2m · last ready" in stale_hosts
    assert "Reason Probe is stale (availability_stale)" in stale_hosts
    ui.handle(KEY_TAB)

    _write_cache(tmp_path, settings, observed_at=time.time(), detail="GPU idle")
    ui._invalidate()
    fresh = _detail(ui)
    assert "Status ○ ready · GPU idle" in fresh
    assert "Eligible yes · Ready (ready)" in fresh
    assert "Observed 0s ago" in fresh
    assert "○ ready" in _row_line(ui.render(120, 30, ""), "studio large")
    ui.handle(KEY_BACKTAB)
    assert "○ ready" in _row_line(ui.render(120, 30, ""), "H studio")
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
    frame = ui.render(120, 30, "")
    assert "– off" in _row_line(frame, "studio large")
    assert "◐ stale 5m" in _row_line(frame, "studio small")
    gated = _detail(ui)
    assert "Status – off · seat mode off" in gated
    assert "Eligible no · Off (mode_off)" in gated
    assert "Observed 5m ago" in gated and "degraded" not in gated
    ui.handle("j")
    small = _detail(ui)
    assert "Status ◐ stale 5m · last ready" in small
    assert "Observed 5m ago · expired, last known state ready" in small

    settings["providers"]["local"]["mode"] = "off"
    _write_local(tmp_path, settings)
    ui._invalidate()

    def no_cache_io(*_args, **_kwargs):
        pytest.fail("global off must not touch the local runtime cache")

    monkeypatch.setattr(local_seats, "_runtime", no_cache_io)
    off = _detail(ui)
    assert "Status – off · global mode off" in off and "Eligible no · Off (mode_off)" in off
    assert "Observed not read · local mode is off, cache untouched" in off
    assert "stale" not in off and "degraded" not in off
    ui.handle(KEY_BACKTAB)
    hosts = _detail(ui)
    assert "– off" in _row_line(ui.render(120, 30, ""), "H studio")
    assert "Status – off · global mode off" in hosts
    assert "Reason Off (mode_off)" in hosts and "degraded" not in hosts


def test_local_probe_states_render_distinctly_in_manager(tmp_path, monkeypatch) -> None:
    _forbid_probes(monkeypatch)
    settings = _local_settings()
    ui, _manager, _refresher = _local_ui(tmp_path, settings=settings)
    ui.handle(KEY_TAB)
    for state, chip, extra in (
        ("gaming", "◐ gaming", {"detail": "GPU busy with a game"}),
        ("busy", "◐ busy", {}),
        ("loading", "◐ loading", {}),
        ("asleep", "○ asleep", {}),
        ("degraded", "× degraded", {"reason": "probe_timeout"}),
        ("ready", "○ ready", {"model_profile": "coder"}),
    ):
        _write_cache(tmp_path, settings, observed_at=time.time() - 2, state=state, **extra)
        ui._invalidate()
        assert chip in _row_line(ui.render(120, 30, ""), "H studio"), state
        detail = _detail(ui)
        assert f"Status {chip}" in detail, state
        assert "Observed 2s ago" in detail, state
        if state == "gaming":
            assert "GPU busy with a game" in detail
        if state == "degraded":
            assert "Status × degraded · probe_timeout" in detail
        if state == "ready":
            assert "Serving coder" in detail
    ui.handle(KEY_TAB)
    assert "○ ready" in _row_line(ui.render(120, 30, ""), "studio large")

    # A served model that does not match the seat's profile is a mismatch.
    _write_cache(tmp_path, settings, observed_at=time.time() - 2, model_profile="other")
    ui._invalidate()
    assert "× model mismatch" in _row_line(ui.render(140, 30, ""), "studio large")
    assert "Status × model mismatch · serving other" in _detail(ui)


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
    frame = ui.render(120, 30, "")
    assert calls and calls[0][0] == tmp_path
    assert "● working" in _row_line(frame, "H studio")
    # The shared derivation strips control bytes from the job label.
    assert "Status ● working · small: local live work[31m" in _detail(ui)
    assert "\x1b" not in frame
    ui.handle(KEY_TAB)
    frame = ui.render(120, 30, "")
    assert "○ ready" in _row_line(frame, "studio large")
    assert "● local live" in _row_line(frame, "studio small")
    ui.handle("j")
    small = _detail(ui)
    assert "Status ● working · local live work[31m" in small
    assert "Observed 1s ago" in small and "\x1b" not in small

    # A dispatch read failure never fabricates or hides a state; it is shown.
    def broken(root, loaded, *, now=None):
        raise DispatchError("queue unreadable\x1b[2J")

    monkeypatch.setattr(dispatch_client, "live_jobs", broken)
    ui._invalidate()
    failed = _detail(ui)
    assert "working" not in failed
    assert "Status ○ ready · GPU idle" in failed
    assert "Live jobs unreadable · queue unreadable?[2J" in failed


def test_local_configuration_changed_is_unknown_not_a_probe_failure(tmp_path, monkeypatch) -> None:
    _forbid_probes(monkeypatch)
    settings = _local_settings()
    ui, _manager, _refresher = _local_ui(tmp_path, settings=settings)
    _write_cache(tmp_path, settings, observed_at=time.time(), digest="0" * 64)
    ui.handle(KEY_TAB)
    assert "◐ not probed" in _row_line(ui.render(120, 30, ""), "H studio")
    hosts = _detail(ui)
    assert "Status ◐ not probed · configuration changed" in hosts
    assert "Reason Configuration changed (configuration_changed)" in hosts
    assert "Observed not observed · configuration changed" in hosts
    assert "degraded" not in hosts
    ui.handle(KEY_TAB)
    seats = _detail(ui)
    assert "Eligible no · Configuration changed (configuration_changed)" in seats
    assert "Status ◐ not probed · configuration changed" in seats
    assert "degraded" not in seats


def test_probe_now_queues_on_refresher_and_never_runs_inline(tmp_path, monkeypatch) -> None:
    _human_terminal(monkeypatch)
    _forbid_probes(monkeypatch)
    ui, _manager, refresher = _local_ui(tmp_path)
    ui.handle(KEY_TAB)
    assert ui.poll() is True  # first snapshot observation marks the frame dirty
    assert ui.poll() is False

    ui.handle("r")
    assert refresher.requests == ["studio"]
    assert ui.view == "list" and ui.message.startswith("↻ Probe queued for studio")
    assert ui.poll() is True  # pending changed on the snapshot
    assert "Probe queued" in _detail(ui)

    ui.handle("enter")
    assert ui.view == "local-actions"
    actions = ui.render(80, 16, "")
    assert f"{MARKER} r Probe now" in actions
    ui.handle("enter")
    assert refresher.requests == ["studio", "studio"]
    assert ui.view == "local-actions"

    ui.handle("esc")
    ui.handle(KEY_TAB)
    ui.handle("r")
    assert refresher.requests == ["studio", "studio", "studio"]
    assert "Probe queued for studio" in ui.message

    closed_ui, _m, _r = _local_ui(tmp_path, refresher=_LocalRefresher(closed=True))
    closed_ui.handle(KEY_TAB)
    closed_ui.handle("r")
    assert "Cannot probe studio" in closed_ui.message and "closed" in closed_ui.message

    no_refresher_ui, _m, _r = _local_ui(tmp_path, refresher="none")
    no_refresher_ui.handle(KEY_TAB)
    no_refresher_ui.handle("r")
    assert "no active refresher" in no_refresher_ui.message
    assert "Probe unavailable · no probe service" in _detail(no_refresher_ui)


def test_global_and_seat_mode_apply_at_once_and_esc_writes_nothing(tmp_path, monkeypatch) -> None:
    _human_terminal(monkeypatch)
    _forbid_probes(monkeypatch)
    ui, _manager, _refresher = _local_ui(tmp_path)
    path = tmp_path / "settings.json"
    original = path.read_bytes()
    ui.handle(KEY_TAB)

    ui.handle("L")
    assert ui.view == "local-choice"
    assert ui.choice_items[ui.choice_index][1] == "opportunistic"
    assert [value for _label, value in ui.choice_items] == list(LOCAL_MODE_ORDER)
    assert ui.choice_items[ui.choice_index][0] == "Opportunistic (current)"
    ui.handle("esc")
    assert ui.view == "list" and path.read_bytes() == original

    ui.handle("L")
    _choose(ui, "shadow")
    assert ui.view == "list"
    assert ui.message == "✓ Global local mode → Shadow"
    assert json.loads(path.read_text())["providers"]["local"]["mode"] == "shadow"
    assert "HOSTS 1 · global Shadow" in ui.render(100, 20, "")

    ui.handle(KEY_TAB)
    ui.handle("j")
    assert ui.local_selected_id == "small"
    ui.handle("enter")
    labels = [action.label for action in ui._menu_actions()]
    ui.menu_index = labels.index("Set seat mode…")
    ui.handle("enter")
    assert ui.view == "local-choice" and "Seat mode · small" in ui.render(60, 12, "")
    _choose(ui, "off")
    assert ui.view == "local-actions"
    assert ui.message == "✓ small mode → Off"
    current = json.loads(path.read_text())
    assert current["local_seats"]["small"]["mode"] == "off"
    assert current["local_seats"]["large"]["mode"] == "opportunistic"
    ui.handle("esc")
    assert "Seat Off" in _detail(ui)


def test_local_pause_resume_and_lifecycle_surface_core_errors(tmp_path, monkeypatch) -> None:
    _human_terminal(monkeypatch)
    ui, _manager, _refresher = _local_ui(tmp_path)
    path = tmp_path / "settings.json"
    ui.handle(KEY_TAB)
    # Pausing the host while its seats are active is refused by the core.
    ui.handle("p")
    assert ui.message_kind == "error" and "non-active local host" in ui.message
    ui.handle("enter")
    labels = [action.label for action in ui._menu_actions()]
    ui.menu_index = labels.index("Set lifecycle…")
    ui.handle("enter")
    assert ui.view == "local-choice" and "Host lifecycle · studio" in ui.render(60, 12, "")
    assert [value for _label, value in ui.choice_items] == ["active", "draining", "inactive"]
    ui.handle("esc")
    ui.handle("esc")

    ui.handle(KEY_TAB)
    for seat_id in ("large", "small"):
        ui.local_selected["local-seats"] = seat_id
        ui.handle("p")
        assert ui.message == f"✓ Paused {seat_id} · new local work skips it · p resumes"
    current = json.loads(path.read_text())
    assert {seat["lifecycle"] for seat in current["local_seats"].values()} == {"inactive"}
    assert {seat["mode"] for seat in current["local_seats"].values()} == {"off"}
    ui.handle("p")
    assert ui.message.startswith("✓ Resumed small · its mode is Off")
    assert json.loads(path.read_text())["local_seats"]["small"]["lifecycle"] == "active"
    ui.handle("p")

    ui.handle(KEY_BACKTAB)
    ui.handle("p")
    assert ui.message == "✓ Paused studio · new local work skips it · p resumes"
    ui.handle("enter")
    labels = [action.label for action in ui._menu_actions()]
    ui.menu_index = labels.index("Set lifecycle…")
    ui.handle("enter")
    assert [value for _label, value in ui.choice_items] == ["active", "inactive", "retired"]
    _choose(ui, "retired")
    assert json.loads(path.read_text())["local_hosts"]["studio"]["lifecycle"] == "retired"
    assert ui.message == "✓ Host studio → Retired"
    ui.handle("esc")
    # A retired host is history: hidden until h, with nothing left to change.
    assert "H studio" not in ui.render(100, 20, "")
    ui.handle("h")
    assert "Retired" in _row_line(ui.render(100, 20, ""), "H studio")
    ui.handle("k")
    assert ui.local_selected_id == "studio"
    ui.handle("p")
    assert ui.message_kind == "error" and "retired" in ui.message


def test_add_host_form_validates_fields_and_saves_on_submit(tmp_path, monkeypatch) -> None:
    _human_terminal(monkeypatch)
    ui, _manager, _refresher = _local_ui(tmp_path)
    path = tmp_path / "settings.json"
    ui.handle(KEY_TAB)
    ui.handle("a")
    assert ui.view == "local-form"
    form = ui.render(90, 20, "")
    assert "Add local host · new host" in form and "required" in form
    assert "→ Save host" in form
    assert [field.option for field in ui.local_form_fields] == [
        field.option for field in HOST_FORM_FIELDS
    ]

    _set_field(ui, HOST_FORM_FIELDS, "id", "other")
    _set_field(ui, HOST_FORM_FIELDS, "label", "Other box")
    _set_field(ui, HOST_FORM_FIELDS, "base_url", "http://user:pw@10.0.0.5/v1")
    ui.local_form_index = len(ui.local_form_fields) - 1
    ui.handle("enter")
    assert ui.view == "local-form" and ui.message.startswith("× Cannot save")
    assert "base_url must be an HTTP(S) URL without credentials" in ui.message
    assert ui.local_form_buffers["id"] == "other"
    assert "other" not in json.loads(path.read_text())["local_hosts"]

    _set_field(ui, HOST_FORM_FIELDS, "base_url", "http://10.0.0.5:8000/v1")
    _set_field(ui, HOST_FORM_FIELDS, "resource_groups", "{")
    ui.local_form_index = len(ui.local_form_fields) - 1
    ui.handle("enter")
    assert "resource_groups is not valid JSON" in ui.message
    _set_field(ui, HOST_FORM_FIELDS, "resource_groups", '{"gpu0": {"max_concurrency": 2}}')
    _set_field(
        ui,
        HOST_FORM_FIELDS,
        "availability_probe",
        json.dumps({"argv": ["/usr/local/bin/probe", "http://10.0.0.5:8002/status"], "timeout_seconds": 3}),
    )
    ui.local_form_index = next(
        i for i, f in enumerate(ui.local_form_fields) if f.option == "lifecycle"
    )
    ui.handle("enter")
    assert ui.view == "local-choice" and "lifecycle" in ui.render(60, 12, "")
    _choose(ui, "active")
    assert ui.view == "local-form" and ui.local_form_buffers["lifecycle"] == "active"

    ui.local_form_index = len(ui.local_form_fields) - 1
    ui.handle("enter")
    assert ui.view == "list" and ui.message.startswith("✓ Added local host other")
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
    assert f"{MARKER} H other" in ui.render(100, 26, "")


def test_add_seat_form_uses_host_choices_and_fingerprint_validation(
    tmp_path, monkeypatch
) -> None:
    _human_terminal(monkeypatch)
    ui, _manager, _refresher = _local_ui(tmp_path)
    path = tmp_path / "settings.json"
    ui.handle(KEY_TAB)
    ui.handle("enter")
    labels = [action.label for action in ui._menu_actions()]
    ui.menu_index = labels.index("Add local seat on this host")
    ui.handle("enter")
    assert ui.view == "local-form" and ui.local_form_action == "add-seat"
    assert ui.local_form_buffers["host"] == "studio"
    assert ui.local_form_buffers["resource_group"] == "gpu0"
    assert [field.option for field in ui.local_form_fields] == [
        field.option for field in SEAT_FORM_FIELDS
    ]
    form = ui.render(100, 24, "")
    assert "Add local seat · new seat" in form
    assert '{"task_shapes": ["execute"]' in form

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
    assert ui.view == "local-form"
    assert "runtime_fingerprint must be sha256 followed by 64 lowercase hex" in ui.message
    _set_field(ui, SEAT_FORM_FIELDS, "runtime_fingerprint", "sha256:" + "b" * 64)
    _set_field(ui, SEAT_FORM_FIELDS, "max_concurrency", "two")
    ui.local_form_index = len(ui.local_form_fields) - 1
    ui.handle("enter")
    assert "max_concurrency must be an integer" in ui.message
    _set_field(ui, SEAT_FORM_FIELDS, "max_concurrency", "1")
    _set_field(ui, SEAT_FORM_FIELDS, "context_window_size", "180224")

    ui.local_form_index = len(ui.local_form_fields) - 1
    ui.handle("enter")
    assert ui.message.startswith("✓ Added local seat third")
    # The form returns to the host list it was opened from; the new seat is
    # selected on Local seats.
    assert ui.screen == "hosts" and ui.view == "list"
    ui.handle(KEY_TAB)
    assert ui.screen == "local-seats" and ui.local_selected_id == "third"
    seat = json.loads(path.read_text())["local_seats"]["third"]
    assert seat["model_profile"] == "qwen3.8-27b"
    assert seat["context_window_size"] == 180224
    assert seat["runtime_fingerprint"] == "sha256:" + "b" * 64
    assert seat["capabilities"] == {"task_shapes": ["execute"], "validation": ["strong"]}
    assert seat["lifecycle"] == "active" and seat["mode"] == "canary"
    frame = ui.render(120, 30, "")
    assert "◐ not probed" in _row_line(frame, "L qwen3.8-27b on studio")
    assert "Effective Canary" in _detail(ui)


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
    ui.handle("e")
    assert ui.view == "local-form" and ui.local_form_action == "edit-host"
    assert "id" not in ui.local_form_buffers and "lifecycle" not in ui.local_form_buffers
    frame = _squash(ui.render(100, 20, ""))
    assert "Edit local host · studio" in frame and "→ Save host" in frame
    assert "label Studio" in frame and "lease_url (blank = none) http://127.0.0.1:8002" in frame
    ui.local_form_index = len(ui.local_form_fields) - 1
    ui.handle("enter")
    assert ui.view == "local-form" and "No changes to save" in ui.message

    _set_field(ui, HOST_FORM_FIELDS, "label", "Renamed")
    _set_field(ui, HOST_FORM_FIELDS, "lease_url", "")
    ui.local_form_index = len(ui.local_form_fields) - 1
    ui.handle("enter")
    assert previews[-1] == ("host", {"label": "Renamed", "lease_url": None})
    assert ui.view == "list" and ui.message == "✓ Saved studio"
    host = json.loads(path.read_text())["local_hosts"]["studio"]
    assert host["label"] == "Renamed" and "lease_url" not in host
    assert host["base_url"] == "http://127.0.0.1:8080/v1"

    ui.handle(KEY_TAB)
    ui.handle("e")
    assert ui.local_form_action == "edit-seat"
    assert "mode" not in ui.local_form_buffers
    _set_field(ui, SEAT_FORM_FIELDS, "max_concurrency", "2")
    ui.local_form_index = len(ui.local_form_fields) - 1
    ui.handle("enter")
    assert "must not exceed its resource group maximum" in ui.message
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
    assert ui.message == "✓ Saved large"
    seat = json.loads(path.read_text())["local_seats"]["large"]
    assert seat["label"] == "Big coder" and seat["max_concurrency"] == 1
    assert seat["context_window_size"] == 32768
    assert seat["capabilities"]["task_shapes"] == ["execute", "investigate"]
    assert json.loads(path.read_text())["local_seats"]["small"]["label"] == "Coder"


def test_local_change_reports_a_stale_preview_without_writing(tmp_path, monkeypatch) -> None:
    _human_terminal(monkeypatch)
    ui, manager, _refresher = _local_ui(tmp_path)
    path = tmp_path / "settings.json"
    real_preview = manager.local.preview_global_mode

    def racing_preview(mode):
        preview = real_preview(mode)
        local_cli.apply_action(tmp_path, "set-seat-mode", "large", mode="off", expected_digest=None)
        return preview

    manager.local.preview_global_mode = racing_preview  # type: ignore[method-assign]
    ui.handle(KEY_TAB)
    ui.handle("L")
    before_choice = path.read_bytes()
    _choose(ui, "shadow")
    assert ui.message_kind == "error"
    assert "changed since this local action was previewed" in ui.message
    written = json.loads(path.read_text())
    assert written["providers"]["local"]["mode"] == "opportunistic"
    assert written["local_seats"]["large"]["mode"] == "off"
    assert path.read_bytes() != before_choice
    assert ui.view == "local-choice"


@pytest.mark.parametrize(
    "environment",
    [{"CODEX_SEATS_MANAGED_WORKER": "1"}, {"CLAUDE_LEAD_WORKER": "1"}],
)
def test_local_mutations_and_probes_are_refused_for_managed_workers(
    tmp_path, monkeypatch, environment
) -> None:
    _human_terminal(monkeypatch)
    _forbid_probes(monkeypatch)
    ui, _manager, refresher = _local_ui(tmp_path)
    path = tmp_path / "settings.json"
    original = path.read_bytes()
    ui.handle(KEY_TAB)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    ui.handle("L")
    _choose(ui, "shadow")
    assert ui.message_kind == "error" and "refused" in ui.message
    assert path.read_bytes() == original
    ui.handle("esc")
    ui.handle("r")
    assert "Cannot probe studio" in ui.message and "refused" in ui.message
    assert refresher.requests == []
    # Reads stay available: the list still renders the registry.
    assert f"{MARKER} H studio" in ui.render(100, 26, "")


def test_local_writes_apply_in_a_head_context(tmp_path, monkeypatch) -> None:
    _human_terminal(monkeypatch)
    ui, _manager, _refresher = _local_ui(tmp_path)
    path = tmp_path / "settings.json"
    original = path.read_bytes()
    ui.handle(KEY_TAB)
    monkeypatch.setenv("CODEX_SEATS_HEAD", "claude")
    ui.handle("L")
    _choose(ui, "shadow")
    assert "refused" not in ui.message
    assert path.read_bytes() != original
    assert json.loads(path.read_text())["providers"]["local"]["mode"] == "shadow"


def test_local_screens_keep_minimum_frame_and_sanitized_highlight(tmp_path) -> None:
    ui, _manager, _refresher = _local_ui(tmp_path)
    ui.color = True
    ui.handle(KEY_TAB)

    def check(frame: str) -> None:
        _fits(frame, 24, 6)
        assert "\x1b" not in _plain_frame(frame)
        assert "Esc" in _plain_frame(frame).splitlines()[-1] or "✓" in frame or "↻" in frame

    check(ui.render(24, 6, ""))
    assert f"{MARKER} H studio" in _plain_frame(ui.render(24, 6, ""))
    ui.handle("enter")
    check(ui.render(24, 6, ""))
    ui.handle("esc")
    ui.handle("L")
    assert ui.view == "local-choice"
    check(ui.render(24, 6, ""))
    ui.handle("esc")
    ui.handle("a")
    frame = ui.render(24, 6, "")
    check(frame)
    assert SELECTED_BG in frame
    ui.handle("enter")
    ui.input_buffer = "studio-two\x1b[2J界"
    frame = ui.render(24, 6, "")
    check(frame)
    assert "?[2J界" in _plain_frame(frame)


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
    output = _read_until(master, b"Inventory")
    output += _read_until(master, b"Add seat")
    assert b"studio" in output and b"claude" in output
    os.write(master, b"\t")
    output += _read_until(master, "▸ Codex seats".encode())
    os.write(master, b"\t")
    output += _read_until(master, "▸ Local hosts".encode())
    assert b"studio" in output
    os.write(master, b"r")
    output += _read_until(master, b"Probe queued for studio")
    # The probe runs on the refresher worker even with --no-active-probe;
    # the detail re-renders with its "requested" result.
    output += _read_until(master, b"requested", timeout=15)
    assert b"requested" in output
    os.write(master, b"\r")
    output += _read_until(master, b"Host actions")
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


def test_real_pty_local_global_mode_applies_on_choice_and_esc_writes_nothing(tmp_path: Path) -> None:
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
    output = _read_until(master, b"Inventory")
    os.write(master, b"\t")
    output += _read_until(master, "▸ Codex seats".encode())
    os.write(master, b"\t")
    output += _read_until(master, "▸ Local hosts".encode())
    os.write(master, b"L")
    output += _read_until(master, b"Global local mode")
    os.write(master, b"\x1b")
    time.sleep(0.3)
    assert path.read_bytes() == original

    os.write(master, b"L")
    output += _read_until(master, b"Global local mode")
    os.write(master, b"j\r")
    output += _read_until(master, "Global local mode → Shadow".encode())
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


def _select(ui: ManagerUI, key: str) -> None:
    keys = [row.key for row in ui._rows() if row.selectable]
    assert key in keys, keys
    for _ in range(len(keys)):
        if ui._selected_row().key == key:
            return
        ui.handle("down")
    raise AssertionError(f"could not select {key}")


def test_inventory_lists_every_provider_under_group_headers(tmp_path, monkeypatch) -> None:
    settings = _local_settings()
    settings["providers"]["claude"]["subscription_tier"] = "Max 5x"
    settings["providers"]["claude"]["small_task_min_headroom_pct"] = 12
    settings["providers"]["claude"]["big_task_min_headroom_pct"] = 25
    ui, _manager = _inventory_ui(tmp_path, monkeypatch, settings=settings)

    rows = ui._rows()
    assert [row.key for row in rows] == [
        "group:codex", "codex:seat1", "codex:seat2",
        "group:claude", "claude:account",
        "group:local", "local-seat:large", "local-seat:small",
        "add:seat",
    ]
    wide = ui.render(120, 30, "")
    _fits(wide, 120, 30)
    assert "▸ Inventory" in wide
    assert "CODEX 2 · primary seat1 is used last" in wide
    assert "CLAUDE 1 · Off" in wide
    assert "LOCAL 2 · global Opportunistic" in wide
    assert f"{MARKER} P seat1 Standard 1x" in wide
    assert "  W seat2 Unverified" in wide and "– paused" in _row_line(wide, "W seat2")
    assert "  C claude Max 5x" in wide and "– off" in _row_line(wide, "C claude")
    assert "L coder on studio large" in wide and "L coder on studio small" in wide
    # A host is placement for local seats, never a seat row of its own.
    assert "H studio" not in wide
    assert wide.count("+ Add seat") == 1
    assert "\x1b" not in wide

    _select(ui, "claude:account")
    stacked = ui.render(84, 30, "")
    assert " │ " not in stacked
    assert "── CLAUDE · MAX 5X" in stacked
    claude = _detail(ui)
    assert "Floors small 12% · big 25% left" in claude and "Tier Max 5x · configured" in claude

    _select(ui, "add:seat")
    prose = " ".join(ui.render(84, 30, "").split())
    assert "chooses the seat type" in prose and "registered host (studio)" in prose
    assert "one Claude account" in prose


def test_inventory_enter_opens_each_rows_controls_and_esc_returns(tmp_path, monkeypatch) -> None:
    ui, _manager = _inventory_ui(tmp_path, monkeypatch)

    _select(ui, "local-seat:small")
    ui.handle("enter")
    assert ui.screen == "local-seats" and ui.view == "local-actions"
    assert ui.local_selected_id == "small"
    actions = ui.render(110, 30, "")
    assert "Local seat actions · small" in actions
    assert "Remove seat…" in actions and "pause it first (lifecycle inactive, mode off)" in actions
    assert "Manage this seat's host" in actions
    assert actions.splitlines()[-1].endswith("Esc back")
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.view == "list"
    assert ui.inventory_selected == "local-seat:small"

    # The host stays reachable from the selected local seat's actions, and
    # Esc walks back through each step.
    ui.handle("enter")
    labels = [action.label for action in ui._menu_actions()]
    ui.menu_index = labels.index("Manage this seat's host")
    ui.handle("enter")
    assert ui.screen == "hosts" and ui.view == "local-actions"
    assert ui.local_selected_id == "studio"
    assert "Host actions · studio" in ui.render(110, 30, "")
    ui.handle("esc")
    assert ui.screen == "local-seats" and ui.view == "local-actions"
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.inventory_selected == "local-seat:small"

    # Enter (or e) on the Claude row opens the Claude screen; Esc comes back.
    _select(ui, "claude:account")
    ui.handle("enter")
    assert ui.screen == "claude" and ui.view == "list"
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.inventory_selected == "claude:account"
    ui.handle("e")
    assert ui.screen == "claude"
    ui.handle("esc")
    ui.handle("p")
    assert ui.message_kind == "error" and "Claude screen" in ui.message

    _select(ui, "codex:seat2")
    ui.handle("enter")
    assert ui.screen == "inventory" and ui.view == "actions" and ui.selected_id == "seat2"
    assert "Seat actions · seat2" in ui.render(110, 30, "")
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.view == "list"
    assert ui.inventory_selected == "codex:seat2"

    # Exactly one Add seat entry point: Enter on the row or a opens the type menu.
    _select(ui, "add:seat")
    ui.handle("enter")
    assert ui.screen == "inventory" and ui.view == "local-choice"
    menu = ui.render(80, 14, "")
    assert "Add seat · choose the seat type" in menu
    assert [value for _label, value in ui.choice_items] == ["add:codex", "add:claude", "add:local"]
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.view == "list"
    assert ui.inventory_selected == "add:seat"

    ui.handle("a")
    _choose(ui, "add:codex")
    assert ui.screen == "inventory" and ui.view == "add-form"
    assert "Add Codex seat · starts paused" in ui.render(80, 14, "")
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.view == "list"

    ui.handle("a")
    _choose(ui, "add:claude")
    assert ui.screen == "claude" and ui.view == "list"
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.view == "list"

    ui.handle("a")
    _choose(ui, "add:local")
    assert ui.screen == "local-seats" and ui.view == "local-form"
    assert ui.local_form_action == "add-seat"
    assert ui.local_form_buffers["host"] == "studio"
    assert "Add local seat" in ui.render(110, 30, "")
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.view == "list"


def test_inventory_direct_keys_on_a_local_row_act_on_the_local_seat(tmp_path, monkeypatch) -> None:
    _human_terminal(monkeypatch)
    ui, manager = _inventory_ui(tmp_path, monkeypatch)
    path = tmp_path / "settings.json"
    _select(ui, "local-seat:large")
    ui.handle("r")
    assert manager.local.refresher.requests == ["studio"]
    ui.handle("c")
    assert ui.message == "× Redeem reset applies to Codex seats only"
    ui.handle("p")
    assert ui.message == "✓ Paused large · new local work skips it · p resumes"
    assert json.loads(path.read_text())["local_seats"]["large"]["lifecycle"] == "inactive"
    assert ui.screen == "inventory" and ui.view == "list"


def test_inventory_shows_one_local_seat_row_for_one_host_and_one_seat(
    tmp_path, monkeypatch
) -> None:
    """The live registry shape: one host, one seat -> one local row, no host row."""
    settings = _local_settings()
    settings["local_seats"] = {"studio-qwen3-8-27b": settings["local_seats"]["small"]}
    settings["local_seats"]["studio-qwen3-8-27b"]["label"] = "Qwen3 8-27B"
    ui, _manager = _inventory_ui(tmp_path, monkeypatch, settings=settings)
    rows = [row for row in ui._rows() if row.selectable]
    assert [row.kind for row in rows] == ["codex", "codex", "claude", "local", "add"]
    frame = ui.render(120, 30, "")
    assert "LOCAL 1 · global Opportunistic" in frame
    assert frame.count("+ Add seat") == 1
    # Columns stay aligned with the header for every row.
    lines = frame.splitlines()
    header = next(line for line in lines if "STATUS" in line)
    local = next(line for line in lines if "L coder on studio" in line)
    assert local.index("◐ not probed") == header.index("STATUS")
    # The host is one Tab away and named in the seat's placement detail.
    _select(ui, "local-seat:studio-qwen3-8-27b")
    assert "Host studio · Active · Studio" in _detail(ui)
    ui.handle(KEY_TAB)
    ui.handle(KEY_TAB)
    assert ui.screen == "hosts" and f"{MARKER} H studio" in ui.render(100, 24, "")


def test_add_seat_type_menu_offers_host_first_path_when_no_host_is_registered(
    tmp_path, monkeypatch
) -> None:
    settings = _local_settings()
    settings["local_hosts"] = {}
    settings["local_seats"] = {}
    ui, _manager = _inventory_ui(tmp_path, monkeypatch, settings=settings)
    assert [row.kind for row in ui._rows() if row.selectable] == ["codex", "codex", "claude", "add"]
    _select(ui, "add:seat")
    assert "none is registered yet" in " ".join(ui.render(84, 30, "").split())
    ui.handle("a")
    _choose(ui, "add:local")
    assert ui.screen == "inventory" and ui.view == "local-choice"
    assert "Add local seat · no local host is registered yet" in ui.render(90, 14, "")
    assert [value for _label, value in ui.choice_items] == ["add:host", "hosts"]
    # Esc returns to the seat type menu, with its choices and selection intact.
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
    assert ui.view == "local-form"
    ui.handle("esc")
    assert ui.screen == "hosts" and ui.view == "list"
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.view == "list"

    ui.handle("a")
    _choose(ui, "add:local")
    _choose(ui, "add:host")
    assert ui.screen == "hosts" and ui.view == "local-form"
    assert ui.local_form_action == "add-host"
    assert "Add local host" in ui.render(110, 30, "")
    ui.handle("esc")
    assert ui.screen == "inventory" and ui.view == "list"


def test_back_stack_tab_and_overview_escape(tmp_path, monkeypatch) -> None:
    ui, _manager = _inventory_ui(tmp_path, monkeypatch)
    ui.handle(KEY_TAB)
    assert ui.screen == "seats"
    ui.handle("enter")
    assert ui.view == "actions"
    ui.handle("esc")
    assert ui.screen == "seats" and ui.view == "list"
    ui.handle("esc")
    assert ui.view == "overview"

    ui.handle("m")
    _select(ui, "codex:seat1")
    ui.handle("enter")
    ui.handle("?")
    ui.handle("esc")
    assert ui.view == "actions"
    # Tab from a list starts a fresh stack: Esc then returns to the overview.
    ui.handle("esc")
    ui.handle(KEY_TAB)
    ui.handle(KEY_TAB)
    assert ui.screen == "hosts"
    ui.handle("enter")
    ui.handle("esc")
    assert ui.screen == "hosts" and ui.view == "list"
    ui.handle("esc")
    assert ui.view == "overview"
    ui.handle("g")
    assert ui.view == "global-settings"
    ui.handle("esc")
    assert ui.view == "overview"


def test_inventory_hides_retired_seats_until_history(tmp_path, monkeypatch) -> None:
    settings = _local_settings()
    settings["local_seats"]["large"]["lifecycle"] = "retired"
    settings["local_seats"]["large"]["mode"] = "off"
    ui, manager = _inventory_ui(tmp_path, monkeypatch, settings=settings)
    manager.settings["pool"]["seats"]["seat2"]["lifecycle"] = "retired"
    ui._invalidate()

    ordinary = ui.render(100, 24, "")
    assert "CODEX 1" in ordinary and "LOCAL 1" in ordinary
    assert "studio large" not in ordinary and "seat2" not in ordinary
    ui.handle("h")
    history = ui.render(100, 24, "")
    assert "CODEX 2" in history and "LOCAL 2" in history
    assert "– retired" in _row_line(history, "W seat2")
    assert "studio large" in history

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


def test_claude_screen_lists_settings_and_applies_edits_at_once(tmp_path, monkeypatch) -> None:
    _human_terminal(monkeypatch)
    settings = _local_settings()
    settings["providers"]["claude"]["subscription_tier"] = "Max 5x"
    settings["providers"]["claude"]["small_task_min_headroom_pct"] = 12
    settings["providers"]["claude"]["big_task_min_headroom_pct"] = 25
    ui, manager = _inventory_ui(tmp_path, monkeypatch, settings=settings)
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

    ui.handle(KEY_BACKTAB)
    assert ui.screen == "claude" and ui.view == "list"
    frame = _squash(ui.render(120, 30, ""))
    for row in ("Mode Off", "Subscription tier Max 5x", "Small-task floor 12% left",
                "Big-task floor 25% left", "Active /usage refresh Enabled"):
        assert row in frame, row
    assert [row.key for row in ui._rows() if row.selectable] == [
        f"setting:{action}" for action, _label in CLAUDE_SETTINGS
    ]
    detail = _detail(ui)
    assert "Provider cooldown" in detail and "raw provider text" not in detail
    assert "Mode Off" in detail and "Tier Max 5x · configured" in detail
    assert "usage cache not read" in detail
    assert "Off stops routing to Claude" in detail

    # Mode: Esc from the choice writes nothing; choosing applies at once.
    ui.handle("enter")
    assert ui.view == "local-choice" and "Claude mode" in ui.render(80, 12, "")
    assert [value for _label, value in ui.choice_items] == list(CLAUDE_MODE_ORDER)
    assert ui.choice_items[ui.choice_index] == ("Off (current)", "off")
    ui.handle("esc")
    assert ui.view == "list" and path.read_bytes() == original
    ui.handle("enter")
    _choose(ui, "review_only")
    # Once Claude is on, the report may read the passive usage cache (a file),
    # never the provider client.
    monkeypatch.setattr(state_module, "load_claude_rate_limits", passive_cache)
    assert ui.view == "list"
    assert ui.message == "✓ Claude mode → Reviews only · Claude may now take routed work"
    assert manager.settings["providers"]["claude"]["mode"] == "review_only"
    written = json.loads(path.read_text())
    assert written["providers"]["claude"]["mode"] == "review_only"
    assert written["providers"]["claude"]["subscription_tier"] == "Max 5x"
    assert written["local_hosts"]["studio"]["label"] == "Studio"
    assert "Mode Reviews only" in _squash(ui.render(120, 30, ""))

    # Tier label choice offers every known plan plus clearing it.
    ui.handle("down")
    ui.handle("enter")
    assert "Claude subscription tier" in ui.render(80, 14, "")
    labels = [label for label, _value in ui.choice_items]
    assert "Max 5x (current)" in labels and "Max 20x" in labels and "Pro" in labels
    assert ui.choice_items[-1] == (CLAUDE_TIER_CLEAR_LABEL, None)
    _choose(ui, "Max 20x")
    assert ui.message == "✓ Claude tier → Max 20x"
    assert json.loads(path.read_text())["providers"]["claude"]["subscription_tier"] == "Max 20x"

    # Headroom floors are typed and validated by the manager.
    ui.handle("down")
    ui.handle("enter")
    assert ui.view == "claude-input" and ui.input_buffer == "12"
    assert ui.handle("q") is None and ui.input_buffer == "12q"
    for _ in range(3):
        ui.handle("backspace")
    _type(ui, "8")
    ui.handle("enter")
    assert ui.view == "list" and ui.message == "✓ Claude small-task floor → 8% left"
    assert json.loads(path.read_text())["providers"]["claude"]["small_task_min_headroom_pct"] == 8
    ui.handle("down")
    ui.handle("enter")
    assert ui.view == "claude-input" and ui.input_buffer == "25"
    ui.input_buffer = "5"
    ui.handle("enter")
    assert ui.view == "claude-input"
    assert ui.message.startswith("× Invalid big-task floor") and "must not exceed" in ui.message
    ui.input_buffer = "abc"
    ui.handle("enter")
    assert ui.view == "claude-input" and "Invalid big-task floor" in ui.message
    assert json.loads(path.read_text())["providers"]["claude"]["big_task_min_headroom_pct"] == 25
    ui.handle("esc")
    ui.handle("down")
    ui.handle("enter")
    assert ui.message == "✓ Active /usage refresh → Disabled"
    assert json.loads(path.read_text())["providers"]["claude"]["active_usage_refresh"] is False


def test_claude_edits_refuse_stale_previews_and_managed_workers(
    tmp_path, monkeypatch
) -> None:
    _human_terminal(monkeypatch)
    ui, manager = _inventory_ui(tmp_path, monkeypatch)
    path = tmp_path / "settings.json"
    real_preview = manager.claude.preview_mode

    def racing_preview(mode):
        preview = real_preview(mode)
        local_cli.apply_action(tmp_path, "set-seat-mode", "large", mode="off", expected_digest=None)
        return preview

    manager.claude.preview_mode = racing_preview  # type: ignore[method-assign]
    ui.handle(KEY_BACKTAB)
    assert ui.screen == "claude"
    ui.handle("enter")
    _choose(ui, "opportunistic")
    assert ui.message_kind == "error" and "re-preview" in ui.message
    before = path.read_bytes()
    assert json.loads(before)["providers"]["claude"]["mode"] == "off"
    assert ui.view == "local-choice"

    manager.claude.preview_mode = real_preview  # type: ignore[method-assign]
    monkeypatch.setenv("CODEX_SEATS_MANAGED_WORKER", "1")
    _choose(ui, "opportunistic")
    assert ui.message_kind == "error" and "refused" in ui.message
    assert path.read_bytes() == before


def test_remove_local_seat_requires_inactive_off_and_confirms_in_a_dialog(
    tmp_path, monkeypatch
) -> None:
    _human_terminal(monkeypatch)
    ui, _manager = _inventory_ui(tmp_path, monkeypatch)
    path = tmp_path / "settings.json"
    original = path.read_bytes()

    _select(ui, "local-seat:small")
    ui.handle("enter")
    labels = [action.label for action in ui._menu_actions()]
    remove_index = labels.index("Remove seat…")
    ui.menu_index = remove_index
    ui.handle("enter")
    assert ui.view == "local-actions"
    assert ui.message == "× Remove seat: pause it first (lifecycle inactive, mode off)"
    assert path.read_bytes() == original
    ui.handle("esc")

    local_cli.apply_action(
        tmp_path, "set-seat-lifecycle", "large", lifecycle="inactive", expected_digest=None
    )
    ui._invalidate()
    _select(ui, "local-seat:large")
    ui.handle("enter")
    ui.menu_index = remove_index
    ui.handle("enter")
    assert ui.view == "confirm" and ui.confirm_kind == "remove-local"
    assert ui.local_preview.action == "remove-seat"
    frame = ui.render(90, 24, "")
    for text in ("Remove local seat large", "coder on studio", "Paused → Retired",
                 "kept as retired history", "! A retired local seat cannot be reactivated"):
        assert text in frame, text
    ui.handle("d")
    raw = ""
    for _ in range(10):
        raw += "\n" + ui.render(90, 16, "")
        ui.handle("pagedown")
    assert '-      "lifecycle": "inactive"' in raw
    assert '+      "lifecycle": "retired"' in raw
    ui.handle("esc")
    assert ui.view == "local-actions" and ui.local_preview is None
    assert json.loads(path.read_text())["local_seats"]["large"]["lifecycle"] == "inactive"

    ui.handle("enter")
    ui.handle("y")
    assert ui.view == "list" and ui.screen == "inventory"
    assert ui.message.startswith("✓ Removed local seat large · kept as retired history")
    written = json.loads(path.read_text())
    assert written["local_seats"]["large"]["lifecycle"] == "retired"
    assert written["local_seats"]["large"]["model_profile"] == "coder"
    assert written["local_seats"]["small"]["lifecycle"] == "active"
    assert written["local_hosts"]["studio"]["label"] == "Studio"
    assert "studio large" not in ui.render(100, 26, "")
    ui.handle("h")
    assert "studio large" in ui.render(100, 26, "")


def test_inventory_and_claude_screens_keep_minimum_frame_and_sanitized_highlight(
    tmp_path, monkeypatch
) -> None:
    ui, manager = _inventory_ui(tmp_path, monkeypatch)
    manager.settings["pool"]["seats"]["seat1"]["label"] = "n\x1b]0;x\x07界"
    ui.color = True

    def check(frame: str, width: int = 24, height: int = 6) -> None:
        _fits(frame, width, height)
        assert "\x1b" not in _plain_frame(frame)

    frame = ui.render(24, 6, "")
    check(frame)
    assert f"{MARKER}P seat1" in _plain_frame(frame) or f"{MARKER} P seat1" in _plain_frame(frame)
    assert SELECTED_BG in frame
    ui.color = False
    assert "n?]0;x?界" in _detail(ui) and "\x1b" not in _detail(ui)
    _select(ui, "local-seat:small")
    check(ui.render(24, 6, ""))
    assert "studio" in _pages(ui, 24, 6).lower()

    stacked = ui.render(60, 20, "")
    assert " │ " not in stacked and len(stacked.splitlines()) == 20
    two_column = ui.render(120, 30, "")
    assert " │ coder on studio  Local · small" in two_column
    assert len(two_column.splitlines()) == 30
    _fits(two_column, 120, 30)

    _select(ui, "add:seat")
    check(ui.render(24, 6, ""))
    ui.handle("enter")
    assert ui.view == "local-choice"
    check(ui.render(24, 6, ""))
    check(ui.render(64, 8, ""), 64, 8)
    ui.handle("esc")

    _select(ui, "codex:seat1")
    ui.handle("enter")
    check(ui.render(24, 6, ""))
    ui.handle("esc")
    _select(ui, "claude:account")
    ui.handle("enter")
    assert ui.screen == "claude"
    check(ui.render(24, 6, ""))
    ui.handle("down")
    ui.handle("down")
    ui.handle("enter")
    assert ui.view == "claude-input"
    ui.input_buffer = "12\x1b[2J界"
    frame = ui.render(24, 6, "")
    check(frame)
    assert "?[2J界" in _plain_frame(frame)


def test_real_pool_view_feeds_the_summary_strip_and_table(tmp_path, monkeypatch) -> None:
    """With a readable pool the strip and rows come from pool_view.collect."""
    for name in ("CODEX_CONTROL_SEAT", "CODEX_SEATS_HEAD", "CODEX_SEATS_MANAGED_WORKER"):
        monkeypatch.delenv(name, raising=False)
    _write_schema3(tmp_path)
    settings, _warnings = load_settings(tmp_path)
    calls = []
    from codex_seats import pool_view

    real_collect = pool_view.collect
    monkeypatch.setattr(
        pool_view, "collect", lambda *a, **k: calls.append(a[0]) or real_collect(*a, **k)
    )
    ui = ManagerUI(SeatManager(tmp_path, settings), direct=True)
    frame = ui.render(120, 30, "")
    assert calls and calls[0] == tmp_path
    assert ui.pool_error is None and "usage unavailable" not in frame
    assert frame.splitlines()[0].startswith("codex-seats  ○ CACHED")
    assert "Ready 0/0" in frame and "Next → none" in frame
    assert "W seat1 Unverified" in frame and "– paused" in _row_line(frame, "W seat1")
    _fits(frame, 120, 30)


def test_why_is_one_plain_sentence_from_the_structured_capacity_report() -> None:
    why = ManagerUI._why
    active = {"lifecycle": "active", "busy": False, "observed_at": time.time() - 4.3 * 3600}
    weekly = {
        "provider_status": "ok", "auth_ok": True,
        "small": {"fits": False, "tier": "fresh", "requirements": [
            {"window": "weekly", "headroom_pct": 0.4, "required_pct": 9.6, "margin_pct": 0.0},
        ]},
        "big": {"fits": False},
    }
    assert why(active, weekly) == "7-day window has 0% left; small tasks need 10%"
    five = {**weekly, "small": {"fits": True}, "big": {"fits": False, "tier": "fresh", "requirements": [
        {"window": "5h", "headroom_pct": 7.2, "required_pct": 10, "margin_pct": 0},
    ]}}
    assert why(active, five) == "Fits small tasks only · 5-hour window has 7% left; big tasks need 10%"
    old = {**weekly, "small": {"fits": False, "tier": "unknown"}}
    assert why(active, old) == "Usage data is 4.3h old · r refreshes it"
    assert why(active, {**weekly, "auth_ok": False}) == "Signed out · l signs in again"
    assert why({**active, "lifecycle": "inactive"}, None) == "Paused · p resumes it"
    assert why(active, {**weekly, "small": {"fits": True}, "big": {"fits": True}}) is None


def test_raw_diagnostic_stays_behind_d() -> None:
    manager = _FakeManager()
    manager.busy = set()
    ui = _frontier_ui(manager)
    ui._pool()["entries"][0]["reason"] = "capacity tier fresh (last-good observation age 4.30371h)"
    plain = _detail(ui, 80)
    assert "4.30371h" not in plain and "Raw" not in plain
    ui.handle("d")
    assert "Raw capacity tier fresh (last-good observation age 4.30371h)" in _detail(ui, 80)
    ui.handle("d")
    assert "4.30371h" not in _detail(ui, 80)


def test_more_marker_counts_hidden_seats_not_wrapped_lines() -> None:
    manager = _FakeManager()
    template = dict(manager.settings["pool"]["seats"]["seat2"])
    manager.settings["pool"]["seats"] = {
        f"seat{number}": {**template, "label": f"Seat {number}"} for number in range(1, 10)
    }
    ui = _frontier_ui(manager)
    frame = ui.render(48, 20, "")
    shown = {f"seat{n}" for n in range(1, 10) if re.search(rf"\bseat{n}\b", frame)}
    marker = re.search(r"… (\d+) more ↓", frame)
    assert marker and int(marker.group(1)) == 9 - len(shown)
