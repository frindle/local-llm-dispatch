#!/usr/bin/env python3
"""test-qagent.py -- qagent against a STUB queue (no real model, no real queue).
Includes a revert-proof: mutating qagent's read-only guard makes the suite fail."""
import json, os, shutil, subprocess, sys, tempfile, time, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
QAGENT = HERE / "qagent"
STUB = """#!/usr/bin/env python3
import sys, json, os
open(os.environ['STUB_LOG'], 'a').write(json.dumps(sys.argv[1:]) + '\\n')
print('enqueued job abc123def456 (stub)')
"""


class Base(unittest.TestCase):
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


class T(Base):
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

    def test_readonly_carries_worker_marker_and_iters(self):
        self.q("run", "--readonly", "--label", "x", "investigate")
        a = self.argv()
        txt = Path(a[a.index("--task-file") + 1]).read_text()
        self.assertIn("Worker-flags: READONLY_JOB=1", txt.splitlines()[0])
        self.assertEqual(a[a.index("--max-iters") + 1], "40")
        self.assertIn("FINAL ANSWER", txt)
        self.q("run", "--readonly", "--max-iters", "7", "y")
        self.assertEqual(self.argv()[self.argv().index("--max-iters") + 1], "7")

    def test_non_readonly_has_no_marker_and_default_iters_12(self):
        self.q("run", "--label", "x", "do a thing")
        a = self.argv()
        self.assertNotIn("READONLY_JOB", Path(a[a.index("--task-file") + 1]).read_text())
        self.assertEqual(a[a.index("--max-iters") + 1], "12")

    def test_bounds_always_set_and_no_sampling(self):
        self.q("run", "--readonly", "x")
        a = self.argv()
        self.assertEqual(a[a.index("--max-tokens") + 1], "12000")
        self.assertEqual(a[a.index("--max-iters") + 1], "40")
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


class FixMode(Base):
    """qagent fix: isolated worktree, qagent-run verify before/after, test-file protection, abort cleanup."""
    def setUp(self):
        super().setUp()
        self.wts = self.d / "wts"
        self.env["QAGENT_WT_ROOT"] = str(self.wts)
        self.repo = self.d / "R"; self.repo.mkdir()
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        (self.repo / "test_calc.py").write_text(
            "from calc import add\nassert add(2, 3) == 5, 'add broken'\nprint('ok')\n")
        self.g("init", "-q"); self.g("add", "-A")
        self.g("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "init")
        self.verify = "python3 test_calc.py"

    def g(self, *x, cwd=None):
        return subprocess.run(["git", "-C", str(cwd or self.repo), *x], capture_output=True, text=True, check=True)

    def fix(self, verify=None, bin=None, extra=()):
        r = self.q("fix", "--repo", str(self.repo), "--verify", verify or self.verify, *extra, "add is wrong", bin=bin)
        return r

    def wt(self):
        w = list(self.wts.iterdir()) if self.wts.exists() else []
        return w[0] if w else None

    def finish(self, wt, status="done"):
        self.state.write_text(json.dumps({"jobs": [{"id": "abc123def456", "status": status, "exit_code": 0,
                              "task_kind": "coding", "cwd": str(wt)}]}))

    def result(self, bin=None):
        return self.q("result", "abc123", bin=bin).stdout

    FIXED = "def add(a, b):\n    return a + b\n"

    def test_fix_enqueue_shape_and_baseline(self):
        r = self.fix()
        self.assertEqual(r.stdout.strip(), "abc123def456", r.stderr)
        a = self.argv(); wt = self.wt()
        self.assertEqual(a[a.index("--cwd") + 1], str(wt))
        self.assertNotIn("--repo", a)
        self.assertEqual(a[a.index("--task-kind") + 1], "coding")
        self.assertEqual(a[a.index("--max-tokens") + 1], "12000")
        self.assertEqual(a[a.index("--max-iters") + 1], "30")
        self.assertEqual(a[a.index("--chat-timeout") + 1], "900")
        self.assertIn("--no-preflight", a); self.assertIn("--bundle", a)
        body = Path(a[a.index("--task-file") + 1]).read_text()
        self.assertIn("MUST NOT modify", body); self.assertIn(self.verify, body)
        self.assertEqual(self.g("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() in ("master", "main"), True)
        self.assertEqual(self.g("status", "--porcelain").stdout, "")   # R untouched
        self.assertTrue(wt.is_dir() and (wt / "calc.py").exists())
        self.assertFalse((wt / "__pycache__").exists())                # baseline litter cleaned

    def test_verify_already_passing_refused(self):
        r = self.fix("true")
        self.assertEqual(r.returncode, 3); self.assertIn("nothing to fix", r.stderr)
        self.assertFalse(self.log.exists()); self.assertIsNone(self.wt())

    def test_fail_to_pass_verified(self):
        self.fix(); wt = self.wt()
        (wt / "calc.py").write_text(self.FIXED); self.finish(wt)
        out = self.result()
        self.assertIn("verdict: VERIFIED", out); self.assertIn("before:  exit 1   after: exit 0", out)
        self.assertIn("calc.py", out); self.assertNotIn("test_calc.py\n", out.split("files touched")[1].split("full diff")[0])
        patch = (self.d / "home" / "fix" / "abc123def456.patch").read_text()
        self.assertIn("+    return a + b", patch); self.assertNotIn("__pycache__", patch)
        self.assertEqual(self.g("status", "--porcelain").stdout, "")   # R never touched
        self.g("apply", "--check", str(self.d / "home" / "fix" / "abc123def456.patch"))

    def test_still_failing_flagged_unverified(self):
        self.fix(); wt = self.wt()
        (wt / "calc.py").write_text("def add(a, b):\n    return a * b\n"); self.finish(wt)
        out = self.result()
        self.assertIn("verdict: UNVERIFIED", out); self.assertNotIn("verdict: VERIFIED", out)
        self.assertIn("after: exit 1", out)

    def test_no_change(self):
        self.fix(); self.finish(self.wt())
        self.assertIn("verdict: NO-CHANGE", self.result())

    def test_test_file_edit_rejected_even_if_verify_passes(self):
        self.fix(); wt = self.wt()
        (wt / "test_calc.py").write_text("print('ok')\n"); self.finish(wt)
        out = self.result()
        self.assertIn("verdict: REJECTED", out); self.assertIn("modifies test file: test_calc.py", out)
        self.assertIn("after: exit 0", out)    # verify went green -- by gutting the test; must not count

    def test_verify_target_edit_rejected(self):
        (self.repo / "check.sh").write_text("python3 -c 'from calc import add; assert add(1,1)==2'\n")
        self.g("add", "-A"); self.g("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "c")
        self.fix("sh check.sh"); wt = self.wt()
        (wt / "check.sh").write_text("true\n"); self.finish(wt)
        out = self.result()
        self.assertIn("verdict: REJECTED", out); self.assertIn("modifies verify target: check.sh", out)

    def test_symlink_escape_rejected(self):
        self.fix(); wt = self.wt()
        (wt / "calc.py").write_text(self.FIXED)
        os.symlink("/etc/hosts", wt / "leak"); self.finish(wt)
        out = self.result()
        self.assertIn("verdict: REJECTED", out); self.assertIn("symlink / escaping path: leak", out)

    def test_write_into_real_repo_rejected(self):
        self.fix(); wt = self.wt()
        (wt / "calc.py").write_text(self.FIXED)
        (self.repo / "calc.py").write_text(self.FIXED)    # worker escaped the worktree and edited R
        self.finish(wt)
        out = self.result()
        self.assertIn("verdict: REJECTED", out); self.assertIn("R's own checkout changed", out)

    def test_result_is_idempotent_and_survives_worktree_state(self):
        self.fix(); wt = self.wt(); (wt / "calc.py").write_text(self.FIXED); self.finish(wt)
        self.assertIn("VERIFIED", self.result())
        (wt / "calc.py").write_text("garbage")   # later tampering must not change the recorded report
        self.assertIn("verdict: VERIFIED", self.result())

    def test_result_points_to_qagent_land(self):
        self.fix(extra=["--land"]); wt = self.wt(); (wt / "calc.py").write_text(self.FIXED); self.finish(wt)
        out = self.result()
        self.assertIn("land it: qagent land abc123def456", out); self.assertNotIn("apply by hand", out)
        self.assertEqual(self.g("log", "--oneline").stdout.count("\n"), 1)   # result itself makes no commit on R

    def test_abort_removes_worktree_and_only_under_root(self):
        self.fix(); wt = self.wt(); self.assertTrue(wt.is_dir())
        r = self.q("abort", "abc123")
        self.assertIn("abc123", (self.d / "abort.out").read_text())
        self.assertFalse(wt.exists())
        self.assertNotIn(str(wt), self.g("worktree", "list").stdout)
        # tampered meta pointing outside the root must be refused
        self.fix(); meta = self.d / "home" / "fix" / "abc123def456.json"
        m = json.loads(meta.read_text()); victim = self.d / "victim"; victim.mkdir()
        m["worktree"] = str(victim); meta.write_text(json.dumps(m))
        r = self.q("abort", "abc123")
        self.assertEqual(r.returncode, 1); self.assertTrue(victim.is_dir()); self.assertIn("refusing", r.stdout)

    # ---- mutant / revert proofs: the real tests above must be able to FAIL
    def _mutant(self, old, new):
        src = QAGENT.read_text(); self.assertIn(old, src)
        mp = self.d / "qagent_mut"; mp.write_text(src.replace(old, new)); return mp

    def test_revert_proof_verify_before_after_logic_bites(self):
        # (1) baseline check removed -> an already-passing verify gets enqueued (real test expects exit 3)
        mp = self._mutant("        if code == 0:\n            cleanup()", "        if False:\n            cleanup()")
        self.assertEqual(self.fix("true", bin=mp).returncode, 0)
        # (2) after-verify ignored (treated as pass) -> a still-failing fix is called VERIFIED
        sub = self.d / "wts2"; self.env["QAGENT_WT_ROOT"] = str(sub); self.wts = sub
        shutil.rmtree(self.d / "home", ignore_errors=True)
        mp2 = self._mutant("if before != 0 and after == 0:", "if before != 0:")
        self.fix(); wt = self.wt(); (wt / "calc.py").write_text("def add(a, b):\n    return a * b\n"); self.finish(wt)
        self.assertIn("verdict: VERIFIED", self.result(bin=mp2))     # mutant wrongly accepts; real code says UNVERIFIED

    def test_revert_proof_test_file_protection_bites(self):
        # a test file NOT named by the verify command: only the TEST_RE guard can catch it
        (self.repo / "tests").mkdir(); (self.repo / "tests" / "test_other.py").write_text("x = 1\n")
        self.g("add", "-A"); self.g("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "t")
        self.fix(); wt = self.wt()
        (wt / "calc.py").write_text(self.FIXED); (wt / "tests" / "test_other.py").write_text("x = 2\n"); self.finish(wt)
        mp = self._mutant("        if TEST_RE.search(f):", "        if False:")
        self.assertIn("verdict: VERIFIED", self.result(bin=mp))    # protection removed -> test edit accepted
        meta = self.d / "home" / "fix" / "abc123def456.json"
        m = json.loads(meta.read_text()); m.pop("report"); meta.write_text(json.dumps(m))
        out = self.result()                                         # real code
        self.assertIn("verdict: REJECTED", out); self.assertIn("modifies test file: tests/test_other.py", out)

    def test_revert_proof_verify_target_protection_bites(self):
        (self.repo / "check.sh").write_text("python3 -c 'from calc import add; assert add(1,1)==2'\n")
        self.g("add", "-A"); self.g("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "c")
        self.fix("sh check.sh"); wt = self.wt(); (wt / "check.sh").write_text("true\n"); self.finish(wt)
        mp = self._mutant("        elif any(f == p or", "        elif False and any(f == p or")
        self.assertIn("verdict: VERIFIED", self.result(bin=mp))


RSTUB = """#!/usr/bin/env python3
# stub queue that also plays the model: attempt k (1-based, from the log) applies behaviour SCRIPT[k-1]
import sys, json, os
log = os.environ['STUB_LOG']
open(log, 'a').write(json.dumps(sys.argv[1:]) + '\\n')
k = len(open(log).read().splitlines())
a = sys.argv[1:]; cwd = a[a.index('--cwd') + 1]
beh = json.loads(os.environ['STUB_SCRIPT'])[min(k, len(json.loads(os.environ['STUB_SCRIPT']))) - 1]
if beh == 'fix':
    open(cwd + '/calc.py', 'w').write('def add(a, b):\\n    return a + b\\n')
elif beh == 'wrong':
    open(cwd + '/calc.py', 'w').write('def add(a, b):\\n    return a * b  # WRONGTRY%d\\n' % k)
    open(cwd + '/calc.py', 'a').write('print("AFTERMARK%d")\\n' % k)
jid = '%012x' % (0xabc000000000 + k)
st = os.environ['QAGENT_STATE']
try: d = json.load(open(st))
except Exception: d = {'jobs': []}
d['jobs'].append({'id': jid, 'status': 'done', 'exit_code': 0, 'task_kind': 'coding', 'cwd': cwd})
json.dump(d, open(st, 'w'))
print('enqueued job ' + jid)
"""


class Attempts(Base):
    """qagent fix --attempts N: verify-gated retry loop against a stub queue that plays the model."""
    g = FixMode.g; _mutant = FixMode._mutant; wt = FixMode.wt   # shared helpers (not the FixMode tests)

    def setUp(self):
        Base.setUp(self)
        self.wts = self.d / "wts"
        self.env["QAGENT_WT_ROOT"] = str(self.wts)
        self.repo = self.d / "R"; self.repo.mkdir()
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        (self.repo / "test_calc.py").write_text(
            "from calc import add\nassert add(2, 3) == 5, 'add broken'\nprint('ok')\n")
        self.g("init", "-q"); self.g("add", "-A")
        self.g("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "init")
        self.verify = "python3 test_calc.py"
        (self.d / "stubq.py").write_text(RSTUB)

    def run_fix(self, script, n, bin=None, extra=()):
        self.env["STUB_SCRIPT"] = json.dumps(script)
        return self.q("fix", "--repo", str(self.repo), "--verify", self.verify, "--wait", "--attempts", str(n),
                      *extra, "add is wrong", bin=bin)

    def calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def prompts(self):
        return [Path(c[c.index("--task-file") + 1]).read_text() for c in self.calls()]

    def cwds(self):
        return [c[c.index("--cwd") + 1] for c in self.calls()]

    def test_n1_unchanged(self):
        r = self.run_fix(["wrong"], 1, extra=["--wait-timeout", "30"])
        self.assertEqual(len(self.calls()), 1)
        self.assertNotIn("attempt", self.prompts()[0].lower().replace("attempts", ""))
        self.assertIn("verdict: UNVERIFIED", r.stdout)
        self.assertNotIn("ATTEMPT GROUP", r.stdout); self.assertNotIn("UNFIXED", r.stdout)
        self.assertEqual(list((self.d / "home" / "fix").glob("*.group.json")), [])
        self.assertEqual(list((self.d / "home" / "fix").glob("*.escalation.md")), [])
        self.assertNotIn("group", json.loads((self.d / "home" / "fix" / "abc000000001.json").read_text()))

    def test_attempts_requires_wait(self):
        r = self.q("fix", "--repo", str(self.repo), "--verify", self.verify, "--attempts", "2", "add is wrong")
        self.assertNotEqual(r.returncode, 0); self.assertFalse(self.log.exists())

    def test_retry_then_verified_stops(self):
        r = self.run_fix(["wrong", "fix", "wrong"], 3)
        self.assertEqual(len(self.calls()), 2, r.stdout + r.stderr)      # stopped at first VERIFIED
        self.assertIn("verdict: VERIFIED", r.stdout)
        self.assertIn("ATTEMPT GROUP", r.stdout)
        self.assertNotIn("UNFIXED", r.stdout)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(list((self.d / "home" / "fix").glob("*.escalation.md")), [])
        # any attempt's result shows the group summary
        out = self.q("result", "abc000000001").stdout
        self.assertIn("ATTEMPT GROUP", out); self.assertIn("abc000000002", out)

    def test_exhausted_writes_escalation(self):
        r = self.run_fix(["wrong", "wrong"], 2)
        self.assertEqual(len(self.calls()), 2)
        self.assertIn("UNFIXED after 2 attempts -- escalate", r.stdout)
        self.assertNotEqual(r.returncode, 0)
        esc = list((self.d / "home" / "fix").glob("*.escalation.md"))
        self.assertEqual(len(esc), 1); self.assertIn(str(esc[0]), r.stdout)
        t = esc[0].read_text()
        for s in ("add is wrong", str(self.repo), self.g("rev-parse", "HEAD").stdout.strip(), "abc000000001",
                  "abc000000002", "UNVERIFIED", "add broken", "abc000000001.patch", "abc000000002.patch"):
            self.assertIn(s, t)

    def test_fresh_worktree_each_attempt(self):
        self.run_fix(["wrong", "wrong", "wrong"], 3)
        cw = self.cwds()
        self.assertEqual(len(cw), 3); self.assertEqual(len(set(cw)), 3)
        # attempt 2 started from the base, not from attempt 1's edit (its WRONGTRY1 edit is absent at enqueue time)
        self.assertIn("WRONGTRY2", (Path(cw[1]) / "calc.py").read_text())
        self.assertNotIn("WRONGTRY1", (Path(cw[1]) / "calc.py").read_text())
        self.assertIn("WRONGTRY1", (Path(cw[0]) / "calc.py").read_text())

    def test_previous_verify_tail_in_next_prompt(self):
        self.run_fix(["wrong", "fix"], 2)
        p = self.prompts()
        self.assertNotIn("attempt 2 of 2", p[0])
        self.assertIn("attempt 2 of 2", p[1])
        self.assertIn("AFTERMARK1", p[1].split("attempt 2 of 2", 1)[1])   # raw after-verify output of attempt 1
        self.assertNotIn("AFTERMARK1", p[0])
        self.assertIn("add is wrong", p[1])                               # same problem text

    # ---- mutant proofs for the retry loop
    def _mut_run(self, old, new, script, n):
        mp = self._mutant(old, new)
        return self.run_fix(script, n, bin=mp)

    def test_mutant_reuse_worktree_caught(self):
        # mutant: every attempt reuses the first worktree path
        mp = self._mutant('    wt = WT_ROOT / fixid\n    if a.dry_run:', '    wt = WT_ROOT / (getattr(a, "_w", None) or fixid); a._w = wt.name\n    if a.dry_run:')
        self.run_fix(["wrong", "wrong"], 2, bin=mp)
        self.assertLess(len(set(self.cwds())), 2)          # mutant really reuses; the fresh-tree test would fail

    def test_mutant_skip_tail_caught(self):
        mp = self._mutant("{(r.get('after_tail') or '')[-1500:]}", "")
        self.run_fix(["wrong", "fix"], 2, bin=mp)
        self.assertNotIn("AFTERMARK1", self.prompts()[1])

    def test_mutant_no_stop_at_verified_caught(self):
        mp = self._mutant('        if r["verdict"] == "VERIFIED":\n            x = type', '        if False:\n            x = type')
        self.run_fix(["fix", "wrong", "wrong"], 3, bin=mp)
        self.assertEqual(len(self.calls()), 3)             # mutant keeps going; stop test expects 2 calls


class LandMode(Base):
    """qagent land: fresh worktree at the current tip, 3-way apply, verify + repo suite, commit only the patch."""
    g = FixMode.g; _mutant = FixMode._mutant; wt = FixMode.wt; finish = FixMode.finish; FIXED = FixMode.FIXED

    def setUp(self):
        super().setUp()
        self.env.update(GIT_AUTHOR_NAME="a", GIT_AUTHOR_EMAIL="a@b", GIT_COMMITTER_NAME="a", GIT_COMMITTER_EMAIL="a@b")
        self.wts = self.d / "wts"; self.env["QAGENT_WT_ROOT"] = str(self.wts)
        self.repo = self.d / "R"; self.repo.mkdir()
        self.bare = self.d / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.bare)], check=True, env=self.env)
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a - b\n\n\n# tail\n")
        (self.repo / "test_calc.py").write_text("from calc import add\nassert add(2, 3) == 5, 'add broken'\nprint('ok')\n")
        (self.repo / "suite.py").write_text("print('suite ok')\n")
        (self.repo / "package.json").write_text('{"scripts": {"test": "python3 suite.py"}}\n')
        self.g("init", "-q", "-b", "main"); self.g("add", "-A"); self.g("commit", "-qm", "init")
        self.g("remote", "add", "origin", str(self.bare)); self.g("push", "-q", "-u", "origin", "main")
        self.base = self.g("rev-parse", "HEAD").stdout.strip()
        self.verify = "python3 test_calc.py"

    def q(self, *a, bin=None):
        return subprocess.run([sys.executable, str(bin or QAGENT), *a], capture_output=True, text=True, env=self.env)

    def g(self, *x, cwd=None):
        return subprocess.run(["git", "-C", str(cwd or self.repo), *x], capture_output=True, text=True, check=True, env=self.env)

    def mkfix(self, verify=None, content=None):
        r = self.q("fix", "--repo", str(self.repo), "--verify", verify or self.verify, "add is wrong")
        self.assertEqual(r.stdout.strip(), "abc123def456", r.stderr)
        wt = self.wt(); (wt / "calc.py").write_text(content or self.FIXED); self.finish(wt)
        return self.q("result", "abc123").stdout

    def mkharness_fix(self):
        (self.repo / "verify.sh").write_text("python3 test_calc.py\n")          # untracked: NOT in HEAD
        r = self.q("fix", "--repo", str(self.repo), "--verify", "sh verify.sh", "--harness-from", str(self.repo), "add is wrong")
        self.assertEqual(r.stdout.strip(), "abc123def456", r.stderr)
        wt = self.wt(); (wt / "calc.py").write_text(self.FIXED); self.finish(wt)
        return self.q("result", "abc123").stdout

    def land(self, *a, bin=None):
        return self.q("land", "abc123", *a, bin=bin)

    def bare_git(self, *x):
        return subprocess.run(["git", "-C", str(self.bare), *x], capture_output=True, text=True).stdout.strip()

    def bare_head(self):
        return self.bare_git("rev-parse", "main")

    def drift(self, fname, text, msg="drift", push=True):
        (self.repo / fname).write_text(text); self.g("add", "-A"); self.g("commit", "-qm", msg)
        if push:
            self.g("push", "-q", "origin", "main")

    def no_leftovers(self):
        left = [p for p in self.wts.iterdir() if p.name.startswith("land-")] if self.wts.exists() else []
        self.assertEqual(left, [])
        self.assertEqual(self.g("worktree", "list").stdout.count("\n"), 2)   # R + the (still existing) fix worktree

    def test_clean_land_pushes_and_fast_forwards(self):
        self.mkfix(); r = self.land()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertRegex(r.stdout, r"LANDED [0-9a-f]{40} on main \(pushed\)")
        sha = r.stdout.strip().splitlines()[-1].split()[1]
        self.assertEqual(self.bare_head(), sha); self.assertEqual(self.g("rev-parse", "HEAD").stdout.strip(), sha)
        self.assertIn("return a + b", (self.repo / "calc.py").read_text())
        self.assertEqual(self.g("status", "--porcelain").stdout, ""); self.no_leftovers()
        self.assertEqual(self.g("show", "--name-only", "--format=", "HEAD").stdout.split(), ["calc.py"])

    def test_land_after_head_drifted_past_base(self):
        self.mkfix()
        self.drift("README.md", "hello\n")
        self.drift("calc.py", "# header\ndef add(a, b):\n    return a - b\n\n\n# tail\n")
        r = self.land()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        txt = (self.repo / "calc.py").read_text()
        self.assertIn("# header", txt); self.assertIn("return a + b", txt)       # 3-way merged both
        self.assertEqual(self.bare_head(), self.g("rev-parse", "HEAD").stdout.strip())

    def test_conflict_aborts_cleanly(self):
        self.mkfix(); self.drift("calc.py", "def add(a, b):\n    return a * b\n\n\n# tail\n")
        head = self.g("rev-parse", "HEAD").stdout.strip(); rem = self.bare_head()
        r = self.land()
        self.assertNotEqual(r.returncode, 0); self.assertIn("calc.py", r.stderr); self.assertIn("abc123def456", r.stderr)
        self.assertNotIn("LANDED", r.stdout)
        self.no_leftovers()
        self.assertEqual(self.g("rev-parse", "HEAD").stdout.strip(), head); self.assertEqual(self.bare_head(), rem)
        self.assertEqual(self.g("status", "--porcelain").stdout, "")
        self.assertIsNone(json.loads((self.d / "home/fix/abc123def456.json").read_text()).get("landed"))

    def test_failing_verify_aborts(self):
        self.mkfix()   # drift makes the verify fail even with the fix applied
        self.drift("test_calc.py", "from calc import add\nassert add(2, 3) == 5\nassert add(1, 1) == 3, 'driftboom'\n")
        rem = self.bare_head(); r = self.land()
        self.assertNotEqual(r.returncode, 0); self.assertIn("verify FAILED", r.stderr); self.assertIn("driftboom", r.stderr)
        self.assertEqual(self.bare_head(), rem); self.no_leftovers()

    def test_failing_repo_suite_aborts(self):
        self.mkfix(); self.drift("suite.py", "import sys\nprint('SUITEBOOM'); sys.exit(1)\n")
        rem = self.bare_head(); r = self.land()
        self.assertNotEqual(r.returncode, 0); self.assertIn("repo suite", r.stderr); self.assertIn("SUITEBOOM", r.stderr)
        self.assertEqual(self.bare_head(), rem); self.no_leftovers()

    def test_harness_missing_at_base_explained_then_harness_from(self):
        (self.repo / "verify.sh").write_text("python3 test_calc.py\n")
        r = self.q("fix", "--repo", str(self.repo), "--verify", "sh verify.sh", "add is wrong")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("verify.sh", r.stderr); self.assertIn("--harness-from", r.stderr)
        self.assertFalse(self.log.exists())
        self.assertEqual(list(self.wts.iterdir()) if self.wts.exists() else [], [])
        out = self.mkharness_fix()
        self.assertIn("verdict: VERIFIED", out)
        self.assertNotIn("verify.sh", (self.d / "home/fix/abc123def456.patch").read_text())   # harness never in the patch

    def test_harness_not_committed(self):
        self.mkharness_fix()
        r = self.land()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)      # verify.sh WAS available to the landing verify
        self.assertEqual(self.bare_git("show", "--name-only", "--format=", "main").split(), ["calc.py"])
        self.assertNotIn("verify.sh", self.bare_git("ls-tree", "-r", "--name-only", "main"))

    def test_dirty_patched_file_not_clobbered_remote_updated(self):
        self.mkfix(); rem = self.bare_head()
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a - b\n\n\n# tail\n# MY UNCOMMITTED WORK\n")
        head = self.g("rev-parse", "HEAD").stdout.strip()
        r = self.land()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertNotEqual(self.bare_head(), rem)                                   # remote updated
        self.assertIn("MY UNCOMMITTED WORK", (self.repo / "calc.py").read_text())    # not clobbered
        self.assertEqual(self.g("rev-parse", "HEAD").stdout.strip(), head)
        self.assertIn("pull --ff-only", r.stdout); self.assertIn("(pushed)", r.stdout)
        self.assertEqual(self.g("stash", "list").stdout, "")

    def test_idempotent_reland(self):
        self.mkfix(); r1 = self.land(); r2 = self.land()
        self.assertEqual(r2.returncode, 0)
        line = [l for l in r1.stdout.splitlines() if l.startswith("LANDED")][0]
        self.assertIn(line, r2.stdout)
        self.assertEqual(self.g("log", "--oneline").stdout.count("\n"), 2)
        self.assertEqual(json.loads((self.d / "home/fix/abc123def456.json").read_text())["landed"]["sha"], line.split()[1])

    def test_commit_message_no_attribution(self):
        self.mkfix(); self.land()
        msg = self.g("log", "-1", "--format=%B").stdout
        self.assertEqual(msg.strip(), "add is wrong")
        for bad in ("Co-Authored", "Claude", "claude.ai", "Generated", "session"):
            self.assertNotIn(bad, msg)

    def test_unverified_job_refused(self):
        self.mkfix(content="def add(a, b):\n    return a * b\n\n\n# tail\n"); rem = self.bare_head()
        r = self.land()
        self.assertNotEqual(r.returncode, 0); self.assertIn("UNVERIFIED", r.stderr); self.assertIn("refusing", r.stderr)
        self.assertEqual(self.bare_head(), rem)

    def test_no_push_is_local_only(self):
        self.mkfix(); rem = self.bare_head(); r = self.land("--no-push")
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("(local)", r.stdout)
        self.assertEqual(self.bare_head(), rem)
        self.assertEqual(self.g("log", "--oneline").stdout.count("\n"), 2)

    def test_remote_moved_lands_on_new_tip(self):
        self.mkfix()
        c = self.d / "clone"; subprocess.run(["git", "clone", "-q", str(self.bare), str(c)], check=True, env=self.env)
        (c / "other.txt").write_text("x\n"); self.g("add", "-A", cwd=c); self.g("commit", "-qm", "other", cwd=c)
        self.g("push", "-q", "origin", "main", cwd=c)
        r = self.land(); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.g("log", "--oneline").stdout.count("\n"), 3)   # init, other, fix (linear)

    # ---- mutant proofs: the gates must be able to FAIL (mutant lands where the real code refuses)
    def test_mutant_verify_gate_bites(self):
        self.mkfix(); self.drift("test_calc.py", "from calc import add\nassert add(1, 1) == 3, 'drift'\n")
        mp = self._mutant("    if vc != 0:\n        return", "    if False:\n        return")
        self.assertEqual(self.land(bin=mp).returncode, 0)      # real code: nonzero (test_failing_verify_aborts)

    def test_mutant_suite_gate_bites(self):
        self.mkfix(); self.drift("suite.py", "import sys; sys.exit(1)\n")
        mp = self._mutant("        if sc != 0:\n", "        if False:\n")
        self.assertEqual(self.land(bin=mp).returncode, 0)

    def test_mutant_harness_committed_bites(self):
        self.mkharness_fix()
        mp = self._mutant('    for f in files:\n        git(lw, "add", "-A", "--", f)', '    git(lw, "add", "-A")')
        src = mp.read_text().replace("    if extra:\n        return", "    if False:\n        return"); mp.write_text(src)
        self.assertEqual(self.land(bin=mp).returncode, 0)
        self.assertIn("verify.sh", self.bare_git("show", "--name-only", "--format=", "main").split())   # real code: not

    def test_mutant_unverified_gate_bites(self):
        self.mkfix(content="def add(a, b):\n    return a * b\n\n\n# tail\n")
        mp = self._mutant('    if v != "VERIFIED":\n        return None', '    if False:\n        return None')
        self.assertEqual(self.land(bin=mp).returncode, 1)      # still refused downstream by verify, but via a different reason
        self.assertNotIn("refusing to land", self.land(bin=mp).stderr)


class LandAuto(Attempts):
    """qagent fix --land --wait lands automatically after VERIFIED."""
    def test_fix_land_wait_lands(self):
        self.env.update(GIT_AUTHOR_NAME="a", GIT_AUTHOR_EMAIL="a@b", GIT_COMMITTER_NAME="a", GIT_COMMITTER_EMAIL="a@b")
        bare = self.d / "o.git"; subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        br = self.g("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.g("remote", "add", "origin", str(bare)); self.g("push", "-q", "-u", "origin", br)
        r = self.run_fix(["fix"], 1, extra=("--land",))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"LANDED [0-9a-f]{40} on " + br + r" \(pushed\)")
        self.assertEqual(self.g("log", "-1", "--format=%s").stdout.strip(), "add is wrong")


class IncidentGuards(LandMode):
    """2026-10-11 incident (job 2378208dac52): verify.sh called a helper that --harness-from did not copy, the baseline
    'failed' only because the file was missing, the model wrote the helper itself, and `land` shipped it.
    Every guard added for that has a mutant proof here."""

    def sh(self, name, text):
        p = self.d / name; p.write_text(text); return p

    def harness_dir(self, files, git_init=False, exclude=""):
        h = self.d / "H"; shutil.rmtree(h, ignore_errors=True); h.mkdir()
        for n, t in files.items():
            (h / n).parent.mkdir(parents=True, exist_ok=True)
            (h / n).write_text(t)
        if git_init:
            subprocess.run(["git", "init", "-q", str(h)], check=True)
            (h / ".git" / "info").mkdir(exist_ok=True); (h / ".git" / "info" / "exclude").write_text(exclude)
        return h

    VSH = "python3 check_literals.py && python3 test_calc.py\n"

    def fixp(self, verify, problem="add is wrong", extra=(), bin=None):
        return self.q("fix", "--repo", str(self.repo), "--verify", verify, *extra, problem, bin=bin)

    def meta(self):
        return json.loads((self.d / "home/fix/abc123def456.json").read_text())

    def finish_with(self, files, problem="add is wrong", verify="bash verify.sh", hdir=None, extra=()):
        h = hdir or self.harness_dir({"verify.sh": self.VSH, "check_literals.py": "print('lit ok')\n"})
        r = self.fixp(verify, problem, ("--harness-from", str(h), *extra))
        self.assertEqual(r.stdout.strip(), "abc123def456", r.stderr)
        wt = self.wt()
        for n, t in files.items():
            if t is None:
                (wt / n).unlink()
            else:
                (wt / n).write_text(t)
        self.finish(wt)
        return self.q("result", "abc123").stdout

    def no_wts_left(self):
        self.assertEqual(list(self.wts.iterdir()) if self.wts.exists() else [], [])

    # ---- 1. baseline validity
    def test_incident_missing_helper_is_invalid_baseline(self):
        h = self.harness_dir({"verify.sh": self.VSH})            # helper NOT in the harness dir (the incident)
        r = self.fixp("bash verify.sh", extra=("--harness-from", str(h)))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("INVALID BASELINE", r.stderr); self.assertIn("check_literals.py", r.stderr)
        self.assertIn("--harness-from", r.stderr)
        self.assertFalse(self.log.exists()); self.no_wts_left()

    def test_invalid_baseline_command_not_found_and_enoent_text(self):
        r = self.fixp("nonexistent_cmd_xyz_1")
        self.assertIn("INVALID BASELINE", r.stderr); self.assertFalse(self.log.exists()); self.no_wts_left()
        r = self.fixp("echo 'Error: ENOENT: no such file' >&2; exit 1")
        self.assertIn("INVALID BASELINE", r.stderr); self.assertFalse(self.log.exists())
        r = self.fixp("python3 -c \"import sys; sys.exit('real failing check')\"")   # a REAL failure still enqueues
        self.assertEqual(r.stdout.strip(), "abc123def456", r.stderr)

    def test_mutant_baseline_guard_bites(self):
        h = self.harness_dir({"verify.sh": self.VSH})
        mp = self._mutant("        inv = invalid_baseline(", "        inv = None and invalid_baseline(")
        r = self.fixp("bash verify.sh", extra=("--harness-from", str(h)), bin=mp)
        self.assertEqual(r.returncode, 0, r.stderr)               # mutant enqueues the broken-baseline fix
        self.assertTrue(self.log.exists())

    # ---- 2. complete harness
    def test_harness_from_copies_complete_harness(self):
        h = self.harness_dir({"verify.sh": self.VSH, "check_literals.py": "print('lit ok')\n", "fixture.json": "{}\n"},
                             git_init=True, exclude="check_literals.py\n")     # helper is git-IGNORED: only ref-scan finds it
        out = self.finish_with({"calc.py": self.FIXED}, hdir=h)
        m = self.meta()
        self.assertEqual(sorted(m["harness"]), ["check_literals.py", "fixture.json", "verify.sh"])
        self.assertEqual(sorted(m["protected"]), ["check_literals.py", "fixture.json", "verify.sh"])
        self.assertIn("verdict: VERIFIED", out)
        patch = (self.d / "home/fix/abc123def456.patch").read_text()
        for n in ("verify.sh", "check_literals.py", "fixture.json"):
            self.assertNotIn(n, patch)
        r = self.land()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.bare_git("show", "--name-only", "--format=", "main").split(), ["calc.py"])

    def test_mutants_complete_harness_bites(self):
        h = self.harness_dir({"verify.sh": self.VSH, "check_literals.py": "print('lit ok')\n", "fixture.json": "{}\n"},
                             git_init=True, exclude="check_literals.py\n")
        mp = self._mutant("cands + referenced_names(cmd, [src, wt])", "cands")
        r = self.fixp("bash verify.sh", extra=("--harness-from", str(h)), bin=mp)
        self.assertIn("INVALID BASELINE", r.stderr)                # ref-scan removed: helper not copied -> caught by guard 1
        mp2 = self._mutant('cands += [x for x in git(src, "ls-files", "-o", "--exclude-standard", "-z").stdout.split("\\0") if x]', "pass")
        r = self.fixp("bash verify.sh", extra=("--harness-from", str(h)), bin=mp2)
        self.assertEqual(r.stdout.strip(), "abc123def456", r.stderr)
        self.assertNotIn("fixture.json", self.meta()["harness"])   # untracked-walk removed: fixture.json missing (real: copied)

    # ---- 2b. harness COMMITTED in the source dir (dispatch worktree seal commit), absent at the fix base
    def committed_harness(self, files):
        h = self.harness_dir(files, git_init=True)
        env = dict(self.env, GIT_AUTHOR_NAME="a", GIT_AUTHOR_EMAIL="a@b", GIT_COMMITTER_NAME="a", GIT_COMMITTER_EMAIL="a@b")
        subprocess.run(["git", "-C", str(h), "add", "-A"], check=True, env=env)
        subprocess.run(["git", "-C", str(h), "commit", "-qm", "seal"], check=True, env=env)
        return h

    CVSH = "python3 check_literals.py && python3 test_calc.py\n"
    CHECK = "print(open('TASK.md').read().strip())\n"

    def committed_files(self):
        return {"verify.sh": self.CVSH, "check_literals.py": self.CHECK, "TASK.md": "task\n",
                "_nd/nonet.cjs": "// x\n", "calc.py": "HARNESS COPY OF TARGET\n"}

    def test_harness_from_copies_committed_harness_absent_at_base(self):
        h = self.committed_harness(self.committed_files())
        out = self.finish_with({"calc.py": self.FIXED}, hdir=h)
        m = self.meta()
        for n in ("TASK.md", "check_literals.py", "verify.sh", "_nd/nonet.cjs"):
            self.assertIn(n, m["harness"]); self.assertIn(n, m["protected"])
        self.assertNotIn("calc.py", m["harness"])                  # exists at base: never copied/overwritten
        self.assertIn("verdict: VERIFIED", out)
        self.assertNotIn("HARNESS COPY", (self.d / "home/fix/abc123def456.patch").read_text())

    def test_harness_from_does_not_copy_node_modules_or_build_dirs(self):
        f = self.committed_files(); f["node_modules/x/index.js"] = "1\n"; f["dist/out.js"] = "1\n"
        (self.d / "H").mkdir(exist_ok=True)
        h = self.harness_dir({"verify.sh": self.CVSH, "check_literals.py": self.CHECK, "TASK.md": "t\n"}, git_init=True)
        for n in ("node_modules/x/index.js", "dist/out.js"):
            (h / n).parent.mkdir(parents=True, exist_ok=True); (h / n).write_text("1\n")
        env = dict(self.env, GIT_AUTHOR_NAME="a", GIT_AUTHOR_EMAIL="a@b", GIT_COMMITTER_NAME="a", GIT_COMMITTER_EMAIL="a@b")
        subprocess.run(["git", "-C", str(h), "add", "-Af"], check=True, env=env)
        subprocess.run(["git", "-C", str(h), "commit", "-qm", "seal"], check=True, env=env)
        self.finish_with({"calc.py": self.FIXED}, hdir=h)
        self.assertEqual(sorted(self.meta()["harness"]), ["TASK.md", "check_literals.py", "verify.sh"])

    def test_mutant_committed_harness_copy_bites(self):
        h = self.committed_harness(self.committed_files())
        mp = self._mutant("        if base:\n            cands +=", "        if False:\n            cands +=")
        r = self.fixp("bash verify.sh", extra=("--harness-from", str(h)), bin=mp)
        self.assertIn("INVALID BASELINE", r.stderr)               # committed harness not copied -> the reported bug

    # ---- 2c. 'Missing:' lists only real paths
    PROSE_VSH = ("# Next.js app; see lib/X.test.ts and migration.sql for background\n"
                 "echo 'built with Next.js, lib/X.test.ts' > /dev/null\n"
                 "python3 missing_helper.py\n")

    def test_invalid_baseline_missing_list_ignores_prose_and_placeholders(self):
        h = self.harness_dir({"verify.sh": self.PROSE_VSH})
        r = self.fixp("bash verify.sh", extra=("--harness-from", str(h)))
        self.assertIn("INVALID BASELINE", r.stderr)               # real missing file still aborts
        missing = [ln for ln in r.stderr.splitlines() if ln.startswith("Missing:")][0]
        self.assertIn("missing_helper.py", missing)
        for tok in ("Next.js", "lib/X.test.ts", "migration.sql"):
            self.assertNotIn(tok, missing)

    def test_mutants_missing_list_filters_bite(self):
        h = self.harness_dir({"verify.sh": self.PROSE_VSH})
        mp = self._mutant("referenced_names(verify, [wt], skip_comments=True)", "referenced_names(verify, [wt])")
        r = self.fixp("bash verify.sh", extra=("--harness-from", str(h)), bin=mp)
        self.assertIn("migration.sql", [ln for ln in r.stderr.splitlines() if ln.startswith("Missing:")][0])
        mp2 = self._mutant("{n for n in cand if _path_like(n) and not", "{n for n in cand if _ref_ok(n) and not")
        r = self.fixp("bash verify.sh", extra=("--harness-from", str(h)), bin=mp2)
        missing = [ln for ln in r.stderr.splitlines() if ln.startswith("Missing:")][0]
        self.assertTrue("Next.js" in missing or "lib/X.test.ts" in missing, missing)

    def test_model_created_protected_name_dropped_and_unverified(self):
        vsh = "python3 test_calc.py && { [ ! -f extra_check.py ] || python3 extra_check.py; }\n"
        h = self.harness_dir({"verify.sh": vsh})
        out = self.finish_with({"calc.py": self.FIXED, "extra_check.py": "print('model wrote me')\n"}, hdir=h)
        self.assertIn("verdict: UNVERIFIED", out); self.assertIn("extra_check.py", out)
        self.assertIn("protected; dropped from the patch", out)
        self.assertNotIn("extra_check.py", (self.d / "home/fix/abc123def456.patch").read_text())
        r = self.land(); self.assertNotEqual(r.returncode, 0); self.assertIn("UNVERIFIED", r.stderr)

    def test_model_modifying_or_deleting_harness_is_unverified(self):
        out = self.finish_with({"calc.py": self.FIXED, "verify.sh": "true\n"})
        self.assertIn("verdict: UNVERIFIED", out); self.assertIn("protected harness file verify.sh", out)
        self.assertNotIn("verify.sh", (self.d / "home/fix/abc123def456.patch").read_text())
        shutil.rmtree(self.d / "home"); shutil.rmtree(self.wts); self.g("worktree", "prune")
        out = self.finish_with({"calc.py": self.FIXED, "check_literals.py": None})
        self.assertIn("verdict: UNVERIFIED", out); self.assertIn("check_literals.py", out)

    def test_mutants_model_created_and_tamper_bite(self):
        vsh = "python3 test_calc.py && { [ ! -f extra_check.py ] || python3 extra_check.py; }\n"
        h = self.harness_dir({"verify.sh": vsh})
        self.finish_with({"calc.py": self.FIXED, "extra_check.py": "print('x')\n"}, hdir=h)
        mp = self._mutant("        if n not in hset and (Path(wt) / n).exists():", "        if False:")
        meta = self.d / "home/fix/abc123def456.json"
        m = json.loads(meta.read_text()); m.pop("report"); meta.write_text(json.dumps(m))
        self.assertIn("verdict: VERIFIED", self.q("result", "abc123", bin=mp).stdout)
        # tamper mutant
        shutil.rmtree(self.d / "home"); shutil.rmtree(self.wts); self.g("worktree", "prune")
        self.finish_with({"calc.py": self.FIXED, "verify.sh": "true\n"})
        meta = self.d / "home/fix/abc123def456.json"
        m = json.loads(meta.read_text()); m.pop("report"); meta.write_text(json.dumps(m))
        mp = self._mutant("        if file_sha(Path(wt) / h) != sha:", "        if False:")
        self.assertIn("verdict: VERIFIED", self.q("result", "abc123", bin=mp).stdout)

    # ---- 3. scope guard
    def test_parse_scope(self):
        import importlib.machinery, importlib.util
        ld = importlib.machinery.SourceFileLoader("qagent_mod", str(QAGENT))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("qagent_mod", ld)); ld.exec_module(mod)
        self.assertEqual(mod.parse_scope("Fix it. Only edit `app/api/import/route.ts`. Do not touch tests."), ["app/api/import/route.ts"])
        self.assertEqual(mod.parse_scope("only edit a.ts, b/c.ts"), ["a.ts", "b/c.ts"])
        self.assertEqual(mod.parse_scope("Only edit a.py and b.py"), ["a.py", "b.py"])
        self.assertEqual(mod.parse_scope("add is wrong"), [])
        self.assertEqual(mod.parse_scope("Only edit the handler please"), [])

    def test_scope_new_file_and_out_of_scope_unverified_and_land_refuses(self):
        out = self.finish_with({"calc.py": self.FIXED, "notes.txt": "hi\n"}, problem="add is wrong\nOnly edit `calc.py`")
        self.assertIn("verdict: UNVERIFIED", out); self.assertIn("notes.txt is outside the declared scope", out)
        r = self.land(); self.assertNotEqual(r.returncode, 0); self.assertIn("UNVERIFIED", r.stderr)
        shutil.rmtree(self.d / "home"); shutil.rmtree(self.wts); self.g("worktree", "prune")
        out = self.finish_with({"calc.py": self.FIXED, "suite.py": "print('suite ok!')\n"}, problem="add is wrong\nOnly edit calc.py")
        self.assertIn("verdict: UNVERIFIED", out); self.assertIn("suite.py is outside the declared scope", out)
        self.assertNotIn("calc.py is outside", out)
        shutil.rmtree(self.d / "home"); shutil.rmtree(self.wts); self.g("worktree", "prune")
        out = self.finish_with({"calc.py": self.FIXED, "notes.txt": "hi\n"}, problem="add is wrong\nOnly edit calc.py, notes.txt")
        self.assertIn("verdict: UNVERIFIED", out); self.assertIn("notes.txt is a NEW file", out)

    def test_scope_ok_still_verified_and_lands(self):
        out = self.finish_with({"calc.py": self.FIXED}, problem="add is wrong\nOnly edit calc.py, suite.py")
        self.assertIn("verdict: VERIFIED", out)
        self.assertEqual(self.land().returncode, 0)

    def _tamper_report(self, **kw):
        meta = self.d / "home/fix/abc123def456.json"; m = json.loads(meta.read_text())
        m["report"].update(kw); meta.write_text(json.dumps(m))

    def test_land_guard_scope_independent_of_recorded_verdict(self):
        self.finish_with({"calc.py": self.FIXED}, problem="add is wrong\nOnly edit calc.py")
        self._tamper_report(files=["M\tcalc.py", "A\tcheck_literals2.py"])     # old/forged VERIFIED report with an extra file
        rem = self.bare_head(); r = self.land()
        self.assertNotEqual(r.returncode, 0); self.assertIn("declared scope", r.stderr); self.assertIn("check_literals2.py", r.stderr)
        self.assertEqual(self.bare_head(), rem)
        mp = self._mutant("    if scope:\n        v = scope_violations(scope, files)", "    if False:\n        v = scope_violations(scope, files)")
        self.assertNotIn("declared scope", self.land(bin=mp).stderr)

    def test_mutant_finalize_scope_bites(self):
        self.finish_with({"calc.py": self.FIXED, "notes.txt": "hi\n"}, problem="add is wrong\nOnly edit calc.py")
        meta = self.d / "home/fix/abc123def456.json"
        m = json.loads(meta.read_text()); m.pop("report"); meta.write_text(json.dumps(m))
        mp = self._mutant("    if scope:\n        for x in scope_violations(scope, files):", "    if False:\n        for x in scope_violations(scope, files):")
        self.assertIn("verdict: VERIFIED", self.q("result", "abc123", bin=mp).stdout)

    def test_no_scope_new_files_only_refused_unless_allowed(self):
        r = self.fixp("python3 -c 'import newmod'")
        self.assertEqual(r.stdout.strip(), "abc123def456", r.stderr)
        wt = self.wt(); (wt / "newmod.py").write_text("x = 1\n"); self.finish(wt)
        self.assertIn("verdict: VERIFIED", self.q("result", "abc123").stdout)
        rem = self.bare_head(); r = self.land()
        self.assertNotEqual(r.returncode, 0); self.assertIn("only adds NEW files", r.stderr); self.assertIn("--allow-new-files", r.stderr)
        self.assertEqual(self.bare_head(), rem)
        mp = self._mutant("    if not scope and not allow_new and files and all(", "    if False and files and all(")
        mut = self.q("land", "abc123", bin=mp)
        self.assertNotIn("only adds NEW files", mut.stderr)
        r = self.q("land", "abc123", "--allow-new-files")
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("newmod.py", self.bare_git("ls-tree", "-r", "--name-only", "main"))

    # ---- 4. protected-name-only patches
    def test_land_refuses_patch_touching_only_harness_names(self):
        self.finish_with({"calc.py": self.FIXED})
        self._tamper_report(files=["A\tcheck_literals.py"])           # the incident shape
        r = self.q("land", "abc123", "--allow-new-files")
        self.assertNotEqual(r.returncode, 0); self.assertIn("protected harness", r.stderr)
        mp = self._mutant("    if files and all(f in names for _, f in files):", "    if False:")
        self.assertNotIn("protected harness", self.q("land", "abc123", "--allow-new-files", bin=mp).stderr)

    # ---- 5. unland
    def unl(self, *a, bin=None):
        return self.q("unland", "abc123", *a, bin=bin)

    def landed(self):
        self.mkfix(); r = self.land(); self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip().splitlines()[-1].split()[1]

    def test_unland_clean_reverts_pushes_no_attribution(self):
        sha = self.landed()
        r = self.unl(); self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertRegex(r.stdout, r"UNLANDED job abc123def456: " + sha + r" reverted by [0-9a-f]{40} on main \(pushed\)")
        head = self.bare_head(); self.assertEqual(self.g("rev-parse", "HEAD").stdout.strip(), head)
        self.assertEqual(self.g("rev-parse", "HEAD~1").stdout.strip(), sha)         # fast-forward, nothing rewritten
        self.assertIn("return a - b", (self.repo / "calc.py").read_text())
        msg = self.g("log", "-1", "--format=%B").stdout
        self.assertEqual(msg.splitlines()[0], 'Revert "add is wrong"')
        for bad in ("Co-Authored-By", "Claude", "Generated with", "claude.ai"):
            self.assertNotIn(bad, msg)
        self.assertEqual(self.g("status", "--porcelain").stdout, "")
        u = self.meta()["unlanded"]; self.assertEqual(u["sha"], head); self.assertEqual(u["branch"], "main")
        left = [p for p in self.wts.iterdir() if p.name.startswith("unland-")]; self.assertEqual(left, [])

    def test_unland_idempotent(self):
        self.landed(); self.unl(); head = self.bare_head()
        r = self.unl(); self.assertEqual(r.returncode, 0); self.assertIn("already unlanded", r.stdout)
        self.assertEqual(self.bare_head(), head)

    def test_unland_not_landed_refused(self):
        self.mkfix(); r = self.unl(); self.assertNotEqual(r.returncode, 0); self.assertIn("nothing to undo", r.stderr)

    def test_unland_after_drift_keeps_later_work(self):
        self.landed(); self.drift("README.md", "later work\n")
        r = self.unl(); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("return a - b", (self.repo / "calc.py").read_text()); self.assertEqual((self.repo / "README.md").read_text(), "later work\n")
        self.assertEqual(self.bare_head(), self.g("rev-parse", "HEAD").stdout.strip())

    def test_unland_conflict_aborts_cleanly(self):
        self.landed(); self.drift("calc.py", "def add(a, b):\n    return a + b  # touched later\n\n\n# tail\n")
        head = self.g("rev-parse", "HEAD").stdout.strip(); rem = self.bare_head()
        r = self.unl(); self.assertNotEqual(r.returncode, 0)
        self.assertIn("calc.py", r.stderr); self.assertIn("conflict", r.stderr)
        self.assertEqual(self.bare_head(), rem); self.assertEqual(self.g("rev-parse", "HEAD").stdout.strip(), head)
        self.assertEqual(self.g("status", "--porcelain").stdout, "")
        self.assertNotIn("unlanded", self.meta())
        self.assertEqual([p for p in self.wts.iterdir() if p.name.startswith("unland-")], [])

    def test_unland_already_reverted_by_hand_noop(self):
        sha = self.landed(); self.g("revert", "--no-edit", sha); self.g("push", "-q", "origin", "main")
        head = self.bare_head(); r = self.unl()
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("already reverted", r.stdout)
        self.assertEqual(self.bare_head(), head); self.assertIn("unlanded", self.meta())
        # mutant: without the already-reverted check a second revert is attempted (conflicts/fails)
        shutil.rmtree(self.d / "home" / "fix" / "x", ignore_errors=True)
        m = self.meta(); m.pop("unlanded"); (self.d / "home/fix/abc123def456.json").write_text(json.dumps(m))
        mp = self._mutant('    if rv:\n        return ("already", rv[0])', '    if False:\n        return ("already", rv[0])')
        self.assertNotEqual(self.unl(bin=mp).returncode, 0)

    def test_unland_needs_no_fix_worktree(self):
        self.landed(); wt = Path(self.meta()["worktree"])
        self.g("worktree", "remove", "--force", str(wt)); self.assertFalse(wt.exists())
        r = self.unl(); self.assertEqual(r.returncode, 0, r.stderr)

    def test_unland_dirty_checkout_not_clobbered(self):
        sha = self.landed(); rem0 = self.bare_head()
        (self.repo / "calc.py").write_text((self.repo / "calc.py").read_text() + "# MY UNCOMMITTED WORK\n")
        head = self.g("rev-parse", "HEAD").stdout.strip()
        r = self.unl(); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(self.bare_head(), rem0)                                   # remote reverted
        self.assertIn("MY UNCOMMITTED WORK", (self.repo / "calc.py").read_text())
        self.assertEqual(self.g("rev-parse", "HEAD").stdout.strip(), head)
        self.assertIn("pull --ff-only", r.stdout); self.assertEqual(self.g("stash", "list").stdout, "")

    def test_unland_no_push_local_only(self):
        self.mkfix(); r = self.land("--no-push"); self.assertEqual(r.returncode, 0, r.stderr)
        rem = self.bare_head(); r = self.unl("--no-push"); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.bare_head(), rem); self.assertIn("(local)", r.stdout)
        self.assertIn("return a - b", (self.repo / "calc.py").read_text())

    def test_unland_remote_moved_reverts_on_new_tip(self):
        self.landed()
        other = self.d / "other"; subprocess.run(["git", "clone", "-q", str(self.bare), str(other)], check=True, env=self.env)
        (other / "x.txt").write_text("x\n"); subprocess.run(["git", "-C", str(other), "add", "-A"], check=True, env=self.env)
        subprocess.run(["git", "-C", str(other), "commit", "-qm", "other"], check=True, env=self.env)
        subprocess.run(["git", "-C", str(other), "push", "-q", "origin", "main"], check=True, env=self.env)
        r = self.unl(); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("x.txt", self.bare_git("ls-tree", "-r", "--name-only", "main"))
        self.assertIn("return a - b", self.bare_git("show", "main:calc.py"))


for _n in dir(LandMode):                  # the inherited LandMode tests already ran in LandMode
    if _n.startswith("test_") and _n not in IncidentGuards.__dict__:
        setattr(IncidentGuards, _n, None)


if __name__ == "__main__":
    unittest.main()
