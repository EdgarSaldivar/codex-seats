from __future__ import annotations

import ast
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from codex_seats import bounded_process


def test_exact_bytes_explicit_text_and_isolated_non_shell_launch(monkeypatch):
    observed = {}
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        observed.update(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(bounded_process.subprocess, "Popen", recording_popen)
    result = bounded_process.run(
        [
            sys.executable,
            "-c",
            (
                "import os,sys; os.write(1, sys.stdin.buffer.read()+b'\\xff'); "
                "os.write(2,b'err\\x00')"
            ),
        ],
        timeout=3,
        input_bytes="snowman: ☃".encode(),
    )

    assert result.returncode == 0
    assert result.stdout == "snowman: ☃".encode() + b"\xff"
    assert result.stderr == b"err\x00"
    assert result.stdout_text(errors="surrogateescape").startswith("snowman: ☃")
    assert observed["shell"] is False
    assert observed["start_new_session"] is True
    assert observed["stdout"] is subprocess.PIPE
    assert observed["stderr"] is subprocess.PIPE


def test_passthrough_output_is_live_and_stdio_is_inherited(capfdbinary, monkeypatch):
    observed = {}
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        observed.update(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(bounded_process.subprocess, "Popen", recording_popen)
    result = bounded_process.run(
        [
            sys.executable,
            "-c",
            "import os; os.write(1,b'live-out\\xff'); os.write(2,b'live-err\\x00')",
        ],
        timeout=3,
        capture_output=False,
    )

    captured = capfdbinary.readouterr()
    assert captured.out == b"live-out\xff"
    assert captured.err == b"live-err\x00"
    assert result.stdout == b"" and result.stderr == b""
    assert observed["stdin"] is None
    assert observed["stdout"] is None
    assert observed["stderr"] is None


def test_launch_and_nonzero_have_distinct_typed_errors(tmp_path):
    missing = tmp_path / "definitely-missing"
    with pytest.raises(bounded_process.ProcessLaunchError) as launch:
        bounded_process.run([str(missing)], timeout=1)
    assert launch.value.argv == (str(missing),)

    with pytest.raises(bounded_process.ProcessCommandError) as command:
        bounded_process.run(
            [
                sys.executable,
                "-c",
                (
                    "import os; os.write(1,b'out\\xff'); os.write(2,b'bad\\x00'); "
                    "raise SystemExit(17)"
                ),
            ],
            timeout=3,
            check=True,
        )
    assert command.value.result.returncode == 17
    assert command.value.result.stdout == b"out\xff"
    assert command.value.result.stderr == b"bad\x00"


def test_timeout_term_result_and_direct_child_is_reaped():
    code = (
        "import os,signal,time; "
        "signal.signal(signal.SIGTERM, lambda *_: (os.write(2,b'term'), exit(23))); "
        "os.write(1,b'ready'); time.sleep(30)"
    )
    with pytest.raises(bounded_process.ProcessTimeoutError) as raised:
        bounded_process.run([sys.executable, "-c", code], timeout=0.2, term_grace=1)

    result = raised.value.result
    assert result.stdout == b"ready"
    assert result.stderr == b"term"
    assert result.returncode == 23
    with pytest.raises(ChildProcessError):
        os.waitpid(result.pid, os.WNOHANG)


def test_cancel_event_terminates_bounded_capture_group():
    event = threading.Event()
    timer = threading.Timer(0.1, event.set)
    timer.start()
    try:
        with pytest.raises(bounded_process.ProcessCancelledError):
            bounded_process.run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout=10,
                max_output_bytes=1024,
                cancel_event=event,
            )
    finally:
        timer.cancel()


def test_cancel_event_requires_bounded_capture():
    with pytest.raises(ValueError, match="requires bounded capture"):
        bounded_process.run(
            [sys.executable, "-c", "pass"],
            timeout=1,
            cancel_event=threading.Event(),
        )


def test_timeout_kills_descendant_group_without_touching_unrelated_process(tmp_path):
    descendant_pid = tmp_path / "descendant.pid"
    child_code = (
        "import os,signal,time; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"open({str(descendant_pid)!r},'w').write(str(os.getpid())); "
        "time.sleep(30)"
    )
    parent_code = (
        "import signal,subprocess,sys,time; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        f"p={str(descendant_pid)!r}; "
        "[time.sleep(.01) for _ in range(100) if not __import__('os').path.exists(p)]; "
        "time.sleep(30)"
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    started = time.monotonic()
    try:
        with pytest.raises(bounded_process.ProcessTimeoutError) as raised:
            bounded_process.run(
                [sys.executable, "-c", parent_code], timeout=0.4, term_grace=0.1
            )
        assert time.monotonic() - started < 5
        assert raised.value.result.returncode == -signal.SIGKILL
        assert unrelated.poll() is None
        pid = int(descendant_pid.read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            pytest.fail("timed-out descendant remained alive")
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=3)


def test_escaped_descendant_cannot_hold_capture_pipes_open_forever(tmp_path):
    escaped_pid = tmp_path / "escaped.pid"
    child_code = (
        "import os,signal,time; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"open({str(escaped_pid)!r},'w').write(str(os.getpid())); "
        "os.write(1,b'held-open'); time.sleep(30)"
    )
    parent_code = (
        "import os,subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child_code!r}],start_new_session=True); "
        f"p={str(escaped_pid)!r}; "
        "[(time.sleep(.01)) for _ in range(100) if not os.path.exists(p)]"
    )
    pid = None
    started = time.monotonic()
    try:
        with pytest.raises(bounded_process.ProcessTimeoutError) as raised:
            bounded_process.run(
                [sys.executable, "-c", parent_code],
                timeout=0.2,
                term_grace=0.1,
            )
        assert time.monotonic() - started < 2
        assert b"held-open" in raised.value.result.stdout
        pid = int(escaped_pid.read_text())
    finally:
        if pid is None and escaped_pid.exists():
            pid = int(escaped_pid.read_text())
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_timeout_must_be_explicit_positive_and_finite(timeout):
    with pytest.raises(ValueError, match="positive and finite"):
        bounded_process.run([sys.executable, "-c", "pass"], timeout=timeout)


def test_all_library_synchronous_calls_use_the_bounded_runner_with_timeout():
    library = Path(__file__).parents[1] / "lib" / "codex_seats"
    migrated = []
    forbidden = []
    missing_bounds = []
    for path in library.glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            owner = node.func.value
            if (
                isinstance(owner, ast.Name)
                and owner.id == "subprocess"
                and node.func.attr in {"run", "check_output", "check_call"}
            ):
                forbidden.append(f"{path.name}:{node.lineno}")
            if (
                isinstance(owner, ast.Name)
                and owner.id == "bounded_process"
                and node.func.attr == "run"
            ):
                migrated.append(f"{path.name}:{node.lineno}")
                keywords = {keyword.arg for keyword in node.keywords}
                if "timeout" not in keywords:
                    missing_bounds.append(f"{path.name}:{node.lineno}")
                assert "shell" not in keywords

    assert not forbidden
    assert not missing_bounds
    assert len(migrated) >= 13
