#!/usr/bin/env python3
"""AUTO-side fixes for the card-bonus pipeline failures (2026-10-14).

  1. refine prompt shows each survivor's MEASURED CASE (mutant_sweep witness) and the rule that explains it
     (cardBonuses-v2: survivors were bare mutated lines; the model prose-looped 4x at the 32k cap, no edit)
  2. mutfb_decide: '+0 kills' after a round that made NO edit is not an actionable-feedback round -> one
     re-feed before the no-progress stop (cardBonuses-v2 r1: zero edits, then 'NO PROGRESS (+0 kills)')
  3. intent_defect: an unfilled placeholder / too-thin --intent is refused (rt-card-bonus-lib-rq ran with
     `<paste the full intent text here ...>`; the TASK author invented bronze/silver/gold -> 1a53991)
  4. staged authoring: a stage whose own check is GREEN on disk is accepted even when the job was reported failed
     and is never re-dispatched as a no-op (cardBonuses-v2 s1-r1: 'tree byte-for-byte unchanged -> OVERRIDING TO
     FAILED' on a stage-check verify)
  5. spec_conformance_block: a TASK whose whole named surface is absent from a thin intent blocks authoring

Drives the REAL functions of ollama-dispatch-auto. --revert-check mutates each fix. AUTO_SRC overrides."""
import importlib.util, json, glob, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def load():
    ld = SourceFileLoader("auto_cb", str(AUTO))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("auto_cb", ld))
    argv, sys.argv = sys.argv, [str(AUTO)]
    try:
        ld.exec_module(m)
    finally:
        sys.argv = argv
    return m


def H(rnd, killed, survived, evidence=40):
    return {"round": rnd, "evidence": evidence, "killed": killed, "survived": survived, "survivors": survived,
            "lines": 11, "equivalent_filtered": 0, "llm_equivalent": 0}


RQ_TASK = """# TASK: rt-card-bonus-lib-rq

## Required change

Implement a `calculateCardBonus` function that computes gift card bonus amounts based on card value and bonus tier rules.

## Must contain

- `calculateCardBonus`
- `cardValue`
- `bonusTier`
- `bonusAmount`
- `CardBonusResult`

## Scope
Only edit `lib/cardBonuses.ts`.
"""


def staged_run(m, *, write_on_dispatch, report_ok, green_after_dispatch, env_off=False, reprompts=2, ext_green=False):
    wt = Path(tempfile.mkdtemp(prefix="stg-wt-"))
    (wt / "TASK.md").write_text("# TASK: x\nTODO\n")
    st = {"dispatches": 0, "wrote": False}
    m.stage_plan = lambda a, t: ["task"]
    m.cls_budgets = lambda c: {"reprompts": reprompts}
    m.literal_lint_precheck = lambda *a, **k: None
    m.chain_state_write = lambda *a, **k: None
    m.stage_prompt = lambda *a, **k: "p"
    m.stage_verify_cmd = lambda *a, **k: "v"
    m.last_job_non_attempt = lambda a, job=None: False
    m.prose_runaway_in_log = lambda p: None
    m.record_attempt = lambda *a, **k: None
    m.salvage_round = lambda *a, **k: None
    m.missing_literal_round = lambda *a, **k: (False, None)
    m._job_log_tail = lambda a, limit=0: ""
    m.check_round_runaway = lambda a, why="": None
    m.log_decision = lambda *a, **k: None
    m.spec_conformance_block = lambda *a, **k: ""
    m.NO_MORE_ROUNDS_PREFIXES = ("\0never",)

    def dispatch(wt_, prompt, label, verify, a, max_iters=0):
        st["dispatches"] += 1
        if ext_green:
            st["ext"] = True
        if write_on_dispatch:
            (wt / "TASK.md").write_text(f"# TASK: x\nreal content {st['dispatches']}\n")
            st["wrote"] = True
        return report_ok, ("done" if report_ok else "job vanished from the queue for >300s without reaching a terminal state")
    m.dispatch_model = dispatch

    def check(stage, wt_, lang, target, requires=()):
        if green_after_dispatch and (st["wrote"] or st.get("ext")):
            return True, "task.md complete"
        return False, "TASK.md still has TODO placeholders"
    m.stage_check = check
    a = SimpleNamespace(lang="ts", label="x", intent="i", interface=None, author_max_iters=6, require=())
    old = os.environ.get("AUTO_STAGE_GREEN_AFTER_FAIL")
    if env_off:
        os.environ["AUTO_STAGE_GREEN_AFTER_FAIL"] = "off"
    try:
        ok, why = m._author_staged(a, wt, "lib/x.ts", None)
    finally:
        os.environ.pop("AUTO_STAGE_GREEN_AFTER_FAIL", None)
        if old is not None:
            os.environ["AUTO_STAGE_GREEN_AFTER_FAIL"] = old
    return ok, st["dispatches"], why


def main():
    m = load()
    # ---- 1. witness in the refine prompt
    wit = {"call": "calculateCardBonus({points: 0, perDollars: 100, maxTimes: 5}, 1000)",
           "expected": "{times: 5, points: 0}", "got": "{times: 0, points: 0}"}
    surv = [{"id": "a", "file": "lib/cardBonuses.ts", "line": 27, "mutation": "< -> <=", "snippet": "rule.points <= 0 ||",
             "original": "rule.points < 0 ||", "witness": wit},
            {"id": "b", "file": "lib/cardBonuses.ts", "line": 31, "mutation": "|| -> &&", "snippet": "x &&", "original": "x ||"}]
    p = m.refine_prompt("lib/cardBonuses.ts", surv, [], "ts")
    chk("prompt carries the measured call, real result and mutant result",
        all(t in p for t in ("MEASURED CASE", "calculateCardBonus({points: 0, perDollars: 100, maxTimes: 5}, 1000)",
                             "{times: 5, points: 0}", "{times: 0, points: 0}")), True)
    chk("...and the rule that says it is a real, killable hole", "do not spend a turn deciding whether it is observable" in p, True)
    chk("a survivor WITHOUT a witness gets no MEASURED CASE line", p.count("MEASURED CASE:"), 1)
    p0 = m.refine_prompt("lib/cardBonuses.ts", [surv[1]], [], "ts")
    chk("no witness anywhere -> the prompt is exactly the legacy one (no witness rule)",
        ("MEASURED CASE" in p0, "do not spend a turn" in p0), (False, False))
    os.environ["DISPATCH_SURVIVOR_WITNESS"] = "off"
    chk("DISPATCH_SURVIVOR_WITNESS=off removes it", "MEASURED CASE" in m.refine_prompt("lib/cardBonuses.ts", surv, [], "ts"), False)
    del os.environ["DISPATCH_SURVIVOR_WITNESS"]
    # ---- 2. no-edit round is not an actionable-feedback round
    hist = [H(1, 29, 11), H(2, 29, 11)]
    chk("legacy (edit state unknown): +0 kills -> stop-no-kills", m.mutfb_decide(hist)[0], "stop-no-kills")
    chk("a round that EDITED and killed nothing still stops", m.mutfb_decide(hist, last_round_edited=True, noop_retries_left=1)[0], "stop-no-kills")
    chk("a round with NO edit and a retry left -> re-feed", m.mutfb_decide(hist, last_round_edited=False, noop_retries_left=1)[0], "feed")
    chk("...but not when no retry is left", m.mutfb_decide(hist, last_round_edited=False, noop_retries_left=0)[0], "stop-no-kills")
    chk("progress is still feed", m.mutfb_decide([H(1, 29, 11), H(2, 31, 9)], last_round_edited=False, noop_retries_left=1)[0], "feed")
    chk("the round cap still bounds re-feeding",
        m.mutfb_decide([H(1, 29, 11), H(2, 29, 11), H(3, 29, 11), H(4, 29, 11)], last_round_edited=False, noop_retries_left=1)[0], "stop-cap")
    wt = Path(tempfile.mkdtemp(prefix="fp-wt-"))
    for n in ("verify.test.ts", "refimpl.py", "TASK.md"):
        (wt / n).write_text("a")
    f1 = m.harness_fingerprint(wt, "ts")
    (wt / "verify.test.ts").write_text("b")
    chk("harness_fingerprint changes when the fixture is edited", f1 != m.harness_fingerprint(wt, "ts"), True)
    chk("...and is stable when nothing changes", m.harness_fingerprint(wt, "ts"), m.harness_fingerprint(wt, "ts"))
    # ---- 3. intent
    chk("the literal placeholder intent is refused", "placeholder" in m.intent_defect("<paste the full intent text here, without the surrounding quotes>"), True)
    chk("a merely SHORT intent is NOT refused (canary/real runs use them)", m.intent_defect("fix card bonus"), "")
    real = "lib/cardBonuses.ts hard-codes INVENTED bronze/silver/gold tiers; card bonuses are USER-CONFIGURED: earn P points per D dollars up to M times"
    chk("a real intent passes", m.intent_defect(real), "")
    os.environ["AUTO_INTENT_CHECK"] = "off"
    chk("AUTO_INTENT_CHECK=off passes anything", m.intent_defect("<paste the full intent text here>"), "")
    del os.environ["AUTO_INTENT_CHECK"]
    bad, n = [], 0
    for f in glob.glob(str(Path.home() / ".ollama-dispatch/auto-runs/argv/*.json")):
        try:
            a = json.load(open(f)).get("argv") or []
        except Exception:
            continue
        if "--intent" in a:
            n += 1
            t = a[a.index("--intent") + 1]
            if m.intent_defect(t) and "<paste" not in t:
                bad.append(os.path.basename(f))
    chk(f"replay: no recorded REAL intent ({n} runs) is refused", bad, [])
    # ---- 4. staged authoring: green stage after a mis-reported failure
    ok, n_disp, why = staged_run(m, write_on_dispatch=True, report_ok=False, green_after_dispatch=True)
    chk("job wrote the stage + reported failed but the check is green -> accepted after ONE dispatch", (ok, n_disp), (True, 1))
    ok, n_disp, why = staged_run(m, write_on_dispatch=True, report_ok=False, green_after_dispatch=True, env_off=True)
    chk("AUTO_STAGE_GREEN_AFTER_FAIL=off: the old path re-dispatches a green stage and fails (the live bug)", (ok, n_disp > 1), (False, True))
    ok, n_disp, why = staged_run(m, write_on_dispatch=True, report_ok=False, green_after_dispatch=True, reprompts=0)
    chk("...with NO retries left only the post-failure accept can save the stage", (ok, n_disp), (True, 1))
    ok, n_disp, why = staged_run(m, write_on_dispatch=False, report_ok=False, green_after_dispatch=True, ext_green=True)
    chk("stage turns green with no file change (completion lost) -> the retry is NOT dispatched as a no-op", (ok, n_disp), (True, 1))
    ok, n_disp, why = staged_run(m, write_on_dispatch=True, report_ok=True, green_after_dispatch=True)
    chk("a normal green job is unchanged", (ok, n_disp), (True, 1))
    ok, n_disp, why = staged_run(m, write_on_dispatch=True, report_ok=False, green_after_dispatch=False)
    chk("a failed job whose check is RED is still a failure (no false accept)", (ok, n_disp), (False, 3))
    ok, n_disp, why = staged_run(m, write_on_dispatch=False, report_ok=False, green_after_dispatch=False)
    chk("a job that wrote nothing and is red still fails after the re-prompts", ok, False)
    # ---- 5. spec conformance
    a = SimpleNamespace(intent="<paste the full intent text here, without the surrounding quotes>", interface=None, lang="ts")
    wt = Path(tempfile.mkdtemp(prefix="sc-wt-"))
    (wt / "TASK.md").write_text(RQ_TASK)
    m.spec_conformance_block = load().spec_conformance_block       # undo the staged stub
    m._hg = lambda: None
    r = m.spec_conformance_block(a, wt, "lib/cardBonuses.ts")
    chk("1a53991: TASK names bonusTier/bonusAmount the (placeholder) intent never stated -> blocked", "SPEC CONFORMANCE" in r, True)
    a2 = SimpleNamespace(intent="card bonuses are user configured: calculateCardBonus(rule, spend) returns cardValue bonusTier bonusAmount CardBonusResult "
                                "for every rule the user sets as points per dollars spent up to some number of times with examples and edge cases listed",
                         interface=None, lang="ts")
    chk("the same TASK is fine when the intent actually names that surface", m.spec_conformance_block(a2, wt, "lib/cardBonuses.ts"), "")
    os.environ["SPEC_CONFORMANCE"] = "off"
    chk("SPEC_CONFORMANCE=off never blocks", m.spec_conformance_block(a, wt, "lib/cardBonuses.ts"), "")
    os.environ["SPEC_CONFORMANCE"] = "warn"
    chk("SPEC_CONFORMANCE=warn never blocks", m.spec_conformance_block(a, wt, "lib/cardBonuses.ts"), "")
    del os.environ["SPEC_CONFORMANCE"]
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


MUTANTS = [
    ("witness line dropped from the prompt", '            if _wl:\n                lines.append("  " + _wl)\n', '            pass\n'),
    ("witness rule dropped", '            lines.append(SURVIVOR_WITNESS_RULE)\n', '            pass\n'),
    ("no-edit round still stops", 'if last_round_edited is False and noop_retries_left > 0:', 'if False:'),
    ("fingerprint ignores the fixture", 'for n in (fixture_name_for(lang), "refimpl.py", "TASK.md"):', 'for n in ("refimpl.py", "TASK.md"):'),
    ("placeholder intent accepted", '    m = _INTENT_PLACEHOLDER_RE.search(t)\n    if m:', '    m = None\n    if m:'),
    ("green-after-fail not accepted", '            if (not ok and stage != "refimpl" and stage_green_after_fail_enabled()\n                    and not wrote_nothing(before, after)):',
     '            if False:'),
    ("retry of a green stage still dispatches", '            if attempt > 0 and stage != "refimpl" and stage_green_after_fail_enabled():', '            if False:'),
    ("conformance block ignored", '        if v["status"] == "block":', '        if False:'),
]


def revert_check():
    src, bad = AUTO.read_text(), 0
    for name, old, new in MUTANTS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-auto", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "AUTO_SRC": f.name},
                           capture_output=True, text=True, timeout=600)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": {name} -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
