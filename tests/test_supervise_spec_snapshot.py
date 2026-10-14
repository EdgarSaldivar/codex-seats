"""The GoalSpec snapshot is parsed from one read."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_seats import cli, heads, supervisor


def _spec(tmp_path: Path, goal: str) -> bytes:
    return json.dumps({
        "goal": goal,
        "repo": str(tmp_path / "repo"),
        "acceptance": ["true"],
        "review": "none",
    }).encode()


@pytest.fixture
def captured(monkeypatch):
    seen: dict[str, object] = {}

    def fake_supervise(spec, _head, **kwargs):
        seen["spec"] = spec
        return supervisor.Outcome("done", 0, "fake", kwargs["branch"])

    monkeypatch.setattr(heads, "dispatch_head", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(supervisor, "supervise", fake_supervise)
    return seen


def _run(spec_path: Path, *extra: str) -> int:
    args = cli.build_parser().parse_args(
        ["supervise", str(spec_path), "--branch", "goal-test", *extra]
    )
    return cli.cmd_supervise(args, {})


def _rewrite_after_first_read(monkeypatch, spec_path: Path, replacement: bytes):
    reads: list[Path] = []
    original = Path.read_bytes

    def read_bytes(self):
        data = original(self)
        if self == spec_path:
            reads.append(self)
            # A concurrent writer replaces the file right after the vetted read.
            spec_path.write_bytes(replacement)
        return data

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    return reads


def test_supervise_reads_spec_once(tmp_path, monkeypatch, captured):
    spec_path = tmp_path / "goal.json"
    spec_path.write_bytes(_spec(tmp_path, "first read"))
    reads = _rewrite_after_first_read(monkeypatch, spec_path, b"{not json")

    assert _run(spec_path) == 0
    assert captured["spec"].goal == "first read"
    assert len(reads) == 1
