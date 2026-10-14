from __future__ import annotations

import json
import hashlib
import math
import os
import queue
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from .seat_registry import registered_seat_ids
from .state import Ledger, has_auth, runtime_seat_home
from .telemetry import (
    apply_usage_limits,
    limits_from_event,
    record_account_observation_failure,
)
from .worker_environment import minimal_worker_env


APP_SERVER_READ_ID = 2
_MANAGER_IDENTITY_PAYLOAD = "_codex_seats_manager_identity"


class AccountReadError(RuntimeError):
    """A read-only Codex account snapshot could not be obtained."""


def _request(method: str, request_id: int | None = None, params: Any = ...) -> dict[str, Any]:
    payload: dict[str, Any] = {"method": method}
    if request_id is not None:
        payload["id"] = request_id
    if params is not ...:
        payload["params"] = params
    return payload


def _send(process: subprocess.Popen[str], payload: dict[str, Any]) -> None:
    if process.stdin is None:
        raise AccountReadError("app-server stdin is unavailable")
    process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _wait_response(
    process: subprocess.Popen[str],
    responses: queue.Queue[str],
    request_id: int,
    deadline: float,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            raise AccountReadError("account read canceled")
        try:
            line = responses.get(timeout=min(0.25, max(0.01, deadline - time.monotonic())))
        except queue.Empty:
            if process.poll() is not None:
                break
            continue
        try:
            payload = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("id") == request_id:
            return payload
    if process.poll() is None:
        raise AccountReadError("account read timed out")
    detail = ""
    if process.stderr is not None:
        detail = process.stderr.read().strip()
    suffix = detail.splitlines()[-1] if detail else f"exit {process.returncode}"
    raise AccountReadError(f"app-server exited before response ({suffix})")


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            # Never turn best-effort provider shutdown into an unbounded caller wait.
            pass


def _app_server_call(
    codex: str,
    home: Path,
    scratch: Path,
    method: str,
    params: Any = ...,
    *,
    timeout: float = 30,
    cancel_event: threading.Event | None = None,
    optional_account_identity: bool = False,
    before_consume: Callable[[dict[str, Any]], tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Call one initialized Codex app-server account method."""
    environment = minimal_worker_env() if before_consume is not None else dict(os.environ)
    environment["CODEX_HOME"] = str(home)
    environment["NO_COLOR"] = "1"
    process: subprocess.Popen[str] | None = None
    try:
        # Provider lifecycle exception: the app-server serves one bounded request;
        # _stop_process always terminates/kills and reaps it in this function's finally.
        process = subprocess.Popen(
            [codex, "app-server", "--stdio"],
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            cwd=scratch,
            env=environment,
            start_new_session=True,
        )
    except OSError as exc:
        raise AccountReadError(str(exc)) from exc
    responses: queue.Queue[str] = queue.Queue()

    def collect() -> None:
        if process is not None and process.stdout is not None:
            for line in process.stdout:
                responses.put(line)

    threading.Thread(target=collect, daemon=True).start()
    deadline = time.monotonic() + timeout
    try:
        _send(
            process,
            _request(
                "initialize",
                1,
                {
                    "clientInfo": {
                        "name": "codex-seats",
                        "title": "Managed Codex Pool",
                        "version": "4.1",
                    },
                    "capabilities": None,
                },
            ),
        )
        initialized = _wait_response(process, responses, 1, deadline, cancel_event)
        if isinstance(initialized.get("error"), dict):
            raise AccountReadError(str(initialized["error"].get("message") or "initialize failed"))
        _send(process, _request("initialized"))
        _send(process, _request(method, APP_SERVER_READ_ID, params))
        response = _wait_response(
            process, responses, APP_SERVER_READ_ID, deadline, cancel_event
        )
        error = response.get("error")
        if isinstance(error, dict):
            raise AccountReadError(str(error.get("message") or "account read failed"))
        payload = response.get("result")
        if not isinstance(payload, dict):
            raise AccountReadError("account read returned an invalid result")
        if optional_account_identity or before_consume is not None:
            # Keep identity on the same initialized provider process. Failure
            # here must not discard an authoritative usage response.
            identity_id = APP_SERVER_READ_ID + 1
            try:
                _send(
                    process,
                    _request("account/read", identity_id, {"refreshToken": False}),
                )
                identity_response = _wait_response(
                    process, responses, identity_id, deadline, cancel_event
                )
                identity_payload = identity_response.get("result")
                available = not isinstance(identity_response.get("error"), dict) and isinstance(
                    identity_payload, dict
                )
                payload = dict(payload)
                payload[_MANAGER_IDENTITY_PAYLOAD] = {
                    "available": available,
                    **({"account": identity_payload} if available else {}),
                }
            except (AccountReadError, OSError):
                if before_consume is not None:
                    raise AccountReadError("provider account identity is unavailable")
                payload = dict(payload)
                payload[_MANAGER_IDENTITY_PAYLOAD] = {"available": False}
        if before_consume is not None:
            # The callback durably journals the attempt immediately before this
            # single send. Any transport failure after that boundary is unknown.
            token, credit_id = before_consume(payload)
            _send(process, _request("account/rateLimitResetCredit/consume", 4,
                                    {"idempotencyKey": token, "creditId": credit_id}))
            consumed = _wait_response(process, responses, 4, deadline, cancel_event)
            error = consumed.get("error")
            if isinstance(error, dict):
                raise AccountReadError(str(error.get("message") or "consume failed"))
            result = consumed.get("result")
            if not isinstance(result, dict) or result.get("outcome") not in {
                "reset", "alreadyRedeemed", "nothingToReset", "noCredit"
            }:
                raise AccountReadError("reset consumption returned an unknown outcome")
            return result
        return payload
    finally:
        _stop_process(process)


def read_account_rate_limits(
    codex: str,
    home: Path,
    scratch: Path,
    *,
    timeout: float = 30,
    cancel_event: threading.Event | None = None,
    include_manager_identity: bool = False,
) -> dict[str, Any]:
    """Read subscription limits through the provider-owned account endpoint."""
    return _app_server_call(
        codex,
        home,
        scratch,
        "account/rateLimits/read",
        timeout=timeout,
        cancel_event=cancel_event,
        optional_account_identity=include_manager_identity,
    )


def limits_from_account(payload: dict[str, Any], at: float | None = None) -> dict[str, Any]:
    """Validate and map the app-server account response onto pool windows."""
    observed_at = time.time() if at is None else at
    limits, _complete = validated_account_limits(payload, observed_at)
    return limits


def _selected_account_snapshot(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Select one authoritative collection, rejecting ambiguous/malformed shapes."""
    if "rateLimitsByLimitId" in payload and payload.get("rateLimitsByLimitId") is not None:
        by_id = payload["rateLimitsByLimitId"]
        if not isinstance(by_id, dict):
            raise AccountReadError("account rate-limit enumeration is malformed")
        if "codex" in by_id:
            selected = by_id["codex"]
        elif not by_id:
            selected = None
        elif len(by_id) == 1:
            selected = next(iter(by_id.values()))
        else:
            raise AccountReadError("account rate-limit enumeration is ambiguous")
        if selected is None:
            return {}, True
        if not isinstance(selected, dict):
            raise AccountReadError("account rate-limit snapshot is malformed")
        return selected, True
    if "rateLimits" not in payload:
        return {}, False
    selected = payload["rateLimits"]
    if selected is None:
        return {}, True
    if not isinstance(selected, dict):
        raise AccountReadError("account rate-limit snapshot is malformed")
    return selected, True


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _usage_credits(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Allowlist optional provider workspace-credit facts without defaults."""
    raw = snapshot.get("credits")
    if not isinstance(raw, dict):
        return None
    has_credits = raw.get("hasCredits")
    unlimited = raw.get("unlimited")
    balance = raw.get("balance")
    if not isinstance(has_credits, bool):
        has_credits = None
    if not isinstance(unlimited, bool):
        unlimited = None
    if isinstance(balance, bool) or not isinstance(balance, (str, int, float)):
        balance = None
    elif isinstance(balance, float) and not math.isfinite(balance):
        balance = None
    elif isinstance(balance, str):
        balance = balance.strip()[:128] or None
    return {
        "has_credits": has_credits,
        "unlimited": unlimited,
        "balance": balance,
    }


def validated_account_limits(
    payload: dict[str, Any], at: float
) -> tuple[dict[str, Any], bool]:
    """Validate every advertised quota window before deriving any capacity."""
    snapshot, complete = _selected_account_snapshot(payload)
    plan_type = _identity_text(snapshot.get("planType"), 128)
    event: dict[str, Any] = {}
    for target, *keys in (
        ("primary", "primary"),
        ("secondary", "secondary"),
        ("individual_limit", "individualLimit", "individual_limit"),
    ):
        present = next((key for key in keys if key in snapshot), None)
        if present is None or snapshot[present] is None:
            continue
        raw = snapshot[present]
        if not isinstance(raw, dict):
            raise AccountReadError(f"account rate-limit window {present} is malformed")
        used = _finite_number(raw.get("usedPercent", raw.get("used_percent")))
        minutes = _finite_number(
            raw.get("windowDurationMins", raw.get("window_minutes"))
        )
        resets_raw = raw.get("resetsAt", raw.get("resets_at"))
        resets_at = None if resets_raw is None else _finite_number(resets_raw)
        if (
            used is None
            or not 0 <= used <= 100
            or minutes is None
            or minutes <= 0
            or (resets_raw is not None and resets_at is None)
        ):
            raise AccountReadError(f"account rate-limit window {present} is malformed")
        event[target] = {
            "used_percent": used,
            "window_minutes": minutes,
            "resets_at": resets_at,
            **({"plan_type": plan_type} if plan_type is not None else {}),
        }
    return limits_from_event(event, at=at), complete


def apply_account_snapshot(
    seat: dict[str, Any],
    payload: dict[str, Any],
    observed_at: float,
) -> None:
    snapshot, _complete = _selected_account_snapshot(payload)
    limits, complete = validated_account_limits(payload, observed_at)
    entry = {
        "tokens": seat.get("session_total_tokens", 0),
        "cached_tokens": seat.get("session_cached_tokens", 0),
        "output_tokens": seat.get("session_output_tokens", 0),
    }
    apply_usage_limits(
        seat,
        limits,
        entry,
        observed_at,
        source="account",
        observation_complete=complete,
    )
    seat["account_usage_plan_type"] = _identity_text(
        snapshot.get("planType"), 128
    )
    seat["account_usage_credits"] = _usage_credits(snapshot)
    try:
        account_key = _provider_identity(payload)
    except AccountReadError:
        bank = seat.get("banked_resets")
        if not isinstance(bank, dict):
            bank = seat["banked_resets"] = {}
        bank.update(status="unavailable", last_attempt_at=observed_at,
                    last_error="provider account identity is unavailable")
        return
    record_banked_resets(seat, payload.get("rateLimitResetCredits"), observed_at,
                         present="rateLimitResetCredits" in payload,
                         account_key=account_key)


def _credit_detail(raw: dict[str, Any]) -> dict[str, Any] | None:
    credit_id = raw.get("id")
    if not isinstance(credit_id, str) or not credit_id:
        return None
    def epoch(value: Any) -> float | None:
        number = _finite_number(value)
        if number is None and isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    number = parsed.timestamp()
            except (ValueError, OverflowError):
                pass
        return number if number is not None and number > 0 else None
    return {
        "fingerprint": hashlib.sha256(credit_id.encode()).hexdigest()[:16],
        "status": _identity_text(raw.get("status"), 64),
        "reset_type": _identity_text(raw.get("resetType"), 64),
        "title": _identity_text(raw.get("title"), 256),
        "description": _identity_text(raw.get("description"), 512),
        "granted_at": epoch(raw.get("grantedAt")),
        "expires_at": epoch(raw.get("expiresAt")),
        "expiry_status": ("none" if "expiresAt" in raw and raw["expiresAt"] is None
                          else "reported" if epoch(raw.get("expiresAt")) is not None else "unknown"),
    }


def record_banked_resets(
    seat: dict[str, Any], summary: Any, observed_at: float, *, present: bool,
    account_key: str | None = None,
) -> None:
    """Keep provider facts and classify only changes supported by observations."""
    bank = seat.get("banked_resets")
    if not isinstance(bank, dict):
        bank = seat["banked_resets"] = {}
    if isinstance(bank.get("observed_at"), (int, float)) and observed_at <= bank["observed_at"]:
        return
    if account_key is not None and bank.get("account_key") != account_key:
        bank.update(account_key=account_key, available_count=None, credits=[], history=[],
                    observed_grants=0, observed_redemptions=0, observed_expirations=0,
                    pending_status_redemptions=0, pending_status_expirations=0)
    elif account_key is None and bank.get("account_key") is not None:
        bank.update(status="unavailable", last_attempt_at=observed_at,
                    last_error="provider account identity is unavailable")
        return
    if not present or not isinstance(summary, dict):
        bank.update(status="unavailable", last_attempt_at=observed_at,
                    last_error="provider did not return reset credit metadata")
        return
    count = summary.get("availableCount")
    if type(count) is not int or count < 0:
        bank.update(status="unavailable", last_attempt_at=observed_at,
                    last_error="provider returned an invalid reset credit count")
        return
    raw_details = summary.get("credits")
    details = [item for item in (_credit_detail(raw) for raw in raw_details
               if isinstance(raw, dict)) if item is not None] if isinstance(raw_details, list) else []
    previous = bank.get("available_count")
    events = bank.get("history")
    if not isinstance(events, list):
        events = bank["history"] = []
    for key in ("observed_grants", "observed_redemptions", "observed_expirations",
                "pending_status_redemptions", "pending_status_expirations"):
        if type(bank.get(key)) is not int or bank[key] < 0:
            bank[key] = 0
    if type(previous) is int and previous != count:
        change = count - previous
        kind = "grant" if change > 0 else "decrease-unattributed"
        if change < 0:
            already_redeemed = min(-change, max(0, bank.get("pending_status_redemptions", 0)))
            bank["pending_status_redemptions"] = max(0, bank.get("pending_status_redemptions", 0) - already_redeemed)
            already_expired = min(-change - already_redeemed,
                                  max(0, bank.get("pending_status_expirations", 0)))
            bank["pending_status_expirations"] = max(0, bank.get("pending_status_expirations", 0) - already_expired)
            unaccounted = -change - already_redeemed - already_expired
            attempts = bank.get("attempts", [])
            attempts = attempts if isinstance(attempts, list) else []
            matching = [item for item in attempts if isinstance(item, dict)
                        and item.get("account_key") == account_key
                        and item.get("outcome") in {"reset", "alreadyRedeemed"}
                        and not item.get("observed")]
            confirmed = len(matching)
            redeemed = min(unaccounted, confirmed)
            attributed_fingerprints = {item.get("credit_fingerprint") for item in matching[:redeemed]}
            for item in matching[:redeemed]:
                item["observed"] = True
            old = {item["fingerprint"]: item for item in bank.get("credits", [])
                   if isinstance(item, dict) and isinstance(item.get("fingerprint"), str)} if isinstance(bank.get("credits"), list) else {}
            new = {item["fingerprint"]: item for item in details}
            transitions = [(key, new[key].get("status")) for key, item in old.items()
                           if item.get("status") == "available" and key in new]
            transitioned_redeemed = sum(status == "redeemed" and key not in attributed_fingerprints
                                        for key, status in transitions)
            transitioned_expired = sum(status == "expired" for _, status in transitions)
            extra_redeemed = min(unaccounted - redeemed, transitioned_redeemed)
            redeemed += extra_redeemed
            expired = min(unaccounted - redeemed, transitioned_expired)
            bank["observed_redemptions"] += redeemed
            bank["observed_expirations"] += expired
            kind = "decrease-observed"
            events.append({"at": observed_at, "kind": kind, "before": previous,
                           "after": count, "redeemed": redeemed, "expired": expired,
                           "unattributed": unaccounted - redeemed - expired})
        else:
            bank["observed_grants"] += change
            events.append({"at": observed_at, "kind": kind, "before": previous,
                           "after": count, "granted": change})
    elif previous is None:
        events.append({"at": observed_at, "kind": "baseline", "available_count": count})
    if type(previous) is int and previous == count and isinstance(raw_details, list):
        prior = {item["fingerprint"]: item for item in bank.get("credits", [])
                 if isinstance(item, dict) and isinstance(item.get("fingerprint"), str)} if isinstance(bank.get("credits"), list) else {}
        transitions = [
            {"fingerprint": item["fingerprint"], "from": prior[item["fingerprint"]].get("status"),
             "to": item.get("status")}
            for item in details if item["fingerprint"] in prior
            and prior[item["fingerprint"]].get("status") != item.get("status")
        ]
        if transitions:
            events.append({"at": observed_at, "kind": "credit-status-transition", "transitions": transitions})
            bank["observed_expirations"] += sum(item["to"] == "expired" for item in transitions)
            bank["observed_redemptions"] += sum(item["to"] == "redeemed" for item in transitions)
            bank["pending_status_expirations"] = bank.get("pending_status_expirations", 0) + sum(
                item["to"] == "expired" for item in transitions)
            bank["pending_status_redemptions"] = bank.get("pending_status_redemptions", 0) + sum(
                item["to"] == "redeemed" for item in transitions)
            attempts = bank.get("attempts", [])
            if isinstance(attempts, list):
                for transition in transitions:
                    if transition["to"] == "redeemed":
                        for attempt in attempts:
                            if (isinstance(attempt, dict) and attempt.get("account_key") == account_key
                                    and attempt.get("credit_fingerprint") == transition["fingerprint"]):
                                attempt["observed"] = True
    bank.update(status="fresh", available_count=count, credits=details,
                details_available=isinstance(raw_details, list), observed_at=observed_at,
                last_attempt_at=observed_at, last_error=None, history=events[-50:])


def _identity_text(value: Any, maximum: int) -> str | None:
    rendered = value.strip() if isinstance(value, str) else ""
    return rendered[:maximum] if rendered else None


def _auth_file_fingerprint(home: Path) -> dict[str, int] | None:
    """Return metadata-only auth identity; never open or follow credentials."""
    try:
        info = (home / "auth.json").lstat()
    except OSError:
        return None
    if not (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_uid == os.geteuid()
        and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        return None
    return {
        "device": int(info.st_dev), "inode": int(info.st_ino), "size": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns), "ctime_ns": int(info.st_ctime_ns),
    }


def _identity_failure(
    seat: dict[str, Any], observed_at: float, fingerprint: dict[str, int] | None,
    reason: str, *, definitive: bool = False,
) -> None:
    previous = seat.get("manager_identity")
    if (
        not definitive
        and isinstance(previous, dict)
        and previous.get("auth_file_fingerprint") == fingerprint
        and _identity_text(previous.get("email"), 320)
    ):
        # Rewrite an old cache entry so unsupported workspace fields disappear
        # even when the current refresh can only retain last-good identity.
        previous.pop("workspace_name", None)
        previous.pop("workspace_id", None)
        previous.update(status="stale", last_attempt_at=observed_at, reason=reason)
        return
    seat["manager_identity"] = {
        "schema": 1, "status": "unavailable", "email": None,
        "observed_plan_type": None, "account_id": None,
        "observed_at": None, "last_attempt_at": observed_at,
        "auth_file_fingerprint": fingerprint, "reason": reason,
    }


def apply_manager_identity_snapshot(
    seat: dict[str, Any], payload: dict[str, Any], observed_at: float,
    fingerprint: dict[str, int] | None,
) -> bool:
    """Cache only manager-visible identity fields from provider read responses."""
    envelope = payload.get(_MANAGER_IDENTITY_PAYLOAD)
    if not isinstance(envelope, dict):  # mocked/legacy rate-only result
        return False
    if envelope.get("available") is not True:
        _identity_failure(
            seat, observed_at, fingerprint,
            "provider account metadata was unavailable during refresh",
        )
        return True
    account_result = envelope.get("account")
    account = account_result.get("account") if isinstance(account_result, dict) else None
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        _identity_failure(
            seat, observed_at, fingerprint,
            "provider did not report a ChatGPT account identity", definitive=True,
        )
        return True
    email = _identity_text(account.get("email"), 320)
    if email is None:
        _identity_failure(
            seat, observed_at, fingerprint,
            "provider did not report an account email", definitive=True,
        )
        return True
    seat["manager_identity"] = {
        "schema": 1, "status": "fresh", "email": email,
        "observed_plan_type": _identity_text(account.get("planType"), 128),
        # accountId is backend-owned but is not assumed to be a workspace ID.
        "account_id": _identity_text(payload.get("accountId"), 256),
        "observed_at": observed_at, "last_attempt_at": observed_at,
        "auth_file_fingerprint": fingerprint, "reason": None,
    }
    return True


def manager_identity_report(
    root: Path, settings: dict[str, Any], seat_id: str
) -> dict[str, Any]:
    """Return an allowlisted manager-only identity view from cached state."""
    with Ledger(root) as data:
        runtime = data.get("seats", {}).get(seat_id, {})
        runtime = runtime if isinstance(runtime, dict) else {}
        cached = runtime.get("manager_identity")
        cached = dict(cached) if isinstance(cached, dict) else {}
    fingerprint = _auth_file_fingerprint(runtime_seat_home(root, seat_id, settings))
    observed = cached.get("observed_at")
    observed = float(observed) if isinstance(observed, (int, float)) else None

    def unavailable(status: str, reason: str) -> dict[str, Any]:
        return {
            "status": status, "email": None, "observed_plan_type": None,
            "account_id": None,
            "observed_at": observed, "reason": reason,
        }

    if fingerprint is None:
        return unavailable("unavailable", "seat needs login before identity can be verified")
    if cached.get("auth_file_fingerprint") != fingerprint:
        status = "stale" if _identity_text(cached.get("email"), 320) else "unavailable"
        return unavailable(status, "login changed since identity was verified; refresh this seat")
    status = cached.get("status") if cached.get("status") in {"fresh", "stale"} else "unavailable"
    reason = _identity_text(cached.get("reason"), 256)
    stale_seconds = float(settings.get("pool", {}).get("probe_stale_seconds", 6 * 3600))
    if status == "fresh" and observed is not None and time.time() - observed > stale_seconds:
        status = "stale"
        reason = "identity verification is older than the refresh freshness window"
    return {
        "status": status, "email": _identity_text(cached.get("email"), 320),
        "observed_plan_type": _identity_text(cached.get("observed_plan_type"), 128),
        "account_id": _identity_text(cached.get("account_id"), 256),
        "observed_at": observed, "reason": reason,
    }


def banked_reset_report(seat: dict[str, Any], settings: dict[str, Any], at: float | None = None,
                        *, root: Path | None = None, seat_id: str | None = None) -> dict[str, Any]:
    bank = seat.get("banked_resets")
    bank = bank if isinstance(bank, dict) else {}
    observed = bank.get("observed_at")
    current = time.time() if at is None else at
    freshness = float(settings.get("pool", {}).get("probe_stale_seconds", 21600))
    status = bank.get("status", "unknown")
    if status == "fresh" and (not isinstance(observed, (int, float)) or current - observed > freshness):
        status = "stale"
    login_changed = False
    if root is not None and seat_id is not None:
        fingerprint = _auth_file_fingerprint(runtime_seat_home(root, seat_id, settings))
        if fingerprint is None or bank.get("auth_fingerprint") != json.dumps(fingerprint, sort_keys=True):
            status = "stale" if bank.get("available_count") is not None else "unknown"
            login_changed = True
    attempts = bank.get("attempts")
    account_key = bank.get("account_key")
    attempts = [item for item in attempts if isinstance(item, dict)
                and (account_key is None or item.get("account_key") == account_key)] if isinstance(attempts, list) else []
    history = bank.get("history")
    credits = bank.get("credits")
    return {"status": status, "available_count": None if login_changed else bank.get("available_count"),
            "credits": [] if login_changed else [item for item in credits if isinstance(item, dict)] if isinstance(credits, list) else [],
            "details_available": False if login_changed else bank.get("details_available", False),
            "observed_at": observed, "last_attempt_at": bank.get("last_attempt_at"),
            "last_error": "login changed since reset credits were observed" if login_changed else bank.get("last_error"),
            "history": history if isinstance(history, list) else [],
            "observed_grants": bank.get("observed_grants", 0),
            "observed_redemptions": bank.get("observed_redemptions", 0),
            "observed_expirations": bank.get("observed_expirations", 0),
            "last_manual_outcome": attempts[-1].get("outcome") if attempts else None}


def _redeem_authority(*, current_user_request: bool = False) -> None:
    from .controller_context import current_controller
    import sys
    context = current_controller()
    if context.managed_worker or os.environ.get("CLAUDE_LEAD_WORKER") == "1":
        raise AccountReadError("workers cannot redeem banked resets")
    if context.is_controller:
        if not current_user_request:
            raise AccountReadError("controller redemption requires current user authority")
    elif not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise AccountReadError("redemption requires an interactive human terminal")


def _registry_digest(root: Path, payload: bytes) -> str:
    try:
        info = (root / "settings.json").lstat()
    except OSError as exc:
        raise AccountReadError("seat registry changed while it was read") from exc
    if not stat.S_ISREG(info.st_mode):
        raise AccountReadError("seat registry changed while it was read")
    marker = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
    return hashlib.sha256(payload + repr(marker).encode()).hexdigest()


def _registry_unlocked(root: Path, settings: dict[str, Any], seat_id: str) -> tuple[Path, str, str]:
    """Validate one pinned, owner-controlled schema-3 registry under settings_lock."""
    from .state import SettingsMutationError, _load_settings_for_mutation
    try:
        current, _before, payload, _warnings = _load_settings_for_mutation(
            root, require_existing=True
        )
    except SettingsMutationError as exc:
        raise AccountReadError(f"seat registry changed or is invalid: {exc}") from exc
    entry = current.get("pool", {}).get("seats", {}).get(seat_id)
    expected = settings.get("pool", {}).get("seats", {}).get(seat_id)
    if entry != expected:
        raise AccountReadError("seat registry changed or is invalid; reload settings")
    if not isinstance(entry, dict) or entry.get("lifecycle") not in {"active", "draining"}:
        raise AccountReadError("reset redemption requires a registered active or draining seat")
    if not has_auth(root, seat_id, current):
        raise AccountReadError(f"{seat_id} needs login")
    home = runtime_seat_home(root, seat_id, current)
    fingerprint = _auth_file_fingerprint(home)
    if fingerprint is None:
        raise AccountReadError("login metadata is unavailable")
    return home, json.dumps(fingerprint, sort_keys=True), _registry_digest(root, payload)


def _registry_snapshot(root: Path, settings: dict[str, Any], seat_id: str) -> tuple[Path, str, str]:
    from .state import settings_lock
    with settings_lock(root):
        return _registry_unlocked(root, settings, seat_id)


def _preview_credit(payload: dict[str, Any]) -> tuple[int, dict[str, Any], str]:
    summary = payload.get("rateLimitResetCredits")
    if not isinstance(summary, dict) or type(summary.get("availableCount")) is not int:
        raise AccountReadError("fresh reset credit metadata is unavailable")
    count = summary["availableCount"]
    if count <= 0:
        raise AccountReadError("no banked reset is available")
    raw = summary.get("credits")
    if not isinstance(raw, list):
        raise AccountReadError("individual reset credit identity is unavailable")
    available = [item for item in raw if isinstance(item, dict)
                 and item.get("status") == "available" and _credit_detail(item)
                 and (_credit_detail(item)["expires_at"] is None
                      or _credit_detail(item)["expires_at"] > time.time())]
    if not available:
        raise AccountReadError("no identified available reset credit was reported")
    available.sort(key=lambda item: (_credit_detail(item)["expires_at"] or float("inf"),
                                     str(item["id"])))
    selected = available[0]
    return count, _credit_detail(selected), selected["id"]


def _provider_identity(payload: dict[str, Any]) -> str:
    envelope = payload.get(_MANAGER_IDENTITY_PAYLOAD)
    account = envelope.get("account", {}).get("account") if isinstance(envelope, dict) else None
    email = _identity_text(account.get("email"), 320) if isinstance(account, dict) else None
    if not isinstance(account, dict) or account.get("type") != "chatgpt" or not email:
        raise AccountReadError("fresh provider account identity is unavailable")
    account_id = _identity_text(payload.get("accountId"), 256)
    return json.dumps({"email": email, "account_id": account_id,
                       "id": _identity_text(account.get("id"), 256)}, sort_keys=True)


def prepare_banked_reset_redemption(root: Path, settings: dict[str, Any], seat_id: str,
                                    *, cancel_event: threading.Event | None = None) -> dict[str, Any]:
    if current_controller_is_worker():
        raise AccountReadError("workers cannot prepare redemption")
    home, auth, digest = _registry_snapshot(root, settings, seat_id)
    from .workers import resolve_executable
    codex = resolve_executable("codex")
    scratch = root / "scratch"
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    timeout = float(settings.get("pool", {}).get("account_probe_timeout_seconds", 30))
    payload = read_account_rate_limits(codex, home, scratch, timeout=timeout,
                                       cancel_event=cancel_event, include_manager_identity=True)
    if cancel_event is not None and cancel_event.is_set():
        raise AccountReadError("redemption preview canceled")
    current_home, current_auth, current_digest = _registry_snapshot(root, settings, seat_id)
    if current_home != home or current_auth != auth or current_digest != digest:
        raise AccountReadError("login or registry changed during redemption preview")
    observed = time.time()
    validated_account_limits(payload, observed)
    count, detail, credit_id = _preview_credit(payload)
    identity = _provider_identity(payload)
    from .state import settings_lock
    with settings_lock(root):
        latest_home, latest_auth, latest_digest = _registry_unlocked(root, settings, seat_id)
        if (latest_home != home or latest_auth != auth or latest_digest != digest):
            raise AccountReadError("login or registry changed during redemption preview")
        with Ledger(root) as data:
            seat = data["seats"][seat_id]
            if seat.get("in_flight"):
                raise AccountReadError("seat is busy; wait for its claim to finish")
            bank = seat.get("banked_resets")
            bank = bank if isinstance(bank, dict) else {}
            attempts = bank.get("attempts", [])
            if not isinstance(attempts, list) or any(not isinstance(item, dict) for item in attempts):
                raise AccountReadError("reset attempt journal is invalid")
            if any(item.get("account_key") in (None, identity)
                   and item.get("credit_fingerprint") == detail["fingerprint"] for item in attempts):
                raise AccountReadError("this credit already has a consume attempt; inspect its outcome")
            apply_account_snapshot(seat, payload, observed)
            seat["banked_resets"]["auth_fingerprint"] = auth
    return {"seat": seat_id, "home": str(home), "auth_fingerprint": auth,
            "provider_identity": identity, "available_count": count,
            "credit": detail, "observed_at": observed, "_credit_id": credit_id,
            "settings_digest": digest}


def current_controller_is_worker() -> bool:
    from .controller_context import current_controller
    return current_controller().managed_worker or os.environ.get("CLAUDE_LEAD_WORKER") == "1"


def redeem_banked_reset(root: Path, settings: dict[str, Any], preview: dict[str, Any], *,
                        current_user_request: bool = False,
                        cancel_event: threading.Event | None = None) -> dict[str, Any]:
    _redeem_authority(current_user_request=current_user_request)
    seat_id = preview["seat"]
    home, auth, digest = _registry_snapshot(root, settings, seat_id)
    if digest != preview.get("settings_digest"):
        raise AccountReadError("settings changed since preview")
    if str(home) != preview["home"]:
        raise AccountReadError("seat home changed since preview")
    from .workers import resolve_executable
    from .state import settings_lock
    codex = resolve_executable("codex")
    scratch = root / "scratch"
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    timeout = float(settings.get("pool", {}).get("account_probe_timeout_seconds", 30))
    claim_token = str(uuid.uuid4())
    claim = {"dispatcher_pid": os.getpid(), "redemption_token": claim_token}
    attempted = False
    token = str(uuid.uuid4())
    account_key = preview["provider_identity"]

    def before_consume(payload: dict[str, Any]) -> tuple[str, str]:
        nonlocal attempted, auth
        validated_account_limits(payload, time.time())
        count, detail, credit_id = _preview_credit(payload)
        if (count != preview["available_count"] or detail != preview["credit"]
                or credit_id != preview["_credit_id"] or _provider_identity(payload) != account_key):
            raise AccountReadError("account or available credit changed since preview")
        with settings_lock(root):
            current_home, current_auth, current_digest = _registry_unlocked(root, settings, seat_id)
            if (current_home != home or current_digest != digest
                    or current_auth != auth):
                raise AccountReadError("seat home, registry, or login changed before consume")
            if cancel_event is not None and cancel_event.is_set():
                raise AccountReadError("redemption canceled before consume")
            with Ledger(root) as data:
                seat = data["seats"][seat_id]
                if seat.get("in_flight") != claim:
                    raise AccountReadError("seat claim changed before consume")
                bank = seat.get("banked_resets")
                if not isinstance(bank, dict):
                    raise AccountReadError("reset attempt journal is invalid")
                attempts = bank.setdefault("attempts", [])
                if not isinstance(attempts, list) or any(not isinstance(item, dict) for item in attempts):
                    raise AccountReadError("reset attempt journal is invalid")
                if any(item.get("account_key") in (None, account_key)
                       and item.get("credit_fingerprint") == detail["fingerprint"] for item in attempts):
                    raise AccountReadError("credit already has a consume attempt")
                attempts.append({"token": token, "account_key": account_key,
                                 "credit_fingerprint": detail["fingerprint"],
                                 "at": time.time(), "outcome": "pending"})
                bank.update(status="stale", last_attempt_at=time.time(),
                            last_error="manual reset pending; awaiting a fresh provider observation")
        attempted = True
        return token, credit_id

    with settings_lock(root):
        current_home, current_auth, current_digest = _registry_unlocked(root, settings, seat_id)
        if current_home != home or current_digest != digest:
            raise AccountReadError("seat home or registry changed since preview")
        # A provider metadata rotation can be accepted only after the native
        # account read below proves the same identity. A replacement login is
        # caught by that read; later rotation before consume remains a refusal.
        auth = current_auth
        with Ledger(root) as data:
            seat = data["seats"][seat_id]
            if seat.get("in_flight"):
                raise AccountReadError("seat is busy; wait for its claim to finish")
            seat["in_flight"] = claim
    try:
        try:
            result = _app_server_call(
                codex, home, scratch, "account/rateLimits/read", timeout=timeout,
                cancel_event=cancel_event, before_consume=before_consume,
            )
            outcome, error = result["outcome"], None
        except (AccountReadError, OSError) as exc:
            if not attempted:
                raise
            outcome, error = "unknown", str(exc)
        with Ledger(root) as data:
            bank = data["seats"][seat_id].get("banked_resets")
            attempts = bank.get("attempts", []) if isinstance(bank, dict) else []
            if isinstance(attempts, list):
                for item in attempts:
                    if isinstance(item, dict) and item.get("token") == token:
                        item.update(outcome=outcome, error=error)
                        break
        refresh_error = None
        try:
            fresh = read_account_rate_limits(codex, home, scratch, timeout=timeout,
                                             cancel_event=cancel_event,
                                             include_manager_identity=True)
            if _provider_identity(fresh) != account_key:
                raise AccountReadError("account changed after consume; refresh not published")
            with settings_lock(root):
                current_home, current_auth, current_digest = _registry_unlocked(root, settings, seat_id)
                if (current_home != home or current_auth != auth or current_digest != digest):
                    raise AccountReadError("target changed after consume; refresh not published")
                with Ledger(root) as data:
                    seat = data["seats"][seat_id]
                    if seat.get("in_flight") != claim:
                        raise AccountReadError("seat claim changed after consume")
                    apply_account_snapshot(seat, fresh, time.time())
                    seat["banked_resets"]["auth_fingerprint"] = auth
        except (AccountReadError, OSError) as exc:
            refresh_error = str(exc)
        return {"seat": seat_id, "outcome": outcome, "error": error,
                "refresh_error": refresh_error, "token": token}
    finally:
        with Ledger(root) as data:
            seat = data.get("seats", {}).get(seat_id)
            if isinstance(seat, dict) and seat.get("in_flight") == claim:
                seat["in_flight"] = None


def refresh_account_usage(
    root: Path,
    settings: dict[str, Any],
    targets: list[str] | None = None,
    *,
    cancel_event: threading.Event | None = None,
) -> list[str]:
    """Refresh read-only Codex account windows in parallel."""
    from .workers import AUTH_ERR, quarantine_auth, recover_auth, resolve_executable

    probeable = set(
        registered_seat_ids(
            settings, lifecycles=("active", "draining", "inactive")
        )
    )
    defaults = registered_seat_ids(
        settings, lifecycles=("active", "draining")
    )
    requested = targets if targets is not None else defaults
    registered = set(registered_seat_ids(settings))
    for seat_id in requested:
        if seat_id not in registered:
            raise AccountReadError(
                f"unknown Codex seat: {seat_id} is not registered"
            )
        if seat_id not in probeable:
            raise AccountReadError(f"{seat_id} is retired and cannot be probed")
    selected = list(dict.fromkeys(requested))
    if not selected:
        return []
    try:
        codex = resolve_executable("codex")
    except FileNotFoundError as exc:
        return [f"account: {exc}"]
    scratch = root / "scratch"
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    timeout = float(settings.get("pool", {}).get("account_probe_timeout_seconds", 30))
    results: dict[
        str, tuple[dict[str, Any] | None, str | None, float, dict[str, int] | None]
    ] = {}

    def isolated_read(seat_id: str, home: Path) -> dict[str, Any]:
        """Retry without a busy seat's SQLite runtime, using a private auth copy."""
        with tempfile.TemporaryDirectory(prefix=f"account-{seat_id}-", dir=scratch) as raw:
            isolated = Path(raw)
            os.chmod(isolated, 0o700)
            shutil.copy2(home / "auth.json", isolated / "auth.json")
            config = home / "config.toml"
            if config.is_file():
                shutil.copy2(config, isolated / "config.toml")
            os.chmod(isolated / "auth.json", 0o600)
            return read_account_rate_limits(
                codex,
                isolated,
                isolated,
                timeout=timeout,
                cancel_event=cancel_event,
                include_manager_identity=True,
            )

    def read_one(
        seat_id: str,
    ) -> tuple[dict[str, Any] | None, str | None, float, dict[str, int] | None]:
        observed_at = time.time()
        if not has_auth(root, seat_id, settings):
            return None, "needs-login", observed_at, None
        home = runtime_seat_home(root, seat_id, settings)
        before_auth = _auth_file_fingerprint(home)
        try:
            payload = read_account_rate_limits(
                codex,
                home,
                scratch,
                timeout=timeout,
                cancel_event=cancel_event,
                include_manager_identity=True,
            )
            after_auth = _auth_file_fingerprint(home)
            if after_auth != before_auth:
                return None, "login changed during account read", time.time(), after_auth
            return payload, None, time.time(), after_auth
        except AccountReadError as exc:
            error = str(exc)
            if "sqlite state runtime" in error or "initialize state runtime" in error:
                try:
                    retry_before_auth = _auth_file_fingerprint(home)
                    retried = isolated_read(seat_id, home)
                    retry_after_auth = _auth_file_fingerprint(home)
                    if retry_before_auth != retry_after_auth:
                        return None, "login changed during isolated account read", time.time(), retry_after_auth
                    return (
                        retried, None, time.time(), retry_after_auth,
                    )
                except (AccountReadError, OSError) as fallback:
                    error = f"{error}; isolated retry failed: {fallback}"
            return None, error, time.time(), _auth_file_fingerprint(home)

    with ThreadPoolExecutor(max_workers=min(5, len(selected))) as executor:
        futures = {executor.submit(read_one, seat_id): seat_id for seat_id in selected}
        for future in as_completed(futures):
            seat_id = futures[future]
            try:
                results[seat_id] = future.result()
            except Exception as exc:  # defensive: one seat must not suppress the others
                results[seat_id] = (
                    None, str(exc), time.time(),
                    _auth_file_fingerprint(runtime_seat_home(root, seat_id, settings)),
                )

    messages: list[str] = []
    with Ledger(root) as data:
        for seat_id in selected:
            payload, error, observed_at, auth_fingerprint = results[seat_id]
            seat = data["seats"][seat_id]
            if payload is not None:
                apply_manager_identity_snapshot(seat, payload, observed_at, auth_fingerprint)
                try:
                    apply_account_snapshot(seat, payload, observed_at)
                except AccountReadError as exc:
                    error = str(exc)
                    record_account_observation_failure(seat, observed_at, error)
                    seat.setdefault("banked_resets", {}).update(status="stale", last_attempt_at=observed_at, last_error=error)
                    messages.append(f"{seat_id} account -> unavailable ({error})")
                else:
                    recover_auth(seat)
                    seat.setdefault("banked_resets", {})["auth_fingerprint"] = json.dumps(auth_fingerprint, sort_keys=True) if auth_fingerprint else None
                    messages.append(f"{seat_id} account -> ok")
            else:
                _identity_failure(
                    seat, observed_at, auth_fingerprint,
                    "provider account metadata was unavailable during refresh",
                )
                record_account_observation_failure(seat, observed_at, error)
                seat.setdefault("banked_resets", {}).update(status="stale", last_attempt_at=observed_at, last_error=error)
                if error and AUTH_ERR.search(error):
                    quarantine_auth(seat, error)
                messages.append(f"{seat_id} account -> unavailable ({error})")
    return messages
