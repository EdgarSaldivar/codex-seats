import argparse
import io
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from codex_seats import cli
from codex_seats.state import default_settings


def _dash_args(**overrides):
    values = {
        "no_color": True,
        "watch": False,
        "interval": 5,
        "probe_interval": 300,
        "no_active_probe": False,
        "forensic": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class SignalTableCliTests(unittest.TestCase):
    def test_no_color_environment_disables_dashboard_ansi_on_a_tty(self):
        class TtyBuffer(io.StringIO):
            def isatty(self):
                return True

        output = TtyBuffer()
        with (
            mock.patch("codex_seats.cli.codex_version", return_value=(None, None)),
            mock.patch("codex_seats.cli.dashboard", return_value="PLAIN") as dashboard,
            mock.patch.dict(os.environ, {"NO_COLOR": "1"}),
            mock.patch("sys.stdout", output),
        ):
            self.assertEqual(
                cli.cmd_dash(_dash_args(no_color=False), default_settings(), []), 0
            )

        self.assertFalse(dashboard.call_args.kwargs["color"])

    def test_one_shot_dashboard_receives_current_terminal_width(self):
        output = io.StringIO()
        with (
            mock.patch("codex_seats.cli.codex_version", return_value=(None, None)),
            mock.patch("codex_seats.cli.dashboard", return_value="SIGNALS") as dashboard,
            mock.patch(
                "codex_seats.cli.shutil.get_terminal_size",
                return_value=os.terminal_size((87, 24)),
            ),
            mock.patch("sys.stdout", output),
        ):
            self.assertEqual(cli.cmd_dash(_dash_args(), default_settings(), []), 0)

        self.assertEqual(dashboard.call_args.kwargs["width"], 87)
        self.assertNotIn("height", dashboard.call_args.kwargs)
        self.assertIn("SIGNALS", output.getvalue())

    def test_watch_resamples_width_and_reuses_view_state_each_redraw(self):
        events = []
        renders = []
        snapshots = [object(), object()]
        view_state = object()

        class FakeRefresher:
            def __init__(self, *_args):
                pass

            def snapshot(self):
                events.append("snapshot")
                return snapshots[len(renders)]

            def start(self):
                events.append("start")

            def stop(self):
                events.append("stop")

        def render(*_args, **kwargs):
            events.append("render")
            renders.append(kwargs)
            return "SIGNALS"

        with (
            mock.patch("codex_seats.cli.codex_version", return_value=(None, None)),
            mock.patch("codex_seats.cli.dashboard", side_effect=render),
            mock.patch(
                "codex_seats.display.DashboardViewState", return_value=view_state, create=True
            ),
            mock.patch("codex_seats.watch.WatchRefresher", FakeRefresher),
            mock.patch(
                "codex_seats.cli.shutil.get_terminal_size",
                side_effect=[os.terminal_size((80, 24)), os.terminal_size((120, 24))],
            ),
            mock.patch("codex_seats.cli.time.sleep", side_effect=[None, KeyboardInterrupt]),
            mock.patch("sys.stdout", io.StringIO()),
        ):
            self.assertEqual(
                cli.cmd_dash(_dash_args(watch=True), default_settings(), []),
                0,
            )

        self.assertEqual([render["width"] for render in renders], [80, 120])
        self.assertEqual([render["height"] for render in renders], [24, 24])
        self.assertEqual(
            [render["refresh_snapshot"] for render in renders],
            snapshots,
        )
        self.assertTrue(all(render["view_state"] is view_state for render in renders))
        self.assertEqual(events[:3], ["snapshot", "render", "start"])
        self.assertEqual(events[-1], "stop")

    def test_disabled_active_probe_passes_no_refresh_snapshot(self):
        renders = []
        view_state = object()

        def render(*_args, **kwargs):
            renders.append(kwargs)
            return "SIGNALS"

        with (
            mock.patch("codex_seats.cli.codex_version", return_value=(None, None)),
            mock.patch("codex_seats.cli.dashboard", side_effect=render),
            mock.patch(
                "codex_seats.display.DashboardViewState", return_value=view_state, create=True
            ),
            mock.patch(
                "codex_seats.cli.shutil.get_terminal_size",
                return_value=os.terminal_size((99, 24)),
            ),
            mock.patch("codex_seats.cli.time.sleep", side_effect=KeyboardInterrupt),
            mock.patch("sys.stdout", io.StringIO()),
        ):
            self.assertEqual(
                cli.cmd_dash(
                    _dash_args(watch=True, no_active_probe=True),
                    default_settings(),
                    [],
                ),
                0,
            )

        self.assertEqual(len(renders), 1)
        self.assertIsNone(renders[0]["refresh_snapshot"])
        self.assertIs(renders[0]["view_state"], view_state)

    def test_real_tty_uses_atomic_alt_screen_frames_without_2j_or_newline(self):
        class TtyBuffer(io.StringIO):
            def __init__(self):
                super().__init__()
                self.writes = []

            def isatty(self):
                return True

            def write(self, value):
                self.writes.append(value)
                return super().write(value)

        output = TtyBuffer()
        with (
            mock.patch("codex_seats.cli.codex_version", return_value=(None, None)),
            mock.patch("codex_seats.cli.dashboard", return_value="FRAME"),
            mock.patch(
                "codex_seats.cli.current_controller",
                return_value=SimpleNamespace(is_controller=False),
            ),
            mock.patch(
                "codex_seats.cli.shutil.get_terminal_size",
                return_value=os.terminal_size((80, 24)),
            ),
            mock.patch("codex_seats.cli.time.sleep", side_effect=KeyboardInterrupt),
            mock.patch("sys.stdout", output),
        ):
            self.assertEqual(
                cli.cmd_dash(
                    _dash_args(watch=True, no_active_probe=True),
                    default_settings(),
                    [],
                ),
                0,
            )

        self.assertEqual(output.writes[0], "\033[?1049h")
        self.assertIn("\033[HFRAME\033[J", output.writes)
        self.assertEqual(output.writes[-1], "\033[?1049l")
        self.assertNotIn("\033[2J", output.getvalue())
        frame_write = next(value for value in output.writes if value.startswith("\033[H"))
        self.assertFalse(frame_write.endswith("\n"))

    def test_captured_watch_suppresses_volatile_repeats_but_emits_fit_change(self):
        output = io.StringIO()
        frames = [
            "CXWATCH  10:00:00 local · LIVE · last ok 1s · next 2m\nP idle · FIT S · 3s",
            "CXWATCH  10:00:05 local · LIVE · last ok 6s · next 1m\nP idle · FIT S · 8s",
            "CXWATCH  10:00:10 local · LIVE · last ok 11s · next 1m\nP idle · FIT none · 13s",
        ]
        with (
            mock.patch("codex_seats.cli.codex_version", return_value=(None, None)),
            mock.patch("codex_seats.cli.dashboard", side_effect=frames),
            mock.patch(
                "codex_seats.cli.shutil.get_terminal_size",
                return_value=os.terminal_size((80, 24)),
            ),
            mock.patch(
                "codex_seats.cli.time.sleep",
                side_effect=[None, None, KeyboardInterrupt],
            ),
            mock.patch("sys.stdout", output),
        ):
            self.assertEqual(
                cli.cmd_dash(
                    _dash_args(watch=True, no_active_probe=True),
                    default_settings(),
                    [],
                ),
                0,
            )

        rendered = output.getvalue()
        self.assertEqual(rendered.count("CXWATCH"), 2)
        self.assertIn("FIT S", rendered)
        self.assertIn("FIT none", rendered)
        self.assertNotIn("\033", rendered)
        self.assertTrue(rendered.endswith("\n"))

    def test_captured_signature_retains_refresh_result_changes(self):
        first = cli._captured_watch_signature(
            "same stable frame", {"last_result": "Codex 5/5; Claude ok"}
        )
        changed = cli._captured_watch_signature(
            "same stable frame", {"last_result": "Codex 4/5; Claude failed"}
        )
        self.assertNotEqual(first, changed)

    def test_controller_tty_uses_captured_plain_mode(self):
        class ControllerTty(io.StringIO):
            def isatty(self):
                return True

        output = ControllerTty()
        renders = []

        def render(*_args, **kwargs):
            renders.append(kwargs)
            return "PLAIN"

        with (
            mock.patch("codex_seats.cli.codex_version", return_value=(None, None)),
            mock.patch("codex_seats.cli.dashboard", side_effect=render),
            mock.patch(
                "codex_seats.cli.current_controller",
                return_value=SimpleNamespace(is_controller=True),
            ),
            mock.patch(
                "codex_seats.cli.shutil.get_terminal_size",
                return_value=os.terminal_size((80, 24)),
            ),
            mock.patch("codex_seats.cli.time.sleep", side_effect=KeyboardInterrupt),
            mock.patch("sys.stdout", output),
        ):
            self.assertEqual(
                cli.cmd_dash(
                    _dash_args(watch=True, no_active_probe=True),
                    default_settings(),
                    [],
                ),
                0,
            )

        self.assertFalse(renders[0]["color"])
        self.assertEqual(output.getvalue(), "PLAIN\n")
        self.assertNotIn("\033", output.getvalue())

    def test_captured_watch_never_attaches_local_host_probes(self):
        constructions = []

        class FakeRefresher:
            def __init__(self, *args, **kwargs):
                constructions.append((args, kwargs))

            def snapshot(self):
                return None

            def start(self):
                pass

            def stop(self):
                pass

        with (
            mock.patch("codex_seats.cli.codex_version", return_value=(None, None)),
            mock.patch("codex_seats.cli.dashboard", return_value="SIGNALS"),
            mock.patch("codex_seats.watch.WatchRefresher", FakeRefresher),
            mock.patch(
                "codex_seats.cli.shutil.get_terminal_size",
                return_value=os.terminal_size((80, 24)),
            ),
            mock.patch("codex_seats.cli.time.sleep", side_effect=KeyboardInterrupt),
            mock.patch("sys.stdout", io.StringIO()),
        ):
            self.assertEqual(
                cli.cmd_dash(_dash_args(watch=True), default_settings(), []), 0
            )

        self.assertEqual(len(constructions), 1)
        self.assertFalse(constructions[0][1].get("local_probes", False))

    def test_interactive_dash_attaches_local_probes_and_honors_no_active_probe(self):
        for no_active_probe in (False, True):
            with self.subTest(no_active_probe=no_active_probe):
                constructions = []
                stops = []

                class FakeRefresher:
                    def __init__(self, *args, **kwargs):
                        constructions.append((args, kwargs))

                    def snapshot(self):
                        return None

                    def start(self):
                        pass

                    def stop(self, *, timeout=5.0):
                        stops.append(timeout)
                        return True

                manager = mock.MagicMock()
                ui = mock.MagicMock()
                ui.shutdown.return_value = True
                with (
                    mock.patch("codex_seats.seat_manager.ensure_interactive_terminal"),
                    mock.patch("codex_seats.seat_manager.SeatManager", return_value=manager),
                    mock.patch("codex_seats.seat_manager_tui.ManagerUI", return_value=ui),
                    mock.patch(
                        "codex_seats.terminal.run_ui",
                        return_value=SimpleNamespace(reason="quit", login_seat_id=None),
                    ),
                    mock.patch("codex_seats.watch.WatchRefresher", FakeRefresher),
                ):
                    code = cli._run_interactive_dash(
                        _dash_args(watch=True, no_active_probe=no_active_probe),
                        default_settings(),
                        [],
                        version_warning=None,
                        color=False,
                        direct=False,
                    )

                self.assertEqual(code, 0)
                self.assertEqual(len(constructions), 1)
                kwargs = constructions[0][1]
                self.assertTrue(kwargs["local_probes"])
                self.assertEqual(kwargs["periodic"], not no_active_probe)
                self.assertEqual(stops, [10.0])


if __name__ == "__main__":
    unittest.main()
