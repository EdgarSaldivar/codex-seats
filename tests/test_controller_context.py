from __future__ import annotations

import pytest

from codex_seats.controller_context import current_controller


def test_explicit_codex_head_preserves_seat_and_binding():
    context = current_controller(
        {
            "CODEX_CONTROL_SEAT": "seat4",
            "CODEX_SEATS_HEAD": "codex",
            "CODEX_THREAD_ID": "thread-4",
            "CODEX_TURN_ID": "turn-4",
        }
    )
    assert context.is_controller
    assert (context.head_kind, context.controller_id, context.seat) == (
        "codex",
        "seat4",
        "seat4",
    )
    assert (context.thread_id, context.turn_id) == ("thread-4", "turn-4")


def test_explicit_claude_controller_has_no_codex_seat():
    context = current_controller({"CODEX_SEATS_HEAD": "claude"})
    assert context.is_controller
    assert context.controller_id == "claude-head"
    assert context.dispatch_identity({"pool": {"primary": "seat1"}}) == (
        "claude",
        None,
    )


@pytest.mark.parametrize("turn_id", [None, "turn-remote"])
@pytest.mark.parametrize(
    "runtime_marker",
    [
        {"CODEX_HOME": "/codex/home"},
        {"CODEX_SESSION_ID": "session-remote"},
    ],
)
def test_remote_codex_context_has_stable_controller_and_optional_turn(
    turn_id, runtime_marker
):
    env = {"CODEX_THREAD_ID": "thread-remote", **runtime_marker}
    if turn_id is not None:
        env["CODEX_TURN_ID"] = turn_id
    context = current_controller(env)
    assert context.is_controller and context.remote
    assert context.controller_id == "codex-primary"
    assert context.thread_id == "thread-remote"
    assert context.turn_id == turn_id
    assert context.dispatch_identity({}) == ("codex", "seat1")


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"CODEX_HOME": "/codex/home"},
        {"CODEX_THREAD_ID": "thread-only"},
        {"CODEX_SESSION_ID": "session-only"},
        {"CODEX_SEATS_HEAD": "codex"},
    ],
)
def test_arbitrary_shell_is_not_inferred_as_remote_controller(env):
    context = current_controller(env)
    assert not context.is_controller


def test_explicit_claude_head_is_a_non_remote_controller():
    context = current_controller(
        {
            "CODEX_SEATS_HEAD": "claude",
            "CODEX_THREAD_ID": "thread",
            "CODEX_HOME": "/codex/home",
        }
    )
    assert context.is_controller
    assert context.controller_id == "claude-head"
    assert not context.remote


def test_managed_worker_is_never_a_controller_even_with_all_markers():
    context = current_controller(
        {
            "CODEX_SEATS_MANAGED_WORKER": "1",
            "CODEX_CONTROL_SEAT": "seat2",
            "CODEX_SEATS_HEAD": "codex",
            "CODEX_THREAD_ID": "thread",
            "CODEX_SESSION_ID": "session",
            "CODEX_HOME": "/codex/home",
        }
    )
    assert not context.is_controller
    assert not context.remote
