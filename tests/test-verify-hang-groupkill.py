#!/usr/bin/env python3
"""Regression test: a hung verify must not outlive its timeout (Rivian s3,
2026-10-02: five `node tsx verify_impl.mts` orphans at 100% CPU for up to 4.5h,
each an infinite-loop relevance mutant whose `sh -c` alone was killed).

  * pgrun.run_group: a shell that spawns a spinning GRANDCHILD is killed whole on
    timeout -- the grandchild is gone too, rc 124, timed_out flagged;
  * verify-relevance._run_verify goes through it (no orphan) and tags [timeout];
  * mutant_timeout bounds a mutant by the measured green run, never above
    verify_timeout, never below the floor; preflight passes its verify time;
  * preflight sh() goes through pgrun;
  * slicer audit_baseline_green_claim measures the TARGET AT HEAD (a dirty
    refimpl-residue target must not "confirm" green-at-baseline) and restores it.
Run: python3 test-verify-hang-groupkill.py [--revert-check]
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
PGRUN = Path(os.environ.get("PGRUN_SRC") or HERE / "pgrun.py")
VREL = Path(os.environ.get("VREL_SRC") or HERE / "verify-relevance.py")
SLICE = Path(os.environ.get("SLICE_SRC") or HERE / "ollama-dispatch-slice")
PREF = Path(os.environ.get("PREF_SRC") or HERE / "ollama-dispatch-preflight")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie is dead for our purposes
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return bool(st.strip()) and not st.strip().startswith("Z")


def spinning_cmd(pidfile):
    # sh -> python grandchild that records its pid and spins forever (the mutant)
    return (f"{sys.executable} -c \"import os;open('{pidfile}','w').write(str(os.getpid()));"
            f"\nwhile True: pass\" & wait")


def grandchild_survives(runner, tmp, tag):
    pf = Path(tmp) / f"gc-{tag}.pid"
    t0 = time.time()
    res = runner(spinning_cmd(pf))
    took = time.time() - t0
    time.sleep(0.5)
    gpid = int(pf.read_text()) if pf.exists() else None
    surv = gpid is not None and alive(gpid)
    if surv:                              # never leave our own test spinner behind
        os.kill(gpid, 9)
    return res, surv, took, gpid


def main():
    tmp = tempfile.mkdtemp(prefix="vhang-")
    pg = load(PGRUN, "pgrun_t")
    res, surv, took, gpid = grandchild_survives(
        lambda c: pg.run_group(c, cwd=tmp, timeout=2), tmp, "pg")
    check("pgrun: the spinning grandchild was started", gpid is not None, True)
    check("pgrun: on timeout the WHOLE group dies (no orphaned grandchild)", surv, False)
    check("pgrun: rc 124 + timed_out", (res[0], res[3]), (124, True))
    check("pgrun: returns promptly after the timeout", took < 10, True)
    check("pgrun: a normal command is unaffected",
          pg.run_group("echo hi; exit 3", cwd=tmp, timeout=10)[:2], (3, "hi\n"))

    vr = load(VREL, "vrel_t")
    res, surv, _, _ = grandchild_survives(
        lambda c: vr._run_verify(c, Path(tmp), 2), tmp, "vr")
    check("verify-relevance: a hung mutant verify leaves no orphan", surv, False)
    check("verify-relevance: tagged [timeout] rc 124", (res[0], res[1].endswith("[timeout]")), (124, True))
    check("mutant_timeout: bounded by the green run", vr.mutant_timeout(30, 900), 300)
    check("mutant_timeout: floored", vr.mutant_timeout(2, 900), 120)
    check("mutant_timeout: never above verify_timeout", vr.mutant_timeout(500, 900), 900)
    check("mutant_timeout: unknown green time -> verify_timeout", vr.mutant_timeout(None, 900), 900)
    vsrc = VREL.read_text()
    check("measure_applied uses mutant_timeout for MUTANTS",
          "_run_verify(verify_cmd, worktree,\n                                        mutant_timeout(green_secs, verify_timeout))" in vsrc, True)
    psrc = PREF.read_text()
    check("preflight passes its measured verify time", "green_secs=self.verify_secs" in psrc, True)
    check("preflight sh() runs through pgrun", "_PGRUN.run_group(cmd, cwd=self.wt" in psrc, True)

    # --- slicer audit measures the target AT HEAD ---------------------------------
    sl = load(SLICE, "slice_t")
    sl.run = lambda cmd, **kw: subprocess.run(cmd, **kw)       # silence the echo
    wt = Path(tmp) / "wt"
    wt.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True)
    g("init", "-q")
    (wt / "t.py").write_text("X = 0\n")
    g("add", "t.py")
    g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    (wt / ".dispatch-harness.json").write_text('{"target": "t.py"}')
    (wt / "verify.sh").write_text('grep -q "X = 1" t.py && echo VERIFY_OK || exit 1\n')
    (wt / "t.py").write_text("X = 1\n")                         # refimpl residue
    check("audit: a dirty (solved) target does NOT confirm green-at-baseline",
          sl.audit_baseline_green_claim(str(wt)), False)
    check("audit: the dirty target is restored byte-for-byte", (wt / "t.py").read_text(), "X = 1\n")
    g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "solved")
    check("audit: a target solved AT HEAD still confirms", sl.audit_baseline_green_claim(str(wt)), True)
    (wt / "verify.sh").write_text(spinning_cmd(Path(tmp) / "gc-audit.pid") + "\n")
    sl.AUDIT_VERIFY_TIMEOUT_S = 2
    res = sl.audit_baseline_green_claim(str(wt))
    time.sleep(0.5)
    ap = Path(tmp) / "gc-audit.pid"
    apid = int(ap.read_text()) if ap.exists() else None
    asurv = apid is not None and alive(apid)
    if asurv:
        os.kill(apid, 9)
    check("audit: a hung verify is bounded and leaves no orphan", (res, asurv), (False, False))
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("PGRUN_SRC", "pgrun without its own session",
     "stderr=subprocess.PIPE, start_new_session=True)", "stderr=subprocess.PIPE)"),
    ("VREL_SRC", "relevance bypasses pgrun", "    if _PGRUN is not None:\n        # A plain",
     "    if False:\n        # A plain"),
    ("VREL_SRC", "mutant timeout = full verify timeout",
     "    return int(min(verify_timeout, max(MUTANT_TIMEOUT_FLOOR_S,",
     "    return verify_timeout or int(min(verify_timeout, max(MUTANT_TIMEOUT_FLOOR_S,"),
    ("SLICE_SRC", "audit measures the dirty target",
     "        if dirty and p.is_file():", "        if False:"),
    ("SLICE_SRC", "audit verify without a timeout",
     "timeout=AUDIT_VERIFY_TIMEOUT_S, shell=False)", "timeout=None, shell=False)"),
]


def revert_check():
    bad = 0
    srcs = {"PGRUN_SRC": PGRUN, "VREL_SRC": VREL, "SLICE_SRC": SLICE}
    for var, name, old, new in MUTATIONS:
        src = srcs[var].read_text()
        assert src.count(old) == 1, f"anchor missing: {name}"
        d = tempfile.mkdtemp(prefix="vhmut-")
        dst = Path(d) / srcs[var].name
        dst.write_text(src.replace(old, new))
        if var != "PGRUN_SRC":            # the mutant must still find the REAL pgrun
            (Path(d) / "pgrun.py").write_text(PGRUN.read_text())
        # Run the mutant suite through the REAL pgrun: a mutant that removes the
        # timeout hangs, and a plain subprocess timeout would orphan its spinner
        # (it did, once -- the very bug under test).
        pg = load(PGRUN, "pgrun_rc")
        rc, _o, _e, to = pg.run_group([sys.executable, __file__], timeout=120, shell=False,
                                      env={**os.environ, var: str(dst)})
        red = to or rc != 0               # hung on the unbounded verify == caught
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
