#!/usr/bin/env python3
"""STAGED-AUTHORING CONTRACT regressions (rt-card-bonus-lib-rq, 2026-10-13: s2 714ed9b8cb39 14/14, s2-r1 521f7bb1c5a0).

Root causes pinned here (each is one assertion group, each has a revert proof in pipeline-canary.py seam `stagecontract`):

  A  stage_check(fixture) rejected a fixture whose only SCAFFOLD_INCOMPLETE was the scaffold's own HEADER COMMENT
     (a bare substring test; dispatch_harness_gates already ignores comment lines). The model wrote 16 real cases,
     kept the header, and was told "still contains the SCAFFOLD guard" for 14 iterations. The message now names
     file:line and the offending text, and comment lines never count.
  B  every retry after a stage job that DID write the file carried the NO-WRITE text ("wrote nothing ... produced prose
     and no file write"); the model believed it and rewrote the whole fixture from scratch. A changed-but-red stage now
     gets a STAGE CHECK STILL RED header with the check's message; NO-WRITE stays for a job that really wrote nothing.
  C  the fixture author is given no fixed `now`: a time-dependent spec (daysLeft / upcoming-active-expired) gets the
     WALL_CLOCK_RULE (mock.timers) in its prompt and the TS scaffold template carries the recipe.
  D  the queue filed a harness-authoring round whose END-OF-RUN stage check was still red on a content rule (or whose
     stage job wrote another stage's file) as class=model, so a plain requeue / escalation repeated it. It is `spec`
     now; no-deliverable messages (TODO placeholders, missing/stub/empty/unparseable file) stay `model`.
  E  the driver does not feed a failure_class=spec stage failure to the continuation ladder or the escalation round:
     attempt class `stage-contract` never counts toward should_escalate and _author_entry stops with SPEC_DEFECT.
  F  TASK.md that names a near-miss of the target path (`lib/cardBonaries.ts` for lib/cardBonuses.ts) or never names the
     target fails the TASK stage check.

Usage: python3 test-auto-stage-contract.py [--auto PATH] [--queue PATH] [--scaffold PATH]    Marker: ALL PASS"""
import argparse, importlib.util, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("--auto", default=str(HERE / "ollama-dispatch-auto"))
ap.add_argument("--queue", default=str(HERE / "ollama-queue.py"))
ap.add_argument("--scaffold", default=str(HERE / "ollama-dispatch-scaffold"))
A = ap.parse_args()
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
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


T = Path(tempfile.mkdtemp(prefix="stagecontract-"))
os.environ["HOME"] = str(T)
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
m = load(A.auto, "auto_sc")
q = load(A.queue, "queue_sc")

# ---- A. comment-aware scaffold-guard check, with a line number ------------------------------------------------------
HEADER = ("// Adversarial repo-style test for: x\n//\n// >>> THE ONE THING THE GENERATOR CANNOT WRITE FOR YOU <<<\n"
          "// The single guard case below FAILS (SCAFFOLD_INCOMPLETE) until you author >= 3 real cases\n"
          "/* SCAFFOLD_INCOMPLETE in a block comment\n * SCAFFOLD: cases not yet authored\n */\n")
REAL = ("import { test } from 'node:test';\nimport assert from 'node:assert/strict';\n"
        "test('a', () => { assert.equal(1, 1); });\ntest('b', () => { assert.equal(2, 2); });\n"
        "test('c', () => { assert.equal(3, 3); });\n")
GUARD = "test('SCAFFOLD: cases not yet authored', () => {\n  assert.fail('SCAFFOLD_INCOMPLETE: author cases');\n});\n"


def wt_with(fx_text, lang_file="verify.test.ts"):
    d = Path(tempfile.mkdtemp(prefix="wt-", dir=T))
    (d / lang_file).write_text(fx_text)
    return d


r = m.stage_check("fixture", wt_with(HEADER + REAL), "typescript", "lib/x.ts")
chk("A: scaffold header COMMENT naming the marker is not a live guard", r[0], True)
r = m.stage_check("fixture", wt_with(HEADER + REAL + GUARD), "typescript", "lib/x.ts")
chk("A: a live guard test still fails the fixture stage", r[0], False)
chk("A: ...and the message names file:line and the offending text",
    ("verify.test.ts:" in r[1] and "SCAFFOLD" in r[1] and "delete that test" in r[1]), True)
_ln = (HEADER + REAL + GUARD).splitlines().index("test('SCAFFOLD: cases not yet authored', () => {") + 1
chk("A: ...the line number is the guard's, not the header comment's", f"verify.test.ts:{_ln} " in r[1], True)
chk("A: scaffold_guard_line ignores `#`/`//`/`*` comment lines",
    m.scaffold_guard_line("# SCAFFOLD_INCOMPLETE\n// SCAFFOLD_INCOMPLETE\n * SCAFFOLD_INCOMPLETE\nx = 1\n"), None)
chk("A: ...but finds a code line", m.scaffold_guard_line("# c\nassert.fail('SCAFFOLD_INCOMPLETE')\n"),
    (2, "assert.fail('SCAFFOLD_INCOMPLETE')"))
PYREAL = ("# SCAFFOLD_INCOMPLETE is mentioned in this comment\nCASES = [1, 2, 3]\n"
          "def test_a():\n    assert True\ndef test_b():\n    assert True\n" + "# pad\n" * 5)
chk("A: python fixture whose only marker is a comment passes",
    m.stage_check("fixture", wt_with(PYREAL, "test_fixture.py"), "python", "app/x.py")[0], True)

# ---- B. retry header honesty -----------------------------------------------------------------------------------------
a = SimpleNamespace(label="t-sc", bundle=None, lang="typescript", intent="Add f", interface="", require=[],
                    drafter_cmd=None, author_max_iters=24, staged="on", task_class="auto", lane="open", edit_file=(),
                    model="m", host="h", num_ctx=1, max_tokens=0, timeout=5, repo=str(T), new_project=None, dest=None,
                    target="lib/x.ts")
p_none = m.stage_prompt(a, "lib/x.ts", "fixture", "create", None, 1, None)
chk("B: a no-write retry still carries NO-WRITE", "NO-WRITE" in p_none, True)
p_red = m.stage_prompt(a, "lib/x.ts", "fixture", "create", None, 0, None, red_msg="verify.test.ts:4 still contains the SCAFFOLD guard")
chk("B: a changed-but-red retry says the file WAS written and quotes the check",
    ("STAGE CHECK STILL RED" in p_red and "DID write" in p_red and "still contains the SCAFFOLD guard" in p_red), True)
chk("B: ...and does NOT claim it wrote nothing", ("wrote nothing" in p_red or "NO-WRITE" in p_red), False)

# drive _author_staged: job 1 writes a (still red) fixture; job 2's prompt must be the RED header, not NO-WRITE
def mk_wt():
    d = Path(tempfile.mkdtemp(prefix="wtl-", dir=T))
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    (d / "lib").mkdir()
    (d / "lib" / "x.ts").write_text("export {};\n")
    (d / "TASK.md").write_text("# T\n## Must contain\n- `f`\nOnly edit `lib/x.ts`\n")
    (d / "verify.test.ts").write_text(HEADER + GUARD)
    (d / "refimpl.py").write_text("print('x')  # real\n")
    (d / ".dispatch-harness.json").write_text('{"target": "lib/x.ts", "lang": "typescript"}')
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b"], check=True)
    return d


jobs = []
state = {"n": 0}


def fake_dispatch(wt_, prompt, label, verify_cmd, a_, max_iters=None):
    jobs.append({"label": label, "prompt": prompt})
    i = state["n"]
    state["n"] += 1
    if i == 0:      # writes the fixture but keeps a live guard -> still red
        (Path(wt_) / "verify.test.ts").write_text(HEADER + REAL + GUARD)
        a_._chain_last_job = "j0"
        a_._last_job_outcome = {"job": "j0", "status": "failed", "failure_class": "model", "terminal_reason": "x", "log_path": None}
        return False, "job j0 failed (exit 1)"
    (Path(wt_) / "verify.test.ts").write_text(HEADER + REAL)
    a_._chain_last_job = f"j{i}"
    return True, "done"


m.dispatch_model = fake_dispatch
m.literal_lint_precheck = lambda *x, **k: None
m.salvage_round = lambda *x, **k: None
wt = mk_wt()
a2 = SimpleNamespace(**dict(vars(a), label="t-sc-loop"))
m.chain_state_write = lambda *x, **k: None
res = m._author_staged(a2, wt, "lib/x.ts", "create")
fx_jobs = [j for j in jobs if "write ONLY `verify.test.ts`" in j["prompt"]]
chk("B: the fixture stage ran two jobs (red, then the retry)", len(fx_jobs), 2)
chk("B: the retry after a CHANGED file shows STAGE CHECK STILL RED + the check message",
    ("STAGE CHECK STILL RED" in fx_jobs[1]["prompt"] and "still contains the SCAFFOLD guard" in fx_jobs[1]["prompt"]), True)
chk("B: ...and never the false NO-WRITE claim", "NO-WRITE" in fx_jobs[1]["prompt"], False)

# ---- C. fixed clock guidance -----------------------------------------------------------------------------------------
chk("C: time_dependent_spec fires on the card-bonus intent",
    m.time_dependent_spec("daysLeft = whole days from now to windowEnd; status upcoming if now<windowStart"), True)
chk("C: ...and not on a pure-arithmetic intent", m.time_dependent_spec("Add f(x) that doubles x"), False)
a3 = SimpleNamespace(**dict(vars(a), intent="status: upcoming if now<windowStart; daysLeft never negative"))
ap_time = m.author_prompt(a3, "lib/x.ts")
chk("C: a time-dependent TS spec gets the fixed-clock rule (mock.timers + at(+/-N))",
    ("mock.timers.enable" in ap_time and "at(-N)" in ap_time and "FREEZE THE CLOCK" in ap_time), True)
chk("C: the rule tells which sign opens a window", "windowStart = at(-N)" in ap_time, True)
ap_plain = m.author_prompt(a, "lib/x.ts")
chk("C: a non-time spec does NOT get the clock rule (no prompt bloat)", "FREEZE THE CLOCK" in ap_plain, False)
scaf = Path(A.scaffold).read_text()
chk("C: the TS scaffold fixture template carries the fixed-clock recipe",
    ("mock.timers.enable({{ apis: ['Date'], now: NOW }})" in scaf and "TIME-DEPENDENT SPEC" in scaf), True)

# ---- D. queue classifier ---------------------------------------------------------------------------------------------
def tail(stage_msg, stage="fixture", writes=(), final_verify=True, cap=True):
    t = "".join(f"[worker] tool write_file({{\"content\": \"x\"}}) -> OK: wrote 100 bytes to /wt/{w}\n" for w in writes)
    t += f"[worker] --- iteration {14 if cap else 3}/14 ---\n"
    if final_verify:
        t += (f"[worker] running verify command: python3 /Users/penn/bin/ollama-dispatch-auto --stage-check {stage} --lang typescript\n"
              f"[worker] verify stdout:\nSTAGE_FAIL: {stage_msg}\n\n[worker] VERIFY FAILED (exit 1) (no recognized diagnostics to attribute -- not treated as no-regression).\n")
    else:
        t += (f"[worker] tool task_complete({{\"summary\": \"done\"}}) -> NOT ACCEPTED: the task's verification command still fails.\n"
              f"--- verify output ---\nSTAGE_FAIL: {stage_msg}\n\n--- end ---\n")
    return t


GUARD_MSG = "verify.test.ts:4 still contains the SCAFFOLD guard (`x`) -- delete that test/line and replace it with real cases"
cls = q.classify_failure("nonconvergence", tail(GUARD_MSG, writes=["verify.test.ts"]), authoring=True)
chk("D: authoring cap + final STAGE_FAIL scaffold-guard -> spec (not model)", cls[0], "spec")
chk("D: ...detail says it is not a model limit and a requeue repeats it",
    ("not a model limit" in cls[1] and "plain requeue repeats it" in cls[1] and "iteration cap (14/14)" in cls[1]), True)
chk("D: 'defines no test(...) case' is spec too",
    q.classify_failure("nonconvergence", tail("verify.test.ts defines no test(...) case"), authoring=True)[0], "spec")
chk("D: unfilled TODO placeholders stay model (unfinished deliverable)",
    q.classify_failure("nonconvergence", tail("TASK.md still has TODO placeholders -- fill every one in", stage="task", writes=["TASK.md"]), authoring=True)[0], "model")
chk("D: a missing file stays model", q.classify_failure("nonconvergence", tail("verify.test.ts is missing"), authoring=True)[0], "model")
chk("D: a file that does not parse stays model",
    q.classify_failure("nonconvergence", tail("verify.test.ts does not parse: x"), authoring=True)[0], "model")
chk("D: the SAME log is model for a NON-authoring job",
    q.classify_failure("nonconvergence", tail(GUARD_MSG), authoring=False)[0], "model")
chk("D: a STAGE_FAIL only in an earlier tool echo (no end-of-run verify block) is not evidence",
    q.classify_failure("nonconvergence", tail(GUARD_MSG, final_verify=False), authoring=True)[0], "model")
ww = q.classify_failure("nonconvergence", tail("TASK.md still has TODO placeholders -- fill every one in", stage="task", writes=["refimpl.py"]), authoring=True)
chk("D: a stage-1 job that wrote only refimpl.py (wrong stage's file) -> spec", ww[0], "spec")
chk("D: ...naming the wrong file", "refimpl.py" in ww[1] and "another stage's file" in ww[1], True)
chk("D: a stage-1 job that wrote TASK.md (its own) stays model",
    q.classify_failure("nonconvergence", tail("TASK.md still has TODO placeholders -- fill every one in", stage="task", writes=["refimpl.py", "TASK.md"]), authoring=True)[0], "model")
chk("D: a harness marker still wins over the stage-check rule (precedence harness > spec)",
    q.classify_failure("nonconvergence", tail(GUARD_MSG) + "FAIL: npm ci failed\n", authoring=True)[0], "harness")
for cand in (Path("/Users/penn/bin/ollama-queue-logs/714ed9b8cb39-auto-author-rt-card-bonus-lib-rq-s2.log"),):
    if cand.is_file():
        chk("D: the REAL 714ed9b8cb39 log (live evidence) classifies spec, not model",
            q.classify_failure("nonconvergence", q._log_tail(str(cand)), authoring=True)[0], "spec")
for cand in (Path("/Users/penn/bin/ollama-queue-logs/5be45b107cea-auto-author-pa-supersede-policy-rq-s1.log"),):
    if cand.is_file():
        chk("D: the REAL pa-supersede s1 5be45b107cea log (wrote refimpl.py in stage 1) classifies spec",
            q.classify_failure("nonconvergence", q._log_tail(str(cand)), authoring=True)[0], "spec")

# ---- E. driver: no escalation / ladder for a spec-class stage failure ----------------------------------------------------
ha = SimpleNamespace(label="t-sc-rec", bundle=None, model="m", _last_job_outcome={"job": "j", "failure_class": "spec"})
rd = Path(tempfile.mkdtemp(prefix="runs-", dir=T))
r1 = m.record_attempt(ha, "j1", "job j1 failed (exit 1)", None, runs_dir=rd)
r2 = m.record_attempt(ha, "j2", "job j2 failed (exit 1)", None, runs_dir=rd)
chk("E: a failure_class=spec attempt is recorded as class stage-contract", (r1 or {}).get("class"), "stage-contract")
chk("E: two stage-contract attempts do NOT trigger the escalation round",
    m.should_escalate(m.load_attempts(ha, rd), pid=1), False)
hm = SimpleNamespace(label="t-sc-rec2", bundle=None, model="m", _last_job_outcome={"job": "j", "failure_class": "model"})
m.record_attempt(hm, "j1", "job j1 failed (exit 1)", None, runs_dir=rd)
m.record_attempt(hm, "j2", "job j2 failed (exit 1)", None, runs_dir=rd)
chk("E: two plain model attempts still escalate (unchanged)", m.should_escalate(m.load_attempts(hm, rd), pid=1), True)
# _author_entry stops with SPEC_DEFECT instead of the ladder
calls = []
m._author_staged = lambda a_, wt_, t_, c_: (False, "staged stage `fixture` did not converge: job j2 failed (exit 1)")
m._author_with_continuations = lambda *x, **k: calls.append("ladder") or (False, "ladder")
m.worker_exit_failure = lambda *x, **k: None
m.log_decision = lambda *x, **k: None
m.last_job_non_attempt = lambda *x, **k: False
he = SimpleNamespace(**dict(vars(a), label="t-sc-entry"))
he._last_job_outcome = {"job": "j", "failure_class": "spec"}
he._task_class = "create"
out = m._author_entry(he, Path(T), "lib/x.ts", "true")
chk("E: _author_entry stops with a SPEC_DEFECT line for a failure_class=spec stage failure",
    (out[0], out[1].startswith(m.SPEC_DEFECT_ROUTE_PREFIX), calls), (False, True, []))
he2 = SimpleNamespace(**dict(vars(a), label="t-sc-entry2"))
he2._last_job_outcome = {"job": "j", "failure_class": "model"}
he2._task_class = "create"
m._author_entry(he2, Path(T), "lib/x.ts", "true")
chk("E: a model-class stage failure still falls back to the ladder (unchanged)", calls, ["ladder"])

# ---- F. TASK.md must name the target exactly ------------------------------------------------------------------------
TASK_OK = "# T\n## Required change\nCreate `lib/cardBonuses.ts` (+ `lib/cardBonuses.test.ts`).\n## Must contain\n- `f`\nOnly edit `lib/cardBonuses.ts`\n"
TASK_TYPO = TASK_OK.replace("Create `lib/cardBonuses.ts`", "Create `lib/cardBonaries.ts`")
TASK_NONAME = "# T\n## Must contain\n- `f`\nOnly edit `lib/other.ts`\n"
for nm, txt, want in (("exact target + a .test sibling", TASK_OK, True), ("near-miss target path", TASK_TYPO, False),
                      ("target never named", TASK_NONAME, False)):
    d = Path(tempfile.mkdtemp(prefix="wtt-", dir=T))
    (d / "TASK.md").write_text(txt)
    r = m.stage_check("task", d, "typescript", "lib/cardBonuses.ts", ["f"])
    chk(f"F: task stage: {nm} -> {'pass' if want else 'fail'}", r[0], want)
    if not want:
        chk(f"F: ...message names the problem ({nm})", ("near-miss" in r[1] or "never names" in r[1]), True)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS))
    sys.exit(1)
print("ALL PASS")
