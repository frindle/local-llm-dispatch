#!/usr/bin/env python3
"""Round runaway (2026-10-10, aw-codec-floor s2b-incoming-name-var: ff50902db43f, r1, r2 all 14/14).

The driver kept queuing authoring rounds while the harness-level failure never improved. Pins, in
ollama-dispatch-auto:
  1. two consecutive failed rounds with the SAME "refimpl does not make verify.sh print VERIFY_OK"
     signature => NO third job is enqueued and the run ends with a SPEC_DEFECT line (the
     slicer's re-spec path) plus a persisted, loud auto-decision;
  2. progress is MEASURABLE: strictly fewer failing cases, or a new (non-repeating) verify failure,
     keeps the loop going; a flap back to an earlier failure stops it;
  3. a hard ceiling (AUTHOR_ROUNDS_HARD_MAX) bounds authoring jobs per driver run regardless of
     classification;
  4. the python stage-check accepts the scaffold's own CASES/main() fixture shape (the old text
     test rejected it AND its guard-stripped form: an unsatisfiable pair).
AUTO_SRC overrides the file under test."""
import importlib.util, os, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    ld = SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    argv, sys.argv = sys.argv, [str(path)]
    try:
        ld.exec_module(m)
    finally:
        sys.argv = argv
    return m


m = load(AUTO, "auto_under_test")
tmp = Path(tempfile.mkdtemp(prefix="round-runaway-"))
m.AUTO_RUNS_DIR = tmp / "runs"

UNSAT = ('[worker] tool run_bash({"command": "python3 auto-harness-check.py 2>&1 | head -100"}) -> '
         '{"exit_code": 0, "stdout": "  FAIL: the reference impl (refimpl.py) does not make verify.sh '
         'print VERIFY_OK -- the task is unsatisfiable as written\\n=== FAILING CASES ===\\n'
         'FAIL case one\\nFAIL case two\\nFAIL case three')
STAGE = "[worker] verify stdout:\nSTAGE_FAIL: test_fixture.py defines no test_ function\n\n[worker] VERIFY FAILED (exit 1)\n"


def mk_log(name, *parts):
    p = tmp / name
    p.write_text("\n".join(parts) + "\n")
    return str(p)


def mk_a(**kw):
    d = dict(label="rr", bundle=None, lang="python", intent="x", interface="", require=[],
             drafter_cmd=None, author_max_iters=14, staged="on", task_class="auto", lane="open",
             edit_file=(), model="m", host="h", num_ctx=1, max_tokens=0, timeout=5, repo=str(tmp),
             new_project=None, dest=None, target="app/x.py")
    d.update(kw)
    return SimpleNamespace(**d)


REAL_DISPATCH = m.dispatch_model
DECISIONS = []
m.log_decision = lambda kind, a, decision, detail="", persist=True, **x: DECISIONS.append((kind, decision, detail))


def run_fixture_stage(logs):
    """Drive _author_staged's FIXTURE stage with a fake dispatch: each job rewrites the fixture
    (so it is never a no-write round) and ends nonconverged with the scripted log."""
    a = mk_a(label="rr-%d" % len(DECISIONS))
    wt = Path(tempfile.mkdtemp(prefix="wt-", dir=tmp))
    (wt / "TASK.md").write_text("# T\n## Must contain\n- `x`\nOnly edit `app/x.py`\n")
    (wt / "test_fixture.py").write_text("# SCAFFOLD_INCOMPLETE\n" + "pad\n" * 30)
    (wt / "refimpl.py").write_text("# TODO -- the exact text to replace\n")
    jobs = []

    def fake(wt_, prompt, label, vc, a_, max_iters=None):
        i = len(jobs)
        jobs.append(label)
        (Path(wt_) / "test_fixture.py").write_text("# SCAFFOLD_INCOMPLETE v%d\n" % i + "pad\n" * 30)
        a_._chain_last_job = "job%d" % i
        a_._last_job_outcome = {"job": "job%d" % i, "status": "failed", "failure_class": "nonconvergence_progressing",
                                "terminal_reason": "nonconvergence", "log_path": logs[min(i, len(logs) - 1)]}
        return False, "job x failed (exit 1)"
    m.dispatch_model = fake
    res = m._author_staged(a, wt, "app/x.py", "modify")
    return res, jobs


# ---- 1. the incident: same refimpl-unsat signature twice => no third round -------------------------
same = [mk_log("a.log", UNSAT, STAGE), mk_log("b.log", UNSAT, STAGE), mk_log("c.log", UNSAT, STAGE)]
DECISIONS.clear()
res, jobs = run_fixture_stage(same)
chk("two identical refimpl-unsat rounds: the loop STOPS at 2 jobs (no -r2)", (res[0], len(jobs)), (False, 2))
chk("...routed to the SPEC_DEFECT/re-spec path", res[1].startswith(m.SPEC_DEFECT_ROUTE_PREFIX), True)
chk("...and says why", "ROUND RUNAWAY" in res[1], True)
chk("...LOUD: a persisted spec-defect decision was logged",
    any(k == "spec-defect" and "round runaway" in d for k, d, _ in DECISIONS), True)

# ---- 2. measurable progress keeps going; a flap stops ---------------------------------------------
FEWER = UNSAT.replace("\\nFAIL case two\\nFAIL case three", "")
res, jobs = run_fixture_stage([mk_log("d.log", UNSAT, STAGE), mk_log("e.log", FEWER, STAGE),
                               mk_log("f.log", FEWER.replace("one", "two"), STAGE)])
chk("strictly fewer failing cases = progress: round 3 is allowed to run", len(jobs) >= 3, True)
S1 = "[worker] verify stdout:\nSTAGE_FAIL: aaa one\n"
S2 = "[worker] verify stdout:\nSTAGE_FAIL: bbb two\n"
res, jobs = run_fixture_stage([mk_log("g.log", S1), mk_log("h.log", S2), mk_log("i.log", S1)])
chk("A -> B -> A flap stops at the third job", (res[0], len(jobs), res[1].startswith(m.SPEC_DEFECT_ROUTE_PREFIX)),
    (False, 3, True))
res, jobs = run_fixture_stage([mk_log("j.log", S1), mk_log("k.log", S2), mk_log("l.log", "[worker] verify stdout:\nSTAGE_FAIL: ccc three\n")])
chk("A -> B -> C (new failures) is progress: no runaway stop", "ROUND RUNAWAY" in (res[1] or ""), False)

# ---- 3. hard ceiling -----------------------------------------------------------------------------
a = mk_a(label="ceil")
a._author_rounds_run = 8     # the shipped default ceiling
m.capture = lambda *x, **k: (1, '', 'stubbed: never reach the real queue')
DECISIONS.clear()
ok, why = REAL_DISPATCH(Path(tmp), "p", "auto-author-ceil-s2", "true", a)
chk("hard ceiling: no job enqueued past AUTHOR_ROUNDS_HARD_MAX", (ok, "ROUND CEILING" in why), (False, True))
chk("...loud (decision logged)", any("ceiling" in d for _, d, _ in DECISIONS), True)

# ---- 4. stage-check accepts the scaffold-shaped fixture ------------------------------------------
wt = Path(tempfile.mkdtemp(prefix="wt-", dir=tmp))
scaffold_ok = ('import sys\nCASES = [\n ("a", lambda: 1, 1),\n ("b", lambda: 2, 2),\n ("c", lambda: 3, 3),\n]\n\n'
               'def main():\n    if len(CASES) < 3:\n        print("  SCAFFOLD_INCOMPLETE: need >= 3")\n        return 1\n'
               '    return 0\n\nif __name__ == "__main__":\n    sys.exit(main())\n')
(wt / "test_fixture.py").write_text(scaffold_ok)
chk("scaffold-shaped fixture with 3 cases and the retained guard passes the fixture stage",
    m.stage_check("fixture", wt, "python", "app/x.py")[0], True)
(wt / "test_fixture.py").write_text(scaffold_ok.replace('("a", lambda: 1, 1),\n ("b", lambda: 2, 2),\n ("c", lambda: 3, 3),\n', ""))
chk("an empty CASES list with the guard still fails", m.stage_check("fixture", wt, "python", "app/x.py")[0], False)

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: %s" % FAILS))
sys.exit(1 if FAILS else 0)
