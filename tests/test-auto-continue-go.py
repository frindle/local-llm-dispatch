#!/usr/bin/env python3
"""Regression: ollama-dispatch-auto no longer STOPS at 'HARNESS GO -- relevance review'.

  1. GO + fail-before/pass-after PASS + relevance measured -> draft --confirm, re-check
     (preflight --auto-seal), `ollama-queue.py enqueue --label <label> --bundle <b>`; the
     whole decision is written to auto-decisions.jsonl and the READY-TO-LAND row is NOT.
  2. contract not PASS / relevance unmeasured / --no-auto-continue / slicer / drafter-cmd
     -> the old pause (nothing confirmed or enqueued).
  3. the re-check after --confirm is NOT a GO -> action "refine" carrying the new outcome
     (a repair round), never a park and never an enqueue.
  4. the enqueue being refused -> pause (the human row), logged.
  5. _preflight_loop wiring: GO reaches auto_continue_go, "enqueued" ends the run with rc 0
     and pause_for_review is never called; "refine" feeds the new NO-GO back into a refine
     round.
  6. repair_state_only_blockers: a baseline-clean-only NO-GO restores EXACTLY the dirty
     tracked product files (never harness files, never untracked), re-runs preflight once
     without a refine round; any other blocker mix is untouched.

Run: python3 test-auto-continue-go.py [--auto PATH]     (prints ALL PASS)
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
AUTO = Path(sys.argv[sys.argv.index("--auto") + 1]) if "--auto" in sys.argv else HERE / "ollama-dispatch-auto"
HOME = Path(tempfile.mkdtemp(prefix="acg-home-"))
os.environ["OLLAMA_DISPATCH_HOME"] = str(HOME)
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load():
    ld = SourceFileLoader("oda_acg", str(AUTO))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("oda_acg", ld))
    sys.argv = [str(AUTO)]
    ld.exec_module(m)
    return m


def go_data(contract="PASS", killed=5):
    return {"verdict": "GO", "blockers": [],
            "checks": [{"check": "fail-before-pass-after", "status": contract, "message": "m"}],
            "verify_relevance": {"killed": killed, "evidence_mutants": 5, "score": 1.0}}


def decisions():
    p = HOME / "auto-decisions.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


class Runner:
    def __init__(self, enqueue_rc=0, confirm_rc=0):
        self.calls, self.enqueue_rc, self.confirm_rc = [], enqueue_rc, confirm_rc

    def __call__(self, cmd, *a, **k):
        self.calls.append([str(x) for x in cmd])
        if "--confirm" in cmd:
            return self.confirm_rc, "confirmed\n", ""
        if "enqueue" in cmd:
            return self.enqueue_rc, "enqueued job abc123\n", "" if self.enqueue_rc == 0 else "refused"
        return 0, "", ""


def args(**kw):
    base = dict(label="lbl", model="m", host="studio", lang="python", bundle=None, drafter_cmd=None,
                no_park=False, no_auto_continue=False, slice_plan=None, slice_id=None, require=[],
                max_rounds=4, no_progress_rounds=2, author_max_iters=24)
    base.update(kw)
    return SimpleNamespace(**base)


def main():
    m = load()
    real_repair = m.repair_state_only_blockers
    wt = Path(tempfile.mkdtemp(prefix="acg-wt-"))
    (wt / "TASK.md").write_text("# t\n")
    m.mark_unconfirmed = lambda *x, **k: (True, "marked")
    m._under_slicer = lambda a=None: False

    # 1. the happy path
    r = Runner()
    act, why, d2 = m.auto_continue_go(wt, "t.py", args(), [], go_data(), runner=r,
                                      rerun=lambda *x: (0, go_data()))
    check("1 GO + contract PASS + relevance measured -> enqueued", act, "enqueued")
    check("1 ... draft --confirm ran first", any("--confirm" in c for c in r.calls[:1]), True)
    enq = next((c for c in r.calls if "enqueue" in c), [])
    check("1 ... enqueue carries --label", enq[enq.index("--label") + 1] if "--label" in enq else None, "lbl")
    check("1 ... enqueue always carries a --bundle (defaults to the label)",
          enq[enq.index("--bundle") + 1] if "--bundle" in enq else None, "lbl")
    check("1 ... enqueue is task-kind coding with bash verify.sh",
          ("--task-kind" in enq and "coding" in enq and "bash verify.sh" in enq), True)
    dec = decisions()
    check("1 ... the decision is logged (kind/decision/label)",
          [(x["kind"], x["decision"], x["label"]) for x in dec], [("go-continue", "enqueued", "lbl")])
    check("1 ... with the measured evidence", "contract PASS" in dec[0]["detail"] and "score 1.0" in dec[0]["detail"], True)

    # 2. every non-applicable case pauses and does nothing
    for name, kw, dd in [("contract UNPROVEN", {}, go_data("UNPROVEN")),
                         ("contract FAIL", {}, go_data("FAIL")),
                         ("no contract check at all", {}, {"verdict": "GO", "checks": []}),
                         ("relevance unmeasured", {}, go_data(killed=None)),
                         ("--no-auto-continue", {"no_auto_continue": True}, go_data()),
                         ("drafter-cmd seam", {"drafter_cmd": "true"}, go_data()),
                         ("--no-park", {"no_park": True}, go_data())]:
        r = Runner()
        act, _w, _d = m.auto_continue_go(wt, "t.py", args(**kw), [], dd, runner=r,
                                         rerun=lambda *x: (0, go_data()))
        check(f"2 {name} -> pause, nothing run", (act, r.calls), ("pause", []))
    m._under_slicer = lambda a=None: True
    r = Runner()
    check("2 under the slicer -> pause", m.auto_continue_go(wt, "t.py", args(), [], go_data(), runner=r)[0], "pause")
    m._under_slicer = lambda a=None: False
    m.is_test_file_target = lambda t: True
    check("2 locked TEST-FILE target (relevance N/A) still auto-continues",
          m.auto_continue_go(wt, "t.test.py", args(), [], go_data(killed=None), runner=Runner(),
                             rerun=lambda *x: (0, go_data(killed=None)))[0], "enqueued")
    m.is_test_file_target = lambda t: False

    # 3. re-check after confirm is not a GO -> refine, never enqueue
    r = Runner()
    nogo = {"verdict": "NO-GO", "blockers": [{"check": "verify-relevance"}], "checks": []}
    act, _w, d2 = m.auto_continue_go(wt, "t.py", args(), [], go_data(), runner=r, rerun=lambda *x: (1, nogo))
    check("3 re-check NO-GO -> refine with the new outcome", (act, d2), ("refine", nogo))
    check("3 ... and nothing was enqueued", any("enqueue" in c for c in r.calls), False)
    r = Runner()
    act, _w, d2 = m.auto_continue_go(wt, "t.py", args(), [], go_data(), runner=r,
                                     rerun=lambda *x: (0, go_data("UNPROVEN")))
    check("3 re-check GO but contract lost -> refine", act, "refine")

    # 4. enqueue refused
    r = Runner(enqueue_rc=1)
    check("4 enqueue refused -> pause", m.auto_continue_go(wt, "t.py", args(), [], go_data(), runner=r,
                                                           rerun=lambda *x: (0, go_data()))[0], "pause")
    r = Runner(confirm_rc=1)
    check("4 draft --confirm failing -> pause, no enqueue",
          (m.auto_continue_go(wt, "t.py", args(), [], go_data(), runner=r)[0],
           any("enqueue" in c for c in r.calls)), ("pause", False))

    # 5. loop wiring
    calls = {"pause": 0, "dispatch": 0}
    seq = [go_data()]
    m.slice_already_satisfied = lambda *x: False
    m.slice_taken_elsewhere = lambda *x: None
    m.driver_gone = lambda a: None
    m.chain_state_write = lambda *x, **k: None
    m.enforce_target_at_head = lambda *x: []
    m.run_preflight = lambda wt_, req, a: (0, seq.pop(0) if seq else go_data())
    m.wait_out_operational_blockers = lambda wt_, req, a, rc, data: (rc, data)
    m.repair_state_only_blockers = lambda wt_, req, a, rc, data: (rc, data)
    m.must_contain_from_task = lambda wt_: []
    m.pause_for_review = lambda *x, **k: calls.__setitem__("pause", calls["pause"] + 1) or 0
    m.auto_continue_go = lambda *x, **k: ("enqueued", "ok", go_data())
    rc = m._preflight_loop(args(), wt, "t.py", "python3 auto-harness-check.py")
    check("5 loop: enqueued ends the run rc 0 without pause_for_review", (rc, calls["pause"]), (0, 0))
    m.auto_continue_go = lambda *x, **k: ("pause", "n/a", go_data())
    rc = m._preflight_loop(args(), wt, "t.py", "python3 auto-harness-check.py")
    check("5 loop: pause falls back to pause_for_review", calls["pause"], 1)
    seen = {}
    gos = [("refine", "x", {"verdict": "NO-GO", "blockers": [{"check": "verify-relevance", "detail": "d"}],
                            "verify_relevance": {"survivors": []}, "checks": []})]
    m.auto_continue_go = lambda *x, **k: gos.pop(0) if gos else ("enqueued", "ok", go_data())
    m.dispatch_model = lambda wt_, prompt, label, v, a, max_iters=None: seen.setdefault("label", label) and (True, "ok")
    m.write_refine_guard = lambda *x, **k: None
    m.clear_refine_guard = lambda *x, **k: None
    m.map_survivor_lines = lambda *x, **k: None
    m.patched_target_text = lambda *x, **k: ""
    m._print_diff = lambda *x, **k: None
    (wt / "refimpl.py").write_text("NEW = ''\n")
    seq[:] = [go_data()]
    rc = m._preflight_loop(args(), wt, "t.py", "python3 auto-harness-check.py")
    check("5 loop: a failed re-check becomes a REFINE round, then continues to enqueue",
          (rc, seen.get("label")), (0, "auto-refine-lbl-r1"))

    # 6. state-only blocker repair
    repo = Path(tempfile.mkdtemp(prefix="acg-repo-"))
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
    g("init", "-q")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    for f, t in {"page.tsx": "orig page\n", "pkg.json": "{}\n", "TASK.md": "task\n", "refimpl.py": "r\n"}.items():
        (repo / f).write_text(t)
    g("add", "-A")
    g("commit", "-qm", "base")
    (repo / "page.tsx").write_text("LEAKED refimpl output\n")
    (repo / "TASK.md").write_text("task edited by refine\n")
    (repo / "stray.txt").write_text("untracked\n")
    reruns = []
    nogo_bc = {"verdict": "NO-GO", "blockers": [{"check": "baseline-clean"}]}
    rc2, d = real_repair(repo, [], args(), 1, nogo_bc,
                                          rerun=lambda *x: reruns.append(1) or (0, go_data()))
    check("6 dirty product file restored to HEAD", (repo / "page.tsx").read_text(), "orig page\n")
    check("6 harness file edit preserved", (repo / "TASK.md").read_text(), "task edited by refine\n")
    check("6 untracked file untouched", (repo / "stray.txt").exists(), True)
    check("6 preflight re-run once and its outcome returned", (len(reruns), d["verdict"]), (1, "GO"))
    check("6 logged", [x["kind"] for x in decisions() if x["kind"] == "repair-baseline-clean"], ["repair-baseline-clean"])
    (repo / "pkg.json").write_text("{\"x\":1}\n")
    mixed = {"verdict": "NO-GO", "blockers": [{"check": "baseline-clean"}, {"check": "verify-relevance"}]}
    reruns.clear()
    rc3, d3 = real_repair(repo, [], args(), 1, mixed, rerun=lambda *x: reruns.append(1) or (0, go_data()))
    check("6 a mixed blocker set is left alone", (reruns, (repo / "pkg.json").read_text()), ([], "{\"x\":1}\n"))

    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
