#!/usr/bin/env python3
"""Round-runaway follow-ups (2026-10-10), all in ollama-dispatch-auto. Pins:
  A. SIGNATURE NORMALISATION: the same failure with different timestamps / tmp paths / durations /
     pids / addresses / line numbers => the SAME signature; genuinely different failing assertions
     (different case, different expected value) => a DIFFERENT one.
  B. SALVAGE: every non-converged round writes worktree diff + last verify tail to
     <auto-runs>/<key>.artifacts/<label>/round-NN-<tag>/ and the path is in the stop and park text.
  C. STOP REASONS: repeated_signature / flap / unsat_no_progress / round_ceiling / iteration_cap /
     external_kill are distinguishable (log_decision stop_reason= and runs[label].stop).
  D. FRESH RESTART (AUTO_FRESH_RESTART=1, default OFF): ONE fresh-context round on a model-authoring
     repeat, WITHOUT the failed history; none for a harness-level (refimpl-unsat) signature.
  E. EXTERNAL_KILL_CODES {129,137,143} are not parked as harness defects.
AUTO_SRC overrides the file under test (baseline revert-proof)."""
import importlib.util, json, os, subprocess, sys, tempfile
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


def guard(name, fn):
    try:
        fn()
    except Exception as e:      # a missing feature at baseline = FAIL, not a crash of the whole file
        print(f"FAIL - {name}  (raised {type(e).__name__}: {e})")
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
tmp = Path(tempfile.mkdtemp(prefix="stop-reasons-"))
m.AUTO_RUNS_DIR = tmp / "runs"
os.environ.pop("AUTO_FRESH_RESTART", None)

DECISIONS = []
m.log_decision = lambda kind, a, decision, detail="", persist=True, **x: DECISIONS.append(
    {"kind": kind, "decision": decision, "detail": detail, **x})


def mk_log(name, *parts):
    p = tmp / name
    p.write_text("\n".join(parts) + "\n")
    return str(p)


def mk_a(**kw):
    d = dict(label="sr", bundle=None, lang="python", intent="x", interface="", require=[],
             drafter_cmd=None, author_max_iters=14, staged="on", task_class="auto", lane="open",
             edit_file=(), model="m", host="h", num_ctx=1, max_tokens=0, timeout=5, repo=str(tmp),
             new_project=None, dest=None, target="app/x.py", author_continue_rounds=2)
    d.update(kw)
    return SimpleNamespace(**d)


def sig(text):
    return m.harness_round_signature(text)


def vblock(*lines):
    return "[worker] verify stdout:\n" + "\n".join(lines) + "\n\n[worker] VERIFY FAILED (exit 1)\n"


# ---- A. normalisation --------------------------------------------------------------------------
def part_a():
    a1 = vblock("FAIL: test_login 2026-10-09T10:11:12Z: /private/var/folders/ab/xyz1/T/tmpa1b2c3/wt/app/x.py:41 "
                "raised KeyError at 0x7f3a9c001a20 pid 4242 in 0.12s")
    a2 = vblock("FAIL: test_login 2026-10-10T23:59:01Z: /private/var/folders/zz/qq77/T/tmpzz99yy/wt/app/x.py:57 "
                "raised KeyError at 0x7f00deadbeef pid 99 in 3.40s")
    chk("same failure, different timestamp/tmp path/line/address/pid/duration => SAME signature",
        sig(a1) == sig(a2), True)
    chk("...the signature is non-empty (not a vacuous equality)", bool(sig(a1)["verify"]), True)
    u1 = vblock("FAIL: Ran 3 tests in 0.002s uuid 123e4567-e89b-12d3-a456-426614174000 deadbeefcafe1234")
    u2 = vblock("FAIL: Ran 3 tests in 1.931s uuid 9b2c1d0e-1111-2222-3333-444455556666 0123456789abcdef")
    chk("uuid / hash / 'Ran N tests in Xs' noise is stripped", sig(u1) == sig(u2), True)
    b1 = vblock("FAIL: expected 3 items, got 4")
    b2 = vblock("FAIL: expected 5 items, got 6")
    chk("different expected/actual values => DIFFERENT signature", sig(b1) == sig(b2), False)
    c1 = vblock("FAIL: test_login rejects empty password")
    c2 = vblock("FAIL: test_logout clears the session cookie")
    chk("different failing assertion => DIFFERENT signature", sig(c1) == sig(c2), False)
    d1 = vblock("FAIL: /tmp/aaa/app/x.py:10 missing parse_id")
    d2 = vblock("FAIL: /tmp/aaa/app/y.py:10 missing parse_id")
    chk("different file (basename) => DIFFERENT signature", sig(d1) == sig(d2), False)
    # the runaway decision sees the noisy repeat as a repeat
    stop, _w = m.round_runaway_decision([sig(a1)], sig(a2))
    chk("a nondeterministic-traceback repeat is detected as identical (stop)", stop, True)


guard("A normalisation", part_a)

# ---- scenario driver: the staged FIXTURE stage with a scripted fake dispatch ---------------------
UNSAT = ('[worker] tool run_bash({"command": "python3 auto-harness-check.py 2>&1 | head -100"}) -> '
         '{"exit_code": 0, "stdout": "  FAIL: the reference impl (refimpl.py) does not make verify.sh '
         'print VERIFY_OK -- the task is unsatisfiable as written\\n=== FAILING CASES ===\\n'
         'FAIL case one\\nFAIL case two\\nFAIL case three')
S1 = "[worker] verify stdout:\nSTAGE_FAIL: aaa one\n"
S2 = "[worker] verify stdout:\nSTAGE_FAIL: bbb two\n"
SCAFFOLD_OK = ('import sys\nCASES = [\n ("a", lambda: 1, 1),\n ("b", lambda: 2, 2),\n ("c", lambda: 3, 3),\n]\n\n'
               'def main():\n    if len(CASES) < 3:\n        print("  SCAFFOLD_INCOMPLETE: need >= 3")\n        return 1\n'
               '    return 0\n\nif __name__ == "__main__":\n    sys.exit(main())\n')


def mk_wt(git=False):
    wt = Path(tempfile.mkdtemp(prefix="wt-", dir=tmp))
    (wt / "TASK.md").write_text("# T\n## Must contain\n- `x`\nOnly edit `app/x.py`\n")
    (wt / "test_fixture.py").write_text("# SCAFFOLD_INCOMPLETE\n" + "pad\n" * 30)
    (wt / "refimpl.py").write_text("# TODO -- the exact text to replace\n")
    if git:
        for c in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"]):
            subprocess.run(["git", "-C", str(wt)] + c, capture_output=True)
    return wt


def run_staged(logs, fresh_ok=False, label="sr", git=False, s3_fail=False):
    """Each job rewrites the fixture and ends nonconverged with the scripted log; a `-fresh` job
    writes a valid fixture and converges when fresh_ok. Returns (result, [(label, prompt)], a, wt)."""
    a = mk_a(label=label)
    wt = mk_wt(git)
    jobs = []

    def fake(wt_, prompt, lab, vc, a_, max_iters=None):
        i = len(jobs)
        jobs.append((lab, prompt))
        a_._chain_last_job = "job%d" % i
        if lab.endswith("-fresh") and fresh_ok:
            (Path(wt_) / "test_fixture.py").write_text(SCAFFOLD_OK)
            return True, "fresh ok"
        if lab.endswith("-s3") and not s3_fail:
            return True, "refimpl ok"
        if lab.endswith("-s3"):
            (Path(wt_) / "refimpl.py").write_text("# edit %d\n" % i)
            a_._last_job_outcome = {"job": "job%d" % i, "status": "failed", "failure_class": "nonconvergence_progressing",
                                    "terminal_reason": "nonconvergence", "log_path": logs[min(i, len(logs) - 1)]}
            return False, "job s3 failed (exit 1)"
        (Path(wt_) / "test_fixture.py").write_text("# SCAFFOLD_INCOMPLETE v%d\n" % i + "pad\n" * 30)
        a_._last_job_outcome = {"job": "job%d" % i, "status": "failed", "failure_class": "nonconvergence_progressing",
                                "terminal_reason": "nonconvergence", "log_path": logs[min(i, len(logs) - 1)]}
        return False, "job x failed (exit 1)"
    m.dispatch_model = fake
    res = m._author_staged(a, wt, "app/x.py", "modify")
    return res, jobs, a, wt


def chain_stop(a):
    fp = m.AUTO_RUNS_DIR / f"{m.chain_key(a)}.json"
    return (json.loads(fp.read_text())["runs"].get(a.label) or {}).get("stop") or {}


def last(kind="spec-defect"):
    xs = [d for d in DECISIONS if d["kind"] == kind and d.get("stop_reason")]
    return xs[-1] if xs else {}


# ---- B + C. salvage + stop reasons ---------------------------------------------------------------
def part_bc():
    DECISIONS.clear()
    res, jobs, a, wt = run_staged([mk_log("a.log", S1), mk_log("b.log", S1)], label="sr-rep", git=True)
    chk("repeat: stops at 2 jobs with a SPEC_DEFECT line", (res[0], len(jobs), res[1].startswith(m.SPEC_DEFECT_ROUTE_PREFIX)),
        (False, 2, True))
    chk("repeated_signature: stop_reason on the logged decision", last().get("stop_reason"), "repeated_signature")
    chk("...and in the chain record runs[label].stop.reason", chain_stop(a).get("reason"), "repeated_signature")
    sal = sorted((m.AUTO_RUNS_DIR / "sr-rep.artifacts" / "sr-rep").glob("round-*"))
    chk("salvage: one artifact dir per non-converged round (2)", len(sal), 2)
    if sal:
        chk("salvage dir holds worktree.diff + verify-tail.txt + meta.json",
            sorted(p.name for p in sal[-1].iterdir()), ["meta.json", "verify-tail.txt", "worktree.diff"])
        chk("worktree.diff carries the model's edit (tracked change)", "SCAFFOLD_INCOMPLETE v1" in (sal[-1] / "worktree.diff").read_text(), True)
        chk("verify-tail.txt carries the real verify output", "STAGE_FAIL: aaa one" in (sal[-1] / "verify-tail.txt").read_text(), True)
        chk("the stop line names the salvage path", str(sal[-1]) in res[1], True)
    # park text surfaces it
    cmds = []
    a._stop_reason = "repeated_signature"
    rec = m.park_visible(a, 1, res[1], runs_dir=m.AUTO_RUNS_DIR, _run=lambda c: (cmds.append(c) or (0, "", "")), log=None)
    chk("park/escalation text carries the salvage path", any(a._last_salvage in " ".join(c) for c in cmds), True)
    chk("park record carries stop_reason + salvage", (rec.get("stop_reason"), rec.get("salvage")), ("repeated_signature", a._last_salvage))

    DECISIONS.clear()
    res, jobs, a, wt = run_staged([mk_log("g.log", S1), mk_log("h.log", S2), mk_log("i.log", S1)], label="sr-flap")
    chk("flap: stop_reason=flap", (len(jobs), last().get("stop_reason"), chain_stop(a).get("reason")), (3, "flap", "flap"))

    DECISIONS.clear()
    res, jobs, a, wt = run_staged([mk_log("u1.log", UNSAT, S1), mk_log("u2.log", UNSAT, S1)], label="sr-unsat")
    chk("refimpl-unsat twice: stop_reason=repeated_signature (identical)", last().get("stop_reason") in
        ("repeated_signature", "unsat_no_progress"), True)
    stop, _w, kind = m.round_runaway_kind([sig(UNSAT)], sig(UNSAT.replace("one", "uno")))
    chk("unsat with same-size case set and a different name => unsat_no_progress", (stop, kind), (True, "unsat_no_progress"))


guard("B+C salvage/stop reasons", part_bc)

REAL_DISPATCH_SRC = None


def part_ceiling():
    m2 = load(AUTO, "auto_ceiling")
    m2.AUTO_RUNS_DIR = tmp / "runs"
    dec = []
    m2.log_decision = lambda kind, a, decision, detail="", persist=True, **x: dec.append({"kind": kind, "decision": decision, **x})
    m2.capture = lambda *x, **k: (1, "", "stubbed: never reach the real queue")
    a = mk_a(label="sr-ceil")
    a._author_rounds_run = 8
    wt = mk_wt(git=True)
    ok, why = m2.dispatch_model(wt, "p", "auto-author-sr-ceil-s2", "true", a)
    chk("ceiling: refused with a ROUND CEILING SPEC_DEFECT", (ok, "ROUND CEILING" in why), (False, True))
    chk("ceiling: stop_reason=round_ceiling (decision + chain record)",
        ([d.get("stop_reason") for d in dec if d["kind"] == "spec-defect"][-1:], chain_stop(a).get("reason")),
        (["round_ceiling"], "round_ceiling"))
    chk("ceiling: a salvage artifact was written and named in the text", bool(a._last_salvage) and a._last_salvage in why, True)


guard("ceiling", part_ceiling)


def part_iteration_cap():
    m3 = load(AUTO, "auto_cap")
    m3.AUTO_RUNS_DIR = tmp / "runs"
    dec = []
    m3.log_decision = lambda kind, a, decision, detail="", persist=True, **x: dec.append({"kind": kind, **x})
    a = mk_a(label="sr-cap")
    wt = mk_wt(git=True)
    n = [0]
    m3.literal_lint_precheck = lambda *x, **k: ""
    m3._harness_check_output = lambda *x, **k: ("self-check output %d" % len(str(n[0])) + "x" * n[0], "")

    def fake(wt_, prompt, lab, vc, a_, max_iters=None):
        n[0] += 1
        (Path(wt_) / "refimpl.py").write_text("# edit %d\n" % n[0])
        a_._chain_last_job = "j%d" % n[0]
        a_._last_job_outcome = {"job": "j%d" % n[0], "status": "failed", "failure_class": "nonconvergence_progressing",
                                "terminal_reason": "nonconvergence",
                                "log_path": mk_log("cap%d.log" % n[0], "[worker] verify stdout:\nSTAGE_FAIL: distinct failure number %s\n" % ("x" * n[0]))}
        return False, "job j%d failed (exit 1)" % n[0]
    m3.dispatch_model = fake
    ok, why = m3._author_with_continuations(a, wt, "app/x.py", "true", resumed_why="job j0 failed")
    chk("continuation rounds exhausted => stop_reason=iteration_cap",
        (ok, [d.get("stop_reason") for d in dec if d.get("stop_reason")][-1:], chain_stop(a).get("reason")),
        (False, ["iteration_cap"], "iteration_cap"))
    chk("...salvaged each continuation round", len(list((m3.AUTO_RUNS_DIR / "sr-cap.artifacts" / "sr-cap").glob("round-*"))) >= 2, True)


guard("iteration cap", part_iteration_cap)


# ---- D. fresh-context restart ---------------------------------------------------------------------
def part_d():
    os.environ.pop("AUTO_FRESH_RESTART", None)
    res, jobs, a, wt = run_staged([mk_log("f1.log", S1), mk_log("f2.log", S1)], fresh_ok=True, label="sr-off")
    chk("flag OFF (default): no fresh round, runaway stops at 2 jobs", (res[0], len(jobs)), (False, 2))

    os.environ["AUTO_FRESH_RESTART"] = "1"
    try:
        DECISIONS.clear()
        res, jobs, a, wt = run_staged([mk_log("f3.log", S1), mk_log("f4.log", S1)], fresh_ok=True, label="sr-on", git=True)
        labs = [j[0] for j in jobs]
        chk("flag ON, model-authoring repeat: exactly ONE fresh round, then the run goes on and converges",
            (res[0], sum(1 for l in labs if l.endswith("-fresh"))), (True, 1))
        fresh_prompt = [p for l, p in jobs if l.endswith("-fresh")][0]
        earlier = [p for l, p in jobs if not l.endswith("-fresh")][-1]
        chk("fresh prompt: failed history OMITTED ('NOT yet passing' / 'PRIOR ATTEMPT FAILED' absent)",
            ("NOT yet passing" in fresh_prompt or "PRIOR ATTEMPT FAILED" in fresh_prompt), False)
        chk("fresh prompt: still carries the task + harness rules", "Must contain" in fresh_prompt and "## Done when" in fresh_prompt, True)
        chk("control: the NON-fresh re-prompt did carry the failure history", "NOT yet passing" in earlier or "PRIOR ATTEMPT FAILED" in earlier, True)
        chk("...a 'fresh-restart' decision was logged", any(d["kind"] == "fresh-restart" for d in DECISIONS), True)
        chk("...and converging cleared the stop (no stop recorded on the chain)", chain_stop(a).get("reason"), None)

        DECISIONS.clear()
        res, jobs, a, wt = run_staged([mk_log("f5.log", S1), mk_log("f6.log", S1), mk_log("f7.log", S1)],
                                      fresh_ok=False, label="sr-on2")
        labs = [j[0] for j in jobs]
        chk("fresh round also fails: stop with stop_reason=fresh_restart_failed after exactly 3 jobs",
            (res[0], len(jobs), sum(1 for l in labs if l.endswith("-fresh")), chain_stop(a).get("reason"),
             res[1].startswith(m.SPEC_DEFECT_ROUTE_PREFIX)), (False, 3, 1, "fresh_restart_failed", True))

        res, jobs, a, wt = run_staged([mk_log("fa.log", S1), mk_log("fb.log", S1), mk_log("fc.log", S1)],
                                      fresh_ok=True, label="sr-once", s3_fail=True)
        chk("a SECOND runaway later in the run gets NO second fresh round (exactly one per run)",
            (res[0], sum(1 for l, _ in jobs if l.endswith("-fresh"))), (False, 1))

        DECISIONS.clear()
        res, jobs, a, wt = run_staged([mk_log("f8.log", UNSAT, S1), mk_log("f9.log", UNSAT, S1)], fresh_ok=True, label="sr-on3")
        chk("flag ON but HARNESS-LEVEL (refimpl-unsat): NO fresh round, straight to SPEC_DEFECT",
            (res[0], len(jobs), any(l.endswith("-fresh") for l, _ in jobs), res[1].startswith(m.SPEC_DEFECT_ROUTE_PREFIX)),
            (False, 2, False, True))
    finally:
        os.environ.pop("AUTO_FRESH_RESTART", None)


guard("D fresh restart", part_d)


# ---- E. external kill -------------------------------------------------------------------------------
def part_e():
    a = mk_a(label="sr-kill")
    for kc in (129, 137, 143):
        chk(f"park: driver killed by signal (exit {kc}) with a job -> no (external kill, not harness)",
            m.park_decision(a, kc, "j"), False)
    chk("park: an ordinary failure with a job still parks", m.park_decision(a, 1, "j"), True)
    chk("EXTERNAL_KILL_CODES == {129,137,143}", set(m.EXTERNAL_KILL_CODES), {129, 137, 143})
    DECISIONS.clear()
    parked = []

    def boom(_a):
        raise SystemExit(143)
    try:
        m.run_chain(a, do=boom, park=lambda *x: parked.append(x))
    except SystemExit:
        pass
    chk("run_chain on exit 143: not parked", parked, [])
    chk("...recorded stop_reason=external_kill (decision + chain record)",
        (last("round-stop").get("stop_reason"), chain_stop(a).get("reason")), ("external_kill", "external_kill"))
    parked.clear()
    try:
        m.run_chain(mk_a(label="sr-fail"), do=lambda _a: (_ for _ in ()).throw(SystemExit(1)), park=lambda *x: parked.append(x))
    except SystemExit:
        pass
    chk("control: run_chain on exit 1 still parks", len(parked), 1)


guard("E external kill", part_e)

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: %s" % FAILS))
sys.exit(1 if FAILS else 0)
