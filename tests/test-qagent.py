#!/usr/bin/env python3
"""test-qagent.py -- qagent against a STUB queue (no real model, no real queue).
Includes a revert-proof: mutating qagent's read-only guard makes the suite fail."""
import json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
QAGENT = HERE / "qagent"
STUB = """#!/usr/bin/env python3
import sys, json, os
open(os.environ['STUB_LOG'], 'a').write(json.dumps(sys.argv[1:]) + '\\n')
print('enqueued job abc123def456 (stub)')
"""


class T(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        (self.d / "stubq.py").write_text(STUB)
        self.log = self.d / "stub.log"
        self.state = self.d / "state.json"
        self.logs = self.d / "logs"; self.logs.mkdir()
        self.env = dict(os.environ, QAGENT_QUEUE_PY=str(self.d / "stubq.py"), STUB_LOG=str(self.log),
                        QAGENT_STATE=str(self.state), QAGENT_LOGDIR=str(self.logs),
                        QAGENT_HOME=str(self.d / "home"), QAGENT_ABORT=str(self.d / "abort.sh"))
        (self.d / "watcher.py").write_text("import sys,os\nopen(os.environ['WATCH_LOG'],'a').write(' '.join(sys.argv[1:])+'\\n')\n")
        self.env.update(QAGENT_WATCHER=str(self.d / "watcher.py"), WATCH_LOG=str(self.d / "watch.log"))
        (self.d / "abort.sh").write_text("#!/bin/sh\necho \"$@\" > %s/abort.out\n" % self.d)
        os.chmod(self.d / "abort.sh", 0o755)

    def q(self, *a, bin=None):
        return subprocess.run([sys.executable, str(bin or QAGENT), *a], capture_output=True, text=True, env=self.env)

    def argv(self):
        return json.loads(self.log.read_text().splitlines()[-1])

    def test_readonly_run(self):
        r = self.q("run", "--readonly", "--label", "x", "do a thing")
        self.assertEqual(r.stdout.strip(), "abc123def456", r.stderr)
        a = self.argv()
        self.assertEqual(a[a.index("--task-kind") + 1], "research")
        self.assertNotIn("--allow-no-verify", a)
        self.assertIn("--cwd", a)
        self.assertEqual(a[a.index("--model") + 1], "qwen3.8:27b-q4_K_M")
        self.assertNotIn("--temperature", a)  # card profile applies; no hardcoded sampling
        self.assertIn("MUST NOT create, edit", Path(a[a.index("--task-file") + 1]).read_text())

    def test_bounds_always_set_and_no_sampling(self):
        self.q("run", "--readonly", "x")
        a = self.argv()
        self.assertEqual(a[a.index("--max-tokens") + 1], "12000")
        self.assertEqual(a[a.index("--max-iters") + 1], "12")
        self.assertEqual(a[a.index("--chat-timeout") + 1], "900")
        for f in ("--temperature", "--top-p", "--num-ctx"):
            self.assertNotIn(f, a)
        self.q("run", "--max-tokens", "500", "x"); a = self.argv()
        self.assertEqual(a[a.index("--max-tokens") + 1], "500")

    def test_wall_cap_aborts_running_job(self):
        self.q("run", "--readonly", "--max-wall", "60", "x")
        tf = self.argv()[self.argv().index("--task-file") + 1]
        self.state.write_text(json.dumps({"jobs": [{"id": "abc123def456", "status": "running",
                              "active_s": 61, "task_file": tf}]}))
        self.q("status", "abc123")
        self.assertIn("abc123", (self.d / "abort.out").read_text())
        (self.d / "abort.out").unlink()
        self.state.write_text(json.dumps({"jobs": [{"id": "abc123def456", "status": "running",
                              "active_s": 59, "task_file": tf}]}))
        self.q("status", "abc123")
        self.assertFalse((self.d / "abort.out").exists())

    def test_write_run_with_repo(self):
        self.q("run", "--repo", str(self.d), "fix it")
        a = self.argv()
        self.assertIn("--repo", a); self.assertNotIn("--cwd", a)
        self.assertEqual(a[a.index("--task-kind") + 1], "coding")
        self.assertIn("--allow-no-verify", a)

    def test_dry_run_does_not_enqueue(self):
        r = self.q("run", "--dry-run", "hi")
        self.assertEqual(r.returncode, 0)
        self.assertFalse(self.log.exists())

    def test_result_banner_answer_and_violation(self):
        tr = self.d / "tr.json"
        tr.write_text(json.dumps({"messages": [{"role": "user", "content": "q"},
                                               {"role": "assistant", "content": "THE ANSWER"}]}))
        lg = self.d / "j.log"; lg.write_text(f"[worker] full transcript written to {tr}\n")
        repo = self.d / "wt"; repo.mkdir()
        g = lambda *x: subprocess.run(["git", "-C", str(repo), *x], capture_output=True, check=True)
        g("init"); g("-c", "user.email=a@b", "-c", "user.name=a", "commit", "--allow-empty", "-m", "i")
        (repo / "new.txt").write_text("x")
        self.state.write_text(json.dumps({"jobs": [{"id": "abc123def456", "status": "done", "exit_code": 0,
                              "task_kind": "research", "log_path": str(lg), "cwd": str(repo)}]}))
        r = self.q("result", "abc123")
        self.assertIn("UNVERIFIED local-model output", r.stdout)
        self.assertIn("THE ANSWER", r.stdout)
        self.assertIn("READONLY VIOLATION", r.stdout)
        self.assertIn(str(lg), r.stdout)

    def test_wait_and_status(self):
        self.state.write_text(json.dumps({"jobs": [{"id": "abc123def456", "status": "running"}]}))
        self.assertEqual(self.q("wait", "abc123", "--timeout", "1", "--poll", "0.2").returncode, 2)
        self.state.write_text(json.dumps({"jobs": [{"id": "abc123def456", "status": "failed"}]}))
        self.assertEqual(self.q("wait", "abc123", "--timeout", "1").returncode, 1)
        self.assertIn("status=failed", self.q("status", "abc123").stdout)

    def test_followup_carries_context_and_abort(self):
        self.q("run", "--readonly", "orig prompt text")
        tf = self.argv()[self.argv().index("--task-file") + 1]
        tr = self.d / "tr.json"; tr.write_text(json.dumps({"messages": [{"role": "assistant", "content": "PRIOR"}]}))
        lg = self.d / "j.log"; lg.write_text(f"[worker] full transcript written to {tr}\n")
        self.state.write_text(json.dumps({"jobs": [{"id": "abc123def456", "status": "done", "task_kind": "research",
                              "task_file": tf, "log_path": str(lg), "model": "m1"}]}))
        self.q("followup", "abc123", "now do B")
        a = self.argv(); body = Path(a[a.index("--task-file") + 1]).read_text()
        for s in ("orig prompt text", "PRIOR", "now do B"):
            self.assertIn(s, body)
        self.assertEqual(a[a.index("--task-kind") + 1], "research")
        self.q("abort", "abc123")
        self.assertIn("abc123", (self.d / "abort.out").read_text())

    def test_revert_proof_readonly_guard_bites(self):
        """Mutate the research task-kind for --readonly; test_readonly_run's assertion must then fail."""
        src = QAGENT.read_text()
        mut = src.replace('argv += ["--task-kind", "research"]', 'argv += ["--task-kind", "coding"]')
        self.assertNotEqual(src, mut)
        mp = self.d / "qagent_mut"; mp.write_text(mut)
        self.q("run", "--readonly", "x", bin=mp)
        a = self.argv()
        self.assertNotEqual(a[a.index("--task-kind") + 1], "research")  # mutant is detectable by the real test

    def _wait_for(self, path, secs=5):
        t = time.time()
        while time.time() - t < secs:
            if path.exists():
                return True
            time.sleep(0.1)
        return False

    def test_preamble_has_truncation_guidance(self):
        for flags in (["--readonly"], []):
            self.q("run", *flags, "x")
            body = Path(self.argv()[self.argv().index("--task-file") + 1]).read_text()
            self.assertIn("may be truncated", body)
            self.assertIn("never claim completeness", body)

    def test_result_surfaces_worker_truncation(self):
        tr = self.d / "tr.json"
        tr.write_text(json.dumps({"messages": [
            {"role": "tool", "content": "[OUTPUT TRUNCATED: the first 9000 chars (~80 lines) of 12000 were omitted; x]\nrows"},
            {"role": "assistant", "content": "ANS"}]}))
        lg = self.d / "j.log"; lg.write_text(f"[worker] full transcript written to {tr}\n")
        self.state.write_text(json.dumps({"jobs": [{"id": "abc123def456", "status": "done", "exit_code": 0,
                              "task_kind": "research", "log_path": str(lg)}]}))
        self.assertIn("TRUNCATED by the worker", self.q("result", "abc123").stdout)
        tr.write_text("not json{{")   # fail-open: unreadable transcript must not crash result
        r = self.q("result", "abc123"); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("TRUNCATED by the worker", r.stdout)

    def test_run_spawns_detached_watchdog_and_dry_run_does_not(self):
        self.q("run", "--dry-run", "x")
        time.sleep(0.5); self.assertFalse((self.d / "watch.log").exists())
        self.q("run", "--no-watch", "x")
        time.sleep(0.5); self.assertFalse((self.d / "watch.log").exists())
        self.q("run", "--readonly", "x")
        self.assertTrue(self._wait_for(self.d / "watch.log"))
        self.assertEqual((self.d / "watch.log").read_text().strip(), "watch abc123def456")

    def _watch_setup(self, active):
        self.q("run", "--readonly", "--max-wall", "60", "--no-watch", "x")
        self.tf = self.argv()[self.argv().index("--task-file") + 1]
        self.state.write_text(json.dumps({"jobs": [{"id": "abc123def456", "status": "running",
                              "active_s": active, "task_file": self.tf}]}))

    def test_watch_aborts_over_cap_without_polling_status(self):
        self._watch_setup(61)
        r = subprocess.run([sys.executable, str(QAGENT), "watch", "abc123", "--poll", "0.1"],
                           capture_output=True, text=True, env=self.env, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("abc123", (self.d / "abort.out").read_text())

    def test_watch_leaves_under_cap_and_exits_when_terminal(self):
        self._watch_setup(10)
        p = subprocess.Popen([sys.executable, str(QAGENT), "watch", "abc123", "--poll", "0.1"], env=self.env)
        time.sleep(0.8); self.assertIsNone(p.poll())   # still guarding
        self.assertFalse((self.d / "abort.out").exists())
        self.state.write_text(json.dumps({"jobs": [{"id": "abc123def456", "status": "done"}]}))
        self.assertEqual(p.wait(timeout=10), 0)
        self.assertFalse((self.d / "abort.out").exists())

    def test_revert_proof_watchdog_bites(self):
        """A watcher with enforcement removed must NOT abort an over-cap job (so the real test discriminates)."""
        self._watch_setup(61)
        src = QAGENT.read_text()
        mut = src.replace("            if enforce_wall(j):\n                return 0", "            if False:\n                return 0")
        self.assertNotEqual(src, mut)
        mp = self.d / "qagent_mut"; mp.write_text(mut)
        p = subprocess.Popen([sys.executable, str(mp), "watch", "abc123", "--poll", "0.1", "--max-watch", "1"], env=self.env)
        self.assertEqual(p.wait(timeout=10), 2)
        self.assertFalse((self.d / "abort.out").exists())


if __name__ == "__main__":
    unittest.main()
