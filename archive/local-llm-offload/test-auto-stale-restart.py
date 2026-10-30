#!/usr/bin/env python3
"""Stale-driver self-restart (ollama-dispatch-auto, 2026-10-06).

Python runs a driver from memory: drivers started before a fix keep the old code for
hours (75088 wrote an AUTO-TASK.md without the optout-guard text long after that fix
landed). Now each driver stamps its code (chain record "code"), and at the top of a
preflight round -- between steps, its own job finished, no gate running -- a driver
whose file changed on disk re-execs ITSELF via the existing --resume-harness path.
Capped (per label / per day), alerting, never from outside.

Hermetic: temp ledger, temp source copy, execv/check/notify/gate injected. Never execs,
never touches a live driver. --revert-check mutates AUTO_SRC and requires RED."""
import importlib.util, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f" -- got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def load():
    ld = SourceFileLoader("oda_stale", str(AUTO))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("oda_stale", ld))
    sys.argv = [str(AUTO)]
    ld.exec_module(m)
    return m


class Exec(Exception):
    pass


def main():
    os.environ.pop("AUTO_NO_STALE_RESTART", None)
    m = load()
    T = Path(tempfile.mkdtemp(prefix="stale-"))
    wt = T / "wt"
    wt.mkdir()
    newsrc = T / "auto-new"
    newsrc.write_text(AUTO.read_text() + "\n# a later fix\n")
    same = T / "auto-same"
    same.write_text(AUTO.read_text())
    a = SimpleNamespace(label="rt-x", lang="typescript", require=[], drafter_cmd=None,
                        _chain_last_job="abc123")
    calls, notes = [], []

    def execv(exe, argv):
        calls.append(argv)
        raise Exec()

    def run(src=newsrc, ledger=None, ok=True, gate=False, argv=None, now=None):
        sys.argv = argv or [str(AUTO), "--label", "rt-x", "--repo", "/r"]
        try:
            return m.maybe_restart_stale(
                a, wt, "lib/x.ts", execv=execv, src=src, ledger=ledger or T / "l.json",
                check=lambda: (0, "VERIFY_OK") if ok else (1, "FAIL x"),
                notify=lambda t, msg, k: notes.append(t), gate_alive=lambda j: gate,
                now=now or "2026-10-06T07:00:00Z")
        except Exec:
            return "EXEC"

    # resume_harness_check needs an authored harness
    for f in m.HARNESS_FILES_REQUIRED:
        (wt / f).write_text("x\n")
    m.refresh_harness_check = lambda *x, **k: None
    m.enforce_target_at_head = lambda *x, **k: []
    m.unmark_go_stamp = lambda *x, **k: []

    check("CODE_STAMP is the sha of the running file", m.CODE_STAMP, m._code_stamp(AUTO))
    check("unchanged code on disk -> no restart", run(src=same), "current")
    check("changed code on disk -> re-exec", run(), "EXEC")
    check("...via --resume-harness, same argv", calls[-1][3:],
          ["--label", "rt-x", "--repo", "/r", "--resume-harness"])
    check("...running the on-disk file", calls[-1][2], str(newsrc))
    check("...alerted", notes[-1], "stale auto driver restarted")
    run(argv=[str(AUTO), "--label", "rt-x", "--resume-harness"])
    check("an already-resumed run gets ONE --resume-harness",
          calls[-1].count("--resume-harness"), 1)
    n = len(calls)
    check("3rd restart of one label in a day -> capped", run().startswith("cap reached"), True)
    check("...no exec", len(calls), n)
    check("...cap alerts", notes[-1], "stale auto driver (cap reached)")
    check("next day the label may restart again", run(now="2026-10-07T01:00:00Z"), "EXEC")
    led = T / "busy.json"
    import json
    led.write_text(json.dumps({"events": [{"label": f"o{i}", "at": "2026-10-06T01:00:00Z"}
                                          for i in range(6)]}))
    check("6 restarts today across labels -> capped", run(ledger=led).startswith("cap"), True)
    check("gate still running for the last job -> not restarted",
          run(ledger=T / "g.json", gate=True).startswith("gate still running"), True)
    bad = T / "auto-bad"
    bad.write_text(AUTO.read_text() + "\ndef (:\n")
    check("on-disk code that does not compile -> not restarted",
          run(src=bad, ledger=T / "b.json").startswith("on-disk code does not compile"), True)
    check("harness would not self-check -> not restarted",
          run(ledger=T / "r.json", ok=False).startswith("resume would refuse"), True)
    a.drafter_cmd = "x"
    check("--drafter-cmd (test) run -> not restarted",
          run(ledger=T / "d.json").startswith("drafter-cmd"), True)
    a.drafter_cmd = None
    os.environ["AUTO_NO_STALE_RESTART"] = "1"
    check("AUTO_NO_STALE_RESTART=1 disables", run(ledger=T / "e.json").startswith("disabled"), True)
    os.environ.pop("AUTO_NO_STALE_RESTART")

    # the chain record carries the stamp
    rd = T / "runs"
    rd.mkdir()
    ca = SimpleNamespace(label="rt-x", bundle="rt-x", slice_plan=None, slice_id=None)
    m.chain_key = lambda a_: "rt-x"
    fp = m.chain_state_write(ca, "advancing", runs_dir=rd)
    rec = json.loads(Path(fp).read_text())["runs"]["rt-x"]
    check("chain record stamps the driver's code", rec.get("code"), m.CODE_STAMP)

    # wired into the preflight loop: the restart happens BEFORE the round's preflight
    pre = []
    m.maybe_restart_stale = lambda *x, **k: (_ for _ in ()).throw(Exec())
    m.slice_already_satisfied = lambda *x: False
    m.slice_taken_elsewhere = lambda *x: None
    m.driver_gone = lambda a_: None
    m.chain_state_write = lambda *x, **k: None
    m.must_contain_from_task = lambda wt_: []
    m.run_preflight = lambda *x: pre.append(1) or (0, {"verdict": "GO"})
    la = SimpleNamespace(require=[], max_rounds=4, no_progress_rounds=2, author_max_iters=24,
                         lang="typescript", slice_plan=None, slice_id=None, label="x")
    try:
        m._preflight_loop(la, wt, "lib/x.ts", "python3 auto-harness-check.py")
        r = "returned"
    except Exec:
        r = "EXEC"
    check("_preflight_loop checks for a stale driver at the top of a round", r, "EXEC")
    check("...before running that round's preflight", pre, [])
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("never restarts", "    if not disk or not CODE_STAMP or disk == CODE_STAMP:",
     "    if True:"),
    ("no cap", "            or len(ev) >= STALE_RESTART_PER_DAY):", "            or False):"),
    ("ignores a running gate", "    if job and (gate_alive or _gate_hook_alive)(job):",
     "    if False:"),
    ("no compile check", '        compile(src.read_text(), str(src), "exec")', "        pass"),
    ("no resume precheck", "    if not ok:\n        note(\"stale auto driver (not restarted)\"",
     "    if False:\n        note(\"stale auto driver (not restarted)\""),
    ("not wired into the loop", "        _stale = maybe_restart_stale(a, wt, target)\n",
     "        _stale = None\n"),
    ("chain record unstamped", '                "code": CODE_STAMP,', ""),
    ("resume flag dropped", "    argv = [x for x in sys.argv[1:] if x != RESUME_FLAG] + [RESUME_FLAG]",
     "    argv = [x for x in sys.argv[1:] if x != RESUME_FLAG]"),
]


def revert_check():
    bad = 0
    src = AUTO.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-auto", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "AUTO_SRC": f.name},
                           capture_output=True, text=True, timeout=300)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
