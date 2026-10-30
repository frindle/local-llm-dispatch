#!/usr/bin/env python3
"""`ollama-dispatch-slice <plan> --cancel` retires the plan's handoff rows (2026-10-05,
s3fix: replay-endorse was cancelled but 16 failed s3 rows lingered in the panel).

End-to-end: runs the REAL slicer CLI with HOME pointed at a temp dir, so the cancel
marker and the handoff-emit it calls ($HOME/bin/handoff-emit.py, a recording stub)
are both sandboxed -- nothing real is cancelled or marked.

Usage:  python3 test-slice-cancel-retires-handoff.py          -> ALL PASS
        SLICE=<slice .bak> python3 ...                        -> FAILs (revert-check)
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SLICE = Path(os.environ.get("SLICE") or HERE / "ollama-dispatch-slice").resolve()
fails = []


def chk(name, got, want):
    ok = got == want
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        fails.append(name)


PLAN = "zz-cancel-t"
ROWS = {
    "complete": [
        {"id": "aaaaaaaaaa01", "label": f"auto-author-{PLAN}-s3-sel", "awaiting_signoff": False},
        {"id": "aaaaaaaaaa02", "label": f"auto-author-{PLAN}-s3-sel-c1", "awaiting_signoff": False},
        {"id": "aaaaaaaaaa03", "label": f"auto-refine-{PLAN}-s3-sel-r2", "awaiting_signoff": False},
        {"id": "aaaaaaaaaa04", "label": f"{PLAN}-s1-norm", "awaiting_signoff": False},
        {"id": "aaaaaaaaaa05", "label": f"{PLAN}-s1-norm", "awaiting_signoff": True},   # sign-off: keep
        {"id": "aaaaaaaaaa06", "label": f"{PLAN}x-s1-norm", "awaiting_signoff": False},  # other plan
        {"id": "aaaaaaaaaa07", "label": "unrelated-job", "awaiting_signoff": False},
        {"id": "aaaaaaaaaa08", "label": f"{PLAN}-s9-notaslice", "awaiting_signoff": False},
    ],
    "pending": [
        {"id": "aaaaaaaaaa09", "label": f"{PLAN}-s3-sel", "awaiting_signoff": False},     # in flight
    ],
}
STUB = r'''#!/usr/bin/env python3
import json, sys, os
log = os.path.join(os.path.dirname(__file__), "handoff-calls.jsonl")
open(log, "a").write(json.dumps(sys.argv[1:]) + "\n")
if "--json" in sys.argv:
    print(open(os.path.join(os.path.dirname(__file__), "rows.json")).read())
'''

with tempfile.TemporaryDirectory() as home:
    hb = Path(home) / "bin"
    hb.mkdir()
    (hb / "handoff-emit.py").write_text(STUB)
    (hb / "rows.json").write_text(json.dumps(ROWS))
    # the slicer imports plan_cancel etc. from its own dir
    plan = {"label": PLAN, "repo": str(Path(home) / "repo"), "target": "lib/x.ts", "lang": "ts",
            "intent": "t", "slices": [
                {"id": "s1-norm", "title": "a", "intent": "a", "depends_on": [], "must_contain": []},
                {"id": "s3-sel", "title": "b", "intent": "b", "depends_on": ["s1-norm"], "must_contain": []}]}
    pf = Path(home) / "plan.json"
    pf.write_text(json.dumps(plan))
    env = {**os.environ, "HOME": home, "DISPATCH_VERIFY_SANDBOX": "1",
           "PYTHONPATH": str(SLICE.parent)}
    cp = subprocess.run([sys.executable, str(SLICE), str(pf), "--cancel", "--reason",
                         "real fix landed and live in main"],
                        capture_output=True, text=True, env=env, timeout=300, cwd=home)
    out = cp.stdout + cp.stderr
    chk("the cancel itself succeeded (marker written in the SANDBOX home)",
        (Path(home) / ".ollama-dispatch" / "slice-runs" / f"{PLAN}.cancelled").exists(), True)
    calls = []
    lf = hb / "handoff-calls.jsonl"
    if lf.exists():
        calls = [json.loads(l) for l in lf.read_text().splitlines() if l.strip()]
    acted = [c for c in calls if "--acted" in c]
    ids = []
    reason = ""
    if acted:
        c = acted[0]
        ids = c[c.index("--acted") + 1:c.index("--reason")] if "--reason" in c else c[c.index("--acted") + 1:]
        reason = c[c.index("--reason") + 1] if "--reason" in c else ""
    chk("--cancel marks the plan's COMPLETE rows acted (author, -c1, refine -r2, coding)",
        sorted(ids), ["aaaaaaaaaa01", "aaaaaaaaaa02", "aaaaaaaaaa03", "aaaaaaaaaa04"])
    chk("...with a reason naming the cancel", "cancelled" in reason and PLAN in reason, True)
    for i, why in (("aaaaaaaaaa05", "awaiting sign-off"), ("aaaaaaaaaa06", "another plan"),
                   ("aaaaaaaaaa07", "unrelated"), ("aaaaaaaaaa08", "not a slice of this plan"),
                   ("aaaaaaaaaa09", "pending/in-flight")):
        chk(f"never touches a row that is {why}", i in ids, False)
    if not acted:
        print(out[-1500:])

# the fixed reason must pass the REAL handoff-emit merge-claim guard
ld = SourceFileLoader("ho_t", str(HERE / "handoff-emit.py"))
sp = importlib.util.spec_from_loader("ho_t", ld)
ho = importlib.util.module_from_spec(sp)
ld.exec_module(ho)
chk("the reason is accepted by handoff-emit's merge-claim guard (the human's 'landed' text is not passed)",
    bool(reason) and not ho.claims_merge_without_commit(reason), True)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAIL")
sys.exit(1 if fails else 0)
