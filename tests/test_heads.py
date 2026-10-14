import tempfile
import unittest
from pathlib import Path

from codex_seats.heads import dispatch_head
from codex_seats.supervisor import HeadResult


class _FakeResult:
    def __init__(self, status, message=None, tokens=0, provider=""):
        self.status = status
        self.message = message
        self.tokens = tokens
        self.provider = provider


class DispatchHeadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = str(Path(temporary.name).resolve())

    def test_builds_request_and_maps_completed_result(self):
        seen: dict = {}

        def fake_dispatch(root, settings, request):
            seen.update(request)
            seen["_root"] = root
            seen["_settings"] = settings
            return _FakeResult("completed", "did it", 1234)

        head = dispatch_head(self.root, {"k": "v"}, provider="codex", dispatch_fn=fake_dispatch)
        result = head(Path("/tmp/wt"), "please fix the failing test")

        self.assertIsInstance(result, HeadResult)
        self.assertTrue(result.ok)
        self.assertEqual(result.summary, "did it")
        self.assertEqual(result.tokens, 1234)
        # request shape the supervisor relies on
        self.assertEqual(seen["cd"], "/tmp/wt")
        self.assertEqual(seen["provider"], "codex")
        self.assertEqual(seen["prompt"], "please fix the failing test")
        self.assertEqual(seen["write_scope"], ["."])
        self.assertEqual(seen["_root"], self.root)
        self.assertEqual(seen["_settings"], {"k": "v"})

    def test_model_and_effort_flow_into_the_request_when_set(self):
        seen: dict = {}

        def fake_dispatch(root, settings, request):
            seen.update(request)
            return _FakeResult("completed", "ok", 0)

        head = dispatch_head(
            self.root, {}, provider="codex", model="gpt-5.6-sol", effort="high",
            dispatch_fn=fake_dispatch,
        )
        head(Path("/tmp/wt"), "p")
        self.assertEqual(seen["model"], "gpt-5.6-sol")
        self.assertEqual(seen["effort"], "high")

    def test_mcp_allowlist_flows_into_request(self):
        seen: dict = {}

        def fake_dispatch(root, settings, request):
            seen.update(request)
            return _FakeResult("completed", "ok", 0)

        dispatch_head(self.root, {}, provider="codex", mcp=("visual",), dispatch_fn=fake_dispatch)(
            Path("/tmp/wt"), "p"
        )
        self.assertEqual(seen["mcp"], ["visual"])

    def test_model_and_effort_absent_when_not_set(self):
        seen: dict = {}

        def fake_dispatch(root, settings, request):
            seen.update(request)
            return _FakeResult("completed", "ok", 0)

        dispatch_head(self.root, {}, dispatch_fn=fake_dispatch)(Path("/tmp/wt"), "p")
        self.assertNotIn("model", seen)
        self.assertNotIn("effort", seen)

    def test_non_completed_status_is_not_ok(self):
        def fake_dispatch(root, settings, request):
            return _FakeResult("reserved", "no capacity", 0)

        head = dispatch_head(self.root, {}, dispatch_fn=fake_dispatch)
        result = head(Path("/tmp/wt"), "p")
        self.assertFalse(result.ok)
        self.assertEqual(result.summary, "no capacity")

    def test_summary_falls_back_to_status_when_no_message(self):
        def fake_dispatch(root, settings, request):
            return _FakeResult("completed", None, 0)

        head = dispatch_head(self.root, {}, dispatch_fn=fake_dispatch)
        self.assertEqual(head(Path("/tmp/wt"), "p").summary, "completed")

    def test_goal_id_stamps_label_and_iteration_across_calls(self):
        requests: list[dict] = []

        def fake_dispatch(root, settings, request):
            requests.append(dict(request))
            return _FakeResult("completed", "ok", 0)

        head = dispatch_head(self.root, {}, goal_id="g123", dispatch_fn=fake_dispatch)
        head(Path("/tmp/wt"), "first")
        head(Path("/tmp/wt"), "second")
        self.assertEqual([r["goal_id"] for r in requests], ["g123", "g123"])
        self.assertEqual([r["label"] for r in requests], ["g123", "g123"])
        self.assertEqual([r["iteration"] for r in requests], [1, 2])

    def test_protected_paths_flow_into_the_request(self):
        seen: dict = {}

        def fake_dispatch(root, settings, request):
            seen.update(request)
            return _FakeResult("completed", "ok", 0)

        dispatch_head(
            self.root, {}, protected_paths=("tools/codex_review.py", ".codex-seats/review.json"),
            dispatch_fn=fake_dispatch,
        )(Path("/tmp/wt"), "p")
        self.assertEqual(
            seen["protected_paths"], ["tools/codex_review.py", ".codex-seats/review.json"]
        )

    def test_no_protected_paths_means_no_key(self):
        seen: dict = {}

        def fake_dispatch(root, settings, request):
            seen.update(request)
            return _FakeResult("completed", "ok", 0)

        dispatch_head(self.root, {}, dispatch_fn=fake_dispatch)(Path("/tmp/wt"), "p")
        self.assertNotIn("protected_paths", seen)

    def test_resolved_provider_flows_onto_the_result(self):
        # provider="auto" but the router landed the task on codex — the head must
        # surface where it ACTUALLY ran so the review gate opposes the real family.
        def fake_dispatch(root, settings, request):
            return _FakeResult("completed", "ok", 0, provider="codex")

        head = dispatch_head(self.root, {}, provider="auto", dispatch_fn=fake_dispatch)
        self.assertEqual(head(Path("/tmp/wt"), "p").provider, "codex")

    def test_provider_defaults_to_empty_when_result_lacks_it(self):
        class _NoProvider:
            status, message, tokens = "completed", "ok", 0

        head = dispatch_head(self.root, {}, dispatch_fn=lambda *a: _NoProvider())
        self.assertEqual(head(Path("/tmp/wt"), "p").provider, "")

    def test_no_goal_id_means_no_trace_keys(self):
        seen: dict = {}

        def fake_dispatch(root, settings, request):
            seen.update(request)
            return _FakeResult("completed", "ok", 0)

        dispatch_head(self.root, {}, dispatch_fn=fake_dispatch)(Path("/tmp/wt"), "p")
        for key in ("goal_id", "label", "iteration"):
            self.assertNotIn(key, seen)


if __name__ == "__main__":
    unittest.main()
