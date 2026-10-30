#!/usr/bin/env python3
"""test-preflight-treelock.py -- ollama-dispatch-preflight serialises its
tree-mutating both-ways proof on <git-dir>/dispatch-tree.lock.

Soak seed 1 (2026-10-06): the slicer's preflight wrote the refimpl and the
relevance mutants into lib/calc.ts while an auto self-check (which holds the
tree lock) snapshotted the target as "the model's edits" and restored it after
preflight's revert -> the coding job launched on a mutant (dirty baseline,
gate UNTRUSTED). Every other in-place mutator took the lock; preflight did not.

Checks (behavioural, real CLI, temp repos only):
  1. while another process holds the tree lock, preflight's refimpl does NOT run
     until it is released
  2. the lock is released when preflight exits (a non-blocking flock succeeds)
  3. OLLAMA_DISPATCH_TREE_LOCK_HELD == this lock path (a parent holds it) -> no
     wait, no self-deadlock
  4. timeout -> proceeds anyway (fail-open, bounded)
  5. --review never takes the lock (read-only)

  --revert-check PRE_FIX_COPY : runs check 1 against the given pre-fix
  preflight and requires it to go RED.
"""
import fcntl
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PF = Path.home() / "bin" / "ollama-dispatch-preflight"
VERIFY = """#!/usr/bin/env bash
fails=0
if python3 -c "import target; assert target.f() == 'FIXED_MARKER'"; then :; else echo "FAIL: f() does not return FIXED_MARKER"; fails=$((fails+1)); fi
echo "--- $fails failed ---"
if [ "$fails" -eq 0 ]; then echo VERIFY_OK; else exit 1; fi
"""
# Phase 2 (2026-10-08) harness-complete gate: the toy harness must be a COMPLETE one (a real
# `## Must contain` literal, a scope line, a verify that drives the real module) or preflight
# NO-GOes before it ever runs the refimpl -- which made this lock test silently inert.
TASK = """# Task
Only edit `target.py`.

Goal: make `f()` in target.py return the string FIXED_MARKER.

## Must contain
- `FIXED_MARKER`

Done when: bash verify.sh prints VERIFY_OK (verify.sh imports target and asserts f() returns the string
FIXED_MARKER; it fails on the untouched baseline where f() returns the integer 1).
Do not: touch verify.sh
"""
# a refimpl so the task is shown satisfiable (the tests drive it through --refimpl-cmd)
REFIMPL = "import pathlib\npathlib.Path('target.py').write_text(\"def f():\\n    return 'FIXED_MARKER'\\n\")\n"


def git(wt, *a):
    return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True)


def make_repo(root):
    wt = Path(root) / "wt"
    wt.mkdir()
    (wt / "target.py").write_text("def f():\n    return 1\n")
    (wt / "verify.sh").write_text(VERIFY)
    (wt / "TASK.md").write_text(TASK)
    (wt / "refimpl.py").write_text(REFIMPL)
    git(wt, "init", "-q")
    git(wt, "config", "user.email", "t@example.com")
    git(wt, "config", "user.name", "t")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "baseline")
    lp = Path(git(wt, "rev-parse", "--absolute-git-dir").stdout.strip()) / "dispatch-tree.lock"
    return wt, lp


def run_pf(pf, wt, stamp, env_extra=None, review=False, timeout=120):
    env = dict(os.environ)
    env["HOME"] = str(Path(wt).parent / "home")      # journal etc. stay in the sandbox
    Path(env["HOME"]).mkdir(exist_ok=True)
    env.update(env_extra or {})
    if review:
        cmd = [sys.executable, str(pf), str(wt), "--review"]
    else:
        rc_cmd = (f"python3 -c \"import time;open('{stamp}','a').write(str(time.time())+'\\\\n')\" "
                  f"&& python3 refimpl.py")
        cmd = [sys.executable, str(pf), str(wt), "--refimpl-cmd", rc_cmd]
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)


def holder(lp, secs):
    """Child process that holds the tree lock for `secs`; returns (proc, release_ts_file)."""
    code = ("import fcntl,sys,time;fh=open(sys.argv[1],'a+');"
            "fcntl.flock(fh.fileno(),fcntl.LOCK_EX);print('held',flush=True);"
            "time.sleep(float(sys.argv[2]));t=time.time();fh.close();print(t,flush=True)")
    p = subprocess.Popen([sys.executable, "-c", code, str(lp), str(secs)],
                         stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def check_waits(pf, results, label="waits for a held tree lock"):
    with tempfile.TemporaryDirectory() as td:
        wt, lp = make_repo(td)
        stamp = Path(td) / "stamp"
        h = holder(lp, 4)
        r = run_pf(pf, wt, stamp, {"OLLAMA_PREFLIGHT_TREE_WAIT_S": "60"})
        released = float(h.stdout.readline().strip())
        h.wait()
        stamps = [float(x) for x in stamp.read_text().split()] if stamp.exists() else []
        ok = bool(stamps) and min(stamps) >= released - 0.05
        results.append((label, ok, f"refimpl ran at {stamps[:1]} vs lock released "
                                   f"{released:.2f}; rc={r.returncode}"))
        return ok


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--revert-check":
        res = []
        ok = check_waits(Path(sys.argv[2]), res)
        print(res[0])
        if ok:
            print("REVERT-CHECK FAILED: pre-fix copy still waits -- test is inert")
            return 1
        print("REVERT-CHECK OK: pre-fix copy goes RED")
        return 0

    results = []
    check_waits(PF, results)

    with tempfile.TemporaryDirectory() as td:
        wt, lp = make_repo(td)
        stamp = Path(td) / "stamp"
        run_pf(PF, wt, stamp)
        fh = open(lp, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            got = True
        except OSError:
            got = False
        fh.close()
        results.append(("lock released after exit", got, ""))
        results.append(("tree back at baseline after preflight",
                         git(wt, "status", "--porcelain", "--untracked-files=no").stdout.strip() == "",
                         git(wt, "status", "--porcelain").stdout.strip()))

    with tempfile.TemporaryDirectory() as td:
        wt, lp = make_repo(td)
        stamp = Path(td) / "stamp"
        h = holder(lp, 15)
        t0 = time.time()
        run_pf(PF, wt, stamp, {"OLLAMA_PREFLIGHT_TREE_WAIT_S": "60",
                               "OLLAMA_DISPATCH_TREE_LOCK_HELD": str(lp)})
        el = time.time() - t0
        h.kill(); h.wait()
        results.append(("parent-held marker -> no wait (no self-deadlock)",
                        el < 12 and stamp.exists(), f"{el:.1f}s"))

    with tempfile.TemporaryDirectory() as td:
        wt, lp = make_repo(td)
        stamp = Path(td) / "stamp"
        h = holder(lp, 30)
        t0 = time.time()
        r = run_pf(PF, wt, stamp, {"OLLAMA_PREFLIGHT_TREE_WAIT_S": "2"})
        el = time.time() - t0
        h.kill(); h.wait()
        results.append(("timeout -> bounded, proceeds anyway",
                        el < 25 and stamp.exists() and "measuring anyway" in r.stderr,
                        f"{el:.1f}s"))

    with tempfile.TemporaryDirectory() as td:
        wt, lp = make_repo(td)
        h = holder(lp, 30)
        t0 = time.time()
        run_pf(PF, wt, Path(td) / "s", {"OLLAMA_PREFLIGHT_TREE_WAIT_S": "20"}, review=True)
        el = time.time() - t0
        h.kill(); h.wait()
        results.append(("--review never takes the lock", el < 15, f"{el:.1f}s"))

    bad = 0
    for name, ok, why in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {why}")
        bad += not ok
    print("ALL PASS" if not bad else f"{bad} FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
