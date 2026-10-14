"""Management-only local-seat commands; all edits use settings_transaction."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import sys
from contextlib import ExitStack
from types import SimpleNamespace

from . import local_seats, state
from .controller_context import current_controller
from .seat_registry import LIFECYCLES, LOCAL_MODES, validate_lifecycle_transition

_HOST_FIELDS = frozenset({"label", "lifecycle", "resource_groups", "base_url", "lease_url", "availability_probe"})
_HOST_OPTIONAL_FIELDS = frozenset({"base_url", "lease_url", "availability_probe"})
_SEAT_FIELDS = frozenset({"label", "lifecycle", "mode", "host", "resource_group",
                          "model_profile", "harness", "capabilities", "max_concurrency",
                          "runtime_fingerprint", "context_window_size"})
_SEAT_OPTIONAL_FIELDS = frozenset({"context_window_size"})
READ_ACTIONS = ("list", "status", "probe")
MUTATING_ACTIONS = ("set-global-mode", "set-seat-mode", "add-host", "add-seat",
                    "edit-host", "edit-seat", "set-host-lifecycle", "set-seat-lifecycle")
# Structured actions written only through ``MANAGER_AUTHORITY``; they are not
# ``codex-seat local`` commands. ``remove-seat`` retires an inactive, off, idle
# seat in place so its id, configuration and history remain as a tombstone.
MANAGER_ACTIONS = ("remove-seat",)
STRUCTURED_ACTIONS = (*MUTATING_ACTIONS, *MANAGER_ACTIONS)
# Modes ordered from most to least restrictive; moving right is an activation.
_MODE_ORDER = ("off", "shadow", "canary", "opportunistic")
# Authority for the trusted interactive manager's internal write path. It is
# distinct from (and never relaxes) the CLI guards: see ``_manager_terminal``.
MANAGER_AUTHORITY = "interactive-manager"


def register(subparsers):
    parser = subparsers.add_parser("local", help="manage local hosts, seats and availability")
    commands = parser.add_subparsers(dest="local_action", required=True)
    for action in (*READ_ACTIONS, *MUTATING_ACTIONS):
        command = commands.add_parser(action)
        command.set_defaults(handler="local")
        command.add_argument("--json", action="store_true")
        if action == "probe":
            command.add_argument("host", nargs="?")
            command.add_argument("--all", action="store_true")
            command.add_argument("--from-current-user-request", action="store_true")
        if action not in READ_ACTIONS:
            command.add_argument("--preview", action="store_true")
            command.add_argument("--expected-digest", help="SHA-256 of settings bytes from preview")
            command.add_argument("--from-current-user-request", action="store_true")
            if action != "set-global-mode":
                command.add_argument("id")
            if action.endswith("mode"):
                command.add_argument("mode", choices=sorted(LOCAL_MODES))
            elif action.endswith("lifecycle"):
                command.add_argument("lifecycle", choices=sorted(LIFECYCLES))
            elif action.startswith("edit-"):
                command.add_argument("--changes-json", required=True, help="non-secret field changes as a JSON object")
            else:
                command.add_argument("--config-json", required=True, help="complete non-secret host/seat JSON object")


def _digest(before):
    return hashlib.sha256((before or "").encode()).hexdigest()


def _effective(document):
    return state._merge(state.default_settings(), document)


def load_local_settings(root):
    """Strict read of the current settings: (effective settings, warnings, digest).

    Invalid settings raise instead of silently becoming default capacity. The
    digest is the SHA-256 of the exact current settings bytes (of the empty
    string when no file exists yet), the same value previews pin.
    """
    document, before, _, warnings = state._load_settings_for_mutation(root, require_existing=False)
    return _effective(document), warnings, _digest(before)


def settings_digest(root):
    """SHA-256 of the exact current settings bytes, as pinned by previews."""
    return load_local_settings(root)[2]


def _trusted_terminal(args, action):
    context = current_controller()
    if context.managed_worker or not context.is_controller:
        raise ValueError(f"{action} requires a trusted controller")
    if not getattr(args, "from_current_user_request", False):
        raise ValueError(f"{action} requires --from-current-user-request")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError(f"{action} requires an interactive trusted human terminal")


def _manager_terminal(args, action):
    """Internal write path for the trusted interactive manager; not a CLI path.

    Allowed only when ``seat_manager.ensure_management_eligible()`` passes, i.e.
    a real stdin/stdout TTY outside controller/head and managed-worker contexts
    (a pseudo-terminal in those contexts is still refused), and only after the
    manager marked the write as an explicitly confirmed current request.
    """
    from .seat_manager import ManagerEligibilityError, ensure_management_eligible

    try:
        ensure_management_eligible()
    except ManagerEligibilityError as exc:
        raise ValueError(f"{action} refused: {exc}") from exc
    if not getattr(args, "from_current_user_request", False):
        raise ValueError(f"{action} requires explicit confirmation of the current request")


def _mode_rank(mode):
    # Unknown modes rank as the least restrictive so they can never bypass the
    # activation check; registry validation rejects them afterwards anyway.
    return _MODE_ORDER.index(mode) if mode in _MODE_ORDER else len(_MODE_ORDER)


def _mutate(document, args):
    action = args.local_action
    if action == "set-global-mode":
        document.setdefault("providers", {}).setdefault("local", {})["mode"] = args.mode
        return args.mode != "off"
    registry = document.setdefault("local_hosts" if "host" in action else "local_seats", {})
    if action.startswith("add-"):
        if args.id in registry:
            raise ValueError("local id already exists")
        if len(args.config_json.encode()) > 16384:
            raise ValueError("configuration too large")
        entry = json.loads(args.config_json)
        if not isinstance(entry, dict):
            raise ValueError("configuration must be an object")
        registry[args.id] = entry
        return action == "add-seat" and entry.get("mode", "off") != "off"
    if args.id not in registry:
        raise ValueError("unknown local id")
    entry = registry[args.id]
    if action == "remove-seat":
        if entry.get("lifecycle") == "retired":
            return False  # already a tombstone
        if entry.get("lifecycle") != "inactive" or entry.get("mode", "off") != "off":
            raise ValueError("local seat removal requires lifecycle inactive and mode off; "
                             "deactivate the seat first")
        validate_lifecycle_transition(entry["lifecycle"], "retired")
        entry["lifecycle"] = "retired"
        return False
    if action.startswith("edit-"):
        if len(args.changes_json.encode()) > 16384:
            raise ValueError("configuration too large")
        changes = json.loads(args.changes_json)
        if not isinstance(changes, dict) or not changes:
            raise ValueError("changes must be a non-empty object")
        allowed = _HOST_FIELDS if action == "edit-host" else _SEAT_FIELDS
        if set(changes) - allowed:
            raise ValueError("unsupported local edit field")
        if "lifecycle" in changes:
            validate_lifecycle_transition(entry["lifecycle"], changes["lifecycle"])
        previous_mode = entry.get("mode", "off")
        for field, value in changes.items():
            optional_field = (
                (action == "edit-host" and field in _HOST_OPTIONAL_FIELDS)
                or (action == "edit-seat" and field in _SEAT_OPTIONAL_FIELDS)
            )
            if optional_field and value is None:
                entry.pop(field, None)
            else:
                entry[field] = value
        if (action == "edit-seat" and changes.get("lifecycle") not in (None, "active")
                and "mode" not in changes):
            entry["mode"] = "off"
        # Only a less restrictive mode is an activation. Editing a label (or
        # any other field) on an already enabled seat leaves its mode alone.
        return (action == "edit-seat"
                and _mode_rank(entry.get("mode", "off")) > _mode_rank(previous_mode))
    if action == "set-seat-mode":
        entry["mode"] = args.mode
        return args.mode != "off"
    validate_lifecycle_transition(entry["lifecycle"], args.lifecycle)
    entry["lifecycle"] = args.lifecycle
    if action == "set-seat-lifecycle" and args.lifecycle != "active":
        entry["mode"] = "off"
    return False


def _seat_slot_held(root, seat_id):
    """True while any local execution holds one of ``seat_id``'s slot locks.

    Never creates the runtime directory or lock files; a missing runtime means
    no local execution ever claimed a slot. Lock files are permanent inode
    anchors, so one vanishing mid-scan could hide a holder of the unlinked
    inode and fails closed. Our brief shared probe lock cannot admit work.
    """
    pattern = re.compile(rf"seat-{re.escape(seat_id)}-\d+\.lock")
    with ExitStack() as stack:
        try:
            directory = stack.enter_context(local_seats._runtime(root))
        except FileNotFoundError:
            return False
        for name in os.listdir(directory):
            if not pattern.fullmatch(name):
                continue
            try:
                fd = local_seats._open_file(directory, name)
            except FileNotFoundError as exc:
                raise ValueError(f"local runtime lock {name} disappeared during the scan") from exc
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            finally:
                os.close(fd)
    return False


def _require_no_running_job(root, seat_id):
    """Fail closed unless no local execution holds one of ``seat_id``'s slots.

    Every local execution holds a seat slot for its whole run, and
    ``local_seats.claim`` re-reads settings under the settings lock after
    taking its slots. Called under that lock, this check therefore either sees
    a claimed slot or the claim sees the retirement and backs out. The dispatch
    live-job snapshot is not consulted: it never attributes jobs to local seats.
    An unsafe or changing runtime refuses the removal.
    """
    try:
        held = _seat_slot_held(root, seat_id)
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot verify that local seat {seat_id} has no running job: {exc}") from exc
    if held:
        raise ValueError(f"local seat {seat_id} has a running local job; wait for it to finish before removal")


def mutate(root, args, *, authority=None):
    """Preview does no writes; apply re-reads and checks digest under the shared lock.

    ``authority`` selects the guard for writes. ``None`` is the CLI path, whose
    guards are unchanged: host probe/config commands require a trusted
    controller terminal. ``MANAGER_AUTHORITY`` is the distinct internal path of
    the trusted interactive manager (``_manager_terminal``): every write must
    pin the previewed digest and is refused for controller/head and managed
    workers even on a PTY. ``MANAGER_ACTIONS`` are written only through
    ``MANAGER_AUTHORITY``.
    """
    if authority not in (None, MANAGER_AUTHORITY):
        raise ValueError("unknown local mutation authority")
    action = args.local_action
    if action not in STRUCTURED_ACTIONS:
        raise ValueError("unknown local action")
    context = current_controller()
    if not args.preview and context.managed_worker:
        raise ValueError("managed workers cannot change local settings")
    if not args.preview and action in MANAGER_ACTIONS and authority != MANAGER_AUTHORITY:
        raise ValueError(f"local {action} is available only through the interactive seat manager")
    if not args.preview and authority == MANAGER_AUTHORITY:
        _manager_terminal(args, f"local {action}")
        if args.expected_digest is None:
            raise ValueError("manager writes require the previewed settings digest")
    elif not args.preview and action in {"add-host", "edit-host"}:
        _trusted_terminal(args, f"local {action}")
    if not args.preview and action.startswith("edit-") and args.expected_digest is None:
        raise ValueError("local edits require --expected-digest from preview")

    def prepare(document, before):
        digest = _digest(before)
        if args.expected_digest is not None and args.expected_digest != digest:
            raise ValueError("settings digest changed; preview again")
        updated = copy.deepcopy(document)
        enabling = _mutate(updated, args)
        if enabling and not args.preview and not args.from_current_user_request:
            raise ValueError("enabling local mode requires --from-current-user-request for this explicit request")
        canonical, warnings = state._validated_settings_document(updated, root=root)
        before_seat = document.get("local_seats", {}).get(args.id) if action in {
            "remove-seat", "set-seat-lifecycle", "edit-seat"
        } else None
        after_seat = canonical.get("local_seats", {}).get(args.id) if before_seat else None
        if (before_seat and after_seat
                and before_seat.get("lifecycle") != "retired"
                and after_seat.get("lifecycle") == "retired"):
            # Every path to retirement checks on the locked re-read so a job
            # cannot slip in after preview or bypass Remove through lifecycle.
            _require_no_running_job(root, args.id)
            if action == "remove-seat":
                warnings = [*warnings, f"local seat {args.id} is retired in place; its id, configuration "
                                       "and history are kept and it can no longer be reactivated"]
        return canonical, {"preview": args.preview, "before_digest": digest,
                           "before": document, "after": canonical, "warnings": warnings,
                           "enabling": enabling}

    if args.preview:
        document, before, _, _ = state._load_settings_for_mutation(root, require_existing=False)
        _, result = prepare(document, before)
        result["changed"] = result["before"] != result["after"]
        return result
    with state.settings_transaction(root) as transaction:
        transaction.settings, result = prepare(transaction.settings, transaction.before)
    result["changed"] = transaction.changed
    result["after_digest"] = _digest(transaction.after)
    return result


def preview_edit(root, kind, entry_id, changes):
    """Return a structured, read-only host or seat edit preview for a manager."""
    return mutate(root, _edit_args(kind, entry_id, changes, preview=True))


def apply_edit(root, kind, entry_id, changes, *, expected_digest, from_current_user_request=False):
    """Apply an edit only if the previewed settings bytes are still current."""
    return mutate(root, _edit_args(kind, entry_id, changes, preview=False,
                                   expected_digest=expected_digest,
                                   from_current_user_request=from_current_user_request))


def _edit_args(kind, entry_id, changes, *, preview, expected_digest=None,
               from_current_user_request=False):
    if kind not in {"host", "seat"}:
        raise ValueError("edit kind must be host or seat")
    return action_args(f"edit-{kind}", entry_id, preview=preview, expected_digest=expected_digest,
                       from_current_user_request=from_current_user_request, changes=changes)


def action_args(action, entry_id=None, *, preview, expected_digest=None,
                from_current_user_request=False, **values):
    """Build the argument namespace ``mutate`` expects for one structured action.

    ``values`` carries exactly one option per action family: ``mode`` for the
    mode actions, ``lifecycle`` for the lifecycle actions, ``changes`` for
    ``edit-*`` and ``config`` for ``add-*``; ``remove-seat`` takes none.
    """
    if action not in STRUCTURED_ACTIONS:
        raise ValueError("unknown local action")
    if action == "set-global-mode":
        if entry_id is not None:
            raise ValueError("global mode takes no host or seat id")
    elif not isinstance(entry_id, str) or not entry_id:
        raise ValueError("a local host or seat id is required")
    args = SimpleNamespace(local_action=action, id=entry_id, preview=preview,
                           expected_digest=expected_digest,
                           from_current_user_request=from_current_user_request)
    if action in MANAGER_ACTIONS:
        if values:
            raise ValueError(f"local {action} takes no options")
        return args
    if action.endswith("mode"):
        option, choices = "mode", LOCAL_MODES
    elif action.endswith("lifecycle"):
        option, choices = "lifecycle", LIFECYCLES
    else:
        option, choices = ("changes" if action.startswith("edit-") else "config"), None
    if set(values) != {option}:
        raise ValueError(f"local {action} takes exactly the {option} option")
    value = values[option]
    if choices is not None:
        if value not in choices:
            raise ValueError(f"{option} must be one of {', '.join(sorted(choices))}")
        setattr(args, option, value)
    else:
        if not isinstance(value, dict):
            raise ValueError(f"{'changes' if option == 'changes' else 'configuration'} must be an object")
        setattr(args, f"{option}_json", json.dumps(value))
    return args


def preview_action(root, action, entry_id=None, **values):
    """Structured, read-only preview of any local mutation (see ``action_args``)."""
    return mutate(root, action_args(action, entry_id, preview=True, **values))


def apply_action(root, action, entry_id=None, *, expected_digest, from_current_user_request=False,
                 authority=None, **values):
    """Apply one structured local mutation pinned to the previewed digest."""
    return mutate(root, action_args(action, entry_id, preview=False, expected_digest=expected_digest,
                                    from_current_user_request=from_current_user_request, **values),
                  authority=authority)


def cmd_local(args, root):
    try:
        if args.local_action in READ_ACTIONS:
            settings, warnings, _ = load_local_settings(root)
            if args.local_action == "probe":
                if bool(args.host) == bool(args.all):
                    raise ValueError("probe requires one host or --all")
                _trusted_terminal(args, "local probe")
                if args.host and args.host not in settings["local_hosts"]:
                    raise ValueError("unknown local host")
                hosts = list(settings["local_hosts"]) if args.all else [args.host]
                result = {"hosts": {host: local_seats.probe_host(root, settings, host) for host in hosts}}
                for host, availability in result["hosts"].items():
                    availability["resource_groups"] = list(settings["local_hosts"][host]["resource_groups"])
            else:
                result = local_seats.status(root, settings)
                if args.local_action == "list":
                    result["local_hosts"] = settings["local_hosts"]
                    result["local_seats"] = settings["local_seats"]
            result["warnings"] = warnings
        else:
            result = mutate(root, args)
        if args.json or args.local_action not in READ_ACTIONS:
            print(json.dumps(result, indent=2))
        else:
            print(f"local mode: {result.get('global_mode', 'probe')}")
            for host, availability in result.get("hosts", {}).items():
                print(f"{host}: {availability['state']} ({availability['reason']})")
            for seat in result.get("seats", []):
                print(f"{seat['seat']}: {seat['host']}/{seat['resource_group']} "
                      f"{seat['effective_mode']} ({seat['reason']})")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if args.json:
            print(json.dumps({"error": "local_control_error", "detail": str(exc)}))
        else:
            print(f"local: {exc}", file=sys.stderr)
        return 2
