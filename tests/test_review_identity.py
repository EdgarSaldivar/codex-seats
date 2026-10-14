"""Offline review identity admission, using only synthetic ledger and homes."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from codex_seats import review_identity
from codex_seats.state import Ledger, new_seat, default_settings


@pytest.fixture(autouse=True)
def plain_caller_environment(monkeypatch):
    # Synthetic pools must not inherit the controller running this test suite.
    for marker in (
        "CODEX_SEATS_MANAGED_WORKER", "CLAUDE_LEAD_WORKER", "CODEX_SEATS_HEAD",
        "CODEX_CONTROL_SEAT", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_HOME",
    ):
        monkeypatch.delenv(marker, raising=False)


def pool(root: Path, *, external_primary: bool = False) -> tuple[Path, Path]:
    primary_home = root.parent / "external-primary" if external_primary else root / "seat1"
    secondary_home = root / "custom-runtime" / "seat2"
    settings = default_settings()
    settings["pool"]["seats"] = {
        seat_id: {
            "label": seat_id, "lifecycle": "active", "home": str(home),
            "capacity_profile": "personal-plus", "relative_capacity": 1.0,
            "capacity_source": "operator-measured",
            "capacity_verified_at": "2026-09-03T00:00:00Z",
        }
        for seat_id, home in (("seat1", primary_home), ("seat2", secondary_home))
    }
    settings["pool"]["primary"] = "seat1"
    settings["pool"]["deputy"] = "seat2"
    root.mkdir()
    (root / "settings.json").write_text(json.dumps(settings))
    for home in (primary_home, secondary_home):
        home.mkdir(parents=True)
        marker = home / "auth.json"
        marker.write_text("{}")  # synthetic metadata marker; never a real credential
        marker.chmod(0o600)
    with Ledger(root) as data:
        for seat_id in ("seat1", "seat2"):
            seat = new_seat()
            seat.update({
                "status": "ok", "pct_left": 90,
                "last_probe_limits": {"weekly": {"pct": 90, "window_minutes": 10080}},
                "active_window_observation": {
                    "source": "account", "complete": True, "observed_at": time.time(),
                },
            })
            data["seats"][seat_id] = seat
    return primary_home, secondary_home


def test_custom_root_registered_home_and_scrubbed_environment(tmp_path, monkeypatch):
    root = tmp_path / "pool"
    primary, secondary = pool(root, external_primary=True)
    monkeypatch.setenv("CODEX_SEATS_ROOT", str(root))
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("PYTHONPATH", "unsafe")
    with review_identity.claim_review("codex", seat="seat1") as identity:
        assert identity.home == primary
        assert identity.env["CODEX_HOME"] == str(primary)
        assert "OPENAI_API_KEY" not in identity.env
        assert "PYTHONPATH" not in identity.env
        with Ledger(root) as data:
            assert data["seats"]["seat1"]["in_flight"]["kind"] == "review"
    with review_identity.claim_review("codex", home=secondary) as identity:
        assert identity.seat == "seat2"
        assert identity.home == secondary
    with Ledger(root) as data:
        assert data["seats"]["seat1"]["in_flight"] is None
        assert data["seats"]["seat2"]["in_flight"] is None


def test_exhaustion_and_forced_selection_never_use_default_home(tmp_path, monkeypatch):
    root = tmp_path / "pool"
    primary, _ = pool(root)
    monkeypatch.setenv("CODEX_SEATS_ROOT", str(root))
    with pytest.raises(review_identity.ReviewUnavailable, match="registered"):
        with review_identity.claim_review("codex", home=tmp_path / "personal"):
            pass
    with pytest.raises(review_identity.ReviewUnavailable, match="registered"):
        with review_identity.claim_review("codex", seat="seat99"):
            pass
    with Ledger(root) as data:
        for seat in data["seats"].values():
            seat["in_flight"] = {"job_id": "other", "dispatcher_pid": 1}
    with pytest.raises(review_identity.ReviewUnavailable, match="eligible"):
        with review_identity.claim_review("codex", home=primary):
            pass
    with pytest.raises(review_identity.ReviewUnavailable, match="eligible"):
        with review_identity.claim_review("codex", root=root):
            pass


def test_atomic_concurrent_selection_and_owned_cleanup(tmp_path):
    root = tmp_path / "pool"
    pool(root)
    entered = threading.Barrier(3)
    release = threading.Event()
    selected: list[str] = []

    def worker():
        with review_identity.claim_review("codex", root=root) as identity:
            selected.append(identity.seat)
            entered.wait(timeout=5)
            release.wait(timeout=5)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    entered.wait(timeout=5)
    assert set(selected) == {"seat1", "seat2"}
    release.set()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    with Ledger(root) as data:
        assert all(data["seats"][seat]["in_flight"] is None for seat in selected)
    with review_identity.claim_review("codex", seat="seat1", root=root):
        with Ledger(root) as data:
            data["seats"]["seat1"]["in_flight"] = {"job_id": "successor"}
    with Ledger(root) as data:
        assert data["seats"]["seat1"]["in_flight"] == {"job_id": "successor"}


def test_claude_off_busy_and_owned_release(tmp_path, monkeypatch):
    root = tmp_path / "pool"
    pool(root)
    with pytest.raises(review_identity.ReviewUnavailable, match="off"):
        with review_identity.claim_review("claude", root=root):
            pass
    settings = json.loads((root / "settings.json").read_text())
    settings["providers"]["claude"]["mode"] = "review_only"
    (root / "settings.json").write_text(json.dumps(settings))
    monkeypatch.setattr(review_identity, "claude_task_fits", lambda *a, **k: (True, None))
    with Ledger(root) as data:
        data["providers"]["claude"]["status"] = "ok"
        data["providers"]["claude"]["in_flight"] = {"job_id": "other"}
    with pytest.raises(review_identity.ReviewUnavailable, match="busy"):
        with review_identity.claim_review("claude", root=root):
            pass
    with Ledger(root) as data:
        data["providers"]["claude"]["in_flight"] = None
    with pytest.raises(TimeoutError):
        with review_identity.claim_review("claude", root=root) as identity:
            assert "CODEX_HOME" not in identity.env
            raise TimeoutError("provider timed out")
    with Ledger(root) as data:
        assert data["providers"]["claude"]["in_flight"] is None


def test_pool_less_standalone_is_unavailable(tmp_path):
    with pytest.raises(review_identity.ReviewUnavailable, match="not configured"):
        with review_identity.claim_review("codex", root=tmp_path):
            pass


def test_review_rejects_both_managed_markers_before_claim(tmp_path, monkeypatch):
    root = tmp_path / "pool"
    pool(root)
    for marker in ("CODEX_SEATS_MANAGED_WORKER", "CLAUDE_LEAD_WORKER"):
        monkeypatch.setenv(marker, "1")
        with pytest.raises(review_identity.ReviewUnavailable, match="managed workers"):
            with review_identity.claim_review("codex", seat="seat2", root=root):
                pass
        monkeypatch.delenv(marker)
    with Ledger(root) as data:
        assert all(data["seats"][seat]["in_flight"] is None for seat in ("seat1", "seat2"))


@pytest.mark.parametrize("head_env", [
    {"CODEX_CONTROL_SEAT": "seat1"},
    {"CODEX_THREAD_ID": "synthetic-thread", "CODEX_SESSION_ID": "synthetic-session"},
])
def test_review_excludes_controller_seat_including_explicit_selection(tmp_path, monkeypatch, head_env):
    root = tmp_path / "pool"
    pool(root)
    monkeypatch.delenv("CODEX_SEATS_MANAGED_WORKER", raising=False)
    monkeypatch.delenv("CLAUDE_LEAD_WORKER", raising=False)
    for key, value in head_env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(review_identity.ReviewUnavailable, match="controller seat"):
        with review_identity.claim_review("codex", seat="seat1", root=root):
            pass
    with review_identity.claim_review("codex", root=root) as identity:
        assert identity.seat == "seat2"
    with Ledger(root) as data:
        assert data["seats"]["seat1"]["in_flight"] is None
