#!/usr/bin/env python3
"""Behavioural tests for the author flow after rt-egift-link-s1-s4 (2026-10-06):
  (C) refimpl-first: while refimpl.py is the scaffold stub the self-check refuses everything
      else (the model burned 24 iterations on a fixture and never wrote the refimpl);
      the Must-contain list is capped relative to the plan's pinned literals;
  (A) when the refimpl fails its OWN verify the self-check names EVERY failing case (not only
      those in the output tail), says COMMON CAUSE instead of 'suspect the fixture' when
      several fail at once, and flags body-shape conflicts; AUTO stops its continuation
      ladder (PLAN SUSPECT) on an identical majority failing set; counters name the real cap.
Run: python3 ~/bin/test-auto-unsat-harness.py  (AUTO_SRC=<path> to test another copy;
--revert-check: AUTO_SRC=ollama-dispatch-auto.bak-20261006T181500Z-unsat must FAIL)."""
import importlib.util, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

SRC = os.environ.get("AUTO_SRC") or os.path.expanduser("~/bin/ollama-dispatch-auto")
ld = SourceFileLoader("oda_u", SRC)
oda = importlib.util.module_from_spec(importlib.util.spec_from_loader("oda_u", ld))
ld.exec_module(oda)
FAILS = []


def chk(name, cond, detail=""):
    print(("ok   - " if cond else "FAIL - ") + name + ("" if cond else "  " + str(detail)[:400]))
    if not cond:
        FAILS.append(name)


def build(refimpl, verify, task_extra="", requires=(), fixture="test_fixture.py", fx_text="# fixture\n"):
    wt = tempfile.mkdtemp(prefix="unsat-")
    subprocess.run(["git", "init", "-q", wt], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", wt, "config", k, v], check=True)
    Path(wt, "TASK.md").write_text("## Must contain\n- `MARK`\n" + task_extra + "\nOnly edit `target.txt`; do not edit verify.sh\n")
    Path(wt, "verify.sh").write_text(verify)
    Path(wt, "refimpl.py").write_text(refimpl)
    Path(wt, "target.txt").write_text("stub\n")
    Path(wt, fixture).write_text(fx_text)
    subprocess.run(["git", "-C", wt, "add", "-A"], check=True)
    subprocess.run(["git", "-C", wt, "commit", "-q", "-m", "base"], check=True)
    oda.write_harness_check(Path(wt), "python", requires=list(requires))
    return wt


def run(wt):
    r = subprocess.run(["python3", "auto-harness-check.py"], cwd=wt, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


GOOD_RI = "open('target.txt','w').write('MARK\\n')\n"
# 6 cases; case 1 is the only one that passes with the refimpl; 60 lines of noise follow so the
# 1500-char tail shows only the LAST case.
VERIFY_5OF6 = (
    'if grep -q GOOD target.txt 2>/dev/null; then echo VERIFY_OK; exit 0; fi\n'
    + "".join(f"echo 'not ok {i} - case number {i}'\n" if i != 1 else "echo 'ok 1 - fine'\n"
              for i in range(1, 7))
    + "i=0; while [ $i -lt 70 ]; do echo \"# noise line $i padding padding padding\"; i=$((i+1)); done\n"
    + "echo 'not ok 6 - case number 6'\nexit 1\n")

# --- (C) refimpl first --------------------------------------------------------------
STUB = "# TODO -- the exact text to replace\n"
wt = build(STUB, "echo nothing; exit 1\n")
rc, out = run(wt)
chk("stub refimpl: check fails with REFIMPL FIRST", rc != 0 and "REFIMPL FIRST" in out, out)
wt = build(GOOD_RI, 'grep -q MARK target.txt && echo VERIFY_OK || { echo "no MARK"; exit 1; }\n')
rc, out = run(wt)
chk("real refimpl: no REFIMPL FIRST, check passes", rc == 0 and "VERIFY_OK" in out, out)

# --- must-contain cap -----------------------------------------------------------------
many = "".join(f"- `lit{i}`\n" for i in range(12))
wt = build(GOOD_RI, 'grep -q MARK target.txt && echo VERIFY_OK || exit 1\n', task_extra=many, requires=["MARK"])
rc, out = run(wt)
chk("13 literals vs 1 plan literal: cap refuses", rc != 0 and "plan pins only 1" in out, out)
wt = build(GOOD_RI, 'grep -q MARK target.txt && echo VERIFY_OK || exit 1\n', task_extra="- `lit1`\n- `lit2`\n", requires=["MARK"])
rc, out = run(wt)
chk("3 literals vs 1 plan literal: within cap", "plan pins only" not in out, out)

# --- (A) every failing case + COMMON CAUSE ---------------------------------------------
wt = build(GOOD_RI, VERIFY_5OF6)
rc, out = run(wt)
names = [f"case number {i}" for i in (2, 3, 4, 5, 6)]
chk("all 5 failing cases named", all(f"FAILED CASE: {n}" in out for n in names), out[-900:])
chk("summary block says 5 of 6", "5 of 6 failed with refimpl.py applied" in out, out[-900:])
chk("COMMON CAUSE hint for multi-case failure", "COMMON CAUSE" in out, out[-900:])
rc, out2 = run(wt)   # second run: same cases repeat
chk("repeat run still COMMON CAUSE, not 'SUSPECT THE FIXTURE'",
    "COMMON CAUSE" in out2 and "SUSPECT THE FIXTURE" not in out2, out2[-900:])
# single failing case repeated keeps the old fixture hint
V1 = ('if grep -q GOOD target.txt 2>/dev/null; then echo VERIFY_OK; exit 0; fi\n'
      "echo 'ok 1 - fine'\necho 'not ok 2 - lone case'\nexit 1\n")
wt = build(GOOD_RI, V1)
run(wt); rc, out3 = run(wt)
chk("one repeating case keeps SUSPECT THE FIXTURE", "SUSPECT THE FIXTURE" in out3 and "COMMON CAUSE" not in out3, out3[-700:])

# --- shape conflict hints ----------------------------------------------------------------
wt = build("open('target.txt','w').write('MARK\\nconst q = req.nextUrl.searchParams\\n')\n", VERIFY_5OF6,
           task_extra="The PUT body is JSON.\n", fx_text="const r = new Request('http://x', {method:'PUT', body: new FormData()});\n")
rc, out = run(wt)
chk("FormData-vs-JSON conflict flagged", "SHAPE CONFLICT" in out and "FormData" in out, out[-1200:])
chk("nextUrl-vs-plain-Request conflict flagged", "nextUrl" in out.split("SHAPE CONFLICT")[-1], out[-1200:])

# --- AUTO: PLAN SUSPECT --------------------------------------------------------------------
block = "=== FAILING CASES (%d of %d failed with refimpl.py applied) ===\n" + "".join("  FAILED CASE: c%d\n" % i for i in range(5))
cases, total = oda.unsat_cases(block % (5, 6))
chk("unsat_cases parses names + total", cases == {"c0", "c1", "c2", "c3", "c4"} and total == 6, (cases, total))
chk("plan_suspect: identical majority set", oda.plan_suspect(cases, cases, total) is True)
chk("plan_suspect: first round (no previous) is not suspect", oda.plan_suspect(None, cases, total) is False)
chk("plan_suspect: changed set is progress", oda.plan_suspect({"c0"}, cases, total) is False)
chk("plan_suspect: minority/single case is not suspect", oda.plan_suspect({"c0"}, {"c0"}, 6) is False)
chk("PLAN SUSPECT routes to die, not autoslice", oda._failure_route(oda.PLAN_SUSPECT_PREFIX + "x") == "die")

# --- counters name the real cap ----------------------------------------------------------
_sd = Path(os.path.expanduser("~/.ollama-dispatch/slice-runs")); _sd.mkdir(parents=True, exist_ok=True)
_sf = _sd / "unsat-test-plan.json"
_sf.write_text(__import__("json").dumps({"slices": {"s4": {"author_attempts": 2,
                                                         "author_job_ids": list("abc"), "author_jobs_at_retry": 0}}}))
try:
    note = oda.escalation_point_note("unsat-test-plan", "s4")
finally:
    _sf.unlink()
chk("counter note names attempts + job budget",
    "job #4 of 8 allowed" in note and "attempt 3 of 5" in note and "ESCALATES" in note, note)
slice_src = os.path.expanduser("~/bin/ollama-dispatch-slice")
txt = open(slice_src).read()
import re
chk("auto mirrors the slice caps (5 attempts / 8 jobs)",
    re.search(r"^MAX_AUTHOR_ATTEMPTS = (\d+)", txt, re.M).group(1) == str(oda.SLICE_MAX_AUTHOR_ATTEMPTS)
    and re.search(r"^MAX_AUTHOR_JOBS = (\d+)", txt, re.M).group(1) == str(oda.SLICE_MAX_AUTHOR_JOBS))

print("ALL PASS" if not FAILS else "FAILED: " + "; ".join(FAILS))
sys.exit(1 if FAILS else 0)
