"""Synthetic browser session tests; no real terminal, provider, or pool is used."""

from __future__ import annotations

import http.client
import json
import math
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from codex_seats import ui_server
from codex_seats.account import apply_account_snapshot
from codex_seats.seat_manager import ManagerEligibilityError, StalePreviewError
from codex_seats.state import Ledger, default_settings, new_seat, runtime_seat_home
from codex_seats.telemetry import apply_usage_limits
from registry_fixture import registry_settings, write_settings


class Reader:
    def __init__(self):
        self.exits = 0

    def load(self, loader):
        raise AssertionError("unexpected settings reload")

    def heartbeat(self):
        return {}

    def __exit__(self, *_args):
        self.exits += 1
        return None


class LoadingReader(Reader):
    def __init__(self, root):
        super().__init__()
        self.root = root

    def load(self, loader):
        return loader(self.root)


def test_launch_checks_authority_before_settings_or_reader(tmp_path):
    with mock.patch.object(ui_server, "ensure_management_eligible", side_effect=ManagerEligibilityError("TTY required")), \
         mock.patch.object(ui_server, "SettingsReader") as reader:
        with pytest.raises(ManagerEligibilityError, match="TTY required"):
            ui_server.launch(tmp_path, no_open=True)
        reader.assert_not_called()
        assert not list(tmp_path.iterdir())
    with mock.patch.object(ui_server, "ensure_management_eligible"), \
         mock.patch.dict(ui_server.os.environ, {"CLAUDE_LEAD_WORKER": "1"}), \
         mock.patch.object(ui_server, "SettingsReader") as reader:
        with pytest.raises(ManagerEligibilityError, match="Claude lead"):
            ui_server.launch(tmp_path, no_open=True)
        reader.assert_not_called()


def test_launch_fragment_and_server_start_failure_cleanup(tmp_path, capsys):
    settings = write_settings(tmp_path, default_settings())
    reader = mock.MagicMock()
    reader.__enter__.return_value.load.return_value = (settings, [])
    session = mock.Mock(token="synthetic-private-nonce")
    server = mock.Mock(server_port=12345)
    server.serve_forever.side_effect = RuntimeError("synthetic server failure")
    with mock.patch.object(ui_server, "ensure_management_eligible"), \
         mock.patch.object(ui_server, "SettingsReader", return_value=reader), \
         mock.patch.object(ui_server, "Session", return_value=session), \
         mock.patch.object(ui_server, "LoopbackHTTPServer", return_value=server):
        with pytest.raises(RuntimeError, match="synthetic server failure"):
            ui_server.launch(tmp_path, no_open=True)
    assert capsys.readouterr().out.strip() == "http://127.0.0.1:12345/#session=synthetic-private-nonce"
    server.shutdown.assert_not_called()
    server.server_close.assert_called_once()
    session.close.assert_called_once()


@pytest.mark.parametrize("raw", [None, {}, {"schema": 2}, {"schema": 3, "pool": "bad"}])
def test_strict_settings_rejects_missing_or_invalid_schema(tmp_path, raw):
    if raw is not None:
        (tmp_path / "settings.json").write_text(json.dumps(raw))
    with pytest.raises((OSError, ValueError)):
        ui_server._strict_settings(tmp_path)


def test_strict_settings_rejects_symlink_and_replacement(tmp_path):
    path = tmp_path / "settings.json"
    other = tmp_path / "other.json"
    other.write_text(json.dumps(default_settings()))
    path.symlink_to(other)
    with pytest.raises(ValueError):
        ui_server._strict_settings(tmp_path)
    path.unlink()
    path.write_text(json.dumps(default_settings()))
    path.chmod(0o600)
    assert ui_server._strict_settings(tmp_path)[0]["schema"] == 3
    path.unlink()
    path.symlink_to(other)
    with pytest.raises(ValueError):
        ui_server._strict_settings(tmp_path)


def test_state_reloads_current_registry_and_refuses_invalid_replacement(tmp_path):
    settings = registry_settings(["seat1", "seat2"])
    write_settings(tmp_path, settings)
    reader = LoadingReader(tmp_path)
    session = ui_server.Session(tmp_path, settings, reader, no_active_probe=True)
    try:
        changed = registry_settings(["seat1", "seat2"], primary="seat2", deputy="seat1")
        changed["pool"]["seats"]["seat1"]["label"] = "Renamed"
        changed["ui"]["theme"] = "dark"
        write_settings(tmp_path, changed)
        with mock.patch.object(ui_server, "read_pool_snapshot", return_value={"seats": {}, "jobs": []}), \
             mock.patch.object(ui_server, "_cached_identity_report", return_value={"status": "unavailable"}), \
             mock.patch.object(ui_server, "banked_reset_report", return_value={"status": "unknown"}), \
             mock.patch.object(session.manager, "local_manager", return_value=SimpleNamespace(report=lambda **_: {"seats": {}})), \
             mock.patch.object(session.manager, "claude_manager", return_value=SimpleNamespace(report=lambda **_: {})):
            state = session.state()
            assert state["seats"][0]["label"] == "Renamed"
            assert state["seats"][0]["role"] == "deputy"
            assert state["policy"]["ui.theme"] == "dark"
            (tmp_path / "settings.json").write_text("{}")
            with pytest.raises(ValueError):
                session.state()
    finally:
        assert session.close()
        assert reader.exits == 1


def test_cached_state_get_does_not_write_ledger(tmp_path):
    settings = write_settings(tmp_path, registry_settings(["seat1"], deputy=None))
    with Ledger(tmp_path):
        pass
    ledger_path = tmp_path / "seats.json"
    before = (ledger_path.read_bytes(), ledger_path.stat().st_mtime_ns)
    session = ui_server.Session(tmp_path, settings, LoadingReader(tmp_path), no_active_probe=True)
    try:
        state = session.state()
        assert state["seats"][0]["identity"]["status"] == "unavailable"
        assert (ledger_path.read_bytes(), ledger_path.stat().st_mtime_ns) == before
    finally:
        session.close()


def test_cached_identity_suppresses_replaced_login(tmp_path):
    settings = registry_settings(["seat1"], deputy=None)
    home = runtime_seat_home(tmp_path, "seat1", settings)
    home.mkdir(parents=True)
    auth = home / "auth.json"
    auth.write_bytes(b"")
    auth.chmod(0o600)
    fingerprint = ui_server._auth_file_fingerprint(home)
    runtime = {"manager_identity": {"status": "fresh", "email": "synthetic@example.test",
                                    "observed_plan_type": "business", "observed_at": time.time(),
                                    "auth_file_fingerprint": fingerprint}}
    report = ui_server._cached_identity_report(tmp_path, settings, "seat1", runtime, time.time())
    assert report["status"] == "fresh" and report["email"] == "synthetic@example.test"
    replacement = home / "replacement"
    replacement.write_bytes(b"x")
    replacement.chmod(0o600)
    replacement.replace(auth)
    report = ui_server._cached_identity_report(tmp_path, settings, "seat1", runtime, time.time())
    assert report["status"] == "stale" and report["email"] is None
    assert report["observed_plan_type"] is None


def test_native_usage_freshness_and_partial_constituents(tmp_path):
    settings = registry_settings(["seat1"], deputy=None)
    session = ui_server.Session(tmp_path, settings, Reader(), no_active_probe=True)
    now = time.time()
    seat = new_seat()
    payload = {"rateLimits": {"primary": {"usedPercent": 25, "windowDurationMins": 300,
                                            "resetsAt": now + 10000}}}
    apply_account_snapshot(seat, payload, now - 3)

    def dto(runtime):
        with mock.patch.object(session.manager, "reload", return_value=settings), \
             mock.patch.object(ui_server, "read_pool_snapshot", return_value={"seats": {"seat1": runtime}, "jobs": []}), \
             mock.patch.object(ui_server, "_cached_identity_report", return_value={"status": "fresh"}), \
             mock.patch.object(ui_server, "banked_reset_report", return_value={"status": "unknown"}), \
             mock.patch.object(session.manager, "local_manager", return_value=SimpleNamespace(report=lambda **_: {"seats": {}})), \
             mock.patch.object(session.manager, "claude_manager", return_value=SimpleNamespace(report=lambda **_: {})):
            return session.state()["seats"][0]

    try:
        fresh = dto(seat)
        assert fresh["freshness"] == "fresh" and fresh["windows"][0]["freshness"] == "fresh"
        assert isinstance(seat["usage_updated_at"], str)
        stale_seconds = settings["pool"]["probe_stale_seconds"]
        old = now - stale_seconds - 30
        mixed = new_seat()
        apply_usage_limits(mixed, {"primary": {"pct": 50, "resets_at": now + 10000},
                                   "weekly": {"pct": 60, "resets_at": now + 100000}}, {}, old, source="account")
        apply_usage_limits(mixed, {"primary": {"pct": 40, "resets_at": now + 10000}}, {}, now - 2, source="account")
        mixed_dto = dto(mixed)
        assert len(mixed_dto["windows"]) == 2
        assert mixed_dto["freshness"] == "stale"
        assert {window["name"]: window["observed_at"] for window in mixed_dto["windows"]} == {
            "primary": now - 2, "weekly": old,
        }
        numeric_only = dict(seat)
        numeric_only["active_window_observation"] = None
        numeric_only["usage_updated_at"] = None
        numeric_only["probed_at"] = None
        assert dto(numeric_only)["freshness"] == "fresh"
        for observed in (None, False, math.nan, now + 100):
            invalid = dict(seat)
            invalid["active_window_observation"] = {"observed_at": observed}
            invalid["usage_updated_at"] = None
            invalid["probed_at"] = None
            invalid["usage_observed_at"] = None
            assert dto(invalid)["freshness"] != "fresh"
    finally:
        session.close()


def test_set_role_uses_native_preview_commit_and_stale_guard(tmp_path):
    settings = registry_settings(["seat1", "seat2", "seat3"])
    write_settings(tmp_path, settings)
    with Ledger(tmp_path):
        pass
    session = ui_server.Session(tmp_path, settings, LoadingReader(tmp_path), no_active_probe=True)
    try:
        op = ui_server.Operation("role", "set-role", "seat3", native={"role": "deputy"})
        native, shown = session._prepare(op)
        assert shown["seat"] == "seat3" and shown["diff"]
        op.native = native
        with mock.patch("codex_seats.seat_manager.ensure_management_eligible"), \
             mock.patch.object(session.manager, "_mutation_guard"):
            assert session._commit(op)["message"] == "Seat seat3 updated"
        assert ui_server._strict_settings(tmp_path)[0]["pool"]["deputy"] == "seat3"
        stale = ui_server.Operation("stale", "set-role", "seat3", native={"role": "primary"})
        stale.native, _shown = session._prepare(stale)
        changed = ui_server._strict_settings(tmp_path)[0]
        changed["pool"]["seats"]["seat3"]["label"] = "Changed elsewhere"
        write_settings(tmp_path, changed)
        with mock.patch("codex_seats.seat_manager.ensure_management_eligible"), \
             mock.patch.object(session.manager, "_mutation_guard"):
            with pytest.raises(StalePreviewError):
                session._commit(stale)
        for invalid in ({}, {"role": "owner"}, {"role": "worker", "label": "bad"}):
            with pytest.raises(ValueError, match="set-role requires"):
                session._prepare(ui_server.Operation("invalid", "set-role", "seat3", native=invalid))
    finally:
        session.close()


def test_edit_rejects_unknown_fields_before_native_preview(tmp_path):
    session = ui_server.Session(tmp_path, default_settings(), Reader(), no_active_probe=True)
    try:
        with mock.patch.object(session.manager, "preview_set") as preview_set:
            with pytest.raises(ValueError, match="supported seat fields"):
                session._prepare(ui_server.Operation("bad", "edit-seat", "seat1",
                                                     native={"label": "New", "role": "primary"}))
            preview_set.assert_not_called()
    finally:
        session.close()


def test_operation_cancel_duplicate_confirm_and_unknown_consume(tmp_path):
    session = ui_server.Session(tmp_path, default_settings(), Reader(), no_active_probe=True)
    entered, release = threading.Event(), threading.Event()
    prepared = threading.Event()
    calls = []

    def prepare(op):
        calls.append(("prepare", op.id))
        prepared.set()
        return object(), {"title": "exact", "diff": ["+x"], "warnings": []}

    def commit(op):
        calls.append(("commit", op.id))
        entered.set()
        assert release.wait(3)
        return {"message": "provider outcome unknown", "outcome": "unknown"}

    session._prepare = prepare
    session._commit = commit
    session._worker.start()
    try:
        first = session.preview({"kind": "redeem-reset", "seat": "seat1"})
        assert prepared.wait(3)
        until = time.monotonic() + 3
        while session.operation(first["id"])["status"] == "preparing" and time.monotonic() < until:
            time.sleep(0.005)
        assert session.operation(first["id"])["status"] == "ready"
        assert session.confirm(first["id"])["status"] == "running"
        assert entered.wait(3)
        with pytest.raises(ValueError, match="cannot be canceled"):
            session.cancel(first["id"])
        assert not session._operations[first["id"]].cancel_event.is_set()
        with pytest.raises(ValueError, match="not ready"):
            session.confirm(first["id"])
        second = session.preview({"kind": "pause", "seat": "seat1"})
        assert session.cancel(second["id"])["status"] == "canceled"
        with pytest.raises(ValueError, match="not ready"):
            session.confirm(second["id"])
        # The HTTP response path can inspect an operation while provider work waits.
        assert session.operation(first["id"])["status"] == "running"
        release.set()
        session._queue.put(("missing", "prepare"))
        session._queue.put(None)
        session._worker.join(3)
        assert not session._worker.is_alive()
        assert session.operation(first["id"])["result"]["outcome"] == "unknown"
        assert ("prepare", second["id"]) not in calls
        assert calls.count(("commit", first["id"])) == 1
    finally:
        release.set()
        session.close()


def test_native_preview_and_commit_adapters_keep_exact_objects(tmp_path):
    session = ui_server.Session(tmp_path, default_settings(), Reader(), no_active_probe=True)
    action = SimpleNamespace(before='{"x": 1}', after='{"x": 2}', warnings=(), seat_id="seat1")
    pause = SimpleNamespace(preview=action)
    checks = SimpleNamespace(passed=True)
    with mock.patch.object(session.manager, "preview_pause", return_value=pause) as preview_pause, \
         mock.patch.object(session.manager, "commit_pause", return_value=SimpleNamespace(pending_drain=True)) as commit_pause, \
         mock.patch.object(session.manager, "run_activation_checks", return_value=checks) as run_checks, \
         mock.patch.object(session.manager, "commit_activation") as commit_activation:
        op = ui_server.Operation("one", "pause", "seat1", native={})
        native, shown = session._prepare(op)
        assert native is pause and shown["seat"] == "seat1"
        op.native = native
        assert session._commit(op)["message"] == "Seat draining"
        preview_pause.assert_called_once_with("seat1")
        commit_pause.assert_called_once_with(pause)
        op = ui_server.Operation("two", "reactivate", "seat1", native=action)
        assert session._commit(op)["message"] == "Seat activated"
        run_checks.assert_called_once_with(action, op.cancel_event)
        commit_activation.assert_called_once_with(action, checks)
    session.close()


def test_stale_commit_is_terminal(tmp_path):
    session = ui_server.Session(tmp_path, default_settings(), Reader(), no_active_probe=True)
    session._prepare = lambda op: (object(), {"title": "exact", "diff": [], "warnings": []})
    session._commit = mock.Mock(side_effect=StalePreviewError("settings changed"))
    session._worker.start()
    op = session.preview({"kind": "settings", "changes": {}})
    until = time.monotonic() + 3
    while session.operation(op["id"])["status"] == "preparing" and time.monotonic() < until:
        time.sleep(0.005)
    session.confirm(op["id"])
    until = time.monotonic() + 3
    while session.operation(op["id"])["status"] == "running" and time.monotonic() < until:
        time.sleep(0.005)
    assert session.operation(op["id"])["status"] == "error"
    assert session.operation(op["id"])["error"] == "settings changed"
    with pytest.raises(ValueError, match="not ready"):
        session.confirm(op["id"])
    session._commit.assert_called_once()
    assert session.close()


def test_close_bounds_a_busy_operation_thread(tmp_path):
    reader = Reader()
    session = ui_server.Session(tmp_path, default_settings(), reader, no_active_probe=True)
    started, release = threading.Event(), threading.Event()
    session._prepare = lambda op: (object(), {"title": "exact", "diff": [], "warnings": []})

    def blocked_commit(_op):
        started.set()
        release.wait(10)
        return {"message": "finished"}

    session._commit = blocked_commit
    session._worker.start()
    op = session.preview({"kind": "settings", "changes": {}})
    until = time.monotonic() + 3
    while session.operation(op["id"])["status"] == "preparing" and time.monotonic() < until:
        time.sleep(0.005)
    session.confirm(op["id"])
    assert started.wait(3)
    try:
        began = time.monotonic()
        assert session.close() is False
        assert time.monotonic() - began < 7
        assert reader.exits == 0
    finally:
        release.set()
        session._worker.join(3)
        assert session.close()
        assert reader.exits == 1


def test_expired_previews_cancel_and_confirmed_outcome_is_retained(tmp_path):
    session = ui_server.Session(tmp_path, default_settings(), Reader(), no_active_probe=True)
    try:
        waiting = session.preview({"kind": "settings", "changes": {}})
        op = session._operations[waiting["id"]]
        op.native = object()
        op.status = "ready"
        op.created_at -= ui_server.OPERATION_TTL + 1
        assert session.operation(op.id)["status"] == "canceled"
        assert op.native is None and op.cancel_event.is_set()
        with pytest.raises(ValueError, match="not ready"):
            session.confirm(op.id)
        confirmed = session.preview({"kind": "settings", "changes": {}})
        active = session._operations[confirmed["id"]]
        active.status = "running"
        active.created_at -= ui_server.OPERATION_TTL + 1
        assert session.operation(active.id)["status"] == "running"
        op.finished_at -= ui_server.OPERATION_TTL + 1
        with pytest.raises(KeyError):
            session.operation(op.id)
    finally:
        session.close()


def test_start_failure_closes_reader_once(tmp_path):
    reader = Reader()
    session = ui_server.Session(tmp_path, default_settings(), reader, no_active_probe=True)
    with mock.patch.object(session.refresher, "start", side_effect=RuntimeError("synthetic start failure")):
        with pytest.raises(RuntimeError, match="synthetic start failure"):
            session.start()
    assert reader.exits == 1
    assert session.close()
    assert reader.exits == 1


def test_cached_state_preserves_unknown_credit_and_local_capability(tmp_path):
    settings = default_settings()
    settings["pool"]["seats"] = {"seat1": {
        "label": "One", "lifecycle": "inactive", "capacity_profile": "custom",
        "relative_capacity": None, "capacity_source": "unverified",
    }}
    session = ui_server.Session(tmp_path, settings, Reader(), no_active_probe=True)
    seat = {"status": "ok", "usage_updated_at": None, "last_probe_limits": {},
            "banked_resets": {"status": "unknown", "available_count": None}}
    local = {"seats": {"local-one": {"config": {"label": "Local", "host": "studio", "lifecycle": "active"},
                                      "effective_mode": "shadow", "status": {"key": "off", "detail": "global mode off"}}}}
    try:
        with mock.patch.object(ui_server, "read_pool_snapshot", return_value={"seats": {"seat1": seat}, "jobs": []}), \
             mock.patch.object(session.manager, "reload", return_value=settings), \
             mock.patch.object(ui_server, "_cached_identity_report", return_value={"status": "unavailable", "email": None,
                                                                                       "observed_plan_type": None, "reason": "unknown"}), \
             mock.patch.object(ui_server, "banked_reset_report", return_value={"status": "unknown", "available_count": None,
                                                                                   "credits": [], "observed_at": None, "last_error": None}), \
             mock.patch.object(session.manager, "local_manager", return_value=SimpleNamespace(report=lambda **_: local)), \
             mock.patch.object(session.manager, "claude_manager", return_value=SimpleNamespace(report=lambda **_: {"account": "claude"})):
            state = session.state()
        assert state["seats"][0]["windows"] == []
        assert state["seats"][0]["banked_resets"]["available_count"] is None
        assert state["seats"][0]["identity"]["email"] is None
        assert state["local_seats"][0]["capabilities"]["login"] == "terminal only"
        assert state["policy"]["ui.theme"] == "system"
    finally:
        session.close()


def test_http_exact_host_origin_token_paths_methods_and_body_limit():
    class FakeSession:
        token = "synthetic-session-token"

        def state(self):
            return {"version": 1}

        def preview(self, _body):
            return {"id": "op1"}

        def operation(self, _ident):
            raise KeyError("operation not found")

    server = ui_server.LoopbackHTTPServer(("127.0.0.1", 0), FakeSession())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    host = f"127.0.0.1:{server.server_port}"
    origin = f"http://{host}"

    def request(method, path, body=None, headers=None):
        conn.request(method, path, body=body, headers={"Host": host, **(headers or {})})
        response = conn.getresponse()
        payload = response.read()
        return response.status, dict(response.getheaders()), json.loads(payload)

    def asset(path, headers=None):
        conn.request("GET", path, headers={"Host": host, **(headers or {})})
        response = conn.getresponse()
        return response.status, response.read()

    try:
        token = {"X-Codex-Seats-Session": FakeSession.token}
        for path in ("/", "/index.html", "/app.js", "/style.css"):
            status, content = asset(path)
            assert status == 200 and FakeSession.token.encode() not in content
        assert request("GET", "/api/state", headers=token)[2] == {"version": 1}
        assert request("GET", "/api/state")[0] == 403
        assert request("GET", "/api/state", headers={"Origin": origin})[0] == 403
        assert request("POST", "/api/preview", json.dumps({"kind": "settings", "changes": {}}),
                       {"Origin": origin, "Content-Type": "application/json"})[0] == 403
        assert request("GET", "/api/state", headers={**token, "Origin": "http://evil.test"})[0] == 403
        assert asset("/", {"Origin": "http://evil.test"})[0] == 403
        assert request("GET", "/api/state", headers={**token, "Host": "localhost"})[0] == 403
        assert request("GET", "/../../settings.json", headers=token)[0] == 404
        assert request("PUT", "/api/preview", headers={**token, "Origin": origin})[0] == 405
        data = json.dumps({"kind": "settings", "changes": {}})
        headers = {**token, "Content-Type": "application/json"}
        assert request("POST", "/api/preview", data, headers)[0] == 403
        assert request("POST", "/api/preview", data, {**headers, "Origin": "http://evil.test"})[0] == 403
        status, response_headers, body = request("POST", "/api/preview", data, {**headers, "Origin": origin})
        assert (status, body) == (200, {"id": "op1"})
        assert response_headers["X-Frame-Options"] == "DENY"
        assert request("POST", "/api/preview", "x" * (ui_server.MAX_BODY + 1),
                       {**headers, "Origin": origin})[0] == 400
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_ui_assets_resolve_from_loaded_package():
    package_ui = Path(__import__("codex_seats").__file__).parent / "ui"
    assert ui_server.resources.files("codex_seats").joinpath("ui", "index.html").is_file()
    assert (package_ui / "index.html").is_file()
    assert set(ui_server.ASSETS.values()) == {"index.html", "app.js", "style.css"}
