from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import sys
from contextlib import contextmanager

import pytest

from codex_seats import bounded_process, cli, local_cli, local_seats as local, state
from codex_seats.controller_context import current_controller
from codex_seats.seat_registry import validate_local_registries

NOW = 1200.0


def configured():
    settings = state.default_settings()
    settings["providers"]["local"]["mode"] = "opportunistic"
    settings["local_hosts"] = {"studio": {
        "label": "Studio", "lifecycle": "active",
        "resource_groups": {"gpu0": {"max_concurrency": 1}},
        "base_url": "http://127.0.0.1:8080/v1",
        "availability_probe": {"argv": [sys.executable, "-c", 'print(\'{"state":"ready"}\')'],
                               "timeout_seconds": 2},
    }}
    seat = {"label": "Coder", "lifecycle": "active", "mode": "opportunistic", "host": "studio",
            "resource_group": "gpu0", "model_profile": "coder", "harness": "qwen",
            "capabilities": {"task_shapes": ["execute"], "validation": ["strong"]},
            "max_concurrency": 1, "runtime_fingerprint": "sha256:" + "a" * 64}
    settings["local_seats"] = {"small": seat, "large": copy.deepcopy(seat)}
    return settings


def ready(settings):
    return {"host": "studio", "state": "ready", "reason": "ready", "observed_at": NOW,
            "config_digest": local._host_digest(settings["local_hosts"]["studio"])}


def claim(root, settings, seat="small", availability=None):
    return local.claim(root, settings, seat, availability or ready(settings),
                       task_shape="execute", validation="strong", now=NOW)


def test_defaults_and_unknown_document_keys(tmp_path):
    settings = configured()
    settings["future_document"] = {"keep": [1, 2]}
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings))
    loaded, warnings = state.load_settings(tmp_path)
    assert loaded["local_hosts"] == settings["local_hosts"]
    assert loaded["future_document"] == {"keep": [1, 2]}
    assert not any("invalid settings" in warning for warning in warnings)
    assert state.default_settings()["providers"]["local"] == {
        "mode": "off", "command": "qwen", "max_turns": 100, "repair_budget": 2}


@pytest.mark.parametrize("url", ["https://user:pass@host/v1", "https://host/v1?a=b", "https://host/#x",
                                  "https://host?", "https://host#", "ftp://host/v1", "http://", "http://host:0",
                                  "http://host:65536", "http://host:bad", " http://host", "http://host\\path"])
@pytest.mark.parametrize("field", ["base_url", "lease_url"])
def test_reject_unsafe_urls(url, field):
    settings = configured()
    settings["local_hosts"]["studio"][field] = url
    with pytest.raises(ValueError):
        validate_local_registries(settings)


def test_accepts_optional_nonsecret_lease_url():
    settings = configured()
    settings["local_hosts"]["studio"]["lease_url"] = "http://studio:8002"
    validate_local_registries(settings)


@pytest.mark.parametrize("mutate", [
    lambda s: s["providers"]["local"].update(mode="manual"),
    lambda s: s["providers"]["local"].update(max_turns=True),
    lambda s: s["providers"]["local"].update(max_turns=101),
    lambda s: s["providers"]["local"].update(repair_budget=-1),
    lambda s: s["providers"]["local"].update(command="qwen --unsafe"),
    lambda s: s["providers"]["local"].update(api_key="no"),
    lambda s: s["local_hosts"]["studio"].update(token="no"),
    lambda s: s["local_hosts"]["studio"].update(availability_probe="echo ready"),
    lambda s: s["local_hosts"]["studio"]["availability_probe"].update(argv="echo ready"),
    lambda s: s["local_hosts"]["studio"]["availability_probe"].update(argv=["/bin/sh", "-c", "echo ready"]),
    lambda s: s["local_hosts"]["studio"]["availability_probe"].update(timeout_seconds=31),
    lambda s: s["local_hosts"]["studio"]["availability_probe"].update(timeout_seconds=float("nan")),
    lambda s: s["local_hosts"]["studio"]["availability_probe"].update(env={"API_KEY": "no"}),
])
def test_reject_invalid_config(mutate):
    settings = configured()
    mutate(settings)
    with pytest.raises(ValueError):
        validate_local_registries(settings)


def test_hard_off_zero_runtime_io(tmp_path, monkeypatch):
    settings = configured()
    settings["providers"]["local"]["mode"] = "off"
    def forbidden(*args, **kwargs):
        pytest.fail("hard off performed runtime I/O")
    monkeypatch.setattr(local.os, "open", forbidden)
    monkeypatch.setattr(local.bounded_process, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    import shutil
    import socket
    monkeypatch.setattr(shutil, "which", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    assert local.probe_host(tmp_path, settings, "studio")["reason"] == "mode_off"
    assert local.status(tmp_path, settings)["seats"][0]["reason"] == "mode_off"
    with claim(tmp_path, settings) as result:
        assert result["reason"] == "mode_off"
        assert not result["acquired"]


@pytest.mark.parametrize("global_mode,seat_mode,expected", [
    ("off", "opportunistic", "off"), ("opportunistic", "off", "off"),
    ("canary", "opportunistic", "canary"), ("opportunistic", "shadow", "shadow"),
])
def test_effective_mode(global_mode, seat_mode, expected):
    settings = configured()
    settings["providers"]["local"]["mode"] = global_mode
    settings["local_seats"]["small"]["mode"] = seat_mode
    assert local.effective_mode(settings, settings["local_seats"]["small"]) == expected


@pytest.mark.parametrize("availability_state", sorted(local.STATES))
def test_only_ready_is_eligible(availability_state):
    settings = configured()
    availability = ready(settings)
    availability.update(state=availability_state, reason="host_not_ready")
    result = local.eligibility(settings, "small", availability, now=NOW)
    assert result["eligible"] == (availability_state == "ready")
    assert (result["host"], result["resource_group"]) == ("studio", "gpu0")


def test_capability_lifecycle_model_freshness():
    settings = configured()
    def decide(**kwargs):
        return local.eligibility(settings, "small", ready(settings), now=NOW, **kwargs)
    assert decide(task_shape="design", validation="strong")["reason"] == "capability_mismatch"
    assert decide(task_shape="execute", validation="weak")["reason"] == "capability_mismatch"
    assert decide(task_shape="execute", validation="strong")["eligible"]
    for patch, reason in [({"model_profile": "other"}, "model_profile_mismatch"),
                          ({"observed_at": NOW - 16}, "availability_stale"),
                          ({"observed_at": NOW + 1}, "availability_stale"),
                          ({"host": "other"}, "host_mismatch"),
                          ({"config_digest": "wrong"}, "configuration_changed")]:
        assert local.eligibility(settings, "small", ready(settings) | patch, now=NOW)["reason"] == reason
    settings["local_hosts"]["studio"]["lifecycle"] = "draining"
    assert decide()["reason"] == "lifecycle_ineligible"


@pytest.mark.parametrize("payload", [b"{}", b"[]", b'{"state":"unknown"}', b'{"state":[]}',
                                     b'{"state":"ready","token":"secret"}', b'{"state":"ready"}\n{}',
                                     b'{"state":"ready","state":"busy"}', b"x" * 4097,
                                     b'{"state":"ready","observed_at":1184}',
                                     b'{"state":"ready","observed_at":NaN}',
                                     b'{"state":"ready","observed_at":true}',
                                     b'{"state":"ready","detail":"bad\\n"}', b"\xff"])
def test_probe_parser_rejects(payload):
    with pytest.raises((ValueError, UnicodeError)):
        local.parse_probe(payload, now=NOW)


def test_real_probe_cache_and_minimal_environment(tmp_path, monkeypatch):
    settings = configured()
    monkeypatch.setenv("SECRET_FOR_TEST", "never inherit")
    script = 'import os,json; assert "SECRET_FOR_TEST" not in os.environ; print(json.dumps({"state":"ready","model_profile":"coder"}))'
    settings["local_hosts"]["studio"]["availability_probe"]["argv"] = [sys.executable, "-c", script]
    value = local.probe_host(tmp_path, settings, "studio", clock=lambda: NOW)
    assert value["state"] == "ready"
    assert local.cached_availability(tmp_path, settings, "studio", now=NOW) == value
    monkeypatch.setattr(local.time, "time", lambda: NOW)
    monkeypatch.setattr(
        local.bounded_process,
        "run",
        lambda *args, **kwargs: pytest.fail("fresh observation was probed again"),
    )
    assert local.observe_host(tmp_path, settings, "studio") == value
    assert local.cached_availability(tmp_path, settings, "studio", now=NOW + 16)["state"] == "degraded"
    directory = tmp_path / "local-runtime"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "availability-studio.json").stat().st_mode & 0o777 == 0o600
    settings["local_hosts"]["studio"]["base_url"] = "http://other/v1"
    assert local.cached_availability(tmp_path, settings, "studio", now=NOW)["state"] == "degraded"


@pytest.mark.parametrize("script,reason", [
    ('raise SystemExit(3)', "probe_exit_nonzero"),
    ('print("not json")', "probe_invalid_output"),
    ('import time; time.sleep(10)', "probe_timeout"),
    ('import os; os.write(1, b"x"*1000000)', "probe_output_limit"),
    ('import os; os.write(2, b"x"*1000000)', "probe_output_limit"),
])
def test_real_probe_failures_replace_ready_cache(tmp_path, script, reason):
    settings = configured()
    local._write_cache(tmp_path, "studio", ready(settings))
    settings["local_hosts"]["studio"]["availability_probe"] = {
        "argv": [sys.executable, "-c", script],
        "timeout_seconds": 0.1 if reason == "probe_timeout" else 2.0,
    }
    result = local.probe_host(tmp_path, settings, "studio", clock=lambda: NOW)
    assert result["reason"] == reason
    assert local.cached_availability(tmp_path, settings, "studio", now=NOW)["state"] == "degraded"


def test_missing_probe_and_executable(tmp_path):
    settings = configured()
    settings["local_hosts"]["studio"]["availability_probe"]["argv"] = ["/nonexistent/local-probe"]
    assert local.probe_host(tmp_path, settings, "studio")["reason"] == "probe_failed"
    del settings["local_hosts"]["studio"]["availability_probe"]
    assert local.probe_host(tmp_path, settings, "studio")["reason"] == "probe_not_configured"


def test_shared_gpu_and_deterministic_release(tmp_path):
    settings = configured()
    with claim(tmp_path, settings) as first:
        assert first["acquired"]
        for seat in ("small", "large"):
            with claim(tmp_path, settings, seat) as second:
                assert second["reason"] == "capacity_claimed"
                assert not second["acquired"]
    with pytest.raises(RuntimeError):
        with claim(tmp_path, settings, "large") as next_claim:
            assert next_claim["acquired"]
            raise RuntimeError("consumer crashed")
    with claim(tmp_path, settings) as released:
        assert released["acquired"]


def test_group_and_seat_limits(tmp_path):
    settings = configured()
    settings["local_hosts"]["studio"]["resource_groups"]["gpu0"]["max_concurrency"] = 2
    with claim(tmp_path, settings) as first:
        assert first["acquired"]
        with claim(tmp_path, settings) as same_seat:
            assert not same_seat["acquired"]
        with claim(tmp_path, settings, "large") as second:
            assert second["acquired"]


def test_process_crash_releases_locks_and_stale_files_do_not_claim(tmp_path):
    settings = configured()
    code = '''import json,sys,signal
from pathlib import Path
from codex_seats import local_seats as local
settings=json.loads(sys.argv[2]); available=json.loads(sys.argv[3])
with local.claim(Path(sys.argv[1]), settings, "small", available, task_shape="execute", validation="strong", now=1200) as result:
 print(json.dumps(result), flush=True)
 signal.pause()
'''
    child = subprocess.Popen([sys.executable, "-c", code, str(tmp_path), json.dumps(settings), json.dumps(ready(settings))],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert json.loads(child.stdout.readline())["acquired"]
        with claim(tmp_path, settings, "large") as busy:
            assert not busy["acquired"]
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=5)
        for path in (tmp_path / "local-runtime").glob("*.lock"):
            path.write_text('{"pid":1,"claimed":true}')
        with claim(tmp_path, settings, "large") as recovered:
            assert recovered["acquired"]
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()


@pytest.mark.parametrize("kind", ["directory_symlink", "file_symlink", "permissions", "hardlink"])
def test_claim_security(tmp_path, kind):
    settings = configured()
    directory = tmp_path / "local-runtime"
    outside = tmp_path / "outside"
    if kind == "directory_symlink":
        outside.mkdir(mode=0o700)
        directory.symlink_to(outside, target_is_directory=True)
    else:
        directory.mkdir(mode=0o700)
        lock = directory / "seat-small-0.lock"
        if kind in {"file_symlink", "hardlink"}:
            outside.write_text("untouched")
            outside.chmod(0o600)
            if kind == "file_symlink":
                lock.symlink_to(outside)
            else:
                os.link(outside, lock)
        else:
            directory.chmod(0o755)
    with claim(tmp_path, settings) as result:
        assert result["reason"] == "unsafe_claim_runtime"
    if outside.is_file():
        assert outside.read_text() == "untouched"


def _retire(seat):
    seat.update(lifecycle="retired", mode="off")


def _deactivate_host(settings):
    # A non-active host may only carry non-active, off seats.
    settings["local_hosts"]["studio"]["lifecycle"] = "inactive"
    for seat in settings["local_seats"].values():
        seat.update(lifecycle="inactive", mode="off")


def _slots_free(root):
    """Every seat and group slot lock can be taken, i.e. nothing is claimed."""
    import fcntl

    for path in (root / "local-runtime").glob("*.lock"):
        fd = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        finally:
            os.close(fd)
    return True


@pytest.mark.parametrize(("change", "reason"), [
    (lambda s: _retire(s["local_seats"]["small"]), "mode_off"),
    (lambda s: s["local_seats"]["small"].update(lifecycle="inactive", mode="off"), "mode_off"),
    (lambda s: s["local_seats"]["small"].update(mode="shadow"), "shadow_only"),
    (lambda s: s["providers"]["local"].update(mode="off"), "mode_off"),
    (_deactivate_host, "mode_off"),
    (lambda s: s["local_seats"].pop("small"), "unknown_seat"),
    (lambda s: s["local_seats"]["small"]["capabilities"].update(task_shapes=["investigate"]),
     "capability_mismatch"),
    (lambda s: s["local_hosts"]["studio"].update(label="Edited"), "configuration_changed"),
])
def test_claim_revalidates_current_settings_after_taking_slots(tmp_path, change, reason):
    # A worker read these settings before the operator changed the seat.
    stale = configured()
    current = configured()
    change(current)
    (tmp_path / "settings.json").write_text(json.dumps(current))
    with claim(tmp_path, stale) as result:
        assert not result["acquired"] and not result["eligible"]
        assert result["reason"] == reason
        # Backed out before yielding: the seat and shared group slots are free.
        assert _slots_free(tmp_path)


def test_claim_backs_out_when_seat_moved_to_another_group(tmp_path):
    stale = configured()
    stale["local_hosts"]["studio"]["resource_groups"]["gpu1"] = {"max_concurrency": 1}
    current = copy.deepcopy(stale)
    current["local_seats"]["small"]["resource_group"] = "gpu1"
    (tmp_path / "settings.json").write_text(json.dumps(current))
    with claim(tmp_path, stale) as result:
        assert not result["acquired"] and result["reason"] == "configuration_changed"
        assert _slots_free(tmp_path)


def test_claim_with_unchanged_installed_settings_still_acquires(tmp_path):
    settings = configured()
    (tmp_path / "settings.json").write_text(json.dumps(settings))
    with claim(tmp_path, settings) as result:
        assert result["acquired"] and result["reason"] == "claimed"
        assert local_cli._seat_slot_held(tmp_path, "small")


def test_claim_invalid_installed_settings_fails_closed(tmp_path):
    settings = configured()
    (tmp_path / "settings.json").write_text("{not json")
    with claim(tmp_path, settings) as result:
        assert not result["acquired"]
        assert _slots_free(tmp_path)


def test_retirement_racing_a_stale_worker_never_starts_on_the_retired_seat(tmp_path, monkeypatch):
    """Removal's slot check passes, then a worker holding a stale active snapshot
    takes the slots before retirement commits: it must back out, not run."""
    import threading

    stale = configured()
    deactivated = configured()
    deactivated["local_seats"]["small"].update(lifecycle="inactive", mode="off")
    (tmp_path / "settings.json").write_text(json.dumps(deactivated))
    slots_held = threading.Event()
    real_current = local._current_settings

    def observed(root, supplied):
        slots_held.set()
        return real_current(root, supplied)

    monkeypatch.setattr(local, "_current_settings", observed)
    outcome = {}

    def worker():
        with claim(tmp_path, stale) as result:
            outcome.update(result, slots_free_inside=_slots_free(tmp_path))

    thread = threading.Thread(target=worker)
    with state.settings_transaction(tmp_path) as transaction:
        # The removal's locked guard sees no running local execution ...
        local_cli._require_no_running_job(tmp_path, "small")
        thread.start()
        # ... then the worker takes both slots and waits on the settings lock.
        assert slots_held.wait(timeout=5)
        thread.join(timeout=0.2)
        assert thread.is_alive()
        _retire(transaction.settings["local_seats"]["small"])
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert json.loads((tmp_path / "settings.json").read_text())["local_seats"]["small"]["lifecycle"] == "retired"
    assert outcome["acquired"] is False and outcome["reason"] == "mode_off"
    assert outcome["slots_free_inside"]


def test_seat_slot_scan_fails_closed_when_a_lock_vanishes(tmp_path, monkeypatch):
    runtime = tmp_path / "local-runtime"
    runtime.mkdir(mode=0o700)
    for name in ("seat-small-0.lock", "seat-small-1.lock"):
        (runtime / name).touch(mode=0o600)
    assert local_cli._seat_slot_held(tmp_path, "missing-seat") is False
    real_open = local._open_file

    def vanishing(directory, name, **kwargs):
        os.unlink(name, dir_fd=directory)
        return real_open(directory, name, **kwargs)

    monkeypatch.setattr(local, "_open_file", vanishing)
    with pytest.raises(ValueError, match="disappeared"):
        local_cli._seat_slot_held(tmp_path, "small")
    with pytest.raises(ValueError, match="cannot verify"):
        local_cli._require_no_running_job(tmp_path, "small")


def test_seat_slot_scan_without_runtime_is_idle(tmp_path):
    assert local_cli._seat_slot_held(tmp_path, "small") is False
    assert not (tmp_path / "local-runtime").exists()


def args(*arguments):
    return cli.build_parser().parse_args(["local", *arguments])


@pytest.fixture
def controller(monkeypatch):
    monkeypatch.setattr(local_cli, "current_controller", lambda: current_controller({"CODEX_CONTROL_SEAT": "seat1"}))
    monkeypatch.setattr(local_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(local_cli.sys.stdout, "isatty", lambda: True)


def test_preview_apply_unknown_fields_and_digest(tmp_path, controller):
    settings = configured()
    settings["future"] = {"keep": True}
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings))
    original = path.read_bytes()
    preview = local_cli.mutate(tmp_path, args("set-global-mode", "shadow", "--preview"))
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]
    applied = local_cli.mutate(tmp_path, args("set-global-mode", "shadow", "--from-current-user-request",
                                           "--expected-digest", preview["before_digest"]))
    assert applied["changed"]
    assert json.loads(path.read_text())["future"] == {"keep": True}
    with pytest.raises(ValueError, match="digest changed"):
        local_cli.mutate(tmp_path, args("set-global-mode", "off", "--expected-digest", preview["before_digest"]))


def test_structured_edit_preview_apply_and_optional_host_fields(tmp_path, controller):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(configured()))
    original = path.read_bytes()
    host_changes = {"label": "Updated host", "lease_url": "http://127.0.0.1:8080/lease",
                    "resource_groups": {"gpu0": {"max_concurrency": 2}}}
    preview = local_cli.preview_edit(tmp_path, "host", "studio", host_changes)
    assert preview["preview"] and preview["changed"]
    assert preview["after"]["local_hosts"]["studio"]["label"] == "Updated host"
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]
    applied = local_cli.apply_edit(tmp_path, "host", "studio", host_changes,
                                   expected_digest=preview["before_digest"],
                                   from_current_user_request=True)
    assert applied["changed"] and applied["after_digest"]
    seat_changes = {"label": "Updated seat", "max_concurrency": 2,
                    "context_window_size": 32768,
                    "capabilities": {"task_shapes": ["execute", "investigate"],
                                     "validation": ["strong"]}}
    preview = local_cli.preview_edit(tmp_path, "seat", "small", seat_changes)
    local_cli.apply_edit(tmp_path, "seat", "small", seat_changes,
                         expected_digest=preview["before_digest"],
                         from_current_user_request=True)
    loaded = json.loads(path.read_text())
    assert loaded["local_seats"]["small"]["max_concurrency"] == 2
    assert loaded["local_seats"]["small"]["context_window_size"] == 32768
    assert loaded["local_seats"]["small"]["capabilities"]["task_shapes"] == ["execute", "investigate"]
    remove = local_cli.preview_edit(tmp_path, "host", "studio", {"lease_url": None})
    local_cli.apply_edit(tmp_path, "host", "studio", {"lease_url": None},
                         expected_digest=remove["before_digest"],
                         from_current_user_request=True)
    assert "lease_url" not in json.loads(path.read_text())["local_hosts"]["studio"]
    remove = local_cli.preview_edit(tmp_path, "seat", "small", {"context_window_size": None})
    local_cli.apply_edit(tmp_path, "seat", "small", {"context_window_size": None},
                         expected_digest=remove["before_digest"],
                         from_current_user_request=True)
    assert "context_window_size" not in json.loads(path.read_text())["local_seats"]["small"]


def test_edit_cli_preview_digest_validation_and_lifecycle(tmp_path, controller):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(configured()))
    changes = json.dumps({"label": "New seat"})
    with pytest.raises(ValueError, match="expected-digest"):
        local_cli.mutate(tmp_path, args("edit-seat", "small", "--changes-json", changes))
    preview = local_cli.mutate(tmp_path, args("edit-seat", "small", "--changes-json", changes, "--preview"))
    path.write_text(json.dumps(configured() | {"future": "concurrent"}))
    with pytest.raises(ValueError, match="digest changed"):
        local_cli.mutate(tmp_path, args("edit-seat", "small", "--changes-json", changes,
                                        "--expected-digest", preview["before_digest"],
                                        "--from-current-user-request"))
    assert json.loads(path.read_text())["local_seats"]["small"]["label"] == "Coder"
    for invalid in ({"secret": "x"}, {"max_concurrency": 99}, {"lifecycle": "retired"}):
        with pytest.raises(ValueError):
            local_cli.preview_edit(tmp_path, "seat", "small", invalid)
    with pytest.raises(ValueError, match="non-empty"):
        local_cli.preview_edit(tmp_path, "host", "studio", {})
    with pytest.raises(ValueError, match="kind"):
        local_cli.preview_edit(tmp_path, "invalid", "studio", changes)
    preview = local_cli.preview_edit(tmp_path, "seat", "small", {"lifecycle": "inactive"})
    assert preview["after"]["local_seats"]["small"]["mode"] == "off"
    local_cli.apply_edit(tmp_path, "seat", "small", {"lifecycle": "inactive"},
                         expected_digest=preview["before_digest"])
    assert json.loads(path.read_text())["local_seats"]["small"]["mode"] == "off"


def test_edit_host_terminal_and_seat_enable_guards(tmp_path, controller, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(configured()))
    host_changes = {"availability_probe": {"argv": [sys.executable, "-c", 'print("ready")'],
                                             "timeout_seconds": 2}}
    host_preview = local_cli.preview_edit(tmp_path, "host", "studio", host_changes)
    with pytest.raises(ValueError, match="from-current-user-request"):
        local_cli.apply_edit(tmp_path, "host", "studio", host_changes,
                             expected_digest=host_preview["before_digest"])
    monkeypatch.setattr(local_cli.sys.stdin, "isatty", lambda: False)
    with pytest.raises(ValueError, match="interactive trusted human terminal"):
        local_cli.apply_edit(tmp_path, "host", "studio", host_changes,
                             expected_digest=host_preview["before_digest"],
                             from_current_user_request=True)
    monkeypatch.setattr(local_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(local_cli, "current_controller", lambda: current_controller({}))
    with pytest.raises(ValueError, match="trusted controller"):
        local_cli.apply_edit(tmp_path, "host", "studio", host_changes,
                             expected_digest=host_preview["before_digest"],
                             from_current_user_request=True)
    monkeypatch.setattr(local_cli, "current_controller", lambda: current_controller({"CODEX_CONTROL_SEAT": "seat1"}))
    local_cli.mutate(tmp_path, args("set-seat-mode", "small", "off"))
    seat_preview = local_cli.preview_edit(tmp_path, "seat", "small", {"mode": "canary"})
    with pytest.raises(ValueError, match="from-current-user-request"):
        local_cli.apply_edit(tmp_path, "seat", "small", {"mode": "canary"},
                             expected_digest=seat_preview["before_digest"])
    monkeypatch.setattr(local_cli, "current_controller", lambda: current_controller({"CODEX_SEATS_MANAGED_WORKER": "1"}))
    with pytest.raises(ValueError, match="managed workers"):
        local_cli.apply_edit(tmp_path, "seat", "small", {"label": "Other"},
                             expected_digest=seat_preview["before_digest"])


def test_status_cache_reasons_are_read_only_and_global_off_is_hard_off(tmp_path, monkeypatch):
    settings = configured()
    monkeypatch.setattr(local, "probe_host", lambda *a, **kw: pytest.fail("status ran a probe"))
    missing = local.status(tmp_path, settings, now=NOW)
    assert missing["hosts"]["studio"]["reason"] == "availability_missing_or_invalid"
    assert {seat["reason"] for seat in missing["seats"]} == {"availability_missing_or_invalid"}
    assert not (tmp_path / "local-runtime").exists()
    local._write_cache(tmp_path, "studio", ready(settings))
    path = tmp_path / "local-runtime" / "availability-studio.json"
    original = path.read_bytes()
    stale = local.status(tmp_path, settings, now=NOW + 16)
    assert stale["hosts"]["studio"]["reason"] == "availability_stale"
    assert {seat["reason"] for seat in stale["seats"]} == {"availability_stale"}
    assert path.read_bytes() == original
    settings["local_hosts"]["studio"]["label"] = "Changed"
    assert local.status(tmp_path, settings, now=NOW + 16)["seats"][0]["reason"] == "availability_stale"
    changed = local.status(tmp_path, settings, now=NOW)
    assert changed["hosts"]["studio"]["reason"] == "configuration_changed"
    assert {seat["reason"] for seat in changed["seats"]} == {"configuration_changed"}
    cache_without_digest = ready(configured())
    del cache_without_digest["config_digest"]
    local._write_cache(tmp_path, "studio", cache_without_digest)
    invalid = local.status(tmp_path, settings, now=NOW)
    assert invalid["hosts"]["studio"]["reason"] == "availability_missing_or_invalid"
    assert {seat["reason"] for seat in invalid["seats"]} == {"availability_missing_or_invalid"}
    path.write_bytes(original)
    settings["providers"]["local"]["mode"] = "off"
    off = local.status(tmp_path, settings, now=NOW)
    assert off["hosts"]["studio"]["reason"] == "mode_off"
    assert {seat["reason"] for seat in off["seats"]} == {"mode_off"}
    assert path.read_bytes() == original


def test_digest_checked_after_locked_reload(tmp_path, controller, monkeypatch):
    (tmp_path / "settings.json").write_text(json.dumps(configured()))
    preview = local_cli.mutate(tmp_path, args("set-global-mode", "off", "--preview"))
    real_transaction = state.settings_transaction
    @contextmanager
    def concurrent_writer(root):
        with real_transaction(root) as transaction:
            transaction.settings["future"] = "concurrent edit"
        with real_transaction(root) as transaction:
            yield transaction
    monkeypatch.setattr(state, "settings_transaction", concurrent_writer)
    with pytest.raises(ValueError, match="digest changed"):
        local_cli.mutate(tmp_path, args("set-global-mode", "off", "--expected-digest", preview["before_digest"]))
    assert json.loads((tmp_path / "settings.json").read_text())["future"] == "concurrent edit"


def test_modes_and_worker_guards(tmp_path, controller, monkeypatch):
    (tmp_path / "settings.json").write_text(json.dumps(configured()))
    with pytest.raises(ValueError, match="from-current-user-request"):
        local_cli.mutate(tmp_path, args("set-seat-mode", "small", "canary"))
    local_cli.mutate(tmp_path, args("set-seat-mode", "small", "off"))
    monkeypatch.setattr(local_cli, "current_controller", lambda: current_controller({"CODEX_SEATS_MANAGED_WORKER": "1"}))
    with pytest.raises(ValueError, match="managed workers"):
        local_cli.mutate(tmp_path, args("set-global-mode", "off"))
    assert local_cli.mutate(tmp_path, args("set-global-mode", "off", "--preview"))["preview"]


def test_lifecycle_guards_and_add_commands(tmp_path, controller):
    settings = configured()
    (tmp_path / "settings.json").write_text(json.dumps(settings))
    with pytest.raises(ValueError, match="non-active local host"):
        local_cli.mutate(tmp_path, args("set-host-lifecycle", "studio", "inactive"))
    for seat in ("small", "large"):
        local_cli.mutate(tmp_path, args("set-seat-lifecycle", seat, "inactive"))
    local_cli.mutate(tmp_path, args("set-host-lifecycle", "studio", "inactive"))
    local_cli.mutate(tmp_path, args("set-host-lifecycle", "studio", "retired"))
    with pytest.raises(ValueError, match="transition"):
        local_cli.mutate(tmp_path, args("set-host-lifecycle", "studio", "active"))
    host = settings["local_hosts"]["studio"]
    local_cli.mutate(tmp_path, args("add-host", "other", "--config-json", json.dumps(host),
                                    "--from-current-user-request"))
    seat = settings["local_seats"]["small"] | {
        "host": "other", "mode": "off", "context_window_size": 32768,
    }
    local_cli.mutate(tmp_path, args("add-seat", "other", "--config-json", json.dumps(seat)))
    assert json.loads((tmp_path / "settings.json").read_text())["local_seats"]["other"]["context_window_size"] == 32768
    with pytest.raises(ValueError, match="already exists"):
        local_cli.mutate(tmp_path, args("add-seat", "other", "--config-json", json.dumps(seat)))
    loaded = json.loads((tmp_path / "settings.json").read_text())
    assert loaded["local_seats"]["small"]["mode"] == "off"


def test_remove_seat_is_manager_only_and_not_a_cli_command(tmp_path, controller, capsys, monkeypatch):
    settings = configured()
    settings["local_seats"]["small"] |= {"lifecycle": "inactive", "mode": "off"}
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings))
    original = path.read_bytes()
    assert "remove-seat" not in local_cli.MUTATING_ACTIONS
    with pytest.raises(SystemExit):
        args("remove-seat", "small")
    capsys.readouterr()
    preview = local_cli.preview_action(tmp_path, "remove-seat", "small")
    assert preview["changed"] and not preview["enabling"]
    assert preview["after"]["local_seats"]["small"]["lifecycle"] == "retired"
    assert path.read_bytes() == original
    # The CLI authority (even a trusted controller terminal) cannot write it.
    with pytest.raises(ValueError, match="only through the interactive seat manager"):
        local_cli.apply_action(tmp_path, "remove-seat", "small", expected_digest=preview["before_digest"],
                               from_current_user_request=True)
    # Nor can the manager authority from a managed worker.
    monkeypatch.setenv("CODEX_SEATS_MANAGED_WORKER", "1")
    with pytest.raises(ValueError, match="refused"):
        local_cli.apply_action(tmp_path, "remove-seat", "small", expected_digest=preview["before_digest"],
                               from_current_user_request=True, authority=local_cli.MANAGER_AUTHORITY)
    assert path.read_bytes() == original


def test_management_preserves_unknown_keys(tmp_path, controller):
    raw = {
        "schema": 3,
        "unrecognized": {"preserve": True},
        "pool": {"seats": {}, "primary": None},
    }
    (tmp_path / "settings.json").write_text(json.dumps(raw))
    local_cli.mutate(tmp_path, args("set-global-mode", "off"))
    after = json.loads((tmp_path / "settings.json").read_text())
    assert after["unrecognized"] == raw["unrecognized"]
    assert after["schema"] == 3


@pytest.mark.parametrize("schema", [1, 2])
def test_management_refuses_older_settings_schemas(tmp_path, schema, controller):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"schema": schema, "unrecognized": {"preserve": True}}))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="only schema 3"):
        local_cli.mutate(tmp_path, args("set-global-mode", "off"))
    assert path.read_bytes() == before


def test_cli_json_readonly_and_errors(tmp_path, monkeypatch, controller, capsys):
    monkeypatch.setattr(local_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(local_cli.sys.stdout, "isatty", lambda: True)
    settings = configured()
    settings["providers"]["local"]["mode"] = "off"
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings))
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    for action in ("list", "status"):
        assert cli.main(["local", action, "--json"]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["seats"][0]["resource_group"] == "gpu0"
    assert cli.main(["local", "probe", "--all", "--json", "--from-current-user-request"]) == 0
    assert json.loads(capsys.readouterr().out)["hosts"]["studio"]["reason"] == "mode_off"
    assert list(tmp_path.iterdir()) == [path]
    assert cli.main(["local", "probe", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "local_control_error"
    path.write_text('{"schema":3,"local_hosts":null}')
    assert cli.main(["local", "status", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "local_control_error"


def test_bounded_capture_no_wait_after_output_limit():
    with pytest.raises(bounded_process.ProcessOutputLimitError):
        bounded_process.run([sys.executable, "-c", 'import os,time; os.write(1,b"x"*10000); time.sleep(30)'],
                            timeout=10, max_output_bytes=100, term_grace=0.1)


@pytest.mark.parametrize("url", ["http://localhost/v1", "https://host.example:443/api/v1", "http://[::1]:8080/v1"])
def test_valid_endpoint_shapes(url):
    settings = configured()
    settings["local_hosts"]["studio"]["base_url"] = url
    validate_local_registries(settings)


@pytest.mark.parametrize("corrupt", ["oversized", "missing_timestamp", "symlink", "permissions", "invalid_state"])
def test_cache_corruption_fails_closed(tmp_path, corrupt):
    settings = configured()
    local._write_cache(tmp_path, "studio", ready(settings))
    path = tmp_path / "local-runtime" / "availability-studio.json"
    if corrupt == "oversized":
        path.write_bytes(b"x" * 8193)
    elif corrupt == "missing_timestamp":
        value = ready(settings)
        del value["observed_at"]
        path.write_text(json.dumps(value))
    elif corrupt == "symlink":
        target = tmp_path / "target"
        target.write_text(path.read_text())
        path.unlink()
        path.symlink_to(target)
    elif corrupt == "permissions":
        path.chmod(0o644)
    else:
        path.write_text(json.dumps(ready(settings) | {"state": "maybe"}))
    assert local.cached_availability(tmp_path, settings, "studio", now=NOW)["state"] == "degraded"


def test_owner_checks(tmp_path, monkeypatch):
    settings = configured()
    local._write_cache(tmp_path, "studio", ready(settings))
    uid = os.getuid()
    monkeypatch.setattr(local.os, "getuid", lambda: uid + 1)
    assert local.cached_availability(tmp_path, settings, "studio", now=NOW)["state"] == "degraded"
    with claim(tmp_path, settings) as result:
        assert result["reason"] == "unsafe_claim_runtime"


def test_shadow_and_all_seats_off_prevent_claims_and_probes(tmp_path, monkeypatch):
    settings = configured()
    settings["providers"]["local"]["mode"] = "shadow"
    with claim(tmp_path, settings) as result:
        assert result["reason"] == "shadow_only"
    for seat in settings["local_seats"].values():
        seat["mode"] = "off"
    monkeypatch.setattr(bounded_process, "run", lambda *a, **kw: pytest.fail("off seat probed"))
    assert local.probe_host(tmp_path, settings, "studio")["reason"] == "no_enabled_seats"
    assert not (tmp_path / "local-runtime").exists()


def test_probe_finished_after_freshness_window_is_invalid(tmp_path, monkeypatch):
    settings = configured()
    times = iter([NOW, NOW + 16])
    monkeypatch.setattr(bounded_process, "run", lambda *a, **kw: bounded_process.ProcessResult(
        ("probe",), 0, b'{"state":"ready"}', b"", 1))
    result = local.probe_host(tmp_path, settings, "studio", clock=lambda: next(times))
    assert result["state"] == "degraded"
    assert result["reason"] == "probe_invalid_output"


def test_noninteractive_mode_guard_and_add_enabled_seat(tmp_path, monkeypatch):
    monkeypatch.setattr(local_cli, "current_controller", lambda: current_controller({}))
    settings = configured()
    (tmp_path / "settings.json").write_text(json.dumps(settings))
    with pytest.raises(ValueError, match="from-current-user-request"):
        local_cli.mutate(tmp_path, args("set-global-mode", "canary"))
    with pytest.raises(ValueError, match="from-current-user-request"):
        local_cli.mutate(tmp_path, args("add-seat", "enabled", "--config-json", json.dumps(settings["local_seats"]["small"])))
    local_cli.mutate(tmp_path, args("set-global-mode", "canary", "--from-current-user-request"))


def test_cli_probe_enabled_json_and_human(tmp_path, controller, monkeypatch, capsys):
    monkeypatch.setattr(local_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(local_cli.sys.stdout, "isatty", lambda: True)
    (tmp_path / "settings.json").write_text(json.dumps(configured()))
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    assert cli.main(["local", "probe", "studio", "--json", "--from-current-user-request"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["hosts"]["studio"]["state"] == "ready"
    assert result["hosts"]["studio"]["resource_groups"] == ["gpu0"]
    assert cli.main(["local", "status"]) == 0
    assert "studio/gpu0" in capsys.readouterr().out


def test_settings_symlink_mutation_refused(tmp_path, controller):
    outside = tmp_path / "other.json"
    outside.write_text(json.dumps(configured()))
    original = outside.read_bytes()
    (tmp_path / "settings.json").symlink_to(outside)
    with pytest.raises(ValueError):
        local_cli.mutate(tmp_path, args("set-global-mode", "off"))
    assert outside.read_bytes() == original


# -- edit-seat activation detection and the distinct manager authority path --
GUARD_VARS = ("CODEX_CONTROL_SEAT", "CODEX_SEATS_HEAD", "CODEX_SEATS_MANAGED_WORKER",
              "CODEX_THREAD_ID", "CODEX_HOME", "CODEX_SESSION_ID")


@pytest.fixture
def human_terminal(monkeypatch):
    """A real human TTY in a plain shell: eligible for the manager, not a controller."""
    for name in GUARD_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(local_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(local_cli.sys.stdout, "isatty", lambda: True)


def test_edit_label_on_enabled_seat_is_not_activation(tmp_path, controller):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(configured()))
    preview = local_cli.preview_edit(tmp_path, "seat", "small", {"label": "Renamed"})
    assert preview["changed"] and not preview["enabling"]
    applied = local_cli.apply_edit(tmp_path, "seat", "small", {"label": "Renamed"},
                                   expected_digest=preview["before_digest"])
    assert applied["changed"] and not applied["enabling"]
    assert (json.loads(path.read_text())["local_seats"]["small"]
            == configured()["local_seats"]["small"] | {"label": "Renamed"})
    # Making an enabled seat more restrictive is not an activation either.
    preview = local_cli.preview_edit(tmp_path, "seat", "small", {"mode": "canary"})
    assert not preview["enabling"]
    local_cli.apply_edit(tmp_path, "seat", "small", {"mode": "canary"},
                         expected_digest=preview["before_digest"])
    # Loosening the mode again is, and still needs the explicit current-request marker.
    preview = local_cli.preview_edit(tmp_path, "seat", "small", {"mode": "opportunistic"})
    assert preview["enabling"]
    with pytest.raises(ValueError, match="from-current-user-request"):
        local_cli.apply_edit(tmp_path, "seat", "small", {"mode": "opportunistic"},
                             expected_digest=preview["before_digest"])
    assert json.loads(path.read_text())["local_seats"]["small"]["mode"] == "canary"
    # An unknown mode fails registry validation and can never rank as "not enabling".
    with pytest.raises(ValueError):
        local_cli.preview_edit(tmp_path, "seat", "small", {"mode": "unlimited"})
    assert local_cli._mode_rank("unlimited") > local_cli._mode_rank("opportunistic")


def test_manager_authority_refuses_only_managed_workers(tmp_path, human_terminal, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(configured()))
    original = path.read_bytes()
    changes = {"label": "Manager host"}
    preview = local_cli.preview_action(tmp_path, "edit-host", "studio", changes=changes)

    def apply(**overrides):
        options = dict(expected_digest=preview["before_digest"], from_current_user_request=True,
                       authority=local_cli.MANAGER_AUTHORITY)
        options.update(overrides)
        return local_cli.apply_action(tmp_path, "edit-host", "studio", changes=changes, **options)

    # Managed workers are refused even though both streams claim to be a
    # (pseudo) terminal; heads and controllers may write through the manager.
    for env in ({"CODEX_SEATS_MANAGED_WORKER": "1"}, {"CLAUDE_LEAD_WORKER": "1"}):
        with monkeypatch.context() as patch:
            for name, value in env.items():
                patch.setenv(name, value)
            with pytest.raises(ValueError, match="managed workers"):
                apply()
    with pytest.raises(ValueError, match="explicit confirmation of the current request"):
        apply(from_current_user_request=False)
    with pytest.raises(ValueError, match="previewed settings digest"):
        apply(expected_digest=None)
    with pytest.raises(ValueError, match="unknown local mutation authority"):
        apply(authority="root")
    assert path.read_bytes() == original
    assert apply()["changed"]
    assert json.loads(path.read_text())["local_hosts"]["studio"]["label"] == "Manager host"
    # Every manager write pins the digest, not just edits.
    with pytest.raises(ValueError, match="previewed settings digest"):
        local_cli.apply_action(tmp_path, "set-seat-mode", "small", mode="off", expected_digest=None,
                               from_current_user_request=True, authority=local_cli.MANAGER_AUTHORITY)


def test_manager_authority_never_relaxes_cli_guards(tmp_path, human_terminal, capsys):
    """The CLI path is unchanged: a plain human shell is still not a trusted controller."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(configured()))
    original = path.read_bytes()
    changes = {"label": "CLI host"}
    preview = local_cli.preview_edit(tmp_path, "host", "studio", changes)
    with pytest.raises(ValueError, match="trusted controller"):
        local_cli.apply_edit(tmp_path, "host", "studio", changes,
                             expected_digest=preview["before_digest"], from_current_user_request=True)
    host = json.dumps(configured()["local_hosts"]["studio"])
    with pytest.raises(ValueError, match="trusted controller"):
        local_cli.mutate(tmp_path, args("add-host", "other", "--config-json", host,
                                        "--from-current-user-request"))
    assert local_cli.cmd_local(args("probe", "studio", "--json", "--from-current-user-request"), tmp_path) == 2
    assert "trusted controller" in json.loads(capsys.readouterr().out)["detail"]
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_structured_action_builders_validate_options(tmp_path, controller):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(configured()))
    for action, target, values in [
        ("set-global-mode", "studio", {"mode": "off"}), ("set-seat-mode", None, {"mode": "off"}),
        ("set-seat-mode", "", {"mode": "off"}), ("set-seat-mode", "small", {"mode": "loud"}),
        ("set-seat-lifecycle", "small", {"lifecycle": "gone"}),
        ("set-seat-mode", "small", {"lifecycle": "active"}), ("add-seat", "x", {"config": "text"}),
        ("edit-host", "studio", {"changes": {"label": "a"}, "config": {}}),
        ("edit-seat", "small", {"changes": {"label": object()}}), ("probe", "studio", {}),
    ]:
        with pytest.raises((ValueError, TypeError)):
            local_cli.preview_action(tmp_path, action, target, **values)
    preview = local_cli.preview_action(tmp_path, "set-global-mode", mode="shadow")
    assert preview["after"]["providers"]["local"]["mode"] == "shadow"
    preview = local_cli.preview_action(tmp_path, "set-seat-lifecycle", "small", lifecycle="inactive")
    assert preview["after"]["local_seats"]["small"]["mode"] == "off" and not preview["enabling"]
    assert path.read_bytes() == json.dumps(configured()).encode()
    applied = local_cli.apply_action(tmp_path, "set-seat-lifecycle", "small", lifecycle="inactive",
                                     expected_digest=preview["before_digest"])
    settings, warnings, digest = local_cli.load_local_settings(tmp_path)
    assert applied["changed"] and applied["after_digest"] == digest == local_cli.settings_digest(tmp_path)
    assert settings["local_seats"]["small"] == configured()["local_seats"]["small"] | {
        "lifecycle": "inactive", "mode": "off"}
    assert warnings == []
    path.write_text('{"schema":3,"local_hosts":null}')
    with pytest.raises(ValueError):
        local_cli.load_local_settings(tmp_path)
