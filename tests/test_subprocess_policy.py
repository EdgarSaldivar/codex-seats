"""Focused fixtures for the tracked production subprocess policy."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

POLICY_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_subprocess_policy.py"
SPEC = importlib.util.spec_from_file_location("check_subprocess_policy", POLICY_PATH)
assert SPEC is not None and SPEC.loader is not None
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("import subprocess as sp\nsp.run(['x'])\n", "direct subprocess.run"),
        (
            "from subprocess import check_output as invoke\ninvoke(['x'])\n",
            "direct subprocess.invoke",
        ),
        ("import subprocess\nsubprocess.Popen(['x'], shell=True)\n", "shell=True"),
        (
            "from codex_seats import bounded_process as bp\nbp.run(['x'])\n",
            "requires an explicit timeout",
        ),
        (
            "from codex_seats.bounded_process import run as bounded\nbounded(['x'])\n",
            "requires an explicit timeout",
        ),
        ("child.wait()\n", "unbounded no-argument wait"),
        ("child.communicate()\n", "unbounded no-argument communicate"),
    ],
)
def test_violating_fixtures_are_rejected(tmp_path, source, message):
    fixture = tmp_path / "fixture.py"
    fixture.write_text(source)
    errors = policy.check_file(fixture)
    assert any(message in error for error in errors)


def test_clean_alias_fixture_is_accepted(tmp_path):
    fixture = tmp_path / "fixture.py"
    fixture.write_text(
        "from codex_seats import bounded_process as bp\n"
        "bp.run(['x'], timeout=5)\n"
        "child.wait(timeout=2)\n"
        "child.communicate(timeout=2)\n"
    )
    assert policy.check_file(fixture) == []


def test_root_scan_excludes_tests(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "lib" / "clean.py").write_text(
        "from codex_seats import bounded_process\n"
        "bounded_process.run(['x'], timeout=1)\n"
    )
    (tmp_path / "tests" / "fixture.py").write_text(
        "import subprocess\nsubprocess.run(['allowed-in-tests'])\n"
    )
    assert policy.check_root(tmp_path) == []
