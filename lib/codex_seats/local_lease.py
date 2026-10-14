"""Optional renewable admission leases for intermittently available local hosts."""

from __future__ import annotations

import http.client
import json
import re
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


MAX_RESPONSE_BYTES = 4096
LEASE_ID_RE = re.compile(r"\A[A-Za-z0-9_.-]{1,128}\Z")


class LocalLeaseUnavailable(RuntimeError):
    """The host could not grant admission before local execution began."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class LocalLease:
    base_url: str
    lease_id: str
    lost: threading.Event
    stop: threading.Event
    thread: threading.Thread | None = None


def _request(base_url: str, method: str, path: str, *, timeout: float) -> bytes:
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        method=method,
        headers={"Accept": "application/json", "User-Agent": "codex-seats-local/1"},
    )
    # Admission is a direct controller-to-host control path. Environment proxy
    # variables must not redirect it through an unrelated service.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise LocalLeaseUnavailable("local lease response exceeded its size limit")
            if method == "POST" and path == "/lease" and response.status != 201:
                raise LocalLeaseUnavailable(
                    f"local lease acquisition returned HTTP {response.status}"
                )
            if path != "/lease" and response.status != 204:
                raise LocalLeaseUnavailable(
                    f"local lease operation returned HTTP {response.status}"
                )
            return payload
    except urllib.error.HTTPError as exc:
        if exc.code in {404, 409}:
            detail = (
                "local host is entering sleep"
                if exc.code == 409
                else "local lease no longer exists"
            )
            raise LocalLeaseUnavailable(detail, retryable=False) from exc
        raise LocalLeaseUnavailable(
            f"local lease returned HTTP {exc.code}", retryable=exc.code >= 500
        ) from exc
    except (
        OSError,
        TimeoutError,
        urllib.error.URLError,
        http.client.HTTPException,
    ) as exc:
        raise LocalLeaseUnavailable(f"local lease is unreachable: {exc}") from exc


def _acquire(base_url: str) -> tuple[str, float]:
    payload = _request(base_url, "POST", "/lease", timeout=10)
    try:
        value = json.loads(payload)
    except (UnicodeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise LocalLeaseUnavailable("local lease returned malformed JSON") from exc
    lease_id = value.get("id") if isinstance(value, dict) else None
    ttl = value.get("ttl_seconds") if isinstance(value, dict) else None
    if not isinstance(lease_id, str) or LEASE_ID_RE.fullmatch(lease_id) is None:
        raise LocalLeaseUnavailable("local lease returned an invalid id")
    if (
        not isinstance(ttl, (int, float))
        or isinstance(ttl, bool)
        or not 15 <= float(ttl) <= 3600
    ):
        raise LocalLeaseUnavailable("local lease returned an invalid TTL")
    return lease_id, float(ttl)


def _renew(lease: LocalLease, interval: float) -> None:
    path = f"/lease/{lease.lease_id}/renew"
    transient_failures = 0
    while not lease.stop.wait(interval):
        try:
            _request(lease.base_url, "POST", path, timeout=10)
            transient_failures = 0
        except LocalLeaseUnavailable as exc:
            if not exc.retryable:
                lease.lost.set()
                return
            transient_failures += 1
            if transient_failures >= 2:
                lease.lost.set()
                return


@contextmanager
def hold(base_url: str | None) -> Iterator[LocalLease | None]:
    """Acquire, renew, and best-effort release one host admission lease."""
    if base_url is None:
        yield None
        return
    lease_id, ttl = _acquire(base_url)
    lost = threading.Event()
    stop = threading.Event()
    lease = LocalLease(base_url.rstrip("/"), lease_id, lost, stop)
    interval = min(45.0, max(5.0, ttl / 3.0))
    thread = threading.Thread(
        target=_renew,
        args=(lease, interval),
        name="codex-seats-local-lease",
        daemon=True,
    )
    lease.thread = thread
    thread.start()
    try:
        yield lease
    finally:
        stop.set()
        thread.join(timeout=1)
        try:
            _request(lease.base_url, "DELETE", f"/lease/{lease_id}", timeout=5)
        except LocalLeaseUnavailable:
            pass
