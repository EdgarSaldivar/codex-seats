from __future__ import annotations

import os
import stat
import json
import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from codex_seats.worker_environment import minimal_worker_env
from codex_seats.claude_isolation import write_isolation_files
from codex_seats.scope import worker_permission_profile
from codex_seats.worker_launch import (
    WORKER_LAUNCH_DIRNAME,
    create_worker_launch_directory,
)


class WorkerEnvironmentTests(unittest.TestCase):
    def test_preserves_developer_context_and_strips_authority(self):
        synthetic = {
            "HOME": "/synthetic/home",
            "PATH": "/synthetic/bin:/usr/bin",
            "LANG": "en_US.UTF-8",
            "LC_MESSAGES": "en_US.UTF-8",
            "DEVELOPER_DIR": "/Synthetic/Xcode.app/Contents/Developer",
            "SDKROOT": "/Synthetic/SDK",
            "MACOSX_DEPLOYMENT_TARGET": "15.0",
            "OPENAI_API_KEY": "synthetic-not-a-credential",
            "ARBITRARY_SECRET": "synthetic-not-a-credential",
            "SSH_AUTH_SOCK": "/synthetic/agent.sock",
            "GPG_AGENT_INFO": "/synthetic/gpg.sock",
            "CODEX_THREAD_ID": "synthetic-controller-thread",
            "CODEX_SESSION_ID": "synthetic-controller-session",
            "CODEX_SEATS_CONTROLLER_SOCKET": "/synthetic/controller.sock",
            "GIT_ASKPASS": "/synthetic/askpass",
            "DYLD_INSERT_LIBRARIES": "/synthetic/inject.dylib",
            "PYTHONPATH": "/synthetic/python",
            "NODE_OPTIONS": "--require=/synthetic/inject.js",
            "CC": "/synthetic/compiler-wrapper",
            "UNRELATED": "drop-me",
        }

        env = minimal_worker_env(synthetic, CODEX_HOME="/synthetic/provider-home")

        self.assertEqual(
            env,
            {
                "HOME": "/synthetic/home",
                "PATH": "/synthetic/bin:/usr/bin",
                "LANG": "en_US.UTF-8",
                "LC_MESSAGES": "en_US.UTF-8",
                "DEVELOPER_DIR": "/Synthetic/Xcode.app/Contents/Developer",
                "SDKROOT": "/Synthetic/SDK",
                "MACOSX_DEPLOYMENT_TARGET": "15.0",
                "CODEX_HOME": "/synthetic/provider-home",
            },
        )

    def test_explicit_unsafe_environment_injection_fails_closed(self):
        for key in (
            "SYNTHETIC_AUTH_TOKEN",
            "SSH_AUTH_SOCK",
            "GIT_ASKPASS",
            "DYLD_INSERT_LIBRARIES",
            "CODEX_SEATS_CONTROLLER_SOCKET",
            "CODEX_SESSION_ID",
        ):
            with self.subTest(key=key), self.assertRaisesRegex(
                ValueError, "unsafe worker environment keys"
            ):
                minimal_worker_env({}, **{key: "synthetic-value"})

    def test_launch_directory_is_owner_only_and_separate_from_scratch(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory).resolve()
            scratch = root / "private-tmp" / "job"
            scratch.mkdir(parents=True)

            launch = create_worker_launch_directory(root, "job/with unsafe prefix")

            self.assertEqual(launch.parent, root / WORKER_LAUNCH_DIRNAME)
            self.assertFalse(launch.is_relative_to(scratch.parent))
            self.assertEqual(stat.S_IMODE(launch.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(launch.parent.stat().st_mode), 0o700)
            self.assertEqual(launch.stat().st_uid, os.geteuid())

    def test_launch_root_rejects_symlink_replacement(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            replacement = root / "replacement"
            replacement.mkdir()
            (root / WORKER_LAUNCH_DIRNAME).symlink_to(replacement)

            with self.assertRaisesRegex(RuntimeError, "non-symlink directory"):
                create_worker_launch_directory(root, "job")

    def test_launch_files_are_separate_and_native_profiles_deny_parent(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory).resolve()
            repo = root / "repo"
            private = root / "private-tmp" / "job"
            repo.mkdir()
            private.mkdir(parents=True)
            hook = root / "worker-hook"
            hook.write_text("")
            launch = create_worker_launch_directory(root, "job")

            settings_path, mcp_path, schema_path = write_isolation_files(
                launch,
                repo,
                [{"path": ".", "type": "tree"}],
                [],
                hook,
                private_temp=private,
                control_root=root,
            )

            self.assertEqual(
                {path.parent for path in (settings_path, mcp_path, schema_path)},
                {launch},
            )
            self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in (
                settings_path,
                mcp_path,
                schema_path,
            )))
            self.assertEqual(json.loads(schema_path.read_text())["type"], "object")
            claude_filesystem = json.loads(settings_path.read_text())["sandbox"][
                "filesystem"
            ]
            self.assertIn(str(root / WORKER_LAUNCH_DIRNAME), claude_filesystem["denyWrite"])
            self.assertIn(str(root / WORKER_LAUNCH_DIRNAME), claude_filesystem["denyRead"])
            self.assertIn(str(private), claude_filesystem["allowWrite"])

            codex_filesystem = tomllib.loads(
                worker_permission_profile(
                    "job",
                    repo,
                    [{"path": ".", "type": "tree"}],
                    [],
                    private,
                    control_root=root,
                )
            )["permissions"]["job"]["filesystem"]
            self.assertEqual(
                codex_filesystem[str(root / WORKER_LAUNCH_DIRNAME)], "deny"
            )
            self.assertEqual(codex_filesystem[str(private)], "write")


if __name__ == "__main__":
    unittest.main()
