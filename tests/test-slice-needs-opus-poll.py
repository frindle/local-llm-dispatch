#!/usr/bin/env python3
"""An ENQUEUED slice whose coding job the queue moved to needs_opus is ESCALATED by
the poll (2026-10-02, Rivian s4 5983078bfe69). The gate sends a reviewer code-high
straight to the needs_opus lane, so the queue never reports `done`; the poll printed
"still running" every 5 min for 2 h and --accept-slice refused the slice ("is
'enqueued', not 'escalated'"). Drives the REAL execute() with side effects stubbed.
--revert-check mutates the slicer (SLICE_SRC env) and requires RED."""
import importlib.util, io, os, subprocess, sys, tempfile, contextlib
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("SLICE_SRC") or HERE / "ollama-dispatch-slice")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load():
    ld = SourceFileLoader("sl_nop", str(SRC))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("sl_nop", ld))
    sys.argv = [str(SRC)]
    ld.exec_module(m)
    return m


def run(m, js):
    m.ensure_chain_worktree = lambda st: "/nope/chain"
    m.save_state = lambda st: None
    m.publish_dag = lambda st: []
    m.vacuous_gate_reason = lambda *a, **k: None
    m.slice_job_inflight = lambda st, sid: None
    m.harness_authored = lambda wt: True
    m.clean_and_seal = lambda wt: None
    m.remove_worktree = lambda cwt, wt: None
    m._park_or_advance = lambda *a, **k: None
    for fn in ("harvest_ready_slices", "auto_land_passed"):
        if hasattr(m, fn):
            setattr(m, fn, lambda *a, **k: [])
    m.job_status = lambda jid: js
    m.gate_record = lambda jid: {"verdict": "fail", "counts": {"code_high": 1},
                                 "issues": [{"severity": "high", "category": "code",
                                             "file": "lib/x.ts", "line": 3, "what": "w"}]}
    m.done_sidecar_age_s = lambda jid, now=None: 10.0
    slices = {"s1": {"status": m.ENQUEUED, "title": "one", "depends_on": [],
                     "worktree": "/wt/s1", "job_id": "job1"}}
    st = {"label": "alpha", "repo": "/nope", "target": "x.ts", "lang": "typescript",
          "chain_branch": "slice/alpha", "chain_worktree": "/nope/chain",
          "dag_published": True, "order": ["s1"], "slices": slices}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            m.execute(st, "mdl", "studio", 20, 1200, "auto", 32768)
        except SystemExit:
            pass
    return st["slices"]["s1"], out.getvalue()


def main():
    m = load()
    s, out = run(m, "needs_opus")
    check("needs_opus coding job -> slice ESCALATED", s["status"], m.ESCALATED)
    check("...with the gate's findings as the reason", "lib/x.ts:3" in (s.get("escalation_reason") or ""), True)
    check("...and NOT reported as still running", "still running" in out, False)
    s, out = run(m, "running")
    check("a genuinely running job stays ENQUEUED", s["status"], m.ENQUEUED)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("needs_opus branch removed", '            if js == "needs_opus":\n                # BUG (2026-10-02, Rivian s4',
     '            if False:\n                # BUG (2026-10-02, Rivian s4'),
    ("status not set", '                s["status"] = ESCALATED\n                if rec.get("verdict"):\n                    s["gate_verdict"] = rec.get("verdict")\n                s["escalation_reason"] = gate_escalation_reason(rec, s["job_id"], sid)\n                save_state(st)\n                print(f"# {sid}: ESCALATED (queue needs_opus)',
     '                pass\n                if rec.get("verdict"):\n                    s["gate_verdict"] = rec.get("verdict")\n                s["escalation_reason"] = gate_escalation_reason(rec, s["job_id"], sid)\n                save_state(st)\n                print(f"# {sid}: ESCALATED (queue needs_opus)'),
]


def revert_check():
    bad = 0
    src = SRC.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-slice", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "SLICE_SRC": f.name},
                           capture_output=True, text=True, timeout=300)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
