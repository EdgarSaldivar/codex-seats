from __future__ import annotations

import os
from pathlib import Path

import pytest

from codex_seats import bounded_process, internal_git


def test_runner_is_bounded_non_shell_noninteractive_and_sanitized(monkeypatch, tmp_path):
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        return bounded_process.ProcessResult(tuple(argv), 0, b" exact \n\n", b"", 1)

    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_EXTERNAL_DIFF",
        "GIT_PAGER",
        "GIT_ASKPASS",
    ):
        monkeypatch.setenv(name, "attacker-controlled")
    monkeypatch.setenv("PAGER", "attacker-pager")
    monkeypatch.setattr(internal_git.bounded_process, "run", fake_run)

    result = internal_git.run(tmp_path, ("status", "--short"), timeout=7.5)

    assert result.stdout == b" exact \n\n"
    assert captured["kwargs"]["timeout"] == 7.5
    assert captured["kwargs"]["input_bytes"] is None
    environment = captured["kwargs"]["env"]
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert environment["GIT_PAGER"] == "cat" and environment["PAGER"] == "cat"
    assert environment["GIT_ASKPASS"] == "/usr/bin/false"
    assert "GIT_DIR" not in environment and "GIT_WORK_TREE" not in environment
    assert "GIT_CONFIG_COUNT" not in environment and "GIT_EXTERNAL_DIFF" not in environment
    argv = captured["argv"]
    assert argv[0] == "git" and "core.hooksPath=/dev/null" in argv
    assert "core.fsmonitor=false" in argv and "diff.external=/usr/bin/false" in argv


def test_exact_input_bytes_are_the_only_way_to_enable_stdin(monkeypatch, tmp_path):
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return bounded_process.ProcessResult(tuple(argv), 0, b"", b"", 1)

    monkeypatch.setattr(internal_git.bounded_process, "run", fake_run)
    internal_git.run(tmp_path, ("apply", "--cached"), timeout=2, input_bytes=b"a\x00b\n")
    assert captured["input_bytes"] == b"a\x00b\n"


def test_typed_timeout_launch_and_command_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(
        internal_git.bounded_process,
        "run",
        lambda argv, **kwargs: (_ for _ in ()).throw(bounded_process.ProcessTimeoutError(
            bounded_process.ProcessResult(tuple(argv), -9, b"", b"", 1), kwargs["timeout"]
        )),
    )
    with pytest.raises(internal_git.GitTimeoutError):
        internal_git.run(tmp_path, ("status",), timeout=1)

    monkeypatch.setattr(
        internal_git.bounded_process,
        "run",
        lambda argv, **_kwargs: (_ for _ in ()).throw(
            bounded_process.ProcessLaunchError("missing", argv)
        ),
    )
    with pytest.raises(internal_git.GitLaunchError):
        internal_git.run(tmp_path, ("status",), timeout=1)

    monkeypatch.setattr(
        internal_git.bounded_process,
        "run",
        lambda argv, **_kwargs: bounded_process.ProcessResult(
            tuple(argv), 9, b"stdout\n\n", b"stderr\n\n", 1
        ),
    )
    with pytest.raises(internal_git.GitCommandError) as raised:
        internal_git.run(tmp_path, ("status",), timeout=1, check=True)
    assert raised.value.result.stderr == b"stderr\n\n"
    assert raised.value.result.stdout == b"stdout\n\n"


def test_overrides_are_typed_and_arbitrary_git_environment_is_not_accepted(tmp_path):
    fields = set(internal_git.GitOverrides.__dataclass_fields__)
    assert fields == {
        "index_file",
        "object_directory",
        "alternate_object_directories",
    }
    with pytest.raises(TypeError):
        internal_git.GitOverrides(GIT_DIR=tmp_path)  # type: ignore[call-arg]


def _init_repo(repo: Path) -> None:
    git_dir = repo / ".git"
    (git_dir / "objects").mkdir(parents=True)
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "refs" / "tags").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (git_dir / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
    )


def test_inherited_redirects_and_global_config_cannot_change_identity(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    repo.mkdir()
    other.mkdir()
    _init_repo(repo)
    _init_repo(other)
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text(f"[core]\n\tworktree = {other}\n")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(other))

    result = internal_git.run(
        repo, ("rev-parse", "--show-toplevel"), timeout=5, check=True
    )

    assert Path(internal_git.line(result.stdout, label="root")) == repo
