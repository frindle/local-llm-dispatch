#!/usr/bin/env python3
"""test-qagent.py -- qagent against a STUB queue (no real model, no real queue).
Includes a revert-proof: mutating qagent's read-only guard makes the suite fail."""
import json, os, subprocess, sys, tempfile, unittest
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


if __name__ == "__main__":
    unittest.main()
