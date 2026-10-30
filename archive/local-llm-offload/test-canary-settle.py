#!/usr/bin/env python3
"""Seam test for the pipeline-canary's own flake fixes (2026-10-13).

 1. decoy_trigger_ready: the foreign decoy is enqueued only once the canary bundle has a REAL
    row (pending/running/...), never on the slicer's `planned` placeholders.
 2. settle_live: the end-of-run "no live jobs" check polls (bounded) instead of one read; a
    job that finishes late is not a failure, a job that stays live IS (and is named), a torn
    read is never "empty", and one empty read between two live ones does not count as quiet.
 3. wiring: check_invariants uses settle_live, the main loop uses decoy_trigger_ready.
"""
import importlib.util, inspect, sys
from pathlib import Path

bin_dir = Path(sys.argv[sys.argv.index("--bin") + 1]) if "--bin" in sys.argv else Path(__file__).parent
spec = importlib.util.spec_from_file_location("pc", bin_dir / "pipeline-canary.py")
pc = importlib.util.module_from_spec(spec)
sys.modules["pc"] = pc
spec.loader.exec_module(pc)

fails = []


def check(name, ok, note=""):
    print(("PASS " if ok else "FAIL ") + name + (f" -- {note}" if note and not ok else ""))
    if not ok:
        fails.append(name)


L = "pcanary-1"
planned = [{"id": f"p{i}", "label": f"{L}-s{i}", "status": "planned"} for i in range(3)]
check("decoy not triggered by planned placeholders only", not pc.decoy_trigger_ready(planned, L))
check("decoy triggered once a real canary row is pending",
      pc.decoy_trigger_ready(planned + [{"id": "a", "label": f"auto-author-{L}-s1".replace("auto-author-", L + "-"), "status": "pending"}], L))
check("decoy not triggered by its own row", not pc.decoy_trigger_ready(
    [{"id": "d", "label": f"{L}-decoy", "status": "pending"}], L))
check("decoy not triggered by another run's row", not pc.decoy_trigger_ready(
    [{"id": "x", "label": "pcanary-2-s1", "status": "running"}], L))
check("decoy not triggered by a done row", not pc.decoy_trigger_ready(
    [{"id": "x", "label": f"{L}-s1", "status": "done"}], L))


class Clk:
    def __init__(self):
        self.t = 0.0
    def now(self):
        return self.t
    def sleep(self, s):
        self.t += s


def run(seq, deadline=30.0, stable=2):
    """seq: list of reads (None = torn, [] = empty, [rows]); last repeats forever."""
    c = Clk()
    it = {"i": 0}
    def rd():
        v = seq[min(it["i"], len(seq) - 1)]
        it["i"] += 1
        return v
    return pc.settle_live(rd, deadline_s=deadline, step=1.0, stable=stable, clock=c.now, sleep=c.sleep)


job = lambda st="running", i="j1": {"id": i, "label": "gate-x", "status": st}
r, polls, bad = run([[job()]] * 5 + [[]])
check("late-finishing job: settles clean (no instant failure)", r == [] and polls >= 7, f"{(r, polls)}")
r, polls, bad = run([[job("held", "h1")]])
check("a job that never finishes fails and is named", r == ["h1:gate-x:held"], f"{r}")
check("it waited the bounded deadline, not one read", polls >= 30, f"polls={polls}")
r, polls, bad = run([[job()], [], [job()]])
check("one empty read between live reads is not quiet", r == ["j1:gate-x:running"], f"{r}")
r, polls, bad = run([None, None, None], deadline=5.0)
check("torn reads are never 'empty'", r == ["<queue state unreadable>"] and bad >= 5, f"{(r, bad)}")
r, polls, bad = run([None, [], []])
check("a torn read then two clean empties settles", r == [], f"{r}")
r, polls, bad = run([[{"id": "d1", "label": "x", "status": "done"}, {"id": "p1", "label": "y", "status": "planned"}]])
check("terminal / placeholder rows are not live", r == [], f"{r}")

ci = inspect.getsource(pc.check_invariants)
check("check_invariants polls via settle_live", "settle_live(" in ci)
check("settle bound is real (>= 30s) and used by the final check", pc.SETTLE_DEADLINE_S >= 30 and "deadline_s=SETTLE_DEADLINE_S" in ci)
check("final-check note names the sandbox", "sandbox=" in ci and "live=" in ci)
src = (bin_dir / "pipeline-canary.py").read_text()
check("canary main loop gates the decoy on decoy_trigger_ready",
      "and decoy_trigger_ready(" in src and 'startswith(sb.label) for j in q.get("jobs") or []):' not in src)

print("CANARY_SETTLE_OK" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
