import json
import tempfile
import unittest
from pathlib import Path

import pytest

from codex_seats.goalspec import GoalSpec, SpecError


def _valid() -> dict:
    return {
        "goal": "Extract the router from provider_claude",
        "repo": "/Users/example/.codex-seats",
        "acceptance": ["PYTHONPATH=lib .venv/bin/python -m pytest tests -q"],
        # keep contract tests hermetic: never auto-discover the real repo preset
        "preset": False,
    }


class GoalSpecParseTests(unittest.TestCase):
    def test_minimal_valid_spec_applies_defaults(self):
        spec = GoalSpec.from_dict(_valid())
        self.assertEqual(spec.goal, "Extract the router from provider_claude")
        self.assertEqual(spec.repo, Path("/Users/example/.codex-seats"))
        self.assertEqual(len(spec.acceptance), 1)
        self.assertEqual(spec.head, "auto")
        self.assertEqual(spec.constraints, ())
        self.assertEqual(spec.max_iterations, 20)
        self.assertEqual(spec.pause_after_failures, 3)

    def test_acceptance_accepts_a_bare_string(self):
        raw = _valid()
        raw["acceptance"] = "make test"
        spec = GoalSpec.from_dict(raw)
        self.assertEqual(spec.acceptance, ("make test",))

    def test_full_spec_round_trips_through_as_dict(self):
        raw = {
            **_valid(),
            "head": "codex",
            "constraints": ["do not touch state.py", "keep tests green"],
            "max_iterations": 8,
            "pause_after_failures": 2,
        }
        spec = GoalSpec.from_dict(raw)
        again = GoalSpec.from_dict(spec.as_dict())
        self.assertEqual(spec, again)
        self.assertEqual(spec.head, "codex")
        self.assertEqual(spec.constraints, ("do not touch state.py", "keep tests green"))

    def test_review_head_model_and_effort_parse_and_round_trip(self):
        raw = {
            **_valid(),
            "review": '  python tools/codex_review.py --diff-file "$CODEX_SEATS_DIFF"  ',
            "head_model": " claude-opus-4-8 ",
            "effort": "high",
        }
        spec = GoalSpec.from_dict(raw)
        self.assertEqual(spec.review, 'python tools/codex_review.py --diff-file "$CODEX_SEATS_DIFF"')
        self.assertEqual(spec.head_model, "claude-opus-4-8")
        self.assertEqual(spec.effort, "high")
        self.assertEqual(GoalSpec.from_dict(spec.as_dict()), spec)

    def test_review_defaults_empty_and_effort_is_validated(self):
        self.assertEqual(GoalSpec.from_dict(_valid()).review, "")
        self.assertEqual(GoalSpec.from_dict(_valid()).effort, "")
        with self.assertRaises(SpecError):
            GoalSpec.from_dict({**_valid(), "effort": "turbo"})

    def test_mcp_allowlist_parses_and_round_trips(self):
        # _valid()'s repo ships no worker-mcp catalog, so names are unvalidated here.
        self.assertEqual(GoalSpec.from_dict(_valid()).mcp, ())
        spec = GoalSpec.from_dict({**_valid(), "mcp": ["visual", "context7"]})
        self.assertEqual(spec.mcp, ("visual", "context7"))
        self.assertEqual(GoalSpec.from_dict(spec.as_dict()).mcp, ("visual", "context7"))

    def test_effort_accepts_ultra(self):
        # ultra is the top reasoning level; it must be accepted everywhere effort is.
        self.assertEqual(GoalSpec.from_dict({**_valid(), "effort": "ultra"}).effort, "ultra")

    def test_mcp_typo_against_a_present_catalog_errors(self):
        # A repo that DOES ship a catalog: an unknown name is a typo, caught at load.
        with tempfile.TemporaryDirectory() as tmp:
            catalog_dir = Path(tmp) / ".seats"
            catalog_dir.mkdir()
            (catalog_dir / "worker-mcp.json").write_text(json.dumps({"servers": {"visual": {"command": "x"}}}))
            raw = {**_valid(), "repo": tmp, "mcp": ["visual"]}
            self.assertEqual(GoalSpec.from_dict(raw).mcp, ("visual",))  # known name: ok
            with self.assertRaises(SpecError) as ctx:
                GoalSpec.from_dict({**raw, "mcp": ["visul"]})  # typo
            self.assertIn("visul", str(ctx.exception))
            self.assertIn("visual", str(ctx.exception))  # lists the known servers

    def test_link_paths_parse_and_default_empty(self):
        self.assertEqual(GoalSpec.from_dict(_valid()).link, ())
        raw = {**_valid(), "link": [".venv", "node_modules"]}
        self.assertEqual(GoalSpec.from_dict(raw).link, (".venv", "node_modules"))

    def test_strings_are_stripped(self):
        raw = _valid()
        raw["goal"] = "  tidy up  "
        raw["acceptance"] = ["  pytest -q  "]
        spec = GoalSpec.from_dict(raw)
        self.assertEqual(spec.goal, "tidy up")
        self.assertEqual(spec.acceptance, ("pytest -q",))


class GoalSpecValidationTests(unittest.TestCase):
    def _rejects(self, mutate, needle: str):
        raw = _valid()
        mutate(raw)
        with self.assertRaises(SpecError) as ctx:
            GoalSpec.from_dict(raw)
        self.assertIn(needle, str(ctx.exception))

    def test_rejects_non_object(self):
        with self.assertRaises(SpecError):
            GoalSpec.from_dict(["not", "an", "object"])

    def test_rejects_unknown_key(self):
        self._rejects(lambda r: r.update(contnuation="typo"), "unknown key")

    def test_rejects_missing_goal(self):
        self._rejects(lambda r: r.pop("goal"), "non-empty 'goal'")

    def test_rejects_blank_goal(self):
        self._rejects(lambda r: r.update(goal="   "), "non-empty 'goal'")

    def test_rejects_missing_repo(self):
        self._rejects(lambda r: r.pop("repo"), "'repo' path")

    def test_rejects_relative_repo(self):
        self._rejects(lambda r: r.update(repo="relative/path"), "absolute path")

    def test_rejects_missing_acceptance(self):
        self._rejects(lambda r: r.pop("acceptance"), "'acceptance'")

    def test_rejects_empty_acceptance_list(self):
        self._rejects(lambda r: r.update(acceptance=[]), "'acceptance'")

    def test_rejects_blank_acceptance_command(self):
        self._rejects(lambda r: r.update(acceptance=["ok", "  "]), "acceptance[1]")

    def test_rejects_unknown_head(self):
        self._rejects(lambda r: r.update(head="gemini"), "'head' must be one of")

    def test_rejects_non_list_constraints(self):
        self._rejects(lambda r: r.update(constraints="a single string"), "'constraints' must be a list")

    def test_rejects_zero_max_iterations(self):
        self._rejects(lambda r: r.update(max_iterations=0), "at least 1")

    def test_rejects_bool_max_iterations(self):
        # bool is a subclass of int; it must not slip through as a count.
        self._rejects(lambda r: r.update(max_iterations=True), "must be an integer")

    def test_rejects_absolute_link(self):
        self._rejects(lambda r: r.update(link=["/etc/passwd"]), "relative path inside the repo")

    def test_rejects_link_escaping_repo(self):
        self._rejects(lambda r: r.update(link=["../secrets"]), "relative path inside the repo")

    def test_protected_paths_parse_and_reject_escapes(self):
        raw = {**_valid(), "protected": ["tests/test_goal.py", "gates.json"]}
        self.assertEqual(
            GoalSpec.from_dict(raw).protected,
            (
                "tests/test_goal.py",
                "gates.json",
            ),
        )
        self._rejects(lambda r: r.update(protected=["../evil"]), "relative path inside the repo")

    def test_rejects_breaker_that_never_trips(self):
        self._rejects(
            lambda r: r.update(max_iterations=3, pause_after_failures=5),
            "cannot exceed",
        )


class GoalSpecPresetTests(unittest.TestCase):
    def _repo_with_preset(self, tmp: str, payload) -> dict:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        if payload is not None:
            (repo / ".seats-gates.json").write_text(json.dumps(payload))
        raw = _valid()
        raw["repo"] = str(repo)
        del raw["preset"]  # default: auto-discover
        return raw

    def test_preset_gates_append_and_preset_file_is_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = self._repo_with_preset(
                tmp, {"gates": ["make test", "make lint"], "link": [".venv"]}
            )
            spec = GoalSpec.from_dict(raw)
            self.assertEqual(
                spec.acceptance,
                ("PYTHONPATH=lib .venv/bin/python -m pytest tests -q", "make test", "make lint"),
            )
            self.assertEqual(spec.link, (".venv",))
            self.assertIn(".seats-gates.json", spec.protected)

    def test_missing_preset_is_fine_when_auto_discovering(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = GoalSpec.from_dict(self._repo_with_preset(tmp, None))
            self.assertEqual(len(spec.acceptance), 1)
            # Baseline auto-protection: the (defaulted) gate's judge config.
            self.assertEqual(
                spec.protected,
                (
                        ),
            )

    def test_preset_false_skips_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = self._repo_with_preset(tmp, {"gates": ["make test"]})
            raw["preset"] = False
            spec = GoalSpec.from_dict(raw)
            self.assertEqual(len(spec.acceptance), 1)

    def test_explicit_preset_path_must_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = self._repo_with_preset(tmp, None)
            raw["preset"] = "missing-gates.json"
            with self.assertRaises(SpecError) as ctx:
                GoalSpec.from_dict(raw)
            self.assertIn("unreadable", str(ctx.exception))

    def test_removed_redteam_preset_is_rejected_with_remedy(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / ".seats-gates.json").write_text(json.dumps({
                "gates": ["true"], "spec_redteam": ["tool", "{spec}"]
            }))
            raw = {"goal": "g", "repo": str(repo), "acceptance": ["true"]}
            with self.assertRaisesRegex(SpecError, "remove them and use acceptance gates"):
                GoalSpec.from_dict(raw)

    def test_malformed_preset_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = self._repo_with_preset(tmp, {"nope": []})
            with self.assertRaises(SpecError) as ctx:
                GoalSpec.from_dict(raw)
            self.assertIn("'gates' list", str(ctx.exception))

    def test_round_trip_does_not_reapply_preset(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = self._repo_with_preset(tmp, {"gates": ["make test"]})
            spec = GoalSpec.from_dict(raw)
            again = GoalSpec.from_dict(spec.as_dict())
            self.assertEqual(spec, again)


class GoalSpecLoadTests(unittest.TestCase):
    def test_load_reads_and_validates_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "goal.json"
            path.write_text(json.dumps(_valid()))
            spec = GoalSpec.load(path)
            self.assertEqual(spec.goal, "Extract the router from provider_claude")

    def test_load_reports_bad_json_with_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "goal.json"
            path.write_text("{not json")
            with self.assertRaises(SpecError) as ctx:
                GoalSpec.load(path)
            self.assertIn("not valid JSON", str(ctx.exception))

    def test_load_reports_missing_file(self):
        with self.assertRaises(SpecError) as ctx:
            GoalSpec.load("/no/such/goal-spec-xyz.json")
        self.assertIn("unreadable", str(ctx.exception))


class JudgeIntegrityAutoProtectTests(unittest.TestCase):
    def _repo(self) -> Path:
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        repo = Path(directory)
        (repo / "tools").mkdir()
        (repo / "tools" / "codex_review.py").write_text("# gate\n")
        (repo / ".codex-seats").mkdir()
        (repo / ".codex-seats" / "review.json").write_text("{}")
        return repo

    def _spec(self, repo: Path, review: str) -> GoalSpec:
        return GoalSpec.from_dict({
            "goal": "do a thing", "repo": str(repo),
            "acceptance": ["true"], "preset": False, "review": review,
        })

    def test_repo_relative_gate_and_config_are_auto_protected(self):
        spec = self._spec(
            self._repo(),
            'python tools/codex_review.py --diff-file "$CODEX_SEATS_DIFF" --implementer codex',
        )
        self.assertIn("tools/codex_review.py", spec.protected)

    def test_absolute_path_gate_in_another_repo_is_not_protected(self):
        # A gate referenced by absolute path lives in another checkout (already
        # pristine); it must not be pulled into this repo's protected set.
        repo = self._repo()
        spec = self._spec(repo, "python /elsewhere/tools/codex_review.py --diff-file x")
        self.assertNotIn("/elsewhere/tools/codex_review.py", spec.protected)
        self.assertFalse(any("elsewhere" in p for p in spec.protected))

    def test_none_review_means_no_auto_protection(self):
        repo = self._repo()
        (repo / ".codex-seats").mkdir(exist_ok=True)
        (repo / ".codex-seats" / "review.json").write_text("{}")
        spec = self._spec(repo, "none")
        self.assertEqual(spec.protected, ())

    def test_non_gate_tokens_are_ignored(self):
        # The venv python and flag values must not be protected.
        repo = self._repo()
        spec = self._spec(repo, 'backend/.venv/bin/python tools/codex_review.py --tier normal')
        self.assertIn("tools/codex_review.py", spec.protected)
        self.assertFalse(any(".venv" in p for p in spec.protected))


if __name__ == "__main__":
    unittest.main()
def test_review_resolution_defaults_off_and_honors_explicit_commands(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from codex_seats import goalspec

    monkeypatch.setattr(goalspec, "REVIEW_GATE", tmp_path / "missing.py")
    spec = SimpleNamespace(review="", review_model="")
    assert goalspec.resolve_review_command(spec) == ""
    with pytest.raises(goalspec.SpecError, match="review gate missing"):
        goalspec.resolve_review_command(spec, mode="blocking")
    spec.review = "echo explicit"
    assert goalspec.resolve_review_command(spec, mode="off") == "echo explicit"
    spec.review = "none"
    assert goalspec.resolve_review_command(spec, mode="blocking") == ""
