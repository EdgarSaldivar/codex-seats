"""Review gate stays inside the loaded source or wheel package."""
from pathlib import Path

from codex_seats import goalspec


def test_review_gate_is_package_local(tmp_path: Path) -> None:
    module = tmp_path / "site-packages" / "codex_seats" / "goalspec.py"
    module.parent.mkdir(parents=True)
    module.touch()
    assert goalspec._review_gate(module) == module.with_name("review.py")
    assert goalspec.REVIEW_GATE.is_file()
    assert goalspec.REVIEW_GATE.parent == Path(goalspec.__file__).resolve().parent


def test_direct_review_script_ignores_poisoned_pythonpath(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    poison = tmp_path / "poison"
    (poison / "codex_seats").mkdir(parents=True)
    (poison / "codex_seats" / "__init__.py").write_text("raise RuntimeError('poison loaded')\n")
    script = goalspec.REVIEW_GATE
    result = subprocess.run(
        [sys.executable, str(script), "--help"], cwd=poison,
        env={**os.environ, "PYTHONPATH": str(poison)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "--diff-file" in result.stdout
