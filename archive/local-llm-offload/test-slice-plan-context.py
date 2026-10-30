#!/usr/bin/env python3
"""Intermediate-slice plan context + deferred-concern obligations (2026-10-03).

Pinned on the REAL artifacts of 143d9acfe43a (replay-endorse s2): its authoritative
gate verdict was `concerns` on one review finding ("the original global dedup by
trackingNumber is removed ...") that dependent slice s3 is planned to establish; the
chain parked and the coordinator --accept-slice'd it by hand.

  pure (slice_obligations)
    * s2's transitive dependents come from the plan; a leaf slice has none
    * the reviewer is told the plan for an intermediate slice; a leaf gets ''
    * decide_deferral defers ONLY: tagged -> a transitive dependent, shared term,
      review/code/non-high, verdict concerns, own verify green, nothing else
  gate (merge_review, authoritative regate, sandboxed)
    * s2 report with the [restored-by: s3] tag -> verdict pass, obligation OPEN on s3
    * REVERT-CHECKS (a genuinely bad input still escalates -> verdict stays concerns):
        untagged finding / tag to a NON-dependent / own verify red / extra
        completeness finding / a code HIGH
    * s3's review answering NOT-RESTORED -> code HIGH -> s3 fails
    * s3 terminal pass + verify green -> obligation DISCHARGED with evidence
  slicer
    * --land-integration REFUSES while an obligation is open

Usage: python3 ~/bin/test-slice-plan-context.py
Revert: GATE=<gate .bak> python3 this.py -> FAIL.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
FX = HERE / "test-fixtures-slice-plan-context"
GATE = Path(os.environ.get("GATE", HERE / "gate-on-complete.py"))
SLICE = Path(os.environ.get("SLICE", HERE / "ollama-dispatch-slice"))
sys.path.insert(0, str(HERE))
fails = 0

S2 = "s2-per-reservation-collapse"
S3 = "s3-endorsement-selection"
RUN = "replay-endorse"
FINDING = ("The original global deduplication by trackingNumber is removed, allowing "
           "multiple links with the same tracking number to survive")


def chk(name, actual, expected):
    global fails
    ok = actual == expected
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f": expected {expected!r} got {actual!r}"))
    fails += 0 if ok else 1


def load(path, name):
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _boom(*a, **k):
    raise RuntimeError("test must not reach a live side effect")


SANDBOX = Path(tempfile.mkdtemp(prefix="planctx-"))
RUNS = SANDBOX / "slice-runs"
RUNS.mkdir()
shutil.copy(FX / "plan.json", SANDBOX / "plan.json")
(RUNS / f"{RUN}.json").write_text(json.dumps({
    "label": RUN, "plan_path": str(SANDBOX / "plan.json"), "target": "lib/bfmrLinkReconcile.ts",
    "slices": {s["id"]: {"status": "pending"} for s in
               json.loads((FX / "plan.json").read_text())["slices"]}}))

so = load(HERE / "slice_obligations.py", "so_t")
runs = so.load_runs(RUNS)
ctx2 = so.slice_context(f"{RUN}-{S2}", runs)
chk("s2 is found in its run", (ctx2 or {}).get("sid"), S2)
chk("s2's transitive dependents come from the plan",
    (ctx2 or {}).get("dependents"),
    ["s3-endorsement-selection", "s4b-integrate-collapse", "s4c-integrate-endorsement",
     "s5-order-906-stale-mislink"])
ctx5 = so.slice_context(f"auto-refine-{RUN}-s5-order-906-stale-mislink-r2", runs)
chk("a leaf slice (s5) has no dependents (authoring wrapper label resolves too)",
    (ctx5 or {}).get("dependents"), [])
txt = so.plan_context_text(ctx2)
chk("reviewer context says intermediate + names s3",
    ("INTERMEDIATE" in txt, S3 in txt, "[restored-by:" in txt), (True, True, True))
chk("a leaf slice gets no plan context", so.plan_context_text(ctx5), "")
chk("a non-slice job gets no context", so.slice_context("rt-costco-fix", runs), None)

# ---------------- gate -------------------------------------------------------
gate = load(GATE, "gate_t")
gate._escalate_regate = _boom
(SANDBOX / "ollama-queue-logs" / "auto-fix").mkdir(parents=True)
gate.BIN = SANDBOX
gate.COMPLETED_ROOT = SANDBOX / "Completed"
gate.TEST_MODE = True
gate._SLICE_RUNS_DIR = RUNS
gate.subprocess = types.SimpleNamespace(run=_boom, Popen=_boom, PIPE=-1, DEVNULL=-3,
                                        TimeoutExpired=Exception, CalledProcessError=Exception)
chk("gate builds the plan context for the s2 job", S3 in gate._slice_review_context(f"{RUN}-{S2}"), True)
chk("gate: non-slice job context is '' (byte-identical review task)",
    gate._slice_review_context("some-other-job"), "")

REAL = json.loads((FX / "s2-gate.json").read_text())


def report(rows, verdict="PASS WITH CAVEATS", extra=""):
    body = "\n".join(f"| {n} | {sev} | `{where}` | {what} |" for n, (sev, where, what) in enumerate(rows, 1))
    return f"## VERDICT: {verdict}\n\n| # | sev | where | what |\n|---|---|---|---|\n{body}\n{extra}\n"


def merge(parent, label, rep, exit_code=0, extra_issues=()):
    td = Path(tempfile.mkdtemp(dir=SANDBOX))
    pl = json.loads(json.dumps(REAL))
    pl["issues"] = list(extra_issues)
    for k in ("gate_authority", "regate", "counts", "review_verdict", "auto_fix_class",
              "auto_fix_action", "auto_fix_escalated", "untrusted"):
        pl.pop(k, None)
    pl["verdict"] = "pass-pending-review"
    pl["job_label"] = label
    pl["job_exit_code"] = exit_code
    (td / f"{parent}.gate.json").write_text(json.dumps(pl))
    rd = td / f"regate-{parent}"
    rd.mkdir()
    (rd / "report.md").write_text(rep)
    a = types.SimpleNamespace(job_label=f"regate-{parent}", cwd=str(rd), job_id="t0", out_dir=str(td))
    try:
        gate.merge_review(a, td, prefix="regate-", authoritative=True)
    except RuntimeError as e:
        print("   (merge stopped at a guarded side effect:", e, ")")
    return json.loads((td / f"{parent}.gate.json").read_text())


def obls():
    return so.load_obligations(RUN, RUNS)


W = "lib/bfmrLinkReconcile.ts:40"
p = merge("s2bad1", f"{RUN}-{S2}", report([("medium", W, FINDING)]))
chk("REVERT: untagged finding -> stays concerns", p.get("verdict"), "concerns")
chk("...refusal reason recorded", "did not tag" in str(p.get("deferral_refused")), True)
p = merge("s2bad2", f"{RUN}-{S2}", report([("medium", W, FINDING + " [restored-by: s1-normalize-tracking]")]))
chk("REVERT: tag to a NON-dependent (s1) -> stays concerns", p.get("verdict"), "concerns")
p = merge("s2bad3", f"{RUN}-{S2}", report([("medium", W, FINDING + f" [restored-by: {S3}]")]), exit_code=1)
chk("REVERT: own verify red -> never deferred", p.get("verdict") in ("concerns", "fail"), True)
chk("...and no obligation written", obls(), [])
p = merge("s2bad4", f"{RUN}-{S2}", report([("medium", W, FINDING + f" [restored-by: {S3}]")]),
          extra_issues=[{"severity": "medium", "category": "input", "source": "completeness",
                         "file": "", "what": "must_contain literal already at baseline"}])
chk("REVERT: an extra non-review finding blocks deferral", p.get("verdict"), "concerns")
p = merge("s2bad5", f"{RUN}-{S2}", report([("high", W, FINDING + f" [restored-by: {S3}]")]))
chk("REVERT: a code HIGH is never deferred (fails)", p.get("verdict"), "fail")
p = merge("s2bad6", f"{RUN}-{S2}", report([("medium", W, "Uses tabs instead of spaces "
                                                         f"[restored-by: {S3}]")]))
chk("REVERT: tag with no content term shared with s3 -> stays concerns", p.get("verdict"), "concerns")
chk("no obligation from any refused case", obls(), [])

p = merge("s2good", f"{RUN}-{S2}", report([("medium", W, FINDING + f" [restored-by: {S3}]")]))
chk("s2 tagged + mappable + verify green -> verdict pass", p.get("verdict"), "pass")
chk("...was concerns, recorded", p.get("verdict_before_deferral"), "concerns")
chk("...mapping recorded (restored_by + shared terms)",
    [(d["restored_by"], bool(d["shared_terms"])) for d in p.get("deferred_concerns") or []], [(S3, True)])
chk("...the tag is stripped from the stored finding text", "[restored-by" in json.dumps(p.get("issues")), False)
o = obls()
chk("one OPEN obligation on s3", [(x["restored_by"], x["status"]) for x in o], [(S3, "open")])
oid = o[0]["oid"] if o else "none"
chk("s3's reviewer is shown the inherited concern",
    oid in gate._slice_review_context(f"{RUN}-{S3}"), True)
chk("...and so is a gate AUTO-FIX round of s3 ('<label> [auto-fix r1]')",
    oid in gate._slice_review_context(f"{RUN}-{S3} [auto-fix r1]"), True)
chk("an auto-fix round label resolves to its slice",
    (so.slice_context(f"{RUN}-{S2} [auto-fix r2]", runs) or {}).get("sid"), S2)

p3 = merge("s3bad", f"{RUN}-{S3}", report([], verdict="PASS", extra=f"NOT-RESTORED: {oid} -- still dedups per reservation only"))
chk("s3 answering NOT-RESTORED -> code HIGH -> fail", p3.get("verdict"), "fail")
chk("...obligation still open", [x["status"] for x in obls()], ["open"])

# slicer: land refused while open
sl = load(SLICE, "slice_t")
st = {"label": RUN, "integration": {"status": "staged"}}
sl.STATE_ROOT = str(RUNS)
died = []
sl.die = lambda m: (_ for _ in ()).throw(SystemExit(m))
try:
    sl.land_integration(st, ff_land=_boom, pusher=_boom, push=False)
except SystemExit as e:
    died.append(str(e))
chk("--land-integration REFUSES while an obligation is open",
    bool(died) and "still OPEN" in died[0] and oid in died[0], True)

p3 = merge("s3good", f"{RUN}-{S3}", report([], verdict="PASS", extra=f"RESTORED: {oid} -- endorsement keeps one per group"))
chk("s3 green PASS -> pass", p3.get("verdict"), "pass")
chk("...discharges the obligation with evidence",
    [(x["status"], x.get("discharged_by_job"), (x.get("discharge_evidence") or {}).get("job_exit_code"))
     for x in obls()], [("discharged", "s3good", 0)])
chk("--land-integration no longer blocked by obligations", sl.open_slice_obligations(st, root=str(RUNS)), [])

shutil.rmtree(SANDBOX, ignore_errors=True)
print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
