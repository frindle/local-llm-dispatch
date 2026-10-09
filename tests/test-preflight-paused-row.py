#!/usr/bin/env python3
"""Unattended-readiness (2b): preflight cwd-exclusive ignores PAUSED (no process)
and CANCELLED job rows on the worktree.

A paused row is a resume transcript with no process: it cannot read the refimpl
mid-gate, so it must not NO-GO the re-gate meant to replace it (reported as a
parked WARN, never FAIL). A paused row whose pid is still alive stays blocking.
Cancelled rows (and rows cancelled in place -> failed) never block.
PREFLIGHT_SRC=<path> for the revert-test.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(__file__).resolve().parent
PF = Path(os.environ.get("PREFLIGHT_SRC") or BIN / "ollama-dispatch-preflight")
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    sys.argv = [str(path)]
    loader.exec_module(m)
    return m


def main():
    tmp = Path(tempfile.mkdtemp(prefix="pf-paused-"))
    qs = tmp / "state.json"
    os.environ["OLLAMA_QUEUE_STATE"] = str(qs)
    pf = load(PF, "pf_paused")
    pf.QUEUE_STATE = qs
    wt = (tmp / "wt").resolve()
    wt.mkdir()

    def cwd_check(jobs):
        qs.write_text(json.dumps({"jobs": jobs}))
        p = pf.Preflight(types.SimpleNamespace(worktree=str(wt)))
        p.check_cwd_exclusive()
        r = [x for x in p.results if x.id == "cwd-exclusive"][-1]
        return r.status, r.msg + " " + r.detail

    # a pid that is certainly dead: a child we started and reaped
    dead = subprocess.Popen(["true"])
    dead.wait()
    st, txt = cwd_check([{"id": "pz1", "status": "paused", "label": "chat-fixes-s6c",
                          "cwd": str(wt), "pid": dead.pid,
                          "pause_reason": "gate_preempt"}])
    chk("paused row (no live process) on this tree does NOT block", st != pf.FAIL, True)
    chk("...reported as a parked WARN naming the row", (st, "pz1" in txt), (pf.WARN, True))
    st, _ = cwd_check([{"id": "pz2", "status": "paused", "label": "x", "cwd": str(wt)}])
    chk("paused row with no pid recorded does NOT block", st != pf.FAIL, True)
    st, _ = cwd_check([{"id": "pz3", "status": "paused", "label": "x", "cwd": str(wt),
                        "pid": os.getpid()}])
    chk("paused row whose pid is STILL ALIVE still FAILs (pause not landed)", st, pf.FAIL)
    st, _ = cwd_check([{"id": "c1", "status": "cancelled", "label": "x", "cwd": str(wt)}])
    chk("cancelled row on this tree -> PASS", st, pf.PASS)
    st, _ = cwd_check([{"id": "c2", "status": "failed", "terminal_reason": "cancelled",
                        "label": "x", "cwd": str(wt)}])
    chk("cancelled-in-place row (failed) -> PASS", st, pf.PASS)
    st, txt = cwd_check([{"id": "pz4", "status": "paused", "label": "x",
                          "cwd": str(tmp / "other")}])
    chk("paused row on ANOTHER tree -> PASS (no 'unrecognised status' noise)", st, pf.PASS)
    for s in ("running", "pending", "held", "planned"):
        chk(f"{s} row on this tree still FAILs", cwd_check(
            [{"id": "r1", "status": s, "label": "x", "cwd": str(wt)}])[0], pf.FAIL)
    subprocess.run(["rm", "-rf", str(tmp)])
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAIL")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
