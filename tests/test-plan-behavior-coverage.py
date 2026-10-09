#!/usr/bin/env python3
"""Plan BEHAVIOR COVERAGE (2026-10-03, replay-bfmr-replace-tracking): the omnibus
auto-slicer let a 3-signature interface win the clause count, so the slices were bare
signatures and every behavioral clause of the intent (trim, throw on empty/equal/wrong
type/status, never mutate, exactly one match) was dropped; each slice gate verified
shape only and wrong code landed with every gate PASS.

Asserted:
  1. the REAL replay plan: >=6 behavioral clauses uncovered; s2/s3 signature-only;
     the type slice s1 is NOT flagged
  2. generator: build_slices(intent, interface) now carries every behavioral clause
     (no uncovered, no signature-only), and the buildReplaceTrackingPayload slice
     literally says "throw" and "never mutate"
  3. paraphrase control: a qwen-style plan that restates the behavior in its own
     words ("raises ValueError when n is less than 1", "returns the original string")
     is COVERED (no false re-prompt)
  4. ollama-dispatch-plan gate_plan: replay plan -> BEHAVIOR_NOT_CARRIED and
     SIGNATURE_ONLY_INTENT; the paraphrase plan -> neither; a plan that DROPS its
     top-level "intent" is still checked against the REQUESTED intent (expect)
  5. ollama-dispatch-slice: plan_behavior_gap flags the replay plan, passes the
     paraphrase plan; a fresh --execute of the replay plan is refused (rc 3) before
     any state is written

OSL_SRC / PLAN_SRC / SLICE_SRC override the files under test (revert-check: .baks).
"""
import importlib.util, json, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(__file__).resolve().parent
OSL = Path(os.environ.get("OSL_SRC") or BIN / "omnibus_slice.py")
PLAN = Path(os.environ.get("PLAN_SRC") or BIN / "ollama-dispatch-plan")
SLICE = Path(os.environ.get("SLICE_SRC") or BIN / "ollama-dispatch-slice")
FAILS = []


def load(name, path):
    ld = SourceFileLoader(name, str(path))
    sp = importlib.util.spec_from_loader(name, ld)
    m = importlib.util.module_from_spec(sp)
    ld.exec_module(m)
    return m


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


REPLAY = json.loads(r"""{
 "intent": "Pure helper buildReplaceTrackingPayload(liveRow, newTracking, dateRange) for BFMR: given the live my-tracker row of a SHIPPED reservation (type \"shipment\", with id/SID/RID/PID/my_tracker_id etc.), return the POST https://www.bfmr.com/api/my-tracker body {tracker_data:[row],dateRange} where row is the live row echoed UNCHANGED except tracking_number set to the trimmed new value. Must: throw if newTracking is empty/whitespace or equals the existing tracking_number, throw if row.type !== \"shipment\" or row.status is not \"shipped\", never mutate the input row, keep every other field identical, empty-string clear is NOT allowed here. Also export pickShipmentRow(rows, myTrackerId) returning the single row with type \"shipment\" and my_tracker_id===myTrackerId, throwing if zero or more than one match.",
 "slices": [
  {
   "id": "s1-export-type-trackerrow-r",
   "title": "export type TrackerRow = Record<string, unknown>",
   "intent": "In lib/bfmrReplaceTracking.ts: export type TrackerRow = Record<string, unknown> & {type?:string; status?:string; tracking_number?:string; my_tracker_id?:number}.",
   "depends_on": [],
   "must_contain": [
    "TrackerRow",
    "tracking_number",
    "my_tracker_id"
   ],
   "verify_shape": "behavioral: exercise the property from 'export type TrackerRow = Record<string, unknown>' and assert it holds. ADVERSARIAL: add a case a plausible-wrong build would fail (e.g. a boundary / opposite-direction case). Kill test: revert this change -> red."
  },
  {
   "id": "s2-export-function-buildrep",
   "title": "export function buildReplaceTrackingPayload(row: TrackerRow, newTracking: string,",
   "intent": "In lib/bfmrReplaceTracking.ts: export function buildReplaceTrackingPayload(row: TrackerRow, newTracking: string, dateRange:{start:string;end:string}): {tracker_data: TrackerRow[]; dateRange:{start:string;end:string}}.",
   "depends_on": [],
   "must_contain": [
    "buildReplaceTrackingPayload",
    "TrackerRow",
    "newTracking",
    "dateRange",
    "tracker_data"
   ],
   "verify_shape": "behavioral: exercise the property from 'export function buildReplaceTrackingPayload(row: TrackerRow, newTracking: string,' and assert it holds. ADVERSARIAL: add a case a plausible-wrong build would fail (e.g. a boundary / opposite-direction case). Kill test: revert this change -> red."
  },
  {
   "id": "s3-export-function-pickship",
   "title": "export function pickShipmentRow(rows: TrackerRow[], myTrackerId: number):",
   "intent": "In lib/bfmrReplaceTracking.ts: export function pickShipmentRow(rows: TrackerRow[], myTrackerId: number): TrackerRow. Tests node:test style like lib/bfmrRetry.test.ts.",
   "depends_on": [],
   "must_contain": [
    "pickShipmentRow",
    "TrackerRow",
    "myTrackerId",
    "bfmrRetry"
   ],
   "verify_shape": "behavioral: exercise the property from 'export function pickShipmentRow(rows: TrackerRow[], myTrackerId: number):' and assert it holds. ADVERSARIAL: add a case a plausible-wrong build would fail (e.g. a boundary / opposite-direction case). Kill test: revert this change -> red."
  }
 ]
}""")
INTENT = REPLAY["intent"]
IFACE = "\n".join(s["intent"].split(": ", 1)[1] for s in REPLAY["slices"])

PARA_INTENT = ("textutil.py: slugify(s) lowercases, collapses non-alphanumerics to one "
               "hyphen, strips hyphens; truncate(s, n) returns s unchanged if len(s)<=n "
               "else s[:n-1]+'…' (single ellipsis char); raises ValueError if n<1, "
               "TypeError if s not str or n not int/bool.")
PARA = {"label": "para", "repo": "/tmp/para", "target": "textutil.py", "lang": "python",
        "intent": PARA_INTENT, "slices": [
    {"id": "s1-slugify", "title": "slugify", "depends_on": [], "must_contain": ["slugify"],
     "verify_shape": "behavioral: 'A  B' -> 'a-b'. ADVERSARIAL: '--x--' -> 'x'.",
     "intent": ("Add a slugify(s) function to textutil.py that lowercases s, collapses runs "
                "of non-alphanumeric characters to a single hyphen and strips leading and "
                "trailing hyphens.")},
    {"id": "s2-truncate", "title": "truncate", "depends_on": [], "must_contain": ["truncate"],
     "verify_shape": "behavioral: truncate('abc',2)=='a…'. ADVERSARIAL: n=0 raises ValueError.",
     "intent": ("Add a truncate function to textutil.py that accepts a string argument s and "
                "an integer argument n, returns the original string when its length does not "
                "exceed n, otherwise returns the first n-1 characters followed by a single "
                "ellipsis character, raises ValueError when n is less than 1, and raises "
                "TypeError when s is not a string or n is not an integer.")}]}


def replay_plan():
    return {"label": "replay-behavgap-test", "repo": "/tmp/behavgap-repo",
            "target": "lib/bfmrReplaceTracking.ts", "lang": "ts", "intent": INTENT,
            "slices": json.loads(json.dumps(REPLAY["slices"]))}


def main():
    osl = load("osl_bc", OSL)
    unc = getattr(osl, "uncovered_behavior", None)
    sig = getattr(osl, "signature_only_slices", None)
    check("omnibus_slice has uncovered_behavior/signature_only_slices",
          callable(unc) and callable(sig), True)
    if callable(unc) and callable(sig):
        check("1. replay plan: >=6 behavioral clauses uncovered",
              len(unc(INTENT, REPLAY["slices"])) >= 6, True)
        so = sig(REPLAY["slices"])
        check("1. replay plan: s2+s3 signature-only, type slice s1 exempt",
              sorted(so), sorted(s["id"] for s in REPLAY["slices"][1:]))
        check("3. paraphrased qwen plan is covered", unc(PARA_INTENT, PARA["slices"]), [])
        check("3. paraphrased qwen plan has no signature-only slice", sig(PARA["slices"]), [])

    built = osl.build_slices(INTENT, IFACE, target="lib/bfmrReplaceTracking.ts")
    check("2. generator still yields 3 slices", len(built), 3)
    if callable(unc):
        check("2. generator: no behavioral clause dropped", unc(INTENT, built), [])
        check("2. generator: no signature-only slice", sig(built), [])
    brt = [s for s in built if "buildReplaceTrackingPayload(" in s["intent"].split("\n")[0]]
    check("2. buildReplaceTrackingPayload slice says throw + never mutate",
          bool(brt) and "throw" in brt[0]["intent"] and "never mutate" in brt[0]["intent"], True)

    pl = load("plan_bc", PLAN)
    codes = {c for c, _ in pl.gate_plan(replay_plan())}
    check("4. gate: replay plan -> BEHAVIOR_NOT_CARRIED", "BEHAVIOR_NOT_CARRIED" in codes, True)
    check("4. gate: replay plan -> SIGNATURE_ONLY_INTENT", "SIGNATURE_ONLY_INTENT" in codes, True)
    pcodes = {c for c, _ in pl.gate_plan(json.loads(json.dumps(PARA)))}
    check("4. gate: paraphrase plan has no coverage defect",
          sorted(pcodes & {"BEHAVIOR_NOT_CARRIED", "SIGNATURE_ONLY_INTENT",
                           "BEHAVIOR_CHECK_UNAVAILABLE"}), [])
    nointent = replay_plan()
    nointent.pop("intent")
    ncodes = {c for c, _ in pl.gate_plan(nointent, {"intent": INTENT})}
    check("4. gate: dropping top-level intent does not opt out (expect intent)",
          "BEHAVIOR_NOT_CARRIED" in ncodes, True)

    sl = load("slice_bc", SLICE)
    gapfn = getattr(sl, "plan_behavior_gap", None)
    check("5. slicer has plan_behavior_gap", callable(gapfn), True)
    if callable(gapfn):
        check("5. slicer: replay plan has a gap", bool(gapfn(replay_plan())), True)
        check("5. slicer: paraphrase plan has no gap", gapfn(json.loads(json.dumps(PARA))), None)
    with tempfile.TemporaryDirectory() as td:
        pf = Path(td) / "p.json"
        pf.write_text(json.dumps(replay_plan()))
        r = subprocess.run([sys.executable, str(SLICE), str(pf), "--execute"],
                           capture_output=True, text=True, timeout=60,
                           env={**os.environ, "HOME": td})
        check("5. fresh --execute of the replay plan refused (rc 3)", r.returncode, 3)
        check("5. refusal names the behavior gap",
              "behavioral clause" in (r.stdout + r.stderr), True)
        leftovers = [str(p.relative_to(td)) for p in Path(td).rglob("*")
                     if p.is_file() and p.name != "p.json"
                     and "replay-behavgap-test" in p.name]
        check("5. no run state written for the refused plan", leftovers, [])

    print(f"\n--- {len(FAILS)} failed ---")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
