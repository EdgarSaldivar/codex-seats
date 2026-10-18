"""Terminal byte-stream and complete-preview regressions."""
import io
import os

from codex_seats.terminal import (
    KEY_BACKTAB,
    TerminalSession,
    decode_key,
    display_width,
    paint_frame,
    wrap_lines,
)


def test_frame_paints_full_width_rows_without_erasing_last_cell_or_scrolling():
    painted = paint_frame('ABCDE\n12345', 2)
    assert painted.endswith('12345')  # no erase at a pending-wrap cursor
    assert '\n' not in painted       # works even with ONLCR disabled
    assert '\033[2;1H' in painted
    assert not painted.startswith('\033[H\033[J')  # no blank-screen flash
    assert paint_frame('short', 2).endswith('\033[2;1H\033[J')


def test_combined_arrows_text_and_enter_keep_event_order():
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd, 'rb', buffering=0) as source:
            terminal = TerminalSession(source, io.StringIO())
            os.write(write_fd, b'\x1b[B\x1b[Aab\r')
            assert [terminal.read_key(0.1) for _ in range(5)] == [
                'down', 'up', 'a', 'b', 'enter'
            ]
    finally:
        os.close(write_fd)


def test_fragmented_arrow_and_utf8_survive_separate_reads():
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd, 'rb', buffering=0) as source:
            terminal = TerminalSession(source, io.StringIO())
            os.write(write_fd, b'\x1b[')
            assert terminal.read_key(0.1) is None
            os.write(write_fd, b'B' + '界'.encode()[:2])
            assert terminal.read_key(0.1) == 'down'
            assert terminal.read_key(0) is None
            os.write(write_fd, '界'.encode()[2:] + b'\x04')
            assert terminal.read_key(0.1) == '界'
            assert terminal.read_key(0.1) == 'eof'
    finally:
        os.close(write_fd)


def test_wide_preview_wrapping_preserves_every_character():
    text = 'label: 界界界界e\u0301 end'
    pieces = wrap_lines([text], 8)
    assert ''.join(pieces) == text
    assert all(display_width(piece) <= 8 for piece in pieces)


def test_shift_tab_decodes_as_backtab_alone_and_inside_a_burst():
    assert KEY_BACKTAB == '\x1b[Z'
    assert decode_key(b'\x1b[Z') == KEY_BACKTAB
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd, 'rb', buffering=0) as source:
            terminal = TerminalSession(source, io.StringIO())
            os.write(write_fd, b'\x1b[Z\t\x1b[')
            assert terminal.read_key(0.1) == KEY_BACKTAB
            assert terminal.read_key(0.1) == '\t'
            assert terminal.read_key(0.1) is None  # fragment waits for its final byte
            os.write(write_fd, b'Z')
            assert terminal.read_key(0.1) == KEY_BACKTAB
    finally:
        os.close(write_fd)
