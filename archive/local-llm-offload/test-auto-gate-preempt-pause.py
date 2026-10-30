#!/usr/bin/env python3
"""AUTO's poller must keep waiting on a gate_preempt / promote_preempt pause (2026-10-02).

Live: superseded-reservations-v2 continuation 76ee077e738e was paused by the QUEUE
(pause_reason=gate_preempt) so a --front regate could use the Studio lane. The
queue's own driver resumes such a job once the gate clears. A regate runs well
over 180s, though, and dispatch_model's poller only knew context_threshold and
request_more_iterations as auto-resumed. It declared the live round "stuck" after
180s and PARKED the whole run for a human. A pause no driver owns (external_sigterm)
must still go terminal after the grace.

Drives the REAL dispatch_model poll loop with a stubbed queue and clock.
--revert-check mutates the fix. AUTO_SRC overrides the file under test."""
import importlib.util, os, subprocess, sys, tempfile
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
    ld = SourceFileLoader("auto_gp", str(AUTO))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("auto_gp", ld))
    argv, sys.argv = sys.argv, [str(AUTO)]
    try:
        ld.exec_module(m)
    finally:
        sys.argv = argv
    return m


def run(m, rows):
    """rows: list of row dicts the queue reports on successive polls (15s apart)."""
    clock = {"t": 1_000_000.0}
    seq = iter(rows)
    m.time = SimpleNamespace(time=lambda: clock["t"],
                             sleep=lambda s: clock.__setitem__("t", clock["t"] + s))
    m.capture = lambda cmd, **k: (0, "enqueued abcdef123456\n", "")
    m.job_row = lambda job: next(seq)
    m.driver_gone = lambda a: None
    m.chain_state_write = lambda *a, **k: None
    wt = Path(tempfile.mkdtemp(prefix="agp-"))
    a = SimpleNamespace(drafter_cmd=None, lang="typescript", model="m", host="studio",
                        num_ctx=65536, author_max_iters=24, bundle="b", max_tokens=None,
                        timeout=10**9, slice_plan=None, slice_id=None, repo=None,
                        no_auto_slice=True)
    return m.dispatch_model(wt, "prompt", "auto-author-x-c1", "python3 x.py", a)


def main():
    m = load()
    paused = lambda r: {"status": "paused", "pause_reason": r, "exit_code": 3}
    for reason in ("gate_preempt", "promote_preempt"):
        rows = [{"status": "running"}] + [paused(reason)] * 40 + [{"status": "done"}]  # 600s paused
        ok, why = run(m, rows)
        chk(f"{reason} pause for 10 min (queue-owned resume) -> keeps polling to done", ok, True)
    rows = [{"status": "running"}] + [paused("external_sigterm")] * 40 + [{"status": "done"}]
    ok, why = run(m, rows)
    chk("external_sigterm pause (no driver owns it) -> terminal after the grace",
        (ok, "paused on 'external_sigterm'" in why), (False, True))
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


MUTANTS = [
    ("gate_preempt not auto-resumed", '''        AUTO_RESUMED = ("context_threshold", "request_more_iterations",
                        "gate_preempt", "promote_preempt")''',
     '''        AUTO_RESUMED = ("context_threshold", "request_more_iterations",
                        "promote_preempt")'''),
    ("promote_preempt not auto-resumed", '''        AUTO_RESUMED = ("context_threshold", "request_more_iterations",
                        "gate_preempt", "promote_preempt")''',
     '''        AUTO_RESUMED = ("context_threshold", "request_more_iterations",
                        "gate_preempt")'''),
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
