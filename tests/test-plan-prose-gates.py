#!/usr/bin/env python3
"""Plan-gate prose fixes A/B/D (2026-10-03).

Pinned on the REAL replay-endorse plan + target (s3 burned 15 author jobs on a datum
the function never receives; s4b was summarised as "relevance NO-GO"; s4b's
self-heal review read the testproject MAIN file, not the chain tip).

  A  _multi_property_defect: prose words ('Keep PURE (no prisma)', 'function body
     must') are NOT signatures/definitions; real multi-definition slices still flag
  B  PROSE_LITERAL flags English must_contain literals; an identifier the intent
     introduces word-by-word passes LITERAL_NOT_IN_INTENT; an invented one does not
  D  DATUM_NOT_IN_TARGET flags s3/s4c ("reservation's trackingNumber"); naming
     `reservationTracking` in s3 clears both; "link's trackingNumber" never flags

Usage: python3 ~/bin/test-plan-prose-gates.py
Revert: PLAN=~/bin/ollama-dispatch-plan.bak-planprose python3 this.py -> FAIL
(fix C's checks live in test-escalation-label-context.py)
"""
import importlib.util
import json
import os
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLAN = Path(os.environ.get("PLAN", HERE / "ollama-dispatch-plan"))
REAL_PLAN = Path.home() / ".ollama-dispatch/slice-plans/replay-endorse.slices.json.bak-datacontract"
fails = 0


def chk(name, actual, expected):
    global fails
    ok = actual == expected
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f": expected {expected!r} got {actual!r}"))
    fails += 0 if ok else 1


def load(path, name):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def safe(fn, *a, default="ABSENT"):
    try:
        return fn(*a)
    except Exception as exc:  # a .bak lacking the function / signature
        return f"{default}:{type(exc).__name__}"


pl = load(PLAN, "plan_t")
plan = json.loads(REAL_PLAN.read_text())
tgt_raw = pl._baseline_target_text(plan) if hasattr(pl, "_baseline_target_text") else None
chk("real target readable", bool(tgt_raw), True)
tgt = pl._code_only(tgt_raw) if hasattr(pl, "_code_only") else tgt_raw


def mpd(intent, title="t"):
    try:
        return pl._multi_property_defect("sx", intent, title, tgt)
    except TypeError:
        return pl._multi_property_defect("sx", intent, title)


# ---------------- A ----------------------------------------------------------
PROSE1 = "Keep PURE (no prisma). Mark endorsed (own reservation) links; drop the rest (unendorsed)."
PROSE2 = ("Inside selectCanonicalBfmrLinks the function body must keep a single pass; "
          "the function result (one per reservation) is returned.")
chk("A: 'Keep PURE (no prisma) ... rest (unendorsed)' is not 3 signatures", mpd(PROSE1), None)
chk("A: 'function body must' / 'function result' is not 2 definitions", mpd(PROSE2), None)
chk("A: two real definitions still flag",
    mpd("Add `function normalizeTracking()` and `function pickCanonical()`.") is not None, True)
chk("A: three real call signatures still flag",
    mpd("Call parseA(x), then parseB(y), then parseC(z).") is not None, True)
chk("A: 'also' + two real signatures still flags",
    mpd("Change normalizeTracking(t); also update pickCanonical(links).") is not None, True)

# rule-by-rule coverage of _codeish (coordinator asked which A rules were untested)
def ci(*a, **k):
    try:
        return pl._codeish(*a, **k)
    except Exception as e:
        return "ABSENT:%s" % type(e).__name__
chk("A: a plain word present in the target CODE counts as code",
    ci("frob", "the frob step", "const frob = 1;"), True)
chk("A: ...the same word only in a target COMMENT does not",
    ci("frob", "the frob step", pl._code_only("// frob\nconst y = 1;")
       if hasattr(pl, "_code_only") else ""), False)
chk("A: PascalCase counts only for definitions", (ci("Widget", "class Widget", None, allow_pascal=True),
                                                   ci("Widget", "the Widget (x)", None)), (True, False))
chk("A: prose parenthetical 'rest (x)' is not a call, 'rest(x)' is",
    (ci("rest", "drop the rest (unendorsed)"), ci("rest", "call rest(x)")), (False, True))

# ---------------- B ----------------------------------------------------------
for lit, want in (("smallest id", True), ("drop all unendorsed", True), ("fall back", True),
                  ("reservationId", False), ("selectCanonicalBfmrLinks(", False),
                  ("per-reservation collapse", False), ("endorsed", False)):
    chk(f"B: _prose_literal({lit!r}) == {want}", safe(pl._prose_literal, lit) if hasattr(pl, "_prose_literal") else "ABSENT", want)
for lit in ("async def", "else if", "not in", "is none", "return null", "if not",
            "raise valueerror", "export default", "const x"):
    chk(f"B: keyword code literal {lit!r} is NOT prose",
        safe(pl._prose_literal, lit) if hasattr(pl, "_prose_literal") else False, False)
chk("B: a plain-word literal present as CODE in the target is NOT prose",
    safe(pl._prose_literal, "foo bar", "x = foo bar;") if hasattr(pl, "_prose_literal") else False,
    False)
chk("B: ...but the same words only in a target COMMENT are still prose",
    safe(pl._prose_literal, "foo bar", pl._code_only("x = 1; // foo bar\n"))
    if hasattr(pl, "_prose_literal") else "ABSENT", True)
INTENT = "Carry the reservation's tracking number onto each link so endorsed links can be told apart."
chk("B: `reservationTracking` is introduced by the intent",
    safe(pl._identifier_from_intent, "reservationTracking", INTENT) if hasattr(pl, "_identifier_from_intent") else "ABSENT", True)
chk("B: `widgetTracking` (invented 'widget') is NOT",
    safe(pl._identifier_from_intent, "widgetTracking", INTENT) if hasattr(pl, "_identifier_from_intent") else "ABSENT", False)
chk("B: a single word is not an 'identifier from intent'",
    safe(pl._identifier_from_intent, "tracking", INTENT) if hasattr(pl, "_identifier_from_intent") else "ABSENT", False)


def codes(p):
    return sorted({(c, m.split("'")[1] if "'" in m else "") for c, m in pl.gate_plan(p)})


base = codes(plan)
chk("B: real plan -> PROSE_LITERAL on s3's 'smallest id'/'fall back'",
    ("PROSE_LITERAL", "s3-endorsement-selection") in base, True)
p2 = json.loads(json.dumps(plan))
s3 = next(s for s in p2["slices"] if s["id"] == "s3-endorsement-selection")
s3["must_contain"] = ["reservationTracking"]
s3["intent"] = INTENT + " " + s3["intent"]
lit_hits = [c for c, sid in codes(p2) if sid == "s3-endorsement-selection" and c in
            ("LITERAL_NOT_IN_INTENT", "PROSE_LITERAL")]
chk("B: identifier literal introduced in prose passes the literal checks", lit_hits, [])
s3["must_contain"] = ["widgetTracking"]
chk("B: invented identifier literal still LITERAL_NOT_IN_INTENT",
    ("LITERAL_NOT_IN_INTENT", "s3-endorsement-selection") in codes(p2), True)
s3["must_contain"] = ["reservationId"]  # present at baseline -> vacuity guard untouched
s3["intent"] = s3["intent"] + " Keyed by reservationId."
chk("B: vacuity guard unchanged (baseline-present literal still VACUOUS)",
    any(c.startswith("VACUOUS") and sid == "s3-endorsement-selection" for c, sid in codes(p2)), True)

# ---------------- D ----------------------------------------------------------
dat = sorted(sid for c, sid in base if c == "DATUM_NOT_IN_TARGET")
chk("D: real plan flags s3 + s4c for reservation's trackingNumber",
    dat, ["s3-endorsement-selection", "s4c-integrate-endorsement"])
p3 = json.loads(json.dumps(plan))
s3 = next(s for s in p3["slices"] if s["id"] == "s3-endorsement-selection")
s3["intent"] += " Add `reservationTracking` to BfmrLinkLike to carry it."
chk("D: naming `reservationTracking` clears both",
    [sid for c, sid in codes(p3) if c == "DATUM_NOT_IN_TARGET"], [])
chk("D: \"the link's trackingNumber\" never flags (BfmrLinkLike carries it)",
    safe(pl.missing_datum_defects, ["Compare the link's trackingNumber."], tgt)
    if hasattr(pl, "missing_datum_defects") else "ABSENT", [])
def md(texts, src, intro=""):
    return (safe(pl.missing_datum_defects, texts, src, intro)
            if hasattr(pl, "missing_datum_defects") else "ABSENT")
chk("D: dotted owner.field access in the target exempts it",
    md(["use the order's shipDate"], "const d = order.shipDate;"), [])
chk("D: ...without it the same datum flags",
    md(["use the order's shipDate"], "const d = 1;"), [("order", "shipDate")])
chk("D: pronoun owners ('its', 'each') never flag",
    md(["use its shipDate and each's shipDate"], "const d = 1;"), [])
chk("D: a field named in the plan's own intents (introduced_text) exempts it",
    md(["use the order's shipDate"], "const d = 1;", "add `orderShipDate` to Input"), [])
chk("D: a plain-English field ('order's date') is not a datum claim",
    md(["use the order's date"], "const d = 1;"), [])
chk("D: comment words in the target are not code (no 'reservation's no')",
    [f for o, f in (safe(pl.missing_datum_defects, ["the reservation's no tracking"], tgt)
                    if hasattr(pl, "missing_datum_defects") else [("x", "ABSENT")])], [])

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
