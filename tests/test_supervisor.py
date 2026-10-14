import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_seats import worktree as worktree_mod
from codex_seats.goalspec import GoalSpec
from codex_seats.supervisor import (AcceptanceResult, HeadResult, run_acceptance,
                                    run_review, supervise)


def test_runtime_review_modes_and_explicit_precedence(tmp_path, monkeypatch):
    tree = SimpleNamespace(path=tmp_path, base="base", diff=lambda: "")
    calls = []

    def review(command, _tree, **_kwargs):
        calls.append(command)
        return AcceptanceResult(False, "review finding")

    def execute(review_value, mode):
        calls.clear()
        spec = SimpleNamespace(
            goal="test", repo=tmp_path, acceptance=("true",), constraints=(),
            review=review_value, review_model="", protected=(), head="codex",
            max_iterations=0, pause_after_failures=2, link=None,
        )
        return supervise(
            spec, lambda *_: HeadResult(True),
            create_worktree=lambda _: tree,
            run_acceptance_fn=lambda *_: AcceptanceResult(True, "accepted"),
            run_review_fn=review,
            settings=(
                None if mode is None
                else {"review": {"mode": mode, "strategy": "single"}}
            ),
        )

    assert execute("", None).status == "done"
    assert calls == []
    assert execute("", "off").status == "done"
    assert calls == []
    advisory = execute("echo automatic", "advisory")
    # Explicit commands are blocking even when the runtime preference is advisory.
    assert advisory.status == "exhausted"
    assert calls == ["echo automatic"]
    assert execute("none", "blocking").status == "done"
    assert calls == []
    assert execute("echo custom", "off").status == "exhausted"
    assert calls == ["echo custom"]


def test_automatic_advisory_and_blocking_review(tmp_path, monkeypatch):
    from codex_seats import goalspec

    monkeypatch.setattr(goalspec, "REVIEW_GATE", tmp_path / "engine.py")
    (tmp_path / "engine.py").write_text("# engine stub\n")
    tree = SimpleNamespace(path=tmp_path, base="base", diff=lambda: "")
    calls = []
    spec = SimpleNamespace(
        goal="test", repo=tmp_path, acceptance=("true",), constraints=(),
        review="", review_model="", protected=(), head="codex",
        max_iterations=0, pause_after_failures=2, link=None,
    )

    def execute(mode):
        calls.clear()
        return supervise(
            spec, lambda *_: HeadResult(True), create_worktree=lambda _: tree,
            run_acceptance_fn=lambda *_: AcceptanceResult(True, "accepted"),
            run_review_fn=lambda command, *_args, **_kwargs: (
                calls.append(command) or AcceptanceResult(False, "finding")
            ),
            settings={"review": {"mode": mode, "strategy": "single"}},
            progress_path=tmp_path / "review-progress.json",
        )

    advisory = execute("advisory")
    assert advisory.status == "done"
    assert "advisory review failed 1" in advisory.reason
    assert json.loads((tmp_path / "review-progress.json").read_text())[
        "review_advisory_failed"
    ] == 1
    assert len(calls) == 1 and "--strategy single" in calls[0]
    assert execute("blocking").status == "exhausted"
    assert len(calls) == 1
    (tmp_path / "engine.py").unlink()
    assert execute("advisory").status == "done"
    assert len(calls) == 1


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


class _FakeHead:
    """Records prompts; runs an injected action(worktree_path, call_index) each call."""

    def __init__(self, action=None, tokens=0, provider=""):
        self.action = action or (lambda path, i: None)
        self.tokens = tokens
        self.provider = provider
        self.calls = 0
        self.prompts: list[str] = []

    def __call__(self, path: Path, prompt: str) -> HeadResult:
        self.calls += 1
        self.prompts.append(prompt)
        self.action(path, self.calls)
        return HeadResult(
            ok=True, summary=f"call {self.calls}", tokens=self.tokens, provider=self.provider
        )


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "T")
        (self.repo / "seed.txt").write_text("seed\n")
        _git(self.repo, "add", "seed.txt")
        _git(self.repo, "commit", "-q", "-m", "init")
        self._trees: list[worktree_mod.Worktree] = []

    def tearDown(self):
        for tree in self._trees:
            tree.remove()
        self._tmp.cleanup()

    def _spec(self, acceptance, **kw) -> GoalSpec:
        raw = {
            "goal": "make the acceptance command pass",
            "repo": str(self.repo),
            "acceptance": acceptance,
            # Hermetic default: empty review now resolves to the REAL in-repo
            # engine (§H wiring); fixtures that mean "no gate" must say so.
            "review": "none",
        }
        raw.update(kw)
        return GoalSpec.from_dict(raw)

    def _creator(self, seed_action=None):
        def make(branch: str) -> worktree_mod.Worktree:
            tree = worktree_mod.create(self.repo, branch)
            self._trees.append(tree)
            if seed_action:
                seed_action(tree.path)
            return tree
        return make

    def test_done_when_head_makes_acceptance_pass(self):
        def action(path, call):
            if call == 2:
                (path / "target.txt").write_text("ok\n")
        head = _FakeHead(action, tokens=100)
        outcome = supervise(
            self._spec(["test -f target.txt"], max_iterations=5),
            head,
            create_worktree=self._creator(),
            clock=lambda: 1000.0,
        )
        self.assertEqual(outcome.status, "done")
        self.assertEqual(outcome.branch, "run-1000")
        self.assertEqual(outcome.iterations, 2)
        self.assertEqual(head.calls, 2)
        self.assertIn("target.txt", outcome.diff)
        self.assertEqual(outcome.tokens, 200)

    def test_done_immediately_when_already_passing(self):
        head = _FakeHead()
        outcome = supervise(
            self._spec(["test -f pre.txt"]),
            head,
            create_worktree=self._creator(lambda path: (path / "pre.txt").write_text("x")),
            clock=lambda: 1000.0,
        )
        self.assertEqual(outcome.status, "done")
        self.assertEqual(outcome.iterations, 0)
        self.assertEqual(head.calls, 0)

    def test_paused_when_head_is_stuck_on_same_failure(self):
        head = _FakeHead()  # does nothing -> same failure every time
        outcome = supervise(
            self._spec(["test -f target.txt"], max_iterations=10, pause_after_failures=3),
            head,
            create_worktree=self._creator(),
            clock=lambda: 1000.0,
        )
        self.assertEqual(outcome.status, "paused")
        self.assertEqual(outcome.iterations, 3)
        self.assertIn("recurred 3x", outcome.reason)

    def test_exhausted_when_progress_but_never_passes(self):
        # evidence changes every iteration (breaker never trips), never passes.
        def action(path, call):
            (path / "n.txt").write_text(str(call))
        head = _FakeHead(action)
        outcome = supervise(
            self._spec(["sh -c 'cat n.txt 2>/dev/null; exit 1'"], max_iterations=3, pause_after_failures=3),
            head,
            create_worktree=self._creator(),
            clock=lambda: 1000.0,
        )
        self.assertEqual(outcome.status, "exhausted")
        self.assertEqual(outcome.iterations, 3)
        self.assertEqual(head.calls, 3)

    def test_progress_file_is_written_each_step(self):
        progress = Path(self._tmp.name) / "progress.json"
        def action(path, call):
            if call == 1:
                (path / "target.txt").write_text("ok")
        supervise(
            self._spec(["test -f target.txt"]),
            _FakeHead(action),
            create_worktree=self._creator(),
            progress_path=progress,
            clock=lambda: 4242.0,
            reader_heartbeat=lambda: {
                "reader_kind": "supervisor",
                "supported_settings": {"min": 1, "max": 3},
            },
        )
        payload = json.loads(progress.read_text())
        self.assertEqual(payload["status"], "done")
        self.assertEqual(payload["goal"], "make the acceptance command pass")
        self.assertEqual(payload["heartbeat_at"], 4242.0)
        self.assertEqual(payload["settings_reader"]["reader_kind"], "supervisor")

    def test_tampering_with_protected_test_cannot_win(self):
        # gate.txt at base holds the strict check; head "weakens" it instead of
        # fixing -> restore keeps the judge pristine, acceptance still fails.
        (self.repo / "gate.txt").write_text("strict\n")
        _git(self.repo, "add", "gate.txt")
        _git(self.repo, "commit", "-q", "-m", "gate")

        def action(path, call):
            (path / "gate.txt").write_text("weak\n")  # tamper every call, never fix
        outcome = supervise(
            self._spec(
                ["grep -q strict gate.txt && test -f fix.txt"],
                protected=["gate.txt"], max_iterations=4, pause_after_failures=2,
            ),
            _FakeHead(action),
            create_worktree=self._creator(),
            clock=lambda: 1000.0,
        )
        self.assertNotEqual(outcome.status, "done")
        self.assertEqual(outcome.tampered_paths, ("gate.txt",))

    def test_real_fix_with_tampering_finishes_but_is_flagged(self):
        (self.repo / "gate.txt").write_text("strict\n")
        _git(self.repo, "add", "gate.txt")
        _git(self.repo, "commit", "-q", "-m", "gate")

        def action(path, call):
            (path / "gate.txt").write_text("weak\n")   # tampers...
            (path / "fix.txt").write_text("done\n")     # ...but also genuinely fixes
        outcome = supervise(
            self._spec(
                ["grep -q strict gate.txt && test -f fix.txt"], protected=["gate.txt"]
            ),
            _FakeHead(action),
            create_worktree=self._creator(),
            clock=lambda: 1000.0,
        )
        self.assertEqual(outcome.status, "done")
        self.assertEqual(outcome.tampered_paths, ("gate.txt",))
        # judge stayed pristine in the worktree
        # (restore happened before the passing acceptance run)

    def test_head_is_told_its_tampering_was_reverted(self):
        (self.repo / "gate.txt").write_text("strict\n")
        _git(self.repo, "add", "gate.txt")
        _git(self.repo, "commit", "-q", "-m", "gate")

        def action(path, call):
            (path / "gate.txt").write_text("weak\n")
        head = _FakeHead(action)
        supervise(
            self._spec(
                ["grep -q strict gate.txt && test -f fix.txt"],
                protected=["gate.txt"], max_iterations=3, pause_after_failures=3,
            ),
            head,
            create_worktree=self._creator(),
            clock=lambda: 1000.0,
        )
        self.assertIn("were reverted", head.prompts[1])

    def test_head_prompt_carries_goal_and_current_failure(self):
        head = _FakeHead()
        supervise(
            self._spec(["test -f target.txt"], constraints=["do not delete seed.txt"], pause_after_failures=1),
            head,
            create_worktree=self._creator(),
            clock=lambda: 1000.0,
        )
        prompt = head.prompts[0]
        self.assertIn("make the acceptance command pass", prompt)
        self.assertIn("test -f target.txt", prompt)
        self.assertIn("do not delete seed.txt", prompt)
        self.assertIn("Current failure", prompt)

    # --- terminal review gate ---------------------------------------------

    def test_review_gate_must_also_pass_before_done(self):
        # acceptance passes at call 1 (target.txt); review only passes at call 2
        # (approved.txt) -> the run is NOT done until the reviewer is satisfied.
        def action(path, call):
            (path / "target.txt").write_text("ok")
            if call == 2:
                (path / "approved.txt").write_text("ok")
        head = _FakeHead(action)
        outcome = supervise(
            self._spec(["test -f target.txt"], review="test -f approved.txt", max_iterations=5),
            head,
            create_worktree=self._creator(),
            clock=lambda: 1000.0,
        )
        self.assertEqual(outcome.status, "done")
        self.assertEqual(outcome.iterations, 2)
        # the head was handed the review block as the failure to fix next.
        self.assertIn("review blocked", head.prompts[1])

    def test_review_receives_the_diff_via_env(self):
        # review greps the diff file for a path the head only creates in the
        # worktree -> proves $CODEX_SEATS_DIFF carries the real worktree diff.
        def action(path, call):
            (path / "target.txt").write_text("ok")
        outcome = supervise(
            self._spec(["true"], review='grep -q target.txt "$CODEX_SEATS_DIFF"'),
            _FakeHead(action),
            create_worktree=self._creator(),
            clock=lambda: 1000.0,
        )
        self.assertEqual(outcome.status, "done")
        # acceptance was already green; the head ran once to satisfy review.
        self.assertEqual(outcome.iterations, 1)

    def test_review_that_never_passes_pauses(self):
        head = _FakeHead()  # never changes anything
        outcome = supervise(
            self._spec(["true"], review="false", max_iterations=10, pause_after_failures=2),
            head,
            create_worktree=self._creator(),
            clock=lambda: 1000.0,
        )
        self.assertEqual(outcome.status, "paused")
        self.assertEqual(outcome.iterations, 2)
        self.assertIn("review blocked", head.prompts[0])

    def test_review_blocking_with_varying_text_still_pauses(self):
        # The deadlock/token-waste case: acceptance stays green, but the review
        # blocks with DIFFERENT text every call (LLM review prose varies run-to-run),
        # so the evidence-equality breaker never trips. The review-block breaker must
        # still pause after N instead of burning all max_iterations.
        head = _FakeHead()  # never changes anything
        varying_review = (
            "sh -c 'c=$(cat rc 2>/dev/null || echo 0); c=$((c+1)); "
            "echo $c > rc; echo blocked-run-$c >&2; exit 1'"
        )
        outcome = supervise(
            self._spec(["true"], review=varying_review, max_iterations=10, pause_after_failures=3),
            head,
            create_worktree=self._creator(),
            clock=lambda: 1000.0,
        )
        self.assertEqual(outcome.status, "paused")
        self.assertEqual(outcome.iterations, 3)  # not 10 (max_iterations) — breaker caught it
        self.assertIn("review blocked 3x in a row", outcome.reason)

    def test_no_review_field_keeps_acceptance_only_behavior(self):
        def action(path, call):
            (path / "target.txt").write_text("ok")
        outcome = supervise(
            self._spec(["test -f target.txt"]),  # review="none": acceptance-only
            _FakeHead(action),
            create_worktree=self._creator(),
            clock=lambda: 1000.0,
        )
        self.assertEqual(outcome.status, "done")
        self.assertEqual(outcome.iterations, 1)

    def test_empty_review_uses_enabled_runtime_engine(self):
        """An empty review uses the engine only when runtime policy enables it."""
        seen = {}

        def fake_review(cmd, tree, *, extra_env=None):
            seen["cmd"] = cmd
            seen["env"] = extra_env or {}
            return AcceptanceResult(True, "review approved")

        def action(path, call):
            (path / "target.txt").write_text("ok")
        outcome = supervise(
            self._spec(["test -f target.txt"], review=""),
            _FakeHead(action),
            create_worktree=self._creator(),
            clock=lambda: 1000.0,
            run_review_fn=fake_review,
            settings={"review": {"mode": "blocking", "strategy": "single"}},
        )
        self.assertEqual(outcome.status, "done")
        self.assertIn("codex_seats/review.py", seen["cmd"])
        self.assertIn("--strategy single", seen["cmd"])
        # The implementer env var is always shadowed (empty when unknown), so a
        # stale parent value can never pick the wrong reviewer family.
        self.assertIn("CODEX_SEATS_HEAD_PROVIDER", seen["env"])


    # --- review telemetry: trace env + persisted counters ---

    def test_review_env_carries_goal_id_iteration_and_round(self):
        # Acceptance is green from the start; the review command itself asserts
        # the trace env. Round 1 happens pre-loop (iteration 0), round 2 after
        # the head runs once (iteration 1).
        probe = (
            "sh -c '[ \"$CODEX_SEATS_GOAL_ID\" = g-42 ] || exit 9; "
            "[ -n \"$CODEX_SEATS_ITERATION\" ] || exit 8; "
            "[ \"$CODEX_SEATS_REVIEW_ROUND\" = 2 ]'"
        )
        def action(path, call):
            (path / "target.txt").write_text("ok")
        outcome = supervise(
            self._spec(["true"], review=probe),
            _FakeHead(action),
            goal_id="g-42",
            create_worktree=self._creator(),
            clock=lambda: 1000.0,
        )
        # Round 1 (pre-loop) fails the ROUND=2 check -> head runs -> round 2 passes.
        self.assertEqual(outcome.status, "done")
        self.assertEqual(outcome.iterations, 1)

    def test_review_env_carries_the_actual_head_provider(self):
        # The review gate opposes the implementer's family, so it must see the
        # provider that ACTUALLY ran — not "auto". Iteration 0 (pre-head) has no
        # implementer, so the env is absent; after the head runs it names the family.
        probe = (
            "sh -c 'if [ \"$CODEX_SEATS_REVIEW_ROUND\" = 1 ]; then "
            "[ -z \"$CODEX_SEATS_HEAD_PROVIDER\" ] || exit 7; exit 9; "
            "else [ \"$CODEX_SEATS_HEAD_PROVIDER\" = codex ]; fi'"
        )
        def action(path, call):
            (path / "target.txt").write_text("ok")
        outcome = supervise(
            self._spec(["true"], review=probe),
            _FakeHead(action, provider="codex"),
            create_worktree=self._creator(),
            clock=lambda: 1000.0,
        )
        # Round 1 blocks (exit 9) with the env empty; the head runs and reports
        # provider=codex; round 2 sees it and approves.
        self.assertEqual(outcome.status, "done")
        self.assertEqual(outcome.iterations, 1)

    def test_progress_persists_review_counters(self):
        progress = Path(self._tmp.name) / "progress.json"
        outcome = supervise(
            self._spec(["true"], review="false", max_iterations=10, pause_after_failures=2),
            _FakeHead(),
            progress_path=progress,
            create_worktree=self._creator(),
            clock=lambda: 1000.0,
        )
        self.assertEqual(outcome.status, "paused")
        payload = json.loads(progress.read_text())
        # acceptance ("true") is green from the start, so review runs at iter-0
        # and each iteration; "false" blocks every time. 3 gate blocks total
        # (iter-0 + 2 in-loop). The breaker still trips on the 2 in-loop blocks
        # (iter-0 is pre-fix, doesn't seed it), so it pauses at iteration 2 — but
        # the telemetry now counts all 3 blocks instead of silently dropping
        # iter-0.
        self.assertEqual(payload["review_rounds"], 3)
        self.assertEqual(payload["review_blocked"], 3)

    def test_goal_id_defaults_to_branch(self):
        probe = "sh -c '[ \"$CODEX_SEATS_GOAL_ID\" = \"$(git rev-parse --abbrev-ref HEAD)\" ]'"
        def action(path, call):
            (path / "target.txt").write_text("ok")
        outcome = supervise(
            self._spec(["true"], review=probe),
            _FakeHead(action),
            branch="my-branch",
            create_worktree=self._creator(),
            clock=lambda: 1000.0,
        )
        self.assertEqual(outcome.status, "done")


_SYNTHETIC_SECRETS = {
    "OPENAI_API_KEY": "sk-synthetic-openai",
    "ANTHROPIC_API_KEY": "sk-ant-synthetic",
    "GITHUB_TOKEN": "ghp_synthetic",
    "AWS_SECRET_ACCESS_KEY": "synthetic-aws",
    "SSH_AUTH_SOCK": "/tmp/synthetic-agent.sock",
    "GPG_AGENT_INFO": "/tmp/synthetic-gpg",
    "CODEX_SEATS_CONTROLLER_TOKEN": "synthetic-controller",
    "CODEX_SEATS_CONTROLLER_ID": "synthetic-controller-id",
    "PYTHONPATH": "/tmp/synthetic-injection",
    "DYLD_INSERT_LIBRARIES": "/tmp/synthetic.dylib",
    "UNRELATED_PARENT_STATE": "should-not-leak",
}

_CONTROLLER_MARKERS = {
    "CODEX_SEATS_ROOT": "/tmp/synthetic-pool",
    "CODEX_SEATS_RELEASE_ID": "a" * 64,
    "CODEX_SEATS_HEAD": "codex",
    "CODEX_CONTROL_SEAT": "seat2",
    "CODEX_THREAD_ID": "thread-synthetic",
    "CODEX_TURN_ID": "turn-synthetic",
    "CODEX_SESSION_ID": "session-synthetic",
    "CODEX_HOME": "/tmp/synthetic-pool/seat2",
}


def _child_env(path: Path) -> dict[str, str]:
    rows = path.read_bytes().split(b"\0")
    return dict(row.decode().split("=", 1) for row in rows if row)


def _capture_command(path: Path) -> str:
    return f"/usr/bin/env -0 > {path}"


def _with_parent_env(monkeypatch, values):
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("CODEX_SEATS_MANAGED_WORKER", raising=False)
    monkeypatch.delenv("CLAUDE_LEAD_WORKER", raising=False)


def test_acceptance_child_env_is_minimal_and_managed(tmp_path, monkeypatch):
    _with_parent_env(monkeypatch, {**_SYNTHETIC_SECRETS, **_CONTROLLER_MARKERS})
    capture = tmp_path / "env.bin"
    result = run_acceptance([_capture_command(capture)], tmp_path)
    assert result.passed, result.evidence
    env = _child_env(capture)
    assert env["CODEX_SEATS_MANAGED_WORKER"] == "1"
    assert env["PATH"] and env["HOME"]
    for key in [*_SYNTHETIC_SECRETS, *_CONTROLLER_MARKERS]:
        assert key not in env, key
    assert not any(
        value in env.values()
        for value in [*_SYNTHETIC_SECRETS.values(), *_CONTROLLER_MARKERS.values()]
    )


def test_review_child_env_keeps_only_gate_context(tmp_path, monkeypatch):
    _with_parent_env(monkeypatch, {**_SYNTHETIC_SECRETS, **_CONTROLLER_MARKERS})
    capture = tmp_path / "env.bin"
    tree = SimpleNamespace(path=tmp_path, base="base-sha", diff=lambda: "diff --git a b\n")
    result = run_review(
        _capture_command(capture), tree,
        extra_env={"CODEX_SEATS_GOAL_ID": "goal-1", "CODEX_SEATS_HEAD_PROVIDER": "claude"},
    )
    assert result.passed, result.evidence
    env = _child_env(capture)
    for key, value in _CONTROLLER_MARKERS.items():
        assert env[key] == value, key
    for key in _SYNTHETIC_SECRETS:
        assert key not in env, key
    # The built-in gate claims capacity as the controller; never mark it managed.
    assert "CODEX_SEATS_MANAGED_WORKER" not in env
    assert env["CODEX_SEATS_WORKTREE"] == str(tmp_path)
    assert env["CODEX_SEATS_BASE"] == "base-sha"
    assert env["CODEX_SEATS_DIFF"].endswith(".patch")
    assert env["CODEX_SEATS_GOAL_ID"] == "goal-1"
    assert env["CODEX_SEATS_HEAD_PROVIDER"] == "claude"


def test_review_env_preserves_inherited_containment(tmp_path, monkeypatch):
    _with_parent_env(monkeypatch, _CONTROLLER_MARKERS)
    monkeypatch.setenv("CLAUDE_LEAD_WORKER", "1")
    capture = tmp_path / "env.bin"
    tree = SimpleNamespace(path=tmp_path, base="base", diff=lambda: "")
    assert run_review(_capture_command(capture), tree).passed
    assert _child_env(capture)["CLAUDE_LEAD_WORKER"] == "1"


def test_review_rejects_secret_bearing_extra_env(tmp_path):
    tree = SimpleNamespace(path=tmp_path, base="base", diff=lambda: "")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        run_review("true", tree, extra_env={"OPENAI_API_KEY": "sk-synthetic"})


class RunReviewTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "T")
        (self.repo / "seed.txt").write_text("seed\n")
        _git(self.repo, "add", "seed.txt")
        _git(self.repo, "commit", "-q", "-m", "init")
        self.tree = worktree_mod.create(self.repo, "review-branch")

    def tearDown(self):
        self.tree.remove()
        self._tmp.cleanup()

    def test_passes_and_exposes_a_nonempty_diff(self):
        (self.tree.path / "new.txt").write_text("hello\n")  # makes the diff non-empty
        result = run_review('test -s "$CODEX_SEATS_DIFF"', self.tree)
        self.assertTrue(result.passed)

    def test_blocks_on_nonzero_with_evidence(self):
        result = run_review('sh -c "echo NOPE >&2; exit 3"', self.tree)
        self.assertFalse(result.passed)
        self.assertIn("review blocked, exit 3", result.evidence)
        self.assertIn("NOPE", result.evidence)


class RunAcceptanceTests(unittest.TestCase):
    def test_all_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_acceptance(["true", "test 1 = 1"], tmp)
            self.assertTrue(result.passed)

    def test_first_failure_short_circuits_with_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_acceptance(["sh -c 'echo boom >&2; exit 2'", "true"], tmp)
            self.assertFalse(result.passed)
            self.assertIn("exit 2", result.evidence)
            self.assertIn("boom", result.evidence)


if __name__ == "__main__":
    unittest.main()
