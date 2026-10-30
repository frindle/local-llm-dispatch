#!/usr/bin/env python3
"""Preserve-only slices + baseline-green authoring stop (2026-10-03, replay-endorse
s6-preserve-parent-drop-rule: 4 author attempts, all output_cap_loop, ~40 min GPU).

Cause: the plan gate's BEHAVIOR_NOT_CARRIED re-prompt pushed qwen to give every
"must still / preserve / stays PURE" clause its OWN slice (s4d, s5, s6). Such a
slice is green at its baseline by construction, so authoring's self-check ("verify.sh
PASSES at baseline") can never clear, and auto's continuation ladder (c1, c2, -esc)
kept re-authoring an already-true property.

Asserted:
  PLAN (OSL_SRC / PLAN_SRC)
  1. preserve_only_slices flags exactly s4d/s5/s6 of the REAL replay-endorse plan
  2. "Ensure f handles null ..." (names a change) and a kind:invariant slice are NOT
     flagged; a TS generic `<T extends X>` is not read as a change verb
  3. gate_plan: the real plan -> PRESERVE_ONLY_SLICE x3; the REPAIRED plan (s4d/s5/
     s6 deleted, their clauses carried in s4c) -> no PRESERVE_ONLY_SLICE and no
     BEHAVIOR_NOT_CARRIED (the constraint form satisfies coverage)
  4. build_plan drops a preserve-only slice the generator emitted
  AUTO (AUTO_SRC)
  5. baseline_green_at_head: true for the reset-to-HEAD wording, false for the
     measured-as-is fallback and for other FAILs
  6. _author_with_continuations: a baseline-green self-check after the first round
     stops with ZERO continuation dispatches and a BASELINE-GREEN reason; a
     different FAIL still continues
  7. _author_escalate does not run (no -esc) for BASELINE-GREEN; _failure_route = die
"""
import importlib.util, json, os, sys, tempfile
from contextlib import contextmanager
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

BIN = Path(__file__).resolve().parent
OSL = Path(os.environ.get("OSL_SRC") or BIN / "omnibus_slice.py")
PLAN = Path(os.environ.get("PLAN_SRC") or BIN / "ollama-dispatch-plan")
AUTO = Path(os.environ.get("AUTO_SRC") or BIN / "ollama-dispatch-auto")
FAILS = []
REAL = json.loads(r"""{"intent": "selectCanonicalBfmrLinks in lib/bfmrLinkReconcile.ts collapses duplicate tracking numbers ORDER-WIDE (smallest link id wins per trackingNumber). That is wrong: one Amazon order can hold SEVERAL separate BFMR reservations whose units ship together under ONE tracking number, and BFMR itself confirms this by putting that same tracking number on each reservation row. CONFIRMED LIVE 2026-09-22 against production data: order 929 has link 188 (reservation 307956, qty 3, value 1176, tracking 9339589725268581127361) and link 189 (reservation 307955, qty 3, value 1176, same tracking) and BOTH reservations carry that exact tracking number themselves; the current rule drops link 189 so recalcBfmrSalePrice returns 1176 instead of the true 2352. The opposite case must keep working: order 906 link 153 (reservation 164353, status purchased, reservation trackingNumber NULL) shares tracking 1Z82AA931379787130 with link 190 (reservation 238161, whose own trackingNumber IS 1Z82AA931379787130); link 153 is a stale mislink and must still be dropped (true total 1893, not 3155). Same shape on order 767: links 104 and 105 sit on reservation 6480 whose own trackingNumber is 9339589725265621672225, yet they claim trackings 9339589725265621788780 and 9339589725265622369872 which belong to reservations 216284 and 216283 respectively; those two unendorsed links must be dropped so the order totals 1196 (4 x 299), not 1794. FIX: resolve a tracking-number collision by RESERVATION ENDORSEMENT, not by link id. A link is ENDORSED when its own reservation reports that same tracking number. Within one normalized tracking group (trim + lowercase, treat empty string as no tracking): first collapse links that share a reservationId down to the smallest id; then, if any link in the group is endorsed, keep every endorsed link (one per reservation) and drop all unendorsed ones; if NO link in the group is endorsed, fall back to the existing behaviour and keep only the smallest id. Untracked links and the existing step-1 parent-drop rule are unchanged. The function must stay PURE (no prisma, no imports) and keep its generic signature so recalcBfmrSalePrice can pass its richer link rows through unchanged.", "slices": [{"id": "s1-normalize-tracking", "title": "normalizeTracking helper (trim + lowercase, empty-as-null)", "intent": "Add a pure helper function normalizeTracking(trackingNumber: string | null): string | null to lib/bfmrLinkReconcile.ts that trims whitespace, lowercases the string, and returns null when the input is null or an empty string after trimming. This helper is used by the endorsement logic to normalize tracking numbers for comparison within a tracking group.", "depends_on": [], "must_contain": ["normalizeTracking", "trim", "lowercase", "null"], "verify_shape": "behavioral: normalizeTracking('  ABC  ') returns 'abc'; normalizeTracking('') returns null; normalizeTracking(null) returns null; normalizeTracking('Z123') returns 'z123'. ADVERSARIAL: with input '  ' (whitespace-only string) the function MUST return null, not an empty string -- an implementation that returns '' fails this."}, {"id": "s2-per-reservation-collapse", "title": "per-reservation collapse within tracking group", "intent": "Within selectCanonicalBfmrLinks in lib/bfmrLinkReconcile.ts, after grouping tracked links by their normalized tracking number, collapse links that share the same reservationId down to the single link with the smallest id. This per-reservation collapse is the first step within each normalized tracking group, performed before the endorsement-based selection. Links with different reservationIds are not collapsed by this step.", "depends_on": ["s1-normalize-tracking"], "must_contain": ["reservationId", "smallest id", "collapse"], "verify_shape": "behavioral: given links with tracking 'ABC' and reservationIds [1,1,2] with ids [10,5,7], the per-reservation collapse keeps link id 5 (smallest for reservationId 1) and link id 7 (only link for reservationId 2), producing 2 links. ADVERSARIAL: with all links having different reservationIds, no collapse occurs and all links pass through unchanged -- an implementation that collapses across reservationIds fails this."}, {"id": "s3-endorsement-selection", "title": "endorsement-based selection within tracking group", "intent": "Within selectCanonicalBfmrLinks in lib/bfmrLinkReconcile.ts, after per-reservation collapse, if any link in the normalized tracking group is endorsed (its own reservation reports that same tracking number), keep every endorsed link (one per reservation) and drop all unendorsed ones; if NO link in the group is endorsed, fall back to the existing behaviour and keep only the smallest id. A link is ENDORSED when its reservation's trackingNumber matches the link's trackingNumber after normalization. This replaces the old order-wide smallest-link-id rule for endorsed groups.", "depends_on": ["s2-per-reservation-collapse"], "must_contain": ["endorsed", "endorsement", "smallest id", "drop all unendorsed", "fall back"], "verify_shape": "behavioral: given 2 links in a group where link A (reservation 100) is endorsed (reservation 100's trackingNumber matches link A's trackingNumber) and link B (reservation 200) is not endorsed, keep link A and drop link B. ADVERSARIAL: with 0 endorsed links in the group, the smallest-id fallback MUST still apply and keep exactly one link -- an implementation that drops all links in an unendorsed group fails this."}, {"id": "s4a-integrate-normalize", "title": "integrate normalizeTracking call into selectCanonicalBfmrLinks grouping", "intent": "Modify selectCanonicalBfmrLinks in lib/bfmrLinkReconcile.ts to call normalizeTracking on each link's trackingNumber when grouping tracked links by their tracking number, replacing the raw trackingNumber key with the normalized result. This ensures empty strings and whitespace-padded tracking numbers are treated as no-tracking consistently, matching the existing isTracked check behavior.", "depends_on": ["s1-normalize-tracking"], "must_contain": ["normalizeTracking", "selectCanonicalBfmrLinks", "grouping"], "verify_shape": "behavioral: links with tracking '  ABC  ' and 'abc' are grouped together under the normalized key 'abc'; links with tracking '' or null are treated as untracked and pass through unchanged. ADVERSARIAL: a link with tracking '  ' (whitespace-only) must be treated as untracked, not grouped with other tracked links -- an implementation that normalizes but still groups whitespace-only strings as a valid key fails this."}, {"id": "s4b-integrate-collapse", "title": "integrate per-reservation collapse into selectCanonicalBfmrLinks", "intent": "Modify selectCanonicalBfmrLinks in lib/bfmrLinkReconcile.ts to apply per-reservation collapse within each normalized tracking group: after grouping tracked links by their normalized tracking number, collapse links that share the same reservationId down to the single link with the smallest id. This is done before the endorsement-based selection step. Links with different reservationIds within the same tracking group are not collapsed by this step.", "depends_on": ["s2-per-reservation-collapse", "s4a-integrate-normalize"], "must_contain": ["per-reservation collapse", "reservationId", "smallest id", "selectCanonicalBfmrLinks"], "verify_shape": "behavioral: within a tracking group, links with reservationIds [1,1,2] and ids [10,5,7] are collapsed to ids [5,7] (one per reservationId). ADVERSARIAL: links with different reservationIds but the same tracking must NOT be collapsed together -- an implementation that collapses across reservationIds fails this."}, {"id": "s4c-integrate-endorsement", "title": "integrate endorsement-based selection into selectCanonicalBfmrLinks", "intent": "Modify selectCanonicalBfmrLinks in lib/bfmrLinkReconcile.ts to apply endorsement-based selection within each normalized tracking group: after per-reservation collapse, if any link in the group is endorsed (its own reservation reports that same tracking number), keep every endorsed link (one per reservation) and drop all unendorsed ones; if NO link in the group is endorsed, fall back to keeping only the smallest id. A link is ENDORSED when its reservation's trackingNumber matches the link's trackingNumber after normalization.", "depends_on": ["s3-endorsement-selection", "s4b-integrate-collapse"], "must_contain": ["endorsement-based selection", "endorsed", "drop all unendorsed", "fall back", "selectCanonicalBfmrLinks"], "verify_shape": "behavioral: within a tracking group, if link A (reservation 100) is endorsed and link B (reservation 200) is not, keep link A and drop link B. ADVERSARIAL: with 0 endorsed links in the group, the smallest-id fallback MUST still apply and keep exactly one link -- an implementation that drops all links in an unendorsed group fails this."}, {"id": "s4d-purity-signature", "title": "ensure selectCanonicalBfmrLinks stays PURE and keeps generic signature", "intent": "Ensure selectCanonicalBfmrLinks in lib/bfmrLinkReconcile.ts stays PURE (no prisma, no imports) and keeps its generic signature selectCanonicalBfmrLinks<T extends BfmrLinkLike>(links: T[]): T[] so recalcBfmrSalePrice can pass its richer link rows through unchanged. The function must not import any database or external modules, and its type signature must remain generic with the same parameter and return types.", "depends_on": [], "must_contain": ["selectCanonicalBfmrLinks", "BfmrLinkLike", "PURE", "generic signature", "no prisma", "no imports"], "verify_shape": "behavioral: the function signature selectCanonicalBfmrLinks<T extends BfmrLinkLike>(links: T[]): T[] is preserved exactly. ADVERSARIAL: an implementation that adds prisma imports or changes the generic signature to a non-generic void function fails this."}, {"id": "s5-order-906-stale-mislink", "title": "order 906: link 153 is a stale mislink and must still be dropped (true total 1893)", "intent": "In lib/bfmrLinkReconcile.ts, ensure that order 906 link 153 (reservation 164353, status purchased, reservation trackingNumber NULL) shares tracking 1Z82AA931379787130 with link 190 (reservation 238161, whose own trackingNumber IS 1Z82AA931379787130); link 153 is a stale mislink and must still be dropped (true total 1893, not 3155). The endorsement logic must correctly identify link 190 as endorsed (its reservation 238161 reports trackingNumber 1Z82AA931379787130 matching link 190's trackingNumber) and link 153 as not endorsed (its reservation 164353 has trackingNumber NULL, which does not match), so link 153 is dropped and link 190 is kept, yielding total 1893.", "depends_on": ["s4c-integrate-endorsement"], "must_contain": ["link 153", "stale mislink", "must still be dropped", "1893", "1Z82AA931379787130", "reservation 238161"], "verify_shape": "behavioral: order 906 with link 153 (tracking 1Z82AA931379787130, reservation 164353, NOT endorsed because reservation trackingNumber is NULL) and link 190 (same tracking, reservation 238161, endorsed because reservation trackingNumber IS 1Z82AA931379787130) keeps only link 190, total 1893. ADVERSARIAL: if the implementation incorrectly treats NULL as matching empty string, it would keep both links and return 3155 -- an implementation that does not distinguish NULL from empty-string endorsements fails this."}, {"id": "s6-preserve-parent-drop-rule", "title": "preserve existing step-1 parent-drop rule and untracked links", "intent": "In lib/bfmrLinkReconcile.ts, preserve the existing step-1 parent-drop rule exactly as before: if a reservation has at least one tracked link, drop that reservation's no-tracking 'parent' link(s) -- superseded by the split. A reservation with NO tracked link keeps its no-tracking link (an un-split reservation must NOT be emptied). Untracked links and the existing step-1 parent-drop rule are unchanged. The function must stay PURE (no prisma, no imports) and keep its generic signature selectCanonicalBfmrLinks<T extends BfmrLinkLike>(links: T[]): T[] so recalcBfmrSalePrice can pass its richer link rows through unchanged.", "depends_on": [], "must_contain": ["parent-drop", "no-tracking", "PURE", "generic signature", "BfmrLinkLike"], "verify_shape": "behavioral: a reservation with no tracked links keeps its no-tracking parent link; a reservation with at least one tracked link has its no-tracking parent link dropped. ADVERSARIAL: a single untracked link (no-tracking) with no tracked siblings MUST be returned unchanged -- an implementation that drops it fails this."}]}""")


def load(name, path):
    ld = SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    ld.exec_module(m)
    return m


def check(name, got, want=True):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def plan_of(slices):
    return {"label": "replay-endorse-t", "repo": "/tmp/pe-repo-none",
            "target": "lib/bfmrLinkReconcile.ts", "lang": "ts", "intent": REAL["intent"],
            "slices": slices}


def repaired():
    sl = [dict(s) for s in REAL["slices"] if s["id"] not in
          ("s4d-purity-signature", "s5-order-906-stale-mislink", "s6-preserve-parent-drop-rule")]
    drop = [s for s in REAL["slices"] if s["id"] in
            ("s4d-purity-signature", "s5-order-906-stale-mislink", "s6-preserve-parent-drop-rule")]
    for s in sl:
        if s["id"] == "s4c-integrate-endorsement":
            s["intent"] += (" CONSTRAINTS this edit must not break: "
                            + " ".join(d["intent"] for d in drop))
            s["must_contain"] = list(s["must_contain"])
    return sl


@contextmanager
def sandbox(m):
    before = dict(m.__dict__)
    try:
        yield
    finally:
        for k in list(m.__dict__):
            if k not in before:
                del m.__dict__[k]
            elif m.__dict__[k] is not before[k]:
                m.__dict__[k] = before[k]


GREEN = ("FAIL: verify.sh PASSES at baseline -- the fixture cannot fail, so it "
         "certifies nothing. Assert the behaviour the reference impl provides; it must "
         "FAIL before refimpl.py runs.")
ASIS = GREEN + ("\n(Note: the target was measured as-is; no baseline target could be "
                "determined from .dispatch-harness.json.)")
TODO = "FAIL: TASK.md still has TODO placeholders -- fill them in"


def main():
    osl = load("osl_pe", OSL)
    po = getattr(osl, "preserve_only_slices", None)
    check("omnibus_slice has preserve_only_slices", callable(po))
    if callable(po):
        check("1. real plan: exactly s4d/s5/s6 flagged", sorted(po(REAL["slices"])),
              ["s4d-purity-signature", "s5-order-906-stale-mislink",
               "s6-preserve-parent-drop-rule"])
        check("2. 'Ensure f handles null' (a change) not flagged",
              po([{"id": "x", "intent": "In lib/a.ts: ensure normalizeTracking handles "
                   "null by returning null instead of throwing."}]), [])
        check("2. kind:invariant exempt",
              po([{"id": "x", "kind": "invariant",
                   "intent": "Preserve the existing parent-drop rule exactly as before."}]), [])
        check("2. TS generic `<T extends X>` is not a change verb",
              po([{"id": "x", "intent": "Ensure f<T extends Row>(xs: T[]): T[] stays "
                   "PURE with no imports."}]), ["x"])

    pl = load("plan_pe", PLAN)
    codes = [c for c, _ in pl.gate_plan(plan_of(REAL["slices"]))]
    check("3. gate: real plan -> 3x PRESERVE_ONLY_SLICE",
          codes.count("PRESERVE_ONLY_SLICE"), 3)
    rcodes = {c for c, _ in pl.gate_plan(plan_of(repaired()))}
    check("3. gate: repaired plan (constraints carried in s4c) has no "
          "PRESERVE_ONLY_SLICE / BEHAVIOR_NOT_CARRIED",
          sorted(rcodes & {"PRESERVE_ONLY_SLICE", "BEHAVIOR_NOT_CARRIED"}), [])

    with sandbox(osl):
        fake = [
            {"id": "s1-a", "title": "a", "intent": "In lib/m.ts: add incr(x) that returns x+1 and throws on NaN."},
            {"id": "s2-b", "title": "b", "intent": "In lib/m.ts: add trimIt(y) that trims y and returns null when empty."},
            {"id": "s3-keep", "title": "keep", "intent": "In lib/m.ts: preserve the existing parseRow rule exactly as before."},
        ]
        osl.build_slices = lambda intent, interface=None, target=None: [dict(s) for s in fake]
        plan = osl.build_plan("/tmp/x", "lib/m.ts", "ts", "pe-t",
                              "incr returns x+1 and throws on NaN; trimIt trims y and "
                              "returns null when empty; preserve the existing parseRow "
                              "rule exactly as before.")
        check("4. build_plan drops the preserve-only slice",
              [s["id"] for s in (plan or {}).get("slices", [])], ["s1-a", "s2-b"])

    oda = load("oda_pe", AUTO)
    bg = getattr(oda, "baseline_green_at_head", None)
    check("auto has baseline_green_at_head", callable(bg))
    if callable(bg):
        check("5. reset-to-HEAD green -> True", bg(GREEN), True)
        check("5. measured-as-is green -> False", bg(ASIS), False)
        check("5. other FAIL -> False", bg(TODO), False)

    def run_cont(check_text):
        calls = []
        with sandbox(oda), tempfile.TemporaryDirectory() as td:
            wt = Path(td)
            a = SimpleNamespace(label="pe", intent="i", lang="python", model="m", host="h",
                                author_max_iters=24, author_continue_rounds=2, num_ctx=1,
                                max_tokens=None, drafter_cmd=None, timeout=60, slice_id=None)
            n = {"i": 0}

            def dm(wt_, prompt, label, verify_cmd, a_, max_iters=None):
                calls.append(label)
                return False, "did not converge (iteration cap)"
            oda.dispatch_model = dm
            oda.author_prompt = lambda a_, t: "P"
            oda.author_continue_prompt = lambda a_, t, c: "C"
            oda._harness_check_output = lambda w, v, t, b=None: (check_text, None)
            oda.record_attempt = lambda *x, **k: None
            oda.chain_state_write = lambda *x, **k: None

            def sig(*x, **k):
                n["i"] += 1
                return f"sig{n['i']}"
            oda._harness_signature = sig
            ok, why = oda._author_with_continuations(a, wt, "lib/t.ts", "bash verify.sh")
        return ok, why, calls

    ok, why, calls = run_cont(GREEN)
    check("6. baseline-green: ZERO continuation dispatches (only the first author round)",
          calls, ["auto-author-pe"])
    check("6. baseline-green: BASELINE-GREEN reason",
          str(why).startswith(getattr(oda, "BASELINE_GREEN_PREFIX", "\0")), True)
    ok, why, calls = run_cont(TODO)
    check("6. other FAIL still continues (c1 dispatched)", "auto-author-pe-c1" in calls, True)

    pfx = getattr(oda, "BASELINE_GREEN_PREFIX", None)
    check("7. BASELINE-GREEN routes to die, not autoslice",
          pfx is not None and oda._failure_route(pfx + "x") == "die", True)
    with sandbox(oda):
        hit = []
        oda._harness_check_output = lambda *x: hit.append(1) or ("", None)
        a = SimpleNamespace(drafter_cmd=None, label="pe")
        r = oda._author_escalate(a, Path("/tmp"), "t", "v", (pfx or "") + "x")
        check("7. no -esc round for BASELINE-GREEN", (r[0], hit), (False, []))

    print(f"\n--- {len(FAILS)} failed ---")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
