import json
import tempfile
import threading
import unittest
import argparse
import io
from pathlib import Path
from unittest import mock

from registry_fixture import registry_settings
from codex_seats.watch import RefreshSnapshot, WatchRefresher


class _Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class WatchRefresherTests(unittest.TestCase):
    def test_structured_snapshot_is_coherent_and_advances_on_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = _Clock()
            observed = []
            refresher = None

            def account_refresh(_root, _settings, _targets, **_kwargs):
                observed.append(refresher.snapshot())
                clock.value += 7
                return [
                    f"seat{index} account -> ok" for index in range(1, 6)
                ]

            refresher = WatchRefresher(
                Path(directory),
                registry_settings(),
                300,
                monotonic=clock,
                wall_time=clock,
                account_refresh=account_refresh,
            )

            initial = refresher.snapshot()
            self.assertEqual(
                initial,
                RefreshSnapshot(
                    refreshing=False,
                    last_result=None,
                    last_succeeded=None,
                    last_finished_at=None,
                    last_success_at=None,
                    next_due=None,
                    completion_generation=0,
                ),
            )

            refresher.run_attempt()

            self.assertEqual(
                observed,
                [
                    RefreshSnapshot(
                        refreshing=True,
                        last_result=None,
                        last_succeeded=None,
                        last_finished_at=None,
                        last_success_at=None,
                        next_due=None,
                        completion_generation=0,
                    )
                ],
            )
            completed = refresher.refresh_snapshot()
            self.assertFalse(completed.refreshing)
            self.assertEqual(completed.last_finished_at, 7.0)
            self.assertEqual(completed.last_success_at, 7.0)
            self.assertEqual(completed.next_due, 307.0)
            self.assertEqual(completed.completion_generation, 1)
            self.assertEqual("Codex 5/5", completed.last_result)

            refresher.run_attempt()
            self.assertEqual(refresher.snapshot().completion_generation, 2)

    def test_failed_attempt_preserves_last_success_time(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = _Clock()

            results = [
                [f"seat{index} account -> ok" for index in range(1, 6)],
                [f"seat{index} account -> ok" for index in range(1, 5)]
                + ["seat5 account -> unavailable (needs-login)"],
            ]

            def account_refresh(_root, _settings, _targets, **_kwargs):
                clock.value += 7
                return results.pop(0)

            refresher = WatchRefresher(
                Path(directory),
                registry_settings(),
                300,
                monotonic=clock,
                wall_time=clock,
                account_refresh=account_refresh,
            )

            refresher.run_attempt()
            self.assertEqual(refresher.snapshot().last_success_at, 7.0)

            refresher.run_attempt()
            snapshot = refresher.snapshot()
            self.assertEqual(snapshot.last_finished_at, 14.0)
            self.assertEqual(snapshot.last_success_at, 7.0)
            self.assertEqual(snapshot.completion_generation, 2)
            self.assertIn("Codex 4/5; seat5 account -> unavailable", snapshot.last_result)

    def test_cached_dashboard_renders_before_background_refresh_starts(self):
        from codex_seats import cli

        events = []

        class FakeRefresher:
            def __init__(self, *_args):
                pass

            def status_line(self):
                return "pending"

            def snapshot(self):
                return RefreshSnapshot(
                    refreshing=False,
                    last_result=None,
                    last_succeeded=None,
                    last_finished_at=None,
                    last_success_at=None,
                    next_due=None,
                    completion_generation=0,
                )

            def start(self):
                events.append("start")

            def stop(self):
                events.append("stop")

        args = argparse.Namespace(
            no_color=True,
            watch=True,
            interval=5,
            probe_interval=300,
            no_active_probe=False,
            forensic=False,
        )
        output = io.StringIO()
        with (
            mock.patch("codex_seats.cli.codex_version", return_value=("codex-cli 0.0", None)),
            mock.patch(
                "codex_seats.cli.dashboard",
                side_effect=lambda *_args, **_kwargs: events.append("render") or "CACHED",
            ),
            mock.patch("codex_seats.watch.WatchRefresher", FakeRefresher),
            mock.patch("codex_seats.cli.time.sleep", side_effect=KeyboardInterrupt),
            mock.patch("sys.stdout", output),
        ):
            self.assertEqual(cli.cmd_dash(args, registry_settings(), []), 0)
        self.assertEqual(events[:2], ["render", "start"])
        self.assertIn("CACHED", output.getvalue())

    def test_runs_immediately_then_interval_after_completed_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = _Clock()
            starts = []
            waits = 0

            def account_refresh(_root, _settings, _targets, **_kwargs):
                starts.append(clock.value)
                clock.value += 7
                return [f"seat{index} account -> ok" for index in range(1, 6)]

            def waiter(delay):
                nonlocal waits
                clock.value += delay
                waits += 1
                return waits == 2

            refresher = WatchRefresher(
                Path(directory),
                registry_settings(),
                300,
                monotonic=clock,
                wall_time=clock,
                waiter=waiter,
                account_refresh=account_refresh,
            )
            refresher.run_forever()
            self.assertEqual(starts, [0.0, 307.0])

    def test_concurrent_watchers_use_one_runtime_refresh_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            root = Path(directory)
            first = WatchRefresher(root, registry_settings(), 300)
            second = WatchRefresher(
                root,
                registry_settings(),
                300,
                account_refresh=lambda *_args, **_kwargs: calls.append(True) or [],
            )
            descriptor = first._acquire_lock()
            self.assertIsNotNone(descriptor)
            try:
                second.run_attempt()
            finally:
                import fcntl
                import os

                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            self.assertEqual(calls, [])
            self.assertIn("shared refresh", second.status_line())
            self.assertEqual(second.snapshot().completion_generation, 0)

    def test_attempt_log_keeps_exact_provider_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            messages = [
                "seat1 account -> ok",
                "seat2 account -> unavailable (timed out)",
                "seat3 account -> ok",
                "seat4 account -> ok",
                "seat5 account -> ok",
            ]
            refresher = WatchRefresher(
                root,
                registry_settings(),
                300,
                account_refresh=lambda *_args, **_kwargs: messages,
            )
            refresher.run_attempt()

            records = [
                json.loads(line)
                for line in (root / "logs" / "watch-refresh.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["event"], "watch-refresh")
            self.assertEqual(records[0]["codex_messages"], messages)
            self.assertIn("Codex 4/5", records[0]["result"])

    def test_single_flight_guard_prevents_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            refresher = WatchRefresher(
                Path(directory),
                registry_settings(),
                300,
                account_refresh=lambda *_args, **_kwargs: calls.append(True) or [],
            )
            refresher.refreshing = True
            refresher.run_attempt()
            self.assertEqual(calls, [])

    def test_stop_cancels_an_in_progress_provider_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            started = threading.Event()
            observed_cancel = threading.Event()

            def account_refresh(_root, _settings, _targets, *, cancel_event):
                started.set()
                if cancel_event.wait(5):
                    observed_cancel.set()
                return []

            refresher = WatchRefresher(
                Path(directory),
                registry_settings(),
                300,
                account_refresh=account_refresh,
            )
            refresher.start()
            self.assertTrue(started.wait(1))
            refresher.stop()
            self.assertTrue(observed_cancel.is_set())
            self.assertFalse(refresher._thread.is_alive())

    def test_no_active_probe_cli_escape_hatch(self):
        from codex_seats.cli import build_parser

        args = build_parser().parse_args(["dash", "--watch", "--no-active-probe"])
        self.assertTrue(args.no_active_probe)
        self.assertIsNone(args.probe_interval)

class RefreshOutcomeFieldTests(unittest.TestCase):
    def _run(self, account_messages):
        with tempfile.TemporaryDirectory() as directory:
            clock = _Clock()

            def account_refresh(_root, _settings, _targets, **_kwargs):
                return account_messages

            refresher = WatchRefresher(
                Path(directory), registry_settings(), 300,
                monotonic=clock, wall_time=clock,
                account_refresh=account_refresh,
            )
            refresher.run_attempt()
            return refresher.snapshot()

    def test_last_succeeded_is_structured_not_prose(self):
        ok = [f"seat{i} account -> ok" for i in range(1, 6)]
        # All five accounts refreshed -> structured success True.
        self.assertIs(self._run(ok).last_succeeded, True)
        # A seat failure -> structured False, independent of message wording.
        self.assertIs(self._run(ok[:4] + ["seat5 account -> error"]).last_succeeded, False)


def _local_settings(*, global_mode="opportunistic", hosts=("studio",), disabled_hosts=()):
    settings = registry_settings()
    settings["providers"]["local"]["mode"] = global_mode
    settings["local_hosts"] = {}
    settings["local_seats"] = {}
    for index, host_id in enumerate((*hosts, *disabled_hosts)):
        settings["local_hosts"][host_id] = {
            "label": host_id.title(),
            "lifecycle": "active",
            "resource_groups": {"gpu0": {"max_concurrency": 1}},
            "base_url": "http://127.0.0.1:8080/v1",
            "availability_probe": {"argv": ["/nonexistent/local-probe"], "timeout_seconds": 1},
        }
        settings["local_seats"][f"seat-{index}"] = {
            "label": f"Seat {index}",
            "lifecycle": "active",
            "mode": "off" if host_id in disabled_hosts else "opportunistic",
            "host": host_id,
            "resource_group": "gpu0",
            "model_profile": "coder",
            "harness": "qwen",
            "capabilities": {"task_shapes": ["execute"], "validation": ["strong"]},
            "max_concurrency": 1,
            "runtime_fingerprint": "sha256:" + "a" * 64,
        }
    return settings


class LocalProbeServiceTests(unittest.TestCase):
    def test_enabled_hosts_follow_global_mode_lifecycle_and_seat_modes(self):
        from codex_seats.watch import enabled_local_hosts

        settings = _local_settings(hosts=("studio", "render"), disabled_hosts=("idle",))
        self.assertEqual(enabled_local_hosts(settings), ["render", "studio"])
        settings["local_hosts"]["render"]["lifecycle"] = "inactive"
        self.assertEqual(enabled_local_hosts(settings), ["studio"])
        settings["providers"]["local"]["mode"] = "off"
        self.assertEqual(enabled_local_hosts(settings), [])

    def test_periodic_probes_each_enabled_host_once_per_ttl_off_the_render_loop(self):
        from codex_seats.watch import LocalProbeService

        with tempfile.TemporaryDirectory() as directory:
            clock = _Clock()
            probes = []
            waits = []

            def probe(_root, _settings, host_id):
                probes.append((host_id, clock.value))
                clock.value += 1
                return {"host": host_id, "state": "ready", "reason": "ready", "observed_at": clock.value}

            def waiter(delay):
                waits.append(delay)
                clock.value += delay
                return len(waits) == 3

            audit = []
            service = LocalProbeService(
                Path(directory),
                _local_settings(hosts=("studio", "render"), disabled_hosts=("idle",)),
                interval=15,
                monotonic=clock,
                wall_time=clock,
                waiter=waiter,
                probe=probe,
                audit=audit.append,
            )
            service.run_forever()

            hosts = [host for host, _ in probes]
            self.assertEqual(hosts[:2], ["render", "studio"])
            self.assertNotIn("idle", hosts)
            self.assertEqual(len(probes), 6)
            for host in ("render", "studio"):
                times = [at for name, at in probes if name == host]
                gaps = [later - earlier for earlier, later in zip(times, times[1:])]
                self.assertTrue(all(14 <= gap <= 16 for gap in gaps), gaps)
            self.assertTrue(all(0 < delay <= 15 for delay in waits), waits)
            # Only transitions are audited, not each TTL heartbeat.
            self.assertEqual(
                sorted((record["host"], record["event"]) for record in audit),
                [("render", "watch-local-probe"), ("studio", "watch-local-probe")],
            )
            snapshot = service.snapshot()
            self.assertEqual(set(snapshot.hosts), {"render", "studio"})
            self.assertEqual(snapshot.hosts["studio"]["state"], "ready")
            self.assertIsNone(snapshot.probing)
            self.assertTrue(service.stop())

    def test_explicit_requests_coalesce_skip_in_flight_and_run_without_periodic(self):
        from codex_seats.watch import LocalProbeService

        with tempfile.TemporaryDirectory() as directory:
            clock = _Clock()
            probes = []
            waits = []

            def probe(_root, _settings, host_id):
                probes.append(host_id)
                return {"host": host_id, "state": "busy", "reason": "host_not_ready", "observed_at": clock.value}

            def waiter(delay):
                waits.append(delay)
                return len(waits) == 2

            service = LocalProbeService(
                Path(directory),
                _local_settings(),
                periodic=False,
                monotonic=clock,
                wall_time=clock,
                waiter=waiter,
                probe=probe,
            )
            service.request_local_probe("studio")
            service.request_local_probe("studio")
            self.assertEqual(service.snapshot().pending, ("studio",))
            service._active = "studio"
            service.request_local_probe("studio")
            self.assertEqual(service.snapshot().pending, ("studio",))
            service._active = None
            service.run_forever()

            # --no-active-probe: no scheduled probes, but the one explicit
            # request is honored exactly once and the loop then idles.
            self.assertEqual(probes, ["studio"])
            self.assertEqual(waits, [3600.0, 3600.0])
            self.assertEqual(service.snapshot().hosts["studio"]["reason"], "host_not_ready")
            self.assertEqual(service.snapshot().hosts["studio"]["source"], "requested")
            service.stop()
            with self.assertRaises(RuntimeError):
                service.request_local_probe("studio")

    def test_global_off_never_probes_and_settings_reload_enables_hosts(self):
        from codex_seats.watch import LocalProbeService

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            documents = [
                _local_settings(global_mode="off"),
                _local_settings(global_mode="off"),
                _local_settings(global_mode="shadow"),
            ]
            loads = []

            def settings_loader():
                loads.append(clock.value)
                return documents[min(len(loads) - 1, len(documents) - 1)]

            probes = []

            def probe(_root, settings, host_id):
                probes.append((host_id, settings["providers"]["local"]["mode"]))
                return {"host": host_id, "state": "ready", "reason": "ready", "observed_at": clock.value}

            waits = []

            def waiter(delay):
                waits.append(delay)
                clock.value += delay
                return len(waits) == 3

            service = LocalProbeService(
                root,
                _local_settings(global_mode="off"),
                interval=15,
                monotonic=clock,
                wall_time=clock,
                waiter=waiter,
                probe=probe,
                settings_loader=settings_loader,
            )
            service.run_forever()
            # Two cycles under global off probe nothing; the reloaded shadow
            # document (observation allowed, no claims) is probed immediately.
            self.assertEqual(probes, [("studio", "shadow")])
            self.assertGreaterEqual(len(loads), 3)
            self.assertFalse((root / "local-runtime").exists())

            # The real probe entry point stays hard-off: an explicit request
            # under global off performs no runtime I/O and records mode_off.
            hard_off = LocalProbeService(
                root, _local_settings(global_mode="off"), periodic=False,
                monotonic=clock, wall_time=clock, waiter=lambda _delay: True,
            )
            hard_off.request_local_probe("studio")
            hard_off.run_forever()
            self.assertEqual(hard_off.snapshot().hosts["studio"]["reason"], "mode_off")
            self.assertFalse((root / "local-runtime").exists())

    def test_settings_load_failure_fails_closed_without_probing(self):
        from codex_seats.watch import LocalProbeService

        with tempfile.TemporaryDirectory() as directory:
            clock = _Clock()
            probes = []

            def loader():
                raise OSError("settings unreadable")

            service = LocalProbeService(
                Path(directory),
                _local_settings(),
                interval=15,
                monotonic=clock,
                wall_time=clock,
                waiter=lambda _delay: True,
                probe=lambda *_args: probes.append(True) or {},
                settings_loader=loader,
            )
            service.run_forever()
            self.assertEqual(probes, [])
            self.assertIn("settings unreadable", service.snapshot().last_error)

    def test_refresher_attaches_local_probes_only_when_requested(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plain = WatchRefresher(root, _local_settings(), 300)
            self.assertIsNone(plain.local)
            self.assertFalse(plain.local_snapshot().enabled)
            with self.assertRaises(RuntimeError):
                plain.request_local_probe("studio")

            probes = []

            def account_refresh(_root, _settings, _targets, **_kwargs):
                return [f"seat{index} account -> ok" for index in range(1, 6)]

            attached = WatchRefresher(
                root,
                _local_settings(),
                300,
                account_refresh=account_refresh,
                periodic=False,
                local_probes=True,
            )
            self.assertIsNotNone(attached.local)
            self.assertFalse(attached.local.periodic)
            self.assertEqual(attached.local.settings_loader, attached._latest_settings)
            attached.local.probe = lambda _root, _settings, host: probes.append(host) or {
                "host": host, "state": "ready", "reason": "ready"
            }
            attached.start()
            try:
                self.assertTrue(attached.local._thread.is_alive())
                self.assertEqual(attached.local._thread.name, "cxwatch-local-probe")
                attached.request_local_probe("studio")
                deadline = threading.Event()
                for _ in range(50):
                    if attached.local_snapshot().hosts.get("studio"):
                        break
                    deadline.wait(0.02)
            finally:
                self.assertTrue(attached.stop())
            self.assertEqual(probes, ["studio"])
            self.assertFalse(attached.local._thread.is_alive())
            self.assertFalse(attached._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
