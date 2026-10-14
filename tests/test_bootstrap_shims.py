"""Bootstrap shims refuse unsupported interpreters before importing release code."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
SHIMS = ("codex-seat", "worker-hook")
RELEASE_ID = "b" * 64


def _install(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """A synthetic runtime root whose release code records that it was imported."""
    root = tmp_path / "runtime"
    package = root / "releases" / RELEASE_ID / "lib" / "codex_seats"
    package.mkdir(parents=True)
    marker = tmp_path / "imported"
    (package / "__init__.py").write_text("")
    body = (
        "import os, pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text(os.environ['CODEX_SEATS_RELEASE_ID'])\n"
        "print('release:' + '.'.join(map(str, sys.version_info[:2])))\n"
    )
    (package / "cli.py").write_text(body)
    (package / "worker_hook.py").write_text(body)
    os.symlink(f"releases/{RELEASE_ID}", root / "current")
    shim = root / name
    shutil.copy2(PROJECT / "bootstrap" / name, shim)
    shim.chmod(0o755)
    return shim, marker


def _path_with_python3(tmp_path: Path, script: str | None = None) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python3 = bin_dir / "python3"
    if script is None:
        python3.symlink_to(sys.executable)
    else:
        python3.write_text(script)
        python3.chmod(0o755)
    # Only the controlled directory and system tools; no developer PATH.
    return f"{bin_dir}:/usr/bin:/bin"


def _run(shim: Path, path: str) -> subprocess.CompletedProcess[str]:
    env = {"PATH": path, "HOME": str(shim.parent), "LANG": "C"}
    return subprocess.run(
        [str(shim)], env=env, capture_output=True, text=True, timeout=30, check=False,
    )


@pytest.mark.parametrize("name", SHIMS)
def test_shebang_resolves_supported_python3_from_path(tmp_path, name):
    shim, marker = _install(tmp_path, name)
    result = _run(shim, _path_with_python3(tmp_path))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "release:" + ".".join(map(str, sys.version_info[:2]))
    assert marker.read_text() == RELEASE_ID


@pytest.mark.parametrize("name", SHIMS)
def test_old_python3_on_path_fails_before_importing_release(tmp_path, name):
    shim, marker = _install(tmp_path, name)
    # A python3 earlier on PATH that reports 3.10, launched through the real shebang.
    wrapper = (
        "#!/bin/sh\n"
        f"exec {sys.executable} -I -c '"
        "import runpy, sys; sys.version_info = (3, 10, 14, \"final\", 0); "
        "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name=\"__main__\")"
        "' \"$@\"\n"
    )
    result = _run(shim, _path_with_python3(tmp_path, wrapper))
    assert result.returncode == 126
    assert "Python 3.11 or newer is required" in result.stderr
    assert "first on PATH" in result.stderr
    assert result.stdout == ""
    assert not marker.exists()


def _native_old_python() -> str | None:
    for candidate in ("python3.10", "python3.9", "python3.8"):
        found = shutil.which(candidate)
        if found:
            return found
    system = Path("/usr/bin/python3")
    if system.exists():
        probe = subprocess.run(
            [str(system), "-c", "import sys; print(sys.version_info < (3, 11))"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if probe.stdout.strip() == "True":
            return str(system)
    return None


@pytest.mark.parametrize("name", SHIMS)
def test_native_old_interpreter_gets_clear_error(tmp_path, name):
    old = _native_old_python()
    if old is None:
        pytest.skip("no Python older than 3.11 is installed")
    shim, marker = _install(tmp_path, name)
    result = _run(shim, _path_with_python3(tmp_path, f"#!/bin/sh\nexec {old} \"$@\"\n"))
    assert result.returncode == 126
    assert "Python 3.11 or newer is required" in result.stderr
    assert not marker.exists()
