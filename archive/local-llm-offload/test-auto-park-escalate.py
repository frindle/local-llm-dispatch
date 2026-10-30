#!/usr/bin/env python3
"""Regression test for ollama-dispatch-auto's stuck-authoring handling (2026-10-01).

Repro: sidecar-bfmr-login-nudge (dbcf30f84454/-c1/-c2) and sidecar-bfmr-auth-headers
(df0f00dcb7e9/-c1): authoring failed twice the SAME way (refimpl fails its own tests),
the continuation guard declared it stuck, the run died with "Human needed." in a log
nobody read, and nothing was parked in the queue/dashboard.

Asserts the BEHAVIOUR of:
  * classify_author_failure / record_attempt / prior_failure_section -- the exact
    prior failure is carried into the next authoring prompt;
  * should_escalate + _author_escalate -- two same-class failures run exactly ONE
    escalation round (label -esc, 2x iterations, fake-browser helper in the prompt);
  * park_decision + run_chain + park_visible -- a failed top-level run parks its last
    job in needs_opus with "authoring stuck: <reason>, needs harness help" + log path;
    a clean / orphaned / under-slicer run does not.

Red-on-revert (run with --revert-check): each mutation below must turn the suite red.
Run: python3 test-auto-park-escalate.py [--revert-check]
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")

FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load():
    loader = SourceFileLoader("oda_park", str(AUTO))
    spec = importlib.util.spec_from_loader("oda_park", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def args(m, tmp, **kw):
    a = SimpleNamespace(label="sidecar-bfmr-auth-headers", bundle=None, lang="typescript",
                        intent="Fix BFMR sync 'fetch tracker 401': the bfmrInterceptorSource fetch "
                               "wrapper and XHR open/send/setRequestHeader must record headers",
                        interface=None, model="qwen3.8:27b-q4_K_M", host="studio-db",
                        author_max_iters=24, drafter_cmd=None, dest=None, slice_plan=None,
                        slice_id=None, escalate_model=None, escalate_host=None, no_park=False)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def main():
    m = load()
    tmp = Path(tempfile.mkdtemp(prefix="auto-park-"))
    m.AUTO_RUNS_DIR = tmp
    os.environ.pop("OLLAMA_DISPATCH_NO_SPLIT", None)
    os.environ.pop("AUTO_ESCALATE_MODEL", None)

    # --- classification -----------------------------------------------------
    own = f"VERIFY FAILED: refimpl.py {m.REFIMPL_OWN_TESTS_MARK} VERIFY_OK"
    check("class: refimpl fails own tests",
          m.classify_author_failure("stopped at iteration 12/24", own), "refimpl-fails-own-tests")
    check("class: byte-identical -> no-progress",
          m.classify_author_failure("x -- continuation round 1 left the harness byte-identical"), "no-progress")
    check("class: iteration cap",
          m.classify_author_failure("hit iteration cap (24/24); VERIFY FAILED"), "iteration-cap")
    check("class: contaminated",
          m.classify_author_failure(m.CONTAMINATED_PREFIX + "x"), "contaminated")

    # --- attempt ledger + prior failure carried into the next prompt ----------
    a = args(m, tmp)
    m.record_attempt(a, "df0f00dcb7e9", "stopped at iteration 12/24", own + "\nAssertionError: expected Bearer")
    m.record_attempt(a, "df0f00dcb7e9", "stopped at iteration 12/24", None)   # same job: update, keep tail
    hist = m.load_attempts(a)
    check("ledger dedupes by job", len(hist), 1)
    check("ledger keeps the richer check tail", "AssertionError" in (hist[0].get("check_tail") or ""), True)
    sec = m.prior_failure_section(hist)
    check("prior failure section names the class", "refimpl-fails-own-tests" in sec, True)
    check("prior failure section carries the exact self-check", "expected Bearer" in sec, True)
    prompt = m.author_prompt(a, "sidecar/src/bfmr.js")
    check("author prompt carries the prior failure", "PRIOR ATTEMPT FAILED" in prompt and "expected Bearer" in prompt, True)
    check("author prompt embeds the fake-browser helper for a browser-ish TS target",
          "installFakeBrowser" in prompt, True)
    py = args(m, tmp, lang="python", label="plain-py", intent="parse a csv")
    check("python target gets no fake-browser helper", m.fake_browser_section(py, "app/x.py"), "")
    check("no history -> no prior-failure section", "PRIOR ATTEMPT FAILED" in m.author_prompt(py, "app/x.py"), False)

    # --- escalate once after 2 same-class failures ---------------------------
    check("1 failure -> no escalation", m.should_escalate(hist), False)
    m.record_attempt(a, "13c9004056d5", "continuation round 1 left the harness byte-identical", own)
    hist = m.load_attempts(a)
    check("2 same-class failures -> escalate", m.should_escalate(hist), True)
    check("different classes -> no escalation",
          m.should_escalate([{"class": "syntax-error"}, {"class": "iteration-cap"}]), False)
    check("already escalated in this process -> never again",
          m.should_escalate(hist + [{"class": hist[-1]["class"], "escalated": True, "pid": os.getpid()}]), False)

    calls = []
    m._harness_check_output = lambda wt, v, t=None, b=None: (own + "\nstill red", None)

    def fake_dispatch(wt, prompt, label, verify, aa, max_iters=None):
        calls.append({"label": label, "max_iters": max_iters, "prompt": prompt,
                      "model": aa.model})
        aa._chain_last_job = "esc000000001"
        return False, "stopped at iteration 30/48; VERIFY FAILED"
    m.dispatch_model = fake_dispatch
    m.chain_state_write = lambda *x, **k: None
    a._chain_last_job = "13c9004056d5"
    ok, why = m._author_escalate(a, Path(tmp), "sidecar/src/bfmr.js", "python3 auto-harness-check.py",
                                 "continuation round 1 left the harness byte-identical")
    check("escalation round dispatched exactly once", len(calls), 1)
    check("escalation round label is -esc", calls and calls[0]["label"], "auto-author-sidecar-bfmr-auth-headers-esc")
    check("escalation round gets 2x iterations", calls and calls[0]["max_iters"], 48)
    check("escalation prompt has helper + prior failure",
          bool(calls) and "installFakeBrowser" in calls[0]["prompt"] and "PRIOR ATTEMPT FAILED" in calls[0]["prompt"], True)
    check("failed escalation still returns not-ok", ok, False)
    check("why says one escalation round ran", "after one escalation round" in why, True)
    ok2, _ = m._author_escalate(a, Path(tmp), "sidecar/src/bfmr.js", "python3 auto-harness-check.py", "again")
    check("a second _author_escalate in the same run does NOT escalate again", len(calls), 1)
    mm, hh, note = m.escalation_target(a, env={})
    check("no distinct served model -> same model, honest note", (mm, "no distinct" in note), (a.model, True))
    mm, hh, note = m.escalation_target(a, env={"AUTO_ESCALATE_MODEL": "qwen3-coder:480b", "AUTO_ESCALATE_HOST": "unraid"})
    check("distinct stronger model is used", (mm, hh), ("qwen3-coder:480b", "unraid"))

    # --- park decision / run_chain / park_visible ---------------------------
    check("park: clean exit -> no", m.park_decision(a, 0, "j"), False)
    check("park: orphaned -> no", m.park_decision(a, m.EXIT_ORPHANED, "j"), False)
    check("park: failure with a job -> yes", m.park_decision(a, 1, "j"), True)
    check("park: refusal before any job -> no", m.park_decision(a, 2, None), False)
    check("park: under a slicer -> no (slicer owns it)",
          m.park_decision(args(m, tmp, slice_plan="p", slice_id="s1"), 1, "j"), False)

    parked = []
    fake_park = lambda aa, code, reason: parked.append((code, reason))

    def dies(aa):
        aa._chain_last_job = "13c9004056d5"
        m.die("authoring did not converge: x\n[auto] auto-requeue NOT applicable: Human needed.", 1)
    try:
        m.run_chain(args(m, tmp), do=dies, park=fake_park)
    except SystemExit as e:
        check("run_chain re-raises the exit code", e.code, 1)
    check("run_chain parks a died run once", len(parked), 1)
    check("park reason is the die() message", bool(parked) and "Human needed" in parked[0][1], True)
    parked.clear()
    m.run_chain(args(m, tmp), do=lambda aa: 0, park=fake_park)
    check("run_chain does not park a clean run", parked, [])

    def orphan(aa):
        aa._chain_last_job = "x"
        return m.EXIT_ORPHANED
    m.run_chain(args(m, tmp), do=orphan, park=fake_park)
    check("run_chain does not park an orphaned run", parked, [])

    ran = []
    rec = m.park_visible(args(m, tmp, _chain_last_job="13c9004056d5"), 1, "authoring did not converge: stuck",
                         runs_dir=tmp, _run=lambda cmd: (ran.append(cmd), (0, "escalated", ""))[1],
                         log="/tmp/bfmr-auth.auto.log")
    cmd = ran[0] if ran else []
    check("park escalates the LAST job id", cmd[2:4] if len(cmd) > 3 else None, ["escalate", "13c9004056d5"])
    reason = cmd[cmd.index("--reason") + 1] if "--reason" in cmd else ""
    check("park reason says authoring stuck + needs harness help",
          reason.startswith("authoring stuck:") and "needs harness help" in reason, True)
    check("park reason carries the log path", "/tmp/bfmr-auth.auto.log" in reason, True)
    check("park passes the log as gate output", "--gate-output" in cmd, True)
    chain = json.loads((tmp / "sidecar-bfmr-auth-headers.json").read_text())
    check("chain record is stamped parked", chain.get("parked", {}).get("job"), "13c9004056d5")
    ran.clear()
    m.park_visible(args(m, tmp), 1, "scaffold crashed", runs_dir=tmp,
                   _run=lambda cmd: (ran.append(cmd), (0, "", ""))[1], log=None)
    check("no job -> needs_opus placeholder via escalate --new", "--new" in (ran[0] if ran else []), True)
    # pruned live row (superseded-reservations-v3): escalate <id> -> rc=1 "job not found"
    ran.clear()
    def pruned(cmd):
        ran.append(cmd)
        return (1, "", "job not found: ab1d48f84630") if "--new" not in cmd else (0, "placeholder", "")
    m.park_visible(args(m, tmp, _chain_last_job="ab1d48f84630"), 1, "preflight NO-GO", runs_dir=tmp,
                   _run=pruned, log="/tmp/v3.auto.log")
    check("pruned row -> falls back to escalate --new", len(ran) == 2 and "--new" in ran[1], True)
    check("fallback keeps the gate output", len(ran) == 2 and "--gate-output" in ran[1], True)
    ran.clear()
    m.park_visible(args(m, tmp, _chain_last_job="ab1d48f84630"), 1, "x", runs_dir=tmp,
                   _run=lambda cmd: (ran.append(cmd), (1, "", "database is locked"))[1], log=None)
    check("other escalate failure -> no placeholder spam", len(ran), 1)

    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("should_escalate never fires", "    return bool(a1) and a1 == a2 and a1 != \"contaminated\"",
     "    return False"),
    ("park_decision always False", "    if code == 2 and not last_job:\n        return False\n    return True",
     "    return False"),
    ("run_chain stops parking", "                (park or park_visible)(a, code, reason)", "                pass"),
    ("prior failure not carried", "{fake_browser_section(a, target)}{prior_failure_section(load_attempts(a))}",
     "{fake_browser_section(a, target)}"),
    ("pruned-row fallback removed", 'if job and rc != 0 and "not found" in', 'if False and'),
    ("park reason loses the log path", '+ (f" (log: {log})" if log else ""))', ')'),
]


def revert_check():
    src = AUTO.read_text()
    bad = 0
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"mutation anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-oda", delete=False) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "AUTO_SRC": f.name},
                           capture_output=True, text=True)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
