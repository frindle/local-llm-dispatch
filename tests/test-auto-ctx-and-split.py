#!/usr/bin/env python3
"""Unit tests for auto num-ctx sizing (PART A) + auto split/recombination (PART B)
in ollama-queue.py.

Tests the REAL functions imported from ollama-queue.py (not mirrors):
  size_num_ctx, estimate_task_tokens, historical_p90_tokens, named_files_chars,
  decide_split, decompose_task, _percentile.

PART A asserts: bucket selection, +headroom, and host-ceiling clamping, plus the
historical-p90 floor read from a stub dispatch-metrics.jsonl.
PART B asserts: the fit-decision (split iff overflow / --auto-split, never on
--no-split) and the decomposer (multi-target task -> N sub-specs; a single-target
task -> no split).

Run:
  python3 -m unittest test-auto-ctx-and-split      # (from ~/bin; hyphen import via loader below is only for direct run)
  python3 test-auto-ctx-and-split.py               # direct
  python3 test-auto-ctx-and-split.py -v            # verbose
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

QUEUE = __import__("os").path.expanduser("~/bin/ollama-queue.py")


def _load():
    spec = importlib.util.spec_from_file_location("ollama_queue_undertest", QUEUE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


oq = _load()


class TestSizeNumCtx(unittest.TestCase):
    STUDIO = 131072  # oq.AUTO_RESUME_STUDIO_CTX_CEILING

    def test_small_task_lowest_bucket(self):
        # 5000 tok * 1.4 = 7000 -> smallest bucket >= 7000 = 16384, no overflow.
        chosen, overflow, target = oq.size_num_ctx(5000, self.STUDIO)
        self.assertEqual(chosen, 16384)
        self.assertFalse(overflow)
        self.assertEqual(target, 7000)

    def test_headroom_is_applied(self):
        # 24000 * 1.4 = 33600 -> next bucket up is 49152 (not 32768).
        chosen, overflow, target = oq.size_num_ctx(24000, self.STUDIO)
        self.assertEqual(target, 33600)
        self.assertEqual(chosen, 49152)
        self.assertFalse(overflow)

    def test_snaps_up_not_down(self):
        # 12000 * 1.4 = 16800 -> 16384 is too small, must snap UP to 32768.
        chosen, _, _ = oq.size_num_ctx(12000, self.STUDIO)
        self.assertEqual(chosen, 32768)

    def test_overflow_top_bucket(self):
        # STALE FIXTURE REPAIRED (2026-09-23): this used to assert 84000 > 65536
        # overflows, but CTX_BUCKETS grew to (..., 98304, 131072) on 2026-09-2x and
        # 84000 now fits the 98304 bucket -- the test had been red ever since, in a
        # file nobody was running. Assert the PROPERTY against the real top bucket.
        top = max(b for b in oq.CTX_BUCKETS if b <= self.STUDIO)
        est = top  # est * 1.4 > top for any headroom > 0 -> must overflow
        chosen, overflow, target = oq.size_num_ctx(est, self.STUDIO)
        self.assertTrue(overflow)
        self.assertEqual(chosen, top)
        self.assertEqual(target, (est * (100 + oq.CTX_HEADROOM_PCT)) // 100)
        # ...and a value that fits the top bucket does NOT overflow.
        chosen2, overflow2, _ = oq.size_num_ctx(60000, self.STUDIO)
        self.assertFalse(overflow2)
        self.assertGreaterEqual(chosen2, 84000)

    def test_host_ceiling_clamps_below_all_buckets(self):
        # An Unraid per-model cap of 12288 is below the smallest bucket (16384):
        # the chosen value must clamp to the ceiling and flag overflow.
        chosen, overflow, _ = oq.size_num_ctx(5000, 12288)
        self.assertEqual(chosen, 12288)
        self.assertTrue(overflow)

    def test_host_ceiling_caps_selection(self):
        # Ceiling of 32768 forbids the 49152/65536 buckets even for a big estimate.
        chosen, overflow, _ = oq.size_num_ctx(60000, 32768)
        self.assertEqual(chosen, 32768)
        self.assertTrue(overflow)

    def test_never_exceeds_ceiling(self):
        for est in (100, 5000, 24000, 60000, 200000):
            for ceiling in (6144, 12288, 32768, 65536, 131072):
                chosen, _, _ = oq.size_num_ctx(est, ceiling)
                self.assertLessEqual(chosen, ceiling,
                                     f"est={est} ceiling={ceiling} -> {chosen}")


class TestPercentile(unittest.TestCase):
    def test_p90(self):
        vals = sorted(range(1, 11))  # 1..10
        self.assertEqual(oq._percentile(vals, 90), 9)

    def test_empty(self):
        self.assertIsNone(oq._percentile([], 90))


class TestHistoricalP90(unittest.TestCase):
    def _write_metrics(self, records):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        for r in records:
            f.write(json.dumps(r) + "\n")
        f.close()
        return f.name

    def test_reads_p90_of_similar_converged(self):
        # 5 converged records near task_chars=2000; peak_total_tokens 10k..50k.
        recs = [{"status": "converged", "task_chars": 2000, "peak_total_tokens": t}
                for t in (10000, 20000, 30000, 40000, 50000)]
        path = self._write_metrics(recs)
        p90 = oq.historical_p90_tokens(2000, path)
        self.assertEqual(p90, 50000)  # nearest-rank p90 of 5 -> top value

    def test_ignores_non_converged_and_out_of_band(self):
        recs = [
            {"status": "failed", "task_chars": 2000, "peak_total_tokens": 999999},
            {"status": "converged", "task_chars": 100000, "peak_total_tokens": 999999},
            {"status": "converged", "task_chars": 2000, "peak_total_tokens": 10000},
            {"status": "converged", "task_chars": 2200, "peak_total_tokens": 12000},
        ]
        path = self._write_metrics(recs)
        # Only 2 in-band converged samples -> below min_samples -> None.
        self.assertIsNone(oq.historical_p90_tokens(2000, path))

    def test_missing_file(self):
        self.assertIsNone(oq.historical_p90_tokens(2000, "/no/such/file.jsonl"))

    def test_feeds_estimate_as_floor(self):
        # A tiny task file but rich history -> estimate rises to the historical p90.
        recs = [{"status": "converged", "task_chars": 50, "peak_total_tokens": t}
                for t in (30000, 31000, 32000, 33000, 34000)]
        path = self._write_metrics(recs)
        est, info = oq.estimate_task_tokens("x" * 50, None, path)
        self.assertEqual(info["hist_p90_tokens"], 34000)
        self.assertEqual(est, 34000)  # history floor beats the ~12-token char estimate


class TestNamedFilesChars(unittest.TestCase):
    def test_counts_existing_named_files(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "app.py").write_text("A" * 400)
            (Path(d) / "util.js").write_text("B" * 600)
            task = "Fix `app.py` and also util.js; ignore missing.ts entirely."
            self.assertEqual(oq.named_files_chars(task, d), 1000)

    def test_none_cwd(self):
        self.assertEqual(oq.named_files_chars("app.py", None), 0)


class TestDecideSplit(unittest.TestCase):
    def test_no_split_flag_wins(self):
        self.assertEqual(oq.decide_split(True, True, True)[0], False)

    def test_overflow_triggers(self):
        self.assertTrue(oq.decide_split(True, False, False)[0])

    def test_fits_no_split(self):
        self.assertFalse(oq.decide_split(False, False, False)[0])

    def test_auto_split_opts_in_even_when_fits(self):
        self.assertTrue(oq.decide_split(False, True, False)[0])

    def test_auto_split_yielded_by_no_split(self):
        self.assertFalse(oq.decide_split(False, True, True)[0])


class TestDecomposeTask(unittest.TestCase):
    def test_targets_section_yields_n_subspecs(self):
        task = (
            "# Big multi-file fix\n\n"
            "Shared context: keep the API stable.\n\n"
            "## Targets\n"
            "- src/a.py: fix the parser\n"
            "- src/b.py: fix the serializer\n"
            "- src/c.py: fix the writer\n"
        )
        subs = oq.decompose_task(task)
        self.assertEqual(len(subs), 3)
        # Shared preamble is carried into every sub-spec.
        for s in subs:
            self.assertIn("keep the API stable", s["body"])
        self.assertIn("src/a.py", subs[0]["body"])
        self.assertIn("src/c.py", subs[2]["body"])

    def test_numbered_subtasks_section(self):
        task = (
            "Preamble line.\n\n"
            "## Sub-tasks\n"
            "1. Update the config loader\n"
            "2. Update the CLI parser\n"
        )
        subs = oq.decompose_task(task)
        self.assertEqual(len(subs), 2)

    def test_h2_per_file_fallback(self):
        task = (
            "# Fix two modules\n\n"
            "## Parser\nEdit parser.py to accept tabs.\n\n"
            "## Writer\nEdit writer.py to emit spaces.\n"
        )
        subs = oq.decompose_task(task)
        self.assertEqual(len(subs), 2)

    def test_single_target_no_split(self):
        task = "# Fix one thing\n\nEdit only app.py to fix the off-by-one.\n"
        self.assertEqual(oq.decompose_task(task), [])

    def test_prose_with_no_targets_no_split(self):
        task = "Please make the login flow remember the device between sessions."
        self.assertEqual(oq.decompose_task(task), [])


if __name__ == "__main__":
    unittest.main()
