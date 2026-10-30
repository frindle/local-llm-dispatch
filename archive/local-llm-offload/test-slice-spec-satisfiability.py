#!/usr/bin/env python3
"""Phase 2 (2026-10-08) slicer gates, driven as functions on the real slicer module.

  1. spec_satisfiability_reason: a must_contain literal with 0 occurrences in the target and
     not named in the slice's own text is refused BEFORE any author job (8a4941aa8521 /
     a580c8793aa7 MANUAL_SYNC_INIT); one named in the intent is fine; lint_ack overrides;
     a creation slice (target absent) is never flagged.
  2. already-satisfied INVARIANT: every literal present -> a reason (execute() => SKIPPED),
     no literal re-derivation / refinement; a normal slice is still re-derived.
  3. retry-storm -> re-spec: at the attempt cap (any signature) respec_on_cap folds the
     failure into the intent ONCE, zeroes the counters, and the second cap parks.
Run: python3 test-slice-spec-satisfiability.py [--src PATH]   (prints ALL PASS)
"""
import importlib.util, os, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(sys.argv[sys.argv.index("--src") + 1]) if "--src" in sys.argv else HERE / "ollama-dispatch-slice"
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


ld = SourceFileLoader("ods_sat", str(SRC))
m = importlib.util.module_from_spec(importlib.util.spec_from_loader("ods_sat", ld))
sys.argv = [str(SRC)]
ld.exec_module(m)

root = tempfile.mkdtemp(prefix="sat-")
os.makedirs(os.path.join(root, "lib"))
Path(root, "lib/sync.ts").write_text("export function syncResult() {}\nconst MANUAL_SYNC = 1;\n")
st = {"target": "lib/sync.ts", "label": "x"}

# 1
s = {"id": "s1", "intent": "add bfmrPushFailureMessage(err)", "must_contain": ["MANUAL_SYNC_INIT"]}
r = m.spec_satisfiability_reason(st, s, root)
check("1 0-occurrence literal refused", bool(r) and "MANUAL_SYNC_INIT" in r, True)
check("1 literal named in the intent is satisfiable",
      m.spec_satisfiability_reason(st, dict(s, must_contain=["bfmrPushFailureMessage"]), root), None)
check("1 literal present in the target is satisfiable",
      m.spec_satisfiability_reason(st, dict(s, must_contain=["syncResult"]), root), None)
check("1 multi-token NEW-CODE literal is never linted (canary regression)",
      m.spec_satisfiability_reason(st, dict(s, must_contain=["if (x < lo) return lo;"]), root), None)
check("1 lint_ack overrides", m.spec_satisfiability_reason(st, dict(s, lint_ack=True), root), None)
check("1 creation slice (target absent) is not flagged",
      m.spec_satisfiability_reason({"target": "lib/new.ts"}, s, root), None)

# 2
inv = {"kind": "invariant", "intent": "no network io", "must_contain": ["syncResult", "MANUAL_SYNC"]}
check("2 invariant, all literals present -> skip reason",
      "already satisfied" in (m.vacuous_literals(st, dict(inv), root) or ""), True)
decl = {"kind": "invariant", "intent": "x", "must_contain": ["syncResult"]}
check("2 invariant with declared-only literal is NOT refined, it is skipped",
      "already satisfied" in (m.vacuous_literals(st, decl, root) or ""), True)
inv2 = dict(inv, must_contain=["syncResult", "ABSENT_ONE"])
check("2 invariant with an absent literal still runs", m.vacuous_literals(st, inv2, root), None)

# 3
s3 = {"id": "s3", "intent": "do the thing", "author_attempts": 5, "author_fail_streak": 1}
check("3 first cap -> re-spec applied", m.respec_on_cap(s3, "sig-A", 1, 5), True)
check("3 ... intent carries the failure", "sig-A" in s3["intent"] and "RE-SPEC NOTE" in s3["intent"], True)
check("3 ... counters zeroed", (s3["author_attempts"], s3.get("author_fail_streak")), (0, None))
s3["author_attempts"] = 5
check("3 second cap -> parks (bounded)", m.respec_on_cap(s3, "sig-B", 1, 5), False)
check("3 ... respec_count capped", s3["respec_count"], m.RESPEC_CAP)

print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
