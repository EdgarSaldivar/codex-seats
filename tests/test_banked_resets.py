"""Synthetic reset metadata and one-attempt redemption contract."""
import copy
import threading
import time
from datetime import datetime, timezone
from unittest import mock

import pytest

from codex_seats import account
from codex_seats.state import Ledger, new_seat, runtime_seat_home
from registry_fixture import registry_settings, write_settings


def payload(count=1, credit="credit-a", email="one@example.test"):
    return {
        "rateLimits": {"primary": {"usedPercent": 25, "windowDurationMins": 300,
                                   "resetsAt": time.time() + 3000}},
        "rateLimitResetCredits": {"availableCount": count, "credits": [
            {"id": credit, "status": "available", "resetType": "codexRateLimits",
             "title": "One reset", "grantedAt": 1000, "expiresAt": 9000000000}
        ] if count else []},
        "_codex_seats_manager_identity": {"available": True, "account": {
            "account": {"type": "chatgpt", "email": email, "planType": "business"}}},
    }


def setup(tmp_path, monkeypatch):
    settings = registry_settings(["seat1"], deputy=None)
    write_settings(tmp_path, settings)
    home = runtime_seat_home(tmp_path, "seat1", settings)
    home.mkdir()
    auth = home / "auth.json"
    auth.write_text("synthetic fixture")
    auth.chmod(0o600)
    monkeypatch.setattr(account, "current_controller_is_worker", lambda: False)
    monkeypatch.setattr(account, "_redeem_authority", lambda **_kwargs: None)
    monkeypatch.setattr("codex_seats.workers.resolve_executable", lambda _name: "/fake/codex")
    return settings, auth


def test_metadata_count_detail_freshness_and_forward_events():
    seat = new_seat()
    account.record_banked_resets(seat, None, 100, present=False)
    assert account.banked_reset_report(seat, {"pool": {}}, 101)["available_count"] is None
    first = payload(1)["rateLimitResetCredits"]
    account.record_banked_resets(seat, first, 200, present=True)
    report = account.banked_reset_report(seat, {"pool": {"probe_stale_seconds": 60}}, 201)
    assert report["status"] == "fresh" and report["available_count"] == 1
    assert report["credits"][0]["expires_at"] == 9000000000
    assert account.banked_reset_report(seat, {"pool": {"probe_stale_seconds": 60}}, 300)["status"] == "stale"
    account.record_banked_resets(seat, {"availableCount": 0, "credits": []}, 400, present=True)
    assert seat["banked_resets"]["history"][-1]["unattributed"] == 1
    assert seat["banked_resets"]["history"][-1]["redeemed"] == 0
    assert seat["banked_resets"]["history"][-1]["expired"] == 0
    assert "id" not in repr(seat["banked_resets"])
    second = payload(1, "credit-b")["rateLimitResetCredits"]
    account.record_banked_resets(seat, second, 500, present=True)
    assert seat["banked_resets"]["observed_grants"] == 1
    expired = copy.deepcopy(second)
    expired["availableCount"] = 0
    expired["credits"][0]["status"] = "expired"
    account.record_banked_resets(seat, expired, 600, present=True)
    assert seat["banked_resets"]["observed_expirations"] == 1


def test_no_credit_and_changed_identity_refuse_without_consume(tmp_path, monkeypatch):
    settings, auth = setup(tmp_path, monkeypatch)
    current = [payload(0)]
    monkeypatch.setattr(account, "read_account_rate_limits", lambda *_a, **_k: current[0])
    with pytest.raises(account.AccountReadError, match="no banked"):
        account.prepare_banked_reset_redemption(tmp_path, settings, "seat1")
    current[0] = payload()
    preview = account.prepare_banked_reset_redemption(tmp_path, settings, "seat1")
    current[0] = payload(email="other@example.test")
    monkeypatch.setattr(account, "_app_server_call", lambda *_a, **kw: kw["before_consume"](current[0]))
    with pytest.raises(account.AccountReadError, match="account or available credit"):
        account.redeem_banked_reset(tmp_path, settings, preview)
    current[0] = payload()
    auth.write_text("synthetic changed login")
    with Ledger(tmp_path) as data:
        report = account.banked_reset_report(data["seats"]["seat1"], settings,
                                             root=tmp_path, seat_id="seat1")
        assert report["status"] == "stale" and report["available_count"] is None
        assert report["credits"] == []
    current[0] = payload(email="replacement@example.test")
    with pytest.raises(account.AccountReadError, match="account or available credit"):
        account.redeem_banked_reset(tmp_path, settings, preview)


def test_one_attempt_duplicate_concurrency_and_uncertain_outcome(tmp_path, monkeypatch):
    settings, _auth = setup(tmp_path, monkeypatch)
    current = [payload()]
    monkeypatch.setattr(account, "read_account_rate_limits", lambda *_a, **_k: copy.deepcopy(current[0]))
    preview = account.prepare_banked_reset_redemption(tmp_path, settings, "seat1")
    entered, release = threading.Event(), threading.Event()
    calls = []

    def consume(*_args, **kwargs):
        token, _credit = kwargs["before_consume"](copy.deepcopy(current[0]))
        calls.append(token)
        entered.set()
        release.wait(2)
        raise account.AccountReadError("synthetic transport loss")

    monkeypatch.setattr(account, "_app_server_call", consume)
    results = []
    thread = threading.Thread(target=lambda: results.append(account.redeem_banked_reset(tmp_path, settings, preview)))
    thread.start()
    assert entered.wait(2)
    with pytest.raises(account.AccountReadError, match="busy"):
        account.redeem_banked_reset(tmp_path, settings, preview)
    release.set()
    thread.join(3)
    assert len(calls) == 1 and results[0]["outcome"] == "unknown"
    with Ledger(tmp_path) as data:
        attempt = data["seats"]["seat1"]["banked_resets"]["attempts"][0]
        assert attempt["token"] == calls[0] and attempt["outcome"] == "unknown"
    with pytest.raises(account.AccountReadError, match="consume attempt"):
        account.prepare_banked_reset_redemption(tmp_path, settings, "seat1")


def test_same_process_reads_identity_then_journals_once_before_consume(tmp_path, monkeypatch):
    process = mock.Mock(stdout=None)
    sent = []
    responses = [
        {"result": {}}, {"result": payload()},
        {"result": payload()["_codex_seats_manager_identity"]["account"]},
        {"result": {"outcome": "reset"}},
    ]
    monkeypatch.setattr(account.subprocess, "Popen", mock.Mock(return_value=process))
    monkeypatch.setattr(account, "_send", lambda proc, request: sent.append((proc, request)))
    monkeypatch.setattr(account, "_wait_response", lambda *_a, **_k: responses.pop(0))
    monkeypatch.setattr(account, "_stop_process", lambda *_a: None)
    result = account._app_server_call("/fake/codex", tmp_path, tmp_path,
                                      "account/rateLimits/read",
                                      before_consume=lambda observed: ("one-token", "credit-a"))
    assert result == {"outcome": "reset"}
    assert [request["method"] for _, request in sent] == [
        "initialize", "initialized", "account/rateLimits/read", "account/read",
        "account/rateLimitResetCredit/consume",
    ]
    assert len({id(proc) for proc, _ in sent}) == 1
    assert sent[-1][1]["params"] == {"idempotencyKey": "one-token", "creditId": "credit-a"}
    account.subprocess.Popen.assert_called_once()
    assert account.subprocess.Popen.call_args.kwargs["env"]["CODEX_HOME"] == str(tmp_path)


@pytest.mark.parametrize("kind", ["missing", "symlink", "invalid", "changed"])
def test_registry_must_be_current_owner_file(tmp_path, monkeypatch, kind):
    settings, _auth = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(account, "read_account_rate_limits", lambda *_a, **_k: payload())
    preview = account.prepare_banked_reset_redemption(tmp_path, settings, "seat1")
    registry = tmp_path / "settings.json"
    if kind == "missing":
        registry.unlink()
    elif kind == "symlink":
        registry.rename(tmp_path / "real-settings.json")
        registry.symlink_to(tmp_path / "real-settings.json")
    elif kind == "invalid":
        registry.write_text('{"schema": 2}')
    else:
        changed = copy.deepcopy(settings)
        changed["pool"]["seats"]["seat1"]["label"] = "other"
        write_settings(tmp_path, changed)
    with pytest.raises(account.AccountReadError, match="registry|settings"):
        account.redeem_banked_reset(tmp_path, settings, preview)


def test_iso_expiry_account_baseline_and_forward_only_transition():
    seat = new_seat()
    at = time.time()
    first = payload()["rateLimitResetCredits"]
    first["credits"][0]["grantedAt"] = datetime.fromtimestamp(at - 100, timezone.utc).isoformat()
    first["credits"][0]["expiresAt"] = datetime.fromtimestamp(at + 100, timezone.utc).isoformat()
    account.record_banked_resets(seat, first, at, present=True, account_key="one")
    assert seat["banked_resets"]["credits"][0]["expires_at"] == pytest.approx(at + 100)
    expired = copy.deepcopy(first)
    expired["credits"][0]["expiresAt"] = datetime.fromtimestamp(at - 1, timezone.utc).isoformat()
    with pytest.raises(account.AccountReadError, match="no identified"):
        account._preview_credit({"rateLimitResetCredits": expired})
    account.record_banked_resets(seat, {"availableCount": 0, "credits": []}, at + 1,
                                 present=True, account_key="other")
    bank = seat["banked_resets"]
    assert bank["observed_redemptions"] == 0 and bank["observed_grants"] == 0
    assert bank["history"][-1]["kind"] == "baseline"
    account.record_banked_resets(seat, first, at, present=True, account_key="other")
    assert bank["available_count"] == 0
    bank["attempts"] = [{"account_key": "other", "credit_fingerprint": "x", "outcome": "reset"}, None]
    account.record_banked_resets(seat, {"availableCount": 1, "credits": first["credits"]},
                                 at + 2, present=True, account_key="other")
    changed = copy.deepcopy(first)
    changed["availableCount"] = 1
    changed["credits"][0]["status"] = "redeemed"
    account.record_banked_resets(seat, changed, at + 3, present=True, account_key="other")
    account.record_banked_resets(seat, {"availableCount": 0, "credits": []}, at + 4,
                                 present=True, account_key="other")
    assert bank["observed_redemptions"] == 1
    assert account.banked_reset_report({"banked_resets": {"attempts": [None], "history": None}},
                                       {"pool": {}})["last_manual_outcome"] is None


def test_busy_claim_is_preserved_and_postconsume_drift_blocks_publish(tmp_path, monkeypatch):
    settings, auth = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(account, "read_account_rate_limits", lambda *_a, **_k: payload())
    preview = account.prepare_banked_reset_redemption(tmp_path, settings, "seat1")
    busy = {"dispatcher_pid": 999999, "redemption_token": "other"}
    with Ledger(tmp_path) as data:
        data["seats"]["seat1"]["in_flight"] = busy
    with pytest.raises(account.AccountReadError, match="busy"):
        account.redeem_banked_reset(tmp_path, settings, preview)
    with Ledger(tmp_path) as data:
        assert data["seats"]["seat1"]["in_flight"] == busy
        data["seats"]["seat1"]["in_flight"] = None

    def provider(*_args, **kwargs):
        kwargs["before_consume"](payload())
        auth.write_text("provider switched credentials")
        return {"outcome": "reset"}

    monkeypatch.setattr(account, "_app_server_call", provider)
    outcome = account.redeem_banked_reset(tmp_path, settings, preview)
    assert outcome["outcome"] == "reset"
    assert "target changed after consume" in outcome["refresh_error"]
    with Ledger(tmp_path) as data:
        seat = data["seats"]["seat1"]
        assert seat["in_flight"] is None
        assert seat["banked_resets"]["auth_fingerprint"] == preview["auth_fingerprint"]
        assert seat["banked_resets"]["attempts"][0]["outcome"] == "reset"


def test_drift_before_consume_never_journals_or_sends(tmp_path, monkeypatch):
    settings, auth = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(account, "read_account_rate_limits", lambda *_a, **_k: payload())
    preview = account.prepare_banked_reset_redemption(tmp_path, settings, "seat1")

    def provider(*_args, **kwargs):
        auth.write_text("changed during provider read")
        return kwargs["before_consume"](payload())

    monkeypatch.setattr(account, "_app_server_call", provider)
    with pytest.raises(account.AccountReadError, match="login changed before consume"):
        account.redeem_banked_reset(tmp_path, settings, preview)
    with Ledger(tmp_path) as data:
        seat = data["seats"]["seat1"]
        assert seat["in_flight"] is None
        assert seat["banked_resets"].get("attempts", []) == []


def test_replaced_identical_registry_and_native_account_id_are_distinct(tmp_path, monkeypatch):
    settings, _auth = setup(tmp_path, monkeypatch)
    current = payload()
    current["accountId"] = "native-one"
    monkeypatch.setattr(account, "read_account_rate_limits", lambda *_a, **_k: current)
    preview = account.prepare_banked_reset_redemption(tmp_path, settings, "seat1")
    assert "native-one" in preview["provider_identity"]
    write_settings(tmp_path, settings)
    with pytest.raises(account.AccountReadError, match="settings changed"):
        account.redeem_banked_reset(tmp_path, settings, preview)
    preview = account.prepare_banked_reset_redemption(tmp_path, settings, "seat1")
    other = copy.deepcopy(current)
    other["accountId"] = "native-two"
    monkeypatch.setattr(account, "_app_server_call",
                        lambda *_a, **kw: kw["before_consume"](other))
    with pytest.raises(account.AccountReadError, match="account or available credit"):
        account.redeem_banked_reset(tmp_path, settings, preview)


def test_cleanup_does_not_erase_another_claim(tmp_path, monkeypatch):
    settings, _auth = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(account, "read_account_rate_limits", lambda *_a, **_k: payload())
    preview = account.prepare_banked_reset_redemption(tmp_path, settings, "seat1")
    replacement = {"dispatcher_pid": 999999, "redemption_token": "replacement"}

    def provider(*_args, **kwargs):
        kwargs["before_consume"](payload())
        with Ledger(tmp_path) as data:
            data["seats"]["seat1"]["in_flight"] = replacement
        raise account.AccountReadError("synthetic EOF")

    monkeypatch.setattr(account, "_app_server_call", provider)
    result = account.redeem_banked_reset(tmp_path, settings, preview)
    assert result["outcome"] == "unknown"
    with Ledger(tmp_path) as data:
        assert data["seats"]["seat1"]["in_flight"] == replacement


def test_unknown_consume_and_failed_refresh_leave_credits_stale(tmp_path, monkeypatch):
    settings, _auth = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(account, "read_account_rate_limits", lambda *_a, **_k: payload())
    preview = account.prepare_banked_reset_redemption(tmp_path, settings, "seat1")

    def provider(*_args, **kwargs):
        kwargs["before_consume"](payload())
        raise account.AccountReadError("synthetic transport loss")

    cancel = threading.Event()
    refresh_calls = []

    def unavailable(*_args, **kwargs):
        refresh_calls.append(kwargs)
        raise account.AccountReadError("synthetic refresh failure")

    monkeypatch.setattr(account, "_app_server_call", provider)
    monkeypatch.setattr(account, "read_account_rate_limits", unavailable)
    result = account.redeem_banked_reset(tmp_path, settings, preview, cancel_event=cancel)
    assert result["outcome"] == "unknown" and result["refresh_error"]
    assert refresh_calls[0]["cancel_event"] is cancel
    with Ledger(tmp_path) as data:
        seat = data["seats"]["seat1"]
        assert account.banked_reset_report(seat, settings)["status"] == "stale"
        assert seat["in_flight"] is None
        assert len(seat["banked_resets"]["attempts"]) == 1


@pytest.mark.parametrize("attempts", [None, [None], {}])
def test_invalid_attempt_journal_refuses_preview(tmp_path, monkeypatch, attempts):
    settings, _auth = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(account, "read_account_rate_limits", lambda *_a, **_k: payload())
    with Ledger(tmp_path) as data:
        data["seats"]["seat1"]["banked_resets"]["attempts"] = attempts
    with pytest.raises(account.AccountReadError, match="journal is invalid"):
        account.prepare_banked_reset_redemption(tmp_path, settings, "seat1")
