#!/usr/bin/env python3
"""Literal lint (2026-10-09, rt-walmart-cancel-import): a `## Must contain` literal that the
base file spells slightly differently (`normalize(orderNumber)` vs the real
`normalize(s.orderNumber)`) is a SPEC defect, not something for the model to chase.

 (a) hg.literal_near_miss / literal_near_miss_findings  -- "did you mean", at authoring + stage entry
 (b) preflight refimpl-satisfies: literal absent with the refimpl APPLIED -> SPEC_DEFECT, not WARN
 (c) hg.missing_literals_in_text / repeat_missing_literals + auto.missing_literal_round:
     the identical missing set on two consecutive failed rounds -> SPEC_DEFECT, no third round
 plus the routing (a SPEC_DEFECT why dies, is never auto-sliced / escalated / ladder-fallen-back).

  test-literal-lint.py                 # run
  test-literal-lint.py --revert-check  # disable each gate in turn; every one must go RED
"""
import importlib.util, os, subprocess, sys, tempfile, types
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
REVERT = "--revert-check" in sys.argv
FAILS = []


def check(name, ok, extra=""):
    print(("ok  " if ok else "FAIL") + ": " + name + ("" if ok else "  " + str(extra)[:300]))
    if not ok:
        FAILS.append(name)


def load(path, name):
    ld = SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    old = sys.argv
    sys.argv = [str(path)]
    try:
        ld.exec_module(m)
    finally:
        sys.argv = old
    return m


hg = load(HERE / "dispatch_harness_gates.py", "hg_t")
if REVERT:      # gates switched off: the suite must go RED
    hg.literal_near_miss = lambda *a, **k: None
    hg.repeat_missing_literals = lambda *a, **k: False
BODY = """\
import { normalize } from '@/lib/orders';
const skipSet = new Set(skipList.map(s => normalize(s.orderNumber)));
const key = normalize(r.orderNumber);
const x = existing.cost !== 0 ? existing.cost : r.cost;
let cancelled = false;
"""


def repo_with(body, task, refimpl=""):
    d = Path(tempfile.mkdtemp(prefix="litlint-"))
    g = lambda *a: subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True)
    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (d / "app").mkdir()
    (d / "app/route.ts").write_text(body)
    g("add", "-A"); g("commit", "-q", "-m", "base")
    (d / "TASK.md").write_text(task)
    (d / "refimpl.py").write_text(refimpl)
    return d


TASK = ("# TASK\n## Required change\nmark cancelled\n## Must contain\n"
        "- `normalize(orderNumber)`\n- `cancelledMarked`\n- `cancelled = true`\n- `existing.cost !== 0`\n"
        "Only edit `app/route.ts`\n")

# ---- (a) near-miss detection --------------------------------------------------------------
nm = hg.literal_near_miss("normalize(orderNumber)", BODY)
check("(a) normalize(orderNumber) near-misses normalize(s.orderNumber)/(r.orderNumber)",
      nm is not None and "orderNumber)" in nm[2] and nm[2] in ("normalize(s.orderNumber)", "normalize(r.orderNumber)"), nm)
check("(a) a verbatim literal is not a near miss", hg.literal_near_miss("normalize(r.orderNumber)", BODY) is None)
check("(a) a brand-new identifier is not a near miss", hg.literal_near_miss("cancelledMarked", BODY) is None)
check("(a) a SUBSTITUTION (= true vs = false) is not a near miss: that is a legitimate new literal",
      hg.literal_near_miss("cancelled = true", BODY) is None)
check("(a) short tokens never qualify", hg.literal_near_miss("cost", BODY) is None)
d = repo_with(BODY, TASK)
fs = hg.literal_near_miss_findings(d, TASK, "app/route.ts")
check("(a) findings: exactly the normalize(orderNumber) literal, with a did-you-mean",
      len(fs) == 1 and fs[0]["code"] == hg.LITERAL_NEAR_MISS and "did you mean `normalize(" in fs[0]["message"], fs)
check("(a) the refimpl text spelling the literal on purpose suppresses the finding",
      hg.literal_near_miss_findings(d, TASK, "app/route.ts", "x = 'normalize(orderNumber)'") == [])
sd = hg.spec_defects(d, target="app/route.ts", creation=False, task_text=TASK)
check("(a) spec_defects() (authoring precheck + preflight) carries it as SPEC_DEFECT",
      any(f["code"] == hg.LITERAL_NEAR_MISS for f in sd) and hg.SPEC_DEFECT_PREFIX in hg.format_spec_defects(sd), sd)
TASK_OK = TASK.replace("- `normalize(orderNumber)`\n", "- `normalize(r.orderNumber)`\n")
check("(a) the corrected spec is clean", hg.literal_near_miss_findings(d, TASK_OK, "app/route.ts") == [])

# ---- (c) retry accounting -----------------------------------------------------------------
LOG = ("[worker] tool run_bash(...) -> MISSING literal in app/route.ts (searched for these exact 3 chars, "
       "shown verbatim between the >>> <<< marks): >>>zzz<<<\n[worker] verify stdout:\n  FAIL: refimpl\n"
       "  MISSING literal in app/route.ts (searched for these exact 22 chars, shown verbatim between the "
       ">>> <<< marks): >>>normalize(orderNumber)<<<\n  8/9 literal(s) present\n[worker] VERIFY FAILED\n")
ms = hg.missing_literals_in_text(LOG)
check("(c) parser reads the literal, not the '>>> <<< marks' explanation",
      ('app/route.ts', 'normalize(orderNumber)') in ms and not any(l.strip() == "" for _f, l in ms), ms)
auto = load(HERE / "ollama-dispatch-auto", "oda_t")
blk = auto.last_verify_block(LOG)
check("(c) only the worker's LAST verify block counts (a model's mid-run baseline MISSING lines do not)",
      "normalize(orderNumber)" in blk and "zzz" not in blk, blk)
check("(c) repeat: identical non-empty sets", hg.repeat_missing_literals([["f", "a"]], [("f", "a")]) is True)
check("(c) no repeat: different set / empty / first round",
      not hg.repeat_missing_literals([["f", "a"]], [("f", "b")]) and not hg.repeat_missing_literals([], [("f", "a")])
      and not hg.repeat_missing_literals([["f", "a"]], []))

with tempfile.TemporaryDirectory() as td:
    ns = types.SimpleNamespace(label="t-lit", bundle="b", slice_plan=None, slice_id=None)
    runs = Path(td)
    auto.attempts_path = lambda a, runs_dir=None: runs / "t.attempts.json"
    hist = []
    auto.load_attempts = lambda a, runs_dir=None: __import__("json").load(open(runs / "t.attempts.json")) \
        if (runs / "t.attempts.json").exists() else []
    import json
    json.dump([{"job": "j1", "missing_literals": [["app/route.ts", "normalize(orderNumber)"]]}],
              open(runs / "t.attempts.json", "w"))
    json.dump(json.load(open(runs / "t.attempts.json")) + [{"job": "j2"}], open(runs / "t.attempts.json", "w"))
    rep, cur = auto.missing_literal_round(ns, "j2", blk)
    check("(c) round 2 with the identical set -> repeat", rep is True, (rep, cur))
    stamped = json.load(open(runs / "t.attempts.json"))[-1].get("missing_literals")
    check("(c) the set is stamped on the attempt record", stamped == [["app/route.ts", "normalize(orderNumber)"]], stamped)
    rep2, _ = auto.missing_literal_round(ns, "j2", "  MISSING literal in app/route.ts (x): >>>other<<<")
    check("(c) a different set is not a repeat", rep2 is False)
msg = hg.format_repeat_missing([("f", "a")])
check("(c) repeat message is a SPEC_DEFECT line", msg.startswith("SPEC_DEFECT: "), msg)

# ---- routing ------------------------------------------------------------------------------
check("routing: a SPEC_DEFECT why dies (never auto-sliced)", auto._failure_route(msg) == "die")
check("routing: an ordinary nonconvergence still auto-slices",
      auto._failure_route("staged stage `refimpl` did not converge") == "autoslice")
pre = auto.literal_lint_precheck(d, "app/route.ts")
check("stage-entry lint returns a SPEC_DEFECT line for the worktree", pre.startswith("SPEC_DEFECT: ") and "did you mean" in pre, pre)
(d / "TASK.md").write_text(TASK_OK)
check("stage-entry lint is silent on the corrected spec", auto.literal_lint_precheck(d, "app/route.ts") == "")
bl = {"blockers": [{"check": "refimpl-satisfies", "message": "SPEC_DEFECT: Must-contain literal absent after the refimpl is applied: 1 literal(s)", "detail": "'x'"}]}
check("(b) auto reads a refimpl-satisfies SPEC_DEFECT blocker as a spec defect",
      auto.spec_defect_from_preflight(bl).startswith("SPEC_DEFECT: "))
check("(b) a plain refimpl-satisfies blocker is still NOT a spec defect",
      auto.spec_defect_from_preflight({"blockers": [{"check": "refimpl-satisfies", "message": "the verify goes GREEN while 1 literal"}]}) == "")

# ---- (b) preflight, end to end ------------------------------------------------------------
pf_src = (HERE / "ollama-dispatch-preflight").read_text()
check("(b) preflight emits SPEC_DEFECT (FAIL) for literals absent after the refimpl is applied",
      'f"{_pfx}Must-contain literal absent after the refimpl is applied' in pf_src
      and 'self.add("refimpl-satisfies", FAIL,\n                             f"{_pfx}' in pf_src)

print()
print(f"{len(FAILS)} FAILED: {FAILS}" if FAILS else "LITERAL_LINT_OK")
if REVERT:
    print("REVERT-CHECK:", "RED as required" if FAILS else "STILL GREEN -- the suite does not bite")
    sys.exit(0 if FAILS else 1)
sys.exit(1 if FAILS else 0)
