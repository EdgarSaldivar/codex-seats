from __future__ import annotations

from pathlib import Path
import threading

from codex_seats.seat_admin import add_seat, retire_seat
from codex_seats.state import load_settings, default_settings
from codex_seats.watch import WatchRefresher


NOW = 1_788_454_400.0


def _prepare_root(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    import json

    (tmp_path / "settings.json").write_text(
        json.dumps(default_settings(), indent=2) + "\n"
    )
    (tmp_path / "settings.json").chmod(0o600)
    return tmp_path


def _refresher(root: Path, account_refresh) -> WatchRefresher:
    settings, _ = load_settings(root)
    refresher = WatchRefresher(
        root,
        settings,
        300,
        account_refresh=account_refresh,
    )
    refresher._selected_retry_delay = 0.0
    return refresher


def test_selected_refresh_targets_only_the_inactive_seat(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path)
    seat_id = add_seat(root, label="Inactive", _clock=lambda: NOW).seat_id
    calls: list[list[str]] = []

    def account_refresh(_root, _settings, targets, **_kwargs):
        calls.append(list(targets))
        return [f"{targets[0]} account -> ok"]

    refresher = _refresher(root, account_refresh)

    status = refresher.run_selected_attempt(seat_id)

    assert status == "ok"
    assert calls == [[seat_id]]  # only the one selected seat
    snapshot = refresher.selected_snapshot()
    assert snapshot.last_target == seat_id
    assert snapshot.last_succeeded is True
    assert snapshot.generation == 1


def test_selected_refresh_rejects_missing_target(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path)
    calls: list[list[str]] = []
    refresher = _refresher(
        root, lambda *_a, **_k: calls.append([]) or []
    )

    status = refresher.run_selected_attempt("seat9")

    assert status == "invalid"
    assert calls == []  # rejected before any provider call
    assert refresher.selected_snapshot().last_succeeded is False


def test_selected_refresh_rejects_retired_target(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path)
    seat_id = add_seat(root, label="Gone", _clock=lambda: NOW).seat_id
    retire_seat(root, seat_id)
    calls: list[list[str]] = []
    refresher = _refresher(
        root, lambda *_a, **_k: calls.append([]) or []
    )

    status = refresher.run_selected_attempt(seat_id)

    assert status == "invalid"
    assert "retired" in (refresher.selected_snapshot().last_result or "")
    assert calls == []


def test_selected_refresh_retains_intent_on_shared_lock_contention(
    tmp_path: Path,
) -> None:
    root = _prepare_root(tmp_path)
    seat_id = add_seat(root, label="Busy", _clock=lambda: NOW).seat_id
    calls: list[list[str]] = []
    refresher = _refresher(
        root, lambda *_a, **_k: calls.append([]) or []
    )
    refresher._selected_lock_attempts = 2

    # Hold the shared refresh lock so the selected attempt cannot acquire it.
    held = refresher._acquire_lock()
    assert held is not None
    try:
        status = refresher.run_selected_attempt(seat_id)
    finally:
        import fcntl
        import os

        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)

    assert status == "contended"
    assert calls == []  # never ran the provider without the lock
    # Pending intent is retained for a later cycle, last-good is preserved.
    assert refresher.selected_snapshot().queued == seat_id
    assert refresher.selected_snapshot().last_succeeded is None


def test_selected_refresh_is_cancelable(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path)
    seat_id = add_seat(root, label="Cancel", _clock=lambda: NOW).seat_id
    calls: list[list[str]] = []
    refresher = _refresher(
        root, lambda *_a, **_k: calls.append([]) or []
    )
    refresher.cancel_event.set()

    status = refresher.run_selected_attempt(seat_id)

    assert status == "canceled"
    assert calls == []
    assert refresher.selected_snapshot().queued == seat_id  # intent retained


def test_request_coalesces_to_latest_seat(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path)
    refresher = _refresher(root, lambda *_a, **_k: [])

    refresher.request_selected_refresh("seat1")
    refresher.request_selected_refresh("seat2")

    assert refresher.selected_snapshot().queued == "seat2"
    assert refresher._wake.is_set()


def test_periodic_refresh_and_last_good_survive_selected_failure(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path)
    seat_id = add_seat(root, label="Seat", _clock=lambda: NOW).seat_id
    refresher = _refresher(
        root, lambda *_a, **_k: [f"{seat_id} account -> error"]
    )

    status = refresher.run_selected_attempt(seat_id)

    assert status == "failed"
    # Periodic snapshot is untouched by a selected refresh failure.
    assert refresher.snapshot().completion_generation == 0
    assert refresher.snapshot().last_result is None


def test_selected_refresh_fails_closed_when_reload_fails_after_lock(
    tmp_path: Path, monkeypatch
) -> None:
    root = _prepare_root(tmp_path)
    seat_id = add_seat(root, label="Seat", _clock=lambda: NOW).seat_id
    calls: list[list[str]] = []
    refresher = _refresher(
        root, lambda *_a, **_k: calls.append([]) or []
    )
    monkeypatch.setattr(
        refresher,
        "_latest_settings",
        lambda: (_ for _ in ()).throw(OSError("fixture read failed")),
    )

    assert refresher.run_selected_attempt(seat_id) == "failed"
    assert calls == []
    assert "failed closed" in (refresher.selected_snapshot().last_result or "")


def test_request_only_mode_services_manual_request_without_periodic_probe(
    tmp_path: Path
) -> None:
    root = _prepare_root(tmp_path)
    seat_id = add_seat(root, label="Manual", _clock=lambda: NOW).seat_id
    calls: list[list[str]] = []
    settings, _ = load_settings(root)
    refresher = WatchRefresher(
        root,
        settings,
        300,
        periodic=False,
        account_refresh=lambda _r, _s, targets, **_k: (
            calls.append(list(targets)) or [f"{seat_id} account -> ok"]
        ),
    )
    refresher.start()
    refresher.request_selected_refresh(seat_id)
    deadline = __import__("time").monotonic() + 2
    while refresher.selected_snapshot().generation == 0:
        assert __import__("time").monotonic() < deadline
        threading.Event().wait(0.01)

    assert calls == [[seat_id]]
    assert refresher.snapshot().completion_generation == 0
    assert refresher.stop(timeout=2)


def test_refresh_request_after_close_is_rejected(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path)
    refresher = _refresher(root, lambda *_a, **_k: [])
    assert refresher.stop(timeout=1)

    import pytest

    with pytest.raises(RuntimeError, match="closed"):
        refresher.request_selected_refresh("seat1")


def test_selected_refresh_revalidates_retirement_after_lock_contention(
    tmp_path: Path, monkeypatch
) -> None:
    root = _prepare_root(tmp_path)
    seat_id = add_seat(root, label="Racing", _clock=lambda: NOW).seat_id
    calls: list[list[str]] = []
    refresher = _refresher(
        root, lambda _r, _s, targets, **_k: calls.append(list(targets)) or []
    )
    real_acquire = refresher._acquire_lock
    attempts = 0

    def contend_then_retire():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            retire_seat(root, seat_id)
            return None
        return real_acquire()

    monkeypatch.setattr(refresher, "_acquire_lock", contend_then_retire)
    assert refresher.run_selected_attempt(seat_id) == "invalid"
    assert calls == []
    assert "retired" in (refresher.selected_snapshot().last_result or "")


def test_stop_is_bounded_and_reports_unfinished_provider_work(tmp_path: Path) -> None:
    root = _prepare_root(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def blocked_provider(*_args, **_kwargs):
        entered.set()
        release.wait(2)
        return []

    refresher = _refresher(root, blocked_provider)
    refresher.start()
    assert entered.wait(1)

    assert refresher.stop(timeout=0.01) is False
    release.set()
    deadline = __import__("time").monotonic() + 2
    while refresher._thread is not None and refresher._thread.is_alive():
        assert __import__("time").monotonic() < deadline
        threading.Event().wait(0.01)
    assert refresher.stop(timeout=0.1) is True
