#!/usr/bin/env python3
"""AUTO's poller must not call a FINISHED job "vanished" (2026-10-14).

Live: cardBonuses c1 (a6c62df817ff), bfmrLinkReconcile c1 (320b4b9e7116), cardBonuses-v2 c1/c2
(9d7631feb093, f735190a6ca9) all finished done/exit 0 but the driver reported "job <id> vanished
from the queue for >300s without reaching a terminal state" and parked the runs. Mechanism: the
queue prunes a done row at once (RETAIN_DONE_RECENT=0); the poller's only durable fallback was the
handoff page, but handoff-autoclear acts on a finished no-deliverable authoring round within one
watcher pass and handoff-emit then sweeps the acted page, leaving only a status-less INDEX bullet.
The queue's own <id>.done.json sidecar (logs dir, then archive/) is never cleared and is now read first.

Drives the REAL dispatch_model poll loop with a stubbed queue (row always absent), a fake HOME whose
handoff page is already swept, and a stubbed clock. --revert-check mutates the fix. AUTO_SRC overrides."""
import importlib.util, json, os, subprocess, sys, tempfile
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
    ld = SourceFileLoader("auto_pt", str(AUTO))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("auto_pt", ld))
    argv, sys.argv = sys.argv, [str(AUTO)]
    try:
        ld.exec_module(m)
    finally:
        sys.argv = argv
    return m


JOB = "abcdef123456"


def run(m, sidecar=None, archived=False, env_off=False):
    fh = Path(tempfile.mkdtemp(prefix="pt-home-"))
    hd = fh / "Desktop" / "GitHub Projects" / "ollama" / "ollama-handoff"
    (hd / "complete").mkdir(parents=True)          # page already swept by handoff-emit
    (hd / "INDEX.md").write_text(f"- `{JOB}` auto-author-x-c1 -- 2026-10-14T08:27:45Z "
                                 "(auto-triage: no deliverable -- finished authoring round)\n")
    logs = fh / "bin" / "ollama-queue-logs"
    (logs / "archive").mkdir(parents=True)
    if sidecar:
        ((logs / "archive") if archived else logs).joinpath(f"{JOB}.done.json").write_text(json.dumps(sidecar))
    clock = {"t": 1_000_000.0}
    m.time = SimpleNamespace(time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s))
    m.capture = lambda cmd, **k: (0, f"enqueued {JOB}\n", "")
    m.job_row = lambda job: None
    m.driver_gone = lambda a: None
    m.chain_state_write = lambda *a, **k: None
    m.await_gate_tree_release = lambda *a, **k: None
    wt = Path(tempfile.mkdtemp(prefix="pt-wt-"))
    a = SimpleNamespace(drafter_cmd=None, lang="typescript", model="m", host="studio",
                        num_ctx=65536, author_max_iters=24, bundle="b", max_tokens=None,
                        timeout=10**9, slice_plan=None, slice_id=None, repo=None, no_auto_slice=True)
    oh, oenv = m.Path.home, os.environ.get("AUTO_SIDECAR_TERMINAL")
    m.Path.home = classmethod(lambda cls: fh)
    if env_off:
        os.environ["AUTO_SIDECAR_TERMINAL"] = "off"
    try:
        return m.dispatch_model(wt, "prompt", "auto-author-x-c1", "python3 x.py", a)
    finally:
        m.Path.home = oh
        os.environ.pop("AUTO_SIDECAR_TERMINAL", None)
        if oenv is not None:
            os.environ["AUTO_SIDECAR_TERMINAL"] = oenv


def main():
    m = load()
    ok, why = run(m, {"id": JOB, "status": "done", "exit_code": 0})
    chk("row pruned + page swept + live sidecar done -> done, NOT vanished", ok, True)
    ok, why = run(m, {"id": JOB, "status": "done", "exit_code": 0}, archived=True)
    chk("...same when the sidecar has moved to archive/", ok, True)
    ok, why = run(m, {"id": JOB, "status": "done_unconverged", "exit_code": 0})
    chk("done_unconverged sidecar is an OK terminal", ok, True)
    ok, why = run(m, {"id": JOB, "status": "failed", "exit_code": 1})
    chk("failed sidecar -> failed (not vanished)", (ok, "vanished" in why), (False, False))
    ok, why = run(m, {"id": JOB, "status": "running"})
    chk("in-flight sidecar is not terminal -> still a vanish after the grace", (ok, "vanished" in why), (False, True))
    ok, why = run(m)
    chk("no durable record at all is still a genuine vanish", (ok, "vanished" in why), (False, True))
    ok, why = run(m, {"id": JOB, "status": "done", "exit_code": 0}, env_off=True)
    chk("AUTO_SIDECAR_TERMINAL=off restores the old behaviour", (ok, "vanished" in why), (False, True))
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


MUTANTS = [
    ("sidecar step removed", "        o = _sidecar_outcome(job)\n        if o:\n            return o\n",
     "        o = None\n"),
    ("archive/ not searched", "    for d in (base, base / \"archive\"):\n        try:\n            rec = json.loads((d / f\"{job}.done.json\")",
     "    for d in (base,):\n        try:\n            rec = json.loads((d / f\"{job}.done.json\")"),
    ("failed sidecar read as done", '        if st in ("failed", "cancelled", "error", "blocked", "needs_opus"):\n            return "failed"\n',
     '        if st in ("failed", "cancelled", "error", "blocked", "needs_opus"):\n            return "done"\n'),
]


def revert_check():
    src, bad = AUTO.read_text(), 0
    for name, old, new in MUTANTS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-auto", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "AUTO_SRC": f.name},
                           capture_output=True, text=True, timeout=300)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": {name} -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
