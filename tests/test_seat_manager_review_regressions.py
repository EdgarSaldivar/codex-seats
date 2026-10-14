"""Behavioral regressions from the head's integrated UI review."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codex_seats.seat_manager_tui import CAPACITY_PARTS, ManagerUI
from test_seat_manager_tui import _FakeManager, _frontier_ui, _preview


@pytest.mark.parametrize('index,text', [(2, 'workspace-ui'), (0, 'unique-job')])
def test_capacity_text_does_not_trigger_navigation_or_quit(index, text):
    ui = ManagerUI(_FakeManager(), direct=True)
    ui.selected_id = 'seat1'
    ui.view = 'capacity-input'
    ui.capacity_index = index
    ui.capacity_buffers = {option: '' for _, option in CAPACITY_PARTS}
    for character in text:
        assert ui.handle(character) is None
    assert ui.capacity_index == index
    assert ui.capacity_buffers[CAPACITY_PARTS[index][1]] == text


def test_selected_seat_remains_visible_in_a_short_long_list():
    manager = _FakeManager()
    for number in range(3, 15):
        manager.settings['pool']['seats'][f'seat{number}'] = dict(
            manager.settings['pool']['seats']['seat2'], label=f'Seat {number}'
        )
    ui = _frontier_ui(manager)
    ui.render(80, 8, 'overview')
    for _ in range(13):
        ui.handle('down')
    frame = ui.render(80, 8, 'overview')
    assert ui.selected_id == 'seat14'
    assert '> seat14' in frame
    assert len(frame.splitlines()) <= 8


def test_cancel_after_checks_finish_prevents_activation_commit():
    manager = _FakeManager()
    commits = []
    manager.commit_activation = lambda *args: commits.append(args)
    ui = ManagerUI(manager, direct=True)
    ui.selected_id = 'seat1'
    ui.view = 'busy'
    ui.preview = replace(_preview(), action='activate')
    ui.activation_thread = SimpleNamespace(is_alive=lambda: False)
    ui.activation_result = SimpleNamespace(passed=True, seat_id='seat1', detail='ok')
    ui.activation_cancel.set()
    ui.poll()
    assert commits == []
    assert 'cancel' in ui.message.lower()


def test_resize_only_quit_does_not_trap_a_pending_drain():
    manager = _FakeManager()
    manager.pending = [SimpleNamespace(seat_id='seat2', state='draining')]
    ui = ManagerUI(manager, direct=True)
    ui.render(20, 4, 'overview')
    assert ui.handle('q').reason == 'quit'
    assert manager.pending[0].state == 'draining'
    assert ui.activation_cancel.is_set()
