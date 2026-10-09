#!/usr/bin/env python3
"""Plan lint root-cause tests (rt-egift-link-s1-s4 / replay-endorse, 2026-10-06).
PLAN_SRC / SLICE_SRC override the tool under test; --revert-check: PLAN_SRC=
ollama-dispatch-plan.bak-20261006T180000Z-planlint and SLICE_SRC=ollama-dispatch-slice.bak-
20261006T181500Z-unsat must FAIL."""
import copy, importlib.util, json, os, sys
from importlib.machinery import SourceFileLoader
B = os.path.expanduser("~/bin")
def load(env, default, name):
    p = os.environ.get(env) or os.path.join(B, default)
    ld = SourceFileLoader(name, p)
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    sys.argv = [p]; ld.exec_module(m); return m
plan = load("PLAN_SRC", "ollama-dispatch-plan", "plan_u")
slc = load("SLICE_SRC", "ollama-dispatch-slice", "slice_u")
FAILS = []
def chk(n, c, d=""):
    print(("ok   - " if c else "FAIL - ") + n + ("" if c else "  " + str(d)[:300]))
    if not c: FAILS.append(n)
codes = lambda p: {c for c, _ in plan.gate_plan(p)}
def mk(mc, intent="PUT accepts a JSON body {link: string}.", vs="behavioral: PUT returns 200"):
    return {"label": "x", "repo": "/nonexistent", "target": "app/route.ts", "lang": "ts",
            "intent": "Add an API route.",
            "slices": [{"id": "s4", "title": "route", "intent": intent, "must_contain": mc,
                        "verify_shape": vs, "depends_on": []}]}
chk("the ORIGINAL egift plan ('Cache-Control: no-store') is refused",
    "HEADER_PAIR_LITERAL" in codes(mk(["getSessionUserId", "Cache-Control: no-store"])), codes(mk(["Cache-Control: no-store"])))
chk("the FIXED egift plan (two tokens) is not", "HEADER_PAIR_LITERAL" not in codes(mk(["getSessionUserId", "no-store"])))
p = mk(["x"], "PUT accepts a JSON body {link: string}.", "behavioral: PUT with a multipart FormData body 'link' returns 200")
chk("JSON intent vs FormData verify_shape is refused", "INTENT_SHAPE_CONFLICT" in codes(p), codes(p))
p = mk(["x"], "GET reads the query with req.nextUrl.searchParams", "behavioral: handlers called with a plain web Request")
chk("req.nextUrl intent vs plain Request verify is refused", "INTENT_SHAPE_CONFLICT" in codes(p), codes(p))
p = mk(["x"], "GET reads the query with new URL(req.url).searchParams, NOT req.nextUrl", "behavioral: handlers called with a plain web Request")
chk("a NOT-nextUrl intent is accepted", "INTENT_SHAPE_CONFLICT" not in codes(p), codes(p))
# warnings: sibling overlap + undefined field
w = lambda p: {c for c, _ in plan.plan_warnings(p)}
q = {"label": "x", "repo": "/nonexistent", "target": "a.py", "lang": "python", "intent": "Add functions.",
     "slices": [{"id": "s1", "title": "t", "intent": "add fooBarBaz handling", "must_contain": ["foo_bar"], "depends_on": []},
                {"id": "s2", "title": "t", "intent": "uses reservationTracking field", "must_contain": ["foo_bar"], "depends_on": []}]}
ww = w(q)
chk("sibling slice with every literal already forced earlier -> SIBLING_OVERLAP warning", "SIBLING_OVERLAP" in ww, ww)
chk("undefined data field -> UNDEFINED_FIELD warning", "UNDEFINED_FIELD" in ww, ww)
# slice pre-author gate
st = {"label": "x"}
r = slc.pre_author_lint_reason(st, {"id": "s4", "must_contain": ["Cache-Control: no-store"], "intent": "i"})
chk("slicer refuses to author a slice with a header-pair literal", bool(r) and "header pair" in r, r)
r = slc.pre_author_lint_reason(st, {"id": "s4", "must_contain": ["Cache-Control", "no-store"], "intent": "i"})
chk("two separate tokens are accepted", r is None, r)
r = slc.pre_author_lint_reason(st, {"id": "s4", "must_contain": ["Cache-Control: no-store"], "lint_ack": True})
chk("lint_ack overrides", r is None, r)
# status is loud
import io, contextlib
stt = {"label": "x", "order": ["s0", "s1"], "slices": {"s0": {"status": "done", "title": "a"},
       "s1": {"status": "escalated", "title": "b", "escalation_reason": "authoring has burned 8 jobs",
              "author_attempts": 5, "author_job_ids": list("abcdefg")}}}
buf = io.StringIO()
with contextlib.redirect_stdout(buf): slc.show_status(stt)
o = buf.getvalue()
chk("--status marks ESCALATED slices, shows reason and budget use",
    "!!escalated" in o and "authoring has burned 8 jobs" in o and "attempts 5/5" in o and "author jobs 7/8" in o and "needs a human" in o, o)
chk("PLAN SUSPECT from AUTO is a deterministic escalation",
    slc.auto_failure_reason("ERROR: PLAN SUSPECT: 5 of 6 cases fail ...")[1] is True)
print("ALL PASS" if not FAILS else "FAILED: " + "; ".join(FAILS))
sys.exit(1 if FAILS else 0)
