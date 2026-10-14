#!/usr/bin/env python3
"""Compatibility entry point for the packaged standalone status line helper."""
import runpy
from pathlib import Path

if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    helper = root / "lib" / "codex_seats" / "claude_statusline.py"
    if not helper.is_file():
        helper = root / "current" / "lib" / "codex_seats" / "claude_statusline.py"
    runpy.run_path(str(helper), run_name="__main__")
