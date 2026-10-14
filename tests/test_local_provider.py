from __future__ import annotations

import copy
import json
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_seats import bounded_process, local_seats, provider_local, state
from codex_seats.telemetry import classify_job_telemetry, telemetry_class_counts


def _settings(command: str) -> dict:
    settings = state.default_settings()
    settings["providers"]["local"] = {
        "mode": "opportunistic",
        "command": command,
        "max_turns": 7,
        "repair_budget": 2,
    }
    settings["local_hosts"] = {
        "workstation": {
            "label": "Workstation",
            "lifecycle": "active",
            "resource_groups": {"gpu0": {"max_concurrency": 1}},
            "base_url": "http://127.0.0.1:8080/exact/v1",
            "availability_probe": {
                "argv": ["/usr/bin/true"],
                "timeout_seconds": 1,
            },
        }
    }
    seat = {
        "label": "Qwen coder",
        "lifecycle": "active",
        "mode": "opportunistic",
        "host": "workstation",
        "resource_group": "gpu0",
        "model_profile": "qwen-coder-local",
        "harness": "qwen",
        "capabilities": {
            "task_shapes": ["execute", "investigate", "design"],
            "validation": ["strong", "weak"],
        },
        "max_concurrency": 1,
        "runtime_fingerprint": "sha256:" + "a" * 64,
    }
    settings["local_seats"] = {
        "coder-a": seat,
        "coder-b": copy.deepcopy(seat),
    }
    return settings


def _receipt(status: str = "completed") -> dict:
    return {
        "status": status,
        "summary": "local task done",
        "changed_files": ["example.py"],
        "test_evidence": ["pytest passed"],
        "warnings": [],
        "needs_controller": None,
    }


def _result_document(receipt: dict | None = None) -> list[dict]:
    receipt = receipt or _receipt()
    return [
        {"type": "system", "subtype": "session_start"},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 1250,
            "num_turns": 3,
            "usage": {
                "input_tokens": 101,
                "output_tokens": 23,
                "cache_read_input_tokens": 11,
                "cache_creation_input_tokens": 2,
                "total_tokens": 124,
            },
            "result": json.dumps(receipt, separators=(",", ":")),
            "structured_result": receipt,
        },
    ]


def _fake_qwen(
    tmp_path: Path,
    output: object,
    *,
    sleep: float = 0,
    exit_code: int = 0,
    stderr: str = "",
) -> Path:
    executable = tmp_path / "fake-qwen"
    capture = tmp_path / "capture.json"
    script = f"""#!{sys.executable}
import json, os, pathlib, sys, time
args = sys.argv[1:]
schema_arg = args[args.index('--json-schema') + 1]
settings_path = pathlib.Path(os.environ['QWEN_CODE_SYSTEM_SETTINGS_PATH'])
mcp_path = pathlib.Path(args[args.index('--mcp-config') + 1])
profile_path = pathlib.Path(os.getcwd()) / '.qwen' / 'sandbox-macos-codex-seats.sb'
capture = {{
    'argv': args,
    'env': dict(os.environ),
    'cwd': os.getcwd(),
    'settings': json.loads(settings_path.read_text()),
    'schema': json.loads(pathlib.Path(schema_arg[1:]).read_text()),
    'mcp': json.loads(mcp_path.read_text()),
    'seatbelt_profile': profile_path.read_text() if profile_path.exists() else None,
}}
pathlib.Path({str(capture)!r}).write_text(json.dumps(capture))
time.sleep({sleep!r})
value = {output!r}
if isinstance(value, list):
    for event in value:
        print(json.dumps(event, ensure_ascii=False))
elif isinstance(value, dict):
    print(json.dumps(value, ensure_ascii=False))
else:
    print(value, end='')
print({stderr!r}, file=sys.stderr, end='')
sys.exit({exit_code!r})
"""
    executable.write_text(script)
    executable.chmod(0o700)
    return executable


def _ready(settings: dict) -> dict:
    host = settings["local_hosts"]["workstation"]
    return {
        "host": "workstation",
        "state": "ready",
        "reason": "ready",
        "observed_at": time.time(),
        "config_digest": local_seats._host_digest(host),
        "model_profile": "qwen-coder-local",
    }


def _request(repository: Path, **overrides) -> dict:
    request = {
        "prompt": "Implement the bounded task.",
        "label": "local-test",
        "seat": "coder-a",
        "cd": str(repository),
        "kind": "implementation",
        "task_shape": "execute",
        "validation": "strong",
        "task_id": "task-17",
        "attempt_id": "attempt-2",
        "request_id": "request-2",
        "review_cycle": 4,
        "write_scope": [{"path": ".", "type": "tree"}],
    }
    request.update(overrides)
    return request


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    return repository


def _run_ready(monkeypatch, root, settings, request):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Fake Qwen inspects generated arguments/profiles; native containment has a
    # separate macOS acceptance job. Keep these contract fixtures portable.
    real_shutil = provider_local.shutil
    monkeypatch.setattr(provider_local, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(provider_local, "shutil", SimpleNamespace(
        which=lambda command: "/synthetic/sandbox-exec" if command == "sandbox-exec"
        else real_shutil.which(command),
        rmtree=real_shutil.rmtree,
    ))
    monkeypatch.setattr(
        provider_local.local_seats,
        "probe_host",
        lambda *args, **kwargs: _ready(settings),
    )
    return provider_local.run_local(root, settings, request)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_missing_native_sandbox_refuses_launch(tmp_path, repository, monkeypatch, platform):
    root = tmp_path / "control"
    root.mkdir(mode=0o700)
    executable = _fake_qwen(tmp_path, _result_document())
    settings = _settings(str(executable))
    real_shutil = provider_local.shutil
    monkeypatch.setattr(provider_local, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(provider_local, "shutil", SimpleNamespace(
        which=lambda command: None if command == "sandbox-exec" else real_shutil.which(command),
        rmtree=real_shutil.rmtree,
    ))
    monkeypatch.setattr(provider_local.local_seats, "probe_host", lambda *_args, **_kwargs: _ready(settings))
    result = provider_local.run_local(root, settings, _request(repository))
    assert result.status == "failed" and result.fallback_safe is True, result.message
    assert "requires macOS sandbox-exec" in result.message
    assert not (tmp_path / "capture.json").exists()


def test_exact_argv_env_config_isolation_and_structured_usage(
    tmp_path, repository, monkeypatch
):
    executable = _fake_qwen(tmp_path, _result_document())
    settings = _settings(str(executable))
    settings["local_seats"]["coder-a"]["context_window_size"] = 32768
    settings["execution"]["worker_access"] = "restricted"
    result = _run_ready(
        monkeypatch,
        tmp_path / "control",
        settings,
        _request(repository, network_policy={"web_fetch": True}),
    )

    assert result.status == "completed"
    assert result.provider == "local" and result.worker == "coder-a"
    assert result.model == "qwen-coder-local"
    assert result.tokens == 124 and result.structured_result["status"] == "completed"
    assert result.structured_result["worker_access"] == "restricted"
    capture = json.loads((tmp_path / "capture.json").read_text())
    argv = capture["argv"]
    assert argv[:6] == [
        "--auth-type",
        "openai",
        "--openai-base-url",
        "http://127.0.0.1:8080/exact/v1",
        "--model",
        "qwen-coder-local",
    ]
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--max-session-turns") + 1] == "7"
    assert argv[argv.index("--approval-mode") + 1] == "yolo"
    assert "--bare" not in argv and "--chat-recording=false" in argv
    excluded = set(argv[argv.index("--exclude-tools") + 1].split(","))
    assert "--allowed-tools" not in argv
    assert {
        "agent",
        "skill",
        "save_memory",
        "cron_create",
        "monitor",
        "task_stop",
        "ask_user_question",
        "enter_worktree",
        "exit_worktree",
        "send_message",
        "tool_search",
    } <= excluded
    assert capture["cwd"] != str(repository)
    env = capture["env"]
    assert env["OPENAI_API_KEY"] == "local-no-auth"
    assert env["HOME"] == env["QWEN_HOME"]
    assert env["QWEN_HOME"].startswith(str(tmp_path / "control"))
    assert env["QWEN_RUNTIME_DIR"].startswith(str(tmp_path / "control"))
    assert env["QWEN_SANDBOX"] == "sandbox-exec"
    assert env["SEATBELT_PROFILE"] == "codex-seats"
    assert "SSH_AUTH_SOCK" not in env and "CLAUDE_CONFIG_DIR" not in env
    assert not any(key.endswith("TOKEN") for key in env)
    config = capture["settings"]
    assert config["telemetry"]["enabled"] is False
    assert config["general"]["chatRecording"] is False
    assert config["memory"] == {
        "enableManagedAutoMemory": False,
        "enableManagedAutoDream": False,
        "enableAutoSkill": False,
        "enableTeamMemory": False,
        "enableTeamMemorySync": False,
    }
    assert config["disableAllHooks"] is True and config["hooks"] == {}
    assert config["experimental"]["cron"] is False
    provider = config["modelProviders"]["openai"][0]
    assert provider["id"] == "qwen-coder-local"
    assert provider["baseUrl"] == "http://127.0.0.1:8080/exact/v1"
    assert provider["generationConfig"]["extra_body"]["enable_thinking"] is True
    assert provider["generationConfig"]["contextWindowSize"] == 32768
    assert config["model"]["skipLoopDetection"] is True
    assert capture["mcp"] == {"mcpServers": {}}
    profile = capture["seatbelt_profile"]
    assert "(deny file-write*)" in profile
    assert str(repository) in profile
    assert str(tmp_path / "control") in profile
    launch_parent = Path(env["QWEN_HOME"]).parent.parent
    assert (
        f'(allow file-read-metadata (literal "{launch_parent}"))' in profile
    )
    assert f'(allow file-read* (subpath "{launch_parent}"))' not in profile
    for device in ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"):
        assert device in profile
    assert "seat[^/]*" in profile
    assert r"auth\\.json" in profile
    metadata = json.loads(
        (tmp_path / "control" / "logs" / "local-coder-a-attempt-2.local-metadata.json").read_text()
    )
    assert metadata["task_id"] == "task-17"
    assert metadata["review_cycle"] == 4
    assert metadata["host"] == "workstation" and metadata["resource_group"] == "gpu0"
    assert metadata["runtime_fingerprint"] == "sha256:" + "a" * 64
    assert metadata["local_policy_version"] == provider_local.LOCAL_POLICY_VERSION
    assert metadata["local_input_tokens"] == 101
    assert metadata["local_cache_read_input_tokens"] == 11
    assert metadata["local_total_tokens"] == 124
    ledger = json.loads((tmp_path / "control" / "seats.json").read_text())
    job = ledger["jobs"][-1]
    assert job["provider"] == "local" and job["seat"] == "coder-a"
    assert job["provider_tokens"] == job["input_tokens"] + job["output_tokens"] == 124
    assert job["input_tokens"] == job["cached_input_tokens"] + job["uncached_input_tokens"]


def test_qwen_settings_omit_context_window_when_seat_has_no_override():
    config = provider_local._qwen_settings(
        {}, model="qwen-coder-local", base_url="http://127.0.0.1:8080/v1"
    )
    generation = config["modelProviders"]["openai"][0]["generationConfig"]
    assert generation == {"extra_body": {"enable_thinking": True}}


def test_stream_json_success_records_safe_progress(tmp_path, repository, monkeypatch):
    events = _result_document()
    events.insert(
        1,
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "private prompt content"},
                    {
                        "type": "tool_use",
                        "name": "read_file",
                        "input": {"path": "private-path"},
                    },
                ],
            },
        },
    )
    events.insert(
        2,
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "is_error": True,
                        "content": "private tool error",
                    }
                ],
            },
        },
    )
    executable = _fake_qwen(tmp_path, events)
    root = tmp_path / "control"
    result = _run_ready(
        monkeypatch, root, _settings(str(executable)), _request(repository)
    )
    assert result.status == "completed" and result.tokens == 124
    metadata = json.loads(
        (root / "logs" / "local-coder-a-attempt-2.local-metadata.json").read_text()
    )
    assert metadata["qwen_event_count"] == 4
    assert metadata["qwen_tool_call_count"] == 1
    assert metadata["qwen_tool_names"] == ["read_file"]
    assert metadata["qwen_tool_call_counts"] == {"read_file": 1}
    assert metadata["qwen_last_tool_names"] == ["read_file"]
    assert metadata["qwen_tool_error_count"] == 1
    assert metadata["local_usage_available"] is True
    assert "private prompt content" not in json.dumps(metadata)
    assert "private-path" not in json.dumps(metadata)
    assert "private tool error" not in json.dumps(metadata)


def test_stream_json_keeps_unicode_line_separators_inside_event_strings(
    tmp_path, repository, monkeypatch
):
    events = _result_document()
    events.insert(
        1,
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "is_error": False,
                        "content": "first\u2028second\u0085third",
                    }
                ],
            },
        },
    )
    executable = _fake_qwen(tmp_path, events)
    result = _run_ready(
        monkeypatch,
        tmp_path / "control",
        _settings(str(executable)),
        _request(repository),
    )
    assert result.status == "completed"
    assert result.tokens == 124


def test_unset_local_turn_limit_uses_large_task_budget(tmp_path, repository, monkeypatch):
    executable = _fake_qwen(tmp_path, _result_document())
    settings = _settings(str(executable))
    settings["providers"]["local"].pop("max_turns")
    result = _run_ready(monkeypatch, tmp_path / "control", settings, _request(repository))
    assert result.status == "completed"
    argv = json.loads((tmp_path / "capture.json").read_text())["argv"]
    assert argv[argv.index("--max-session-turns") + 1] == "100"


@pytest.mark.parametrize(
    "events",
    [
        [],
        [
            {"type": "system", "subtype": "session_start"},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "run_shell_command",
                            "input": {"command": "private command"},
                        },
                    ],
                },
            },
        ],
    ],
)
def test_exit_53_without_result_reports_turn_limit_and_unavailable_usage(
    tmp_path, repository, monkeypatch, events
):
    root = tmp_path / "control"
    executable = _fake_qwen(
        tmp_path, events, exit_code=53, stderr="Reached max session turns"
    )
    result = _run_ready(
        monkeypatch, root, _settings(str(executable)), _request(repository)
    )
    assert result.status == "failed" and result.fallback_safe is False
    assert "max-session-turns" in result.message
    assert "malformed" not in result.message
    assert result.tokens is None and result.structured_result is None
    metadata = json.loads(
        (root / "logs" / "local-coder-a-attempt-2.local-metadata.json").read_text()
    )
    assert metadata["local_usage_available"] is False
    assert metadata["local_total_tokens"] is None
    assert metadata["qwen_num_turns"] is None
    assert metadata["qwen_exit_code"] == 53
    assert metadata["qwen_event_count"] == len(events)
    assert metadata["qwen_tool_call_count"] == (1 if events else 0)
    assert metadata["qwen_tool_names"] == (["run_shell_command"] if events else [])
    assert metadata["qwen_tool_call_counts"] == ({"run_shell_command": 1} if events else {})
    assert "private command" not in json.dumps(metadata)
    job = json.loads((root / "seats.json").read_text())["jobs"][-1]
    assert job["local_usage_available"] is False
    assert job["provider_tokens"] is None and job["input_tokens"] is None
    assert job["qwen_event_count"] == len(events)
    assert classify_job_telemetry(job) == "usage-unavailable"
    assert telemetry_class_counts([job])["usage-unavailable"] == 1
    assert "private command" not in json.dumps(job)
    transcript = root / "logs" / "local-coder-a-attempt-2.qwen.json"
    assert transcript.stat().st_mode & 0o777 == 0o600
    assert b"Reached max session turns" in transcript.read_bytes()


def test_exit_53_with_truncated_event_fails_closed(tmp_path, repository, monkeypatch):
    executable = _fake_qwen(
        tmp_path,
        '{"type":"system","subtype":"session_start"}\n{"type":"assistant"',
        exit_code=53,
    )
    result = _run_ready(
        monkeypatch,
        tmp_path / "control",
        _settings(str(executable)),
        _request(repository),
    )
    assert result.status == "failed" and result.fallback_safe is False
    assert "malformed JSONL" in result.message
    assert result.tokens is None


def test_read_only_and_network_default_narrow_tools(tmp_path, repository, monkeypatch):
    executable = _fake_qwen(tmp_path, _result_document())
    settings = _settings(str(executable))
    result = _run_ready(
        monkeypatch,
        tmp_path / "control",
        settings,
        _request(repository, sandbox="read-only"),
    )
    assert result.status == "completed"
    argv = json.loads((tmp_path / "capture.json").read_text())["argv"]
    excluded = set(argv[argv.index("--exclude-tools") + 1].split(","))
    assert "--allowed-tools" not in argv
    assert {"edit", "write_file", "notebook_edit"} <= excluded
    assert argv[argv.index("--include-directories") + 1] == str(repository)
    assert argv[argv.index("--prompt") + 1].endswith("\n\nImplement the bounded task.")


@pytest.mark.parametrize(
    "document",
    [
        "not-json",
        '{"type":"assistant"',
        '{"type":"system"}\nnot-json',
        '{"type":"system","value":NaN}',
        [{"type": "assistant", "message": "not an event"}, _result_document()[-1]],
        [],
        [{"type": "result", "subtype": "success", "is_error": False}],
        _result_document() + [_result_document()[-1]],
        [
            {
                **_result_document()[-1],
                "is_error": True,
            }
        ],
        [
            {
                **_result_document()[-1],
                "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 5},
            }
        ],
        [
            {
                **_result_document()[-1],
                "structured_result": {**_receipt(), "extra": True},
            }
        ],
    ],
)
def test_malformed_or_contradictory_output_fails_closed(
    tmp_path, repository, monkeypatch, document
):
    executable = _fake_qwen(tmp_path, document)
    settings = _settings(str(executable))
    result = _run_ready(
        monkeypatch, tmp_path / "control", settings, _request(repository)
    )
    assert result.status == "failed"
    assert result.fallback_safe is False
    assert result.structured_result is None


def test_timeout_cancel_and_output_bound_are_post_start_unsafe(
    tmp_path, repository, monkeypatch
):
    slow = _fake_qwen(tmp_path, _result_document(), sleep=2)
    settings = _settings(str(slow))
    timeout = _run_ready(
        monkeypatch,
        tmp_path / "timeout-control",
        settings,
        _request(repository, timeout=1),
    )
    assert timeout.status == "timeout" and timeout.fallback_safe is False
    assert "Implement the bounded task" not in timeout.message

    monkeypatch.setattr(
        provider_local.bounded_process,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            bounded_process.ProcessCancelledError("cancelled by controller")
        ),
    )
    (tmp_path / "cancel-control").mkdir(mode=0o700)
    cancelled = provider_local.run_local(
        tmp_path / "cancel-control", settings, _request(repository)
    )
    assert cancelled.status == "cancelled" and cancelled.fallback_safe is False

    noisy = _fake_qwen(tmp_path, "x" * 200)
    noisy_settings = _settings(str(noisy))
    monkeypatch.undo()
    monkeypatch.setattr(
        provider_local.local_seats,
        "probe_host",
        lambda *args, **kwargs: _ready(noisy_settings),
    )
    monkeypatch.setattr(provider_local, "MAX_QWEN_OUTPUT_BYTES", 100)
    (tmp_path / "bound-control").mkdir(mode=0o700)
    oversized = provider_local.run_local(
        tmp_path / "bound-control", noisy_settings, _request(repository)
    )
    assert oversized.status == "failed" and oversized.fallback_safe is False
    assert "output limit" in oversized.message


def test_lost_host_lease_is_recorded_as_preemption_and_requeued_signal(
    tmp_path, repository, monkeypatch
):
    executable = _fake_qwen(tmp_path, _result_document())
    settings = _settings(str(executable))
    settings["local_hosts"]["workstation"]["lease_url"] = (
        "http://127.0.0.1:8002"
    )
    lost = threading.Event()
    lost.set()

    @contextmanager
    def fake_hold(_url):
        yield SimpleNamespace(lost=lost)

    monkeypatch.setattr(provider_local.local_lease, "hold", fake_hold)
    original_run = provider_local.bounded_process.run

    def cancel_qwen(*args, **kwargs):
        if kwargs.get("cancel_event") is lost:
            raise bounded_process.ProcessCancelledError("host lease lost")
        return original_run(*args, **kwargs)

    monkeypatch.setattr(
        provider_local.bounded_process,
        "run",
        cancel_qwen,
    )
    result = _run_ready(
        monkeypatch,
        tmp_path / "control",
        settings,
        _request(repository),
    )

    assert result.status == "unavailable"
    assert result.fallback_safe is False
    assert "lease" in result.message
    ledger = json.loads((tmp_path / "control" / "seats.json").read_text())
    assert ledger["jobs"][-1]["status"] == "preempted"


def test_global_and_seat_off_do_no_probe_or_qwen_discovery(
    tmp_path, repository, monkeypatch
):
    settings = _settings("qwen-does-not-exist")
    settings["providers"]["local"]["mode"] = "off"
    monkeypatch.setattr(
        provider_local,
        "_reload_settings",
        lambda *args: pytest.fail("global off reloaded settings"),
    )
    monkeypatch.setattr(
        provider_local.local_seats,
        "probe_host",
        lambda *args, **kwargs: pytest.fail("global off probed"),
    )
    monkeypatch.setattr(
        provider_local,
        "_resolve_qwen",
        lambda *args: pytest.fail("global off discovered Qwen"),
    )
    result = provider_local.run_local(tmp_path / "global", settings, _request(repository))
    assert result.status == "disabled" and result.fallback_safe is True
    assert not (tmp_path / "global").exists()

    monkeypatch.undo()
    settings = _settings("qwen-does-not-exist")
    settings["local_seats"]["coder-a"]["mode"] = "off"
    monkeypatch.setattr(
        provider_local.local_seats,
        "probe_host",
        lambda *args, **kwargs: pytest.fail("off seat probed"),
    )
    monkeypatch.setattr(
        provider_local,
        "_resolve_qwen",
        lambda *args: pytest.fail("off seat discovered Qwen"),
    )
    result = provider_local.run_local(tmp_path / "seat", settings, _request(repository))
    assert result.status == "disabled" and result.fallback_safe is True


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda settings, request: settings["local_seats"]["coder-a"].update(lifecycle="inactive", mode="off"), "off"),
        (lambda settings, request: settings["local_seats"]["coder-a"].update(harness="local-cli"), "harness"),
        (lambda settings, request: request.update(task_shape="design", validation="strong") or settings["local_seats"]["coder-a"]["capabilities"].update(task_shapes=["execute"]), "capability"),
        (lambda settings, request: request.update(model="another-model"), "model"),
    ],
)
def test_lifecycle_capability_and_model_mismatch_are_prestart_fallback_safe(
    tmp_path, repository, monkeypatch, mutation, message
):
    settings = _settings("qwen-does-not-exist")
    request = _request(repository)
    mutation(settings, request)
    monkeypatch.setattr(
        provider_local,
        "_resolve_qwen",
        lambda *args: pytest.fail("prestart mismatch discovered Qwen"),
    )
    result = provider_local.run_local(tmp_path / "control", settings, request)
    assert result.fallback_safe is True
    assert message in result.message


@pytest.mark.parametrize("state_name", ["gaming", "asleep", "loading", "busy", "degraded"])
def test_nonready_probe_states_fail_without_qwen(
    tmp_path, repository, monkeypatch, state_name
):
    settings = _settings("qwen-does-not-exist")
    availability = _ready(settings) | {
        "state": state_name,
        "reason": "host_not_ready",
    }
    monkeypatch.setattr(
        provider_local.local_seats, "probe_host", lambda *args, **kwargs: availability
    )
    monkeypatch.setattr(
        provider_local,
        "_resolve_qwen",
        lambda *args: pytest.fail("unavailable seat discovered Qwen"),
    )
    result = provider_local.run_local(
        tmp_path / "control", settings, _request(repository)
    )
    assert result.status == "unavailable" and result.fallback_safe is True
    assert state_name in result.message


def test_claim_contention_and_shared_gpu_are_fallback_safe(
    tmp_path, repository, monkeypatch
):
    settings = _settings("qwen-does-not-exist")
    availability = _ready(settings)
    monkeypatch.setattr(
        provider_local.local_seats, "probe_host", lambda *args, **kwargs: availability
    )
    with local_seats.claim(
        tmp_path,
        settings,
        "coder-b",
        availability,
        task_shape="execute",
        validation="strong",
    ) as held:
        assert held["acquired"] is True
        result = provider_local.run_local(tmp_path, settings, _request(repository))
    assert result.status == "unavailable" and result.fallback_safe is True
    assert "capacity_claimed" in result.message


def test_access_resolved_once_and_cannot_be_widened(tmp_path, repository, monkeypatch):
    executable = _fake_qwen(tmp_path, _result_document())
    settings = _settings(str(executable))
    settings["execution"]["worker_access"] = "trusted-development"
    calls = 0
    original = provider_local.resolve_worker_access

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(provider_local, "resolve_worker_access", counted)
    result = _run_ready(
        monkeypatch,
        tmp_path / "control",
        settings,
        _request(repository, sandbox="read-only"),
    )
    assert calls == 1
    assert result.structured_result["worker_access"] == "restricted"
    capture = json.loads((tmp_path / "capture.json").read_text())
    assert capture["env"]["CODEX_SEATS_WORKER_ACCESS"] == "restricted"
    assert capture["env"]["QWEN_SANDBOX"] == "sandbox-exec"


def test_trusted_development_is_narrowed_to_restricted_os_sandbox(
    tmp_path, repository, monkeypatch
):
    executable = _fake_qwen(tmp_path, _result_document())
    settings = _settings(str(executable))
    settings["execution"]["worker_access"] = "trusted-development"
    result = _run_ready(
        monkeypatch, tmp_path / "control", settings, _request(repository)
    )
    assert result.status == "completed"
    assert result.structured_result["worker_access"] == "restricted"
    capture = json.loads((tmp_path / "capture.json").read_text())
    assert capture["env"]["CODEX_SEATS_WORKER_ACCESS"] == "restricted"
    assert capture["env"]["QWEN_SANDBOX"] == "sandbox-exec"
    assert capture["seatbelt_profile"] is not None


def test_lease_acquisition_failure_is_retry_safe_capacity(
    tmp_path, repository, monkeypatch
):
    executable = _fake_qwen(tmp_path, _result_document())
    settings = _settings(str(executable))
    settings["local_hosts"]["workstation"]["lease_url"] = "http://127.0.0.1:8002"

    def unavailable(_url):
        raise provider_local.local_lease.LocalLeaseUnavailable("host asleep")

    monkeypatch.setattr(provider_local.local_lease, "hold", unavailable)
    result = _run_ready(
        monkeypatch, tmp_path / "control", settings, _request(repository)
    )
    assert result.status == "unavailable"
    assert result.fallback_safe is True
    assert "asleep" in result.message


def test_sandbox_exit_before_agent_output_is_retry_safe(
    tmp_path, repository, monkeypatch
):
    executable = _fake_qwen(tmp_path, _result_document())
    settings = _settings(str(executable))
    monkeypatch.setattr(
        provider_local.bounded_process,
        "run",
        lambda *args, **kwargs: bounded_process.ProcessResult(
            tuple(args[0]), 71, b"", b"sandbox-exec: cannot apply profile", 123
        ),
    )
    result = _run_ready(
        monkeypatch, tmp_path / "control", settings, _request(repository)
    )
    assert result.status == "unavailable"
    assert result.fallback_safe is True
    assert "sandbox" in result.message


def test_model_profile_from_probe_must_match_before_claim(
    tmp_path, repository, monkeypatch
):
    settings = _settings("qwen-does-not-exist")
    mismatch = _ready(settings) | {"model_profile": "other-profile"}
    monkeypatch.setattr(
        provider_local.local_seats, "probe_host", lambda *args, **kwargs: mismatch
    )
    result = provider_local.run_local(
        tmp_path / "control", settings, _request(repository)
    )
    assert result.status == "unavailable" and result.fallback_safe is True
    assert "model_profile_mismatch" in result.message


def test_explicit_mcp_is_filtered_through_repository_catalog(
    tmp_path, repository, monkeypatch
):
    executable = _fake_qwen(tmp_path, _result_document())
    settings = _settings(str(executable))
    config = repository / ".seats"
    config.mkdir(mode=0o700)
    (config / "worker-mcp.json").write_text(
        json.dumps(
            {
                "servers": {
                    "repo-tools": {
                        "command": "bin/repo-mcp",
                        "args": ["--stdio"],
                        "env": {"MODE": "readonly"},
                    }
                }
            }
        )
    )
    result = _run_ready(
        monkeypatch,
        tmp_path / "control",
        settings,
        _request(repository, mcp=["repo-tools"]),
    )
    assert result.status == "completed"
    capture = json.loads((tmp_path / "capture.json").read_text())
    assert capture["mcp"]["mcpServers"] == {
        "repo-tools": {
            "command": "bin/repo-mcp",
            "args": ["--stdio"],
            "env": {"MODE": "readonly"},
        }
    }
    argv = capture["argv"]
    assert argv[argv.index("--allowed-mcp-server-names") + 1] == "repo-tools"

    (tmp_path / "other-control").mkdir(mode=0o700)
    failed = provider_local.run_local(
        tmp_path / "other-control",
        settings,
        _request(repository, mcp=["not-granted"]),
    )
    assert failed.status == "failed" and failed.fallback_safe is True
    assert "not in the repository catalog" in failed.message
