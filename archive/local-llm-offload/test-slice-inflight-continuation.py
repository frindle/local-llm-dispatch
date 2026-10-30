#!/usr/bin/env python3
"""test-slice-inflight-continuation.py -- slice_job_inflight sees AUTHOR CONTINUATION
jobs (`auto-author-<label>-<sid>-cN` / `-esc`), not just the first author pass, the
coding job and refine rounds (soak seed 19, 2026-10-06).

Seed 19: with auto-author-pcanary-...-s2-fmt-c1 PENDING in the queue, a second
--execute's in-flight guard returned None, so the PENDING "harness already authored
-- resuming" path ran clean_and_seal + preflight on the worktree c1 was about to use.

  --revert-check PRE_FIX_COPY : the pre-fix slicer must go RED.
"""
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

SLICER = Path.home() / "bin" / "ollama-dispatch-slice"


def load(path):
    ld = SourceFileLoader("_slice_inflight_cont", str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(ld.name, ld))
    ld.exec_module(m)
    return m


def run(mod):
    st = {"label": "pc", "slices": {}}
    res = []

    def q(by_label):
        mod.all_job_statuses = lambda: ({}, by_label)
        return mod.slice_job_inflight(st, "s2-fmt")

    r = q({"auto-author-pc-s2-fmt": ("a0", "done"), "auto-author-pc-s2-fmt-c1": ("c1", "pending")})
    res.append(("pending -c1 continuation is in flight", (r or (None,))[0] == "c1", r))
    r = q({"auto-author-pc-s2-fmt-c1": ("c1", "done"), "auto-author-pc-s2-fmt-c2": ("c2", "running")})
    res.append(("running -c2 (c1 done) is in flight", (r or (None,))[0] == "c2", r))
    r = q({"auto-author-pc-s2-fmt-esc": ("e", "queued")})
    res.append(("queued -esc author is in flight", (r or (None,))[0] == "e", r))
    r = q({"auto-author-pc-s2-fmt-c1": ("c1", "done"), "pc-s2-fmt": ("j", "done")})
    res.append(("all terminal -> None", r is None, r))
    r = q({"auto-author-pc-s2-fmt-x-c1": ("o", "running"), "auto-author-pc-s2-c1": ("p", "running")})
    res.append(("other slices' continuations are not this slice's", r is None, r))
    r = q({"pc-s2-fmt": ("j", "running")})
    res.append(("coding job still detected", (r or (None,))[0] == "j", r))
    r = q({"auto-refine-pc-s2-fmt-r2": ("r", "held")})
    res.append(("refine round still detected", (r or (None,))[0] == "r", r))
    return res


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--revert-check":
        res = run(load(Path(sys.argv[2])))
        for n, ok, why in res:
            print(f"  pre-fix [{'PASS' if ok else 'FAIL'}] {n}  {why}")
        if all(ok for _, ok, _ in res):
            print("REVERT-CHECK FAILED: pre-fix copy passes -- inert test")
            return 1
        print("REVERT-CHECK OK: pre-fix copy goes RED")
        return 0
    bad = 0
    for n, ok, why in run(load(SLICER)):
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}  {why}")
        bad += not ok
    print("ALL PASS" if not bad else f"{bad} FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
