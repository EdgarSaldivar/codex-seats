"""Reusable dependency-free terminal ownership, input, and display helpers."""

from __future__ import annotations

import codecs
import os
import select
import termios
import time
import tty
import unicodedata
from typing import Any, Callable, Protocol


ALT_ON = "\033[?1049h"
ALT_OFF = "\033[?1049l"
# Shift-Tab arrives as CSI Z; the raw sequence is the key's stable name.
KEY_BACKTAB = "\x1b[Z"


def paint_frame(frame: str, height: int) -> str:
    """Paint rows without a blank frame, newline translation, or edge wrapping."""
    lines = frame.split("\n")[:height]
    output = "".join(
        f"\033[{row};1H\033[2K{line}" for row, line in enumerate(lines, 1)
    )
    if len(lines) < height:
        output += f"\033[{len(lines) + 1};1H\033[J"
    return output


def clean_text(value: Any) -> str:
    """Remove terminal control characters while preserving printable Unicode."""
    text = str(value)
    return "".join(
        character
        if character in "\t " or unicodedata.category(character)[0] != "C"
        else "?"
        for character in text
    ).replace("\t", " ")


def display_width(value: str) -> int:
    width = 0
    for character in clean_text(value):
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
    return width


def fit_text(value: Any, width: int) -> str:
    """Fit sanitized text to display cells without splitting wide glyphs."""
    if width <= 0:
        return ""
    output: list[str] = []
    used = 0
    for character in clean_text(value):
        cells = 0 if unicodedata.combining(character) else (
            2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        )
        if used + cells > width:
            break
        output.append(character)
        used += cells
    return "".join(output) + " " * max(0, width - used)


def wrap_lines(lines: list[str], width: int) -> list[str]:
    """Wrap by terminal cells without dropping preview text at wide glyphs."""
    width = max(1, width)
    wrapped: list[str] = []
    for line in lines:
        piece = ""
        cells = 0
        for character in clean_text(line):
            size = display_width(character)
            if piece and cells + size > width:
                wrapped.append(piece)
                piece, cells = "", 0
            piece += character
            cells += size
        wrapped.append(piece)
    return wrapped


def decode_key(payload: bytes) -> str:
    """Decode one terminal input burst into a stable key name."""
    arrows = {
        b"\x1b[A": "up",
        b"\x1b[B": "down",
        b"\x1b[C": "right",
        b"\x1b[D": "left",
        b"\x1b[5~": "pageup",
        b"\x1b[6~": "pagedown",
    }
    if payload in arrows:
        return arrows[payload]
    if payload == b"\x1b[Z":
        return KEY_BACKTAB
    if payload == b"\x1b":
        return "esc"
    if payload in {b"\r", b"\n"}:
        return "enter"
    if payload in {b"\x7f", b"\x08"}:
        return "backspace"
    if payload == b"\x03":
        return "interrupt"
    if payload in {b"", b"\x04"}:
        return "eof"
    try:
        return codecs.decode(payload, "utf-8", "replace")
    except Exception:
        return "?"


class TerminalSession:
    """Own cbreak mode and alternate-screen restoration for one UI session."""

    def __init__(self, stdin: Any, stdout: Any) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.fd = stdin.fileno()
        self._attributes: list[Any] | None = None
        self._input = b""

    def __enter__(self) -> "TerminalSession":
        self._attributes = termios.tcgetattr(self.fd)
        try:
            # Enable responsive input before announcing the screen as ready.
            # PTY drivers may otherwise buffer a key written after ALT_ON.
            tty.setcbreak(self.fd, termios.TCSANOW)
            self.stdout.write(ALT_ON + "\033[?25l")
            self.stdout.flush()
        except BaseException:
            termios.tcsetattr(self.fd, termios.TCSANOW, self._attributes)
            raise
        return self

    def __exit__(self, *_args: object) -> None:
        try:
            if self._attributes is not None:
                # TCSANOW is required for SIGHUP/SIGTERM cleanup: there may be
                # no pending output drain once the peer is closing.
                termios.tcsetattr(self.fd, termios.TCSANOW, self._attributes)
        finally:
            try:
                self.stdout.write("\033[?25h" + ALT_OFF)
                self.stdout.flush()
            except OSError:
                pass

    def _take_key(self) -> str | None:
        if not self._input:
            return None
        sequences = (
            b"\x1b[A", b"\x1b[B", b"\x1b[C", b"\x1b[D", b"\x1b[5~", b"\x1b[6~", b"\x1b[Z"
        )
        for sequence in sequences:
            if self._input.startswith(sequence):
                self._input = self._input[len(sequence):]
                return decode_key(sequence)
        if any(sequence.startswith(self._input) for sequence in sequences):
            return None
        first = self._input[0]
        length = 2 if 0xC2 <= first <= 0xDF else (
            3 if 0xE0 <= first <= 0xEF else (4 if 0xF0 <= first <= 0xF4 else 1)
        )
        if len(self._input) < length:
            return None
        payload, self._input = self._input[:length], self._input[length:]
        return decode_key(payload)

    def read_key(self, timeout: float) -> str | None:
        key = self._take_key()
        if key is not None:
            return key
        try:
            ready, _write, _error = select.select(
                [self.fd], [], [], max(0.0, timeout)
            )
        except OSError:
            return "eof"
        if not ready:
            return None
        try:
            payload = os.read(self.fd, 32)
        except OSError:
            return "eof"
        if not payload:
            self._input = b""
            return "eof"
        self._input += payload
        if self._input == b"\x1b":
            # Distinguish standalone Escape from a fragmented CSI key.
            more, _write, _error = select.select([self.fd], [], [], 0.025)
            if more:
                try:
                    self._input += os.read(self.fd, 31)
                except OSError:
                    return "eof"
            if self._input == b"\x1b":
                self._input = b""
                return "esc"
        return self._take_key()


class UI(Protocol):
    def poll(self) -> bool: ...

    def render(self, width: int, height: int, overview: str) -> str: ...

    def handle(self, key: str) -> Any: ...


def run_ui(
    ui: UI,
    *,
    stdin: Any,
    stdout: Any,
    size: Callable[[], os.terminal_size],
    overview: Callable[[os.terminal_size], str],
    redraw_interval: float,
    on_first_render: Callable[[], None] | None = None,
    heartbeat: Callable[[], Any] | None = None,
) -> Any:
    """Run one terminal ownership epoch, restoring it before every return."""
    first = True
    dirty = True
    next_render = 0.0
    next_heartbeat = 0.0
    with TerminalSession(stdin, stdout) as terminal:
        while True:
            now = time.monotonic()
            if heartbeat is not None and now >= next_heartbeat:
                try:
                    heartbeat()
                except OSError:
                    pass
                next_heartbeat = now + 2.0
            dirty = ui.poll() or dirty
            now = time.monotonic()
            if dirty or now >= next_render:
                current = size()
                frame = ui.render(current.columns, current.lines, overview(current))
                stdout.write(paint_frame(frame, current.lines))
                stdout.flush()
                if first and on_first_render is not None:
                    on_first_render()
                first = False
                dirty = False
                next_render = now + max(0.05, redraw_interval)
            key = terminal.read_key(min(0.1, max(0.0, next_render - time.monotonic())))
            if key is None:
                continue
            outcome = ui.handle(key)
            if outcome is not None:
                return outcome
            dirty = True
