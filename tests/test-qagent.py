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

    def test_land_prints_manual_command_only(self):
        self.fix(extra=["--land"]); wt = self.wt(); (wt / "calc.py").write_text(self.FIXED); self.finish(wt)
        out = self.result()
        self.assertIn("no automated landing entry", out); self.assertIn("git -C", out)
        self.assertEqual(self.g("log", "--oneline").stdout.count("\n"), 1)   # no commit made on R

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


if __name__ == "__main__":
    unittest.main()
