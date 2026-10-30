#!/usr/bin/env python3
"""test-slice-orphan-enqueue.py -- a coding job whose enqueuing driver died before
recording it (slice still CONFIRMED) is adopted, not re-preflighted into an
ESCALATION (soak seed 36, 2026-10-06).

Seed 36: the driver enqueued s2-fmt's coding job f6d9fc5c11d1 and was SIGKILLed
before save_state(ENQUEUED, job_id). The next advance found s2-fmt CONFIRMED, ran
confirm_and_enqueue -> preflight over the job's own deliverable -> baseline-clean
NO-GO -> ESCALATED (later AUTO-LANDED: the job had passed).

Checks (real slicer module, I/O injected):
  1. passed_job_to_land: CONFIRMED + newest coding job done/pass -> that job
  2. auto_land_passed lands the CONFIRMED slice when the worktree holds the job's diff
  3. ...and does NOT when the worktree does not (an older attempt's job) -- guard intact
  4. PENDING is still not eligible
  5. confirm_and_enqueue refuses while a finished job's gate has not reported
     (no draft/preflight touches the tree)

  --revert-check PRE_FIX_COPY : the pre-fix slicer must go RED.
"""
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

SLICER = Path.home() / "bin" / "ollama-dispatch-slice"


def load(path):
    ld = SourceFileLoader("_slice_orphan_enq", str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(ld.name, ld))
    ld.exec_module(m)
    return m


def st_with(mod, status):
    return {"label": "pc", "target": "lib/fmt.ts", "order": ["s2"], "slices": {
        "s2": {"status": status, "depends_on": [], "worktree": "/nonexistent/wt", "intent": "x"}}}


JOBS = [("t", "f6d9fc5c11d1", {"status": "done", "exit_code": 0})]
GATE = staticmethod(lambda jid: {"verdict": "pass"})


def run(mod):
    res = []
    mod.deps_done = lambda st, sid: True
    st = st_with(mod, mod.CONFIRMED)
    jid, why = mod.passed_job_to_land(st, "s2", JOBS, lambda j: {"verdict": "pass"})
    res.append(("CONFIRMED + passed newest coding job -> adopt it", jid == "f6d9fc5c11d1", why))

    landed_calls = []

    def land(st, sid, s, cwt, require_relevance=True):
        landed_calls.append(sid)
        s["status"] = mod.DONE
        return True, "abcdef12"
    for match_ok, want in ((True, ["s2"]), (False, [])):
        landed_calls.clear()
        st = st_with(mod, mod.CONFIRMED)
        try:
            got = mod.auto_land_passed(st, "/nonexistent/cwt", finder=lambda st, sid: JOBS,
                                       gate_fn=lambda j: {"verdict": "pass"},
                                       inflight_fn=lambda st, sid: None,
                                       match_fn=lambda *a: (match_ok, "" if match_ok else "no match"),
                                       land_fn=land, save_fn=lambda st: None)
        except Exception as e:
            got = f"raised {e!r}"
        res.append((f"auto_land_passed CONFIRMED, tree {'holds' if match_ok else 'lacks'} the diff "
                     f"-> {'lands' if want else 'left alone'}", got == want, got))
        if not match_ok:
            res.append(("...and the not-adopted slice is NOT escalated (stays CONFIRMED)",
                        st["slices"]["s2"].get("status") == mod.CONFIRMED,
                        st["slices"]["s2"].get("status")))
    st = st_with(mod, mod.PENDING)
    jid, why = mod.passed_job_to_land(st, "s2", JOBS, lambda j: {"verdict": "pass"})
    res.append(("PENDING still not eligible", jid is None, why))

    called = []

    class _Stop(Exception):
        pass

    def fake_run(cmd, **kw):
        called.append(Path(cmd[0]).name)
        raise _Stop()
    mod.run = fake_run
    mod.clean_and_seal = lambda wt: called.append("seal")
    mod.read_state = lambda label: {"slices": {"s2": {"status": "confirmed"}}}
    mod.slice_job_inflight = lambda st, sid: None
    mod.slice_job_awaiting_gate = lambda st, sid, *a, **k: ("f6d9fc5c11d1", "pc-s2", 4.0)
    try:
        mod.confirm_and_enqueue(st_with(mod, mod.CONFIRMED), "s2", {"must_contain": []},
                                "/nonexistent/wt", "m", "h", 1, 1, "auto", None)
    except _Stop:
        pass
    except Exception as e:
        called.append(f"raised {type(e).__name__}")
    res.append(("finished job awaiting its gate -> confirm does not touch the tree", called == [], called))
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
