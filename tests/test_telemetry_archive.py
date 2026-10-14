"""Jobs-ledger truncation must archive the dropped entries (by ts month) so
long-horizon routing telemetry survives the in-ledger cap — and archiving must
never be able to break job recording."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from codex_seats.telemetry import trim_jobs_with_archive


def _job(i: int, ts: str) -> dict:
    return {"id": f"job-{i}", "ts": ts, "provider": "codex", "tokens": i}


class TrimJobsWithArchiveTests(unittest.TestCase):
    def test_over_cap_truncates_and_archives_by_month(self):
        jobs = (
            [_job(i, "2026-06-30T12:00:00Z") for i in range(3)]
            + [_job(i + 3, "2026-07-01T12:00:00Z") for i in range(2)]
            + [_job(i + 5, "2026-07-02T12:00:00Z") for i in range(10)]
        )
        data = {"jobs": list(jobs)}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trim_jobs_with_archive(data, root, cap=10)
            self.assertEqual(len(data["jobs"]), 10)
            self.assertEqual(data["jobs"][0]["id"], "job-5")  # newest 10 kept
            june = (root / "archive" / "jobs-2026-06.jsonl").read_text().splitlines()
            july = (root / "archive" / "jobs-2026-07.jsonl").read_text().splitlines()
            self.assertEqual([json.loads(line)["id"] for line in june],
                             ["job-0", "job-1", "job-2"])
            self.assertEqual([json.loads(line)["id"] for line in july],
                             ["job-3", "job-4"])

    def test_at_or_under_cap_is_a_no_op(self):
        data = {"jobs": [_job(0, "2026-07-01T00:00:00Z")]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trim_jobs_with_archive(data, root, cap=1)
            self.assertEqual(len(data["jobs"]), 1)
            self.assertFalse((root / "archive").exists())

    def test_appends_across_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = {"jobs": [_job(i, "2026-07-01T00:00:00Z") for i in range(3)]}
            trim_jobs_with_archive(data, root, cap=2)
            data["jobs"].insert(0, _job(99, "2026-07-01T00:00:00Z"))
            trim_jobs_with_archive(data, root, cap=2)
            lines = (root / "archive" / "jobs-2026-07.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 2)  # one dropped per call, appended

    def test_bad_ts_buckets_to_current_month_and_never_crashes(self):
        data = {"jobs": [{"id": "weird", "ts": None}, _job(1, "2026-07-01T00:00:00Z")]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trim_jobs_with_archive(data, root, cap=1)
            files = list((root / "archive").glob("jobs-*.jsonl"))
            self.assertEqual(len(files), 1)  # bucketed somewhere valid

    def test_unwritable_archive_still_truncates(self):
        data = {"jobs": [_job(i, "2026-07-01T00:00:00Z") for i in range(5)]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.chmod(root, stat.S_IRUSR | stat.S_IXUSR)  # no write -> mkdir fails
            try:
                trim_jobs_with_archive(data, root, cap=2)
            finally:
                os.chmod(root, stat.S_IRWXU)
            self.assertEqual(len(data["jobs"]), 2)  # truncation applied regardless


if __name__ == "__main__":
    unittest.main()
