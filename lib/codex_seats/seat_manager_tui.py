"""Terminal seat manager: an ops view with light management for every seat.

The manager is for visibility, diagnosis and light management of seats and
usage; it is not an approval layer. ``render(width, height, overview)`` is
pure: it builds one bounded frame from read-only reports. The summary strip and
the seat table come from :func:`pool_view.collect` (the same model agents read
through ``codex-seat capacity``); seat detail comes from the registry, Claude
and local managers. Provider work runs on the refresher or one background
worker, never on the key loop.

Reversible changes (pause/resume, refresh, role, plan and headroom, Claude,
local and global settings) apply on the keypress or form submit: the managers'
digest-pinned ``preview_*`` and ``commit`` calls run back to back and a one-line
result replaces the footer until the next key. Only irreversible actions —
redeeming a banked reset and removing a seat — open a bordered confirmation
dialog with a plain summary, a "what changes" list and the raw settings diff
behind ``d``. The dialog keeps the preview's digest pin: when settings change in
the meantime nothing is written and the dialog offers a fresh preview.

Keys come from one declarative keymap per screen (:data:`KEYMAP`); the footer,
the ``?`` help and the documented keymap table are all generated from it. Esc
walks a real back stack, so help, forms and menus return to where they were
opened from.
"""

from __future__ import annotations

import difflib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from . import pool_view
from . import seat_manager as _seat_manager
from .account import prepare_banked_reset_redemption, redeem_banked_reset
from .claude_manager import CLAUDE_SUBSCRIPTION_TIERS
from .policy_settings import CHOICES, DEFAULTS, PolicyPreview
from .seat_manager import (
    ActionPreview,
    LoginPlan,
    ManagerError,
    SeatManager,
    StalePreviewError,
    _thaw,
)
from .seat_registry import (
    CAPACITY_PLAN_PRESETS,
    SeatRegistryError,
    validate_lifecycle_transition,
)
from .terminal import KEY_BACKTAB, clean_text, display_width, wrap_lines
from .tui_style import (
    ANSI,
    age,
    ago,
    clip,
    countdown,
    day,
    human,
    status_chip,
    until,
    usage_bar,
    window_label,
)

ANSI_RE = re.compile(r"\033\[[0-9;]*m")
Parts = list[tuple[Any, "str | None"]]
KEY_TAB = "\t"
MARKER = "▌"
# Friendly names for row actions in result lines.
_ACTION_NAMES = {
    "redeem": "Redeem reset", "refresh": "Refresh", "edit": "Edit", "role": "Make primary",
    "pause": "Pause", "login": "Sign in", "local-mode": "Local mode",
}
_CODEX_ONLY = frozenset({"redeem", "role", "login"})
# Decoded names of non-text keys; never typed into a text field.
_KEY_NAMES = frozenset({"up", "down", "left", "right", "pageup", "pagedown"})


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


# Plan and headroom form for one Codex seat. Roles have their own key (``o``)
# and lifecycle has ``p``, so neither is a free-text field here.
FIELDS = (
    Field("Label", "label", "text"),
    Field("Reserve %", "reserve_pct", "number"),
    Field("Small-task floor %", "small_task_min_headroom_pct", "number"),
    Field("Big-task floor %", "big_task_min_headroom_pct", "number"),
    Field("Plan & capacity", "capacity", "capacity"),
)

CAPACITY_PARTS = (
    ("profile", "profile"),
    ("relative capacity (0.1..1000 or null)", "relative_capacity"),
    ("source", "capacity_source"),
)
CAPACITY_CHOICES = (*CAPACITY_PLAN_PRESETS, {"label": "Advanced / custom values"})
ADD_FIELDS = (
    Field("Label", "label", "text"),
    Field("Home (blank = default)", "home", "text"),
    Field("Plan", "profile", "preset"),
    Field("Add seat", "submit", "submit"),
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
SCREENS = ("inventory", "seats", "hosts", "local-seats", "claude")
SCREEN_TITLES = {
    "inventory": "Inventory",
    "seats": "Codex seats",
    "hosts": "Local hosts",
    "local-seats": "Local seats",
    "claude": "Claude",
}
LOCAL_SCREENS = frozenset({"hosts", "local-seats"})
LOCAL_MODE_ORDER = ("off", "shadow", "canary", "opportunistic")
LOCAL_LIFECYCLE_ORDER = ("active", "draining", "inactive", "retired")
LOCAL_INPUT_LIMIT = 4096
# Views that type text: printable keys (including q, j, k, ?) are input there.
TEXT_VIEWS = frozenset(
    {"input", "add-input", "capacity-input", "local-form-input", "claude-input", "global-input"}
)
# Views drawn as a dialog over the view they were opened from.
DIALOG_VIEWS = frozenset({"confirm", "busy", "quit-confirm"})

# -- claude singleton account -------------------------------------------------
CLAUDE_TARGET = "providers.claude"
CLAUDE_MODE_ORDER = ("off", "manual_only", "review_only", "opportunistic")
CLAUDE_INPUT_LIMIT = 16
CLAUDE_HEADROOM_FIELDS = {
    "small": ("small_task_min_headroom_pct", "Small-task floor"),
    "big": ("big_task_min_headroom_pct", "Big-task floor"),
}
CLAUDE_TIER_CLEAR_LABEL = "Unknown (clear the label)"
# The Claude screen lists its editable settings as selectable rows.
CLAUDE_SETTINGS = (
    ("claude-mode", "Mode"),
    ("claude-tier", "Subscription tier"),
    ("claude-small", "Small-task floor"),
    ("claude-big", "Big-task floor"),
    ("claude-active-refresh", "Active /usage refresh"),
)

# -- global settings ------------------------------------------------------------
GLOBAL_LABELS = {
    "review.mode": "Goal review mode",
    "review.strategy": "Review strategy",
    "execution.delegation_policy": "Delegation",
    "execution.inline_max_minutes": "Inline minutes",
    "execution.head_permissions": "Head mode",
    "execution.head_worker_fallback": "Head fallback",
}
GLOBAL_HELP = {
    "review.mode": "Automatic goal supervision only. Explicit `codex-seat review` "
    "requests are unaffected; off by default.",
    "review.strategy": "Default reviewer strategy for goal review and for "
    "`codex-seat review`.",
    "execution.delegation_policy": "When heads hand work to seats.",
    "execution.inline_max_minutes": "Longest task a head runs inline before "
    "delegating (1–240 minutes).",
    "execution.head_permissions": "Permissions a head starts with.",
    "execution.head_worker_fallback": "Let a head fall back to a worker seat when "
    "it cannot run the work itself.",
}

# -- inventory ----------------------------------------------------------------
# Rows are Codex seats, the Claude account and local seats under group
# headers, then one Add seat row. Local hosts are placement, not seats: they
# live on the Local hosts screen.
INVENTORY_ADD_KEY = "add:seat"
ADD_TYPE_CODEX = "add:codex"
ADD_TYPE_CLAUDE = "add:claude"
ADD_TYPE_LOCAL = "add:local"
ADD_TYPE_CHOICES = (
    ("Codex seat (starts paused; sign in, then p resumes it)", ADD_TYPE_CODEX),
    ("Claude account (one account only; opens its settings)", ADD_TYPE_CLAUDE),
    ("Local seat on a registered local host", ADD_TYPE_LOCAL),
)
ADD_LOCAL_NO_HOST_CHOICES = (
    ("Add a local host first (required before any local seat)", "add:host"),
    ("Open Local hosts", "hosts"),
)


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
    LocalField("Save host", "submit", "submit"),
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
    LocalField("Save seat", "submit", "submit"),
)
# Edit forms change non-secret configuration only; lifecycle and mode use their
# dedicated actions so transition rules stay visible.
EDIT_EXCLUDED_OPTIONS = frozenset({"id", "lifecycle", "mode"})


# -- keymap -------------------------------------------------------------------


@dataclass(frozen=True)
class Key:
    """One key binding: how it is written, what it does, where it shows.

    ``footer`` is the footer priority (1 is always shown; 0 means help only);
    bindings sharing a ``group`` collapse into one footer entry such as
    ``c r e o p l seat actions``.
    """

    cap: str
    label: str
    help: str
    action: str = ""
    keys: tuple[str, ...] = ()
    footer: int = 0
    group: str = ""
    footer_cap: str = ""

    @property
    def names(self) -> tuple[str, ...]:
        return self.keys or (self.cap,)

    @property
    def short(self) -> str:
        return self.footer_cap or self.cap


_SELECT = Key("↑↓ j k", "select", "Select a row", keys=("up", "down", "j", "k"), footer=2,
              footer_cap="↑↓")
_SCROLL = Key("PgUp PgDn", "scroll", "Scroll the detail", keys=("pageup", "pagedown"))
_OPEN = Key("Enter", "open", "Open the row's actions", "open", keys=("enter",), footer=2)
_HISTORY = Key("h", "history", "Show or hide retired seats", "history")
_RAW = Key("d", "diagnostics", "Show raw capacity diagnostic", "raw")


def _add_key(text: str) -> Key:
    return Key("a", "add", text, "add", footer=3)


SEAT_KEYS = (
    Key("c", "Redeem reset", "Redeem a banked reset", "redeem", footer=5, group="seat actions"),
    Key("r", "Refresh", "Refresh usage for this seat", "refresh", footer=5, group="seat actions"),
    Key("e", "Edit", "Edit plan and headroom", "edit", footer=5, group="seat actions"),
    Key("o", "Role", "Make primary (used last)", "role", footer=5, group="seat actions"),
    Key("p", "Pause", "Pause or resume", "pause", footer=5, group="seat actions"),
    Key("l", "Sign in", "Sign in again", "login", footer=5, group="seat actions"),
)
LOCAL_SEAT_KEYS = (
    Key("r", "Probe", "Probe this seat's host now", "refresh", footer=5, group="seat actions"),
    Key("e", "Edit", "Edit the seat's settings", "edit", footer=5, group="seat actions"),
    Key("p", "Pause", "Pause or resume the seat", "pause", footer=5, group="seat actions"),
)
HOST_KEYS = (
    Key("r", "Probe", "Probe the host now (on the refresher)", "refresh", footer=5, group="host actions"),
    Key("e", "Edit", "Edit the host's settings", "edit", footer=5, group="host actions"),
    Key("p", "Pause", "Pause or resume the host", "pause", footer=5, group="host actions"),
    Key("L", "Local mode", "Set the global local mode", "local-mode", footer=5, group="host actions"),
)
_EDIT_ROW = Key("Enter", "edit", "Change the selected setting", "open", keys=("enter",), footer=2)

KEYMAP: dict[str, tuple[Key, ...]] = {
    "overview": (
        Key("m", "manage", "Open the seat manager", "manage", footer=1),
        Key("R", "refresh all", "Refresh every seat", "refresh-all", footer=2),
        Key("g", "settings", "Global settings", "global", footer=2),
        Key("?", "help", "Show help", "help", footer=1),
        Key("q", "quit", "Quit", "quit", footer=1),
    ),
    "inventory": (
        _SELECT, _OPEN, *SEAT_KEYS, _add_key("Add a seat of any kind"), _HISTORY, _RAW, _SCROLL,
    ),
    "seats": (_SELECT, _OPEN, *SEAT_KEYS, _add_key("Add a Codex seat"), _HISTORY, _RAW, _SCROLL),
    "hosts": (
        _SELECT, _OPEN, *HOST_KEYS, _add_key("Add a local host"),
        Key("h", "history", "Show or hide retired hosts", "history"), _SCROLL,
    ),
    "local-seats": (_SELECT, _OPEN, *LOCAL_SEAT_KEYS, _add_key("Add a local seat"), _HISTORY, _SCROLL),
    "claude": (_SELECT, _EDIT_ROW, _SCROLL),
    "global": (_SELECT, _EDIT_ROW),
    "menu": (
        Key("↑↓ j k", "select", "Select an action", keys=("up", "down", "j", "k"), footer=2,
            footer_cap="↑↓"),
        Key("Enter", "choose", "Run the selected action", "open", keys=("enter",), footer=2),
    ),
    "form": (
        Key("↑↓ j k", "field", "Select a field", keys=("up", "down", "j", "k"), footer=2,
            footer_cap="↑↓"),
        Key("Enter", "edit", "Edit the field, or save on the last row", "open", keys=("enter",), footer=2),
    ),
    "input": (
        Key("Enter", "save", "Keep the typed value", "open", keys=("enter",), footer=1),
        Key("Backspace", "delete", "Delete the last character", keys=("backspace",), footer=2),
        Key("Esc", "cancel", "Discard the typed value", "back", keys=("esc",), footer=1),
    ),
}
GLOBAL_KEYS = (
    Key("Esc", "back", "Go back one step", "back", keys=("esc",), footer=1),
    Key("Tab", "screens", "Next screen", "next-screen", keys=(KEY_TAB,), footer=4),
    Key("Shift-Tab", "screens", "Previous screen", "prev-screen", keys=(KEY_BACKTAB,)),
    Key("/", "filter", "Filter rows by seat or label", "filter", footer=4),
    Key("g", "settings", "Global settings", "global", footer=6),
    Key("R", "refresh all", "Refresh every seat", "refresh-all"),
    Key("?", "help", "Help for this screen", "help", footer=1),
    Key("q", "quit", "Quit", "quit"),
)
# Footer order for the global keys: filter, screens, settings, help, back.
_GLOBAL_FOOTER = ("/", "Tab", "g", "?", "Esc")
# Keys that work on the pushed settings/menu/form views as well as the lists.
_ALWAYS = frozenset({"Esc", "?", "q", "R"})
HELP_TITLES = {
    **SCREEN_TITLES,
    "overview": "Overview",
    "global": "Global settings",
    "menu": "Actions",
    "form": "Form",
    "input": "Text entry",
}


def footer_parts(keys: tuple[Key, ...], width: int, *, esc: str = "back") -> Parts:
    """Fit a generated ``key label`` footer to ``width`` by priority."""
    entries: list[tuple[int, str, str]] = []
    groups: dict[str, list[Key]] = {}
    for key in keys:
        if not key.footer:
            continue
        if key.group:
            if key.group not in groups:
                groups[key.group] = []
                entries.append((key.footer, "", key.group))
            groups[key.group].append(key)
            continue
        entries.append((key.footer, key.short, esc if key.cap == "Esc" and key.label == "back" else key.label))
    rendered = [
        (priority, " ".join(k.short for k in groups[label]) if not cap else cap, label)
        for priority, cap, label in entries
    ]
    kept: set[int] = set()
    used = 0
    for index in sorted(range(len(rendered)), key=lambda i: (rendered[i][0], i)):
        _priority, cap, label = rendered[index]
        cost = display_width(f"{cap} {label}") + (2 if kept else 0)
        if used + cost <= width:
            kept.add(index)
            used += cost
    parts: Parts = []
    for index, (_priority, cap, label) in enumerate(rendered):
        if index not in kept:
            continue
        if parts:
            parts.append(("  ", None))
        parts.extend([(cap, "accent"), (" " + label, "muted")])
    return parts


def keymap_markdown() -> str:
    """The documented keymap table, generated from :data:`KEYMAP`."""
    rows = ["| Where | Key | What it does |", "| --- | --- | --- |"]

    def add(where: str, keys: tuple[Key, ...]) -> None:
        for key in keys:
            rows.append(f"| {where} | `{key.cap}` | {key.help} |")

    add("Everywhere", GLOBAL_KEYS)
    add("Overview", KEYMAP["overview"])
    for screen in SCREENS:
        add(SCREEN_TITLES[screen], KEYMAP[screen])
    add("Global settings", KEYMAP["global"])
    return "\n".join(rows)


@dataclass
class Line:
    """One frame row: ``(text, role)`` parts, optionally a filled background."""

    parts: Parts
    bg: str | None = None


@dataclass
class Row:
    """One table row. ``kind`` is codex, claude, local, host, setting, add or group."""

    key: str
    kind: str
    entry: dict[str, Any] = field(default_factory=dict)
    title: str = ""
    note: str = ""

    @property
    def selectable(self) -> bool:
        return self.kind != "group"


@dataclass(frozen=True)
class Action:
    """One row action with its key, label and why it cannot run (if so)."""

    key: str
    label: str
    action: str
    reason: str | None = None
    hint: str = ""
    short: str = ""


class ManagerUI:
    """State machine and pure renderer for the overview and manager screens."""

    def __init__(
        self, manager: SeatManager | None, *, direct: bool = False, color: bool = False
    ) -> None:
        self.manager = manager
        self.color = bool(color)
        self.view = "list" if direct else "overview"
        self.screen = "inventory"
        # Back stack of (screen, view) pairs: Esc returns to where a view was
        # opened from. Tab clears it; an empty stack on a list means overview.
        self._stack: list[tuple[str, str, Any]] = []
        self.selected_id: str | None = None
        self.inventory_selected: str | None = None
        self.local_selected: dict[str, str | None] = {"hosts": None, "local-seats": None}
        # Screens that select by id remember when the Add row is selected.
        self._on_add: dict[str, bool] = {}
        self.claude_index = 0
        self.menu_index = 0
        self.field_index = 0
        self.scroll = 0
        self.detail_scroll = 0
        self.help_scroll = 0
        self._detail_page_size = 1
        self._page_size = 1
        self._too_small = False
        self._quit_return_view = "list"
        # One-line result shown instead of the footer until the next key.
        self.message = ""
        self.message_kind = "info"
        self.busy_text = ""
        self.filter = ""
        self.filtering = False
        self.show_history = False
        self.preview: ActionPreview | None = None
        self.login_plan: LoginPlan | None = None
        self.confirm_kind: str | None = None
        self.confirm_return_view = "list"
        self.confirm_raw = False
        self.confirm_stale = False
        self._confirm_refresh: Callable[[], None] | None = None
        self.input_buffer = ""
        self.capacity_buffers: dict[str, str] = {}
        self.capacity_index = 0
        self.capacity_preset_index = 0
        self.add_field_index = 0
        self.add_preset_index = 0
        self.add_buffers: dict[str, Any] = {"label": "New seat", "home": "", "profile": None}
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
        self._drain_signature: tuple[tuple[str, str], ...] = ()
        self.pool_error: str | None = None
        # The pool snapshot behind the cached view; seat detail derives its
        # plain-words "why" from the structured capacity report over it.
        self._snapshot: dict[str, Any] | None = None
        # ``d`` shows the raw capacity diagnostic in the detail pane.
        self.show_raw = False
        self._pool_cache: dict[str, Any] | None = None
        self._pool_at = 0.0
        self._records_cache: dict[str, Mapping[str, Any]] | None = None
        self._records_at = 0.0
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
        self.choice_title = ""
        self.choice_context: Any = ""
        self.choice_purpose = ""
        self.choice_items: list[tuple[str, Any]] = []
        self.choice_index = 0
        self.choice_target: str | None = None
        self.claude_error: str | None = None
        self._claude_report_cache: dict[str, Any] | None = None
        self._claude_report_at = 0.0
        self.claude_input_field = "small"
        self.global_preview: PolicyPreview | None = None
        self.global_values: dict[str, Any] = {}
        self.global_index = 0
        self.global_choice_index = 0

    # -- screens and the back stack -------------------------------------------
    @property
    def local_screen(self) -> bool:
        return self.screen in LOCAL_SCREENS

    @property
    def inventory_screen(self) -> bool:
        return self.screen == "inventory"

    @property
    def claude_screen(self) -> bool:
        return self.screen == "claude"

    @property
    def local_selected_id(self) -> str | None:
        return self.local_selected.get(self.screen)

    def _push(self, view: str, screen: str | None = None) -> None:
        """Open ``view`` (on ``screen``); Esc returns to the current place."""
        saved = self._choice_state() if self.view == "local-choice" else None
        self._stack.append((self.screen, self.view, saved))
        if screen is not None and screen != self.screen:
            self.screen = screen
            self.detail_scroll = 0
        self.view = view

    def _back(self) -> None:
        """Esc: the previous place, else the overview from a top-level list."""
        if self._stack:
            screen, view, saved = self._stack.pop()
            if screen != self.screen:
                self.detail_scroll = 0
            self.screen, self.view = screen, view
            if saved is not None:
                (self.choice_title, self.choice_context, self.choice_purpose,
                 self.choice_items, self.choice_index, self.choice_target) = saved
        elif self.view == "list":
            self.view = "overview"
        else:
            self.view = "list"
        self.help_scroll = 0

    def _choice_state(self) -> tuple[Any, ...]:
        return (self.choice_title, self.choice_context, self.choice_purpose,
                list(self.choice_items), self.choice_index, self.choice_target)

    def _pop_to_list(self) -> None:
        """Return to the list a chain of menus, choices and forms came from."""
        while self.view != "list" and self._stack:
            self._back()
        self.view = "list"

    def _switch_screen(self, delta: int) -> None:
        index = SCREENS.index(self.screen)
        self._show_screen(SCREENS[(index + delta) % len(SCREENS)])

    def _show_screen(self, screen: str) -> None:
        """Land on a top-level list with fresh reports and an empty back stack."""
        self._stack.clear()
        self.screen = screen
        self.view = "list"
        self.detail_scroll = 0
        self.menu_index = 0
        self.filter = ""
        self.filtering = False
        self.local_preview = None
        self._invalidate()
        self._selected_row()

    def _open_inventory(self) -> None:
        """``m`` from the overview always lands on the inventory."""
        self._show_screen("inventory")

    def _invalidate(self) -> None:
        self._pool_cache = None
        self._records_cache = None
        self._local_report_cache = None
        self._claude_report_cache = None

    def _toast(self, text: str, kind: str = "ok") -> None:
        self.message = clean_text(text)
        self.message_kind = kind

    def _error(self, text: Any) -> None:
        self._toast(f"× {text}", "error")

    # -- read models ------------------------------------------------------------
    def _pool(self, *, force: bool = False) -> dict[str, Any]:
        """The shared capacity model (``pool_view.collect``), cached briefly.

        A manager without a readable pool (no ledger yet, a test fake) falls
        back to registry-only entries so every seat stays manageable.
        """
        now = time.monotonic()
        if not force and self._pool_cache is not None and now - self._pool_at < 1.0:
            return self._pool_cache
        if self.manager is None:
            view: dict[str, Any] = {"pool": self._empty_pool(), "entries": [], "jobs": []}
        else:
            from .state import PoolSnapshotError

            try:
                snapshot = pool_view.read_snapshot(self.manager.root, self.manager.settings)
                view = pool_view.collect(
                    self.manager.root, self.manager.settings, snapshot=snapshot
                )
                self._snapshot = snapshot
                self.pool_error = None
            except (
                PoolSnapshotError, ManagerError, OSError, ValueError, KeyError,
                TypeError, AttributeError,
            ) as exc:
                self.pool_error = clean_text(exc) or type(exc).__name__
                self._snapshot = None
                view = self._fallback_pool()
        self._pool_cache = view
        self._pool_at = now
        return view

    def _capacity_report(self, seat_id: str) -> dict[str, Any] | None:
        """The structured capacity verdict routing uses, over the cached snapshot."""
        snapshot = self._snapshot
        if snapshot is None or self.manager is None:
            return None
        from .capacity import seat_capacity_report

        seat = snapshot.get("seats", {}).get(seat_id)
        learning = snapshot.get("capacity_learning")
        try:
            return seat_capacity_report(
                seat_id, dict(seat) if isinstance(seat, Mapping) else {},
                self.manager.settings, current=time.time(),
                learning=learning if isinstance(learning, dict) else {},
            )
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _why(entry: Mapping[str, Any], report: Mapping[str, Any] | None) -> str | None:
        """One plain sentence for why a seat takes no (or only small) work."""
        lifecycle = entry.get("lifecycle")
        if lifecycle != "active":
            return {
                "inactive": "Paused · p resumes it",
                "draining": "Draining · it pauses when its task ends",
                "retired": "Retired · kept for history",
            }.get(str(lifecycle))
        if entry.get("busy"):
            return "Busy with a task · it takes new work when that ends"
        if report is None:
            return None
        status = str(report.get("provider_status") or "unknown")
        if report.get("auth_ok") is False or status in {"needs-login", "needs-auth"}:
            return "Signed out · l signs in again"
        if status not in {"ok", "capped", "unknown", "cooldown"}:
            return f"The provider reports {status}"
        small = report.get("small") if isinstance(report.get("small"), Mapping) else {}
        big = report.get("big") if isinstance(report.get("big"), Mapping) else {}
        if small.get("fits") and big.get("fits"):
            return None
        size, verdict = ("small", small) if not small.get("fits") else ("big", big)
        prefix = "Fits small tasks only · " if size == "big" else ""
        observed = entry.get("observed_at")
        old = (
            f"Usage data is {age(time.time() - observed)} old"
            if isinstance(observed, (int, float)) and not isinstance(observed, bool) else ""
        )
        tier = verdict.get("tier") or report.get("tier")
        if tier == "unknown":
            return prefix + (f"{old} · r refreshes it" if old else "No usage reported yet · r refreshes it")
        for requirement in verdict.get("requirements") or []:
            have = requirement.get("headroom_pct")
            need = (requirement.get("required_pct") or 0) + (requirement.get("margin_pct") or 0)
            if isinstance(have, (int, float)) and isinstance(need, (int, float)) and have < need:
                window = window_label(requirement.get("window"))
                return prefix + f"{window} window has {have:.0f}% left; {size} tasks need {need:.0f}%"
        usable, floor = verdict.get("usable_headroom_pct"), verdict.get("floor_pct")
        if verdict.get("floor_fits") is False and isinstance(usable, (int, float)) and isinstance(
                floor, (int, float)):
            return prefix + f"Only {usable:.0f}% usable; {size} tasks need {floor:.0f}%"
        if tier == "degraded":
            return prefix + (f"{old} · r refreshes it" if old else "Usage data is stale · r refreshes it")
        return prefix + f"Not enough headroom for {size} tasks"

    @staticmethod
    def _empty_pool() -> dict[str, Any]:
        return {
            "ready": 0, "total": 0, "running": 0, "queued": 0, "waiting_capacity": 0,
            "next": {"small": None, "big": None}, "banked_resets": None, "warnings": [],
        }

    def _fallback_pool(self) -> dict[str, Any]:
        """Registry-only entries when the pool snapshot cannot be read."""
        entries: list[dict[str, Any]] = []
        records = self._codex_records()
        for seat_id in self._codex_ids(include_retired=False):
            entries.append(self._codex_skeleton(seat_id, records.get(seat_id, {})))
        claude = self._claude_skeleton()
        if claude is not None:
            entries.append(claude)
        entries.extend(self._local_skeletons())
        pool = self._empty_pool()
        enabled = [e for e in entries if e["lifecycle"] == "active" and e.get("mode") not in {"off", "shadow"}]
        pool["total"] = len(enabled)
        pool["running"] = sum(1 for e in entries if e["busy"])
        return {"pool": pool, "entries": entries, "jobs": []}

    def _codex_ids(self, *, include_retired: bool) -> list[str]:
        if self.manager is None:
            return []
        from .seat_registry import sort_seat_ids

        seats = self.manager.settings.get("pool", {}).get("seats", {})
        if not include_retired:
            seats = {
                seat_id: entry for seat_id, entry in seats.items()
                if entry.get("lifecycle") != "retired"
            }
        return sort_seat_ids(seats)

    def _codex_records(self) -> dict[str, Mapping[str, Any]]:
        """Registry list report keyed by stable seat id (empty when unreadable)."""
        if self.manager is None:
            return {}
        now = time.monotonic()
        if self._records_cache is not None and now - self._records_at < 1.0:
            return self._records_cache
        try:
            report = self.manager.list_report(include_all=True)
        except ManagerError:
            report = {}
        self._records_cache = {
            seat["id"]: seat
            for seat in report.get("seats", [])
            if isinstance(seat, Mapping) and isinstance(seat.get("id"), str)
        }
        self._records_at = now
        return self._records_cache

    def _codex_skeleton(self, seat_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
        assert self.manager is not None
        config = self.manager.settings.get("pool", {}).get("seats", {}).get(seat_id, {})
        claim = record.get("claim") if isinstance(record.get("claim"), Mapping) else {}
        live = bool(claim.get("live"))
        capacity = record.get("capacity") if isinstance(record.get("capacity"), Mapping) else {}
        windows = []
        for window in capacity.get("active_windows") or []:
            if not isinstance(window, Mapping) or not isinstance(window.get("remaining_pct"), (int, float)):
                continue
            name = {"five_hour": "5h", "weekly": "7d", "seven_day": "7d"}.get(
                str(window.get("name")), str(window.get("name") or "")
            )
            windows.append({
                "name": name, "minutes": window.get("window_minutes"),
                "remaining_pct": float(window["remaining_pct"]),
                "resets_at": window.get("resets_at"), "fresh": True,
            })
        left = {"5h": None, "7d": None}
        for window in windows:
            if window["name"] in left:
                left[window["name"]] = window["remaining_pct"]
        provider = record.get("provider") if isinstance(record.get("provider"), Mapping) else {}
        bank = record.get("banked_resets") if isinstance(record.get("banked_resets"), Mapping) else {}
        return {
            "provider": "codex",
            "seat": seat_id,
            "label": str(config.get("label") or seat_id),
            "plan": pool_view.codex_plan_name(self.manager.settings, seat_id),
            "role": record.get("role") or (
                "primary" if self.manager.settings.get("pool", {}).get("primary") == seat_id else None
            ),
            "lifecycle": str(config.get("lifecycle") or "unknown"),
            "mode": None,
            "status": str(provider.get("health") or "unknown"),
            "busy": live,
            "task": {"label": str(claim.get("task_kind") or claim.get("job_id") or "live job"),
                     "kind": claim.get("task_kind"), "job_id": claim.get("job_id")} if live else None,
            "windows": windows,
            "left": left,
            "fits": [],
            "ready": False,
            "reason": None,
            "fresh": bool(windows),
            "banked_resets": bank.get("available_count") if bank.get("status") == "fresh" else None,
            "observed_at": provider.get("observed_at"),
        }

    def _claude_skeleton(self) -> dict[str, Any] | None:
        report = self._claude_report()
        if report is None:
            if self.claude_error is None:
                return None
            return {
                "provider": "claude", "seat": "claude", "label": "Claude", "plan": "",
                "role": None, "lifecycle": "unknown", "mode": None, "status": "error",
                "busy": False, "task": None, "windows": [], "left": {"5h": None, "7d": None},
                "fits": [], "ready": False, "reason": self.claude_error, "fresh": False,
                "banked_resets": None, "observed_at": None,
            }
        mode = str(report.get("mode") or "off")
        status = report.get("status") if isinstance(report.get("status"), Mapping) else {}
        usage = report.get("usage") if isinstance(report.get("usage"), Mapping) else {}
        windows = []
        for raw, name in (("five_hour", "5h"), ("seven_day", "7d")):
            window = (usage.get("windows") or {}).get(raw) if isinstance(usage.get("windows"), Mapping) else None
            pct = window.get("remaining_percentage") if isinstance(window, Mapping) else None
            if isinstance(pct, (int, float)) and not isinstance(pct, bool):
                windows.append({"name": name, "minutes": 300.0 if name == "5h" else 10080.0,
                                "remaining_pct": float(pct), "resets_at": window.get("resets_at"),
                                "fresh": bool(usage.get("fresh"))})
        left = {"5h": None, "7d": None}
        for window in windows:
            left[window["name"]] = window["remaining_pct"]
        tier = report.get("subscription_tier") if isinstance(report.get("subscription_tier"), Mapping) else {}
        return {
            "provider": "claude", "seat": "claude", "label": "Claude",
            "plan": tier.get("label") or "tier ?", "role": None,
            "lifecycle": "off" if mode == "off" else "active", "mode": mode,
            "status": str(status.get("status") or "unknown"), "busy": bool(status.get("busy")),
            "task": {"label": "Claude job", "kind": None, "job_id": None} if status.get("busy") else None,
            "windows": windows, "left": left, "fits": [], "ready": False,
            "reason": report.get("mode_reason"), "fresh": bool(usage.get("fresh")),
            "banked_resets": None, "observed_at": None,
        }

    def _local_skeletons(self) -> list[dict[str, Any]]:
        report = self._local_report()
        entries = []
        for seat_id in sorted(report.get("seats", {})):
            seat = report["seats"][seat_id]
            config = seat.get("config", {}) if isinstance(seat, Mapping) else {}
            status = seat.get("status") if isinstance(seat, Mapping) else None
            status = status if isinstance(status, Mapping) else {}
            key = str(status.get("key") or "unknown")
            entries.append({
                "provider": "local", "seat": seat_id, "label": str(config.get("label") or seat_id),
                "plan": str(config.get("model_profile") or "?"), "role": None,
                "lifecycle": str(config.get("lifecycle") or "unknown"),
                "mode": str(seat.get("effective_mode") or "off"), "status": key,
                "busy": key == "working",
                "task": {"label": str(status.get("detail") or "managed job"), "kind": None, "job_id": None}
                if key == "working" else None,
                "windows": [], "left": {"5h": None, "7d": None}, "fits": [],
                "ready": key in {"ready", "working"}, "reason": status.get("detail") or None,
                "fresh": key != "stale" and status.get("observed_at") is not None,
                "banked_resets": None, "observed_at": status.get("observed_at"),
            })
        return entries

    def _entries(self) -> list[dict[str, Any]]:
        """Pool entries plus retired Codex and local seats while ``h`` is on."""
        view = self._pool()
        entries = list(view["entries"])
        if self.show_history and self.manager is not None:
            known = {e["seat"] for e in entries if e["provider"] == "codex"}
            records = self._codex_records()
            for seat_id in self._codex_ids(include_retired=True):
                if seat_id not in known:
                    entries.append(self._codex_skeleton(seat_id, records.get(seat_id, {})))
        if not self.show_history:
            entries = [e for e in entries if e["lifecycle"] != "retired"]
        order = {"codex": 0, "claude": 1, "local": 2}
        codex_order = {seat_id: index for index, seat_id in enumerate(self._codex_ids(include_retired=True))}
        entries.sort(key=lambda e: (order.get(e["provider"], 3), codex_order.get(e["seat"], 0), e["seat"]))
        return entries

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
            report = {"global_mode": "unknown", "hosts": {}, "seats": {}, "warnings": [], "probe": None}
        self._local_report_cache = report
        self._local_report_at = now
        return report

    def _local_entries(self, report: Mapping[str, Any] | None = None) -> dict[str, Any]:
        report = report if report is not None else self._local_report()
        entries = report["hosts"] if self.screen == "hosts" else report["seats"]
        return entries if isinstance(entries, dict) else {}

    def _host_groups(self, host_id: str) -> list[str]:
        host = self._local_report()["hosts"].get(host_id)
        groups = host["config"].get("resource_groups") if isinstance(host, Mapping) else None
        return sorted(groups) if isinstance(groups, Mapping) else []

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

    # -- rows and selection -------------------------------------------------------
    def _matches(self, entry: Mapping[str, Any]) -> bool:
        if not self.filter:
            return True
        needle = self.filter.lower()
        haystack = " ".join(
            str(entry.get(name) or "") for name in ("seat", "label", "plan", "host", "model")
        ).lower()
        return needle in haystack

    def _rows(self) -> list[Row]:
        """Rows of the current screen, group headers included, filter applied."""
        if self.screen == "hosts":
            return self._host_rows()
        if self.screen == "claude":
            return self._claude_rows()
        entries = [e for e in self._entries() if self._matches(self._row_fields(e))]
        groups: list[tuple[str, list[dict[str, Any]]]] = []
        if self.screen in {"inventory", "seats"}:
            groups.append(("codex", [e for e in entries if e["provider"] == "codex"]))
        if self.screen == "inventory":
            groups.append(("claude", [e for e in entries if e["provider"] == "claude"]))
        if self.screen in {"inventory", "local-seats"}:
            groups.append(("local", [e for e in entries if e["provider"] == "local"]))
        rows: list[Row] = []
        for name, members in groups:
            if not members and (self.filter or name != "codex" or self.screen != "seats"):
                continue
            rows.append(Row(f"group:{name}", "group", title=self._group_title(name, members)))
            for entry in members:
                key = {
                    "codex": f"codex:{entry['seat']}",
                    "claude": "claude:account",
                    "local": f"local-seat:{entry['seat']}",
                }[entry["provider"]]
                rows.append(Row(key, entry["provider"], entry))
        if self.screen == "local-seats" and self.local_error:
            rows.append(Row("local-seat:unavailable", "group", title=f"Local registry unavailable: {self.local_error}"))
        add = {"inventory": "Add seat", "seats": "Add Codex seat", "local-seats": "Add local seat"}
        rows.append(Row(INVENTORY_ADD_KEY if self.screen == "inventory" else f"add:{self.screen}", "add",
                        title=add[self.screen]))
        return rows

    def _row_fields(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        fields = dict(entry)
        if entry.get("provider") == "local":
            config = self._local_config(str(entry["seat"]))
            fields["host"] = config.get("host")
            fields["model"] = config.get("model_profile")
        return fields

    def _local_config(self, seat_id: str) -> dict[str, Any]:
        seat = self._local_report().get("seats", {}).get(seat_id)
        config = seat.get("config") if isinstance(seat, Mapping) else None
        return config if isinstance(config, dict) else {}

    def _group_title(self, name: str, members: list[dict[str, Any]]) -> str:
        if name == "codex":
            primary = next((e["seat"] for e in members if e.get("role") == "primary"), None)
            tail = f" · primary {primary} is used last" if primary else ""
            return f"CODEX {len(members)}{tail}"
        if name == "claude":
            mode = members[0].get("mode") if members else None
            return "CLAUDE 1" + (f" · {human('claude_mode', mode)}" if mode else "")
        report = self._local_report()
        return f"LOCAL {len(members)} · global {human('local_mode', report.get('global_mode'))}"

    def _host_rows(self) -> list[Row]:
        report = self._local_report()
        rows: list[Row] = []
        hosts = report.get("hosts", {})
        members = []
        for host_id in sorted(hosts):
            host = hosts[host_id]
            config = host.get("config", {}) if isinstance(host, Mapping) else {}
            lifecycle = str(config.get("lifecycle") or "unknown")
            if lifecycle == "retired" and not self.show_history:
                continue
            entry = {"seat": host_id, "label": config.get("label") or host_id, "lifecycle": lifecycle,
                     "host": host_id, "provider": "host"}
            if self._matches(entry):
                members.append(Row(f"host:{host_id}", "host", entry))
        if members or self.local_error:
            title = f"HOSTS {len(members)} · global {human('local_mode', report.get('global_mode'))}"
            if self.local_error:
                title = f"Local registry unavailable: {self.local_error}"
            rows.append(Row("group:hosts", "group", title=title))
        rows.extend(members)
        rows.append(Row("add:hosts", "add", title="Add local host"))
        return rows

    def _claude_rows(self) -> list[Row]:
        rows = [Row("group:claude", "group", title="CLAUDE SETTINGS")]
        for action, label in CLAUDE_SETTINGS:
            rows.append(Row(f"setting:{action}", "setting", {"seat": action, "label": label}, title=label))
        return rows

    def _selected_key(self) -> str | None:
        if self.screen == "inventory":
            return self.inventory_selected
        if self.screen == "seats":
            return f"codex:{self.selected_id}" if self.selected_id else None
        if self.screen == "hosts":
            selected = self.local_selected.get("hosts")
            return f"host:{selected}" if selected else None
        if self.screen == "local-seats":
            selected = self.local_selected.get("local-seats")
            return f"local-seat:{selected}" if selected else None
        return f"setting:{CLAUDE_SETTINGS[self.claude_index % len(CLAUDE_SETTINGS)][0]}"

    def _set_selected(self, key: str) -> None:
        if self.screen == "inventory":
            self.inventory_selected = key
            if key.startswith("codex:"):
                self.selected_id = key.split(":", 1)[1]
        elif self.screen == "seats":
            self.selected_id = key.split(":", 1)[1] if key.startswith("codex:") else self.selected_id
            self._on_add["seats"] = key.startswith("add:")
        elif self.screen == "hosts":
            if key.startswith("host:"):
                self.local_selected["hosts"] = key.split(":", 1)[1]
            self._on_add["hosts"] = key.startswith("add:")
        elif self.screen == "local-seats":
            if key.startswith("local-seat:"):
                self.local_selected["local-seats"] = key.split(":", 1)[1]
            self._on_add["local-seats"] = key.startswith("add:")
        else:
            names = [action for action, _label in CLAUDE_SETTINGS]
            action = key.split(":", 1)[1]
            if action in names:
                self.claude_index = names.index(action)

    def _on_add_row(self) -> bool:
        return self._on_add.get(self.screen, False)

    def _selected_row(self, rows: list[Row] | None = None) -> Row | None:
        """The selected row, repairing a vanished selection to the first row."""
        rows = rows if rows is not None else self._rows()
        selectable = [row for row in rows if row.selectable]
        if not selectable:
            return None
        key = self._selected_key()
        if self._on_add_row():
            key = selectable[-1].key
        chosen = next((row for row in selectable if row.key == key), None)
        if chosen is None:
            chosen = selectable[0]
            self._set_selected(chosen.key)
        return chosen

    def _move(self, delta: int) -> None:
        rows = [row for row in self._rows() if row.selectable]
        if not rows:
            return
        current = self._selected_row()
        index = rows.index(current) if current in rows else 0
        self._set_selected(rows[(index + delta) % len(rows)].key)
        self.detail_scroll = 0

    def _entry(self, provider: str, seat: str | None) -> dict[str, Any] | None:
        return next(
            (e for e in self._entries() if e["provider"] == provider and e["seat"] == seat), None
        )

    # -- row actions ----------------------------------------------------------------
    def _actions_for(self, row: Row | None, *, menu: bool = False) -> list[Action]:
        """Every action for one row, with the reason it cannot run (if any)."""
        if row is None:
            return []
        if row.kind == "codex":
            return self._codex_actions(row.entry, menu=menu)
        if row.kind == "local":
            return self._local_seat_actions(str(row.entry["seat"]), menu=menu)
        if row.kind == "host":
            return self._host_actions(str(row.entry["seat"]), menu=menu)
        if row.kind == "claude":
            return [Action("Enter", "Open Claude settings", "open")]
        if row.kind == "setting":
            return [Action("Enter", f"Change {row.title.lower()}", "open")]
        if row.kind == "add":
            return [Action("Enter", row.title, "open")]
        return []

    def _codex_actions(self, entry: Mapping[str, Any], *, menu: bool = False) -> list[Action]:
        seat_id = str(entry["seat"])
        lifecycle = str(entry.get("lifecycle") or "unknown")
        primary = entry.get("role") == "primary"
        busy = bool(entry.get("busy"))
        gone = "retired · kept for history" if lifecycle == "retired" else None
        bank = entry.get("banked_resets")
        redeem_reason = gone or (
            "resume the seat first" if lifecycle not in {"active", "draining"}
            else "no banked resets" if bank == 0 else None
        )
        redeem_hint = (
            f"spends 1 of {bank}" if isinstance(bank, int) and bank > 0
            else "checks the bank first" if bank is None else ""
        )
        refresher = getattr(self.manager, "refresher", None)
        refresh_reason = gone or (None if refresher is not None else "no refresher in this session")
        if lifecycle == "active":
            pause_label = "Pause"
            pause_reason = "make another seat primary first" if primary else None
            pause_hint = "drains: finishes its task first" if busy else "new work skips it"
        elif lifecycle in {"draining", "inactive"}:
            pause_label, pause_reason, pause_hint = "Resume", None, "checks sign-in and usage first"
        else:
            pause_label, pause_reason, pause_hint = "Pause", gone or "not available", ""
        role_reason = gone or (
            "already primary · used last" if primary
            else "resume the seat first" if lifecycle != "active" else None
        )
        actions = [
            Action("c", "Redeem a reset", "redeem", redeem_reason, redeem_hint, "Redeem reset"),
            Action("r", "Refresh usage", "refresh", refresh_reason, "", "Refresh"),
            Action("e", "Edit plan & headroom", "edit", gone, "", "Edit"),
            Action("o", "Make primary", "role", role_reason, "used last", "Role"),
            Action("p", pause_label, "pause", pause_reason, pause_hint, pause_label),
            Action("l", "Sign in again", "login", gone, "", "Sign in"),
        ]
        if menu and self.manager is not None:
            if lifecycle == "draining":
                actions.append(Action(
                    "", "Pause now", "pause-now",
                    "it is still working" if busy else None, "it is idle", "",
                ))
            pending = {item.seat_id: item for item in self.manager.pending_drains()}
            if seat_id in pending and pending[seat_id].state == "draining":
                actions.append(Action("", "Cancel this session's drain wait", "cancel-drain"))
            remove_reason = gone or (
                "make another seat primary first" if primary
                else "pause it first" if lifecycle != "inactive"
                else "wait for its task to finish" if busy else None
            )
            actions.append(Action(
                "", "Remove seat…", "remove", remove_reason, "keeps home, credentials and history",
            ))
        return actions

    def _local_seat_actions(self, seat_id: str, *, menu: bool = False) -> list[Action]:
        report = self._local_report()
        seat = report.get("seats", {}).get(seat_id, {})
        config = seat.get("config", {}) if isinstance(seat, Mapping) else {}
        lifecycle = str(config.get("lifecycle") or "unknown")
        gone = "retired · kept as history" if lifecycle == "retired" else None
        host = config.get("host")
        host_reason = None if isinstance(host, str) and host in report.get("hosts", {}) else (
            "its host is not registered"
        )
        active = lifecycle == "active"
        actions = [
            Action("r", "Probe host now", "refresh", gone or host_reason, "read-only", "Probe"),
            Action("e", "Edit seat settings", "edit", gone, "", "Edit"),
            Action("p", "Pause" if active else "Resume", "pause", gone,
                   "sets mode off" if active else "mode stays off until you set it",
                   "Pause" if active else "Resume"),
        ]
        if menu:
            remove_reason = gone or (
                None if lifecycle == "inactive" and config.get("mode") == "off"
                else "pause it first (lifecycle inactive, mode off)"
            )
            actions += [
                Action("", "Set seat mode…", "seat-mode", gone),
                Action("", "Set lifecycle…", "seat-lifecycle", gone),
                Action("", "Manage this seat's host", "open-host", host_reason),
                Action("", "Remove seat…", "remove-seat", remove_reason, "keeps retired history"),
                Action("L", "Set global local mode…", "local-mode"),
            ]
        return actions

    def _host_actions(self, host_id: str, *, menu: bool = False) -> list[Action]:
        host = self._local_report().get("hosts", {}).get(host_id, {})
        config = host.get("config", {}) if isinstance(host, Mapping) else {}
        lifecycle = str(config.get("lifecycle") or "unknown")
        gone = "retired · kept as history" if lifecycle == "retired" else None
        active = lifecycle == "active"
        actions = [
            Action("r", "Probe now", "refresh", gone, "read-only, on the refresher", "Probe"),
            Action("e", "Edit host settings", "edit", gone, "", "Edit"),
            Action("p", "Pause" if active else "Resume", "pause", gone, "",
                   "Pause" if active else "Resume"),
            Action("L", "Set global local mode…", "local-mode", None, "", "Local mode"),
        ]
        if menu:
            actions += [
                Action("", "Set lifecycle…", "host-lifecycle", gone),
                Action("", "Add local seat on this host", "add-seat-here", gone),
            ]
        return actions

    # -- key handling ----------------------------------------------------------------
    def _cancel_activation(self) -> None:
        with self._activation_gate:
            self.activation_cancel.set()
        if self.reset_phase == "preview":
            self.reset_cancel.set()

    def handle(self, key: str) -> UIExit | None:
        if (
            self.reset_phase == "redeem"
            and self.reset_thread is not None
            and self.reset_thread.is_alive()
            and key in {"q", "interrupt", "eof"}
        ):
            self.busy_text = "Redemption attempt is in progress; wait for its bounded result"
            return None
        if key in {"interrupt", "eof"}:
            self._cancel_activation()
            return UIExit("quit")
        # The resize-only frame must not accept a now-hidden mutation. q and
        # terminal closure/signals remain usable so the session can exit.
        if self._too_small:
            if key == "q":
                self._cancel_activation()
                # A hidden quit dialog cannot be answered while resize mode
                # blocks its keys. Exit as EOF does, leaving drains intact.
                return UIExit("quit")
            return None
        # A result line lasts until the next key.
        self.message = ""
        view = self.view
        if view in TEXT_VIEWS:
            return self._handle_text(key)
        if self.filtering:
            return self._handle_filter(key)
        if view == "quit-confirm":
            if key in {"enter", "y"}:
                return UIExit("quit")
            if key in {"esc", "n"}:
                self.view = self._quit_return_view
            return None
        if key == "q":
            self._cancel_activation()
            return self._request_quit()
        if view == "busy":
            if key == "esc":
                if self.reset_phase == "redeem":
                    self.busy_text = "Confirmed attempt is in progress; awaiting its result"
                else:
                    self._cancel_activation()
                    self.busy_text = "Cancel requested; waiting for bounded provider work to stop"
            return None
        if view == "confirm":
            return self._handle_confirm(key)
        if view == "overview":
            if key == "m":
                self._open_inventory()
            elif key == "?":
                self._push("help")
            elif key == "g":
                self._open_global_settings()
            elif key == "R":
                self._refresh_all()
            return None
        if view == "help":
            if key in {"esc", "?"}:
                self._back()
            elif key in {"down", "j"}:
                self.help_scroll += 1
            elif key in {"up", "k"}:
                self.help_scroll = max(0, self.help_scroll - 1)
            elif key == "pagedown":
                self.help_scroll += self._page_size
            elif key == "pageup":
                self.help_scroll = max(0, self.help_scroll - self._page_size)
            return None
        # Help, back, refresh-all and settings work on every remaining view.
        if key == "?":
            self._push("help")
            return None
        if key == "R":
            self._refresh_all()
            return None
        if view == "list":
            return self._handle_list(key)
        if key == "g" and view not in {"global-settings", "global-choice"}:
            self._open_global_settings()
            return None
        if view == "global-settings":
            return self._handle_global_settings(key)
        if view in {"global-choice", "local-choice"}:
            return self._handle_choice(key)
        if view in {"actions", "local-actions"}:
            return self._handle_menu(key)
        if view in {"settings", "add-form", "local-form"}:
            return self._handle_form(key)
        if view in {"add-presets", "capacity-presets"}:
            return self._handle_presets(key)
        return None

    def _request_quit(self) -> UIExit | None:
        pending = (
            [item for item in self.manager.pending_drains() if item.state == "draining"]
            if self.manager is not None
            else []
        )
        if pending:
            self.busy_text = (
                "Quitting stops this session's wait. These seats remain draining: "
                + ", ".join(item.seat_id for item in pending)
                + ". They will not be deactivated unattended."
            )
            self._quit_return_view = self.view
            self.confirm_return_view = self.view if self.view not in DIALOG_VIEWS else self.confirm_return_view
            self.view = "quit-confirm"
            return None
        return UIExit("quit")

    def _handle_filter(self, key: str) -> UIExit | None:
        if key == "esc":
            self.filter = ""
            self.filtering = False
        elif key == "enter":
            self.filtering = False
        elif key == "backspace":
            self.filter = self.filter[:-1]
        elif key in {"up", "down"}:
            self._move(1 if key == "down" else -1)
        elif len(key) == 1 and key.isprintable() and len(self.filter) < 40:
            self.filter += key
        self.detail_scroll = 0
        return None

    def _handle_list(self, key: str) -> UIExit | None:
        if key in {"down", "j"}:
            self._move(1)
        elif key in {"up", "k"}:
            self._move(-1)
        elif key == "pagedown":
            self.detail_scroll += self._detail_page_size
        elif key == "pageup":
            self.detail_scroll = max(0, self.detail_scroll - self._detail_page_size)
        elif key in {KEY_TAB, KEY_BACKTAB}:
            self._switch_screen(1 if key == KEY_TAB else -1)
        elif key == "/" and self.screen != "claude":
            self.filtering = True
        elif key == "d" and self.screen in {"inventory", "seats"}:
            self.show_raw = not self.show_raw
        elif key == "h" and self.screen != "claude":
            self.show_history = not self.show_history
            self.detail_scroll = 0
            self._invalidate()
        elif key == "g":
            self._open_global_settings()
        elif key == "esc":
            if self.filter:
                self.filter = ""
            else:
                self._back()
        else:
            binding = next((k for k in KEYMAP[self.screen] if key in k.names and k.action), None)
            if binding is not None:
                return self._run(binding.action)
        return None

    def _run(self, action: str, row: Row | None = None) -> UIExit | None:
        """Run one row action from a key or the actions menu."""
        row = row if row is not None else self._selected_row()
        if action == "add":
            self._open_add()
            return None
        if row is None:
            return None
        if action == "open":
            return self._open_row(row)
        if action == "local-mode" and self.screen in LOCAL_SCREENS | {"inventory"}:
            if row.kind not in {"local", "host"} and self.screen == "inventory":
                self._error("L sets the local mode from a local row or Local hosts")
                return None
            self._open_mode_choice("global")
            return None
        if row.kind == "claude":
            if action == "edit":
                self._push("list", "claude")
            else:
                self._error("Claude settings are on the Claude screen · Enter opens them")
            return None
        if row.kind in {"add", "group", "setting"}:
            if row.kind == "setting" and action == "edit":
                return self._open_row(row)
            self._error("select a host first" if self.screen == "hosts" else "select a seat first")
            return None
        available = {item.action: item for item in self._actions_for(row, menu=True)}
        item = available.get(action)
        if item is None:
            name = _ACTION_NAMES.get(action, action.replace("-", " ").capitalize())
            where = "Codex seats" if action in _CODEX_ONLY else "this row"
            self._error(f"{name} applies to {where} only" if action in _CODEX_ONLY
                        else f"{name} is not available for {where}")
            return None
        if item.reason:
            self._error(f"{item.label.rstrip('…')}: {item.reason}")
            return None
        if row.kind == "codex":
            return self._codex_action(str(row.entry["seat"]), action, row.entry)
        if row.kind == "local":
            self._local_action("local-seats", str(row.entry["seat"]), action)
        elif row.kind == "host":
            self._local_action("hosts", str(row.entry["seat"]), action)
        return None

    def _open_row(self, row: Row) -> UIExit | None:
        """Enter on a row opens its actions (or the edit for a setting)."""
        self.menu_index = 0
        if row.kind == "codex":
            self.selected_id = str(row.entry["seat"])
            self._push("actions")
        elif row.kind == "claude":
            self._push("list", "claude")
        elif row.kind == "local":
            seat_id = str(row.entry["seat"])
            if seat_id not in self._local_report().get("seats", {}):
                return None
            self._push("local-actions", "local-seats")
            self.local_selected["local-seats"] = seat_id
        elif row.kind == "host":
            self._push("local-actions", "hosts")
            self.local_selected["hosts"] = str(row.entry["seat"])
        elif row.kind == "add":
            self._open_add()
        elif row.kind == "setting":
            self._choose_claude_action(str(row.entry["seat"]))
        return None

    def _open_add(self) -> None:
        if self.screen == "inventory":
            self._open_choice("Add seat", "choose the seat type", "inventory-add",
                              list(ADD_TYPE_CHOICES), ADD_TYPE_CODEX)
        elif self.screen == "seats":
            self._open_codex_add()
        elif self.screen == "hosts":
            self._open_local_form("add-host")
        elif self.screen == "local-seats":
            self._open_local_form("add-seat")
        else:
            self._error("Claude is a single account; its settings are listed here")

    def _open_codex_add(self) -> None:
        self.add_buffers = {"label": "New seat", "home": "", "profile": None}
        self.add_field_index = 0
        self._push("add-form")

    def _handle_menu(self, key: str) -> UIExit | None:
        actions = self._menu_actions()
        if key in {"down", "j"} and actions:
            self.menu_index = (self.menu_index + 1) % len(actions)
        elif key in {"up", "k"} and actions:
            self.menu_index = (self.menu_index - 1) % len(actions)
        elif key == "pagedown" and actions:
            self.menu_index = min(len(actions) - 1, self.menu_index + self._page_size)
        elif key == "pageup":
            self.menu_index = max(0, self.menu_index - self._page_size)
        elif key == "esc":
            self._back()
        elif key == "enter" and actions:
            item = actions[min(self.menu_index, len(actions) - 1)]
            return self._run(item.action, self._menu_row())
        else:
            # Direct keys work inside the menu too (c, r, e, o, p, l, L).
            item = next((a for a in actions if a.key == key and a.key), None)
            if item is not None:
                return self._run(item.action, self._menu_row())
        return None

    def _menu_row(self, view: str | None = None) -> Row | None:
        if (view or self.view) == "actions":
            entry = self._entry("codex", self.selected_id)
            if entry is None and self.manager is not None and self.selected_id in self._codex_ids(
                include_retired=True
            ):
                entry = self._codex_skeleton(self.selected_id, self._codex_records().get(self.selected_id, {}))
            return Row(f"codex:{self.selected_id}", "codex", entry) if entry else None
        selected = self.local_selected_id
        if selected is None or selected not in self._local_entries():
            return None
        kind = "host" if self.screen == "hosts" else "local"
        return Row(f"{kind}:{selected}", kind, {"seat": selected, "provider": kind})

    def _menu_actions(self, view: str | None = None) -> list[Action]:
        actions = self._actions_for(self._menu_row(view), menu=True)
        self.menu_index = min(max(0, self.menu_index), max(0, len(actions) - 1))
        return actions

    def _handle_choice(self, key: str) -> UIExit | None:
        """Shared key handling for every short choice list."""
        if self.view == "global-choice":
            items = [(str(value), value) for value in self._global_choices()]
            index_name = "global_choice_index"
        else:
            items = self.choice_items
            index_name = "choice_index"
        index = getattr(self, index_name)
        if key in {"down", "j"} and items:
            setattr(self, index_name, (index + 1) % len(items))
        elif key in {"up", "k"} and items:
            setattr(self, index_name, (index - 1) % len(items))
        elif key == "pagedown" and items:
            setattr(self, index_name, min(len(items) - 1, index + self._page_size))
        elif key == "pageup":
            setattr(self, index_name, max(0, index - self._page_size))
        elif key == "esc":
            self._back()
        elif key == "enter" and items:
            if self.view == "global-choice":
                self._apply_global_value(items[index][1])
            else:
                self._choice_selected(items[index][1])
        return None

    def _handle_form(self, key: str) -> UIExit | None:
        if self.view == "settings":
            fields: list[Any] = list(FIELDS)
            index_name = "field_index"
        elif self.view == "add-form":
            fields = list(ADD_FIELDS)
            index_name = "add_field_index"
        else:
            fields = list(self.local_form_fields)
            index_name = "local_form_index"
        index = getattr(self, index_name)
        if key in {"down", "j"} and fields:
            setattr(self, index_name, (index + 1) % len(fields))
        elif key in {"up", "k"} and fields:
            setattr(self, index_name, (index - 1) % len(fields))
        elif key == "pagedown" and fields:
            setattr(self, index_name, min(len(fields) - 1, index + self._page_size))
        elif key == "pageup":
            setattr(self, index_name, max(0, index - self._page_size))
        elif key == "esc":
            self._back()
        elif key == "enter" and fields:
            field_item = fields[index]
            if self.view == "settings":
                self._open_settings_field(field_item)
            elif self.view == "add-form":
                if field_item.kind == "preset":
                    self._push("add-presets")
                elif field_item.kind == "submit":
                    self._apply_add()
                else:
                    self.input_buffer = str(self.add_buffers[field_item.option] or "")
                    self._push("add-input")
            elif field_item.kind == "submit":
                self._apply_local_form()
            elif field_item.kind == "choice":
                self._open_form_choice(field_item)
            else:
                self.input_buffer = self.local_form_buffers.get(field_item.option, "")
                self._push("local-form-input")
        return None

    def _open_settings_field(self, field_item: Field) -> None:
        if field_item.kind == "capacity":
            self.capacity_buffers = {
                option: self._current_capacity_value(option) for _label, option in CAPACITY_PARTS
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
            self._push("capacity-presets")
        else:
            self.input_buffer = self._current_field_value(field_item)
            self._push("input")

    def _handle_presets(self, key: str) -> UIExit | None:
        if self.view == "add-presets":
            choices: list[Any] = list(ADD_PLAN_CHOICES)
            index_name = "add_preset_index"
        else:
            choices = list(CAPACITY_CHOICES)
            index_name = "capacity_preset_index"
        index = getattr(self, index_name)
        if key in {"down", "j"}:
            setattr(self, index_name, (index + 1) % len(choices))
        elif key in {"up", "k"}:
            setattr(self, index_name, (index - 1) % len(choices))
        elif key == "pagedown":
            setattr(self, index_name, min(len(choices) - 1, index + self._page_size))
        elif key == "pageup":
            setattr(self, index_name, max(0, index - self._page_size))
        elif key == "esc":
            self._back()
        elif key == "enter":
            if self.view == "add-presets":
                self.add_buffers["profile"] = ADD_PLAN_CHOICES[index]["profile"]
                self._back()
            elif index == len(CAPACITY_CHOICES) - 1:
                self.capacity_index = 0
                self._push("capacity-input")
            else:
                self._apply_capacity_preset(CAPACITY_CHOICES[index])
        return None

    def _handle_text(self, key: str) -> UIExit | None:
        """Text entry: printable keys (q, j, k and ? included) are typed."""
        view = self.view
        if view == "capacity-input":
            option = CAPACITY_PARTS[self.capacity_index][1]
            if key == "esc":
                self._back()
            elif key == "down":
                self.capacity_index = (self.capacity_index + 1) % len(CAPACITY_PARTS)
            elif key == "up":
                self.capacity_index = (self.capacity_index - 1) % len(CAPACITY_PARTS)
            elif key == "backspace":
                self.capacity_buffers[option] = self.capacity_buffers[option][:-1]
            elif key == "enter":
                self._apply_capacity()
            elif len(key) == 1 and key.isprintable() and len(self.capacity_buffers[option]) < 240:
                self.capacity_buffers[option] += key
            return None
        if key == "esc":
            self._back()
            return None
        if key == "backspace":
            self.input_buffer = self.input_buffer[:-1]
            return None
        if key == "enter":
            if view == "input":
                self._apply_field(FIELDS[self.field_index])
            elif view == "add-input":
                self.add_buffers[ADD_FIELDS[self.add_field_index].option] = self.input_buffer.strip()
                self._back()
            elif view == "local-form-input":
                field_item = self.local_form_fields[self.local_form_index]
                self.local_form_buffers[field_item.option] = self.input_buffer.strip()
                self._back()
            elif view == "claude-input":
                self._apply_claude_headroom()
            elif view == "global-input":
                self._apply_global_value(self.input_buffer)
            return None
        if view == "global-input":
            if len(key) == 1 and key.isdecimal() and len(self.input_buffer) < 3:
                self.input_buffer += key
        elif view == "claude-input":
            if len(key) == 1 and key.isprintable() and len(self.input_buffer) < CLAUDE_INPUT_LIMIT:
                self.input_buffer += key
        elif view == "local-form-input":
            # Pasted bursts arrive as one printable string; JSON fields such as
            # probe argv are easier to paste than to type.
            if (
                key not in _KEY_NAMES
                and key.isprintable()
                and len(self.input_buffer) + len(key) <= LOCAL_INPUT_LIMIT
            ):
                self.input_buffer += key
        elif len(key) == 1 and key.isprintable() and len(self.input_buffer) < 240:
            self.input_buffer += key
        return None

    # -- codex seat actions ------------------------------------------------------------
    def _codex_action(
        self, seat_id: str, action: str, entry: Mapping[str, Any]
    ) -> UIExit | None:
        assert self.manager is not None
        self.selected_id = seat_id
        try:
            if action == "refresh":
                self.manager.request_refresh(seat_id)
                self._toast(f"↻ Refreshing {seat_id} · last-good usage stays visible", "info")
            elif action == "redeem":
                self._start_reset_work("preview")
            elif action == "edit":
                self.field_index = 0
                self._push("settings")
            elif action == "role":
                if self._commit_codex(self.manager.preview_set_role(seat_id, "primary")):
                    self._toast(f"✓ {seat_id} is now primary · routing uses it last")
            elif action == "pause":
                lifecycle = (
                    self.manager.settings.get("pool", {}).get("seats", {}).get(seat_id, {})
                    .get("lifecycle", entry.get("lifecycle"))
                )
                if lifecycle == "active":
                    self._pause(seat_id)
                else:
                    self._resume(seat_id)
            elif action == "pause-now":
                self._pause(seat_id)
            elif action == "login":
                self.login_plan = self.manager.preview_login(seat_id)
                return UIExit("login", self.login_plan.seat_id, self.login_plan)
            elif action == "cancel-drain":
                result = self.manager.cancel_pending_drain(seat_id)
                self._toast(f"– Stopped waiting on {seat_id} · {result.detail}", "info")
            elif action == "remove":
                self._open_remove(seat_id)
        except ManagerError as exc:
            self._error(exc)
        return None

    def _commit_codex(self, preview: ActionPreview) -> bool:
        """Commit a fresh preview; ``False`` (with a result line) if nothing changed."""
        assert self.manager is not None
        result = self.manager.commit(preview)
        self._invalidate()
        if not result.changed:
            self._toast("No change was needed", "info")
            return False
        return True

    def _pause(self, seat_id: str) -> None:
        assert self.manager is not None
        plan = self.manager.preview_pause(seat_id)
        result = self.manager.commit_pause(plan)
        self._invalidate()
        if result.pending_drain:
            self._toast(
                f"◐ Draining {seat_id} · it pauses when its task ends · p resumes", "info"
            )
        else:
            self._toast(f"✓ Paused {seat_id} · new work skips it · p resumes")

    def _resume(self, seat_id: str) -> None:
        assert self.manager is not None
        seats = self.manager.settings.get("pool", {}).get("seats", {})
        active = any(entry.get("lifecycle") == "active" for entry in seats.values())
        self.preview = self.manager.preview_reactivate(seat_id, as_primary=not active)
        self._start_activation(self.preview)

    def _open_remove(self, seat_id: str) -> None:
        assert self.manager is not None
        manager = self.manager

        def preview_it() -> None:
            self.preview = manager.preview_remove(seat_id)

        preview_it()
        self.confirm_kind = "remove"
        self._open_dialog(preview_it)

    def _open_dialog(self, refresh: Callable[[], None] | None = None) -> None:
        if self.view not in DIALOG_VIEWS:
            self.confirm_return_view = self.view
        self._confirm_refresh = refresh
        self.confirm_raw = False
        self.confirm_stale = False
        self.scroll = 0
        self.view = "confirm"

    def _close_dialog(self) -> None:
        self.view = self.confirm_return_view
        self.preview = None
        self.reset_preview = None
        self.local_preview = None
        self.confirm_kind = None
        self.confirm_stale = False
        self._confirm_refresh = None

    def _handle_confirm(self, key: str) -> UIExit | None:
        if key == "d":
            self.confirm_raw = not self.confirm_raw
            self.scroll = 0
        elif key in {"down", "j"}:
            self.scroll += 1
        elif key in {"up", "k"}:
            self.scroll = max(0, self.scroll - 1)
        elif key == "pagedown":
            self.scroll += self._page_size
        elif key == "pageup":
            self.scroll = max(0, self.scroll - self._page_size)
        elif key in {"esc", "n"}:
            self._close_dialog()
        elif key in {"enter", "y"}:
            if self.confirm_stale:
                refresh = self._confirm_refresh
                if refresh is None:
                    self._close_dialog()
                    self._error("settings changed since the preview; start the action again")
                    return None
                try:
                    refresh()
                except ManagerError as exc:
                    self._close_dialog()
                    self._error(exc)
                    return None
                self.confirm_stale = False
                self.confirm_raw = False
                self.scroll = 0
                return None
            return self._confirm()
        return None

    def _confirm(self) -> UIExit | None:
        """Apply the confirmed irreversible action; the digest pin is kept."""
        assert self.manager is not None
        kind = self.confirm_kind
        if kind == "redeem-reset":
            if self.reset_preview is None:
                self._close_dialog()
                self._error("Reset preview expired; press c again")
            else:
                self._start_reset_work("redeem")
            return None
        try:
            if kind == "remove" and self.preview is not None:
                seat_id = self.preview.seat_id
                self.manager.commit(self.preview)
                done = (
                    f"✓ Removed {seat_id} from service · home, credentials and history kept"
                    " · h shows retired seats"
                )
                self.selected_id = None
            elif kind == "remove-local" and self.local_preview is not None:
                seat_id = str(self.local_preview.target_id)
                result = self._local().commit(self.local_preview, from_current_user_request=True)
                done = (
                    f"✓ Removed local seat {seat_id} · kept as retired history · h shows it"
                    if result.changed else f"Local seat {seat_id} was already retired"
                )
            else:
                self._close_dialog()
                return None
        except StalePreviewError:
            # Nothing was written; offer a fresh preview in place.
            self.confirm_stale = True
            return None
        except ManagerError as exc:
            self._close_dialog()
            self._error(exc)
            return None
        self._close_dialog()
        self._invalidate()
        self.show_history = False
        self._pop_to_list()
        self._toast(done)
        return None

    def _apply_add(self) -> None:
        assert self.manager is not None
        try:
            label = str(self.add_buffers["label"]).strip()
            home_text = str(self.add_buffers["home"]).strip()
            profile = self.add_buffers["profile"]
            preview = self.manager.preview_add(
                label=label, home=home_text or None, profile=str(profile) if profile else None,
            )
            result = self.manager.commit(preview)
        except (ManagerError, ValueError) as exc:
            self._error(f"Cannot add seat: {exc}")
            return
        self._invalidate()
        seat_id = result.seat_id
        self.selected_id = seat_id
        self.show_history = False
        self._pop_to_list()
        if self.screen == "inventory":
            self.inventory_selected = f"codex:{seat_id}"
        self._on_add[self.screen] = False
        self._toast(f"✓ Added {seat_id} (paused) · l signs it in · p resumes it")

    def _current_field_value(self, field_item: Field) -> str:
        assert self.manager is not None and self.selected_id is not None
        pool = self.manager.settings["pool"]
        entry = pool["seats"][self.selected_id]
        if field_item.kind == "capacity":
            profile = entry.get("capacity_profile")
            return human("capacity_profile", profile)
        if field_item.option == "reserve_pct":
            return str(pool.get("reserves_pct", {}).get(self.selected_id, 0))
        if field_item.option in {"small_task_min_headroom_pct", "big_task_min_headroom_pct"}:
            size = "small" if field_item.option.startswith("small") else "big"
            overrides = pool.get("task_min_headroom_pct_by_seat", {}).get(self.selected_id, {})
            return str(overrides.get(size, pool.get(field_item.option, 0)))
        value = entry.get(field_item.option)
        return "" if value is None else str(value)

    def _current_capacity_value(self, option: str) -> str:
        assert self.manager is not None and self.selected_id is not None
        entry = self.manager.settings["pool"]["seats"][self.selected_id]
        key = "capacity_profile" if option == "profile" else option
        value = entry.get(key)
        return "null" if value is None else str(value)

    def _back_to(self, view: str) -> None:
        while self.view != view and self._stack:
            self._back()

    def _apply_field(self, field_item: Field) -> None:
        assert self.manager is not None and self.selected_id is not None
        raw = self.input_buffer.strip()
        try:
            value: Any = float(raw) if field_item.kind == "number" else raw
            changed = self._commit_codex(
                self.manager.preview_set(self.selected_id, **{field_item.option: value})
            )
        except (ManagerError, ValueError) as exc:
            self._error(f"Invalid value: {exc}")
            return
        self._back_to("settings")
        if changed:
            self._toast(f"✓ Saved {self.selected_id} · {field_item.label} {raw}")

    def _apply_capacity(self) -> None:
        assert self.manager is not None and self.selected_id is not None
        try:
            relative_text = self.capacity_buffers["relative_capacity"].strip()
            relative: Any = (
                None if relative_text.lower() in {"null", "none"} else float(relative_text)
            )
            changed = self._commit_codex(self.manager.preview_set(
                self.selected_id,
                profile=self.capacity_buffers["profile"].strip(),
                relative_capacity=relative,
                capacity_source=self.capacity_buffers["capacity_source"].strip(),
            ))
        except (ManagerError, ValueError) as exc:
            self._error(f"Invalid capacity values: {exc}")
            return
        self._back_to("settings")
        if changed:
            self._toast(f"✓ Saved {self.selected_id} · plan and capacity")

    def _apply_capacity_preset(self, preset: Mapping[str, Any]) -> None:
        assert self.manager is not None and self.selected_id is not None
        try:
            changed = self._commit_codex(self.manager.preview_set(
                self.selected_id,
                profile=preset["profile"],
                relative_capacity=preset["relative_capacity"],
                capacity_source=preset["capacity_source"],
            ))
        except (ManagerError, KeyError, ValueError) as exc:
            self._error(f"Invalid capacity preset: {exc}")
            return
        self._back_to("settings")
        if changed:
            self._toast(f"✓ Saved {self.selected_id} · plan {human('capacity_profile', preset['profile'])}")

    def _refresh_all(self) -> None:
        refresher = getattr(self.manager, "refresher", None) if self.manager is not None else None
        if refresher is None or not hasattr(refresher, "request_full_refresh"):
            self._error("no refresher in this session")
            return
        try:
            _seat_manager.ensure_not_managed_worker()
            refresher.request_full_refresh()
        except (ManagerError, RuntimeError) as exc:
            self._error(exc)
            return
        self._toast("↻ Refreshing every seat · last-good usage stays visible", "info")

    # -- background provider work ----------------------------------------------------
    def _start_activation(self, preview: ActionPreview) -> None:
        assert self.manager is not None
        self.activation_cancel = threading.Event()
        self.activation_result = None
        self.activation_error = None
        manager = self.manager

        def work() -> None:
            try:
                result = manager.run_activation_checks(preview, self.activation_cancel)
                with self._result_lock:
                    self.activation_result = result
            except Exception as exc:  # surfaced as a structured UI failure
                with self._result_lock:
                    self.activation_error = str(exc)

        self.activation_thread = threading.Thread(
            target=work, name="seat-activation-check", daemon=True
        )
        self.activation_thread.start()
        if self.view not in DIALOG_VIEWS:
            self.confirm_return_view = self.view
        self.view = "busy"
        self.busy_text = f"Checking sign-in and fresh usage for {preview.seat_id}…"

    def _start_reset_work(self, phase: str) -> None:
        assert self.manager is not None and self.selected_id is not None
        if self.reset_thread is not None and self.reset_thread.is_alive():
            self._error("A reset action is already in progress")
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
        if self.view not in DIALOG_VIEWS:
            self.confirm_return_view = self.view
        self.view = "busy"
        self.busy_text = (
            f"Reading {seat_id}'s banked resets and account…"
            if phase == "preview" else f"Redeeming one banked reset on {seat_id}…"
        )

    def poll(self) -> bool:
        changed = False
        if self.manager is not None and time.monotonic() - self.last_reconcile >= 0.5:
            self.last_reconcile = time.monotonic()
            try:
                drains = self.manager.reconcile_pending_drains()
            except ManagerError as exc:
                self._error(exc)
                changed = True
            else:
                signature = tuple((item.seat_id, item.state) for item in drains)
                if signature != self._drain_signature:
                    self._drain_signature = signature
                    self._invalidate()
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
                self._pool_cache = None
                changed = True
        reset_thread = self.reset_thread
        if self.view == "busy" and reset_thread is not None and not reset_thread.is_alive():
            with self._result_lock:
                result, error = self.reset_result, self.reset_error
            if (
                self.reset_phase == "preview"
                and not error
                and isinstance(result, dict)
                and not self.reset_cancel.is_set()
            ):
                self.reset_preview = result
                self.confirm_kind = "redeem-reset"
                self._open_dialog(None)
            else:
                self.view = self.confirm_return_view
                self.reset_preview = None
                self.confirm_kind = None
                if error:
                    self._error(f"Reset action failed: {error}")
                elif isinstance(result, dict):
                    outcome = clean_text(result.get("outcome", "unknown"))
                    refresh = result.get("refresh_error")
                    self._toast(
                        f"{'✓' if outcome == 'redeemed' else '!'} Reset outcome: {outcome}"
                        + (f" · refresh: {refresh}" if refresh else " · usage refreshed"),
                        "ok" if outcome == "redeemed" else "info",
                    )
                else:
                    self._toast("Reset preview canceled · nothing was spent", "info")
                self._invalidate()
            self.reset_thread = None
            self.reset_phase = None
            return True
        thread = self.activation_thread
        if self.view != "busy" or thread is None or thread.is_alive():
            return changed
        with self._result_lock:
            checks = self.activation_result
            error = self.activation_error
        self.view = self.confirm_return_view
        try:
            if error:
                self._error(f"Activation checks failed: {error}")
            elif checks is None:
                self._error("Activation checks ended without a result; seat remains paused")
            elif not checks.passed:
                self._error(f"{checks.detail}; seat remains paused")
            else:
                assert self.preview is not None and self.manager is not None
                with self._activation_gate:
                    if self.activation_cancel.is_set():
                        self._toast(
                            "Activation canceled before commit; seat remains paused", "info"
                        )
                    else:
                        self.manager.commit_activation(self.preview, checks)
                        self._toast(f"✓ Resumed {checks.seat_id} · sign-in and usage checked")
        except ManagerError as exc:
            self._error(exc)
        self.activation_thread = None
        self._invalidate()
        return True

    def shutdown(self, timeout: float = 10.0) -> bool:
        self._cancel_activation()
        thread = self.activation_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, timeout))
        reset_thread = self.reset_thread
        if reset_thread is not None and reset_thread.is_alive():
            reset_thread.join(timeout=max(0.0, timeout))
        return (thread is None or not thread.is_alive()) and (
            reset_thread is None or not reset_thread.is_alive()
        )

    # -- local hosts and seats --------------------------------------------------------
    def _local_action(self, screen: str, target: str, action: str) -> None:
        """One local host/seat action; ``screen`` names the registry it targets."""
        report = self._local_report()
        if action == "refresh":
            host = target
            if screen == "local-seats":
                config = report["seats"].get(target, {}).get("config", {})
                host = config.get("host") if isinstance(config, Mapping) else None
            self._request_probe(str(host))
        elif action == "edit":
            self._open_local_form("edit-host" if screen == "hosts" else "edit-seat", target)
        elif action == "pause":
            entries = report["hosts" if screen == "hosts" else "seats"]
            config = entries.get(target, {}).get("config", {})
            current = config.get("lifecycle") if isinstance(config, Mapping) else None
            lifecycle = "inactive" if current == "active" else "active"
            try:
                local = self._local()
                preview = (
                    local.preview_host_lifecycle(target, lifecycle)
                    if screen == "hosts"
                    else local.preview_seat_lifecycle(target, lifecycle)
                )
            except (ManagerError, ValueError) as exc:
                self._error(exc)
                return
            if lifecycle == "inactive":
                done = f"✓ Paused {target} · new local work skips it · p resumes"
            elif screen == "hosts":
                done = f"✓ Resumed host {target}"
            else:
                done = f"✓ Resumed {target} · its mode is Off until you set it (Enter → Set seat mode)"
            self._commit_local_preview(preview, done)
        elif action in {"seat-mode", "local-mode"}:
            if action == "seat-mode":
                self.local_selected["local-seats"] = target
            self._open_mode_choice("seat" if action == "seat-mode" else "global", target)
        elif action in {"seat-lifecycle", "host-lifecycle"}:
            self._open_lifecycle_choice(screen, target)
        elif action == "open-host":
            config = report["seats"].get(target, {}).get("config", {})
            host = config.get("host") if isinstance(config, Mapping) else None
            self._push("local-actions", "hosts")
            self.local_selected["hosts"] = str(host)
            self.menu_index = 0
        elif action == "add-seat-here":
            self._open_local_form("add-seat", preset_host=target)
        elif action == "remove-seat":
            self._open_remove_local(target)

    def _open_remove_local(self, seat_id: str) -> None:
        """Retire a local seat in place; the core refuses unless inactive and off."""
        def preview_it() -> None:
            self.local_preview = self._local().preview_remove_seat(seat_id)

        try:
            preview_it()
        except ManagerError as exc:
            self._error(
                f"Cannot remove local seat {seat_id}: {exc} · it must be inactive with mode off"
            )
            return
        if not self.local_preview.changed:
            self.local_preview = None
            self._toast(f"Local seat {seat_id} is already retired", "info")
            return
        self.confirm_kind = "remove-local"
        self._open_dialog(preview_it)

    def _request_probe(self, host_id: str) -> None:
        """Queue one read-only probe on the refresher; never probe here."""
        try:
            queued = self._local().request_probe(host_id)
        except ManagerError as exc:
            self._error(f"Cannot probe {host_id}: {exc}")
            return
        self._toast(f"↻ Probe queued for {queued} · the detail shows the result when it lands", "info")

    def _commit_local_preview(self, preview: Any, done: str) -> bool:
        try:
            result = self._local().commit(preview, from_current_user_request=True)
        except ManagerError as exc:
            self._error(exc)
            return False
        self._invalidate()
        if self.manager is not None and hasattr(self.manager, "reload"):
            try:
                self.manager.reload()
            except (ManagerError, OSError, ValueError):
                pass
        text = done if result.changed else "No change was needed"
        if result.warnings:
            text += " · " + "; ".join(str(warning) for warning in result.warnings)
        self._toast(text, "ok" if result.changed else "info")
        return True

    def _open_choice(
        self,
        title: str,
        context: Any,
        purpose: str,
        items: list[tuple[str, Any]],
        current: Any,
        *,
        screen: str | None = None,
        target: str | None = None,
    ) -> None:
        # Push first: the back stack saves the choice this one was opened from.
        self._push("local-choice", screen)
        self.choice_title = title
        self.choice_context = context
        self.choice_purpose = purpose
        self.choice_items = items
        self.choice_target = target
        self.choice_index = next(
            (index for index, (_label, value) in enumerate(items) if value == current), 0
        )

    def _open_mode_choice(self, scope: str, target: str | None = None) -> None:
        report = self._local_report()
        if scope == "global":
            current = report.get("global_mode")
            title, context = "Global local mode", "every local seat"
        else:
            target = target or self.local_selected.get("local-seats")
            seat = report["seats"].get(target, {})
            current = seat.get("config", {}).get("mode") if isinstance(seat, Mapping) else None
            title, context = "Seat mode", target
        items = [
            (human("local_mode", mode) + (" (current)" if mode == current else ""), mode)
            for mode in LOCAL_MODE_ORDER
        ]
        self._open_choice(title, context, f"{scope}-mode", items, current, target=target)

    def _open_lifecycle_choice(self, screen: str, target: str) -> None:
        entries = self._local_report()["hosts" if screen == "hosts" else "seats"]
        entry = entries.get(target, {})
        config = entry.get("config", {}) if isinstance(entry, Mapping) else {}
        current = config.get("lifecycle")
        items: list[tuple[str, Any]] = []
        for lifecycle in LOCAL_LIFECYCLE_ORDER:
            label = human("lifecycle", lifecycle)
            if lifecycle == current:
                items.append((f"{label} (current)", lifecycle))
                continue
            try:
                validate_lifecycle_transition(str(current), lifecycle)
            except (SeatRegistryError, ValueError, KeyError):
                continue
            items.append((label, lifecycle))
        if len(items) <= 1:
            self._error(f"No lifecycle change is available from {human('lifecycle', current)}")
            return
        purpose = "host-lifecycle" if screen == "hosts" else "seat-lifecycle"
        self._open_choice(
            "Host lifecycle" if screen == "hosts" else "Seat lifecycle", target, purpose,
            items, current, target=target,
        )

    def _choice_selected(self, value: Any) -> None:
        purpose = self.choice_purpose
        if purpose.startswith("form:"):
            option = purpose[len("form:"):]
            self.local_form_buffers[option] = str(value)
            if option == "host":
                groups = self._host_groups(str(value))
                if self.local_form_buffers.get("resource_group") not in groups:
                    self.local_form_buffers["resource_group"] = groups[0] if groups else ""
            self._back()
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
                self._error(exc)
                return
            text = (
                f"✓ Claude mode → {human('claude_mode', value)}"
                if purpose == "claude-mode"
                else f"✓ Claude tier → {value or 'Unknown'}"
            )
            if self._commit_claude_preview(preview, text):
                self._back()
            return
        target = self.choice_target
        try:
            local = self._local()
            if purpose == "global-mode":
                preview = local.preview_global_mode(value)
                text = f"✓ Global local mode → {human('local_mode', value)}"
            elif purpose == "seat-mode":
                preview = local.preview_seat_mode(target, value)
                text = f"✓ {target} mode → {human('local_mode', value)}"
            elif purpose == "host-lifecycle":
                preview = local.preview_host_lifecycle(target, value)
                text = f"✓ Host {target} → {human('lifecycle', value)}"
            elif purpose == "seat-lifecycle":
                preview = local.preview_seat_lifecycle(target, value)
                text = f"✓ {target} → {human('lifecycle', value)}"
            else:
                raise ManagerError(f"unknown choice {purpose!r}")
        except (ManagerError, ValueError) as exc:
            self._error(exc)
            return
        if self._commit_local_preview(preview, text):
            self._back()

    def _inventory_add(self, target: str) -> None:
        """The Add seat type menu: Codex form, Claude settings, or a local seat."""
        if target == ADD_TYPE_LOCAL and not self._local_report().get("hosts"):
            self._open_choice(
                "Add local seat", "no local host is registered yet", "inventory-add-local",
                list(ADD_LOCAL_NO_HOST_CHOICES), "add:host",
            )
            return
        self._pop_to_list()
        if target == ADD_TYPE_CODEX:
            self._open_codex_add()
        elif target == ADD_TYPE_CLAUDE:
            self._push("list", "claude")
        elif target == ADD_TYPE_LOCAL:
            self._open_local_form("add-seat")
        elif target == "add:host":
            self._open_local_form("add-host")
        elif target == "hosts":
            self._push("list", "hosts")

    @staticmethod
    def _buffer_text(field_item: LocalField, value: Any) -> str:
        if value is None:
            return ""
        if field_item.kind in {"json", "optional-json"}:
            return json.dumps(value, sort_keys=True)
        return str(value)

    def _open_local_form(
        self, action: str, target: str | None = None, *, preset_host: str | None = None
    ) -> None:
        kind = "host" if action.endswith("host") else "seat"
        fields = HOST_FORM_FIELDS if kind == "host" else SEAT_FORM_FIELDS
        editing = action.startswith("edit-")
        report = self._local_report()
        if editing:
            fields = tuple(f for f in fields if f.option not in EDIT_EXCLUDED_OPTIONS)
            entry = report["hosts" if kind == "host" else "seats"].get(target, {})
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
                hosts = sorted(report["hosts"])
                host = preset_host or (hosts[0] if hosts else "")
                buffers["host"] = host
                groups = self._host_groups(host)
                buffers["resource_group"] = groups[0] if groups else ""
        self.local_form_action = action
        self.local_form_target = target
        self.local_form_fields = fields
        self.local_form_buffers = buffers
        self.local_form_index = 0
        screen = "hosts" if kind == "host" else "local-seats"
        if editing and target is not None:
            self.local_selected[screen] = target
        self._push("local-form", screen)

    def _open_form_choice(self, field_item: LocalField) -> None:
        option = field_item.option
        current = self.local_form_buffers.get(option, "")
        if option == "lifecycle":
            values: list[str] = list(LOCAL_LIFECYCLE_ORDER)
        elif option == "mode":
            values = list(LOCAL_MODE_ORDER)
        elif option == "host":
            values = sorted(self._local_report()["hosts"])
            if not values:
                self._error("Add a local host first; a local seat must name a registered host")
                return
        elif option == "resource_group":
            values = self._host_groups(self.local_form_buffers.get("host", ""))
            if not values:
                self.input_buffer = current
                self._push("local-form-input")
                return
        else:
            values = []
        items = [
            (f"{value}{' (current)' if value == current else ''}", value) for value in values
        ]
        self._open_choice(
            field_item.label, self.local_form_action, f"form:{option}", items, current
        )

    def _local_form_values(self) -> dict[str, Any]:
        """Parse form buffers; ``None`` marks a cleared optional field."""
        values: dict[str, Any] = {}
        for field_item in self.local_form_fields:
            if field_item.kind in {"submit", "id"}:
                continue
            raw = self.local_form_buffers.get(field_item.option, "").strip()
            if field_item.kind == "optional-text":
                values[field_item.option] = raw or None
            elif field_item.kind in {"int", "optional-int"}:
                if not raw and field_item.kind == "optional-int":
                    values[field_item.option] = None
                    continue
                try:
                    values[field_item.option] = int(raw)
                except ValueError:
                    raise ValueError(f"{field_item.option} must be an integer") from None
            elif field_item.kind in {"json", "optional-json"}:
                if not raw:
                    if field_item.kind == "optional-json":
                        values[field_item.option] = None
                        continue
                    raise ValueError(f"{field_item.option} requires a JSON object")
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{field_item.option} is not valid JSON: {exc}") from None
                if not isinstance(parsed, dict):
                    raise ValueError(f"{field_item.option} must be a JSON object")
                values[field_item.option] = parsed
            else:
                values[field_item.option] = raw
        return values

    def _apply_local_form(self) -> None:
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
                    self._error("No changes to save; edit a field first")
                    return
                preview = (
                    local.preview_edit_host(self.local_form_target, changes)
                    if kind == "host"
                    else local.preview_edit_seat(self.local_form_target, changes)
                )
        except (ManagerError, ValueError) as exc:
            self._error(f"Cannot save: {exc}")
            return
        target = preview.target_id
        if action == "add-host":
            done = f"✓ Added local host {target} · next: add a seat on it, then set lifecycle and mode"
        elif action == "add-seat":
            done = (
                f"✓ Added local seat {target} · its effective mode is the lower of the "
                "global and seat modes"
            )
        else:
            done = f"✓ Saved {target}"
        if not self._commit_local_preview(preview, done):
            return
        message, kind_text = self.message, self.message_kind
        if action.startswith("add-"):
            self._pop_to_list()
            screen = "hosts" if kind == "host" else "local-seats"
            self.local_selected[screen] = target
            if self.screen == "inventory" and kind == "seat":
                self.inventory_selected = f"local-seat:{target}"
            self._on_add[self.screen] = False
        else:
            self._back()
        self.message, self.message_kind = message, kind_text

    def _local_preview_material(self) -> list[str]:
        preview = self.local_preview
        if preview is None:
            return ["Local preview expired; select the action again."]
        before = json.dumps(_thaw(preview.before), indent=2, sort_keys=True).splitlines(keepends=True)
        after = json.dumps(_thaw(preview.after), indent=2, sort_keys=True).splitlines(keepends=True)
        diff = [
            line.rstrip("\n")
            for line in difflib.unified_diff(
                before, after, fromfile="settings.before", tofile="settings.after"
            )
        ]
        return [*[f"! {warning}" for warning in preview.warnings], *(diff or ["No settings bytes would change."])]

    # -- claude account -------------------------------------------------------------------
    def _choose_claude_action(self, action: str) -> None:
        report = self._claude_report(force=True)
        if report is None:
            self._error(f"Claude account unavailable: {self.claude_error}")
            return
        if action == "claude-mode":
            current = report.get("configured_mode")
            items = [
                (human("claude_mode", mode) + (" (current)" if mode == current else ""), mode)
                for mode in CLAUDE_MODE_ORDER
            ]
            self._open_choice("Claude mode", CLAUDE_TARGET, "claude-mode", items, current, screen="claude")
        elif action == "claude-active-refresh":
            enabled = not bool(report.get("active_usage_refresh"))
            try:
                preview = self._claude().preview(active_usage_refresh=enabled)
            except (ManagerError, ValueError) as exc:
                self._error(exc)
                return
            self._commit_claude_preview(
                preview, f"✓ Active /usage refresh → {human('bool', enabled)}"
            )
        elif action == "claude-tier":
            tier = report.get("subscription_tier", {})
            current = tier.get("configured") if isinstance(tier, Mapping) else None
            items: list[tuple[str, Any]] = [
                (f"{label}{' (current)' if label == current else ''}", label)
                for label in CLAUDE_SUBSCRIPTION_TIERS
            ]
            items.append((CLAUDE_TIER_CLEAR_LABEL + (" (current)" if current is None else ""), None))
            self._open_choice("Claude subscription tier", CLAUDE_TARGET, "claude-tier", items, current,
                              screen="claude")
        elif action in {"claude-small", "claude-big"}:
            self.claude_input_field = "small" if action == "claude-small" else "big"
            headroom = report.get("headroom", {})
            headroom = headroom if isinstance(headroom, Mapping) else {}
            current = headroom.get(f"{self.claude_input_field}_min_pct")
            self.input_buffer = "" if current is None else f"{current:g}"
            self._push("claude-input", "claude")

    def _apply_claude_headroom(self) -> None:
        field_name = self.claude_input_field
        raw = self.input_buffer.strip()
        try:
            number = float(raw)
            value: Any = int(number) if number.is_integer() else number
            claude = self._claude()
            preview = (
                claude.preview_headroom(small=value)
                if field_name == "small"
                else claude.preview_headroom(big=value)
            )
        except (ManagerError, ValueError, AttributeError) as exc:
            self._error(f"Invalid {CLAUDE_HEADROOM_FIELDS[field_name][1].lower()}: {exc}")
            return
        label = CLAUDE_HEADROOM_FIELDS[field_name][1]
        if self._commit_claude_preview(preview, f"✓ Claude {label.lower()} → {value}% left"):
            self._back()

    def _commit_claude_preview(self, preview: Any, done: str) -> bool:
        try:
            result = self._claude().commit(preview, from_current_user_request=True)
        except ManagerError as exc:
            self._error(exc)
            return False
        # Keep the dashboard and the refresher on the committed revision.
        if self.manager is not None:
            self.manager.reload()
        self._invalidate()
        text = done if result.changed else "No change was needed"
        if result.enabling:
            text += " · Claude may now take routed work"
        if result.warnings:
            text += " · " + "; ".join(str(warning) for warning in result.warnings)
        self._toast(text, "ok" if result.changed else "info")
        return True

    # -- global settings ------------------------------------------------------------------
    def _open_global_settings(self) -> None:
        if self.manager is None:
            return
        try:
            self.global_values = self.manager.global_settings_report()
        except ManagerError as exc:
            self._error(exc)
            return
        self.global_index = 0
        self._push("global-settings")

    def _global_choices(self) -> tuple[Any, ...]:
        key = tuple(DEFAULTS)[self.global_index]
        if isinstance(DEFAULTS[key], bool):
            return (True, False)
        return tuple(sorted(CHOICES[key]))

    @staticmethod
    def _global_text(key: str, value: Any) -> str:
        if key == "execution.inline_max_minutes":
            return f"{value} min"
        return human(key, value)

    def _handle_global_settings(self, key: str) -> UIExit | None:
        fields = tuple(DEFAULTS)
        if key in {"down", "j"}:
            self.global_index = (self.global_index + 1) % len(fields)
        elif key in {"up", "k"}:
            self.global_index = (self.global_index - 1) % len(fields)
        elif key == "pagedown":
            self.global_index = min(len(fields) - 1, self.global_index + self._page_size)
        elif key == "pageup":
            self.global_index = max(0, self.global_index - self._page_size)
        elif key == "esc":
            self._back()
        elif key == "enter":
            selected = fields[self.global_index]
            if selected == "execution.inline_max_minutes":
                self.input_buffer = str(self.global_values.get(selected, ""))
                self._push("global-input")
            else:
                choices = self._global_choices()
                current = self.global_values.get(selected)
                self.global_choice_index = choices.index(current) if current in choices else 0
                self._push("global-choice")
        return None

    def _apply_global_value(self, value: Any) -> None:
        assert self.manager is not None
        key = tuple(DEFAULTS)[self.global_index]
        try:
            self.global_preview = self.manager.preview_global_settings({key: value})
            changed = self.manager.commit_global_settings(self.global_preview)
            self.global_values = self.manager.global_settings_report()
        except ManagerError as exc:
            self._error(exc)
            return
        finally:
            self.global_preview = None
        self._invalidate()
        self._back_to("global-settings")
        if changed:
            self._toast(f"✓ {GLOBAL_LABELS.get(key, key)} → {self._global_text(key, self.global_values.get(key))}")
        else:
            self._toast("No change was needed", "info")

    # -- painting -------------------------------------------------------------------------
    # Parts are ``(text, role)``; a role written ``sel:<role>`` paints that
    # part on the selected-row background. Untrusted text is sanitized here,
    # so a label can never inject an escape sequence.
    def _paint(self, text: str, role: str | None) -> str:
        if not self.color or not text:
            return text
        codes = ""
        if role and role.startswith("sel:"):
            codes = ANSI["selected"]
            role = role[4:]
        if role in {"heading", "title"}:
            codes += ANSI["bold"]
        if role and role in ANSI:
            codes += ANSI[role]
        return f"{codes}{text}{ANSI['reset']}" if codes else text

    def _render_line(self, line: Line, width: int) -> str:
        remaining = max(0, width)
        clipped: list[tuple[str, str | None]] = []
        for raw, role in line.parts:
            if remaining <= 0:
                break
            text = clip(clean_text(raw), remaining)
            if not text:
                continue
            clipped.append((text, role))
            remaining -= display_width(text)
        if line.bg:
            if remaining > 0:
                clipped.append((" " * remaining, None))
            clipped = [(text, f"sel:{role or ''}") for text, role in clipped]
        else:
            # Trim trailing blanks before painting so plain and coloured match.
            while clipped and not clipped[-1][0].rstrip() and not str(clipped[-1][1] or "").startswith("sel:"):
                clipped.pop()
            if clipped and not str(clipped[-1][1] or "").startswith("sel:"):
                clipped[-1] = (clipped[-1][0].rstrip(), clipped[-1][1])
        return "".join(self._paint(text, role) for text, role in clipped)

    @staticmethod
    def _plain(line: Line) -> str:
        return "".join(clean_text(text) for text, _role in line.parts)

    @staticmethod
    def _cell(text: Any, width: int, role: str | None = None, *, right: bool = False) -> Parts:
        value = clip(clean_text(text), width)
        pad = " " * max(0, width - display_width(value))
        return [(pad, None), (value, role)] if right else [(value, role), (pad, None)]

    @staticmethod
    def _section(title: str, width: int, right: str = "") -> Line:
        """``── TITLE ──────── right ──`` exactly ``width`` cells wide."""
        head = f"── {title.upper()} "
        tail = f" {right} ──" if right else ""
        fill = width - display_width(head) - display_width(tail)
        if fill < 2:
            return Line([("── ", "muted"), (title.upper(), "heading")])
        return Line([
            ("── ", "muted"), (title.upper(), "heading"), (" " + "─" * fill, "muted"),
            (tail, "muted"),
        ])

    def _kv(self, label: str, value: Any, width: int, role: str | None = None,
            *, label_width: int = 8) -> list[Line]:
        """A muted label column and a wrapped value."""
        text = clean_text(value)
        if width < 28:
            return [Line([(piece, role)]) for piece in self._wrap(f"{label} {text}", width)]
        label_width = max(label_width, display_width(label) + 1) if label else label_width
        value_width = max(1, width - label_width)
        pieces = self._wrap(text, value_width) or [""]
        return [
            Line([((label if index == 0 else "").ljust(label_width), "muted"), (piece, role)])
            for index, piece in enumerate(pieces)
        ]

    @staticmethod
    def _wrap(value: Any, width: int) -> list[str]:
        """Wrap prose on word boundaries, falling back to display-cell chunks."""
        width = max(1, width)
        output: list[str] = []
        current = ""
        for word in clean_text(value).split(" "):
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

    # -- frame ------------------------------------------------------------------------------
    def render(self, width: int, height: int, overview: str) -> str:
        width, height = max(1, width), max(1, height)
        self._too_small = height < 6 or width < 24
        if self._too_small:
            lines = [
                Line([("codex-seats", "heading")]),
                Line([("Terminal too small", "warn")]),
                Line([("Resize to at least 24×6", None)]),
                Line([("q quit", "muted")]),
            ]
            return "\n".join(self._render_line(line, width) for line in lines[:height])
        if self.view == "overview":
            # dashboard() already sanitizes, bounds and paints its own output;
            # re-sanitizing would turn its escapes into visible text.
            body = overview.splitlines()[: height - 1]
            footer = self._render_line(Line(footer_parts(KEYMAP["overview"], width)), width)
            return "\n".join([*body, footer])
        lines = self._frame(width, height)
        return "\n".join(self._render_line(line, width) for line in lines[:height])

    def _frame(self, width: int, height: int) -> list[Line]:
        view = self.view
        if view in DIALOG_VIEWS:
            base = self._view_lines(self.confirm_return_view, width, height)
            return self._overlay(base, self._dialog_lines(width, height), width, height)
        return self._view_lines(view, width, height)

    def _view_lines(self, view: str, width: int, height: int) -> list[Line]:
        if view == "help":
            return self._help_lines(width, height)
        strip = self._strip(width)
        spare = height - len(strip) - 1
        top = [*strip, self._tabs(width), Line([])] if spare >= 12 else (
            [*strip, self._tabs(width)] if spare >= 8 else list(strip)
        )
        body_height = max(1, height - len(top) - 1)
        if view == "list":
            body = self._list_body(width, body_height)
        elif view in {"global-settings"}:
            body = self._global_body(width, body_height)
        elif view in {"actions", "local-actions"}:
            body = self._menu_body(view, width, body_height)
        elif view in {"local-choice", "global-choice", "add-presets", "capacity-presets"}:
            body = self._choice_body(view, width, body_height)
        elif view in {"settings", "add-form", "local-form"}:
            body = self._form_body(view, width, body_height)
        else:
            body = self._input_body(view, width, body_height)
        body = body[:body_height]
        body += [Line([])] * (body_height - len(body))
        return [*top, *body, self._footer_line(view, width)][:height]

    # -- summary strip and footer ------------------------------------------------------
    def _strip(self, width: int) -> list[Line]:
        """The dashboard's live header with the manager's capacity summary."""
        from .display import header_parts, live_header

        # One summary rule for the dashboard header, `capacity` and this strip.
        pool = pool_view.summary(self._pool())
        refresher = getattr(self.manager, "refresher", None)
        snapshot_reader = getattr(refresher, "snapshot", None)
        try:
            snapshot = snapshot_reader() if callable(snapshot_reader) else None
        except Exception:  # a closing refresher must not break the frame
            snapshot = None
        live, freshness = live_header(snapshot, current=time.time())
        ready, total = pool.get("ready", 0), pool.get("total", 0)
        nxt = (pool.get("next") or {}).get("small")
        next_seat = nxt.get("seat") if isinstance(nxt, Mapping) else None
        items: list[tuple[int, Parts]] = [
            (0, [(f"Ready {ready}/{total}", "ok" if ready == total else "warn")]),
            (1, [(f"Running {pool.get('running', 0)}", "live" if pool.get("running") else None)]),
            (4, [(f"Queued {pool.get('queued', 0)}", None)]),
        ]
        if pool.get("waiting_capacity"):
            items.append((3, [(f"Waiting {pool['waiting_capacity']}", "warn")]))
        items.append((2, [("Next → ", "muted"), (next_seat or "none", "accent" if next_seat else "muted")]))
        if pool.get("banked_resets") is not None:
            items.append((5, [(f"Resets {pool['banked_resets']}", None)]))
        if self.pool_error:
            items.append((1, [("usage unavailable", "warn")]))

        def summary(kept: list[tuple[int, Parts]]) -> Parts:
            parts: Parts = []
            for _priority, item in kept:
                if parts:
                    parts.append((" · ", "muted"))
                parts.extend(item)
            return parts

        kept = list(items)
        while len(kept) > 1 and sum(display_width(str(t)) for t, _ in summary(kept)) > width:
            drop = max(range(len(kept)), key=lambda index: kept[index][0])
            kept.pop(drop)
        return [Line(parts) for parts in header_parts(live, summary(kept), freshness, width)]

    def _tabs(self, width: int) -> Line:
        """Where Tab goes: every screen with the current one highlighted."""
        parts: Parts = []
        for screen in SCREENS:
            if parts:
                parts.append(("  ", None))
            title = SCREEN_TITLES[screen]
            parts.append((f"▸ {title}", "heading") if screen == self.screen else (title, "muted"))
        if sum(display_width(text) for text, _role in parts) <= width:
            return Line(parts)
        following = SCREENS[(SCREENS.index(self.screen) + 1) % len(SCREENS)]
        return Line([(f"▸ {SCREEN_TITLES[self.screen]}", "heading"),
                     (f"  Tab → {SCREEN_TITLES[following]}", "muted")])

    def _footer_keys(self, view: str) -> tuple[Key, ...]:
        if view in TEXT_VIEWS:
            return KEYMAP["input"]
        if view == "list":
            keys: tuple[Key, ...] = KEYMAP[self.screen]
            caps = _GLOBAL_FOOTER if self.screen != "claude" else tuple(c for c in _GLOBAL_FOOTER if c != "/")
        else:
            keys = KEYMAP["global"] if view == "global-settings" else (
                KEYMAP["form"] if view in {"settings", "add-form", "local-form"} else KEYMAP["menu"]
            )
            caps = ("?", "Esc")
        by_cap = {key.cap: key for key in GLOBAL_KEYS}
        return (*keys, *(by_cap[cap] for cap in caps))

    def _footer_line(self, view: str, width: int) -> Line:
        if self.message:
            text = self.message
            if self.message_kind == "error":
                return Line([(text, "bad")])
            head, _sep, tail = text.partition(" · ")
            parts: Parts = [(head, "ok" if self.message_kind == "ok" else "accent")]
            if tail:
                parts.append(("  ·  " + tail.replace(" · ", "  ·  "), "muted"))
            return Line(parts)
        if self.filtering:
            return Line([("/ ", "accent"), (self.filter, None), ("▏", "accent"),
                         ("   Enter keep  Esc clear", "muted")])
        esc = "overview" if view == "list" and not self._stack else "back"
        return Line(footer_parts(self._footer_keys(view), width, esc=esc))

    # -- seat table ------------------------------------------------------------------------
    def _name(self, entry: Mapping[str, Any]) -> tuple[str, str]:
        """``(name, plan)`` for one entry, as the dashboard names it.

        Local seats are named by model and host; when two seats share both,
        the seat id is added so the rows stay distinguishable.
        """
        if entry.get("provider") == "local":
            seat_id = str(entry["seat"])
            name = self._local_name(seat_id, entry)
            twins = [
                other for other in self._local_report().get("seats", {})
                if other != seat_id and self._local_name(other, {}) == name
            ]
            return name, seat_id if twins else ""
        return str(entry.get("seat")), str(entry.get("plan") or "")

    def _local_name(self, seat_id: str, entry: Mapping[str, Any]) -> str:
        config = self._local_config(seat_id)
        model = config.get("model_profile") or entry.get("plan") or "?"
        return f"{model} on {config.get('host') or '?'}"

    @staticmethod
    def _badge(entry: Mapping[str, Any]) -> str:
        provider = entry.get("provider")
        if provider == "claude":
            return "C"
        if provider == "local":
            return "L"
        return "P" if entry.get("role") == "primary" else "W"

    def _chip(self, entry: Mapping[str, Any]) -> Any:
        provider = entry.get("provider")
        task = (entry.get("task") or {}).get("label") if entry.get("busy") else None
        left = entry.get("left") or {}
        exhausted = next(
            (short for short in ("7d", "5h")
             if isinstance(left.get(short), (int, float)) and left[short] <= 0),
            None,
        )
        stale = not entry.get("fresh") and bool(entry.get("windows"))
        if provider == "local":
            observed = entry.get("observed_at")
            age = ago(observed).replace(" ago", "") if isinstance(observed, (int, float)) else None
            return status_chip("local", entry.get("status"), task=task, age=age)
        if provider == "claude":
            return status_chip("claude", entry.get("status"), mode=entry.get("mode"), task=task,
                               exhausted=exhausted, stale=stale)
        return status_chip("codex", entry.get("status"), lifecycle=entry.get("lifecycle"),
                           task=task, exhausted=exhausted, stale=stale)

    @staticmethod
    def _binding_reset(entry: Mapping[str, Any]) -> Any:
        windows = [
            w for w in entry.get("windows") or []
            if isinstance(w.get("remaining_pct"), (int, float)) and w.get("resets_at")
        ]
        if not windows:
            return None
        return min(windows, key=lambda w: w["remaining_pct"])["resets_at"]

    def _layout(self, width: int, rows: list[Row]) -> dict[str, Any] | None:
        """Column widths for one-line rows, or ``None`` for two-line rows."""
        if width < 60:
            return None
        names = [
            display_width(" ".join(part for part in self._name(row.entry) if part))
            for row in rows if row.kind in {"codex", "claude", "local"}
        ]
        name_width = min(max(max(names, default=10) + 1, 14), 22)
        bars = (8, 6, 5, 4) if width >= 100 else (6, 5, 4)
        for reset in (True, False):
            for bar in bars:
                fixed = 4 + 2 * (bar + 5) + 4 + (8 if reset else 0)
                status = width - fixed - name_width
                if status >= 13:
                    return {"name": name_width, "bar": bar, "reset": reset, "status": status}
        name_width = max(12, width - (4 + 18 + 4) - 12)
        return {"name": name_width, "bar": 4, "reset": False,
                "status": max(8, width - 26 - name_width)}

    def _column_header(self, layout: dict[str, Any]) -> Line:
        bar = layout["bar"] + 5
        parts: Parts = [("    ", None), *self._cell("SEAT", layout["name"], "muted"),
                        *self._cell("5H LEFT", bar + 2, "muted"), *self._cell("7D LEFT", bar + 2, "muted")]
        if layout["reset"]:
            parts += self._cell("RESET", 8, "muted")
        parts.append(("STATUS", "muted"))
        return Line(parts)

    def _entry_lines(self, entry: Mapping[str, Any], layout: dict[str, Any] | None,
                     width: int, selected: bool) -> list[Line]:
        chip = self._chip(entry)
        name, plan = self._name(entry)
        marker: Parts = [(MARKER + " ", "accent")] if selected else [("  ", None)]
        badge = self._badge(entry)
        left = entry.get("left") or {}
        fresh = bool(entry.get("fresh"))
        if layout is not None:
            name_cell = self._name_cell(name, plan, layout["name"])
            parts: Parts = [*marker, (badge + " ", None), *name_cell]
            for short in ("5h", "7d"):
                parts += usage_bar(left.get(short), layout["bar"], fresh=fresh)
                parts.append(("  ", None))
            if layout["reset"]:
                reset = self._binding_reset(entry)
                parts += self._cell(countdown(reset) if reset else "—", 8, "muted")
            parts += self._cell(chip.text, layout["status"], chip.role)
            return [Line(parts, "selected" if selected else None)]
        # Two lines per seat: identity and status, then the usage bars.
        narrow_marker: Parts = [(MARKER, "accent")] if selected else [(" ", None)]
        status_width = max(8, min(18, width // 3))
        name_width = max(6, width - 3 - status_width - 1)
        first: Parts = [*narrow_marker, (badge + " ", None),
                        *self._name_cell(name, plan, name_width), (" ", None),
                        *self._cell(chip.text, status_width, chip.role)]
        lines = [Line(first, "selected" if selected else None)]
        if entry.get("provider") != "local":
            second: Parts = [("   ", None)]
            for index, short in enumerate(("5h", "7d")):
                if index:
                    second.append(("   ", None))
                second += [(f"{short} ", "muted"), *usage_bar(left.get(short), 5, fresh=fresh)]
            lines.append(Line(second, "selected" if selected else None))
        return lines

    @staticmethod
    def _name_cell(name: str, plan: str, width: int) -> Parts:
        """Seat name with its plan in muted text, padded to ``width`` cells."""
        name_text = clip(clean_text(name), max(1, width - 1))
        used = display_width(name_text)
        plan_text = ""
        if plan and width - used - 2 > 2:
            plan_text = clip(" " + clean_text(plan), width - used - 1)
        used += display_width(plan_text)
        return [(name_text, None), (plan_text, "muted"), (" " * max(0, width - used), None)]

    def _row_lines(self, row: Row, layout: dict[str, Any] | None, width: int,
                   selected: bool) -> list[Line]:
        if row.kind in {"codex", "claude", "local"}:
            return self._entry_lines(row.entry, layout, width, selected)
        marker: Parts = [(MARKER + " ", "accent")] if selected else [("  ", None)]
        bg = "selected" if selected else None
        if row.kind == "add":
            return [Line([*marker, ("+ ", "ok"), (row.title, "ok"), ("   a", "accent")], bg)]
        if row.kind == "host":
            return [self._host_line(row.entry, width, marker, bg)]
        if row.kind == "setting":
            value = self._claude_setting_value(str(row.entry["seat"]))
            label_width = min(26, max(14, width // 2))
            return [Line([*marker, *self._cell(row.title, label_width), (value, "accent")], bg)]
        head, _sep, tail = row.title.partition(" · ")
        parts: Parts = [(head, "heading")]
        if tail:
            parts.append((" · " + tail, "muted"))
        return [Line(parts)]

    def _host_line(self, entry: Mapping[str, Any], width: int, marker: Parts, bg: str | None) -> Line:
        report = self._local_report()
        host = report.get("hosts", {}).get(entry["seat"], {})
        status = host.get("status") if isinstance(host, Mapping) else None
        status = status if isinstance(status, Mapping) else {}
        observed = status.get("observed_at")
        age = ago(observed).replace(" ago", "") if isinstance(observed, (int, float)) else None
        chip = status_chip("local", status.get("key"), age=age)
        seats = host.get("seats") if isinstance(host, Mapping) else None
        name_width = min(28, max(14, width // 3))
        status_width = min(22, max(10, width // 4))
        parts: Parts = [*marker, ("H ", None),
                        *self._name_cell(str(entry["seat"]), str(entry.get("label") or ""), name_width),
                        *self._cell(human("lifecycle", entry.get("lifecycle")), 10, "muted"),
                        *self._cell(chip.text, status_width, chip.role),
                        (f"{len(seats or [])} seat{'s' if len(seats or []) != 1 else ''}", "muted")]
        return Line(parts, bg)

    def _claude_setting_value(self, action: str) -> str:
        report = self._claude_report()
        if report is None:
            return "unavailable"
        headroom = report.get("headroom") if isinstance(report.get("headroom"), Mapping) else {}
        tier = report.get("subscription_tier") if isinstance(report.get("subscription_tier"), Mapping) else {}
        if action == "claude-mode":
            return human("claude_mode", report.get("configured_mode") or "off")
        if action == "claude-tier":
            return str(tier.get("label") or "Unknown")
        if action == "claude-small":
            return f"{headroom.get('small_min_pct')}% left"
        if action == "claude-big":
            return f"{headroom.get('big_min_pct')}% left"
        return human("bool", bool(report.get("active_usage_refresh")))

    def _table(
        self, rows: list[Row], selected: Row | None, width: int
    ) -> tuple[list[Line], int, list[str | None]]:
        """Table lines, the selected row's first line, and each line's seat row."""
        layout = self._layout(width, rows) if self.screen not in {"hosts", "claude"} else None
        lines: list[Line] = []
        owners: list[str | None] = []
        if layout is not None:
            lines.append(self._column_header(layout))
            owners.append(None)
        selected_at = 0
        for index, row in enumerate(rows):
            if index and (row.kind == "group" or (row.kind == "add" and rows[index - 1].kind != "group")):
                lines.append(Line([]))
                owners.append(None)
            chosen = selected is not None and row.key == selected.key
            if chosen:
                selected_at = len(lines)
            row_lines = self._row_lines(row, layout, width, chosen)
            lines.extend(row_lines)
            owners.extend([row.key if row.kind not in {"group", "add"} else None] * len(row_lines))
        if self.filter and not any(row.kind not in {"group", "add"} for row in rows):
            lines.insert(len(lines) - 1, Line([("  nothing matches the filter · Esc clears it", "muted")]))
            owners.insert(len(owners) - 1, None)
        return lines, selected_at, owners

    @staticmethod
    def _window(
        lines: list[Line], selected_at: int, slots: int, owners: list[str | None] | None = None
    ) -> list[Line]:
        """``slots`` lines keeping the selection visible, with a "more" marker.

        The marker counts hidden seats (rows), not wrapped lines.
        """
        if slots <= 0:
            return []
        if len(lines) <= slots:
            return lines
        start = min(max(0, selected_at - slots // 2), len(lines) - slots)
        visible = lines[start:start + slots]
        if start + slots < len(lines) and slots > 2 and selected_at < start + slots - 1:
            shown = set(owners[start:start + slots - 1]) if owners else set()
            hidden_lines = owners[start + slots - 1:] if owners else []
            hidden = len({key for key in hidden_lines if key is not None and key not in shown})
            if owners is None:
                hidden = len(lines) - start - slots + 1
            if hidden:
                visible[-1] = Line([(f"  … {hidden} more ↓", "muted")])
        return visible

    def _list_body(self, width: int, height: int) -> list[Line]:
        rows = self._rows()
        selected = self._selected_row(rows)
        wide = width >= 110
        right = max(40, min(60, int(width * 0.38))) if wide else width
        left = width - right - 3 if wide else width
        table, selected_at, owners = self._table(rows, selected, left)
        if wide:
            detail = self._detail(selected, right, compact=False)
            ongoing = self._ongoing(width)
            split = height
            if ongoing and height - len(ongoing) - 1 >= min(len(table), 8):
                split = height - len(ongoing) - 1
            else:
                ongoing = []
            self._detail_page_size = max(1, split)
            self.detail_scroll = min(max(0, self.detail_scroll), max(0, len(detail) - split))
            shown = detail[self.detail_scroll:self.detail_scroll + split]
            visible = self._window(table, selected_at, split, owners)
            body: list[Line] = []
            for index in range(split):
                row = visible[index] if index < len(visible) else Line([])
                parts = list(row.parts)
                used = sum(display_width(clip(clean_text(t), 10_000)) for t, _ in parts)
                if row.bg:
                    parts = [(text, f"sel:{role or ''}") for text, role in parts]
                    parts.append((" " * max(0, left - used), "sel:"))
                else:
                    parts.append((" " * max(0, left - used), None))
                parts = self._fit(parts, left)
                parts.append((" │ ", "muted"))
                if index < len(shown):
                    parts.extend(shown[index].parts)
                body.append(Line(parts))
            if ongoing:
                body.append(Line([]))
                body.extend(ongoing)
            return body
        detail = self._detail(selected, width, compact=True)
        if height >= 10:
            detail_height = min(len(detail), max(4, height - len(table) - 1))
            table_height = height - detail_height - 1
            if table_height < min(len(table), 4):
                table_height = min(len(table), max(3, height // 2))
                detail_height = height - table_height - 1
            gap = [Line([])]
        else:
            table_height = min(len(table), max(2, height // 2))
            detail_height = height - table_height
            gap = []
        self._detail_page_size = max(1, detail_height - 1)
        body = self._window(table, selected_at, table_height, owners)
        if detail_height > 0 and detail:
            title, rest = detail[0], detail[1:]
            slots = detail_height - 1
            self.detail_scroll = min(max(0, self.detail_scroll), max(0, len(rest) - slots))
            more = "PgDn more" if self.detail_scroll + slots < len(rest) else (
                "PgUp back" if self.detail_scroll else ""
            )
            body += gap
            body.append(self._section(self._plain(title), width, more))
            body += rest[self.detail_scroll:self.detail_scroll + slots]
        return body

    @staticmethod
    def _fit(parts: Parts, width: int) -> Parts:
        """Clip parts to exactly ``width`` cells (keeping their roles)."""
        out: Parts = []
        remaining = width
        for text, role in parts:
            if remaining <= 0:
                break
            piece = clip(clean_text(text), remaining)
            out.append((piece, role))
            remaining -= display_width(piece)
        return out

    def _ongoing(self, width: int) -> list[Line]:
        jobs = [job for job in self._pool().get("jobs", []) if job.get("state") == "running"]
        busy = [
            e for e in self._entries()
            if e.get("busy") and not any(
                job.get("request_id") and job.get("request_id") == (e.get("task") or {}).get("job_id")
                for job in jobs
            )
        ]
        if not jobs and not busy:
            return []
        count = len(jobs) + len(busy)
        lines = [self._section("Ongoing", width, f"{count} running")]
        for job in jobs:
            elapsed = job.get("elapsed_seconds")
            minutes = f"{int(elapsed // 60)}m" if isinstance(elapsed, (int, float)) else ""
            where = " · ".join(str(part) for part in (job.get("seat"), job.get("size"), minutes) if part)
            lines.append(Line([("● ", "live"), (clip(clean_text(job.get("label") or "job"), 40), None),
                               ("   " + where, "muted")]))
        for entry in busy:
            lines.append(Line([("● ", "live"), ((entry.get("task") or {}).get("label") or "work", None),
                               (f"   {entry['seat']}", "muted")]))
        return lines

    # -- detail pane ---------------------------------------------------------------------------
    def _detail(self, row: Row | None, width: int, *, compact: bool) -> list[Line]:
        """Detail for the selected row; ``compact`` starts with a title line."""
        if row is None:
            text = "No seats yet · press a to add one" if self.screen != "hosts" else (
                "No local hosts yet · press a to add one"
            )
            return [Line([(text, "muted")])] if not compact else [
                Line([("Nothing selected", None)]), Line([(text, "muted")])
            ]
        if row.kind == "codex":
            return self._codex_detail(row.entry, width, compact=compact)
        if row.kind == "claude":
            return self._claude_detail(row.entry, width, compact=compact)
        if row.kind == "local":
            return self._local_seat_detail(str(row.entry["seat"]), width, compact=compact)
        if row.kind == "host":
            return self._host_detail(str(row.entry["seat"]), width, compact=compact)
        if row.kind == "setting":
            return self._claude_detail(self._entry("claude", "claude") or {}, width, compact=compact,
                                       setting=str(row.entry["seat"]))
        return self._add_detail(row, width, compact=compact)

    def _title(self, parts: list[str]) -> Line:
        return Line([(" · ".join(part for part in parts if part), None)])

    def _status_sentence(self, entry: Mapping[str, Any], chip: Any) -> str:
        label = chip.label
        lifecycle = entry.get("lifecycle")
        fits = entry.get("fits") or []
        if entry.get("busy"):
            return f"Working on {label}"
        if label == "paused":
            return "Paused · no new work is routed here"
        if label == "retired":
            return "Retired · kept for history"
        if label == "draining":
            return "Draining · finishes its task, then pauses"
        if label.startswith("capped"):
            short = label.split(" ", 1)[1] if " " in label else ""
            names = {"5h": "5-hour", "7d": "7-day"}
            return f"Capped by the {names.get(short, short)} window" if short else "Capped"
        if label == "needs sign-in":
            return "Needs sign-in · l signs in again"
        if label == "stale":
            return "Usage is stale · r refreshes it"
        if label == "not probed":
            return "Not probed yet · r refreshes usage"
        if label == "off":
            return "Off · no work is routed here"
        if label == "idle" and lifecycle == "active":
            if fits == ["small", "big"] or set(fits) == {"small", "big"}:
                return "Idle · fits small and big tasks"
            if fits == ["small"]:
                return "Idle · fits small tasks only"
            if entry.get("reason") and self.pool_error is None:
                return "Idle · not enough headroom for new work"
            return "Idle"
        return label[:1].upper() + label[1:]

    def _usage_lines(self, entry: Mapping[str, Any], width: int, *, compact: bool) -> list[Line]:
        windows = sorted(
            (w for w in entry.get("windows") or [] if isinstance(w, Mapping)),
            key=lambda w: float(w.get("minutes") or 0),
        )
        fresh = bool(entry.get("fresh"))
        if not windows:
            return [Line([("Usage   ", "muted"), ("not reported yet", "muted")])]
        cells = 12 if width >= 40 else 6
        pieces: list[Parts] = []
        for window in windows:
            label = window_label(window.get("name"), window.get("minutes"))
            reset = window.get("resets_at")
            when = until(reset) if reset else "—"
            pieces.append([
                (f"{label:<8}", "muted"),
                *usage_bar(window.get("remaining_pct"), cells, fresh=fresh),
                (f"  {when if compact else ('resets ' + when if reset else 'no reset time')}", "muted"),
            ])
        if compact and len(pieces) == 2 and 2 * (8 + cells + 5 + 12) + 4 <= width:
            return [Line([*pieces[0], ("    ", None), *pieces[1]])]
        return [Line(piece) for piece in pieces]

    def _action_lines(self, actions: list[Action], width: int) -> list[Line]:
        """``key Label  note``; an action that cannot run is dimmed with its reason."""
        lines = []
        for action in actions:
            key = action.key if action.key and action.key != "Enter" else "·" if not action.key else "↵"
            note = action.reason or action.hint
            role = "muted" if action.reason else None
            head: Parts = [(f"{key} ", "muted" if action.reason else "accent"), (action.label, role)]
            used = 2 + display_width(action.label)
            if not note:
                lines.append(Line(head))
            elif used + 2 + display_width(note) <= width:
                lines.append(Line([*head, ("  " + note, "muted")]))
            else:
                lines.append(Line(head))
                lines += [Line([("    " + piece, "muted")]) for piece in self._wrap(note, max(1, width - 4))]
        return lines

    def _compact_actions(self, actions: list[Action], width: int) -> list[Line]:
        """Direct keys on as few lines as fit; dimmed when they cannot run."""
        items: list[Parts] = []
        for action in actions:
            if not action.key or action.key == "Enter":
                continue
            role = "muted" if action.reason else None
            items.append([(action.key, "muted" if action.reason else "accent"),
                          (" " + (action.short or action.label), role)])
        items.append([("Enter", "muted"), (" more", "muted")])
        lines: list[Line] = []
        current: Parts = []
        used = 0
        for item in items:
            cost = sum(display_width(text) for text, _role in item) + (2 if current else 0)
            if current and used + cost > width:
                lines.append(Line(current))
                current, used = [], 0
                cost -= 2
            if current:
                current.append(("  ", None))
            current += item
            used += cost
        if current:
            lines.append(Line(current))
        return lines

    def _codex_detail(self, entry: Mapping[str, Any], width: int, *, compact: bool) -> list[Line]:
        assert self.manager is not None
        seat_id = str(entry.get("seat"))
        record = self._codex_records().get(seat_id, {})
        try:
            identity = self.manager.identity_report(seat_id)
        except (ManagerError, AttributeError, OSError, ValueError):
            identity = {}
        identity = identity if isinstance(identity, Mapping) else {}
        chip = self._chip(entry)
        role = "Primary" if entry.get("role") == "primary" else "Worker"
        plan = str(entry.get("plan") or "")
        actions = self._codex_actions(entry, menu=True)
        bank = record.get("banked_resets") if isinstance(record.get("banked_resets"), Mapping) else {}
        bank_text, bank_count = self._bank_text(bank)
        status = Line([(f"{chip.glyph} ", chip.role), (self._status_sentence(entry, chip), chip.role)])
        lines: list[Line] = []
        if compact:
            lines.append(self._title([seat_id, role, plan]))
            if bank_count:
                status.parts.append((f"  ·  {bank_text}", "muted"))
            lines.append(status)
            lines += self._usage_lines(entry, width, compact=True)
            lines += self._compact_actions(actions, width)
        else:
            lines.append(Line([(seat_id, "accent"), ("  ", None), (f"{role} · {plan}", None)]))
            lines.append(status)
            observed = entry.get("observed_at")
            lines.append(self._section("Usage", width, f"updated {ago(observed)}" if observed else ""))
            lines += self._usage_lines(entry, width, compact=False)
            lines += self._kv("Banked", bank_text, width, None if bank_count else "muted")
        lines.append(self._section("Account", width))
        email = identity.get("email")
        identity_status = clean_text(identity.get("status") or "unavailable")
        if email:
            stale = identity_status == "stale"
            lines += self._kv("Email", f"{email}{' · stale' if stale else ''}", width, "warn" if stale else None)
        else:
            reason = identity.get("reason") or "not reported by the provider"
            lines += self._kv("Email", f"unavailable · {reason}", width, "muted")
        provider = record.get("provider") if isinstance(record.get("provider"), Mapping) else {}
        health = str(provider.get("health") or entry.get("status") or "unknown")
        verified = identity.get("observed_at")
        if health in {"needs-login", "needs-auth"}:
            lines += self._kv("Sign-in", "× needs sign-in · l signs in again", width, "bad")
        elif isinstance(verified, (int, float)) and not isinstance(verified, bool):
            stale = identity_status == "stale"
            lines += self._kv("Sign-in", f"{'◐' if stale else '✓'} verified {ago(verified)}"
                              + (" · stale" if stale else ""), width, "warn" if stale else "ok")
        else:
            lines += self._kv("Sign-in", "not verified yet · r refreshes it", width, "muted")
        observed_plan = identity.get("observed_plan_type")
        plan_text = plan + (f" · observed: {human('plan_type', observed_plan)}" if observed_plan else "")
        lines += self._kv("Plan", plan_text, width)
        label = str(entry.get("label") or "")
        if label and label != seat_id:
            lines += self._kv("Label", label, width)
        lines.append(self._section("Actions", width))
        lines += self._action_lines(actions, width)
        lines.append(self._section("Details", width))
        fits = entry.get("fits") or []
        lines += self._kv("Fits", " + ".join(fits) if fits else "no new work right now", width,
                          None if fits else "muted")
        why = self._why(entry, self._capacity_report(seat_id))
        if why:
            lines += self._kv("Why", why, width, "warn" if not fits else "muted")
        if self.show_raw and entry.get("reason"):
            lines += self._kv("Raw", entry["reason"], width, "muted")
        capacity = record.get("capacity") if isinstance(record.get("capacity"), Mapping) else {}
        declared = capacity.get("relative_capacity")
        if isinstance(declared, (int, float)):
            capacity_text = f"{declared:g}x · {capacity.get('source') or 'source unknown'}"
        elif capacity:
            effective = capacity.get("effective_relative_capacity", 1)
            capacity_text = f"unverified · routing assumes {effective:g}x" if isinstance(
                effective, (int, float)) else "unverified"
        else:
            capacity_text = "unavailable"
        lines += self._kv("Capacity", capacity_text, width, None if isinstance(declared, (int, float)) else "muted")
        usage_plan = capacity.get("observed_plan_type")
        if usage_plan:
            lines += self._kv("Usage plan", human("plan_type", usage_plan), width, label_width=11)
        credits = capacity.get("credits")
        if capacity and not isinstance(credits, Mapping):
            lines += self._kv("Credits", "not reported", width, "muted")
        if isinstance(credits, Mapping):
            if credits.get("unlimited") is True:
                credit_text = "unlimited · provider reported"
            elif credits.get("balance") is not None:
                credit_text = f"balance {credits['balance']}"
            elif credits.get("has_credits") is True:
                credit_text = "available · balance unavailable"
            elif credits.get("has_credits") is False:
                credit_text = "none available"
            else:
                credit_text = "unavailable"
            lines += self._kv("Credits", credit_text, width)
        if identity.get("account_id"):
            lines += self._kv("Account ID", identity["account_id"], width)
        for credit in bank.get("credits") or []:
            if not isinstance(credit, Mapping):
                continue
            title = credit.get("title") or credit.get("reset_type") or "credit"
            expires = credit.get("expires_at")
            lines += self._kv("Reset", f"{title} · {human('bank_status', credit.get('status') or 'unknown')}"
                              + (f" · expires {day(expires)}" if expires else ""), width, "muted")
        refresh = self.manager.refresh_state() if hasattr(self.manager, "refresh_state") else None
        if refresh is not None:
            state = (
                "refreshing now" if getattr(refresh, "active", None) == seat_id
                else "queued" if getattr(refresh, "queued", None) == seat_id
                else getattr(refresh, "last_result", None)
                if getattr(refresh, "last_target", seat_id) == seat_id else None
            )
            if state:
                lines += self._kv("Refresh", state, width, "accent")
        return lines

    @staticmethod
    def _bank_text(bank: Mapping[str, Any]) -> tuple[str, int]:
        status = bank.get("status") or "unknown"
        count = bank.get("available_count")
        if status == "fresh" and isinstance(count, int):
            if count == 0:
                return "none available", 0
            expiries = [
                credit.get("expires_at") for credit in bank.get("credits") or []
                if isinstance(credit, Mapping) and isinstance(credit.get("expires_at"), (int, float))
            ]
            text = f"{count} banked reset{'s' if count != 1 else ''} available"
            if expiries:
                text += f" · expires {day(min(expiries))}"
            return text, count
        return f"{human('bank_status', status).lower()} · r refreshes the count", 0

    def _claude_detail(self, entry: Mapping[str, Any], width: int, *, compact: bool,
                       setting: str | None = None) -> list[Line]:
        report = self._claude_report()
        if report is None:
            text = f"Claude account unavailable: {self.claude_error or 'unknown error'}"
            return [Line([("Claude", None)]), *[Line([(piece, "bad")]) for piece in self._wrap(text, width)]]
        entry = entry or self._claude_skeleton() or {}
        chip = self._chip(entry) if entry else status_chip("claude", "unknown")
        tier = report.get("subscription_tier") if isinstance(report.get("subscription_tier"), Mapping) else {}
        tier_label = tier.get("label") or "tier unknown"
        status = Line([(f"{chip.glyph} ", chip.role), (self._status_sentence(entry, chip), chip.role)])
        lines: list[Line] = []
        if compact:
            lines.append(self._title(["Claude", str(tier_label)]))
            lines.append(status)
            if (report.get("mode") or "off") == "off":
                lines.append(Line([("Usage   ", "muted"), ("not read while Claude is off", "muted")]))
            else:
                lines += self._usage_lines(entry, width, compact=True)
        else:
            lines.append(Line([("claude", "accent"), ("  ", None), (f"Claude · {tier_label}", None)]))
            lines.append(status)
            usage = report.get("usage") if isinstance(report.get("usage"), Mapping) else {}
            age = usage.get("age_seconds")
            right = f"updated {ago(time.time() - age)}" if isinstance(age, (int, float)) else ""
            lines.append(self._section("Usage", width, right))
            if not usage.get("read"):
                lines += self._kv("Usage", usage.get("reason") or "not read while Claude is off", width, "muted")
            else:
                lines += self._usage_lines(entry, width, compact=False)
        if setting is not None:
            lines.append(self._section("Setting", width))
            lines += [Line([(piece, None)]) for piece in self._wrap(self._claude_setting_help(setting), width)]
        lines.append(self._section("Settings", width))
        mode = report.get("mode") or "off"
        configured = report.get("configured_mode") or "off"
        mode_text = human("claude_mode", configured)
        if mode != configured:
            mode_text += f" · effective {human('claude_mode', mode)}"
        if report.get("mode_reason"):
            mode_text += f" · {report['mode_reason']}"
        lines += self._kv("Mode", mode_text, width)
        source = clean_text(tier.get("source") or "unknown")
        lines += self._kv("Tier", f"{tier_label} · {source}" if tier.get("label") else "unknown · not configured",
                          width, None if tier.get("label") else "muted")
        if tier.get("mismatch"):
            lines += self._kv("", f"configured {tier.get('configured')} differs from detected "
                              f"{tier.get('detected')}", width, "warn")
        headroom = report.get("headroom") if isinstance(report.get("headroom"), Mapping) else {}
        lines += self._kv("Floors", f"small {headroom.get('small_min_pct')}% · big "
                          f"{headroom.get('big_min_pct')}% left", width)
        lines += self._kv("Refresh", "active /usage refresh " + human(
            "bool", bool(report.get("active_usage_refresh"))).lower(), width)
        if report.get("expires_at"):
            lines += self._kv("Expires", report["expires_at"], width)
        if self.screen == "inventory":
            lines.append(self._section("Actions", width))
            lines += self._action_lines([Action("Enter", "Open Claude settings", "open")], width)
        status_report = report.get("status")
        lines.append(self._section("Details", width))
        if not isinstance(status_report, Mapping):
            lines += self._kv("Provider", f"unavailable · {report.get('status_error') or 'no pool snapshot'}",
                              width, "muted")
        else:
            word = clean_text(status_report.get("status") or "unknown")
            until_at = status_report.get("status_until")
            lines += self._kv("Provider", word + (f" · {until(until_at)}" if isinstance(until_at, (int, float)) else ""),
                              width)
            lines += self._kv("Busy", "yes · a job is in flight" if status_report.get("busy") else "no",
                              width, "live" if status_report.get("busy") else "muted")
            capped = status_report.get("capped_models")
            if isinstance(capped, list) and capped:
                lines += self._kv("Capped", ", ".join(map(str, capped)), width, "warn")
            last_job = status_report.get("last_job_at")
            lines += self._kv("Last job", ago(last_job) if isinstance(last_job, (int, float)) else "none recorded",
                              width, None if isinstance(last_job, (int, float)) else "muted")
        for warning in report.get("warnings") or []:
            lines += self._kv("Warning", warning, width, "warn")
        return lines

    @staticmethod
    def _claude_setting_help(action: str) -> str:
        return {
            "claude-mode": "Off stops routing to Claude. Manual only and Reviews only limit what it "
            "takes; Opportunistic lets routing use it whenever it fits.",
            "claude-tier": "A display label for the Claude subscription. It does not change routing "
            "or quota limits.",
            "claude-small": "Percent of the usage window that must remain before small tasks route "
            "to Claude (0–100; not above the big-task floor).",
            "claude-big": "Percent of the usage window that must remain before big tasks route to "
            "Claude (0–100).",
            "claude-active-refresh": "Refresh Claude usage with an active /usage read. Off keeps the "
            "passive status-line observations only.",
        }.get(action, "")

    def _availability_lines(
        self,
        availability: Any,
        width: int,
        *,
        eligibility: Mapping[str, Any] | None = None,
        status: Any = None,
        jobs_error: Any = None,
    ) -> list[Line]:
        """Cached availability for one host or seat, in plain words."""
        lines: list[Line] = []
        availability = availability if isinstance(availability, Mapping) else {}
        status = status if isinstance(status, Mapping) else {}
        key = clean_text(status.get("key") or "unknown")
        observed_at = status.get("observed_at")
        if isinstance(observed_at, bool) or not isinstance(observed_at, (int, float)):
            observed_at = None
        chip = status_chip("local", key, age=ago(observed_at).replace(" ago", "") if observed_at else None)
        detail = clean_text(status.get("detail") or "")
        lines += self._kv("Status", f"{chip.text}" + (f" · {detail}" if detail else ""), width, chip.role,
                          label_width=10)
        reason = clean_text((eligibility or {}).get("reason") or availability.get("reason") or "unknown")
        if eligibility is not None:
            eligible = bool(eligibility.get("eligible"))
            lines += self._kv("Eligible", f"{'yes' if eligible else 'no'} · {human('reason', reason)} ({reason})",
                              width, "ok" if eligible else "warn", label_width=10)
        else:
            lines += self._kv("Reason", f"{human('reason', reason)} ({reason})", width,
                              "ok" if reason in {"ready", "claimed"} else "warn", label_width=10)
        gate = clean_text(availability.get("reason") or "")
        last_state = clean_text(status.get("last_state") or "")
        if observed_at is not None:
            text = ago(observed_at)
            if key == "stale":
                text += f" · expired, last known state {last_state or 'unknown'}"
            role: str | None = "warn" if key == "stale" else None
        elif gate == "mode_off":
            text, role = "not read · local mode is off, cache untouched", "muted"
        elif gate in {"unknown_host", "lifecycle_ineligible", "no_enabled_seats"}:
            text, role = f"not read · host gated off ({human('reason', gate).lower()})", "muted"
        elif gate == "configuration_changed":
            text, role = "not observed · configuration changed", "muted"
        else:
            text, role = "not observed · no probe cached yet", "muted"
        lines += self._kv("Observed", text, width, role, label_width=10)
        served = status.get("model_profile") or availability.get("model_profile")
        if served:
            lines += self._kv("Serving", served, width, label_width=10)
        if jobs_error:
            lines += self._kv("Live jobs", f"unreadable · {jobs_error}", width, "warn", label_width=10)
        return lines

    def _probe_lines(self, host_id: str | None, report: Mapping[str, Any], width: int) -> list[Line]:
        snapshot = report.get("probe")
        if not isinstance(snapshot, Mapping) or not snapshot.get("enabled"):
            return self._kv("Probe", "unavailable · no probe service in this session", width, "muted",
                            label_width=10)
        lines: list[Line] = []
        host = report["hosts"].get(host_id) if host_id else None
        last = host.get("probe") if isinstance(host, Mapping) else None
        if snapshot.get("probing") == host_id:
            lines += self._kv("Probe", "probing now…", width, "live", label_width=10)
        elif host_id in tuple(snapshot.get("pending") or ()):
            lines += self._kv("Probe", "queued", width, "live", label_width=10)
        elif isinstance(last, Mapping):
            state = clean_text(last.get("state") or "unknown")
            finished = last.get("finished_at")
            text = f"{state} · {human('reason', last.get('reason') or 'unknown')} · {last.get('source') or 'probe'}"
            if isinstance(finished, (int, float)):
                text += f" · finished {ago(finished)}"
            lines += self._kv("Probe", text, width, status_chip("local", state).role, label_width=10)
            if last.get("detail"):
                lines += self._kv("", last["detail"], width, "muted", label_width=10)
        else:
            lines += self._kv("Probe", "idle · no probe result yet", width, "muted", label_width=10)
        periodic = (
            "on · refreshes near the cache lifetime" if snapshot.get("periodic")
            else "off (--no-active-probe) · r still probes now"
        )
        lines += self._kv("Periodic", periodic, width, "muted", label_width=10)
        if snapshot.get("last_error"):
            lines += self._kv("Error", snapshot["last_error"], width, "bad", label_width=10)
        return lines

    def _host_detail(self, host_id: str, width: int, *, compact: bool) -> list[Line]:
        report = self._local_report()
        host = report["hosts"].get(host_id, {})
        config = host.get("config", {}) if isinstance(host, Mapping) else {}
        status = host.get("status") if isinstance(host, Mapping) else None
        status = status if isinstance(status, Mapping) else {}
        lifecycle = human("lifecycle", config.get("lifecycle") or "unknown")
        lines: list[Line] = []
        if compact:
            lines.append(self._title([host_id, "Host", lifecycle]))
        else:
            lines.append(Line([(host_id, "accent"), ("  ", None), (f"Host · {lifecycle}", None)]))
        chip = status_chip("local", status.get("key"))
        lines.append(Line([(f"{chip.glyph} ", chip.role), (chip.label[:1].upper() + chip.label[1:], chip.role),
                           (f" · {status.get('detail')}" if status.get("detail") else "", "muted")]))
        if compact:
            lines += self._compact_actions(self._host_actions(host_id), width)
        lines += self._kv("Label", config.get("label") or "—", width, label_width=10)
        lines += self._kv("Global", human("local_mode", report.get("global_mode")), width, label_width=10)
        # Live status first: it is what an operator opens a host to check.
        lines.append(self._section("Availability", width, "cached"))
        lines += self._availability_lines(
            host.get("availability") if isinstance(host, Mapping) else None, width,
            status=status, jobs_error=report.get("jobs_error"),
        )
        lines.append(self._section("Live probe", width))
        lines += self._probe_lines(host_id, report, width)
        lines.append(self._section("Endpoint", width))
        lines += self._kv("Base URL", config.get("base_url") or "not configured", width,
                          None if config.get("base_url") else "muted", label_width=10)
        lines += self._kv("Lease URL", config.get("lease_url") or "none", width,
                          None if config.get("lease_url") else "muted", label_width=10)
        probe = config.get("availability_probe")
        if isinstance(probe, Mapping) and isinstance(probe.get("argv"), list) and probe["argv"]:
            # One clipped line: the program's short name and its arguments. A
            # long interpreter path would otherwise wrap over the whole pane.
            argv = [str(part) for part in probe["argv"]]
            command = " ".join([argv[0].rsplit("/", 1)[-1], *argv[1:]])
            lines += self._kv("Probe", clip(command, max(1, width - 10)), width, label_width=10)
            lines += self._kv("Timeout", f"{probe.get('timeout_seconds')} s", width, label_width=10)
        else:
            lines += self._kv("Probe", "not configured · probes report probe_not_configured",
                              width, "muted", label_width=10)
        lines.append(self._section("Capacity", width))
        groups = config.get("resource_groups")
        if isinstance(groups, Mapping) and groups:
            text = ", ".join(
                f"{group_id} ×{group.get('max_concurrency') if isinstance(group, Mapping) else '?'}"
                for group_id, group in groups.items()
            )
            lines += self._kv("Groups", text, width, label_width=10)
        else:
            lines += self._kv("Groups", "none", width, "muted", label_width=10)
        seats = host.get("seats") if isinstance(host, Mapping) else None
        lines += self._kv("Seats", ", ".join(seats) if seats else "none registered", width,
                          None if seats else "muted", label_width=10)
        if not compact:
            lines.append(self._section("Actions", width))
            lines += self._action_lines(self._host_actions(host_id), width)
        return lines

    def _local_seat_detail(self, seat_id: str, width: int, *, compact: bool) -> list[Line]:
        report = self._local_report()
        seat = report["seats"].get(seat_id, {})
        if not seat and self.local_error:
            return [Line([(piece, "bad")]) for piece in self._wrap(
                f"Local registry unavailable: {self.local_error}", width)]
        config = seat.get("config", {}) if isinstance(seat, Mapping) else {}
        eligibility = seat.get("eligibility") if isinstance(seat, Mapping) else None
        eligibility = eligibility if isinstance(eligibility, Mapping) else None
        host_id = config.get("host")
        host = report["hosts"].get(host_id, {}) if isinstance(host_id, str) else {}
        host_config = host.get("config", {}) if isinstance(host, Mapping) else {}
        entry = self._entry("local", seat_id) or {"provider": "local", "seat": seat_id,
                                                  "status": (seat.get("status") or {}).get("key")}
        chip = self._chip(entry)
        model = config.get("model_profile") or "?"
        lines: list[Line] = []
        if compact:
            lines.append(self._title([f"{model} on {host_id or '?'}", "Local", seat_id]))
        else:
            lines.append(Line([(f"{model} on {host_id or '?'}", "accent"), ("  ", None),
                               (f"Local · {seat_id}", None)]))
        detail = (seat.get("status") or {}).get("detail") if isinstance(seat.get("status"), Mapping) else None
        lines.append(Line([(f"{chip.glyph} ", chip.role), (chip.label[:1].upper() + chip.label[1:], chip.role),
                           (f" · {detail}" if detail else "", "muted")]))
        actions = self._local_seat_actions(seat_id, menu=True)
        if compact:
            lines += self._compact_actions(self._local_seat_actions(seat_id), width)
        lines.append(self._section("Mode", width))
        effective = (seat.get("effective_mode") if isinstance(seat, Mapping) else None) or "off"
        lines += self._kv("Effective", human("local_mode", effective), width,
                          "muted" if effective == "off" else "accent", label_width=10)
        lines += self._kv("Global", human("local_mode", report.get("global_mode")), width, label_width=10)
        lines += self._kv("Seat", human("local_mode", config.get("mode") or "off"), width, label_width=10)
        lines += self._kv("Lifecycle", human("lifecycle", config.get("lifecycle") or "unknown"), width,
                          label_width=10)
        lines.append(self._section("Placement", width))
        if isinstance(host_id, str) and host_config:
            lines += self._kv("Host", f"{host_id} · {human('lifecycle', host_config.get('lifecycle'))}"
                              f" · {host_config.get('label') or ''}", width, label_width=10)
        else:
            lines += self._kv("Host", f"{host_id or 'none'} · not registered", width, "bad", label_width=10)
        groups = host_config.get("resource_groups")
        group_id = config.get("resource_group")
        group = groups.get(group_id) if isinstance(groups, Mapping) and group_id else None
        group_max = group.get("max_concurrency") if isinstance(group, Mapping) else None
        lines += self._kv("Group", f"{group_id or 'none'}" + (
            f" · max {group_max}" if group_max is not None else " · unknown"), width,
            None if group_max is not None else "warn", label_width=10)
        lines += self._kv("Slots", config.get("max_concurrency", "unknown"), width, label_width=10)
        lines.append(self._section("Model", width))
        lines += self._kv("Model", model, width, label_width=10)
        lines += self._kv("Context", config.get("context_window_size") or "model default", width, label_width=10)
        lines += self._kv("Harness", config.get("harness") or "unknown", width, label_width=10)
        capabilities = config.get("capabilities") if isinstance(config.get("capabilities"), Mapping) else {}
        shapes = capabilities.get("task_shapes")
        validation = capabilities.get("validation")
        lines += self._kv("Shapes", ", ".join(map(str, shapes)) if isinstance(shapes, list) and shapes else "none",
                          width, None if shapes else "muted", label_width=10)
        lines += self._kv("Checks", ", ".join(map(str, validation))
                          if isinstance(validation, list) and validation else "none",
                          width, None if validation else "muted", label_width=10)
        lines += self._kv("Runtime", config.get("runtime_fingerprint") or "missing", width, label_width=10)
        lines.append(self._section("Availability", width, "cached"))
        lines += self._availability_lines(
            host.get("availability") if isinstance(host, Mapping) else None, width,
            eligibility=eligibility, status=seat.get("status") if isinstance(seat, Mapping) else None,
            jobs_error=report.get("jobs_error"),
        )
        lines.append(self._section("Live probe", width))
        lines += self._probe_lines(host_id if isinstance(host_id, str) else None, report, width)
        if not compact:
            lines.append(self._section("Actions", width))
            lines += self._action_lines(actions, width)
        return lines

    def _add_detail(self, row: Row, width: int, *, compact: bool) -> list[Line]:
        if self.screen == "hosts":
            text = ("A host is a machine serving local models. Add it here, then add local "
                    "seats on it; hosts start paused.")
        elif self.screen == "local-seats":
            text = "A local seat is one model on a registered host. It starts paused with mode off."
        elif self.screen == "seats":
            text = ("A new Codex seat starts paused. Sign in with l, then resume it with p "
                    "(as primary when it is the only active seat).")
        else:
            hosts = sorted(self._local_report().get("hosts", {}))
            text = (
                "Enter or a chooses the seat type. Codex: a new seat that starts paused. "
                "Claude: there is one Claude account, so this opens its settings. Local: a seat "
                + (f"on a registered host ({', '.join(hosts)})." if hosts else
                   "on a local host; none is registered yet, so you add a host first.")
            )
        lines = [self._title([row.title])] if compact else [Line([(row.title, "accent")])]
        return lines + [Line([(piece, None)]) for piece in self._wrap(text, width)]

    # -- pushed views ------------------------------------------------------------------------
    def _selectable(self, label_parts: Parts, selected: bool, *, disabled: bool = False) -> Line:
        marker: Parts = [(MARKER + " ", "accent")] if selected else [("  ", None)]
        if disabled:
            label_parts = [(text, "muted") for text, _role in label_parts]
        return Line([*marker, *label_parts], "selected" if selected else None)

    def _paged(self, lines: list[Line], selected: int, height: int, header: int) -> list[Line]:
        """Keep the selected item visible below ``header`` fixed lines."""
        slots = max(1, height - header)
        self._page_size = slots
        head, items = lines[:header], lines[header:]
        if len(items) <= slots:
            return lines
        start = min(max(0, selected - slots // 2), len(items) - slots)
        return [*head, *items[start:start + slots]]

    def _heading(self, title: str, context: Any = "") -> Line:
        parts: Parts = [(title, "heading")]
        if context:
            parts += [(" · ", "muted"), (clean_text(context), "accent")]
        return Line(parts)

    def _global_body(self, width: int, height: int) -> list[Line]:
        fields = tuple(DEFAULTS)
        lines = [self._heading("Global settings", "changes apply at once"), Line([])]
        longest = max(display_width(label) for label in GLOBAL_LABELS.values())
        label_width = min(longest + 2, max(10, width // 2))
        for index, key in enumerate(fields):
            value = self._global_text(key, self.global_values.get(key))
            lines.append(self._selectable(
                [*self._cell(GLOBAL_LABELS.get(key, key), label_width), (value, "accent")],
                index == self.global_index,
            ))
        selected = fields[self.global_index % len(fields)]
        lines.append(Line([]))
        lines.append(self._section(GLOBAL_LABELS.get(selected, selected), width))
        lines += [Line([(piece, None)]) for piece in self._wrap(GLOBAL_HELP.get(selected, ""), width)]
        lines += [Line([(piece, "muted")]) for piece in self._wrap(f"Setting: {selected}", width)]
        self._page_size = max(1, height - 2)
        return lines

    def _menu_body(self, view: str, width: int, height: int) -> list[Line]:
        row = self._menu_row(view)
        actions = self._menu_actions(view)
        target = self.selected_id if view == "actions" else self.local_selected_id
        kind = {"actions": "Seat actions", "local-actions": "Host actions" if self.screen == "hosts"
                else "Local seat actions"}[view]
        lines = [self._heading(kind, target)]
        if row is not None:
            detail = self._detail(row, width, compact=True)
            lines += detail[1:2]
        lines.append(Line([]))
        header = len(lines)
        label_width = min(30, max(16, max((display_width(a.label) for a in actions), default=16) + 2))
        for index, action in enumerate(actions):
            key = action.key if action.key and action.key != "Enter" else " "
            note = action.reason or action.hint
            lines.append(self._selectable(
                [(f"{key:<2}", "muted" if action.reason else "accent"),
                 *self._cell(action.label, label_width), (note, "muted")],
                index == self.menu_index, disabled=bool(action.reason),
            ))
        if not actions:
            lines.append(Line([("  This item has no actions right now.", "muted")]))
        return self._paged(lines, self.menu_index, height, header)

    def _choice_body(self, view: str, width: int, height: int) -> list[Line]:
        if view == "global-choice":
            key = tuple(DEFAULTS)[self.global_index]
            title, context = GLOBAL_LABELS.get(key, key), "choose a value"
            current = self.global_values.get(key)
            items = [
                self._global_text(key, value) + (" (current)" if value == current else "")
                for value in self._global_choices()
            ]
            index = self.global_choice_index
        elif view == "add-presets":
            title, context = "Plan", "for the new seat"
            items = [str(choice["label"]) for choice in ADD_PLAN_CHOICES]
            index = self.add_preset_index
        elif view == "capacity-presets":
            title, context = "Plan & capacity", self.selected_id
            items = [str(preset["label"]) for preset in CAPACITY_CHOICES]
            index = self.capacity_preset_index
        else:
            title, context = self.choice_title, self.choice_context
            items = [label for label, _value in self.choice_items]
            index = self.choice_index
        lines = [self._heading(title, context), Line([])]
        for position, label in enumerate(items):
            lines.append(self._selectable([(label, None)], position == index))
        return self._paged(lines, index, height, 2)

    def _form_body(self, view: str, width: int, height: int) -> list[Line]:
        label_width = min(28, max(8, width // 3))
        rows: list[tuple[str, str, bool]] = []
        hint = ""
        if view == "settings":
            title, context = "Edit plan & headroom", self.selected_id
            for field_item in FIELDS:
                rows.append((field_item.label, self._current_field_value(field_item), False))
            index = self.field_index
            hint = "Each saved field applies at once."
        elif view == "add-form":
            title, context = "Add Codex seat", "starts paused"
            profile = self.add_buffers["profile"]
            values = {
                "label": str(self.add_buffers["label"]),
                "home": str(self.add_buffers["home"] or "default (pool root/seat id)"),
                "profile": human("capacity_profile", profile) if profile else "Unverified",
            }
            for field_item in ADD_FIELDS:
                rows.append((field_item.label, values.get(field_item.option, ""), field_item.kind == "submit"))
            index = self.add_field_index
        else:
            verb = "Add" if self.local_form_action.startswith("add-") else "Edit"
            noun = "host" if self.local_form_action.endswith("host") else "seat"
            title, context = f"{verb} local {noun}", self.local_form_target or f"new {noun}"
            for field_item in self.local_form_fields:
                if field_item.kind == "submit":
                    rows.append((field_item.label, "", True))
                    continue
                value = self.local_form_buffers.get(field_item.option, "")
                if not value:
                    value = "none" if field_item.kind.startswith("optional") else "required"
                rows.append((field_item.label, value, False))
            index = self.local_form_index
            fields = self.local_form_fields
            if 0 <= index < len(fields):
                hint = fields[index].hint
        lines = [self._heading(title, context), Line([])]
        for position, (label, value, submit) in enumerate(rows):
            if submit:
                parts: Parts = [("→ " + label, "ok")]
            else:
                parts = [*self._cell(label, label_width, "muted"), (value, None)]
            lines.append(self._selectable(parts, position == index))
        body = self._paged(lines, index, max(1, height - (3 if hint else 0)), 2)
        if hint:
            body.append(Line([]))
            body += [Line([(piece, "muted")]) for piece in self._wrap(hint, width)][:2]
        return body

    def _input_body(self, view: str, width: int, height: int) -> list[Line]:
        hint = ""
        if view == "capacity-input":
            lines = [self._heading("Plan & capacity", f"{self.selected_id} · advanced"), Line([])]
            for index, (label, option) in enumerate(CAPACITY_PARTS):
                lines.append(self._selectable(
                    [(f"{label}: ", "muted"), (self.capacity_buffers.get(option, ""), None)],
                    index == self.capacity_index,
                ))
            if height >= 7:
                lines.append(Line([]))
                lines += [Line([(piece, "muted")]) for piece in self._wrap(
                    "↑↓ changes field. A verified source needs a relative capacity.", width)]
            return lines
        if view == "input":
            field_item = FIELDS[self.field_index]
            title, context = field_item.label, self.selected_id
        elif view == "add-input":
            title, context = ADD_FIELDS[self.add_field_index].label, "new Codex seat"
            hint = "The label is 1–128 visible characters. Home is blank or an absolute path."
        elif view == "local-form-input":
            field_item = self.local_form_fields[self.local_form_index]
            title, context = field_item.label, self.local_form_target or "new"
            hint = field_item.hint
        elif view == "claude-input":
            title, context = CLAUDE_HEADROOM_FIELDS[self.claude_input_field][1], "Claude"
            hint = self._claude_setting_help(f"claude-{self.claude_input_field}")
        else:
            key = tuple(DEFAULTS)[self.global_index]
            title, context = GLOBAL_LABELS.get(key, key), "minutes, 1–240"
        lines = [self._heading(title, context), Line([])]
        buffer_lines = wrap_lines([self.input_buffer], max(1, width - 3)) or [""]
        for index, piece in enumerate(buffer_lines):
            cursor = "▏" if index == len(buffer_lines) - 1 else ""
            lines.append(Line([("> " if index == 0 else "  ", "accent"), (piece + cursor, None)], "selected"))
        if hint:
            lines.append(Line([]))
            lines += [Line([(piece, "muted")]) for piece in self._wrap(hint, width)]
        return lines

    # -- help ----------------------------------------------------------------------------------
    def _help_context(self) -> str:
        screen, view = (self._stack[-1][0], self._stack[-1][1]) if self._stack else (self.screen, "list")
        if view == "overview":
            return "overview"
        if view == "list":
            return screen
        if view == "global-settings":
            return "global"
        if view in TEXT_VIEWS:
            return "input"
        if view in {"settings", "add-form", "local-form"}:
            return "form"
        return "menu"

    def _help_lines(self, width: int, height: int) -> list[Line]:
        context = self._help_context()
        title = f"Help · {HELP_TITLES.get(context, context)}"
        note = "keys change with the screen you are on"
        gap = width - display_width(title) - display_width(note)
        top = Line([(title, "heading"), (" " * gap, None), (note, "muted")]) if gap >= 2 else Line(
            [(title, "heading")])

        def rows(keys: tuple[Key, ...]) -> list[Parts]:
            return [[*self._cell(key.cap, 10, "accent"), (key.help, None)] for key in keys]

        this = rows(KEYMAP[context])
        everywhere = rows(GLOBAL_KEYS if context != "overview" else ())
        body: list[Line] = []
        if width >= 80 and everywhere:
            column = (width - 2) // 2
            body.append(Line([*self._section("This screen", column - 1).parts, ("  ", None),
                              *self._section("Everywhere", column - 1).parts]))
            for index in range(max(len(this), len(everywhere))):
                left = self._fit(this[index], column - 2) if index < len(this) else []
                used = sum(display_width(text) for text, _role in left)
                parts: Parts = [*left, (" " * max(0, column - used), None)]
                if index < len(everywhere):
                    parts += everywhere[index]
                body.append(Line(parts))
        else:
            body.append(self._section("This screen", width))
            body += [Line(parts) for parts in this]
            if everywhere:
                body.append(self._section("Everywhere", width))
                body += [Line(parts) for parts in everywhere]
        notes = [
            "Changes apply at once and show a result line; only Redeem (c) and Remove ask first.",
            "Screens: " + " → ".join(SCREEN_TITLES[screen] for screen in SCREENS),
            "Esc or ? closes help and returns to the screen you opened it from.",
        ]
        body.append(Line([]))
        for note_text in notes:
            body += [Line([(piece, "muted")]) for piece in self._wrap(note_text, width)]
        slots = max(1, height - 3)
        self._page_size = slots
        self.help_scroll = min(max(0, self.help_scroll), max(0, len(body) - slots))
        visible = body[self.help_scroll:self.help_scroll + slots]
        more = len(body) > slots
        footer = Line(footer_parts((
            Key("↑↓ PgDn", "scroll", "", footer=2) if more else Key("", "", ""),
            Key("Esc", "back", "", footer=1),
        ), width))
        return [top, Line([]), *visible, *[Line([])] * (slots - len(visible)), footer][:height]

    # -- dialogs -------------------------------------------------------------------------------
    def _dialog_content(self) -> tuple[str, list[Line], Parts]:
        """Title, body lines and key line for the open dialog."""
        if self.view == "busy":
            body = [Line([(piece, None)]) for piece in self._wrap(self.busy_text, 60)]
            keys: Parts = (
                [("waiting for the provider's answer", "muted")] if self.reset_phase == "redeem"
                else [("Esc", "accent"), (" Cancel", "muted")]
            )
            return "Working", body, keys
        if self.view == "quit-confirm":
            body = [Line([(piece, "warn")]) for piece in self._wrap(self.busy_text, 60)]
            return "Quit?", body, [("Enter", "accent"), (" Quit, leave draining   ", "muted"),
                                   ("Esc", "accent"), (" Stay", "muted")]
        summary, subtitle, changes, warning, confirm, raw = self._confirm_material()
        if self.confirm_raw:
            body = []
            for source in raw:
                role = (
                    "muted" if source.startswith(("---", "+++")) else
                    "bad" if source.startswith("-") or source.startswith("!") else
                    "ok" if source.startswith("+") else
                    "accent" if source.startswith("@@") else None
                )
                body.append(Line([(source, role)]))
            keys = [("Enter", "accent"), (f" {confirm}   ", "muted"), ("Esc", "accent"),
                    (" Cancel   ", "muted"), ("d", "accent"), (" Hide diff", "muted")]
            return "Raw settings diff", body, keys
        body = [Line([]), Line([(summary, None)])]
        if subtitle:
            body.append(Line([(subtitle, "muted")]))
        body.append(Line([]))
        body.append(Line([("What changes", "heading")]))
        label_width = max((display_width(label) for label, _value in changes), default=10) + 2
        for label, value in changes:
            body.append(Line([("  ", None), *self._cell(label, label_width, "muted"), (value, None)]))
        body.append(Line([]))
        if self.confirm_stale:
            body.append(Line([("! Settings changed since this preview; nothing was written.", "warn")]))
            body.append(Line([("  Enter shows a fresh preview.", "warn")]))
            keys = [("Enter", "accent"), (" Fresh preview   ", "muted"), ("Esc", "accent"), (" Cancel", "muted")]
        else:
            body.append(Line([(f"! {warning}", "warn")]))
            keys = [("Enter", "accent"), (f" {confirm}   ", "muted"), ("Esc", "accent"),
                    (" Cancel   ", "muted"), ("d", "accent"), (" Show raw diff", "muted")]
        return "Confirm", body, keys

    def _confirm_material(self) -> tuple[str, str, list[tuple[str, str]], str, str, list[str]]:
        kind = self.confirm_kind
        if kind == "redeem-reset" and self.reset_preview is not None:
            preview = self.reset_preview
            seat_id = str(preview.get("seat") or self.selected_id)
            try:
                email = json.loads(preview["provider_identity"])["email"]
            except (KeyError, TypeError, ValueError):
                email = "account unavailable"
            entry = self._entry("codex", seat_id) or {}
            count = preview.get("available_count")
            credit = preview.get("credit") if isinstance(preview.get("credit"), Mapping) else {}
            raw = [
                f"Seat: {seat_id}",
                f"Home: {preview.get('home')}",
                f"Account: {email}",
                f"Available banked resets: {count}",
                f"Selected credit: {credit.get('title') or credit.get('reset_type') or 'credit'} "
                f"({credit.get('fingerprint')})",
                f"Granted: {self._credit_date(credit.get('granted_at'))}; expires: "
                f"{self._credit_date(credit.get('expires_at'), credit.get('expiry_status'))}",
                "Effect: spend exactly one banked reset. This cannot be undone.",
                "The login, seat registry and credit are checked again before spending.",
            ]
            changes = [("Usage windows", "reset immediately")]
            if isinstance(count, int):
                changes.append(("Banked resets", f"{count} → {max(0, count - 1)}"))
            return (
                f"Redeem one banked reset on {seat_id}",
                f"{email} · {entry.get('plan') or ''}".rstrip(" ·"),
                changes,
                "Spends a provider credit. This cannot be undone.",
                "Redeem",
                raw,
            )
        if kind == "remove-local" and self.local_preview is not None:
            seat_id = str(self.local_preview.target_id)
            config = self._local_config(seat_id)
            return (
                f"Remove local seat {seat_id}",
                f"{config.get('model_profile') or '?'} on {config.get('host') or '?'}",
                [("Lifecycle", "Paused → Retired"), ("Configuration", "kept as retired history")],
                "A retired local seat cannot be reactivated.",
                "Remove",
                self._local_preview_material(),
            )
        preview = self.preview
        if preview is None:
            return ("Preview expired", "", [], "Start the action again.", "Close", [])
        lifecycle = "inactive"
        if self.manager is not None:
            lifecycle = self.manager.settings.get("pool", {}).get("seats", {}).get(
                preview.seat_id, {}).get("lifecycle", lifecycle)
        entry = self._entry("codex", preview.seat_id) or {}
        diff = [
            line.rstrip("\n")
            for line in difflib.unified_diff(
                preview.before.splitlines(keepends=True),
                preview.after.splitlines(keepends=True),
                fromfile="settings.before",
                tofile="settings.after",
            )
        ]
        raw = [*[f"! {warning}" for warning in preview.warnings], *(diff or ["No settings bytes would change."])]
        return (
            f"Remove {preview.seat_id} from service",
            " · ".join(part for part in (str(entry.get("label") or ""), str(entry.get("plan") or "")) if part
                       and part != preview.seat_id),
            [(
                "Lifecycle", f"{human('lifecycle', lifecycle)} → Retired"),
                ("Home & sign-in", "kept on disk"),
                ("History", "kept · h shows retired seats"),
            ],
            "A retired seat cannot be reactivated; its id is never reused.",
            "Remove",
            raw,
        )

    @staticmethod
    def _credit_date(value: Any, status: str | None = None) -> str:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return day(value)
        return "none" if status == "none" else "unknown"

    def _dialog_lines(self, width: int, height: int) -> tuple[int, int, list[Line]]:
        """``(x, y, lines)`` of the bordered dialog box."""
        box_width = max(10, min(width - 4, 64)) if width >= 28 else width
        inner = box_width - 4
        title, body, keys = self._dialog_content()
        wrapped: list[Line] = []
        for line in body:
            text_width = sum(display_width(clean_text(t)) for t, _r in line.parts)
            if text_width <= inner:
                wrapped.append(line)
                continue
            role = next((r for t, r in line.parts if t.strip()), None)
            text = self._plain(line)
            hang = "  " if text.startswith("! ") else ""
            pieces = self._wrap(text, inner - len(hang))
            wrapped += [Line([((hang if index else "") + piece, role)]) for index, piece in enumerate(pieces)]
        key_width = sum(display_width(clean_text(t)) for t, _r in keys)
        if key_width > inner and len(keys) > 4:
            # Too narrow for one key line: confirm and cancel first, the rest below.
            wrapped += [Line([]), Line(keys[:4])]
            keys = keys[4:]
        footer = Line(keys)
        available = max(1, height - 4)
        slots = max(1, available - 1)
        self._page_size = slots
        self.scroll = min(max(0, self.scroll), max(0, len(wrapped) - slots))
        visible = wrapped[self.scroll:self.scroll + slots]
        head = f"╭─ {title} "
        tail = "╮"
        if len(wrapped) > slots:
            tail = f" ↑↓ {self.scroll + 1}-{self.scroll + len(visible)}/{len(wrapped)} ─╮"
        fill = max(0, box_width - display_width(head) - display_width(tail))
        top = Line([(head, "accent"), ("─" * fill, "accent"), (tail, "accent")])
        lines = [top]
        for line in [*visible, Line([]), footer]:
            fitted = self._fit(line.parts, inner)
            used = sum(display_width(t) for t, _r in fitted)
            lines.append(Line([("│ ", "accent"), *fitted, (" " * (inner - used), None), (" │", "accent")]))
        lines.append(Line([("╰" + "─" * max(0, box_width - 2) + "╯", "accent")]))
        x = max(0, (width - box_width) // 2)
        y = max(0, (height - len(lines)) // 2)
        return x, y, lines[:height]

    def _overlay(self, base: list[Line], box: tuple[int, int, list[Line]], width: int, height: int) -> list[Line]:
        """Draw the dialog over a dimmed copy of the screen it was opened from."""
        x, y, lines = box
        result = [Line([(self._plain(line), "muted")]) for line in base]
        result += [Line([])] * (height - len(result))
        for offset, box_line in enumerate(lines):
            row = y + offset
            if row >= len(result):
                break
            text = self._plain(base[row]) if row < len(base) else ""
            box_width = sum(display_width(clean_text(t)) for t, _r in box_line.parts)
            left = self._cells(text, 0, x)
            right = self._cells(text, x + box_width, width)
            left += " " * (x - display_width(left))
            result[row] = Line([(left, "muted"), *box_line.parts, (right, "muted")])
        return result[:height]

    @staticmethod
    def _cells(text: str, start: int, end: int) -> str:
        """The characters of ``text`` occupying display cells ``start``..``end``."""
        out: list[str] = []
        position = 0
        for character in text:
            cells = display_width(character)
            if position >= start and position + cells <= end:
                out.append(character)
            elif start < position + cells and position < start:
                out.append(" ")
            position += cells
            if position >= end:
                break
        return "".join(out)
