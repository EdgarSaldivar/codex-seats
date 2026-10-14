from __future__ import annotations

import json

import pytest

from codex_seats import bounded_process, dispatch_child, dispatch_runner, releases


FIRST = "a" * 64
SECOND = "b" * 64


def _release_dirs(root):
    for release_id in (FIRST, SECOND):
        (root / "releases" / release_id).mkdir(parents=True)


def test_managed_child_cannot_override_the_trusted_environment_pin(
    tmp_path, monkeypatch
):
    _release_dirs(tmp_path)
    monkeypatch.setenv("CODEX_SEATS_ROOT", str(tmp_path))
    monkeypatch.setenv("CODEX_SEATS_RELEASE_ID", FIRST)
    with pytest.raises(releases.ReleaseError, match="cannot override"):
        dispatch_child.execute_from_record(
            {
                "request": {"release_id": SECOND},
                "retain": False,
            }
        )


def test_parent_forces_its_independent_pin_over_internal_request(
    tmp_path, monkeypatch
):
    _release_dirs(tmp_path)
    sent = {}
    monkeypatch.setattr(releases, "verify_release", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        releases,
        "release_module_argv",
        lambda *_args, **_kwargs: ["/python", "dispatch-child"],
    )

    artifact = {
        "result": {
            "code": 0,
            "status": "completed",
            "provider": "codex",
            "worker": "seat2",
            "label": "job",
        },
        "changed_files": [],
        "diff_text": "",
        "worktree_path": None,
        "base": None,
        "branch": None,
        "retained": False,
    }

    def run(argv, **kwargs):
        sent["argv"] = argv
        sent["request"] = json.loads(kwargs["input_bytes"])
        sent["env"] = kwargs["env"]
        sent["timeout"] = kwargs["timeout"]
        return bounded_process.ProcessResult(
            tuple(argv), 0, json.dumps(artifact).encode(), b"", 1
        )

    monkeypatch.setattr(dispatch_runner.bounded_process, "run", run)
    result = dispatch_runner.run_pinned_release_task(
        tmp_path,
        {"prompt": "x", "label": "job", "timeout": 1, "release_id": SECOND},
        FIRST,
        retain=False,
    )
    assert result.result.status == "completed"
    assert sent["request"]["request"]["release_id"] == FIRST
    assert sent["env"]["CODEX_SEATS_RELEASE_ID"] == FIRST
    assert sent["request"]["request"]["timeout"] == 1
    assert sent["timeout"] == 61
