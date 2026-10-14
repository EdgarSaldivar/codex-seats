from __future__ import annotations

import json
import http.client
import threading

import pytest

from codex_seats import local_lease


def test_acquire_validates_id_and_ttl(monkeypatch):
    monkeypatch.setattr(
        local_lease,
        "_request",
        lambda *args, **kwargs: json.dumps(
            {"id": "l17-safe", "ttl_seconds": 120}
        ).encode(),
    )
    assert local_lease._acquire("http://studio:8002") == ("l17-safe", 120.0)


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"{}",
        b'{"id":"../bad","ttl_seconds":120}',
        b'{"id":"ok","ttl_seconds":1}',
        b'{"id":"ok","ttl_seconds":true}',
    ],
)
def test_acquire_rejects_malformed_contract(monkeypatch, payload):
    monkeypatch.setattr(local_lease, "_request", lambda *args, **kwargs: payload)
    with pytest.raises(local_lease.LocalLeaseUnavailable):
        local_lease._acquire("http://studio:8002")


def test_hold_none_performs_no_io(monkeypatch):
    monkeypatch.setattr(
        local_lease,
        "_acquire",
        lambda *args, **kwargs: pytest.fail("lease acquisition occurred"),
    )
    with local_lease.hold(None) as lease:
        assert lease is None


def test_hold_releases_exact_lease(monkeypatch):
    calls = []
    monkeypatch.setattr(local_lease, "_acquire", lambda _url: ("lease-1", 3600.0))
    monkeypatch.setattr(
        local_lease,
        "_request",
        lambda base, method, path, timeout: calls.append(
            (base, method, path, timeout)
        )
        or b"",
    )
    with local_lease.hold("http://studio:8002/") as lease:
        assert lease is not None and not lease.lost.is_set()
    assert calls == [
        ("http://studio:8002", "DELETE", "/lease/lease-1", 5)
    ]


def test_failed_renewal_marks_lease_lost(monkeypatch):
    lease = local_lease.LocalLease(
        "http://studio:8002",
        "lease-1",
        threading.Event(),
        threading.Event(),
    )
    monkeypatch.setattr(
        local_lease,
        "_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            local_lease.LocalLeaseUnavailable("gaming")
        ),
    )
    local_lease._renew(lease, 0)
    assert lease.lost.is_set()


def test_one_transient_renewal_failure_is_tolerated(monkeypatch):
    lease = local_lease.LocalLease(
        "http://studio:8002",
        "lease-1",
        threading.Event(),
        threading.Event(),
    )
    calls = 0

    def request(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise local_lease.LocalLeaseUnavailable("temporary disconnect")
        lease.stop.set()
        return b""

    monkeypatch.setattr(local_lease, "_request", request)
    local_lease._renew(lease, 0)
    assert calls == 2
    assert not lease.lost.is_set()


def test_http_protocol_disconnect_is_normalized(monkeypatch):
    class Opener:
        def open(self, *args, **kwargs):
            raise http.client.RemoteDisconnected("peer closed")

    monkeypatch.setattr(local_lease.urllib.request, "build_opener", lambda *a: Opener())
    with pytest.raises(local_lease.LocalLeaseUnavailable, match="unreachable"):
        local_lease._request("http://studio:8002", "POST", "/lease", timeout=1)
