#!/usr/bin/env python3
"""A slicer-advance CPU/driver gap holds no GPU lane past SLICER_GAP_YIELD_S (2026-10-09).
Run: python3 test-slicer-gap-yield.py [--bin DIR]  -> SLICER_GAP_YIELD_OK"""
import argparse, importlib.util as u, json, sys, tempfile, time
from pathlib import Path
ap = argparse.ArgumentParser(); ap.add_argument("--bin", default=str(Path(__file__).resolve().parent))
BIN = Path(ap.parse_args().bin).resolve()
sp = u.spec_from_file_location("q_gap", BIN / "ollama-queue.py"); q = u.module_from_spec(sp); sp.loader.exec_module(q)
fails = []
def chk(n, c):
    print(("ok   " if c else "FAIL ") + n)
    if not c: fails.append(n)
NOW = 1_000_000.0
pk = lambda j: j.get("bundle")
q.SLICER_GAP_YIELD_S = 45.0
def st(since, jobs=(), settling=None, key="B"):
    plan = {"known": True, "driver_live": True, "driver_since": since, "live": [("B", "s2", "pending")]}
    return q.bundle_commit_status(key, list(jobs), pk, plan, {}, settling=settling, now=NOW)
chk("fresh advance (10s) keeps the lane", st(NOW - 10)[0::2] == ("working", True))
r = st(NOW - 120)
chk("a 120s advance with no GPU row is a CPU gap: waiting, not moving", r[0] == "waiting" and r[2] is False and "holds no GPU lane" in r[1])
chk("a running row of the bundle still holds it", st(NOW - 120, [{"id": "r", "status": "running", "bundle": "B"}])[0] == "working")
chk("a pending runnable row still holds it", st(NOW - 120, [{"id": "p", "status": "pending", "bundle": "B"}])[0] == "working")
chk("gate-on-complete settling still holds it", st(NOW - 120, settling=["g"])[0] == "working")
q.SLICER_GAP_YIELD_S = 0
chk("0 switches the yield off", st(NOW - 600)[0] == "working")
q.SLICER_GAP_YIELD_S = 45.0
# the commit step: parks the gap bundle so a pending row of ANOTHER bundle can launch; resumes it first
status = {"B": ("waiting", "gap", False), "C": ("working", "1 live", True)}
parked = {}
commit, ev = q.bundle_commit_step({"key": "B", "since": NOW - 500, "empty_since": None, "idle_since": None}, parked,
                                  None, ["C"], lambda k: status[k], NOW)
chk("step: the gap bundle is parked cpu_wait and the commitment moves to the waiting bundle",
    parked.get("B", {}).get("kind") == "cpu_wait" and (commit or {}).get("key") == "C")
status["B"] = ("working", "1 live row(s)", True)
commit, ev = q.bundle_commit_step(commit, parked, None, ["C"], lambda k: status[k], NOW + 5)
chk("step: when its next row exists it takes the lane back FIRST (visitor yields)",
    (commit or {}).get("key") == "B" and "B" not in parked)
# driver_since is read from the advance lock
d = Path(tempfile.mkdtemp(prefix="gap-")); import os
(d / "B.json").write_text(json.dumps({"label": "B", "order": ["s1"], "slices": {"s1": {"status": "pending"}}, "plan_path": "x"}))
(d / "B.advance.lock").write_text(json.dumps({"pid": os.getpid(), "started_at": "2026-10-09T00:00:00+00:00"}))
p = q.slice_plan_runnability("B", runs_dir=d, now=time.time())
chk("slice_plan_runnability exposes driver_since from the lock's started_at",
    p.get("driver_live") and abs((p.get("driver_since") or 0) - 1791504000.0) < 1)
print("SLICER_GAP_YIELD_OK" if not fails else f"{len(fails)} FAILED"); sys.exit(1 if fails else 0)
