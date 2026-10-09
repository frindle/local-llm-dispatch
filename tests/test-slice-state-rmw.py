#!/usr/bin/env python3
"""Race tests for the slice-runs lost update (2026-09-27, BFMR s2).

THE BUG. The detached driver loaded sidecar-bfmr-login-fetch's run state with s2
ESCALATED; a `--regate s2` (another slicer process) then enqueued coding job
0a7ff4902d90 and saved s2 = ENQUEUED/job_id; the driver's next save_state() wrote
its stale whole state back -- s2 ESCALATED, job_id None -- and the job's passed
deliverable was orphaned. merge_terminal_facts() protected DONE/SKIPPED only.

Asserts (each red on revert to the whole-state write-back):
  * a stale writer that did not touch s2 cannot clobber another writer's s2 enqueue;
  * a stale writer that DID edit s2 loses to the newer job (newer job wins);
  * edits to different fields of one slice by two writers both survive;
  * refresh_state folds another writer's change into memory, keeping own edits;
  * the driver adopts a coding job enqueued AFTER the escalation (queue truth);
  * two processes hammering saves concurrently lose no update (the lock);
  * DONE is still never undone by a stale writer.

Sandboxed: temp HOME, so STATE_ROOT is a temp dir. SLICER=<path> tests another copy
(the revert check). Run: python3 test-slice-state-rmw.py
"""
import importlib.util
import json
import multiprocessing as mp
import os
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(__file__).resolve().parent
SLICER = os.environ.get("SLICER") or str(BIN / "ollama-dispatch-slice")
failures = []


def ok(name, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")
    if not cond:
        failures.append(name)


def load(tag):
    loader = SourceFileLoader(f"slicer_{tag}", SLICER)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


PLAN = {"label": "race", "repo": "/nonexistent", "target": "a.js", "lang": "javascript",
        "slices": [{"id": "s1", "intent": "one"}, {"id": "s2", "intent": "two",
                   "depends_on": ["s1"]}, {"id": "s3", "intent": "three"}]}


def seed(m, s2_status="escalated"):
    st = m.load_state(dict(PLAN))
    st["slices"]["s1"]["status"] = "done"
    st["slices"]["s2"]["status"] = s2_status
    st["slices"]["s2"]["escalation_reason"] = "relevance NO-GO"
    m.save_state(st)


def disk(m):
    return json.loads(Path(m.state_path("race")).read_text())


def fresh(m):
    m._STATE_BASE.clear() if hasattr(m, "_STATE_BASE") else None


def _hammer(slicer, home, field, n, q):
    os.environ["HOME"] = home
    global SLICER
    SLICER = slicer
    m = load(f"h{field}")
    st = m.load_state(dict(PLAN))
    for i in range(n):
        if hasattr(m, "refresh_state"):
            pass                       # the save path alone must be race-safe
        st["slices"]["s3"][field] = i + 1
        m.save_state(st)
    q.put(field)


def main():
    home = tempfile.mkdtemp(prefix="rmw-home-")
    os.environ["HOME"] = home
    os.environ["OLLAMA_DISPATCH_PROGRESS_DIR"] = os.path.join(home, "progress")

    print("stale writer vs --regate enqueue (the BFMR s2 shape)")
    a, b = load("a"), load("b")               # two processes: driver A, regate B
    seed(b)
    fresh(b)
    drv = a.load_state(dict(PLAN))            # driver loads: s2 escalated
    reg = b.load_state(dict(PLAN))            # regate loads, enqueues, saves
    reg["slices"]["s2"].update(status="enqueued", job_id="0a7ff4902d90",
                               escalation_reason=None)
    b.save_state(reg)
    drv["slices"]["s3"]["status"] = "pending"
    drv["slices"]["s3"]["worktree"] = "/wt/s3"  # driver saves an unrelated change
    a.save_state(drv)
    d = disk(a)
    ok("s2 keeps the regate's ENQUEUED", d["slices"]["s2"]["status"] == "enqueued")
    ok("s2 keeps job 0a7ff4902d90", d["slices"]["s2"].get("job_id") == "0a7ff4902d90")
    ok("the driver's own s3 edit is written", d["slices"]["s3"].get("worktree") == "/wt/s3")
    ok("the driver's memory now shows the regate's enqueue",
       drv["slices"]["s2"]["status"] == "enqueued")

    print("stale writer that edits s2 itself loses to the newer job")
    seed(b)
    drv = a.load_state(dict(PLAN))
    reg = b.load_state(dict(PLAN))
    reg["slices"]["s2"].update(status="enqueued", job_id="J2")
    b.save_state(reg)
    drv["slices"]["s2"]["escalation_reason"] = "stale re-escalation"
    drv["slices"]["s2"]["status"] = "escalated"
    import contextlib, io
    with contextlib.redirect_stderr(io.StringIO()):
        a.save_state(drv)
    d = disk(a)
    ok("newer job wins: s2 stays ENQUEUED on J2",
       (d["slices"]["s2"]["status"], d["slices"]["s2"].get("job_id")) == ("enqueued", "J2"))

    print("two writers, different fields of one slice")
    seed(b)
    x = a.load_state(dict(PLAN))
    y = b.load_state(dict(PLAN))
    x["slices"]["s3"]["title"] = "from-x"
    y["slices"]["s3"]["worktree"] = "/from-y"
    a.save_state(x)
    b.save_state(y)
    d = disk(a)
    ok("both fields survive",
       (d["slices"]["s3"].get("title"), d["slices"]["s3"].get("worktree"))
       == ("from-x", "/from-y"))

    print("refresh_state")
    if hasattr(a, "refresh_state"):
        seed(b)
        x = a.load_state(dict(PLAN))
        y = b.load_state(dict(PLAN))
        x["slices"]["s3"]["title"] = "unsaved-x"
        y["slices"]["s2"].update(status="enqueued", job_id="J3")
        b.save_state(y)
        a.refresh_state(x)
        ok("memory picks up the other writer's enqueue",
           (x["slices"]["s2"]["status"], x["slices"]["s2"].get("job_id")) == ("enqueued", "J3"))
        ok("own unsaved edit kept", x["slices"]["s3"].get("title") == "unsaved-x")
        a.save_state(x)
        ok("...and still written on the next save",
           disk(a)["slices"]["s3"].get("title") == "unsaved-x")
    else:
        ok("refresh_state exists", False)

    print("driver adopts a job enqueued after the escalation")
    if hasattr(a, "adopt_post_escalation_job"):
        seed(b)
        st = a.load_state(dict(PLAN))
        st["slices"]["s2"]["escalated_at"] = "2026-09-27T19:37:00+00:00"
        a.save_state(st)
        s = st["slices"]["s2"]
        before = a.adopt_post_escalation_job(
            st, "s2", s, by_label={"race-s2": ("old1", "done")},
            enqueued_at_of=lambda j: "2026-09-27T19:30:00+00:00")
        ok("a job older than the escalation is NOT adopted", before is None
           and s["status"] == "escalated")
        got = a.adopt_post_escalation_job(
            st, "s2", s, by_label={"race-s2": ("0a7ff4902d90", "done")},
            enqueued_at_of=lambda j: "2026-09-27T19:37:13+00:00")
        ok("a newer coding job is adopted as ENQUEUED",
           got == "0a7ff4902d90" and disk(a)["slices"]["s2"]["status"] == "enqueued")
        s.pop("escalated_at", None)
        s["status"] = "escalated"
        s["job_id"] = None
        ok("no escalated_at -> nothing adopted",
           a.adopt_post_escalation_job(st, "s2", s, by_label={"race-s2": ("z", "done")},
                                       enqueued_at_of=lambda j: "2099-01-01T00:00:00+00:00")
           is None)
        st2 = a.load_state(dict(PLAN))
        st2["slices"]["s3"]["status"] = "escalated"
        a.save_state(st2)
        ok("save_state stamps escalated_at on the transition",
           bool(disk(a)["slices"]["s3"].get("escalated_at")))
    else:
        ok("adopt_post_escalation_job exists", False)

    print("DONE is never undone by a stale writer")
    seed(b)
    x = a.load_state(dict(PLAN))
    y = b.load_state(dict(PLAN))
    y["slices"]["s2"]["status"] = "done"
    b.save_state(y)
    x["slices"]["s2"]["status"] = "failed"
    with contextlib.redirect_stderr(io.StringIO()):
        a.save_state(x)
    ok("s2 stays DONE", disk(a)["slices"]["s2"]["status"] == "done")

    print("concurrent saves lose nothing (lock + field scope)")
    seed(b)
    q = mp.Queue()
    ps = [mp.Process(target=_hammer, args=(SLICER, home, f, 40, q)) for f in ("ca", "cb")]
    for p in ps:
        p.start()
    for p in ps:
        p.join(60)
    d = disk(a)
    ok("both writers' final counters survive",
       (d["slices"]["s3"].get("ca"), d["slices"]["s3"].get("cb")) == (40, 40))

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'ALL PASSED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    mp.set_start_method("fork", force=True)
    sys.exit(main())
