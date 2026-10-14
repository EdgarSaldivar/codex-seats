"""resolve_review_command — §H wiring: the package review gate as the default gate."""
import unittest
from pathlib import Path
from unittest import mock

from codex_seats import goalspec


def _spec(review: str = "", review_model: str = "") -> goalspec.GoalSpec:
    spec = mock.Mock(spec=goalspec.GoalSpec)
    spec.review = review
    spec.review_model = review_model
    return spec


class ResolveReviewCommandTests(unittest.TestCase):
    def _resolve(self, spec, *, mode="off", strategy="single"):
        return goalspec.resolve_review_command(spec, mode=mode, strategy=strategy)

    def test_explicit_command_passes_through_verbatim(self):
        cmd = 'python3 tools/codex_review.py --diff-file "$CODEX_SEATS_DIFF"'
        self.assertEqual(self._resolve(_spec(cmd)), cmd)

    def test_none_is_an_explicit_opt_out(self):
        self.assertEqual(self._resolve(_spec("none"), mode="blocking"), "")
        self.assertEqual(self._resolve(_spec("  NONE "), mode="blocking"), "")

    def test_empty_defaults_to_off(self):
        self.assertEqual(self._resolve(_spec("")), "")

    def test_enabled_default_uses_review_and_strategy(self):
        cmd = self._resolve(_spec(""), mode="blocking", strategy="cross")
        self.assertIn("codex_seats/review.py", cmd)
        self.assertIn('--diff-file "$CODEX_SEATS_DIFF"', cmd)
        # Opposes the REAL implementer, exported per judge cycle.
        self.assertIn('--implementer "${CODEX_SEATS_HEAD_PROVIDER:-codex}"', cmd)
        self.assertNotIn("--review-model", cmd)

    def test_review_model_pin_only_when_enabled(self):
        self.assertEqual(self._resolve(_spec("", review_model="gpt-5.6-sol")), "")
        cmd = self._resolve(_spec("", review_model="gpt-5.6-sol"), mode="advisory")
        self.assertIn("--review-model gpt-5.6-sol", cmd)

    def test_missing_review_fails_when_enabled(self):
        with mock.patch.object(goalspec, "REVIEW_GATE",
                               Path("/nonexistent/codex_review.py")):
            self.assertEqual(self._resolve(_spec("")), "")
            with self.assertRaises(goalspec.SpecError):
                self._resolve(_spec(""), mode="blocking")
            # The explicit opt-out still works without the review gate.
            self.assertEqual(self._resolve(_spec("none"), mode="blocking"), "")

    def test_review_is_committed_here(self):
        # The default is only sound because the review gate is part of the package.
        self.assertTrue(goalspec.REVIEW_GATE.is_file())


if __name__ == "__main__":
    unittest.main()
