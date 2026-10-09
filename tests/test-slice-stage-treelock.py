#!/usr/bin/env python3
"""test-slice-stage-treelock.py -- the slicer reads a slice's deliverable under the
worktree tree lock (soak seed 3, 2026-10-06).

A self-check / gate relevance pass holds <git-dir>/dispatch-tree.lock while it
resets the target to HEAD for its baseline measurement, then restores the model's
edit. The slicer's _stage_slice read the live target in that window, saw the
baseline, reported "target IDENTICAL to the chain baseline (empty deliverable)"
and FAILED a green slice.

Checks (temp git repos, the real slicer module loaded in-process):
  1. a holder that has the target reset to baseline while holding the lock ->
     _stage_slice waits and stages the REAL deliverable ('ok', chain gets it)
  2. a genuinely empty deliverable is still 'empty' (guard not weakened)
  3. no holder -> no wait
  4. the lock is free again after _stage_slice returns
  6. confirm_and_enqueue holds the lock from `draft --confirm` through preflight:
     a racing self-check that snapshotted the MARKED fixture cannot restore it over
     the sealed confirmed one; preflight runs with OLLAMA_DISPATCH_TREE_LOCK_HELD
     naming the lock (no parent/child deadlock)
  5. _land_green_slice (live verify -> stage -> commit) with the same racing holder
     lands the slice instead of failing it on the baseline the holder exposed

  --revert-check PRE_FIX_COPY : check 1 against the given pre-fix slicer must go RED.
"""
import fcntl
import os
import subprocess
import sys
import tempfile
import time
from importlib.machinery import SourceFileLoader
import importlib.util
from pathlib import Path

SLICER = Path.home() / "bin" / "ollama-dispatch-slice"


def load(path):
    ld = SourceFileLoader("_slice_under_test", str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(ld.name, ld))
    ld.exec_module(m)
    return m


def g(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)


def make(td, edit=True):
    c = Path(td) / "chain"
    c.mkdir()
    g(c, "init", "-q", "-b", "main")
    g(c, "config", "user.email", "t@t"); g(c, "config", "user.name", "t")
    (c / "t.txt").write_text("base\n")
    g(c, "add", "."); g(c, "commit", "-q", "-m", "seed")
    w = Path(td) / "wt"
    g(c, "worktree", "add", "-q", "-b", "sl", str(w), "HEAD")
    if edit:
        (w / "t.txt").write_text("base\nFIXED\n")
    lp = Path(g(w, "rev-parse", "--absolute-git-dir").stdout.strip()) / "dispatch-tree.lock"
    return c, w, lp


def holder(w, lp, secs):
    """Simulates auto-harness-check: take the lock, reset the target to HEAD
    (baseline measurement), hold, restore the model's bytes, release."""
    code = ("import fcntl,subprocess,sys,time,pathlib\n"
            "w,lp,s=sys.argv[1],sys.argv[2],float(sys.argv[3])\n"
            "fh=open(lp,'a+');fcntl.flock(fh.fileno(),fcntl.LOCK_EX)\n"
            "p=pathlib.Path(w,'t.txt');keep=p.read_bytes()\n"
            "subprocess.run(['git','-C',w,'checkout','HEAD','--','t.txt'])\n"
            "print('held',flush=True);time.sleep(s);p.write_bytes(keep);fh.close()\n")
    p = subprocess.Popen([sys.executable, "-c", code, str(w), str(lp), str(secs)],
                         stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


ST = {"target": "t.txt", "slices": {"s1": {"intent": "x"}}}
S = ST["slices"]["s1"]


def check_race(mod):
    with tempfile.TemporaryDirectory() as td:
        c, w, lp = make(td)
        h = holder(w, lp, 3)
        oc, msg = mod._stage_slice(str(c), str(w), ST, "s1", S)
        h.wait()
        staged = (c / "t.txt").read_text()
        return oc == "ok" and "FIXED" in staged, f"{oc}: {msg[:90]}"


def check_land(mod):
    with tempfile.TemporaryDirectory() as td:
        c, w, lp = make(td)
        (w / "verify.sh").write_text("grep -q FIXED t.txt && echo VERIFY_OK\n")
        mod.save_state = lambda st: None
        mod._draft_unconfirmed = lambda wt: False
        s = {"intent": "x", "title": "s1 title", "worktree": str(w)}
        st = {"target": "t.txt", "slices": {"s1": s}}
        h = holder(w, lp, 3)
        ok, msg = mod._land_green_slice(st, "s1", s, str(c), require_relevance=True)
        h.wait()
        landed = "FIXED" in (g(c, "show", "HEAD:t.txt").stdout or "")
        return ok and landed and s.get("status") == mod.DONE, f"{ok}: {str(msg)[:90]}"


class _Stop(Exception):
    pass


def check_confirm(mod):
    """Holder = a self-check that took the lock, snapshotted the (marked) fixture,
    and restores it 3s later. The slicer's confirm+seal+preflight must not interleave."""
    import subprocess as sp
    with tempfile.TemporaryDirectory() as td:
        c, w, lp = make(td)
        fx = w / "verify.test.ts"
        fx.write_text("// DRAFT_UNCONFIRMED\nok\n")
        g(w, "add", "-A"); g(w, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "h")
        fx.write_text("// DRAFT_UNCONFIRMED\nok\n// edited\n")   # dirty, marked
        code = ("import fcntl,sys,time,pathlib\n"
                "w,lp=sys.argv[1],sys.argv[2]\n"
                "fh=open(lp,'a+');fcntl.flock(fh.fileno(),fcntl.LOCK_EX)\n"
                "p=pathlib.Path(w,'verify.test.ts');keep=p.read_bytes()\n"
                "print('held',flush=True);time.sleep(3);p.write_bytes(keep);fh.close()\n")
        h = sp.Popen([sys.executable, "-c", code, str(w), str(lp)], stdout=sp.PIPE, text=True)
        assert h.stdout.readline().strip() == "held"
        seen = {}

        def fake_run(cmd, **kw):
            if cmd[0] == mod.DRAFT:
                fx.write_text(fx.read_text().replace("// DRAFT_UNCONFIRMED\n", ""))
            elif cmd[0] == mod.PREFLIGHT:
                seen["porcelain"] = g(w, "status", "--porcelain", "--", "verify.test.ts").stdout.strip()
                seen["env"] = os.environ.get("OLLAMA_DISPATCH_TREE_LOCK_HELD")
                raise _Stop()
            return sp.CompletedProcess(cmd, 0, "", "")

        def fake_seal(wt):
            g(wt, "add", "-A")
            g(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seal")

        mod.run, mod.clean_and_seal = fake_run, fake_seal
        try:
            mod.confirm_and_enqueue({}, "s1", {"must_contain": []}, str(w), "m", "h", 1, 1, "auto", None)
        except _Stop:
            pass
        h.wait()
        after = g(w, "status", "--porcelain", "--", "verify.test.ts").stdout.strip()
        ok = seen.get("porcelain") == "" and after == "" and seen.get("env") == str(lp)
        return ok, f"at preflight={seen.get('porcelain')!r} after={after!r} env_ok={seen.get('env') == str(lp)}"


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--revert-check":
        ok, why = check_race(load(Path(sys.argv[2])))
        ok2, why2 = check_land(load(Path(sys.argv[2])))
        ok3, why3 = check_confirm(load(Path(sys.argv[2])))
        print(f"pre-fix: stage={why} | land={why2} | confirm={why3}")
        if ok and ok2 and ok3:
            print("REVERT-CHECK FAILED: pre-fix copy passes every race check -- inert test")
            return 1
        print("REVERT-CHECK OK: pre-fix copy goes RED")
        return 0
    mod = load(SLICER)
    res = []
    ok, why = check_race(mod)
    res.append(("racing self-check: waits, stages the real deliverable", ok, why))
    ok, why = check_land(load(SLICER))
    res.append(("racing self-check during live-verify land: lands, not FAILED", ok, why))
    ok, why = check_confirm(load(SLICER))
    res.append(("confirm+seal+preflight in one lock span; preflight told parent holds it", ok, why))

    with tempfile.TemporaryDirectory() as td:
        c, w, lp = make(td, edit=False)
        oc, msg = mod._stage_slice(str(c), str(w), ST, "s1", S)
        res.append(("genuinely empty deliverable still 'empty'", oc == "empty", oc))

    with tempfile.TemporaryDirectory() as td:
        c, w, lp = make(td)
        t0 = time.time()
        oc, _ = mod._stage_slice(str(c), str(w), ST, "s1", S)
        el = time.time() - t0
        res.append(("no holder -> no wait", oc == "ok" and el < 5, f"{oc} {el:.1f}s"))
        fh = open(lp, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            free = True
        except OSError:
            free = False
        fh.close()
        res.append(("lock free after return", free, ""))

    bad = 0
    for n, ok, why in res:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}  {why}")
        bad += not ok
    print("ALL PASS" if not bad else f"{bad} FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
