#!/usr/bin/env python3
"""Planner fallback on 'no clean decomposition', test-file scope hint, worker-exit routing.
Run: python3 test-auto-routing-fallback.py [--bin DIR]   -> ROUTING_FALLBACK_OK"""
import argparse, json, os, sys, tempfile, types
from importlib.machinery import SourceFileLoader
from pathlib import Path

ap = argparse.ArgumentParser(); ap.add_argument("--bin", default=str(Path(__file__).resolve().parent))
BIN = Path(ap.parse_args().bin).resolve()
sys.path.insert(0, str(BIN))
fails = []
def chk(n, c):
    print(("ok   " if c else "FAIL ") + n)
    if not c: fails.append(n)

auto = SourceFileLoader("auto_rf", str(BIN / "ollama-dispatch-auto")).load_module()
om = auto.omnibus_slice
tmp = Path(tempfile.mkdtemp(prefix="rf-"))

# --- 2. test-file scope hint
act, msg = auto.multi_module_check("Add x; tests in new test_upgrade_codec_floor.py", None, "app.py", str(tmp))
chk("test file not in scope: warns", act == "warn")
chk("...suggests dropping it from the intent", "drop the test file from the intent" in msg)
chk("...or --edit-file", "--edit-file test_upgrade_codec_floor.py" in msg)
act, msg = auto.multi_module_check("Add x; also new helper lib/newHelper.py", None, "app.py", str(tmp))
chk("a non-test foreign file gets no test-file advice", "--edit-file" not in msg)

# --- 3. worker exit routing
k, m = auto.worker_exit_route("spec_defect_repeat_abort", "j1", "boom")
chk("spec_defect_repeat_abort -> spec_defect with the SPEC_DEFECT prefix", k == "spec_defect" and m.startswith(auto.SPEC_DEFECT_ROUTE_PREFIX))
chk("...and _failure_route dies (never auto-slices)", auto._failure_route(m) == "die")
k, m = auto.worker_exit_route("sampling_ladder_exhausted", "j2")
chk("sampling_ladder_exhausted -> respec", k == "respec" and m.startswith(auto.RESPEC_ROUTE_PREFIX))
chk("...carries the SPEC_DEFECT mark so the slicer re-specs on first sight", auto.SPEC_DEFECT_ROUTE_PREFIX in m)
chk("...and top level re-specs by decomposing (autoslice route)", auto._failure_route(m) == "autoslice")
chk("ordinary reasons are not routed", auto.worker_exit_route("nonconvergence")[0] is None)
a = types.SimpleNamespace(_last_job_outcome={"job": "j9", "terminal_reason": "spec_defect_repeat_abort"})
chk("worker_exit_failure reads the polled outcome", (auto.worker_exit_failure(a, "x") or "").startswith("SPEC_DEFECT: "))
chk("...none without an outcome", auto.worker_exit_failure(types.SimpleNamespace(), "x") is None)

# --- 1. planner fallback
om.PLANS_DIR = tmp / "plans"; om.PLANS_DIR.mkdir()
cmds, hand = [], []
def run_ok(cmd):
    cmds.append(cmd); om.write_plan({"label": "lbl", "slices": [1, 2]}); return 0
kw = dict(repo="/r", target="t.py", lang="python", label="lbl", intent="do it", interface="iface", bundle="bnd",
          _handoff=lambda p, **k: hand.append(p) or 0)
rc = om.planner_fallback_and_handoff(_run=run_ok, **kw)
chk("fallback generates a plan then hands it to the slicer", rc == 0 and len(cmds) == 1 and len(hand) == 1)
c = cmds[0]
chk("...via ollama-dispatch-plan --generate with the bundle, gated plan only (no --then-execute)",
    "--generate" in c and "--bundle" in c and "bnd" in c and "--auto-confirm-plan" in c and "--then-execute" not in c)
chk("...interface rides in the intent", "iface" in c[c.index("--intent") + 1])
chk("...bundle stamped on the plan", json.loads((om.PLANS_DIR / "lbl.slices.json").read_text()).get("bundle") == "bnd")
rc = om.planner_fallback_and_handoff(_run=run_ok, **kw)
chk("idempotent: an existing plan is reused, not regenerated", rc == 0 and len(cmds) == 1 and len(hand) == 2)
(om.PLANS_DIR / "lbl.slices.json").unlink()
chk("planner failure propagates (caller escalates)", om.planner_fallback_and_handoff(_run=lambda c: 3, **kw) == 3)
os.environ[om.PLANNER_FALLBACK_OFF_ENV] = "1"
chk("kill switch", om.planner_fallback_and_handoff(_run=run_ok, **kw) == 1)
del os.environ[om.PLANNER_FALLBACK_OFF_ENV]

# --- 1b. _autoslice_on_failure wiring
big = tmp / "repo"; big.mkdir(); (big / "t.py").write_text("x\n" * 5000)
(big / "s.py").write_text("x\n")
ns = lambda: types.SimpleNamespace(repo=str(big), no_auto_slice=False, drafter_cmd=None, label="lbl", intent="i",
    interface=None, auto_slice_threshold=3, lang="python", model="m", host="h", num_ctx=1, bundle="b",
    slice_plan=None, slice_id=None)
called = []
om.plan_failure_action = lambda *x, **k: "cannot-slice-escalate"
om.planner_fallback_and_handoff = lambda **k: called.append(k) or 0
auto._under_slicer = lambda a: False
class Die(Exception): pass
def die(m, c=1): raise Die(m)
auto.die = die
chk("cannot-slice-escalate on a big target -> planner fallback, returns its rc",
    auto._autoslice_on_failure(ns(), "t.py", "boom") == 0 and len(called) == 1 and called[0]["bundle"] == "b")
called.clear()
om.target_is_small = lambda p: (True, "small")
try: auto._autoslice_on_failure(ns(), "s.py", "boom"); r = "no-die"
except Die as e: r = str(e)
chk("a small target never calls the planner; escalates as before", not called and "Human needed" in r)
om.target_is_small = lambda p: (False, "")
om.planner_fallback_and_handoff = lambda **k: 1
try: auto._autoslice_on_failure(ns(), "t.py", "boom"); r = "no-die"
except Die as e: r = str(e)
chk("planner failure still ends in the human escalation", "Human needed" in r)


# --- 3b. wiring: a spec-naming worker exit ends authoring with NO further model round
calls = []
auto.dispatch_model = lambda *x, **k: calls.append(1) or (False, "job j9 failed (exit 1)")
auto._harness_signature = lambda *x, **k: "sig"
auto.log_decision = lambda *x, **k: None
ac = lambda tr: types.SimpleNamespace(label="l", lang="python", author_continue_rounds=2, author_max_iters=5,
    _last_job_outcome={"job": "j9", "terminal_reason": tr})
author_prompt_orig = auto.author_prompt
auto.author_prompt = lambda a, t: "p"
ok, why = auto._author_with_continuations(ac("spec_defect_repeat_abort"), tmp, "t.py", "v")
chk("continuations: spec_defect_repeat_abort stops after ONE dispatch with the SPEC_DEFECT message",
    not ok and why.startswith("SPEC_DEFECT: ") and len(calls) == 1)
calls.clear()
ok, why = auto._author_with_continuations(ac("sampling_ladder_exhausted"), tmp, "t.py", "v")
chk("continuations: sampling_ladder_exhausted stops after ONE dispatch with the RESPEC message",
    not ok and why.startswith("RESPEC: ") and len(calls) == 1)
a0 = ac("sampling_ladder_exhausted"); a0.author_continue_rounds = 0
ok, why = auto._author_with_continuations(a0, tmp, "t.py", "v")
chk("continuations: with no rounds the final return still routes", why.startswith("RESPEC: "))
auto.staged_enabled = lambda a: True
auto._author_staged = lambda a, wt, t, c: (False, "staged stage `task` wrote nothing")
def _boom(*x, **k): raise AssertionError("continuation ladder must not run")
auto._author_with_continuations = _boom
chk("entry: staged failure with spec_defect_repeat_abort skips the continuation ladder",
    auto._author_entry(ac("spec_defect_repeat_abort"), tmp, "t.py", "v")[1].startswith("SPEC_DEFECT: "))
chk("entry: ...and sampling_ladder_exhausted likewise",
    auto._author_entry(ac("sampling_ladder_exhausted"), tmp, "t.py", "v")[1].startswith("RESPEC: "))

auto._harness_check_output = lambda *x, **k: (_ for _ in ()).throw(AssertionError("escalation must not run"))
chk("escalate: a RESPEC failure gets no escalation round",
    auto._author_escalate(types.SimpleNamespace(drafter_cmd=None), tmp, "t.py", "v", "RESPEC: x") == (False, "RESPEC: x"))

# --- 1c. the planner stamps the bundle on the plan it writes
plan_mod = SourceFileLoader("plan_rf", str(BIN / "ollama-dispatch-plan")).load_module()
plan_mod.PLAN_DIR = tmp / "pd"
an = types.SimpleNamespace(bundle="bnd", out=None, auto_confirm_plan=False, then_execute=False, intent="i")
pl = {"slices": [{"id": "s1", "title": "t", "intent": "x", "depends_on": [], "must_contain": ["a"], "verify_shape": "v"}]}
plan_mod.finish(an, pl, "lblx", {"repo": "/r", "target": "t.py", "lang": "python"}, 1)
chk("planner finish() records plan['bundle']", json.loads((tmp / "pd" / "lblx.slices.json").read_text()).get("bundle") == "bnd")
an.bundle = None
plan_mod.finish(an, {"slices": pl["slices"]}, "lbly", {"repo": "/r", "target": "t.py", "lang": "python"}, 1)
chk("...and no bundle key without --bundle", "bundle" not in json.loads((tmp / "pd" / "lbly.slices.json").read_text()))

print("ROUTING_FALLBACK_OK" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
