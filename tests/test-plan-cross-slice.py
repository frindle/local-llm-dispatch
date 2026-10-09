#!/usr/bin/env python3
"""Plan-gate CROSS-SLICE literal checks (2026-10-04).

Pinned on the REAL chat-frontend plan (plan-gen job d53c7400b2b5). The gate called
it CLEAN in 1 round, but the coordinator hand-fixed 3 defects before executing:
  (1) s6-routes must_contain 'chat_template_kwargs' -- property (5)'s literal, laundered
      through a clause copied into s6's own intent      -> FOREIGN_PROPERTY_LITERAL
  ('stores_image' in s3-store-image was NOT a defect: the slice's own title/intent/
   verify_shape define stores_image(...); the gate correctly leaves it alone)
  (3) s5-model-call 'status','done','error' -- bare words shared with other slices
      (raw-substring literal check: anything satisfies them)  -> GENERIC_LITERAL,
      a NON-BLOCKING warning (plan_warnings; coordinator 2026-10-04: no REDO round)
plus VACUOUS_AT_CHAIN_BASELINE (every literal already forced in by an ancestor), and
the prompt fixes (rules text, example slice no longer models 'phone'/'email').

Usage:  python3 ~/bin/test-plan-cross-slice.py
Revert: PLAN=~/bin/ollama-dispatch-plan.bak-crossslice python3 this.py  -> FAIL
"""
import copy
import json
import os
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLAN = Path(os.environ.get("PLAN", HERE / "ollama-dispatch-plan"))
# frozen copies of the real plans (hermetic: no live ~/.ollama-dispatch/slice-plans read)
PLANS = HERE / "test-fixtures-live-artifacts"
ORIG = PLANS / "chat-frontend-plan.orig-generated.json"
LIVE = PLANS / "chat-frontend-plan.hand-corrected.json"
NEW = ("FOREIGN_PROPERTY_LITERAL", "GENERIC_LITERAL", "VACUOUS_AT_CHAIN_BASELINE")
fails = 0


def chk(name, actual, expected):
    global fails
    ok = actual == expected
    print(("ok   - " if ok else "FAIL - ") + name
          + ("" if ok else f": expected {expected!r} got {actual!r}"))
    fails += 0 if ok else 1


m = SourceFileLoader("plan_under_test", str(PLAN)).load_module()


def fn(name):
    f = getattr(m, name, None)
    if f is None:
        chk(f"{name} exists", False, True)
        return lambda *a, **k: None
    return f


def _parse(rows):
    return sorted((c, msg.split("'")[1], msg.split("'")[3] if msg.count("'") >= 4 else "")
                  for c, msg in rows if c in NEW)


def new_codes(plan):
    """BLOCKING new defects (gate_plan)."""
    return _parse(m.gate_plan(plan))


def warn_codes(plan):
    """NON-BLOCKING findings (plan_warnings)."""
    f = getattr(m, "plan_warnings", None)
    return _parse(f(plan)) if f else [("plan_warnings missing", "", "")]


# ---- 1. the REAL generated plan: flagged exactly where the coordinator fixed it ----
orig = json.loads(ORIG.read_text())
chk("orig chat plan: BLOCKING new defect is exactly (1)", new_codes(orig), [
    ("FOREIGN_PROPERTY_LITERAL", "s6-routes", "chat_template_kwargs")])
chk("orig chat plan: (3) is reported as non-blocking WARNINGS", warn_codes(orig), sorted([
    ("GENERIC_LITERAL", "s5-model-call", "done"),
    ("GENERIC_LITERAL", "s5-model-call", "error"),
    ("GENERIC_LITERAL", "s5-model-call", "status"),
]))
chk("orig chat plan is no longer CLEAN", bool(m.gate_plan(orig)), True)
only_generic = copy.deepcopy(orig)
next(s for s in only_generic["slices"] if s["id"] == "s6-routes")["must_contain"].remove(
    "chat_template_kwargs")
chk("GENERIC alone never blocks: orig minus (1) has no blocking new defect",
    new_codes(only_generic), [])
chk("...but still warns", len(warn_codes(only_generic)), 3)

# ---- 2. the coordinator's corrected plan clears every new check ----
live = json.loads(LIVE.read_text())
chk("hand-corrected live chat plan: no new defects or warnings",
    new_codes(live) + warn_codes(live), [])

# ---- 3. numbered-property parsing ----
segs = fn("_property_segments")(orig["intent"]) or {}
chk("omnibus parses into properties (1)..(7)", sorted(segs), [1, 2, 3, 4, 5, 6, 7])
chk("'running (4)+(5) for that message' is a cross-ref, not a new segment",
    "(4)+(5)" in segs.get(7, ""), True)
chk("unnumbered omnibus -> no segments (check off)",
    fn("_property_segments")("Add foo(x) returning 1 (fast path) and bar."), {})
occ = fn("_lit_occurs")
chk("'6' does not occur in 'base64'", occ("6", "data:<mime>;base64,..."), False)
chk("'6' occurs in 'max 6 per message'", occ("6", "max 6 per message"), True)

# ---- 4. FOREIGN_PROPERTY_LITERAL edges ----
p = copy.deepcopy(orig)
s7 = next(s for s in p["slices"] if s["id"] == "s7-cli")
s7["must_contain"].append("image_url")          # property (4): (7) cross-refs (4)+(5)
chk("s7 may carry a literal of (4): its property says 'running (4)+(5)'",
    [x for x in new_codes(p) if x[1] == "s7-cli"], [])
p = copy.deepcopy(orig)
s1 = next(s for s in p["slices"] if s["id"] == "s1-session-store")
s1["must_contain"].append("os.path.realpath")   # property (1), s1 is property (3)
chk("s1 (property 3) listing a property-(1) code literal is flagged",
    ("FOREIGN_PROPERTY_LITERAL", "s1-session-store", "os.path.realpath") in new_codes(p), True)
s1["must_contain"][-1] = "6"                    # non-code literal: not attributed
chk("a non-code literal ('6') is never FOREIGN",
    [x for x in new_codes(p) if x[1] == "s1-session-store" and x[0].startswith("FOREIGN")], [])

# ---- 5. VACUOUS_AT_CHAIN_BASELINE ----
def mk(slices, intent=None):
    d = {"label": "demo", "repo": "/nonexistent/demo-repo", "target": "demo/x.py",
         "lang": "python", "slices": slices}
    if intent:
        d["intent"] = intent
    return d


def sl(sid, lits, deps=(), intent=None):
    return {"id": sid, "title": sid, "depends_on": list(deps), "must_contain": lits,
            "intent": intent or ("In demo/x.py implement " + " and ".join(lits)
                                 + " so the behaviour is exactly as described here."),
            "verify_shape": "behavioral check plus an ADVERSARIAL wrong-impl case"}


vac = mk([sl("s1-a", ["parse_header(", "HEADER_RE"]),
          sl("s2-b", ["parse_header", "HEADER_RE"], ["s1-a"]),
          sl("s3-c", ["parse_header"], ["s2-b"])])
chk("every literal carried by an ancestor (direct or transitive) -> flagged",
    sorted(x[1] for x in new_codes(vac) if x[0] == "VACUOUS_AT_CHAIN_BASELINE"),
    ["s2-b", "s3-c"])
vac["slices"][1]["must_contain"].append("render_header(")
vac["slices"][1]["intent"] += " Add render_header( too."
chk("one literal the slice introduces -> s2 not vacuous",
    "s2-b" in [x[1] for x in new_codes(vac) if x[0] == "VACUOUS_AT_CHAIN_BASELINE"], False)
indep = mk([sl("s1-a", ["parse_header("]), sl("s2-b", ["parse_header"])])
chk("same literal in a NON-ancestor slice is not chain-vacuous",
    [x for x in new_codes(indep) if x[0] == "VACUOUS_AT_CHAIN_BASELINE"], [])
inv = mk([sl("s1-a", ["parse_header("]), dict(sl("s2-b", ["parse_header"], ["s1-a"]), kind="invariant")])
chk("an invariant slice is exempt", [x for x in new_codes(inv) if x[0].startswith("VAC")], [])
aw = PLANS / "aw-alert-filters.slices.json"
if aw.is_file():
    chk("real aw-alert-filters s6-filter-results-preserves is chain-vacuous",
        "s6-filter-results-preserves" in [x[1] for x in new_codes(json.loads(aw.read_text()))
                                          if x[0] == "VACUOUS_AT_CHAIN_BASELINE"], True)

# ---- 6. GENERIC_LITERAL edges (must NOT flag) ----
g = mk([sl("s1-a", ["parse_header", "version", "flags"],
           intent="Create demo/x.py with parse_header(raw) returning {'version': 1, "
                  "'flags': []}; a missing version raises ValueError."),
        sl("s2-b", ["render_header", "flags"], ["s1-a"],
           intent="Add render_header(h) emitting 'v<version> <flags>' with no trailing space.")])
chk("dict keys in key form + a word carried from an ancestor are NOT generic",
    [x for x in warn_codes(g) if x[0] == "GENERIC_LITERAL"], [])
g2 = mk([sl("s1-a", ["walk_tree", "yield"], intent="Make walk_tree(root) a generator: yield each path."),
         sl("s2-b", ["count_paths"], intent="Add count_paths(root) using walk_tree; yield order is irrelevant.")])
chk("a language keyword ('yield') is not generic",
    [x for x in warn_codes(g2) if x[0] == "GENERIC_LITERAL"], [])
g3 = mk([sl("s1-a", ["run_job", "job"], intent="Add run_job; the CLI is `tool job <id>`."),
         sl("s2-b", ["list_jobs"], intent="Add list_jobs returning every job id, newest first.")])
chk("a backticked word is code, not generic",
    [x for x in warn_codes(g3) if x[0] == "GENERIC_LITERAL"], [])
g4 = mk([sl("s1-a", ["set_state", "queued"], intent="Add set_state(m) marking status 'queued'."),
         sl("s2-b", ["drain"], intent="Add drain() that empties the pending list in order.")])
chk("a word NO other slice uses is not flagged", [x for x in warn_codes(g4) if x[0] == "GENERIC_LITERAL"], [])
g4["slices"][1]["intent"] += " Skip anything still queued."
chk("...but the same word shared with another slice IS flagged",
    [x[1:] for x in warn_codes(g4) if x[0] == "GENERIC_LITERAL"], [("s1-a", "queued")])

# ---- 6b. --gate CLI: WARN printed, exit 0 when only warnings ----
import subprocess
import tempfile
with tempfile.TemporaryDirectory() as td:
    pf = Path(td) / "plan.json"
    pf.write_text(json.dumps(g4))
    r = subprocess.run([sys.executable, str(PLAN), "--gate", str(pf)],
                       capture_output=True, text=True, timeout=60)
    chk("--gate on a warn-only plan exits 0 (CLEAN)", (r.returncode, "GATE: CLEAN" in r.stdout),
        (0, True))
    chk("--gate prints the GENERIC_LITERAL WARN line",
        "WARN (non-blocking) [GENERIC_LITERAL]" in r.stdout, True)
    src = PLAN.read_text()
    chk("finish() records warnings in the plan note",
        "plan_warnings(plan)" in src and 'plan["note"] += (" GATE WARNINGS' in src, True)

# ---- 7. the prompt no longer teaches the defects ----
ex = json.loads(getattr(m, "EXAMPLE_SLICE", "{}"))
chk("EXAMPLE_SLICE lists no bare short word ('phone'/'email' taught 'status'/'done')",
    [l for l in ex.get("must_contain", []) if l.isalpha() and l.islower() and len(l) <= 6], [])
rules = getattr(m, "GATE_RULES_TEXT", "")
chk("GATE_RULES_TEXT names the three new defect codes",
    all(c in rules for c in NEW), True)

# ---- 8. whole corpus still gates without raising ----
errs = []
corpus = sorted(PLANS.glob("*.json"))
chk("frozen plan corpus is non-empty", len(corpus) >= 2, True)
for f in corpus:
    try:
        m.gate_plan(json.loads(f.read_text()))
    except Exception as e:  # noqa: BLE001
        errs.append(f"{f.name}: {e}")
chk("gate_plan runs on every real plan without raising", errs, [])

print("\nALL PASS" if not fails else f"\n{fails} FAIL")
sys.exit(1 if fails else 0)
