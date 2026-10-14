"""End-to-end: head client submits → runner drains → head client reads the receipt.

Proves the two independently-built halves (dispatch_client, dispatch_runner) interlock
through the queue, with worker execution faked so no real sandbox/worker is spawned.
"""

from __future__ import annotations

from codex_seats import dispatch_client as client
from codex_seats import dispatch_protocol as dp
from codex_seats import dispatch_runner as runner
from codex_seats.workers import RunResult
from codex_seats.worktree_task import TaskArtifact
from registry_fixture import registry_settings, write_settings

NOW = 1200.0
RELEASE_ID = "a" * 64


def _fake_execute(root, settings, request, *, retain):
    result = RunResult(
        code=0, status="completed", provider="codex", worker="seat4",
        label=request["label"], duration=3, tokens=42, message="ok",
    )
    return TaskArtifact(result=result, changed_files=["x.py"], diff_text="d\n",
                        worktree_path=root / "wt", base="b" * 40, branch="br", retained=retain)


def test_submit_drain_receipt_roundtrip(tmp_path):
    package = tmp_path / "releases" / RELEASE_ID / "lib" / "codex_seats"
    package.mkdir(parents=True)
    (tmp_path / "current").symlink_to(f"releases/{RELEASE_ID}")
    settings = registry_settings()
    settings["projects"] = {"sample-project": {"path": str(tmp_path / "repo")}}
    write_settings(tmp_path, settings)
    rid = client.submit_task(
        tmp_path, settings,
        repo_id="sample-project", prompt="implement the thing",
        origin_head="codex", origin_seat="seat1", now=NOW,
    )
    # the head would be asleep here; the launchd-activated runner drains:
    outcomes = runner.drain_once(tmp_path, settings, now_fn=lambda: NOW, execute=_fake_execute)
    assert outcomes == [(rid, "completed")]
    # head wakes on its blocking wait and reads the terminal receipt:
    receipt = client.wait_for_receipt(tmp_path, rid, wait_timeout=1)
    assert receipt["status"] == "completed"
    assert receipt["seat"] == "seat4"
    assert receipt["origin"] == {"head": "codex", "seat": "seat1"}


def test_queued_jobs_keep_their_release_pin_across_current_switch(tmp_path):
    first_release = "a" * 64
    second_release = "b" * 64
    for release_id in (first_release, second_release):
        (tmp_path / "releases" / release_id).mkdir(parents=True)
    current = tmp_path / "current"
    current.symlink_to(f"releases/{first_release}")
    settings = registry_settings()
    settings["projects"] = {"sample-project": {"path": str(tmp_path / "repo")}}
    write_settings(tmp_path, settings)

    first = client.submit_task(
        tmp_path,
        settings,
        repo_id="sample-project",
        prompt="first",
        origin_head="codex",
        now=NOW,
    )
    replacement = tmp_path / ".current.next"
    replacement.symlink_to(f"releases/{second_release}")
    replacement.replace(current)
    second = client.submit_task(
        tmp_path,
        settings,
        repo_id="sample-project",
        prompt="second",
        origin_head="codex",
        now=NOW,
    )

    calls = []

    def execute(root, settings, request, *, retain):
        calls.append((request["prompt"], request["release_id"]))
        return _fake_execute(root, settings, request, retain=retain)

    runner.drain_once(tmp_path, settings, now_fn=lambda: NOW, execute=execute)
    assert dict(calls) == {"first": first_release, "second": second_release}
    assert dp.read_receipt(tmp_path, first)["release_id"] == first_release
    assert dp.read_receipt(tmp_path, second)["release_id"] == second_release
