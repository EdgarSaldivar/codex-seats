from __future__ import annotations

import re
import tomllib
from pathlib import Path

from codex_seats import __version__
from codex_seats.workers import TESTED_CODEX


ROOT = Path(__file__).resolve().parents[1]


def test_release_and_codex_pins_do_not_drift() -> None:
    project_version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
        "version"
    ]
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked_project = next(
        package for package in lock["package"] if package["name"] == "codex-seats"
    )
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    native_pin = re.search(r"@openai/codex@([0-9.]+)", ci)
    wheel_version = re.search(r'codex-seat ([0-9.]+)"$', ci, re.MULTILINE)

    assert project_version == __version__ == locked_project["version"] == "8.1.3"
    assert native_pin and native_pin.group(1) == TESTED_CODEX == "0.153.1"
    assert wheel_version and wheel_version.group(1) == project_version
