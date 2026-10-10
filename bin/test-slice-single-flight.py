#!/usr/bin/env python3
"""Single-flight per slice worktree (2026-10-09, rt-costco-receipt-attach-s1).

Live defect: ONE slice (s1) had four author rows (-s1, -s1-r1, -s1-r2, -c1) on one
worktree. The slicer's --execute died (BrokenPipe) -> its AUTO child was re-parented
to pid 1 -> the job poller returned (False, "ORPHANED ...") -> the staged-authoring
loop counted that as a failed model attempt and RE-PROMPTED -> _stop_for_hand did not
know the driver was gone -> a new round was enqueued every ~17s, all before the first
ran. The duplicate-label guard missed them (each round has its own label), so the
slicer's preflight NO-GO'd on cwd-exclusive for a healthy slice and the failed
duplicate raised Needs-attention.

Three layers, each proven red by reverting it:
  1. ollama-dispatch-auto: an orphaned poll stops the whole run (SystemExit
     EXIT_ORPHANED) -- it is not a failed attempt and enqueues nothing further.
  2. ollama-dispatch-auto: hand_abort_reason (-> _stop_for_hand, run before EVERY
     enqueue) refuses once the driver is gone.
  3. ollama-queue.py enqueue: a second LIVE row on a wt-slice-* worktree is refused
     with the duplicate-label protocol line naming the owner (AUTO adopts it).

Run: python3 test-slice-single-flight.py   -> prints SINGLE_FLIGHT_OK
"""
import importlib.util
import inspect
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

BIN = Path(__file__).resolve().parent
failures = []


def ok(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


def load(modname, fname):
    loader = SourceFileLoader(modname, str(BIN / fname))
    spec = importlib.util.spec_from_loader(modname, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def main():
    os.environ.pop("OLLAMA_DISPATCH_NO_SPLIT", None)
    auto = load("oda_singleflight", "ollama-dispatch-auto")
    q = load("oq_singleflight", "ollama-queue.py")

    # ---- layer 3: queue-side owner lookup (pure) --------------------------------------
    with tempfile.TemporaryDirectory() as td:
        wt = Path(td) / "wt-slice-p-s1-x"
        chain = Path(td) / "wt-slice-p-chain"
        repo = Path(td) / "repo"
        for d in (wt, chain, repo):
            d.mkdir()
        jobs = [
            {"id": "aaa", "status": "done", "cwd": str(wt), "label": "old"},
            {"id": "bbb", "status": "planned", "cwd": str(wt), "label": "placeholder"},
            {"id": "ccc", "status": "needs_opus", "cwd": str(wt), "label": "parked"},
            "garbage", None,
        ]
        own = q.slice_worktree_live_owner
        ok("terminal / planned / parked rows do not own the worktree", own(jobs, str(wt)) is None)
        for st in ("pending", "running", "paused", "queued", "held", "scheduled"):
            ok(f"a {st} row owns the slice worktree",
               (own(jobs + [{"id": "live", "status": st, "cwd": str(wt)}], str(wt)) or {}).get("id") == "live")
        live = {"id": "live", "status": "pending", "cwd": str(wt)}
        ok("another worktree is not blocked", own([live], str(wt / ".." / "wt-slice-other")) is None)
        ok("a symlink/relative spelling of the same dir still collides",
           (own([dict(live, cwd=str(wt) + "/.")], str(wt)) or {}).get("id") == "live")
        ok("the shared -chain checkout is not single-flight",
           own([dict(live, cwd=str(chain))], str(chain)) is None)
        ok("a non-slice cwd (repo root) is legitimately shared",
           own([dict(live, cwd=str(repo))], str(repo)) is None)
        ok("a dict-shaped jobs map works", own({"x": live}, str(wt)) is live)

    src_enq = inspect.getsource(q.cmd_enqueue)
    ok("cmd_enqueue consults the owner lookup before minting a job id",
       "slice_worktree_live_owner(state[\"jobs\"], args.cwd)" in src_enq
       and src_enq.index("slice_worktree_live_owner") < src_enq.index("job_id = uuid.uuid4()"))
    ok("...refuses with the duplicate-label protocol line and the duplicate rc",
       'print(f"duplicate-label {_owner[\'id\']}' in src_enq and "_DUPLICATE_LABEL_RC" in src_enq)

    # ---- layer 2: hand_abort_reason knows the driver is gone ---------------------------
    under = SimpleNamespace(slice_plan="alpha", slice_id="s1")
    with tempfile.TemporaryDirectory() as td:
        saved = auto.driver_gone
        try:
            auto.driver_gone = lambda a, **k: "this AUTO was re-parented to pid 1"
            auto.slice_taken_elsewhere = lambda p, s: None
            why = auto.hand_abort_reason(under, td)
            ok("hand_abort_reason refuses once the driver is gone", bool(why) and "DRIVER GONE" in why)
            try:
                auto._stop_for_hand(under, td, "enqueuing x")
                ok("_stop_for_hand raises EXIT_ORPHANED for an orphan", False)
            except SystemExit as e:
                ok("_stop_for_hand raises EXIT_ORPHANED for an orphan", e.code == auto.EXIT_ORPHANED)
            auto.driver_gone = lambda a, **k: None
            ok("a live driver is not refused", auto.hand_abort_reason(under, td) is None)
        finally:
            auto.driver_gone = saved

    # ---- layer 1: the poller stops the run; no failed attempt, no further enqueue ------
    with tempfile.TemporaryDirectory() as td:
        auto._stop_for_hand = lambda *x, **k: None
        calls = []
        auto.capture = lambda cmd, **k: (calls.append(cmd) or (0, "enqueued abc123def456\n", ""))
        auto.time.sleep = lambda s: None
        auto.driver_gone = lambda a, **k: "this AUTO was re-parented to pid 1"
        auto.job_row = lambda j: {"status": "pending"}
        subprocess.run(["git", "init", "-q", td], check=True)
        a = SimpleNamespace(drafter_cmd=None, label="t", slice_plan="alpha", slice_id="s1",
                            timeout=60, bundle=None, model="m", host="h", num_ctx=1,
                            max_tokens=0, lang="ts", author_max_iters=6, repo=td,
                            new_project=None, dest=None, target="x.ts")
        try:
            r = auto.dispatch_model(Path(td), "prompt", "auto-author-t-s1", "true", a)
            ok("an orphaned poll stops the run (SystemExit), it does not return a failed attempt", False)
        except SystemExit as e:
            ok("an orphaned poll stops the run (SystemExit EXIT_ORPHANED)", e.code == auto.EXIT_ORPHANED)
        except Exception as e:  # harness setup problem, not a pass
            ok(f"dispatch_model reached the poller ({type(e).__name__}: {e})", False)
        # and the staged loop must never reach record_attempt / a second dispatch for it
        n = {"dispatch": 0, "record": 0}

        def fake_dispatch(*x, **k):
            n["dispatch"] += 1
            raise SystemExit(auto.EXIT_ORPHANED)
        auto.dispatch_model = fake_dispatch
        auto.record_attempt = lambda *x, **k: n.__setitem__("record", n["record"] + 1)
        auto.stage_plan = lambda a, t: ["task"]
        auto.stage_files = lambda s, l: ["TASK.md"]
        auto.stage_check = lambda *x, **k: (False, "no")
        auto.literal_lint_precheck = lambda *x, **k: None
        auto.chain_state_write = lambda *x, **k: None
        auto.stage_prompt = lambda *x, **k: "p"
        auto.stage_verify_cmd = lambda *x, **k: "true"
        auto.cls_budgets = lambda c: {"reprompts": 2}
        a2 = SimpleNamespace(lang="ts", label="t", author_max_iters=6, require=())
        try:
            auto._author_staged(a2, Path(td), "x.ts", None)
        except SystemExit:
            pass
        ok("the staged loop dispatches ONCE and records no attempt for an orphan",
           n == {"dispatch": 1, "record": 0})

    if failures:
        print(f"\n{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("\nSINGLE_FLIGHT_OK")


if __name__ == "__main__":
    main()
