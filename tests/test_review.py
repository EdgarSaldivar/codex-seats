"""Small review boundary: strict verdicts, provider claims, and read-only calls."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from codex_seats import bounded_process, review, review_identity
from codex_seats.cli import build_parser


APPROVE = {"verdict": "approve", "findings": []}
BLOCK = {"verdict": "block", "findings": [
    {"file": "a.py", "line": 3, "severity": "high", "claim": "bad call", "blocking": True}
]}


def test_strict_result_and_blocker_retention(monkeypatch):
    assert review._result(json.dumps(APPROVE))["verdict"] == "approve"
    for bad in ("null", "[]", "{}", '{"verdict":"approve"}',
                '{"verdict":"maybe","findings":[]}',
                '{"verdict":"approve","findings":[{}]}'):
        with pytest.raises((ValueError, TypeError)):
            review._result(bad)
    monkeypatch.setattr(review, "_call", lambda family, *a, **k: BLOCK if family == "codex" else APPROVE)
    result = review.review("diff", "spec", implementer="codex", strategy="cross")
    assert result["verdict"] == "block"
    assert [row["family"] for row in result["reviewers"]] == ["codex", "claude"]
    assert result["findings"][0]["claim"] == "bad call"


def test_single_fallback_only_for_unavailable_family(monkeypatch):
    calls = []

    def caller(family, *a, **k):
        calls.append(family)
        if family == "claude":
            raise review_identity.ReviewUnavailable("off")
        return APPROVE

    monkeypatch.setattr(review, "_call", caller)
    assert review.review("d", "", implementer="codex", strategy="single")["verdict"] == "approve"
    assert calls == ["claude", "codex"]
    calls.clear()
    assert review.review("d", "", implementer="codex", strategy="cross")["verdict"] == "block"
    assert calls == ["codex", "claude"]


def test_provider_failure_cannot_approve_by_fallback(monkeypatch):
    calls = []

    def caller(family, *a, **k):
        calls.append(family)
        raise OSError("subprocess failed")

    monkeypatch.setattr(review, "_call", caller)
    result = review.review("d", "", implementer="codex", strategy="single")
    assert result["verdict"] == "block"
    assert calls == ["claude"]


def test_claim_released_on_subprocess_exception_and_read_only_argv(monkeypatch, tmp_path):
    events = []

    @contextmanager
    def claim(family, **kwargs):
        events.append(("enter", family))
        try:
            yield review_identity.ReviewIdentity(family, "seat1", tmp_path, {"SAFE": "yes"})
        finally:
            events.append(("exit", family))

    monkeypatch.setattr(review_identity, "claim_review", claim)

    def fake_run(argv, **kwargs):
        assert kwargs["timeout"] == review.TIMEOUT
        assert kwargs["env"] == {"SAFE": "yes"}
        assert kwargs["check"] is True
        assert kwargs["cwd"] == tmp_path
        assert b"DIFF:" in kwargs["input_bytes"]
        if argv[0] == "codex":
            assert ["--sandbox", "read-only"] == argv[argv.index("--sandbox"):argv.index("--sandbox") + 2]
            permissions = argv.index('default_permissions=":read-only"')
            assert argv[permissions - 1] == "-c"
            assert not any(
                arg.startswith("default_permissions=") and arg != 'default_permissions=":read-only"'
                for arg in argv
            )
            assert "danger-full-access" not in " ".join(argv)
            assert 'approval_policy="never"' in argv
            assert "features.multi_agent=false" in argv
            Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps(APPROVE))
            return bounded_process.ProcessResult(tuple(argv), 0, b"", b"", 1)
        assert argv[:3] == ["claude", "-p", "--output-format"]
        assert argv[argv.index("--tools") + 1] == "Read" and "dontAsk" in argv
        assert not any("Grep" in arg or "Glob" in arg for arg in argv)
        assert "--strict-mcp-config" in argv
        assert json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text()) == {"mcpServers": {}}
        assert argv[argv.index("--setting-sources") + 1] == ""
        policy = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
        assert "Read(**/auth.json)" in policy["permissions"]["deny"]
        assert policy["permissions"]["allow"] == []
        return bounded_process.ProcessResult(tuple(argv), 0,
                                             json.dumps({"result": json.dumps(BLOCK)}).encode(), b"", 1)

    monkeypatch.setattr(bounded_process, "run", fake_run)
    assert review._call("codex", "DIFF: x", model=None, seat="seat1", repo=tmp_path)["verdict"] == "approve"
    assert review._call("claude", "DIFF: x", model=None, seat=None, repo=tmp_path)["verdict"] == "block"
    assert events == [("enter", "codex"), ("exit", "codex"),
                      ("enter", "claude"), ("exit", "claude")]

    def broken(*a, **kw):
        raise OSError("crash")

    monkeypatch.setattr(bounded_process, "run", broken)
    with pytest.raises(OSError):
        review._call("codex", "DIFF: x", model=None, seat=None)
    assert events[-1] == ("exit", "codex")


def test_reviewer_argv_pins_selected_model(monkeypatch, tmp_path):
    @contextmanager
    def claim(family, **kwargs):
        yield review_identity.ReviewIdentity(family, None, None, {"SAFE": "yes"})

    monkeypatch.setattr(review_identity, "claim_review", claim)
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(list(argv))
        assert kwargs["cwd"] == tmp_path and kwargs["env"] == {"SAFE": "yes"}
        if argv[0] == "codex":
            Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps(APPROVE))
            return bounded_process.ProcessResult(tuple(argv), 0, b"", b"", 1)
        return bounded_process.ProcessResult(
            tuple(argv), 0, json.dumps({"result": json.dumps(APPROVE)}).encode(), b"", 1)

    monkeypatch.setattr(bounded_process, "run", fake_run)
    review._call("codex", "DIFF: x", model="codex-synthetic", seat=None, repo=tmp_path)
    review._call("claude", "DIFF: x", model="claude-synthetic", seat=None, repo=tmp_path)
    codex_argv, claude_argv = seen
    assert codex_argv[codex_argv.index("-m") + 1] == "codex-synthetic"
    assert 'default_permissions=":read-only"' in codex_argv
    assert claude_argv[claude_argv.index("--model") + 1] == "claude-synthetic"
    assert claude_argv[claude_argv.index("--tools") + 1] == "Read"


def test_cli_stdout_exit_status(monkeypatch, tmp_path, capsys):
    diff = tmp_path / "diff.patch"
    diff.write_text("+x\n")
    monkeypatch.delenv("CODEX_SEATS_MANAGED_WORKER", raising=False)
    monkeypatch.delenv("CLAUDE_LEAD_WORKER", raising=False)
    monkeypatch.setattr(review, "load_settings", lambda root: ({}, []))
    monkeypatch.setattr(review, "_call", lambda *a, **kw: BLOCK)
    assert review.main(["--diff-file", str(diff), "--strategy", "single"]) == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == "block"
    monkeypatch.setattr(review, "_call", lambda *a, **kw: APPROVE)
    assert review.main(["--diff-file", str(diff), "--strategy", "single"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "approve"
    with pytest.raises(SystemExit) as error:
        review.main(["--diff-file", str(diff / "missing"), "--strategy", "single"])
    assert error.value.code == 2


@pytest.mark.parametrize("envelope,approved", [
    ({"structured_output": APPROVE, "result": ""}, True),
    ({"structured_output": BLOCK, "result": json.dumps(APPROVE)}, False),
    ({"structured_output": APPROVE, "is_error": True}, False),
    ({"result": json.dumps(APPROVE), "subtype": "error_max_turns"}, False),
    ({"structured_output": None, "result": json.dumps(APPROVE)}, False),
])
def test_claude_structured_envelope_never_hides_failure(monkeypatch, tmp_path, envelope, approved):
    @contextmanager
    def claim(family, **kwargs):
        yield review_identity.ReviewIdentity(family, None, None, {})

    monkeypatch.setattr(review_identity, "claim_review", claim)
    monkeypatch.setattr(bounded_process, "run", lambda argv, **kw:
        bounded_process.ProcessResult(tuple(argv), 0, json.dumps(envelope).encode(), b"", 1))
    result = review.review("d", "", implementer="codex", strategy="single", repo=tmp_path)
    assert (result["verdict"] == "approve") is approved


def test_cross_model_override_stays_with_codex_and_uses_review_repository(monkeypatch, tmp_path):
    calls = []

    def caller(family, *args, **kwargs):
        calls.append((family, kwargs))
        return APPROVE

    monkeypatch.setattr(review, "_call", caller)
    result = review.review("d", "", implementer="claude", strategy="cross",
                           model="codex-custom", repo=tmp_path, settings={
        "providers": {"claude": {"model": "claude-custom"}},
    })
    assert result["verdict"] == "approve"
    assert [kwargs["model"] for _, kwargs in calls] == ["codex-custom", "claude-custom"]
    assert all(kwargs["repo"] == tmp_path for _, kwargs in calls)


@pytest.mark.parametrize("argv", [
    ["campaign", "list"], ["ship", "status", "intent"],
    ["stats", "--review-replay"], ["goals", "add", "goal.json", "--accept-redteam", "hash"],
])
def test_removed_workflows_are_unavailable(argv):
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(argv)
    assert error.value.code == 2
