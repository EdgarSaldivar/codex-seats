"""Small, dependency-free terminal UI for the guarded seat manager.

The renderer is intentionally pure: it accepts structured registry reports and
produces one bounded frame. Provider work lives in WatchRefresher or the single
activation-check worker; settings commits are performed by SeatManager on the
key loop after the user confirms the exact preview.

Management opens on one unified Inventory listing every Codex seat, the
singleton Claude account, and every local seat (hosts are placement, not seats,
and live on the Local Hosts screen); Enter on a row jumps straight to that
item's controls, and Escape from those controls returns to the Inventory with
its prior selection. One ``Add seat`` row (or ``a``) opens a type menu: Codex,
Claude, or Local. Tab / Shift-Tab cycle through the Inventory, the frontier
seat registry, Local Hosts, Local Seats, and the Claude account screen; lists
entered by Tab keep their regular Escape-to-overview behaviour. The local
screens read through ``SeatManager.local_manager()``;
every local write is previewed through ``LocalSeatManager.preview_*`` and
committed with the explicit ``from_current_user_request=True`` marker only after
the user confirms the exact before/after. Local probes are queued through the
refresher and never run on the key loop. The Claude screen reads and edits
only through ``SeatManager.claude_manager()`` (settings, pool snapshot and the
passive usage cache); it never runs the provider client or opens credentials.
"""

from __future__ import annotations

import difflib
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, TypeAlias

from .claude_manager import CLAUDE_SUBSCRIPTION_TIERS
from .account import prepare_banked_reset_redemption, redeem_banked_reset
from .policy_settings import CHOICES, DEFAULTS, PolicyPreview
from .seat_manager import (
    ActionPreview,
    LoginPlan,
    ManagerError,
    SeatManager,
    _thaw,
)
from .seat_registry import (
    CAPACITY_PLAN_PRESETS,
    SeatRegistryError,
    validate_lifecycle_transition,
)
from .terminal import clean_text, display_width, wrap_lines


ANSI = {
    "green": "\033[38;2;80;250;123m",
    "cyan": "\033[38;2;139;233;253m",
    "amber": "\033[38;2;241;250;140m",
    "red": "\033[38;2;255;85;85m",
    "gray": "\033[38;2;98;114;164m",
    "purple": "\033[38;2;189;147;249m",
    "bold": "\033[1m",
    "selected": "\033[1;48;2;68;71;90m",
    "reset": "\033[0m",
}
ANSI_RE = re.compile(r"\033\[[0-9;]*m")
LineParts: TypeAlias = list[tuple[Any, str | None]]


def _credit_date(value: Any, status: str | None = None) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(value))
    return "none" if status == "none" else "unknown"


@dataclass(frozen=True)
class UIExit:
    reason: str
    login_seat_id: str | None = None
    login_plan: LoginPlan | None = None


@dataclass(frozen=True)
class Field:
    label: str
    option: str
    kind: str


FIELDS = (
    Field("label", "label", "text"),
    Field("role (primary/deputy/worker)", "role", "role"),
    Field("reserve percent", "reserve_pct", "number"),
    Field("small-task floor percent", "small_task_min_headroom_pct", "number"),
    Field("big-task floor percent", "big_task_min_headroom_pct", "number"),
    Field("configured plan and capacity", "capacity", "capacity"),
)

CAPACITY_PARTS = (
    ("profile", "profile"),
    ("relative capacity (0.1..1000 or null)", "relative_capacity"),
    ("source", "capacity_source"),
)
CAPACITY_CHOICES = (*CAPACITY_PLAN_PRESETS, {"label": "Advanced / custom values"})
ADD_FIELDS = (
    Field("label", "label", "text"),
    Field("home (blank = default)", "home", "text"),
    Field("configured plan", "profile", "preset"),
    Field("preview Add seat", "submit", "submit"),
)
ADD_PLAN_CHOICES = (
    {"label": "Unverified / configure after adding", "profile": None},
    *(
        {
            "label": f"{preset['label']} · capacity configured after adding",
            "profile": preset["profile"],
        }
        for preset in CAPACITY_PLAN_PRESETS
    ),
)

# -- screens ------------------------------------------------------------------
# The Inventory is the landing screen for `m` and direct management. The
# frontier registry stays adjacent to the local screens so Tab keeps the
# established seats -> hosts -> local seats path.
SCREENS = ("inventory", "seats", "hosts", "local-seats", "claude")
SCREEN_TITLES = {
    "inventory": "INVENTORY",
    "seats": "SEAT MANAGER",
    "hosts": "LOCAL HOSTS",
    "local-seats": "LOCAL SEATS",
    "claude": "CLAUDE ACCOUNT",
}
SCREEN_NAMES = {
    "inventory": "inventory",
    "seats": "frontier seats",
    "hosts": "local hosts",
    "local-seats": "local seats",
    "claude": "claude account",
}
LOCAL_SCREENS = frozenset({"hosts", "local-seats"})
LOCAL_MODE_ORDER = ("off", "shadow", "canary", "opportunistic")
LOCAL_LIFECYCLE_ORDER = ("active", "draining", "inactive", "retired")
LOCAL_VIEWS = frozenset(
    {"list", "local-actions", "local-form", "local-form-input", "local-choice"}
)
INVENTORY_VIEWS = frozenset({"list", "local-choice"})
KEY_TAB = "\t"
KEY_BACKTAB = "\x1b[Z"
LOCAL_INPUT_LIMIT = 4096

# -- claude singleton account -------------------------------------------------
CLAUDE_TARGET = "providers.claude"
CLAUDE_MODE_ORDER = ("off", "manual_only", "review_only", "opportunistic")
CLAUDE_VIEWS = frozenset({"list", "claude-actions", "claude-input", "local-choice"})
CLAUDE_INPUT_LIMIT = 16
CLAUDE_HEADROOM_FIELDS = {
    "small": ("small_task_min_headroom_pct", "small-task minimum headroom percent"),
    "big": ("big_task_min_headroom_pct", "big-task minimum headroom percent"),
}
CLAUDE_TIER_CLEAR_LABEL = "Clear the label (tier unknown)"

# -- inventory ----------------------------------------------------------------
# Rows are listed as codex seats, the claude account, local seats, then exactly
# one Add seat row. Local hosts are placement for local seats, not seats: they
# are reachable through Tab (Local Hosts) or a local seat's host action.
INVENTORY_ADD_KEY = "add:seat"
INVENTORY_GLOBAL_KEY = "settings:global"
GLOBAL_LABELS = {
    "review.mode": "Goal review mode",
    "review.strategy": "Review strategy",
    "execution.delegation_policy": "Delegation",
    "execution.inline_max_minutes": "Inline minutes",
    "execution.head_permissions": "Head mode",
    "execution.head_worker_fallback": "Head fallback",
    "ui.theme": "Appearance theme",
    "ui.density": "Display density",
    "ui.accent": "Accent color",
}
INVENTORY_ADD_LABEL = "Add seat (Codex, Claude, or Local)"
# The ID column grows to the longest listed id (local ids such as
# ``studio-qwen3-8-27b`` exceed the frontier's ``seatN``) up to a cap.
INVENTORY_ID_MIN_WIDTH = 10
INVENTORY_ID_MAX_WIDTH = 20
# The Add seat type menu. Claude is a singleton account: its option opens the
# existing account's setup instead of pretending to create a second login.
ADD_TYPE_CODEX = "add:codex"
ADD_TYPE_CLAUDE = "add:claude"
ADD_TYPE_LOCAL = "add:local"
ADD_TYPE_CHOICES = (
    ("Codex seat (dynamically allocated, starts inactive)", ADD_TYPE_CODEX),
    (
        "Claude account (one account only; opens the existing account setup)",
        ADD_TYPE_CLAUDE,
    ),
    ("Local seat on a registered local host", ADD_TYPE_LOCAL),
)
# Offered when Local is chosen but no local host is registered yet.
ADD_LOCAL_NO_HOST_CHOICES = (
    ("Add a local host first (required before any local seat)", "add:host"),
    ("Open Local Hosts", "hosts"),
)


@dataclass(frozen=True)
class InventoryRow:
    """One selectable row of the unified inventory; ``key`` is stable."""

    key: str
    kind: str  # codex, claude, local-seat, add
    entry_id: str
    state: str
    status: str
    label: str
    status_style: str | None = None


@dataclass(frozen=True)
class LocalField:
    """One field-oriented form entry for a local host or seat document."""

    label: str
    option: str
    kind: str  # id, text, optional-text, choice, int, json, optional-json, submit
    hint: str = ""
    default: str = ""


HOST_FORM_FIELDS = (
    LocalField(
        "host id",
        "id",
        "id",
        "Immutable lowercase slug of 1-64 letters, digits, or hyphens.",
    ),
    LocalField(
        "label",
        "label",
        "text",
        "1-128 visible characters without surrounding whitespace.",
        "New host",
    ),
    LocalField(
        "lifecycle",
        "lifecycle",
        "choice",
        "Seats can only be active on an active host; hosts start inactive by default.",
        "inactive",
    ),
    LocalField(
        "base_url (blank = none)",
        "base_url",
        "optional-text",
        "OpenAI-compatible HTTP(S) endpoint without credentials, query, or fragment.",
    ),
    LocalField(
        "lease_url (blank = none)",
        "lease_url",
        "optional-text",
        "Optional lease endpoint with the same URL rules. Keep credentials out of settings.",
    ),
    LocalField(
        "resource_groups (JSON)",
        "resource_groups",
        "json",
        'Object of group id to {"max_concurrency": 1..64}, e.g. {"gpu0": {"max_concurrency": 1}}.',
        '{"gpu0": {"max_concurrency": 1}}',
    ),
    LocalField(
        "availability_probe (JSON, blank = none)",
        "availability_probe",
        "optional-json",
        'Object {"argv": ["/absolute/probe", "arg"], "timeout_seconds": 0.1..30}. Direct argv only; no shell.',
    ),
    LocalField("preview", "submit", "submit"),
)
SEAT_FORM_FIELDS = (
    LocalField(
        "seat id",
        "id",
        "id",
        "Immutable lowercase slug of 1-64 letters, digits, or hyphens.",
    ),
    LocalField(
        "label",
        "label",
        "text",
        "1-128 visible characters without surrounding whitespace.",
        "New local seat",
    ),
    LocalField(
        "lifecycle",
        "lifecycle",
        "choice",
        "Mode must be off unless lifecycle is active, and the host must be active too.",
        "inactive",
    ),
    LocalField(
        "mode",
        "mode",
        "choice",
        "The effective mode is the lower of the global mode and this seat mode.",
        "off",
    ),
    LocalField("host", "host", "choice", "A registered local host id."),
    LocalField(
        "resource_group",
        "resource_group",
        "choice",
        "A resource group declared on the chosen host.",
    ),
    LocalField(
        "model_profile",
        "model_profile",
        "text",
        "Lowercase model identifier served by the host, e.g. qwen3.8-27b.",
    ),
    LocalField(
        "context_window_size",
        "context_window_size",
        "optional-int",
        "Server effective context limit in tokens; leave blank to use Qwen's model default.",
    ),
    LocalField("harness", "harness", "text", "Lowercase slug; qwen is the supported harness.", "qwen"),
    LocalField(
        "max_concurrency",
        "max_concurrency",
        "int",
        "Integer 1-64, not above the resource group maximum.",
        "1",
    ),
    LocalField(
        "capabilities (JSON)",
        "capabilities",
        "json",
        'Object {"task_shapes": [execute|investigate|design], "validation": [strong|weak]}; non-empty unique lists.',
        '{"task_shapes": ["execute"], "validation": ["strong"]}',
    ),
    LocalField(
        "runtime_fingerprint",
        "runtime_fingerprint",
        "text",
        "sha256: followed by 64 lowercase hex characters identifying model plus runtime configuration.",
        "sha256:",
    ),
    LocalField("preview", "submit", "submit"),
)
# Edit forms change non-secret configuration only; lifecycle and mode use their
# dedicated actions so transition rules stay visible.
EDIT_EXCLUDED_OPTIONS = frozenset({"id", "lifecycle", "mode"})


class ManagerUI:
    """State machine for overview, seat actions, forms, and confirmations."""

    def __init__(self, manager: SeatManager | None, *, direct: bool = False) -> None:
        self.manager = manager
        self.view = "list" if direct else "overview"
        self.selected_id: str | None = None
        self.menu_index = 0
        self.field_index = 0
        self.scroll = 0
        self.detail_scroll = 0
        self._detail_page_size = 1
        self._action_page_size = 1
        self._field_page_size = 1
        self._capacity_preset_page_size = 1
        self._confirm_page_size = 1
        self._too_small = False
        self._color = False
        self._quit_return_view = "overview"
        self.message = ""
        self.preview: ActionPreview | None = None
        self.login_plan: LoginPlan | None = None
        self.pause_plan: Any | None = None
        self.confirm_kind: str | None = None
        self.input_buffer = ""
        self.capacity_buffers: dict[str, str] = {}
        self.capacity_index = 0
        self.capacity_preset_index = 0
        self.add_field_index = 0
        self.add_preset_index = 0
        self.add_buffers = {"label": "New seat", "home": "", "profile": None}
        self.show_history = False
        self.confirm_return_view = "actions"
        self.message_return_view = "actions"
        self.activation_cancel = threading.Event()
        self.activation_thread: threading.Thread | None = None
        self.activation_result: Any | None = None
        self.activation_error: str | None = None
        self.reset_preview: dict[str, Any] | None = None
        self.reset_thread: threading.Thread | None = None
        self.reset_cancel = threading.Event()
        self.reset_phase: str | None = None
        self.reset_result: Any | None = None
        self.reset_error: str | None = None
        self._result_lock = threading.Lock()
        # Linearize a late cancel/quit against the main-loop activation commit.
        # Provider work never holds this gate.
        self._activation_gate = threading.Lock()
        self.last_reconcile = 0.0
        # Management lands on the unified inventory. Selection is a stable key
        # per screen; reports are re-read from the managers (never probed).
        self.screen = "inventory"
        self.inventory_selected: str | None = None
        # True while a provider screen was entered from the Inventory (Enter on
        # a row or the Add seat menu): Escape from that item's controls returns
        # to the Inventory. Tab always clears it so provider lists keep their
        # regular Escape-to-overview behaviour.
        self.opened_from_inventory = False
        self.local_selected: dict[str, str | None] = {"hosts": None, "local-seats": None}
        self.local_error: str | None = None
        self._local_report_cache: dict[str, Any] | None = None
        self._local_report_at = 0.0
        self._probe_signature: Any = None
        self.local_preview: Any | None = None
        self.local_form_action = ""
        self.local_form_target: str | None = None
        self.local_form_fields: tuple[LocalField, ...] = ()
        self.local_form_buffers: dict[str, str] = {}
        self.local_form_index = 0
        self.local_form_original: dict[str, Any] = {}
        self.local_form_return_view = "list"
        self.choice_title = ""
        self.choice_context: Any = ""
        self.choice_purpose = ""
        self.choice_items: list[tuple[str, Any]] = []
        self.choice_index = 0
        self.choice_return_view = "list"
        self._choice_page_size = 1
        # Claude singleton account screen: cached read-only report, one
        # digest-pinned preview at a time, and the headroom field being typed.
        self.claude_error: str | None = None
        self._claude_report_cache: dict[str, Any] | None = None
        self._claude_report_at = 0.0
        self.claude_preview: Any | None = None
        self.claude_input_field = "small"
        self.global_preview: PolicyPreview | None = None
        self.global_values: dict[str, Any] = {}
        self.global_index = 0
        self.global_choice_index = 0
        self.global_return_view = "list"

    @property
    def local_screen(self) -> bool:
        return self.screen in LOCAL_SCREENS

    @property
    def inventory_screen(self) -> bool:
        return self.screen == "inventory"

    @property
    def claude_screen(self) -> bool:
        return self.screen == "claude"

    def _seat_ids(self) -> list[str]:
        if self.manager is None:
            return []
        from .seat_registry import sort_seat_ids

        seats = self.manager.settings.get("pool", {}).get("seats", {})
        if not self.show_history:
            seats = {
                seat_id: entry
                for seat_id, entry in seats.items()
                if entry.get("lifecycle") != "retired"
            }
        ids = sort_seat_ids(seats)
        if self.selected_id not in ids:
            self.selected_id = ids[0] if ids else None
        return ids

    def _selected_record(self) -> Mapping[str, Any] | None:
        if self.manager is None or self.selected_id is None:
            return None
        try:
            report = self.manager.list_report(include_all=True)
        except ManagerError as exc:
            self.message = str(exc)
            return None
        return next(
            (
                seat
                for seat in report.get("seats", [])
                if seat.get("id") == self.selected_id
            ),
            None,
        )

    def _actions(self) -> list[tuple[str, str]]:
        record = self._selected_record()
        if record is None:
            return []
        lifecycle = record.get("lifecycle")
        actions: list[tuple[str, str]] = []
        if lifecycle != "retired":
            actions.append(("Refresh provider usage now", "refresh"))
        if lifecycle in {"active", "draining"}:
            actions.append(("Redeem one banked reset…", "redeem-reset"))
        if lifecycle in {"active", "draining"}:
            actions.append(("Pause / finish drain", "pause"))
        if lifecycle in {"inactive", "draining"}:
            active = any(
                entry.get("lifecycle") == "active"
                for entry in self.manager.settings["pool"]["seats"].values()
            )
            if active:
                actions.append(("Reactivate (login + fresh usage checks)", "activate"))
            else:
                actions.append(
                    (
                        "Reactivate as primary (login + fresh usage checks)",
                        "activate-primary",
                    )
                )
        if lifecycle != "retired":
            actions.append(("Sign in again", "login"))
            actions.append(("Edit supported seat settings", "settings"))
            actions.append(
                (
                    "Remove seat (requires inactive, idle, non-primary/deputy)",
                    "remove",
                )
            )
        pending = {item.seat_id: item for item in self.manager.pending_drains()}
        if (
            self.selected_id in pending
            and pending[self.selected_id].state == "draining"
        ):
            actions.append(("Cancel this session's drain wait", "cancel-drain"))
        self.menu_index = min(max(0, self.menu_index), max(0, len(actions) - 1))
        return actions

    def _move(
        self, values: list[Any], delta: int, attribute: str = "menu_index"
    ) -> None:
        if not values:
            return
        current = getattr(self, attribute)
        setattr(self, attribute, (current + delta) % len(values))

    def _page_move(
        self, values: list[Any], delta: int, attribute: str = "menu_index"
    ) -> None:
        if not values:
            return
        current = min(max(0, getattr(self, attribute)), len(values) - 1)
        setattr(self, attribute, min(max(0, current + delta), len(values) - 1))

    def _cancel_activation(self) -> None:
        with self._activation_gate:
            self.activation_cancel.set()
        if self.reset_phase == "preview":
            self.reset_cancel.set()

    def handle(self, key: str) -> UIExit | None:
        if self.reset_phase == "redeem" and self.reset_thread is not None and self.reset_thread.is_alive() and key in {"q", "interrupt", "eof"}:
            self.message = "Redemption attempt is in progress; wait for its bounded result"
            return None
        if key in {"interrupt", "eof"}:
            self._cancel_activation()
            return UIExit("quit")
        # The resize-only frame must not accept a now-hidden mutation. q and
        # terminal closure/signals remain usable so the session can exit.
        if self._too_small:
            if key == "q":
                self._cancel_activation()
                # A hidden quit-confirm dialog cannot be answered while resize
                # mode blocks its keys. Exit as EOF does, leaving drains intact.
                return UIExit("quit")
            return None
        if key == "q" and self.view not in {
            "input",
            "add-input",
            "capacity-input",
            "local-form-input",
            "claude-input",
            "global-input",
            "quit-confirm",
        }:
            self._cancel_activation()
            return self._request_quit()
        if self.view == "overview":
            if key == "q":
                return self._request_quit()
            if key == "?":
                self.view = "help"
            elif key == "m":
                self._open_inventory()
            elif key == "g":
                self._open_global_settings("overview")
            return None
        if self.view == "help":
            if key in {"esc", "?"}:
                self.view = "overview"
            elif key == "q":
                return self._request_quit()
            return None
        if self.view in {"global-settings", "global-choice", "global-input"}:
            return self._handle_global_settings(key)
        if self.local_screen and self.view in LOCAL_VIEWS:
            return self._handle_local(key)
        if self.inventory_screen and self.view in INVENTORY_VIEWS:
            return self._handle_inventory(key)
        if self.claude_screen and self.view in CLAUDE_VIEWS:
            return self._handle_claude(key)
        if self.view == "list":
            ids = self._seat_ids()
            if key in {KEY_TAB, KEY_BACKTAB}:
                self._switch_screen(1 if key == KEY_TAB else -1)
            elif key == "a":
                self.opened_from_inventory = False
                self.add_buffers = {"label": "New seat", "home": "", "profile": None}
                self.add_field_index = 0
                self.view = "add-form"
            elif key == "g":
                self._open_global_settings("list")
            elif key == "h":
                self.show_history = not self.show_history
                self.detail_scroll = 0
                self._seat_ids()
            elif key in {"down", "j"} and ids:
                self.selected_id = ids[(ids.index(self.selected_id) + 1) % len(ids)]
                self.detail_scroll = 0
            elif key in {"up", "k"} and ids:
                self.selected_id = ids[(ids.index(self.selected_id) - 1) % len(ids)]
                self.detail_scroll = 0
            elif key == "pagedown":
                self.detail_scroll += self._detail_page_size
            elif key == "pageup":
                self.detail_scroll = max(0, self.detail_scroll - self._detail_page_size)
            elif key == "enter" and self.selected_id:
                self.opened_from_inventory = False
                self.view = "actions"
                self.menu_index = 0
            elif key == "?":
                self.view = "help"
            elif key == "esc":
                self._back_to_overview()
            elif key == "q":
                return self._request_quit()
            return None
        if self.view == "add-form":
            if key in {"down", "j"}:
                self._move(list(ADD_FIELDS), 1, "add_field_index")
            elif key in {"up", "k"}:
                self._move(list(ADD_FIELDS), -1, "add_field_index")
            elif key == "esc":
                self._back_from_provider("list")
            elif key == "enter":
                field = ADD_FIELDS[self.add_field_index]
                if field.kind == "preset":
                    self.view = "add-presets"
                elif field.kind == "submit":
                    self._preview_add()
                else:
                    self.input_buffer = str(self.add_buffers[field.option] or "")
                    self.view = "add-input"
            return None
        if self.view == "add-presets":
            if key in {"down", "j"}:
                self._move(list(ADD_PLAN_CHOICES), 1, "add_preset_index")
            elif key in {"up", "k"}:
                self._move(list(ADD_PLAN_CHOICES), -1, "add_preset_index")
            elif key == "pagedown":
                self._page_move(
                    list(ADD_PLAN_CHOICES),
                    self._capacity_preset_page_size,
                    "add_preset_index",
                )
            elif key == "pageup":
                self._page_move(
                    list(ADD_PLAN_CHOICES),
                    -self._capacity_preset_page_size,
                    "add_preset_index",
                )
            elif key == "esc":
                self.view = "add-form"
            elif key == "enter":
                self.add_buffers["profile"] = ADD_PLAN_CHOICES[
                    self.add_preset_index
                ]["profile"]
                self.view = "add-form"
            return None
        if self.view == "add-input":
            if key == "esc":
                self.view = "add-form"
            elif key == "backspace":
                self.input_buffer = self.input_buffer[:-1]
            elif key == "enter":
                field = ADD_FIELDS[self.add_field_index]
                self.add_buffers[field.option] = self.input_buffer.strip()
                self.view = "add-form"
            elif len(key) == 1 and key.isprintable() and len(self.input_buffer) < 240:
                self.input_buffer += key
            return None
        if self.view == "actions":
            actions = self._actions()
            if key in {"down", "j"}:
                self._move(actions, 1)
            elif key in {"up", "k"}:
                self._move(actions, -1)
            elif key == "pagedown":
                self._page_move(actions, self._action_page_size)
            elif key == "pageup":
                self._page_move(actions, -self._action_page_size)
            elif key == "esc":
                self._back_from_provider("list")
            elif key == "q":
                return self._request_quit()
            elif key == "enter" and actions:
                return self._choose_action(actions[self.menu_index][1])
            return None
        if self.view == "settings":
            if key in {"down", "j"}:
                self._move(list(FIELDS), 1, "field_index")
            elif key in {"up", "k"}:
                self._move(list(FIELDS), -1, "field_index")
            elif key == "pagedown":
                self._page_move(list(FIELDS), self._field_page_size, "field_index")
            elif key == "pageup":
                self._page_move(list(FIELDS), -self._field_page_size, "field_index")
            elif key == "esc":
                self.view = "actions"
            elif key == "enter":
                field = FIELDS[self.field_index]
                if field.kind == "capacity":
                    self.capacity_buffers = {
                        option: self._current_capacity_value(option)
                        for _label, option in CAPACITY_PARTS
                    }
                    current_profile = self.capacity_buffers["profile"]
                    self.capacity_preset_index = next(
                        (
                            index
                            for index, preset in enumerate(CAPACITY_PLAN_PRESETS)
                            if preset["profile"] == current_profile
                        ),
                        len(CAPACITY_CHOICES) - 1,
                    )
                    self.view = "capacity-presets"
                else:
                    self.input_buffer = self._current_field_value(field)
                    self.view = "input"
            return None
        if self.view == "capacity-presets":
            if key in {"down", "j"}:
                self.capacity_preset_index = (self.capacity_preset_index + 1) % len(
                    CAPACITY_CHOICES
                )
            elif key == "pagedown":
                self._page_move(
                    list(CAPACITY_CHOICES),
                    self._capacity_preset_page_size,
                    "capacity_preset_index",
                )
            elif key == "pageup":
                self._page_move(
                    list(CAPACITY_CHOICES),
                    -self._capacity_preset_page_size,
                    "capacity_preset_index",
                )
            elif key in {"up", "k"}:
                self.capacity_preset_index = (self.capacity_preset_index - 1) % len(
                    CAPACITY_CHOICES
                )
            elif key == "esc":
                self.view = "settings"
            elif key == "enter":
                if self.capacity_preset_index == len(CAPACITY_CHOICES) - 1:
                    self.capacity_index = 0
                    self.view = "capacity-input"
                else:
                    self._preview_capacity_preset(
                        CAPACITY_CHOICES[self.capacity_preset_index]
                    )
            return None
        if self.view == "input":
            if key == "esc":
                self.view = "settings"
            elif key == "backspace":
                self.input_buffer = self.input_buffer[:-1]
            elif key == "enter":
                self._preview_field(FIELDS[self.field_index])
            elif len(key) == 1 and key.isprintable() and len(self.input_buffer) < 240:
                self.input_buffer += key
            return None
        if self.view == "capacity-input":
            option = CAPACITY_PARTS[self.capacity_index][1]
            if key == "esc":
                self.view = "settings"
            elif key == "down":
                self.capacity_index = (self.capacity_index + 1) % len(CAPACITY_PARTS)
            elif key == "up":
                self.capacity_index = (self.capacity_index - 1) % len(CAPACITY_PARTS)
            elif key == "backspace":
                self.capacity_buffers[option] = self.capacity_buffers[option][:-1]
            elif key == "enter":
                self._preview_capacity()
            elif (
                len(key) == 1
                and key.isprintable()
                and len(self.capacity_buffers[option]) < 240
            ):
                self.capacity_buffers[option] += key
            return None
        if self.view == "confirm":
            if key in {"down", "j", "pagedown"}:
                self.scroll += 1 if key != "pagedown" else self._confirm_page_size
            elif key in {"up", "k", "pageup"}:
                self.scroll = max(
                    0,
                    self.scroll - (1 if key != "pageup" else self._confirm_page_size),
                )
            elif key in {"esc", "n"}:
                self.view = self.confirm_return_view
                self.preview = None
                self.reset_preview = None
                self.pause_plan = None
                self.local_preview = None
                self.claude_preview = None
                self.global_preview = None
            elif key in {"enter", "y"}:
                return self._confirm()
            return None
        if self.view == "busy":
            if key == "esc":
                if self.reset_phase == "redeem":
                    self.message = "Confirmed attempt is in progress; awaiting its result"
                else:
                    self._cancel_activation()
                    self.message = "Cancel requested; waiting for bounded provider work to stop"
            elif key == "q":
                self._cancel_activation()
                return self._request_quit()
        if self.view == "quit-confirm":
            if key in {"enter", "y"}:
                return UIExit("quit")
            if key in {"esc", "n"}:
                self.view = self._quit_return_view
        if self.view == "message" and key in {"enter", "esc"}:
            self.view = self.message_return_view
        return None

    def _request_quit(self) -> UIExit | None:
        pending = (
            [item for item in self.manager.pending_drains() if item.state == "draining"]
            if self.manager is not None
            else []
        )
        if pending:
            self.message = (
                "Quitting stops this session's wait. These seats remain draining: "
                + ", ".join(item.seat_id for item in pending)
                + ". They will not be deactivated unattended."
            )
            self._quit_return_view = self.view
            self.view = "quit-confirm"
            return None
        return UIExit("quit")

    def _back_to_overview(self) -> UIExit | None:
        """Escape from a provider list: Inventory if entered from it, else overview."""
        if self.opened_from_inventory:
            self._return_to_inventory()
        else:
            self.view = "overview"
        return None

    def _back_from_provider(self, view: str) -> None:
        """Escape from an item's controls entered from the Inventory.

        ``view`` is the provider screen's regular parent view. When that parent
        is the provider list and the controls were opened from the Inventory,
        return straight to the Inventory (all provider rows, prior selection)
        instead of the single-provider list.
        """
        if view == "list" and self.opened_from_inventory:
            self._return_to_inventory()
        else:
            self.view = view

    def _return_to_inventory(self) -> None:
        """Land on the Inventory keeping ``inventory_selected`` as it was."""
        self._show_screen("inventory")

    # -- local hosts and seats ----------------------------------------------
    def _switch_screen(self, delta: int) -> None:
        index = SCREENS.index(self.screen)
        self._show_screen(SCREENS[(index + delta) % len(SCREENS)])

    def _show_screen(
        self, screen: str, view: str = "list", *, from_inventory: bool = False
    ) -> None:
        """Land on ``screen`` with fresh reports and no stale preview.

        ``from_inventory`` records that the screen was entered from an
        Inventory row or its Add seat menu; Tab and ``m`` always clear it.
        """
        self.screen = screen
        self.view = view
        self.detail_scroll = 0
        self.menu_index = 0
        self.local_preview = None
        self.claude_preview = None
        self._local_report_cache = None
        self._claude_report_cache = None
        self.opened_from_inventory = from_inventory and screen != "inventory"

    def _open_inventory(self) -> None:
        """``m`` from the dashboard always lands on the unified inventory."""
        self._show_screen("inventory")

    def _local(self) -> Any:
        if self.manager is None:
            raise ManagerError("local management requires the seat manager")
        return self.manager.local_manager()

    def _local_report(self, *, force: bool = False) -> dict[str, Any]:
        """Cached structured local report; reads settings and cache only."""
        now = time.monotonic()
        cached = self._local_report_cache
        if not force and cached is not None and now - self._local_report_at < 0.25:
            return cached
        try:
            report = self._local().report()
            self.local_error = None
        except (ManagerError, ValueError, OSError, AttributeError) as exc:
            self.local_error = str(exc)
            report = {
                "global_mode": "unknown",
                "hosts": {},
                "seats": {},
                "warnings": [],
                "probe": None,
            }
        self._local_report_cache = report
        self._local_report_at = now
        return report

    def _local_entries(self, report: Mapping[str, Any] | None = None) -> dict[str, Any]:
        report = report if report is not None else self._local_report()
        entries = report["hosts"] if self.screen == "hosts" else report["seats"]
        return entries if isinstance(entries, dict) else {}

    def _local_ids(self, report: Mapping[str, Any] | None = None) -> list[str]:
        ids = sorted(self._local_entries(report))
        if self.local_selected.get(self.screen) not in ids:
            self.local_selected[self.screen] = ids[0] if ids else None
        return ids

    @property
    def local_selected_id(self) -> str | None:
        return self.local_selected.get(self.screen)

    def _context_id(self) -> str | None:
        if self.local_screen:
            return self.local_selected_id
        if self.claude_screen:
            return CLAUDE_TARGET
        if self.inventory_screen:
            return self.inventory_selected
        return self.selected_id

    def _host_groups(self, host_id: str) -> list[str]:
        host = self._local_report()["hosts"].get(host_id)
        groups = host["config"].get("resource_groups") if isinstance(host, Mapping) else None
        return sorted(groups) if isinstance(groups, Mapping) else []

    # -- unified inventory --------------------------------------------------
    def _codex_records(self) -> dict[str, Mapping[str, Any]]:
        """Frontier list report keyed by stable seat id (empty when unreadable)."""
        if self.manager is None:
            return {}
        try:
            report = self.manager.list_report(include_all=True)
        except ManagerError as exc:
            self.message = str(exc)
            return {}
        return {
            seat["id"]: seat
            for seat in report.get("seats", [])
            if isinstance(seat, Mapping) and isinstance(seat.get("id"), str)
        }

    def _inventory_rows(self) -> list[InventoryRow]:
        """Every Codex seat, the Claude account, local seats, then one Add row.

        Local hosts are not seats and are not listed here; they stay on the
        Local Hosts screen (Tab) and behind a local seat's host action. Retired
        Codex and local seats are hidden until ``h`` shows history, matching
        the frontier registry. Reads are cached reports only.
        """
        rows: list[InventoryRow] = []
        if self.manager is not None:
            records = self._codex_records()
            pool = self.manager.settings.get("pool", {})
            for seat_id in self._seat_ids():
                entry = pool.get("seats", {}).get(seat_id, {})
                record = records.get(seat_id, {})
                claim = record.get("claim", {})
                live = isinstance(claim, Mapping) and bool(claim.get("live"))
                role = record.get("role")
                label = clean_text(entry.get("label") or "")
                if role:
                    label = f"{label} · {role}" if label else str(role)
                rows.append(
                    InventoryRow(
                        key=f"codex:{seat_id}",
                        kind="codex",
                        entry_id=seat_id,
                        state=clean_text(entry.get("lifecycle") or "unknown"),
                        status="working" if live else "idle",
                        label=label,
                        status_style="cyan" if live else "gray",
                    )
                )
        rows.append(self._claude_inventory_row())
        report = self._local_report()
        for seat_id in sorted(report.get("seats", {})):
            seat = report["seats"].get(seat_id, {})
            config = seat.get("config", {}) if isinstance(seat, Mapping) else {}
            lifecycle = clean_text(config.get("lifecycle") or "unknown")
            if lifecycle == "retired" and not self.show_history:
                continue
            status, style = self._local_status_word(
                seat.get("status") if isinstance(seat, Mapping) else None
            )
            label = clean_text(config.get("label") or "")
            host = config.get("host")
            if isinstance(host, str) and host:
                label = f"{label} · host {host}" if label else f"host {host}"
            rows.append(
                InventoryRow(
                    key=f"local-seat:{seat_id}",
                    kind="local-seat",
                    entry_id=seat_id,
                    state=lifecycle,
                    status=status,
                    label=label,
                    status_style=style,
                )
            )
        if self.local_error:
            rows.append(
                InventoryRow(
                    key="local-seat:unavailable",
                    kind="local-seat",
                    entry_id="—",
                    state="unknown",
                    status="error",
                    label=f"Local registry unavailable: {self.local_error}",
                    status_style="red",
                )
            )
        rows.append(
            InventoryRow(
                key=INVENTORY_GLOBAL_KEY,
                kind="global",
                entry_id="settings",
                state="",
                status="",
                label="Global settings (review, execution, probe mode)",
            )
        )
        rows.insert(
            len(rows) - 1,
            InventoryRow(
                key=INVENTORY_ADD_KEY,
                kind="add",
                entry_id="seat",
                state="",
                status="",
                label=INVENTORY_ADD_LABEL,
                status_style=None,
            )
        )
        return rows

    def _claude_inventory_row(self) -> InventoryRow:
        report = self._claude_report()
        if report is None:
            return InventoryRow(
                key="claude:account",
                kind="claude",
                entry_id="account",
                state="unknown",
                status="error",
                label=f"Claude account unavailable: {self.claude_error}",
                status_style="red",
            )
        status = report.get("status")
        # "none": the pool snapshot is missing or unreadable (detail shows why).
        status_word = (
            clean_text(status.get("status") or "unknown")
            if isinstance(status, Mapping)
            else "none"
        )
        tier = report.get("subscription_tier", {})
        tier = tier if isinstance(tier, Mapping) else {}
        headroom = report.get("headroom", {})
        headroom = headroom if isinstance(headroom, Mapping) else {}
        mode = clean_text(report.get("mode") or "off")
        label = (
            f"mode {clean_text(report.get('configured_mode') or 'off')} · "
            f"tier {tier.get('label') or 'unknown'} ({tier.get('source') or 'unknown'}) · "
            f"floors {headroom.get('small_min_pct')}%/{headroom.get('big_min_pct')}%"
        )
        return InventoryRow(
            key="claude:account",
            kind="claude",
            entry_id="account",
            # The lifecycle column reads on/off; the full mode is in the label.
            state="off" if mode == "off" else "on",
            status=status_word,
            label=label,
            status_style=self._claude_status_style(status_word),
        )

    def _inventory_keys(self, rows: list[InventoryRow] | None = None) -> list[str]:
        rows = rows if rows is not None else self._inventory_rows()
        keys = [row.key for row in rows]
        if self.inventory_selected not in keys:
            self.inventory_selected = keys[0] if keys else None
        return keys

    def _handle_inventory(self, key: str) -> UIExit | None:
        if self.view == "local-choice":
            return self._handle_choice(key)
        rows = self._inventory_rows()
        keys = self._inventory_keys(rows)
        selected = self.inventory_selected
        if key in {KEY_TAB, KEY_BACKTAB}:
            self._switch_screen(1 if key == KEY_TAB else -1)
        elif key in {"down", "j"} and keys:
            self.inventory_selected = keys[(keys.index(selected) + 1) % len(keys)]
            self.detail_scroll = 0
        elif key in {"up", "k"} and keys:
            self.inventory_selected = keys[(keys.index(selected) - 1) % len(keys)]
            self.detail_scroll = 0
        elif key == "pagedown":
            self.detail_scroll += self._detail_page_size
        elif key == "pageup":
            self.detail_scroll = max(0, self.detail_scroll - self._detail_page_size)
        elif key == "enter" and selected:
            row = next((row for row in rows if row.key == selected), None)
            if row is not None:
                self._open_inventory_row(row)
        elif key == "a":
            self._open_add_type_menu()
        elif key == "g":
            self._open_global_settings("list")
        elif key == "h":
            self.show_history = not self.show_history
            self.detail_scroll = 0
        elif key == "?":
            self.view = "help"
        elif key == "esc":
            self.view = "overview"
        return None

    def _open_global_settings(self, return_view: str) -> None:
        assert self.manager is not None
        try:
            self.global_values = self.manager.global_settings_report()
        except ManagerError as exc:
            self.message = str(exc)
            self.message_return_view = return_view
            self.view = "message"
            return
        self.global_return_view = return_view
        self.global_index = 0
        self.view = "global-settings"

    def _preview_global_value(self, value: Any) -> None:
        assert self.manager is not None
        key = tuple(DEFAULTS)[self.global_index]
        try:
            self.global_preview = self.manager.preview_global_settings({key: value})
        except ManagerError as exc:
            self.message = str(exc)
            self.message_return_view = "global-settings"
            self.view = "message"
            return
        self.confirm_kind = "global"
        self.confirm_return_view = "global-settings"
        self.scroll = 0
        self.view = "confirm"

    def _handle_global_settings(self, key: str) -> UIExit | None:
        fields = tuple(DEFAULTS)
        selected = fields[self.global_index]
        if self.view == "global-settings":
            if key in {"down", "j"}:
                self.global_index = (self.global_index + 1) % len(fields)
            elif key in {"up", "k"}:
                self.global_index = (self.global_index - 1) % len(fields)
            elif key == "pagedown":
                self.global_index = min(len(fields) - 1, self.global_index + self._field_page_size)
            elif key == "pageup":
                self.global_index = max(0, self.global_index - self._field_page_size)
            elif key == "esc":
                self.view = self.global_return_view
            elif key == "enter":
                if selected == "execution.inline_max_minutes":
                    self.input_buffer = str(self.global_values[selected])
                    self.view = "global-input"
                else:
                    choices = (True, False) if isinstance(DEFAULTS[selected], bool) else tuple(sorted(CHOICES[selected]))
                    self.global_choice_index = choices.index(self.global_values[selected])
                    self.view = "global-choice"
        elif self.view == "global-choice":
            choices = (True, False) if isinstance(DEFAULTS[selected], bool) else tuple(sorted(CHOICES[selected]))
            if key in {"down", "j"}:
                self.global_choice_index = (self.global_choice_index + 1) % len(choices)
            elif key in {"up", "k"}:
                self.global_choice_index = (self.global_choice_index - 1) % len(choices)
            elif key == "esc":
                self.view = "global-settings"
            elif key == "enter":
                self._preview_global_value(choices[self.global_choice_index])
        elif self.view == "global-input":
            if key == "esc":
                self.view = "global-settings"
            elif key == "backspace":
                self.input_buffer = self.input_buffer[:-1]
            elif key == "enter":
                self._preview_global_value(self.input_buffer)
            elif len(key) == 1 and key.isdecimal() and len(self.input_buffer) < 3:
                self.input_buffer += key
        return None

    def _render_global_settings(self, width: int, height: int) -> str:
        fields = tuple(DEFAULTS)
        selected = fields[self.global_index]
        if self.view == "global-input":
            lines: list[LineParts] = [
                self._header("GLOBAL SETTINGS", GLOBAL_LABELS.get(selected, selected)),
                self._section("Minutes (1–240)"),
                [(f"> {self.input_buffer}", "selected")],
            ]
            return self._bounded(lines, "Enter preview · Esc cancel · Backspace edit", width, height)
        if self.view == "global-choice":
            choices = (True, False) if isinstance(DEFAULTS[selected], bool) else tuple(sorted(CHOICES[selected]))
            return self._menu_frame(
                title="GLOBAL SETTINGS", context=GLOBAL_LABELS.get(selected, selected), section="Choices",
                rows=[str(value).lower() if isinstance(value, bool) else value for value in choices],
                selected=self.global_choice_index, page_size_attribute="_field_page_size",
                footer="↑↓ choose · Enter preview · Esc cancel", width=width, height=height,
            )
        return self._menu_frame(
            title="GLOBAL SETTINGS", context="Operating preferences",
            section="Preferences",
            rows=[f"{GLOBAL_LABELS.get(field, field)} = {str(self.global_values[field]).lower() if isinstance(self.global_values[field], bool) else self.global_values[field]}" for field in fields],
            selected=self.global_index, page_size_attribute="_field_page_size",
            footer="↑↓/PgUp/PgDn · Enter edit · Esc back", width=width, height=height,
        )

    def _open_inventory_row(self, row: InventoryRow) -> None:
        """Enter on an inventory row lands directly on that item's controls.

        Every screen entered here is marked as opened from the Inventory, so
        Escape from the item's controls returns to the Inventory rather than
        to the single-provider list.
        """
        if row.kind == "codex":
            self._show_screen("seats", "actions", from_inventory=True)
            self.selected_id = row.entry_id
            self._seat_ids()
        elif row.kind == "claude":
            self._show_screen("claude", "claude-actions", from_inventory=True)
        elif row.kind == "local-seat":
            if row.entry_id not in self._local_report().get("seats", {}):
                # The registry-unavailable placeholder has no controls.
                self._show_screen("local-seats", from_inventory=True)
                return
            self._show_screen("local-seats", "local-actions", from_inventory=True)
            self.local_selected["local-seats"] = row.entry_id
        elif row.kind == "add":
            self._open_add_type_menu()
        elif row.kind == "global":
            self._open_global_settings("list")

    def _add_type_items(self) -> list[tuple[str, str]]:
        return list(ADD_TYPE_CHOICES)

    def _open_add_type_menu(self, initial: str = ADD_TYPE_CODEX) -> None:
        """``a`` or Enter on the Add seat row: choose Codex, Claude, or Local."""
        items = self._add_type_items()
        self._open_choice(
            "ADD SEAT",
            "choose the seat type",
            "inventory-add",
            items,
            initial,
            return_view="list",
        )

    def _inventory_add(self, target: str) -> None:
        """The Add seat type menu opens the same forms as ``a`` on each screen.

        Codex opens the existing Add seat form. Claude
        is a singleton account, so its option opens the existing account's
        setup rather than creating a second login. Local opens the local seat
        form when a host is registered, otherwise a short menu to register or
        manage a host first.
        """
        if target == ADD_TYPE_CODEX:
            self._show_screen("seats", "add-form", from_inventory=True)
            self.add_buffers = {"label": "New seat", "home": "", "profile": None}
            self.add_field_index = 0
        elif target == ADD_TYPE_CLAUDE:
            self._show_screen("claude", "claude-actions", from_inventory=True)
        elif target == ADD_TYPE_LOCAL:
            if not self._local_report().get("hosts"):
                self._open_choice(
                    "ADD LOCAL SEAT",
                    "no local host is registered yet",
                    "inventory-add-local",
                    list(ADD_LOCAL_NO_HOST_CHOICES),
                    "add:host",
                    return_view="list",
                )
                return
            self._show_screen("local-seats", from_inventory=True)
            self._open_local_form("add-seat")
        elif target == "add:host":
            self._show_screen("hosts", from_inventory=True)
            self._open_local_form("add-host")
        elif target == "hosts":
            self._show_screen("hosts", from_inventory=True)
        else:
            self._return_to_inventory()

    # -- claude singleton account -------------------------------------------
    def _claude(self) -> Any:
        if self.manager is None:
            raise ManagerError("Claude account management requires the seat manager")
        return self.manager.claude_manager()

    def _claude_report(self, *, force: bool = False) -> dict[str, Any] | None:
        """Cached read-only account report; settings, snapshot and cache only.

        ``ClaudeAccountManager.report`` never runs the provider client or opens
        credentials, and this UI adds no other Claude reads.
        """
        now = time.monotonic()
        cached = self._claude_report_cache
        if not force and cached is not None and now - self._claude_report_at < 0.25:
            return cached
        try:
            report = self._claude().report()
            self.claude_error = None
        except (ManagerError, ValueError, OSError, AttributeError) as exc:
            self.claude_error = str(exc)
            self._claude_report_cache = None
            return None
        self._claude_report_cache = report
        self._claude_report_at = now
        return report

    @staticmethod
    def _claude_status_style(status: str) -> str | None:
        return {
            "ok": "green",
            "available": "green",
            "ready": "green",
            "cooldown": "amber",
            "capped": "amber",
            "unavailable": "red",
            "error": "red",
            "needs-login": "red",
        }.get(status, "gray")

    def _claude_actions(self) -> list[tuple[str, str]]:
        actions = [
            ("Set mode (" + " / ".join(CLAUDE_MODE_ORDER) + ")", "claude-mode"),
            (
                "Set subscription tier label (" + ", ".join(CLAUDE_SUBSCRIPTION_TIERS) + ")",
                "claude-tier",
            ),
            ("Set small-task minimum headroom percent", "claude-small"),
            ("Set big-task minimum headroom percent", "claude-big"),
            ("Toggle active /usage refresh", "claude-active-refresh"),
        ]
        self.menu_index = min(max(0, self.menu_index), len(actions) - 1)
        return actions

    def _handle_claude(self, key: str) -> UIExit | None:
        if self.view == "local-choice":
            return self._handle_choice(key)
        if self.view == "list":
            if key in {KEY_TAB, KEY_BACKTAB}:
                self._switch_screen(1 if key == KEY_TAB else -1)
            elif key == "pagedown":
                self.detail_scroll += self._detail_page_size
            elif key == "pageup":
                self.detail_scroll = max(0, self.detail_scroll - self._detail_page_size)
            elif key in {"down", "j"}:
                self.detail_scroll += 1
            elif key in {"up", "k"}:
                self.detail_scroll = max(0, self.detail_scroll - 1)
            elif key == "enter":
                self.opened_from_inventory = False
                self.view = "claude-actions"
                self.menu_index = 0
            elif key == "?":
                self.view = "help"
            elif key == "esc":
                self._back_to_overview()
            return None
        if self.view == "claude-actions":
            actions = self._claude_actions()
            if key in {"down", "j"}:
                self._move(actions, 1)
            elif key in {"up", "k"}:
                self._move(actions, -1)
            elif key == "pagedown":
                self._page_move(actions, self._action_page_size)
            elif key == "pageup":
                self._page_move(actions, -self._action_page_size)
            elif key == "esc":
                self._claude_report_cache = None
                self._back_from_provider("list")
            elif key == "enter" and actions:
                self._choose_claude_action(actions[self.menu_index][1])
            return None
        if self.view == "claude-input":
            if key == "esc":
                self.view = "claude-actions"
            elif key == "backspace":
                self.input_buffer = self.input_buffer[:-1]
            elif key == "enter":
                self._preview_claude_headroom()
            elif (
                len(key) == 1
                and key.isprintable()
                and len(self.input_buffer) < CLAUDE_INPUT_LIMIT
            ):
                self.input_buffer += key
            return None
        return None

    def _choose_claude_action(self, action: str) -> None:
        self.message_return_view = "claude-actions"
        self.confirm_return_view = "claude-actions"
        report = self._claude_report(force=True)
        if report is None:
            self.message = f"Claude account unavailable: {self.claude_error}"
            self.view = "message"
            return
        if action == "claude-mode":
            current = report.get("configured_mode")
            items = [
                (f"{mode}{' (current)' if mode == current else ''}", mode)
                for mode in CLAUDE_MODE_ORDER
            ]
            self._open_choice(
                "CLAUDE MODE", CLAUDE_TARGET, "claude-mode", items, current,
                return_view="claude-actions",
            )
        elif action == "claude-active-refresh":
            try:
                preview = self._claude().preview(active_usage_refresh=not bool(report.get("active_usage_refresh")))
                self._show_claude_preview(preview, return_view="claude-actions")
            except (ManagerError, ValueError) as exc:
                self.message = f"Cannot preview: {exc}"
                self.view = "message"
        elif action == "claude-tier":
            tier = report.get("subscription_tier", {})
            current = tier.get("configured") if isinstance(tier, Mapping) else None
            items: list[tuple[str, Any]] = [
                (f"{label}{' (current)' if label == current else ''}", label)
                for label in CLAUDE_SUBSCRIPTION_TIERS
            ]
            items.append(
                (
                    CLAUDE_TIER_CLEAR_LABEL + (" (current)" if current is None else ""),
                    None,
                )
            )
            self._open_choice(
                "CLAUDE SUBSCRIPTION TIER", CLAUDE_TARGET, "claude-tier", items, current,
                return_view="claude-actions",
            )
        elif action in {"claude-small", "claude-big"}:
            self.claude_input_field = "small" if action == "claude-small" else "big"
            headroom = report.get("headroom", {})
            headroom = headroom if isinstance(headroom, Mapping) else {}
            current = headroom.get(f"{self.claude_input_field}_min_pct")
            self.input_buffer = "" if current is None else f"{current:g}"
            self.view = "claude-input"

    def _preview_claude_headroom(self) -> None:
        field = self.claude_input_field
        raw = self.input_buffer.strip()
        try:
            number = float(raw)
            value: Any = int(number) if number.is_integer() else number
            claude = self._claude()
            preview = (
                claude.preview_headroom(small=value)
                if field == "small"
                else claude.preview_headroom(big=value)
            )
        except (ManagerError, ValueError, AttributeError) as exc:
            self.message = f"Invalid {CLAUDE_HEADROOM_FIELDS[field][1]}: {exc}"
            self.message_return_view = "claude-input"
            self.view = "message"
            return
        self._show_claude_preview(preview, return_view="claude-actions")

    def _show_claude_preview(self, preview: Any, *, return_view: str) -> None:
        self.claude_preview = preview
        self.preview = None
        self.pause_plan = None
        self.local_preview = None
        self.confirm_kind = "claude"
        self.confirm_return_view = return_view
        self.scroll = 0
        self.view = "confirm"

    def _commit_claude(self) -> None:
        preview = self.claude_preview
        if preview is None:
            self.message = "Claude preview expired; select the action again"
            self.message_return_view = "list"
            self.view = "message"
            return
        try:
            result = self._claude().commit(preview, from_current_user_request=True)
        except ManagerError as exc:
            self.message = str(exc)
            self.message_return_view = self.confirm_return_view
            self.view = "message"
            return
        # Keep the dashboard and the periodic refresher on the committed
        # settings revision, just as the local manager does after its edits.
        if self.manager is not None:
            self.manager.reload()
        self.claude_preview = None
        self._claude_report_cache = None
        self.detail_scroll = 0
        changes = ", ".join(
            f"{key} = {value if value is not None else 'null'}"
            for key, value in _thaw(preview.changes).items()
        )
        self.message = (
            f"Applied Claude account edit ({changes})."
            if result.changed
            else f"No change was needed for the Claude account ({changes})."
        )
        if result.enabling:
            self.message += " Claude's configured mode now permits routing."
        if result.warnings:
            self.message += " Warnings: " + "; ".join(result.warnings)
        self.message_return_view = "list"
        self.view = "message"

    def _claude_preview_material(self) -> list[str]:
        preview = self.claude_preview
        if preview is None:
            return ["Claude preview expired; select the action again."]
        public = preview.as_dict()
        before = json.dumps(public["before"], indent=2, sort_keys=True).splitlines(
            keepends=True
        )
        after = json.dumps(public["after"], indent=2, sort_keys=True).splitlines(
            keepends=True
        )
        diff = [
            line.rstrip("\n")
            for line in difflib.unified_diff(
                before, after, fromfile="providers.claude.before", tofile="providers.claude.after"
            )
        ]
        changes = ", ".join(
            f"{key} = {value if value is not None else 'null'}"
            for key, value in public["changes"].items()
        )
        material = [
            f"Claude account edit: {changes}  Target: {CLAUDE_TARGET}",
            *[f"WARNING: {warning}" for warning in preview.warnings],
        ]
        if preview.enabling:
            material.append(
                "WARNING: this change turns Claude on or switches its on-mode (enabling)"
            )
        material.append(
            "Only settings change; no provider client runs and no credentials are read."
        )
        material.extend(diff or ["No settings bytes would change."])
        return material

    def _handle_local(self, key: str) -> UIExit | None:
        if self.view == "list":
            ids = self._local_ids()
            selected = self.local_selected_id
            if key in {KEY_TAB, KEY_BACKTAB}:
                self._switch_screen(1 if key == KEY_TAB else -1)
            elif key in {"down", "j"} and ids:
                self.local_selected[self.screen] = ids[(ids.index(selected) + 1) % len(ids)]
                self.detail_scroll = 0
            elif key in {"up", "k"} and ids:
                self.local_selected[self.screen] = ids[(ids.index(selected) - 1) % len(ids)]
                self.detail_scroll = 0
            elif key == "pagedown":
                self.detail_scroll += self._detail_page_size
            elif key == "pageup":
                self.detail_scroll = max(0, self.detail_scroll - self._detail_page_size)
            elif key == "enter" and selected:
                self.opened_from_inventory = False
                self.view = "local-actions"
                self.menu_index = 0
            elif key == "a":
                self.opened_from_inventory = False
                self._open_local_form("add-host" if self.screen == "hosts" else "add-seat")
            elif key == "g":
                self.opened_from_inventory = False
                self.message_return_view = "list"
                self._open_mode_choice("global")
            elif key == "p" and self.screen == "hosts" and selected:
                self.opened_from_inventory = False
                self.message_return_view = "list"
                self._request_probe(selected)
            elif key == "?":
                self.view = "help"
            elif key == "esc":
                self._back_to_overview()
            return None
        if self.view == "local-actions":
            actions = self._local_actions()
            if key in {"down", "j"}:
                self._move(actions, 1)
            elif key in {"up", "k"}:
                self._move(actions, -1)
            elif key == "pagedown":
                self._page_move(actions, self._action_page_size)
            elif key == "pageup":
                self._page_move(actions, -self._action_page_size)
            elif key == "esc":
                self._back_from_provider("list")
            elif key == "enter" and actions:
                self._choose_local_action(actions[self.menu_index][1])
            return None
        if self.view == "local-choice":
            return self._handle_choice(key)
        if self.view == "local-form":
            fields = list(self.local_form_fields)
            if key in {"down", "j"}:
                self._move(fields, 1, "local_form_index")
            elif key in {"up", "k"}:
                self._move(fields, -1, "local_form_index")
            elif key == "pagedown":
                self._page_move(fields, self._field_page_size, "local_form_index")
            elif key == "pageup":
                self._page_move(fields, -self._field_page_size, "local_form_index")
            elif key == "esc":
                self._back_from_provider(self.local_form_return_view)
            elif key == "enter" and fields:
                field = fields[self.local_form_index]
                if field.kind == "submit":
                    self._local_form_preview()
                elif field.kind == "choice":
                    self._open_form_choice(field)
                else:
                    self.input_buffer = self.local_form_buffers.get(field.option, "")
                    self.view = "local-form-input"
            return None
        if self.view == "local-form-input":
            field = self.local_form_fields[self.local_form_index]
            if key == "esc":
                self.view = "local-form"
            elif key == "backspace":
                self.input_buffer = self.input_buffer[:-1]
            elif key == "enter":
                self.local_form_buffers[field.option] = self.input_buffer.strip()
                self.view = "local-form"
            elif (
                key.isprintable()
                and len(self.input_buffer) + len(key) <= LOCAL_INPUT_LIMIT
            ):
                # Pasted bursts arrive as one printable string; JSON fields
                # such as probe argv are easier to paste than to type.
                self.input_buffer += key
            return None
        return None

    def _handle_choice(self, key: str) -> UIExit | None:
        """Shared key handling for the choice list used by every screen."""
        items = self.choice_items
        if key in {"down", "j"}:
            self._move(items, 1, "choice_index")
        elif key in {"up", "k"}:
            self._move(items, -1, "choice_index")
        elif key == "pagedown":
            self._page_move(items, self._choice_page_size, "choice_index")
        elif key == "pageup":
            self._page_move(items, -self._choice_page_size, "choice_index")
        elif key == "esc":
            if self.choice_purpose == "inventory-add-local":
                self._open_add_type_menu(ADD_TYPE_LOCAL)
            else:
                self.view = self.choice_return_view
        elif key == "enter" and items:
            self._choice_selected(items[self.choice_index][1])
        return None

    def _local_actions(self) -> list[tuple[str, str]]:
        report = self._local_report()
        selected = self.local_selected_id
        actions: list[tuple[str, str]] = []
        if selected is None or selected not in self._local_entries(report):
            self.menu_index = 0
            return actions
        if self.screen == "hosts":
            actions.append(("Probe now (read-only, runs on the refresher)", "probe"))
            actions.append(("Set host lifecycle", "host-lifecycle"))
            actions.append(("Edit host configuration", "edit-host"))
            actions.append(("Add local seat on this host", "add-seat-here"))
        else:
            actions.append(("Set seat mode", "seat-mode"))
            actions.append(("Set seat lifecycle", "seat-lifecycle"))
            actions.append(("Edit seat configuration", "edit-seat"))
            actions.append(("Probe this seat's host now (read-only)", "probe-host"))
            actions.append(("Manage this seat's host (Local Hosts)", "open-host"))
            actions.append(
                (
                    "Remove seat (requires lifecycle inactive and mode off; "
                    "keeps retired history)",
                    "remove-seat",
                )
            )
        actions.append(("Set global local mode", "global-mode"))
        self.menu_index = min(max(0, self.menu_index), max(0, len(actions) - 1))
        return actions

    def _choose_local_action(self, action: str) -> None:
        selected = self.local_selected_id
        self.message_return_view = "local-actions"
        self.confirm_return_view = "local-actions"
        if selected is None:
            self.view = "list"
            return
        if action == "probe":
            self._request_probe(selected)
        elif action == "probe-host":
            seat = self._local_report()["seats"].get(selected, {})
            host = seat.get("config", {}).get("host") if isinstance(seat, Mapping) else None
            if not isinstance(host, str) or not host:
                self.message = f"Local seat {selected} has no configured host to probe"
                self.view = "message"
            else:
                self._request_probe(host)
        elif action == "open-host":
            report = self._local_report()
            seat = report["seats"].get(selected, {})
            host = seat.get("config", {}).get("host") if isinstance(seat, Mapping) else None
            if not isinstance(host, str) or host not in report.get("hosts", {}):
                self.message = (
                    f"Local seat {selected} names host {host or 'none'}, which is not "
                    "registered; Tab to Local Hosts to add or inspect hosts"
                )
                self.view = "message"
                return
            # Keep the Inventory return path when the seat was opened from it.
            self._show_screen(
                "hosts", "local-actions", from_inventory=self.opened_from_inventory
            )
            self.local_selected["hosts"] = host
        elif action == "global-mode":
            self._open_mode_choice("global")
        elif action == "seat-mode":
            self._open_mode_choice("seat")
        elif action in {"host-lifecycle", "seat-lifecycle"}:
            self._open_lifecycle_choice()
        elif action in {"edit-host", "edit-seat"}:
            self._open_local_form(action, selected)
        elif action == "add-seat-here":
            self._open_local_form("add-seat", preset_host=selected)
        elif action == "remove-seat":
            self._preview_remove_local_seat(selected)

    def _preview_remove_local_seat(self, seat_id: str) -> None:
        """Preview retiring a local seat in place through the local manager.

        The core refuses unless the seat is inactive with mode off and has no
        running local job; the entry is kept as retired history, never deleted.
        """
        self.message_return_view = "local-actions"
        try:
            preview = self._local().preview_remove_seat(seat_id)
        except ManagerError as exc:
            self.message = (
                f"Cannot remove local seat {seat_id}: {exc}. Removal requires "
                "lifecycle inactive and mode off; the entry is kept as retired history."
            )
            self.view = "message"
            return
        if not preview.changed:
            self.message = (
                f"Local seat {seat_id} is already retired; its id, configuration and "
                "history remain visible as retired history."
            )
            self.view = "message"
            return
        self._show_local_preview(preview, return_view="local-actions")

    def _request_probe(self, host_id: str) -> None:
        """Queue one read-only probe on the refresher; never probe here."""
        try:
            queued = self._local().request_probe(host_id)
            self.message = (
                f"Probe queued for {queued}. It runs on the refresher worker; the "
                "host detail shows the live probe state when it finishes."
            )
        except ManagerError as exc:
            self.message = f"Cannot probe {host_id}: {exc}"
        self.view = "message"

    def _open_choice(
        self,
        title: str,
        context: Any,
        purpose: str,
        items: list[tuple[str, Any]],
        current: Any,
        *,
        return_view: str,
    ) -> None:
        self.choice_title = title
        self.choice_context = context
        self.choice_purpose = purpose
        self.choice_items = items
        self.choice_index = next(
            (index for index, (_label, value) in enumerate(items) if value == current),
            0,
        )
        self.choice_return_view = return_view
        self.view = "local-choice"

    def _open_mode_choice(self, scope: str) -> None:
        report = self._local_report()
        if scope == "global":
            current = report.get("global_mode")
            title, context = "GLOBAL LOCAL MODE", "providers.local.mode"
        else:
            seat = report["seats"].get(self.local_selected_id, {})
            current = seat.get("config", {}).get("mode") if isinstance(seat, Mapping) else None
            title, context = "SEAT MODE", self.local_selected_id
        items = [
            (f"{mode}{' (current)' if mode == current else ''}", mode)
            for mode in LOCAL_MODE_ORDER
        ]
        self._open_choice(title, context, f"{scope}-mode", items, current, return_view=self.view)

    def _open_lifecycle_choice(self) -> None:
        entry = self._local_entries().get(self.local_selected_id, {})
        config = entry.get("config", {}) if isinstance(entry, Mapping) else {}
        current = config.get("lifecycle")
        items: list[tuple[str, Any]] = []
        for lifecycle in LOCAL_LIFECYCLE_ORDER:
            if lifecycle == current:
                items.append((f"{lifecycle} (current)", lifecycle))
                continue
            try:
                validate_lifecycle_transition(str(current), lifecycle)
            except (SeatRegistryError, ValueError, KeyError):
                continue
            items.append((lifecycle, lifecycle))
        if len(items) <= 1:
            self.message = (
                f"No lifecycle transition is available from {current}; retired is terminal"
            )
            self.view = "message"
            return
        kind = "HOST" if self.screen == "hosts" else "SEAT"
        self._open_choice(
            f"{kind} LIFECYCLE",
            self.local_selected_id,
            "host-lifecycle" if self.screen == "hosts" else "seat-lifecycle",
            items,
            current,
            return_view=self.view,
        )

    def _choice_selected(self, value: Any) -> None:
        purpose = self.choice_purpose
        if purpose.startswith("form:"):
            option = purpose[len("form:") :]
            self.local_form_buffers[option] = str(value)
            if option == "host":
                groups = self._host_groups(str(value))
                if self.local_form_buffers.get("resource_group") not in groups:
                    self.local_form_buffers["resource_group"] = groups[0] if groups else ""
            self.view = "local-form"
            return
        if purpose in {"inventory-add", "inventory-add-local"}:
            self._inventory_add(str(value))
            return
        if purpose in {"claude-mode", "claude-tier"}:
            try:
                claude = self._claude()
                preview = (
                    claude.preview_mode(value)
                    if purpose == "claude-mode"
                    else claude.preview_subscription_tier(value)
                )
            except (ManagerError, ValueError, AttributeError) as exc:
                self.message = f"Cannot preview: {exc}"
                self.message_return_view = self.choice_return_view
                self.view = "message"
                return
            self._show_claude_preview(preview, return_view=self.choice_return_view)
            return
        target = self.local_selected_id
        try:
            local = self._local()
            if purpose == "global-mode":
                preview = local.preview_global_mode(value)
            elif purpose == "seat-mode":
                preview = local.preview_seat_mode(target, value)
            elif purpose == "host-lifecycle":
                preview = local.preview_host_lifecycle(target, value)
            elif purpose == "seat-lifecycle":
                preview = local.preview_seat_lifecycle(target, value)
            else:
                raise ManagerError(f"unknown local choice {purpose!r}")
        except (ManagerError, ValueError) as exc:
            self.message = f"Cannot preview: {exc}"
            self.message_return_view = self.choice_return_view
            self.view = "message"
            return
        self._show_local_preview(preview, return_view=self.choice_return_view)

    def _show_local_preview(self, preview: Any, *, return_view: str) -> None:
        self.local_preview = preview
        self.preview = None
        self.pause_plan = None
        self.confirm_kind = "local"
        self.confirm_return_view = return_view
        self.scroll = 0
        self.view = "confirm"

    @staticmethod
    def _buffer_text(field: LocalField, value: Any) -> str:
        if value is None:
            return ""
        if field.kind in {"json", "optional-json"}:
            return json.dumps(value, sort_keys=True)
        return str(value)

    def _open_local_form(
        self, action: str, target: str | None = None, *, preset_host: str | None = None
    ) -> None:
        kind = "host" if action.endswith("host") else "seat"
        fields = HOST_FORM_FIELDS if kind == "host" else SEAT_FORM_FIELDS
        editing = action.startswith("edit-")
        if editing:
            fields = tuple(f for f in fields if f.option not in EDIT_EXCLUDED_OPTIONS)
            entry = self._local_entries().get(target, {})
            config = entry.get("config", {}) if isinstance(entry, Mapping) else {}
            self.local_form_original = _thaw(config)
            buffers = {
                f.option: self._buffer_text(f, config.get(f.option))
                for f in fields
                if f.kind != "submit"
            }
        else:
            self.local_form_original = {}
            buffers = {f.option: f.default for f in fields if f.kind != "submit"}
            if kind == "seat":
                hosts = sorted(self._local_report()["hosts"])
                host = preset_host or (hosts[0] if hosts else "")
                buffers["host"] = host
                groups = self._host_groups(host)
                buffers["resource_group"] = groups[0] if groups else ""
        self.local_form_action = action
        self.local_form_target = target
        self.local_form_fields = fields
        self.local_form_buffers = buffers
        self.local_form_index = 0
        self.local_form_return_view = "local-actions" if editing else "list"
        self.view = "local-form"

    def _open_form_choice(self, field: LocalField) -> None:
        option = field.option
        current = self.local_form_buffers.get(option, "")
        if option == "lifecycle":
            values: list[str] = list(LOCAL_LIFECYCLE_ORDER)
        elif option == "mode":
            values = list(LOCAL_MODE_ORDER)
        elif option == "host":
            values = sorted(self._local_report()["hosts"])
            if not values:
                self.message = "Add a local host first; a local seat must name a registered host"
                self.message_return_view = "local-form"
                self.view = "message"
                return
        elif option == "resource_group":
            values = self._host_groups(self.local_form_buffers.get("host", ""))
            if not values:
                self.input_buffer = current
                self.view = "local-form-input"
                return
        else:
            values = []
        items = [
            (f"{value}{' (current)' if value == current else ''}", value)
            for value in values
        ]
        self._open_choice(
            field.label.upper(),
            self.local_form_action,
            f"form:{option}",
            items,
            current,
            return_view="local-form",
        )

    def _local_form_values(self) -> dict[str, Any]:
        """Parse form buffers; ``None`` marks a cleared optional field."""
        values: dict[str, Any] = {}
        for field in self.local_form_fields:
            if field.kind in {"submit", "id"}:
                continue
            raw = self.local_form_buffers.get(field.option, "").strip()
            if field.kind == "optional-text":
                values[field.option] = raw or None
            elif field.kind in {"int", "optional-int"}:
                if not raw and field.kind == "optional-int":
                    values[field.option] = None
                    continue
                try:
                    values[field.option] = int(raw)
                except ValueError:
                    raise ValueError(f"{field.option} must be an integer") from None
            elif field.kind in {"json", "optional-json"}:
                if not raw:
                    if field.kind == "optional-json":
                        values[field.option] = None
                        continue
                    raise ValueError(f"{field.option} requires a JSON object")
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{field.option} is not valid JSON: {exc}") from None
                if not isinstance(parsed, dict):
                    raise ValueError(f"{field.option} must be a JSON object")
                values[field.option] = parsed
            else:
                values[field.option] = raw
        return values

    def _local_form_preview(self) -> None:
        action = self.local_form_action
        kind = "host" if action.endswith("host") else "seat"
        try:
            values = self._local_form_values()
            local = self._local()
            if action.startswith("add-"):
                entry_id = self.local_form_buffers.get("id", "").strip()
                config = {key: value for key, value in values.items() if value is not None}
                preview = (
                    local.preview_add_host(entry_id, config)
                    if kind == "host"
                    else local.preview_add_seat(entry_id, config)
                )
            else:
                original = self.local_form_original
                changes: dict[str, Any] = {}
                for option, value in values.items():
                    if value is None:
                        if option in original:
                            changes[option] = None
                    elif value != original.get(option):
                        changes[option] = value
                if not changes:
                    self.message = "No changes to preview; edit a field first"
                    self.message_return_view = "local-form"
                    self.view = "message"
                    return
                preview = (
                    local.preview_edit_host(self.local_form_target, changes)
                    if kind == "host"
                    else local.preview_edit_seat(self.local_form_target, changes)
                )
        except (ManagerError, ValueError) as exc:
            self.message = f"Cannot preview {action}: {exc}"
            self.message_return_view = "local-form"
            self.view = "message"
            return
        self._show_local_preview(preview, return_view="local-form")

    def _commit_local(self) -> None:
        preview = self.local_preview
        if preview is None:
            self.message = "Local preview expired; select the action again"
            self.message_return_view = "list"
            self.view = "message"
            return
        try:
            result = self._local().commit(preview, from_current_user_request=True)
        except ManagerError as exc:
            self.message = str(exc)
            self.message_return_view = self.confirm_return_view
            self.view = "message"
            return
        self.local_preview = None
        self._local_report_cache = None
        target = preview.target_id
        if preview.action == "add-host":
            self.screen = "hosts"
            self.local_selected["hosts"] = target
            self.message = (
                f"Added local host {target}. Next: add a seat on it, then set the host "
                "lifecycle and seat mode when it should take work."
            )
            self.message_return_view = "list"
        elif preview.action == "add-seat":
            self.screen = "local-seats"
            self.local_selected["local-seats"] = target
            if self.opened_from_inventory:
                self.inventory_selected = f"local-seat:{target}"
            self.message = (
                f"Added local seat {target}. Its effective mode is the lower of the "
                "global and seat modes."
            )
            self.message_return_view = "list"
        elif preview.action == "remove-seat":
            self.screen = "local-seats"
            self.local_selected["local-seats"] = target
            self.message = (
                f"Removed local seat {target} from service. It is retired in place: its "
                "id, configuration and history stay in settings as retired history, and "
                "it can no longer be reactivated."
                if result.changed
                else f"Local seat {target} was already retired; its history is retained."
            )
            self.message_return_view = "list"
        else:
            label = f"{preview.action} on {target}" if target else preview.action
            self.message = (
                f"Applied {label}" if result.changed else f"No change was needed for {label}"
            )
            self.message_return_view = (
                "list" if self.confirm_return_view == "local-form" else self.confirm_return_view
            )
        if result.warnings:
            self.message += " Warnings: " + "; ".join(result.warnings)
        self.view = "message"

    def _local_preview_material(self) -> list[str]:
        preview = self.local_preview
        if preview is None:
            return ["Local preview expired; select the action again."]
        target = (
            f"{preview.kind} {preview.target_id}" if preview.target_id else "providers.local"
        )
        before = json.dumps(_thaw(preview.before), indent=2, sort_keys=True).splitlines(
            keepends=True
        )
        after = json.dumps(_thaw(preview.after), indent=2, sort_keys=True).splitlines(
            keepends=True
        )
        diff = [
            line.rstrip("\n")
            for line in difflib.unified_diff(
                before, after, fromfile="settings.before", tofile="settings.after"
            )
        ]
        material = [
            f"Local action: {preview.action}  Target: {target}",
            *[f"WARNING: {warning}" for warning in preview.warnings],
        ]
        if preview.enabling:
            material.append(
                "WARNING: this change makes local execution less restrictive (enabling)"
            )
        if preview.action == "remove-seat":
            material.append(
                "Remove retires the seat in place: it requires lifecycle inactive and "
                "mode off with no running local job, and the entry stays as retired "
                "history (h shows retired seats in the inventory)."
            )
        material.extend(diff or ["No settings bytes would change."])
        return material

    def _choose_action(self, action: str) -> UIExit | None:
        assert self.manager is not None and self.selected_id is not None
        self.message_return_view = "actions"
        self.confirm_return_view = "actions"
        try:
            if action == "refresh":
                seat_id = self.manager.request_refresh(self.selected_id)
                self.message = (
                    f"Refresh queued for {seat_id}; last-good usage remains visible"
                )
                self.view = "message"
            elif action == "redeem-reset":
                self._start_reset_work("preview")
            elif action == "pause":
                self.pause_plan = self.manager.preview_pause(self.selected_id)
                self.preview = self.pause_plan.preview
                self.confirm_kind = "pause"
                self.scroll = 0
                self.view = "confirm"
            elif action in {"activate", "activate-primary"}:
                self.preview = self.manager.preview_reactivate(
                    self.selected_id, as_primary=action == "activate-primary"
                )
                self.confirm_kind = "activate"
                self.scroll = 0
                self.view = "confirm"
            elif action == "login":
                self.login_plan = self.manager.preview_login(self.selected_id)
                self.confirm_kind = "login"
                self.preview = None
                self.scroll = 0
                self.view = "confirm"
            elif action == "settings":
                self.field_index = 0
                self.view = "settings"
            elif action == "remove":
                self.preview = self.manager.preview_remove(self.selected_id)
                self.confirm_kind = "remove"
                self.confirm_return_view = "actions"
                self.scroll = 0
                self.view = "confirm"
            elif action == "cancel-drain":
                result = self.manager.cancel_pending_drain(self.selected_id)
                self.message = result.detail
                self.view = "message"
        except ManagerError as exc:
            self.message = str(exc)
            self.view = "message"
        return None

    def _preview_add(self) -> None:
        assert self.manager is not None
        try:
            label = str(self.add_buffers["label"]).strip()
            home_text = str(self.add_buffers["home"]).strip()
            profile = self.add_buffers["profile"]
            self.preview = self.manager.preview_add(
                label=label,
                home=home_text or None,
                profile=str(profile) if profile else None,
            )
            self.confirm_kind = "add"
            self.confirm_return_view = "add-form"
            self.scroll = 0
            self.view = "confirm"
        except (ManagerError, ValueError) as exc:
            self.message = f"Cannot add seat: {exc}"
            self.message_return_view = "add-form"
            self.view = "message"

    def _current_field_value(self, field: Field) -> str:
        assert self.manager is not None and self.selected_id is not None
        pool = self.manager.settings["pool"]
        entry = pool["seats"][self.selected_id]
        if field.kind == "capacity":
            return " / ".join(
                self._current_capacity_value(option)
                for _label, option in CAPACITY_PARTS
            )
        if field.option == "role":
            from .seat_registry import derive_roles

            return derive_roles(self.manager.settings).get(self.selected_id) or "worker"
        if field.option == "reserve_pct":
            return str(pool.get("reserves_pct", {}).get(self.selected_id, 0))
        if field.option in {"small_task_min_headroom_pct", "big_task_min_headroom_pct"}:
            size = "small" if field.option.startswith("small") else "big"
            overrides = pool.get("task_min_headroom_pct_by_seat", {}).get(
                self.selected_id, {}
            )
            return str(overrides.get(size, pool.get(field.option, 0)))
        value = entry.get(
            field.option if field.option != "profile" else "capacity_profile"
        )
        return "null" if value is None else str(value)

    def _current_capacity_value(self, option: str) -> str:
        assert self.manager is not None and self.selected_id is not None
        entry = self.manager.settings["pool"]["seats"][self.selected_id]
        key = "capacity_profile" if option == "profile" else option
        value = entry.get(key)
        return "null" if value is None else str(value)

    def _preview_field(self, field: Field) -> None:
        assert self.manager is not None and self.selected_id is not None
        raw = self.input_buffer.strip()
        try:
            if field.kind == "number":
                value: Any = float(raw)
            elif field.kind == "nullable":
                value = None if raw.lower() in {"null", "none"} else float(raw)
            else:
                value = raw
            if field.kind == "role":
                preview = self.manager.preview_set_role(self.selected_id, value)
            else:
                preview = self.manager.preview_set(
                    self.selected_id, **{field.option: value}
                )
            self.preview = preview
            self.confirm_kind = "settings"
            self.scroll = 0
            self.view = "confirm"
        except (ManagerError, ValueError) as exc:
            self.message = f"Invalid value: {exc}"
            self.view = "message"

    def _preview_capacity(self) -> None:
        assert self.manager is not None and self.selected_id is not None
        try:
            relative_text = self.capacity_buffers["relative_capacity"].strip()
            relative: Any = (
                None
                if relative_text.lower() in {"null", "none"}
                else float(relative_text)
            )
            self.preview = self.manager.preview_set(
                self.selected_id,
                profile=self.capacity_buffers["profile"].strip(),
                relative_capacity=relative,
                capacity_source=self.capacity_buffers["capacity_source"].strip(),
            )
            self.confirm_kind = "settings"
            self.scroll = 0
            self.view = "confirm"
        except (ManagerError, ValueError) as exc:
            self.message = f"Invalid capacity values: {exc}"
            self.view = "message"

    def _preview_capacity_preset(self, preset: Mapping[str, Any]) -> None:
        assert self.manager is not None and self.selected_id is not None
        try:
            self.preview = self.manager.preview_set(
                self.selected_id,
                profile=preset["profile"],
                relative_capacity=preset["relative_capacity"],
                capacity_source=preset["capacity_source"],
            )
            self.confirm_kind = "settings"
            self.scroll = 0
            self.view = "confirm"
        except (ManagerError, KeyError, ValueError) as exc:
            self.message = f"Invalid capacity preset: {exc}"
            self.view = "message"

    def _confirm(self) -> UIExit | None:
        assert self.manager is not None
        if self.confirm_kind == "redeem-reset":
            if self.reset_preview is None:
                self.message = "Reset preview expired; select the action again"
                self.view = "message"
            else:
                self._start_reset_work("redeem")
            return None
        if self.confirm_kind == "login":
            if self.login_plan is None:
                self.message = "Login confirmation expired; select the action again"
                self.view = "message"
                return None
            return UIExit("login", self.login_plan.seat_id, self.login_plan)
        if self.confirm_kind == "local":
            # The explicit confirmation marker is passed only here.
            self._commit_local()
            return None
        if self.confirm_kind == "claude":
            self._commit_claude()
            return None
        if self.confirm_kind == "global":
            try:
                changed = self.manager.commit_global_settings(self.global_preview)
                self.global_values = self.manager.global_settings_report()
                self.message = "Applied global settings" if changed else "No change was needed"
            except ManagerError as exc:
                self.message = str(exc)
            self.message_return_view = "global-settings"
            self.view = "message"
            self.global_preview = None
            return None
        if self.preview is None:
            return None
        try:
            if self.confirm_kind == "pause":
                result = self.manager.commit_pause(self.pause_plan)
                if result.pending_drain:
                    self.message = result.pending_drain.detail
                else:
                    self.message = f"{result.seat_id} is inactive"
                self.view = "message"
            elif self.confirm_kind == "activate":
                self._start_activation(self.preview)
            elif self.confirm_kind == "add":
                result = self.manager.commit(self.preview)
                self.selected_id = result.seat_id
                if self.opened_from_inventory:
                    self.inventory_selected = f"codex:{result.seat_id}"
                self.show_history = False
                self.message = (
                    f"Added {result.seat_id} as inactive. Next: configure settings, "
                    "sign in, then Reactivate (as primary if it is the first active seat)."
                )
                self.message_return_view = "actions"
                self.view = "message"
            elif self.confirm_kind == "remove":
                seat_id = self.preview.seat_id
                self.manager.commit(self.preview)
                self.show_history = False
                self.selected_id = None
                self._seat_ids()
                self.message = (
                    f"Removed {seat_id} from service. Its home, credentials, and "
                    "historical attribution were preserved; press h in the seat list "
                    "to view retired history."
                )
                self.message_return_view = "list"
                self.view = "message"
            else:
                result = self.manager.commit(self.preview)
                self.message = "Applied" if result.changed else "No change was needed"
                self.message_return_view = "actions"
                self.view = "message"
        except ManagerError as exc:
            self.message = str(exc)
            self.message_return_view = self.confirm_return_view
            self.view = "message"
        return None

    def _start_activation(self, preview: ActionPreview) -> None:
        assert self.manager is not None
        self.activation_cancel = threading.Event()
        self.activation_result = None
        self.activation_error = None

        def work() -> None:
            try:
                result = self.manager.run_activation_checks(
                    preview, self.activation_cancel
                )
                with self._result_lock:
                    self.activation_result = result
            except Exception as exc:  # surfaced as a structured UI failure
                with self._result_lock:
                    self.activation_error = str(exc)

        self.activation_thread = threading.Thread(
            target=work, name="seat-activation-check", daemon=True
        )
        self.activation_thread.start()
        self.view = "busy"
        self.message = "Checking login and fresh provider usage… Esc cancels"

    def _start_reset_work(self, phase: str) -> None:
        assert self.manager is not None and self.selected_id is not None
        if self.reset_thread is not None and self.reset_thread.is_alive():
            self.message = "A reset action is already in progress"
            return
        self.reset_phase = phase
        self.reset_cancel = threading.Event()
        self.reset_result = None
        self.reset_error = None
        root, settings, seat_id = self.manager.root, self.manager.settings, self.selected_id
        preview = self.reset_preview

        def work() -> None:
            try:
                if phase == "preview":
                    result = prepare_banked_reset_redemption(
                        root, settings, seat_id, cancel_event=self.reset_cancel)
                else:
                    assert preview is not None
                    result = redeem_banked_reset(
                        root, settings, preview, current_user_request=True,
                        cancel_event=self.reset_cancel)
                with self._result_lock:
                    self.reset_result = result
            except Exception as exc:  # provider failure must return to the UI
                with self._result_lock:
                    self.reset_error = str(exc)

        self.reset_thread = threading.Thread(target=work, name="seat-reset-action", daemon=True)
        self.reset_thread.start()
        self.view = "busy"
        self.message = "Reading fresh credit and identity… Esc cancels" if phase == "preview" else "Confirming one provider reset attempt…"

    def poll(self) -> bool:
        changed = False
        if self.manager is not None and time.monotonic() - self.last_reconcile >= 0.5:
            self.last_reconcile = time.monotonic()
            try:
                self.manager.reconcile_pending_drains()
            except ManagerError as exc:
                self.message = str(exc)
                changed = True
        if self.manager is not None and (self.local_screen or self.inventory_screen):
            # Probe results arrive on the refresher worker; a snapshot change
            # only marks the frame dirty so the cached report is re-read.
            try:
                snapshot = self._local().local_snapshot()
            except (ManagerError, AttributeError):
                snapshot = None
            signature = (
                None
                if snapshot is None
                else (
                    getattr(snapshot, "generation", None),
                    getattr(snapshot, "probing", None),
                    tuple(getattr(snapshot, "pending", ()) or ()),
                )
            )
            if signature != self._probe_signature:
                self._probe_signature = signature
                self._local_report_cache = None
                changed = True
        reset_thread = self.reset_thread
        if self.view == "busy" and reset_thread is not None and not reset_thread.is_alive():
            with self._result_lock:
                result, error = self.reset_result, self.reset_error
            if self.reset_phase == "preview" and not error and isinstance(result, dict) and not self.reset_cancel.is_set():
                self.reset_preview = result
                self.confirm_kind = "redeem-reset"
                self.confirm_return_view = "actions"
                self.scroll = 0
                self.view = "confirm"
            else:
                self.message = (f"Reset action failed: {error}" if error else
                                f"Reset outcome: {result.get('outcome', 'unknown')}; post-refresh: {result.get('refresh_error') or 'ok'}"
                                if isinstance(result, dict) else "Reset preview canceled")
                self.view = "message"
                self.reset_preview = None
            self.reset_thread = None
            self.reset_phase = None
            return True
        thread = self.activation_thread
        if self.view != "busy" or thread is None or thread.is_alive():
            return changed
        with self._result_lock:
            checks = self.activation_result
            error = self.activation_error
        try:
            if error:
                self.message = f"Activation checks failed: {error}"
            elif checks is None:
                self.message = (
                    "Activation checks ended without a result; seat remains inactive"
                )
            elif not checks.passed:
                self.message = checks.detail + "; seat remains inactive"
            else:
                assert self.preview is not None and self.manager is not None
                with self._activation_gate:
                    if self.activation_cancel.is_set():
                        self.message = (
                            "Activation canceled before commit; seat remains inactive"
                        )
                    else:
                        self.manager.commit_activation(self.preview, checks)
                        self.message = (
                            f"{checks.seat_id} activated after confirmed checks"
                        )
        except ManagerError as exc:
            self.message = str(exc)
        self.activation_thread = None
        self.view = "message"
        return True

    def shutdown(self, timeout: float = 10.0) -> bool:
        self._cancel_activation()
        thread = self.activation_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, timeout))
        reset_thread = self.reset_thread
        if reset_thread is not None and reset_thread.is_alive():
            reset_thread.join(timeout=max(0.0, timeout))
        return (thread is None or not thread.is_alive()) and (reset_thread is None or not reset_thread.is_alive())

    def _preview_material(self) -> list[str]:
        if self.confirm_kind == "redeem-reset" and self.reset_preview is not None:
            credit = self.reset_preview["credit"]
            return [
                f"Seat: {self.reset_preview['seat']}",
                f"Home: {self.reset_preview['home']}",
                f"Account: {json.loads(self.reset_preview['provider_identity'])['email']}",
                f"Available banked resets: {self.reset_preview['available_count']}",
                f"Selected credit: {credit.get('title') or credit.get('reset_type') or 'credit'} ({credit['fingerprint']})",
                f"Granted: {_credit_date(credit.get('granted_at'))}; expires: {_credit_date(credit.get('expires_at'), credit.get('expiry_status'))}",
                "Effect: spend exactly one banked reset. This cannot be undone.",
                "The login, seat registry and credit will be checked again before spending.",
            ]
        if self.confirm_kind == "login":
            return [
                f"Seat: {self.login_plan.seat_id if self.login_plan else self.selected_id}",
                "The dashboard will quiesce provider work, restore the real terminal,",
                "revalidate this confirmed settings/home identity, and run the guarded",
                "`codex login` contract with the confirmed settings snapshot.",
                "No lifecycle or capacity setting changes as part of login.",
            ]
        if self.confirm_kind == "local":
            return self._local_preview_material()
        if self.confirm_kind == "claude":
            return self._claude_preview_material()
        if self.confirm_kind == "global" and self.global_preview is not None:
            diff = difflib.unified_diff(
                self.global_preview.before.splitlines(keepends=True),
                self.global_preview.after.splitlines(keepends=True),
                fromfile="settings.before", tofile="settings.after",
            )
            return [
                "Action: set global settings",
                *(line.rstrip("\n") for line in diff),
            ]
        assert self.preview is not None
        before = self.preview.before.splitlines(keepends=True)
        after = self.preview.after.splitlines(keepends=True)
        diff = list(
            difflib.unified_diff(
                before, after, fromfile="settings.before", tofile="settings.after"
            )
        )
        material = [
            f"Action: {self.preview.action}  Seat: {self.preview.seat_id}",
            *[f"WARNING: {warning}" for warning in self.preview.warnings],
            *(line.rstrip("\n") for line in diff),
        ]
        return material or ["No settings bytes would change."]

    @staticmethod
    def _clip(value: Any, width: int, *, ellipsis: bool = True) -> str:
        """Sanitize and clip one value without splitting a display cell."""
        text = clean_text(value)
        if width <= 0:
            return ""
        if display_width(text) <= width:
            return text
        limit = max(0, width - (1 if ellipsis else 0))
        kept: list[str] = []
        used = 0
        for character in text:
            size = display_width(character)
            if used + size > limit:
                break
            kept.append(character)
            used += size
        return "".join(kept) + ("…" if ellipsis else "")

    def _paint(self, text: str, style: str | None) -> str:
        if not self._color or not style or not text:
            return text
        if style == "title":
            return f"{ANSI['bold']}{ANSI['cyan']}{text}{ANSI['reset']}"
        if style == "rule":
            style = "gray"
        return f"{ANSI[style]}{text}{ANSI['reset']}"

    def _render_parts(
        self,
        parts: LineParts,
        width: int,
        *,
        fill_style: str | None = None,
    ) -> str:
        """Render trusted styles around sanitized, width-bounded content."""
        remaining = max(0, width)
        rendered: list[str] = []
        used = 0
        for raw, style in parts:
            if remaining <= 0:
                break
            text = self._clip(raw, remaining, ellipsis=style != "rule")
            rendered.append(self._paint(text, style))
            cells = display_width(text)
            remaining -= cells
            used += cells
        if fill_style and used < width:
            rendered.append(self._paint(" " * (width - used), fill_style))
        return "".join(rendered)

    @staticmethod
    def _header(title: str, context: Any = "") -> LineParts:
        parts: LineParts = [(title, "title")]
        if context:
            parts.extend([("  /  ", "gray"), (context, "purple")])
        return parts

    @staticmethod
    def _section(title: str) -> LineParts:
        return [
            ("── ", "gray"),
            (title.upper(), "purple"),
            (" ", None),
            ("─" * 160, "rule"),
        ]

    def _detail_row(
        self,
        label: str,
        value: Any,
        width: int,
        style: str | None = None,
    ) -> list[LineParts]:
        value_text = clean_text(value)
        if width < 32:
            return [
                [(line, style)]
                for line in self._wrap_words(f"{label}: {value_text}", width)
            ]
        label_width = min(18, max(12, width // 3))
        if display_width(label) + 1 > label_width:
            return [
                [(line, style)]
                for line in self._wrap_words(f"{label}: {value_text}", width)
            ]
        value_width = max(1, width - label_width - 1)
        pieces = self._wrap_words(value_text, value_width) or [""]
        return [
            [
                ((f"{label}:" if index == 0 else "").ljust(label_width), "gray"),
                (" ", None),
                (piece, style),
            ]
            for index, piece in enumerate(pieces)
        ]

    @staticmethod
    def _wrap_words(value: Any, width: int) -> list[str]:
        """Wrap prose on word boundaries, falling back to display-cell chunks."""
        width = max(1, width)
        words = clean_text(value).split(" ")
        output: list[str] = []
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if display_width(candidate) <= width:
                current = candidate
                continue
            if current:
                output.append(current)
                current = ""
            if display_width(word) <= width:
                current = word
            else:
                chunks = wrap_lines([word], width)
                output.extend(chunks[:-1])
                current = chunks[-1]
        output.append(current)
        return output

    def _selected_detail(
        self, record: Mapping[str, Any], width: int, seat_id: str | None = None
    ) -> list[LineParts]:
        """Build the grouped, scrollable selected-seat detail pane."""
        from .display import _countdown, _usage_age_text

        seat_id = self.selected_id if seat_id is None else seat_id
        assert self.manager is not None and seat_id is not None
        provider = record.get("provider", {})
        provider = provider if isinstance(provider, Mapping) else {}
        claim = record.get("claim", {})
        claim = claim if isinstance(claim, Mapping) else {}
        capacity = record.get("capacity", {})
        capacity = capacity if isinstance(capacity, Mapping) else {}
        identity = self.manager.identity_report(seat_id)
        identity = identity if isinstance(identity, Mapping) else {}
        lines: list[LineParts] = []

        def section(name: str) -> None:
            lines.append(self._section(name))

        def row(label: str, value: Any, style: str | None = None) -> None:
            lines.extend(self._detail_row(label, value, width, style))

        lifecycle = record.get("lifecycle") or "unknown"
        role = record.get("role") or "worker"
        work = "idle"
        if claim.get("live"):
            work = claim.get("task_kind") or claim.get("job_id") or "live job"
        row("Seat", f"{seat_id} · {lifecycle} · {role}", "cyan")
        row("Work", work, "cyan" if claim.get("live") else "gray")

        section("Usage")
        observed = provider.get("observed_at")
        freshness = "Not reported"
        if isinstance(observed, (int, float)):
            age = max(0, int(time.time() - float(observed)))
            freshness = f"Updated {_usage_age_text(age)} ago"
        row(
            "Freshness",
            freshness,
            "cyan" if isinstance(observed, (int, float)) else "gray",
        )
        windows = capacity.get("active_windows")
        if isinstance(windows, list) and windows:
            for window in windows:
                if not isinstance(window, Mapping):
                    continue
                duration = window.get("window_minutes")
                duration_text = (
                    f"{duration:g} min"
                    if isinstance(duration, (int, float))
                    else "duration unavailable"
                )
                resets_at = window.get("resets_at")
                reset_text = (
                    _countdown(resets_at)
                    if isinstance(resets_at, (int, float))
                    else "unavailable"
                )
                row(
                    str(window.get("name") or "Window"),
                    f"{window.get('remaining_pct')}% remaining · {duration_text} · resets {reset_text}",
                    "green",
                )
        else:
            row("Windows", "Unavailable — no current provider window", "gray")
        bank = record.get("banked_resets", {})
        bank = bank if isinstance(bank, Mapping) else {}
        row("Banked resets", f"{bank.get('available_count') if bank.get('status') == 'fresh' else 'unknown'} · {bank.get('status', 'unknown')}")
        for credit in bank.get("credits", []):
            row("Credit", f"{credit.get('title') or credit.get('reset_type') or 'credit'} · {credit.get('status') or 'unknown'} · granted {_credit_date(credit.get('granted_at'))} · expires {_credit_date(credit.get('expires_at'), credit.get('expiry_status'))}")
        refresh = self.manager.refresh_state()
        if refresh is not None:
            state = (
                "refreshing"
                if refresh.active == seat_id
                else (
                    "queued"
                    if refresh.queued == seat_id
                    else refresh.last_result
                )
            )
            if state:
                row("Refresh", state, "cyan")

        section("Account")
        identity_status = clean_text(identity.get("status") or "unavailable")
        email = identity.get("email")
        if email:
            suffix = " · stale" if identity_status == "stale" else ""
            row("Email", f"{email}{suffix}", "amber" if suffix else None)
        else:
            reason = identity.get("reason") or "not reported by provider"
            row("Email", f"Unavailable — {reason}", "gray")
        if identity.get("account_id"):
            row("Account ID", identity["account_id"])
        identity_observed = identity.get("observed_at")
        if isinstance(identity_observed, (int, float)):
            age = max(0, int(time.time() - float(identity_observed)))
            row("Identity", f"{identity_status} · verified {_usage_age_text(age)} ago")
        else:
            row(
                "Identity",
                identity_status,
                "gray" if identity_status == "unavailable" else None,
            )
        health = clean_text(provider.get("health") or "unknown")
        health_style = (
            "green"
            if health == "ok"
            else "red"
            if health not in {"unknown", "unavailable"}
            else "gray"
        )
        row("Authentication", health, health_style)

        section("Plan")
        configured_profile = capacity.get("profile") or "unverified"
        configured_label = next(
            (
                preset["label"]
                for preset in CAPACITY_PLAN_PRESETS
                if preset["profile"] == configured_profile
            ),
            configured_profile,
        )
        row("Configured", f"{configured_label} · {configured_profile}", "purple")
        declared = capacity.get("relative_capacity")
        if isinstance(declared, (int, float)):
            configured_capacity = (
                f"{declared:g}x · {capacity.get('source') or 'source unavailable'}"
            )
        else:
            effective = capacity.get("effective_relative_capacity", 1)
            effective_text = (
                f"{effective:g}"
                if isinstance(effective, (int, float))
                else clean_text(effective)
            )
            configured_capacity = f"Unverified · scheduler fallback {effective_text}x"
        row("Capacity", configured_capacity, "amber" if declared is None else None)
        observed_plan = identity.get("observed_plan_type")
        if observed_plan and identity_status == "stale":
            observed_plan = f"{observed_plan} · stale"
        row(
            "Observed account",
            observed_plan or "Unavailable",
            "gray" if not observed_plan else None,
        )
        usage_plan = capacity.get("observed_plan_type")
        row(
            "Observed usage",
            usage_plan or "Unavailable",
            "gray" if not usage_plan else None,
        )

        section("Credits")
        usage_credits = capacity.get("credits")
        if not isinstance(usage_credits, Mapping):
            row("Workspace credits", "Unavailable — not reported", "gray")
        elif usage_credits.get("unlimited") is True:
            row("Workspace credits", "Unlimited · provider reported", "green")
        elif usage_credits.get("balance") is not None:
            row("Workspace balance", usage_credits["balance"], "green")
        elif usage_credits.get("has_credits") is True:
            row("Workspace credits", "Available · balance unavailable", "green")
        elif usage_credits.get("has_credits") is False:
            row("Workspace credits", "None available", "gray")
        else:
            row("Workspace credits", "Unavailable — incomplete provider data", "gray")
        return lines

    # -- local rendering -----------------------------------------------------
    @staticmethod
    def _age_text(timestamp: Any) -> str | None:
        from .display import _usage_age_text

        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            return None
        return f"{_usage_age_text(max(0.0, time.time() - float(timestamp)))} ago"

    @staticmethod
    def _state_style(state: Any) -> str | None:
        return {
            "ready": "green",
            "busy": "amber",
            "loading": "amber",
            "gaming": "amber",
            "asleep": "gray",
            "degraded": "red",
        }.get(clean_text(state))

    @staticmethod
    def _lifecycle_style(lifecycle: Any) -> str | None:
        return {
            "active": "green",
            "draining": "amber",
            "inactive": "gray",
            "retired": "gray",
        }.get(clean_text(lifecycle))

    @staticmethod
    def _local_status_word(status: Any) -> tuple[str, str | None]:
        """Compact list-column word and style for a shared local status row.

        ``status`` is the dashboard-derived row carried by the manager report
        (never the raw availability cache, whose gated and expired entries all
        read ``degraded``). A missing row renders as ``unknown``.
        """
        status = status if isinstance(status, Mapping) else {}
        key = clean_text(status.get("key") or "unknown")
        word = {"model_mismatch": "mismatch"}.get(key, key)
        style = {
            "working": "cyan",
            "ready": "green",
            "shadow": "gray",
            "off": "gray",
            "stale": "amber",
            "model_mismatch": "amber",
            "unknown": "gray",
        }.get(key, ManagerUI._state_style(key))
        return word[:9], style

    def _local_row(
        self, entry_id: str, width: int, *, selected: bool, report: Mapping[str, Any]
    ) -> str:
        marker = ">" if selected else " "
        if self.screen == "hosts":
            entry = report["hosts"].get(entry_id, {})
        else:
            entry = report["seats"].get(entry_id, {})
        config = entry.get("config", {}) if isinstance(entry, Mapping) else {}
        third, third_style = self._local_status_word(
            entry.get("status") if isinstance(entry, Mapping) else None
        )
        third_width = 9
        lifecycle = clean_text(config.get("lifecycle") or "unknown")
        label = config.get("label") or ""
        plain = f"{marker} {entry_id:<10} {lifecycle:<9} {third:<{third_width}}  {label}"
        if selected:
            return self._render_parts([(plain, "selected")], width, fill_style="selected")
        return self._render_parts(
            [
                (marker + " ", "gray"),
                (f"{entry_id:<10} ", None),
                (f"{lifecycle:<9}", self._lifecycle_style(lifecycle)),
                (" ", None),
                (f"{third:<{third_width}}", third_style),
                ("  ", None),
                (label, None),
            ],
            width,
        )

    def _availability_lines(
        self,
        availability: Any,
        width: int,
        *,
        eligibility: Mapping[str, Any] | None = None,
        status: Any = None,
        jobs_error: Any = None,
    ) -> list[LineParts]:
        """Detail rows for one host's or seat's cached availability.

        ``status`` is the shared dashboard derivation carried by the manager
        report. Off and expired cache entries are shown as ``off`` and ``stale``
        (with the last known state and its age) instead of the raw cache's
        ``degraded`` placeholder; the control-plane reason/eligibility rows
        stay verbatim so routing decisions remain explainable.
        """
        lines: list[LineParts] = []
        availability = availability if isinstance(availability, Mapping) else {}
        status = status if isinstance(status, Mapping) else {}
        key = clean_text(status.get("key") or "unknown")
        state_label = clean_text(status.get("state") or "? unknown")
        detail = clean_text(status.get("detail") or "")
        _word, style = self._local_status_word(status)
        lines.extend(
            self._detail_row(
                "Status", f"{state_label} · {detail}" if detail else state_label, width, style
            )
        )
        reason = clean_text(
            (eligibility or {}).get("reason") or availability.get("reason") or "unknown"
        )
        reason_style = "green" if reason in {"ready", "claimed"} else "amber"
        if eligibility is not None:
            eligible = "yes" if eligibility.get("eligible") else "no"
            lines.extend(
                self._detail_row(
                    "Eligible",
                    f"{eligible} · {reason}",
                    width,
                    "green" if eligibility.get("eligible") else "amber",
                )
            )
        else:
            lines.extend(self._detail_row("Reason", reason, width, reason_style))
        observed_at = status.get("observed_at")
        if isinstance(observed_at, bool) or not isinstance(observed_at, (int, float)):
            observed_at = None
        age_text = clean_text(status.get("age_text") or "—")
        last_state = clean_text(status.get("last_state") or "")
        # The host's control-plane reason says whether the cache was read at all;
        # a seat's own mode_off eligibility does not stop the host read.
        gate = clean_text(availability.get("reason") or "")
        if observed_at is not None and age_text != "—":
            age = f"{age_text} ago"
            if key == "stale":
                age += f" · expired, last known state {last_state or 'unknown'}"
            observed_style: str | None = "amber" if key == "stale" else None
        elif gate == "mode_off":
            age = "Not read — local mode off, cache untouched"
            observed_style = "gray"
        elif gate in {"unknown_host", "lifecycle_ineligible", "no_enabled_seats"}:
            age = f"Not read — host gated off ({gate.replace('_', ' ')})"
            observed_style = "gray"
        elif gate == "configuration_changed":
            age = "Not observed — configuration changed"
            observed_style = "gray"
        else:
            age = "Not observed — no probe cached yet"
            observed_style = "gray"
        lines.extend(self._detail_row("Observed", age, width, observed_style))
        served = status.get("model_profile") or availability.get("model_profile")
        if served:
            lines.extend(self._detail_row("Served model", served, width))
        if jobs_error:
            lines.extend(
                self._detail_row(
                    "Live jobs", f"Unreadable · {jobs_error}", width, "amber"
                )
            )
        return lines

    def _probe_lines(
        self, host_id: str | None, report: Mapping[str, Any], width: int
    ) -> list[LineParts]:
        lines: list[LineParts] = []
        snapshot = report.get("probe")
        if not isinstance(snapshot, Mapping) or not snapshot.get("enabled"):
            lines.extend(
                self._detail_row(
                    "Live probe",
                    "Unavailable — no probe service in this session",
                    width,
                    "gray",
                )
            )
            return lines
        host = report["hosts"].get(host_id) if host_id else None
        last = host.get("probe") if isinstance(host, Mapping) else None
        if snapshot.get("probing") == host_id:
            lines.extend(self._detail_row("Live probe", "Probing now…", width, "cyan"))
        elif host_id in tuple(snapshot.get("pending") or ()):
            lines.extend(self._detail_row("Live probe", "Queued", width, "cyan"))
        elif isinstance(last, Mapping):
            state = clean_text(last.get("state") or "unknown")
            age = self._age_text(last.get("finished_at"))
            text = f"{state} · {last.get('reason') or 'unknown'} · {last.get('source') or 'probe'}"
            if age:
                text += f" · finished {age}"
            lines.extend(self._detail_row("Live probe", text, width, self._state_style(state)))
            if last.get("detail"):
                lines.extend(self._detail_row("Probe detail", last["detail"], width))
        else:
            lines.extend(
                self._detail_row("Live probe", "Idle — no probe result yet", width, "gray")
            )
        periodic = (
            "on · refreshes near the cache TTL"
            if snapshot.get("periodic")
            else "off (--no-active-probe) · Probe now still available"
        )
        lines.extend(self._detail_row("Periodic", periodic, width, "gray"))
        if snapshot.get("last_error"):
            lines.extend(self._detail_row("Probe error", snapshot["last_error"], width, "red"))
        return lines

    def _host_detail(
        self, host_id: str, report: Mapping[str, Any], width: int
    ) -> list[LineParts]:
        host = report["hosts"].get(host_id, {})
        config = host.get("config", {}) if isinstance(host, Mapping) else {}
        lines: list[LineParts] = []

        def row(label: str, value: Any, style: str | None = None) -> None:
            lines.extend(self._detail_row(label, value, width, style))

        lifecycle = config.get("lifecycle") or "unknown"
        row("Host", f"{host_id} · {lifecycle} · {config.get('label') or ''}", "cyan")
        row("Global mode", report.get("global_mode") or "unknown", "purple")
        lines.append(self._section("Endpoint"))
        row("Base URL", config.get("base_url") or "Not configured", None if config.get("base_url") else "gray")
        row("Lease URL", config.get("lease_url") or "None", None if config.get("lease_url") else "gray")
        probe = config.get("availability_probe")
        if isinstance(probe, Mapping) and isinstance(probe.get("argv"), list):
            argv = " ".join(str(part) for part in probe["argv"])
            row("Probe argv", argv)
            row("Probe timeout", f"{probe.get('timeout_seconds')} s")
        else:
            row("Probe argv", "Not configured — probes report probe_not_configured", "gray")
        lines.append(self._section("Capacity"))
        groups = config.get("resource_groups")
        if isinstance(groups, Mapping) and groups:
            row(
                "Resource groups",
                ", ".join(
                    f"{group_id} ×{group.get('max_concurrency') if isinstance(group, Mapping) else '?'}"
                    for group_id, group in groups.items()
                ),
            )
        else:
            row("Resource groups", "None", "gray")
        seats = host.get("seats") if isinstance(host, Mapping) else None
        row("Seats", ", ".join(seats) if seats else "None registered", None if seats else "gray")
        lines.append(self._section("Availability (cached)"))
        lines.extend(
            self._availability_lines(
                host.get("availability") if isinstance(host, Mapping) else None,
                width,
                status=host.get("status") if isinstance(host, Mapping) else None,
                jobs_error=report.get("jobs_error"),
            )
        )
        lines.append(self._section("Live probe"))
        lines.extend(self._probe_lines(host_id, report, width))
        return lines

    def _local_seat_detail(
        self, seat_id: str, report: Mapping[str, Any], width: int
    ) -> list[LineParts]:
        seat = report["seats"].get(seat_id, {})
        config = seat.get("config", {}) if isinstance(seat, Mapping) else {}
        eligibility = seat.get("eligibility") if isinstance(seat, Mapping) else None
        eligibility = eligibility if isinstance(eligibility, Mapping) else None
        host_id = config.get("host")
        host = report["hosts"].get(host_id, {}) if isinstance(host_id, str) else {}
        host_config = host.get("config", {}) if isinstance(host, Mapping) else {}
        lines: list[LineParts] = []

        def row(label: str, value: Any, style: str | None = None) -> None:
            lines.extend(self._detail_row(label, value, width, style))

        lifecycle = config.get("lifecycle") or "unknown"
        row("Seat", f"{seat_id} · {lifecycle} · {config.get('label') or ''}", "cyan")
        lines.append(self._section("Mode"))
        effective = (seat.get("effective_mode") if isinstance(seat, Mapping) else None) or "off"
        row("Global", report.get("global_mode") or "unknown", "purple")
        row("Seat", config.get("mode") or "off")
        row("Effective", effective, "gray" if effective == "off" else "cyan")
        lines.append(self._section("Placement"))
        if isinstance(host_id, str) and host_config:
            row(
                "Host",
                f"{host_id} · {host_config.get('lifecycle') or 'unknown'} · {host_config.get('label') or ''}",
            )
        else:
            row("Host", f"{host_id or 'none'} · not registered", "red")
        groups = host_config.get("resource_groups")
        group_id = config.get("resource_group")
        group = groups.get(group_id) if isinstance(groups, Mapping) and group_id else None
        group_max = group.get("max_concurrency") if isinstance(group, Mapping) else None
        row(
            "Resource group",
            f"{group_id or 'none'}" + (f" · max {group_max}" if group_max is not None else " · unknown"),
            None if group_max is not None else "amber",
        )
        row("Concurrency", config.get("max_concurrency", "unknown"))
        lines.append(self._section("Model"))
        row("Model profile", config.get("model_profile") or "unknown")
        row("Context window", config.get("context_window_size") or "Qwen default")
        row("Harness", config.get("harness") or "unknown")
        capabilities = config.get("capabilities")
        capabilities = capabilities if isinstance(capabilities, Mapping) else {}
        shapes = capabilities.get("task_shapes")
        validation = capabilities.get("validation")
        row("Task shapes", ", ".join(map(str, shapes)) if isinstance(shapes, list) and shapes else "None", None if shapes else "gray")
        row("Validation", ", ".join(map(str, validation)) if isinstance(validation, list) and validation else "None", None if validation else "gray")
        row("Fingerprint", config.get("runtime_fingerprint") or "missing")
        lines.append(self._section("Availability (cached)"))
        lines.extend(
            self._availability_lines(
                host.get("availability") if isinstance(host, Mapping) else None,
                width,
                eligibility=eligibility,
                status=seat.get("status") if isinstance(seat, Mapping) else None,
                jobs_error=report.get("jobs_error"),
            )
        )
        lines.append(self._section("Live probe"))
        lines.extend(
            self._probe_lines(host_id if isinstance(host_id, str) else None, report, width)
        )
        return lines

    def _seat_row(self, seat_id: str, width: int, *, selected: bool) -> str:
        assert self.manager is not None
        entry = self.manager.settings["pool"]["seats"][seat_id]
        lifecycle = clean_text(entry.get("lifecycle") or "unknown")
        label = entry.get("label") or ""
        marker = ">" if selected else " "
        plain = f"{marker} {seat_id:<7} {lifecycle:<9}  {label}"
        if selected:
            return self._render_parts(
                [(plain, "selected")], width, fill_style="selected"
            )
        style = {
            "active": "green",
            "draining": "amber",
            "inactive": "gray",
            "retired": "gray",
        }.get(lifecycle)
        return self._render_parts(
            [
                (marker + " ", "gray"),
                (f"{seat_id:<7} ", None),
                (f"{lifecycle:<9}", style),
                ("  ", None),
                (label, None),
            ],
            width,
        )

    def _footer(self, text: str, width: int) -> str:
        return self._render_parts([(text, "gray")], width)

    def _esc_hint(self, default: str = "Esc") -> str:
        """Footer word for Escape: names the Inventory when it returns there."""
        return "Esc inventory" if self.opened_from_inventory else default

    def _menu_frame(
        self,
        *,
        title: str,
        context: Any,
        section: str,
        rows: list[str],
        selected: int,
        page_size_attribute: str,
        footer: str,
        width: int,
        height: int,
    ) -> str:
        available = max(1, height - 3)
        setattr(self, page_size_attribute, available)
        start = self._window_start(len(rows), selected, available)
        lines: list[str] = [self._render_parts(self._header(title, context), width)]
        lines.append(self._render_parts(self._section(section), width))
        for index in range(start, min(len(rows), start + available)):
            marker = ">" if index == selected else " "
            raw = f"{marker} {rows[index]}"
            lines.append(
                self._render_parts(
                    [(raw, "selected" if index == selected else None)],
                    width,
                    fill_style="selected" if index == selected else None,
                )
            )
        lines.append(self._footer(footer, width))
        return "\n".join(lines[:height])

    def render(self, width: int, height: int, overview: str) -> str:
        width, height = max(1, width), max(1, height)
        self._color = bool(ANSI_RE.search(overview))
        self._too_small = height < 6 or width < 24
        if self._too_small:
            lines: list[LineParts] = [
                self._header("SEAT MANAGER"),
                [("Terminal too small", "amber")],
                [("Resize to at least 24×6", None)],
                [("q quit", "gray")],
            ]
            return "\n".join(self._render_parts(line, width) for line in lines[:height])
        if self.view == "overview":
            # dashboard() already sanitizes data, bounds display cells, and
            # paints trusted ANSI styling. Plain-text sanitization here turns
            # its escapes into visible ?[32m text and destroys column widths.
            lines = overview.splitlines()[: height - 1]
            footer = self._footer(
                "m manage · g settings · ? help · q quit"
                if width >= 48 else "m manage · ? help · q quit",
                width,
            )
            return "\n".join([*lines, footer])
        if self.view == "help":
            body = wrap_lines(
                [
                    "Overview: m manage, q quit, ? help.",
                    "Global settings: g from overview, inventory, or Codex seat list. Choose a typed preference, inspect a settings diff, then Enter/y confirms or Esc/n cancels. The preview fails if settings changed.",
                    "Goal review mode (off/advisory/blocking) governs automatic goal supervision only; delegate, await, and ChangeSet apply review only when a review is explicitly requested. Review strategy is also the default for explicit codex-seat review. Every preference is optional; review mode defaults to off.",
                    "Inventory: m lands here. It lists every Codex seat, the Claude account, and every local seat (hosts are not seats; see Local Hosts). Enter opens that item's controls and Esc from them returns here with the same selection. a or the single Add seat row opens a type menu: Codex (add form), Claude (the one existing account's setup), or Local (seat form; register a host first if none). h toggles retired history.",
                    "Tab / Shift-Tab cycle Inventory, frontier seats, Local Hosts, Local Seats, and Claude account; lists entered by Tab return to the overview on Esc. Local Hosts is also reachable from a local seat's Manage host action.",
                    "Frontier seats: a adds a dynamically allocated seat; h toggles retired history; arrows or j/k select a stable ID.",
                    "Local screens: a adds a host or seat, g sets the global local mode, p (hosts) queues a read-only probe on the refresher; Enter opens lifecycle, mode, edit, probe, and remove actions. Complex fields are validated JSON.",
                    "Claude account: Enter opens mode, subscription tier label, and small/big minimum headroom percent edits; each is previewed and confirmed. No provider client runs and no credentials are read.",
                    "Enter opens/accepts and Escape cancels or goes back. Remove retires only an inactive, idle, non-primary/deputy Codex seat, or an inactive, mode-off local seat; both keep retired history.",
                    "Previews are digest-pinned. Provider checks, refreshes, and local probes stay off the key loop.",
                    "A canceled busy-seat wait leaves the seat draining. Quitting does not finish it unattended.",
                    "Removing preserves the seat home, credentials, and historical attribution. A selected Codex seat offers a confirmed one-credit Redeem action. Billing, membership, and credential revocation remain separate owner actions.",
                ],
                width,
            )
            lines: list[LineParts] = [
                self._header("HELP"),
                self._section("Keyboard & safety"),
            ]
            lines.extend([[(line, None)] for line in body])
            return self._bounded(lines, "Esc back · q quit", width, height)
        if self.view in {"global-settings", "global-choice", "global-input"}:
            return self._render_global_settings(width, height)
        if self.local_screen and self.view in LOCAL_VIEWS:
            return self._render_local(width, height)
        if self.inventory_screen and self.view in INVENTORY_VIEWS:
            return self._render_inventory(width, height)
        if self.claude_screen and self.view in CLAUDE_VIEWS:
            return self._render_claude(width, height)
        if self.view == "list":
            seat_ids = self._seat_ids()
            selected = self.selected_id
            record = self._selected_record()
            header = self._render_parts(
                self._header(
                    "SEAT MANAGER",
                    (
                        "all identities + retired history"
                        if self.show_history
                        else "registered seats"
                    )
                    + " · Tab: local hosts",
                ),
                width,
            )
            if width >= 84:
                footer_text = (
                    "a Add seat · g global settings · h history · Tab local · ↑↓ seat · Enter actions · "
                    "PgUp/Dn detail · Esc/q"
                )
            elif width >= 50:
                footer_text = "a Add seat · g settings · h all · Enter · q quit"
            elif width >= 32:
                footer_text = "a Add · g · Enter · Esc/q"
            else:
                footer_text = "a Add · h all · q quit"
            return self._render_master_detail(
                header=header,
                ids=seat_ids,
                selected=selected,
                count_title=f"Seats {len(seat_ids)}",
                column_header="  ID      STATE      LABEL",
                row=lambda seat_id, row_width, chosen: self._seat_row(
                    seat_id, row_width, selected=chosen
                ),
                detail_for=lambda detail_width: (
                    self._selected_detail(record, detail_width) if record else []
                ),
                empty_short="  No seats. Press a to Add seat.",
                empty_long="No seats registered. Press a to Add seat.",
                footer_text=footer_text,
                width=width,
                height=height,
            )
        if self.view == "add-form":
            profile = self.add_buffers["profile"] or "unverified"
            rows = [
                f"Label: {self.add_buffers['label']}",
                f"Home: {self.add_buffers['home'] or '<default>'}",
                f"Plan: {profile}",
                "Preview Add seat",
            ]
            return self._menu_frame(
                title="ADD SEAT",
                context="new inactive Codex seat",
                section="Seat details",
                rows=rows,
                selected=self.add_field_index,
                page_size_attribute="_field_page_size",
                footer="↑↓ · Enter · Esc cancel"
                + (" (back to inventory)" if self.opened_from_inventory else ""),
                width=width,
                height=height,
            )
        if self.view == "add-input":
            field = ADD_FIELDS[self.add_field_index]
            lines: list[LineParts] = [
                self._header("ADD SEAT", field.label),
                self._section("Value"),
                [("> " + self.input_buffer, "selected")],
                *[
                    [(line, "gray")]
                    for line in wrap_lines(
                        [
                            "The label must be 1–128 visible characters. Home must be blank or an absolute path."
                        ],
                        width,
                    )
                ],
            ]
            return self._bounded(
                lines,
                "Enter · Esc cancel" if width < 48 else "Enter accept · Esc cancel · Backspace edit",
                width,
                height,
            )
        if self.view == "add-presets":
            return self._menu_frame(
                title="ADD SEAT",
                context="configured plan",
                section="Plan preset",
                rows=[str(choice["label"]) for choice in ADD_PLAN_CHOICES],
                selected=self.add_preset_index,
                page_size_attribute="_capacity_preset_page_size",
                footer="↑↓/j/k · PgUp/PgDn · Enter select · Esc cancel",
                width=width,
                height=height,
            )
        if self.view == "actions":
            actions = self._actions()
            return self._menu_frame(
                title="SEAT ACTIONS",
                context=self.selected_id,
                section="Available actions",
                rows=[label for label, _action in actions],
                selected=self.menu_index,
                page_size_attribute="_action_page_size",
                footer=(
                    f"↑↓/j/k · PgUp/PgDn · Enter · {self._esc_hint()} · q quit"
                    if width >= 48
                    else "↑↓ · Enter · Esc · q"
                ),
                width=width,
                height=height,
            )
        if self.view == "settings":
            rows = [
                f"{field.label:<34} {self._current_field_value(field)}"
                for field in FIELDS
            ]
            return self._menu_frame(
                title="SEAT SETTINGS",
                context=self.selected_id,
                section="Supported fields",
                rows=rows,
                selected=self.field_index,
                page_size_attribute="_field_page_size",
                footer="Enter edit · ↑↓/j/k · PgUp/PgDn · Esc",
                width=width,
                height=height,
            )
        if self.view == "input":
            field = FIELDS[self.field_index]
            lines: list[LineParts] = [
                self._header("EDIT SETTING", self.selected_id),
                self._section(field.label),
            ]
            lines.extend(
                [
                    [(line, "gray")]
                    for line in wrap_lines(
                        [
                            "Validation and a complete before/after preview follow Enter."
                        ],
                        width,
                    )
                ]
            )
            lines.append([("> " + self.input_buffer, "selected")])
            return self._bounded(
                lines, "Enter preview · Esc cancel · Backspace edit", width, height
            )
        if self.view == "capacity-input":
            lines: list[LineParts] = [
                self._header("EDIT CAPACITY", self.selected_id),
                self._section("Advanced values"),
            ]
            for index, (label, option) in enumerate(CAPACITY_PARTS):
                lines.append(
                    [
                        (
                            f"{'>' if index == self.capacity_index else ' '} {label}: {self.capacity_buffers[option]}",
                            "selected" if index == self.capacity_index else None,
                        )
                    ]
                )
            if height >= 8:
                lines.extend(
                    [
                        [(line, "gray")]
                        for line in wrap_lines(
                            [
                                "Values are validated and previewed atomically; a verified source requires a non-null relative capacity."
                            ],
                            width,
                        )
                    ]
                )
            return self._bounded(
                lines,
                "↑↓ field · type/Backspace edit · Enter preview · Esc cancel",
                width,
                height,
            )
        if self.view == "capacity-presets":
            return self._menu_frame(
                title="CONFIGURED PLAN",
                context=self.selected_id,
                section="Presets",
                rows=[str(preset["label"]) for preset in CAPACITY_CHOICES],
                selected=self.capacity_preset_index,
                page_size_attribute="_capacity_preset_page_size",
                footer="↑↓/j/k · PgUp/PgDn · Enter preview · Esc",
                width=width,
                height=height,
            )
        if self.view == "confirm":
            material: list[tuple[str, str | None]] = []
            for source_line in self._preview_material():
                style = (
                    "red"
                    if source_line.startswith(("WARNING:", "-"))
                    else "green"
                    if source_line.startswith("+")
                    else "purple"
                    if source_line.startswith("@@")
                    else None
                )
                material.extend(
                    (line, style) for line in wrap_lines([source_line], width)
                )
            available = max(1, height - 3)
            self._confirm_page_size = available
            maximum = max(0, len(material) - available)
            self.scroll = min(maximum, self.scroll)
            visible = material[self.scroll : self.scroll + available]
            footer = (
                f"Enter/y confirm · Esc/n cancel · ↑↓ page {self.scroll + 1}-"
                f"{min(len(material), self.scroll + available)}/{len(material)}"
            )
            if self.confirm_kind == "local" and self.local_preview is not None:
                confirm_context: Any = self.local_preview.target_id or "providers.local"
            elif self.confirm_kind == "claude":
                confirm_context = CLAUDE_TARGET
            elif self.confirm_kind == "global":
                confirm_context = "Global settings"
            elif self.preview is not None:
                confirm_context = self.preview.seat_id
            else:
                confirm_context = self._context_id()
            lines: list[LineParts] = [
                self._header("CONFIRM CHANGE", confirm_context),
                self._section("Exact mutation preview"),
            ]
            for line, style in visible:
                lines.append([(line, style)])
            return self._bounded(lines, footer, width, height)
        title = (
            "QUIT WITH PENDING DRAINS"
            if self.view == "quit-confirm"
            else ("WORKING" if self.view == "busy" else "RESULT")
        )
        message_style = "amber" if self.view in {"quit-confirm", "busy"} else None
        lines: list[LineParts] = [
            self._header(title, self._context_id()),
            self._section("Status"),
        ]
        lines.extend(
            [[(line, message_style)] for line in wrap_lines([self.message], width)]
        )
        if self.view == "quit-confirm":
            footer = "Enter/y quit and leave draining · Esc/n stay"
        else:
            footer = "Esc cancel · q quit" if self.view == "busy" else "Enter/Esc back"
        return self._bounded(lines, footer, width, height)

    def _render_master_detail(
        self,
        *,
        header: str,
        ids: list[str],
        selected: str | None,
        count_title: str,
        column_header: str,
        row: Any,
        detail_for: Any,
        empty_short: str,
        empty_long: str,
        footer_text: str,
        width: int,
        height: int,
        master_max: int = 34,
        master_fraction: float = 1 / 3,
    ) -> str:
        """Shared two-column (≥84 cols) or stacked master/detail list frame."""
        selected_index = ids.index(selected) if selected in ids else 0
        if width >= 84:
            master_width = min(master_max, max(28, int(width * master_fraction)))
            detail_width = width - master_width - 3
            body_height = max(1, height - 3)
            row_slots = max(1, body_height - 1)
            row_start = self._window_start(len(ids), selected_index, row_slots)
            detail = detail_for(detail_width)
            detail_max = max(0, len(detail) - body_height)
            self.detail_scroll = min(max(0, self.detail_scroll), detail_max)
            self._detail_page_size = max(1, body_height)
            left_title = self._render_parts(self._section(count_title), master_width)
            right_title = self._render_parts(
                self._section(f"Selected {selected or '-'}"), detail_width
            )
            lines = [header, f"{left_title} │ {right_title}"]
            left_rows = [self._render_parts([(column_header, "gray")], master_width)]
            if not ids:
                left_rows.append(self._render_parts([(empty_short, "gray")], master_width))
            left_rows.extend(
                row(entry_id, master_width, entry_id == selected)
                for entry_id in ids[row_start : row_start + row_slots]
            )
            right_rows = [
                self._render_parts(parts, detail_width)
                for parts in detail[self.detail_scroll : self.detail_scroll + body_height]
            ]
            for index in range(body_height):
                left = left_rows[index] if index < len(left_rows) else " " * master_width
                left += " " * max(0, master_width - display_width(ANSI_RE.sub("", left)))
                right = right_rows[index] if index < len(right_rows) else ""
                lines.append(f"{left} │ {right}")
            lines.append(self._footer(footer_text, width))
            return "\n".join(lines[:height])

        remaining = max(2, height - 4)
        row_budget = min(len(ids), max(1, min(5, remaining // 3))) if ids else 0
        row_start = self._window_start(len(ids), selected_index, row_budget)
        detail_slots = max(1, remaining - row_budget)
        detail = detail_for(width)
        detail_max = max(0, len(detail) - detail_slots)
        self.detail_scroll = min(max(0, self.detail_scroll), detail_max)
        self._detail_page_size = max(1, detail_slots)
        lines = [header, self._render_parts(self._section(count_title), width)]
        if not ids:
            lines.extend(
                self._render_parts([(line, "gray")], width)
                for line in self._wrap_words(empty_long, width)
            )
        lines.extend(
            row(entry_id, width, entry_id == selected)
            for entry_id in ids[row_start : row_start + row_budget]
        )
        first = self.detail_scroll + 1 if detail else 0
        last = min(len(detail), self.detail_scroll + detail_slots)
        lines.append(
            self._render_parts(
                self._section(f"Selected {selected or '-'} · {first}-{last}/{len(detail)}"),
                width,
            )
        )
        lines.extend(
            self._render_parts(parts, width)
            for parts in detail[self.detail_scroll : self.detail_scroll + detail_slots]
        )
        lines.append(self._footer(footer_text, width))
        return "\n".join(lines[:height])

    def _render_local(self, width: int, height: int) -> str:
        hosts_screen = self.screen == "hosts"
        title = SCREEN_TITLES[self.screen]
        if self.view == "list":
            report = self._local_report()
            ids = self._local_ids(report)
            selected = self.local_selected_id
            next_screen = SCREEN_NAMES[SCREENS[(SCREENS.index(self.screen) + 1) % len(SCREENS)]]
            header = self._render_parts(
                self._header(
                    title,
                    f"global mode {report.get('global_mode') or 'unknown'} · Tab: {next_screen}",
                ),
                width,
            )
            probe_key = " · p probe" if hosts_screen else ""
            if width >= 90:
                footer_text = (
                    f"a Add {'host' if hosts_screen else 'seat'} · g global mode{probe_key} · "
                    "Tab screen · ↑↓ · Enter actions · PgUp/Dn · Esc/q"
                )
            elif width >= 48:
                footer_text = f"a Add · g mode{probe_key} · Tab · Enter · Esc/q"
            elif width >= 32:
                footer_text = "a Add · g mode · Tab · Esc/q"
            else:
                footer_text = "a Add · Tab · q quit"

            def detail_for(detail_width: int) -> list[LineParts]:
                if self.local_error:
                    return [
                        [(line, "red")]
                        for line in self._wrap_words(
                            f"Local registry unavailable: {self.local_error}", detail_width
                        )
                    ]
                if selected is None:
                    return []
                if hosts_screen:
                    return self._host_detail(selected, report, detail_width)
                return self._local_seat_detail(selected, report, detail_width)

            return self._render_master_detail(
                header=header,
                ids=ids,
                selected=selected,
                count_title=f"{'Hosts' if hosts_screen else 'Local seats'} {len(ids)}",
                column_header="  ID         STATE      STATUS     LABEL",
                row=lambda entry_id, row_width, chosen: self._local_row(
                    entry_id, row_width, selected=chosen, report=report
                ),
                detail_for=detail_for,
                empty_short=(
                    "  No hosts. Press a to Add host."
                    if hosts_screen
                    else "  No local seats. Press a to Add seat."
                ),
                empty_long=(
                    "No local hosts registered. Press a to Add host; g sets the global local mode."
                    if hosts_screen
                    else "No local seats registered. Press a to Add seat (a registered host is required)."
                ),
                footer_text=footer_text,
                width=width,
                height=height,
            )
        if self.view == "local-actions":
            actions = self._local_actions()
            return self._menu_frame(
                title="HOST ACTIONS" if hosts_screen else "LOCAL SEAT ACTIONS",
                context=self.local_selected_id,
                section="Available actions",
                rows=[label for label, _action in actions],
                selected=self.menu_index,
                page_size_attribute="_action_page_size",
                footer=(
                    f"↑↓/j/k · PgUp/PgDn · Enter · {self._esc_hint()} · q quit"
                    if width >= 48
                    else "↑↓ · Enter · Esc · q"
                ),
                width=width,
                height=height,
            )
        if self.view == "local-choice":
            return self._render_choice(width, height)
        action = self.local_form_action
        verb = "Add" if action.startswith("add-") else "Edit"
        noun = "host" if action.endswith("host") else "seat"
        form_title = f"{verb.upper()} LOCAL {noun.upper()}"
        if self.view == "local-form":
            rows = []
            for field in self.local_form_fields:
                if field.kind == "submit":
                    rows.append(f"Preview {verb} {noun}")
                    continue
                value = self.local_form_buffers.get(field.option, "")
                if not value:
                    value = "<none>" if field.kind.startswith("optional") else "<required>"
                rows.append(f"{field.label}: {value}")
            return self._menu_frame(
                title=form_title,
                context=self.local_form_target or f"new {noun}",
                section=f"{noun} details",
                rows=rows,
                selected=self.local_form_index,
                page_size_attribute="_field_page_size",
                footer=(
                    "↑↓ · Enter edit/select · Esc cancel"
                    if width >= 48
                    else "Enter · Esc cancel"
                ),
                width=width,
                height=height,
            )
        field = self.local_form_fields[self.local_form_index]
        buffer_lines = wrap_lines([self.input_buffer], max(1, width - 2)) or [""]
        lines: list[LineParts] = [
            self._header(form_title, field.label),
            self._section("Value"),
            *[
                [(("> " if index == 0 else "  ") + line, "selected")]
                for index, line in enumerate(buffer_lines)
            ],
            *[[(line, "gray")] for line in wrap_lines([field.hint], width)],
        ]
        return self._bounded(
            lines,
            "Enter · Esc cancel" if width < 48 else "Enter accept · Esc cancel · Backspace edit",
            width,
            height,
        )

    def _render_choice(self, width: int, height: int) -> str:
        purpose = self.choice_purpose
        adding = purpose == "inventory-add"
        verb = "select" if purpose.startswith("form:") or adding else "preview"
        if purpose == "inventory-add-local":
            verb = "select"
        esc = (
            "Esc back to inventory"
            if adding
            else "Esc back to seat type" if purpose == "inventory-add-local" else "Esc"
        )
        return self._menu_frame(
            title=self.choice_title,
            context=self.choice_context,
            section="Seat type" if adding else "Choices",
            rows=[label for label, _value in self.choice_items],
            selected=self.choice_index,
            page_size_attribute="_choice_page_size",
            footer=(
                f"↑↓/j/k · PgUp/PgDn · Enter {verb} · {esc}"
                if width >= 48
                else f"↑↓ · Enter {verb} · Esc"
            ),
            width=width,
            height=height,
        )

    # -- inventory rendering -------------------------------------------------
    @staticmethod
    def _inventory_id_width(rows: list[InventoryRow]) -> int:
        """ID column width: fits the longest id (local ids can be long), capped."""
        longest = max((display_width(row.entry_id) for row in rows), default=0)
        return min(INVENTORY_ID_MAX_WIDTH, max(INVENTORY_ID_MIN_WIDTH, longest))

    def _inventory_row(
        self,
        row: InventoryRow,
        width: int,
        *,
        selected: bool,
        id_width: int = INVENTORY_ID_MIN_WIDTH,
    ) -> str:
        marker = ">" if selected else " "
        kind = {"add": "+ add"}.get(row.kind, row.kind)
        entry_id = self._clip(row.entry_id, id_width)
        entry_id += " " * max(0, id_width - display_width(entry_id))
        plain = (
            f"{marker} {kind:<10} {entry_id} {row.state:<9} "
            f"{row.status:<9}  {row.label}"
        )
        if selected:
            return self._render_parts([(plain, "selected")], width, fill_style="selected")
        kind_style = {
            "codex": "cyan",
            "claude": "purple",
            "local-seat": "amber",
            "add": "green",
        }.get(row.kind)
        return self._render_parts(
            [
                (marker + " ", "gray"),
                (f"{kind:<10} ", kind_style),
                (f"{entry_id} ", None),
                (f"{row.state:<9}", self._lifecycle_style(row.state)),
                (" ", None),
                (f"{row.status:<9}", row.status_style),
                ("  ", None),
                (row.label, "green" if row.kind == "add" else None),
            ],
            width,
        )

    def _inventory_detail(
        self, row: InventoryRow | None, width: int
    ) -> list[LineParts]:
        if row is None:
            return []
        if row.kind == "codex":
            record = self._codex_records().get(row.entry_id)
            if record is None:
                return [[("Seat report unavailable", "gray")]]
            return self._selected_detail(record, width, seat_id=row.entry_id)
        if row.kind == "claude":
            return self._claude_detail(self._claude_report(), width)
        if row.kind == "global":
            return [
                [(line, None)] for line in self._wrap_words(
                    "Edit review, execution, head fallback, and Codex probe preferences. "
                    "Each change shows a settings diff before confirmation.",
                    width,
                )
            ]
        report = self._local_report()
        if row.kind == "local-seat":
            if row.entry_id in report.get("seats", {}):
                return self._local_seat_detail(row.entry_id, report, width)
            return [
                [(line, "red")]
                for line in self._wrap_words(
                    f"Local registry unavailable: {self.local_error or 'unknown error'}",
                    width,
                )
            ]
        hosts = sorted(report.get("hosts", {}))
        host_note = (
            f"registered hosts: {', '.join(hosts)}"
            if hosts
            else "no host is registered yet, so Local first offers Add host"
        )
        text = (
            "Enter (or a) chooses the seat type. Codex: a dynamically allocated "
            "seat that starts inactive; configure its plan, sign in, then "
            "Reactivate it. Claude: there is one Claude account, so this opens "
            "its existing setup (mode, tier, headroom) rather than a second login. "
            f"Local: a seat on a registered local host ({host_note}); hosts are "
            "managed on the Local Hosts screen (Tab), not listed as seats here."
        )
        return [[(line, None)] for line in self._wrap_words(text, width)]

    def _render_inventory(self, width: int, height: int) -> str:
        if self.view == "local-choice":
            return self._render_choice(width, height)
        rows = self._inventory_rows()
        by_key = {row.key: row for row in rows}
        keys = self._inventory_keys(rows)
        selected = self.inventory_selected
        listed = sum(1 for row in rows if row.kind not in {"add", "global"})
        id_width = self._inventory_id_width(rows)
        header = self._render_parts(
            self._header(
                SCREEN_TITLES["inventory"],
                (
                    "all seats"
                    + (" + retired history" if self.show_history else "")
                    + " · codex · claude · local seats · Tab: frontier seats"
                ),
            ),
            width,
        )
        if width >= 90:
            footer_text = (
                "Enter open controls · a Add seat · g settings · h history · Tab screen "
                "(hosts via Tab) · ↑↓ · PgUp/Dn detail · Esc/q"
            )
        elif width >= 50:
            footer_text = "Enter open · g global settings · a Add · Tab · Esc/q"
        elif width >= 32:
            footer_text = "Enter · g global · a Add · Esc/q"
        else:
            footer_text = "a Add · q quit · g set"
        return self._render_master_detail(
            header=header,
            ids=keys,
            selected=selected,
            count_title=f"Inventory {listed}",
            column_header=(
                f"  {'KIND':<10} {'ID':<{id_width}} {'STATE':<9} {'STATUS':<9}  LABEL"
            ),
            row=lambda key, row_width, chosen: self._inventory_row(
                by_key[key], row_width, selected=chosen, id_width=id_width
            ),
            detail_for=lambda detail_width: self._inventory_detail(
                by_key.get(selected), detail_width
            ),
            empty_short="  No seats. Press a to Add seat.",
            empty_long="No seats registered. Press a to Add seat (Codex, Claude, or Local).",
            footer_text=footer_text,
            width=width,
            height=height,
            # Five columns need a wider master pane than the single-provider lists.
            master_max=80,
            master_fraction=0.6,
        )

    # -- claude rendering ----------------------------------------------------
    def _claude_detail(
        self, report: Mapping[str, Any] | None, width: int
    ) -> list[LineParts]:
        from .display import _countdown, _usage_age_text

        if report is None:
            return [
                [(line, "red")]
                for line in self._wrap_words(
                    f"Claude account unavailable: {self.claude_error or 'unknown error'}",
                    width,
                )
            ]
        lines: list[LineParts] = []

        def section(name: str) -> None:
            lines.append(self._section(name))

        def row(label: str, value: Any, style: str | None = None) -> None:
            lines.extend(self._detail_row(label, value, width, style))

        mode = clean_text(report.get("mode") or "off")
        row("Account", f"claude · singleton subscription account · {mode}", "cyan")
        section("Status")
        status = report.get("status")
        if not isinstance(status, Mapping):
            row(
                "Provider",
                f"Unavailable — {report.get('status_error') or 'no pool snapshot'}",
                "gray",
            )
        else:
            word = clean_text(status.get("status") or "unknown")
            until = status.get("status_until")
            until_text = (
                f" · until {_countdown(until)}" if isinstance(until, (int, float)) else ""
            )
            row("Provider", f"{word}{until_text}", self._claude_status_style(word))
            row(
                "Busy",
                "yes · job in flight" if status.get("busy") else "no",
                "cyan" if status.get("busy") else "gray",
            )
            capped = status.get("capped_models")
            if isinstance(capped, list) and capped:
                row("Capped models", ", ".join(map(str, capped)), "amber")
            last_job = self._age_text(status.get("last_job_at"))
            row("Last job", last_job or "None recorded", None if last_job else "gray")
        section("Mode")
        row("Configured", report.get("configured_mode") or "off", "purple")
        reason = report.get("mode_reason")
        row(
            "Effective",
            f"{mode} · {reason}" if reason else mode,
            "gray" if mode == "off" else "cyan",
        )
        expires = report.get("expires_at")
        row("Expires", expires or "never", None if expires else "gray")
        section("Subscription tier")
        tier = report.get("subscription_tier", {})
        tier = tier if isinstance(tier, Mapping) else {}
        label = tier.get("label")
        row("Tier", label or "Unknown — not configured", "green" if label else "gray")
        source = clean_text(tier.get("source") or "unknown")
        source_text = {
            "configured": "configured · operator-declared label",
            "provider": "provider · detected label",
            "unknown": "unknown · no label configured",
        }.get(source, source)
        row("Source", source_text, None if source != "unknown" else "gray")
        if tier.get("mismatch"):
            row(
                "Mismatch",
                f"configured {tier.get('configured')} differs from detected {tier.get('detected')}",
                "amber",
            )
        detection = tier.get("detection")
        if isinstance(detection, Mapping) and not detection.get("available", False):
            row("Detection", f"Unavailable — {detection.get('reason') or 'not supported'}", "gray")
        section("Headroom floors")
        headroom = report.get("headroom", {})
        headroom = headroom if isinstance(headroom, Mapping) else {}
        row("Small-task minimum", f"{headroom.get('small_min_pct')}% remaining")
        row("Big-task minimum", f"{headroom.get('big_min_pct')}% remaining")
        section("Usage (passive cache)")
        usage = report.get("usage", {})
        usage = usage if isinstance(usage, Mapping) else {}
        if not usage.get("read"):
            row("Usage", usage.get("reason") or "Not read", "gray")
        else:
            windows = usage.get("windows", {})
            windows = windows if isinstance(windows, Mapping) else {}
            for name, window in windows.items():
                if not isinstance(window, Mapping):
                    continue
                resets = window.get("resets_at")
                reset_text = (
                    _countdown(resets) if isinstance(resets, (int, float)) else "unavailable"
                )
                row(
                    str(name).replace("_", " "),
                    f"{window.get('remaining_percentage')}% remaining · resets {reset_text}",
                    "green",
                )
            if not windows:
                row("Windows", "Unavailable — no cached window", "gray")
            age = usage.get("age_seconds")
            fresh = usage.get("fresh")
            age_text = (
                f"Updated {_usage_age_text(age)} ago"
                if isinstance(age, (int, float))
                else "Age unknown"
            )
            row(
                "Freshness",
                f"{'fresh' if fresh else 'stale'} · {age_text}",
                "cyan" if fresh else "amber",
            )
        warnings = report.get("warnings")
        if isinstance(warnings, list) and warnings:
            section("Warnings")
            for warning in warnings:
                row("Warning", warning, "amber")
        return lines

    def _render_claude(self, width: int, height: int) -> str:
        title = SCREEN_TITLES["claude"]
        if self.view == "list":
            report = self._claude_report()
            detail = self._claude_detail(report, width)
            header = self._render_parts(
                self._header(title, f"{CLAUDE_TARGET} · Tab: {SCREEN_NAMES['inventory']}"),
                width,
            )
            if width >= 90:
                footer_text = (
                    "Enter edit mode / tier / headroom · Tab screen · ↑↓ PgUp/Dn scroll · Esc/q"
                )
            elif width >= 48:
                footer_text = "Enter edit · Tab · ↑↓ scroll · Esc/q"
            else:
                footer_text = "Enter · Tab · q quit"
            slots = max(1, height - 3)
            detail_max = max(0, len(detail) - slots)
            self.detail_scroll = min(max(0, self.detail_scroll), detail_max)
            self._detail_page_size = slots
            first = self.detail_scroll + 1 if detail else 0
            last = min(len(detail), self.detail_scroll + slots)
            lines = [
                header,
                self._render_parts(
                    self._section(f"Account · {first}-{last}/{len(detail)}"), width
                ),
            ]
            lines.extend(
                self._render_parts(parts, width)
                for parts in detail[self.detail_scroll : self.detail_scroll + slots]
            )
            lines.append(self._footer(footer_text, width))
            return "\n".join(lines[:height])
        if self.view == "claude-actions":
            actions = self._claude_actions()
            return self._menu_frame(
                title=f"{title} ACTIONS",
                context=CLAUDE_TARGET,
                section="Available actions",
                rows=[label for label, _action in actions],
                selected=self.menu_index,
                page_size_attribute="_action_page_size",
                footer=(
                    f"↑↓/j/k · PgUp/PgDn · Enter · {self._esc_hint()} · q quit"
                    if width >= 48
                    else "↑↓ · Enter · Esc · q"
                ),
                width=width,
                height=height,
            )
        if self.view == "local-choice":
            return self._render_choice(width, height)
        option, label = CLAUDE_HEADROOM_FIELDS[self.claude_input_field]
        lines: list[LineParts] = [
            self._header(title, label),
            self._section("Value"),
            [("> " + self.input_buffer, "selected")],
            *[
                [(line, "gray")]
                for line in wrap_lines(
                    [
                        f"{option}: percent of the subscription window that must remain "
                        "before such tasks route to Claude (0-100; small must not exceed big). "
                        "Validation and a before/after preview follow Enter."
                    ],
                    width,
                )
            ],
        ]
        return self._bounded(
            lines,
            "Enter · Esc cancel" if width < 48 else "Enter preview · Esc cancel · Backspace edit",
            width,
            height,
        )

    @staticmethod
    def _window_start(length: int, selected: int, size: int) -> int:
        if length <= 0 or size <= 0:
            return 0
        selected = min(max(0, selected), length - 1)
        return min(max(0, selected - size // 2), max(0, length - size))

    def _bounded(
        self, lines: list[LineParts], footer: str, width: int, height: int
    ) -> str:
        available = max(0, height - 1)
        visible = lines[:available]
        if len(lines) > available and available:
            visible[-1] = [(f"… {len(lines) - available + 1} more line(s)", "gray")]
        rendered = [
            self._render_parts(
                line,
                width,
                fill_style=(
                    "selected" if len(line) == 1 and line[0][1] == "selected" else None
                ),
            )
            for line in visible
        ]
        rendered.append(self._footer(footer, width))
        return "\n".join(rendered[:height])
