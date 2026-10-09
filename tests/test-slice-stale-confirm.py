#!/usr/bin/env python3
"""test-slice-stale-confirm.py -- confirm_and_enqueue re-reads DISK under the tree
lock and refuses to touch a slice another driver moved on (soak seed 10, 2026-10-06).

Seed 10: a stale driver (it read s2-fmt before another driver enqueued coding job
fa1b143deb7e) ran confirm_and_enqueue anyway -> its preflight collided with the
pending job, then (job finished, deliverable uncommitted) NO-GO'd baseline-clean and
ESCALATED a healthy slice.

Checks (temp git worktree, real slicer module in-process, queue/state reads stubbed):
  1. disk says ENQUEUED (job X), job no longer live -> returns, no draft/preflight
  2. a live job for the slice -> returns, no draft/preflight
  3. disk says CONFIRMED, no live job -> proceeds (guard not over-broad)
  4. the heal-requeue path (expect incl. ENQUEUED) still proceeds
  5. the tree is untouched in 1 and 2

  --revert-check PRE_FIX_COPY : checks 1/2 against the pre-fix slicer must go RED.
"""
import importlib.util
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

SLICER = Path.home() / "bin" / "ollama-dispatch-slice"


def load(path):
    ld = SourceFileLoader("_slice_stale_confirm", str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(ld.name, ld))
    ld.exec_module(m)
    return m


def g(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)


class _Stop(Exception):
    pass


def attempt(mod, disk_status, live, expect=None):
    """-> (called_tools, tree_porcelain_after)"""
    with tempfile.TemporaryDirectory() as td:
        w = Path(td) / "wt"
        w.mkdir()
        g(w, "init", "-q")
        (w / "t.ts").write_text("export const x = 1;\n")
        g(w, "add", "."); g(w, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b")
        (w / "t.ts").write_text("export const x = 2;\n")      # the live job's deliverable
        called = []

        def fake_run(cmd, **kw):
            called.append(Path(cmd[0]).name)
            raise _Stop()
        mod.run = fake_run
        mod.clean_and_seal = lambda wt: called.append("seal")
        mod.read_state = lambda label: {"slices": {"s2": {"status": disk_status,
                                                          "job_id": "fa1b143deb7e"}}}
        mod.slice_job_inflight = (lambda st, sid: ("fa1b143deb7e", "running", "p-s2")) \
            if live else (lambda st, sid: None)
        st = {"label": "p", "slices": {"s2": {}}}
        kw = {} if expect is None else {"expect": expect}
        try:
            mod.confirm_and_enqueue(st, "s2", {"must_contain": []}, str(w), "m", "h",
                                    1, 1, "auto", None, **kw)
        except _Stop:
            pass
        return called, g(w, "status", "--porcelain").stdout.strip()


def checks(mod, with_expect=True):
    res = []
    c, t = attempt(mod, "enqueued", live=False)
    res.append(("disk ENQUEUED (another driver) -> no draft/preflight", c == [], c))
    res.append(("...tree untouched", t == "M t.ts", t))
    c, t = attempt(mod, "confirmed", live=True)
    res.append(("live job for the slice -> no draft/preflight", c == [], c))
    res.append(("...tree untouched", t == "M t.ts", t))
    if with_expect:
        c, _ = attempt(mod, "confirmed", live=False)
        res.append(("disk CONFIRMED, no live job -> proceeds", bool(c), c))
        c, _ = attempt(mod, "enqueued", live=False,
                       expect=(mod.CONFIRMED, mod.AWAITING_REVIEW, mod.PENDING, mod.ENQUEUED))
        res.append(("heal-requeue (expect incl. ENQUEUED) -> proceeds", bool(c), c))
    return res


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--revert-check":
        res = checks(load(Path(sys.argv[2])), with_expect=False)
        for n, ok, why in res:
            print(f"  pre-fix [{'PASS' if ok else 'FAIL'}] {n}  {why}")
        if all(ok for _, ok, _ in res):
            print("REVERT-CHECK FAILED: pre-fix copy passes -- inert test")
            return 1
        print("REVERT-CHECK OK: pre-fix copy goes RED")
        return 0
    res = checks(load(SLICER))
    bad = 0
    for n, ok, why in res:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}  {why}")
        bad += not ok
    print("ALL PASS" if not bad else f"{bad} FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
