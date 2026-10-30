#!/usr/bin/env python3
"""A DONE slice must pick up the AUTHORITATIVE regate verdict that lands after it was
landed on the pre-gate's verdict (2026-10-05, chat-frontend-plan s7-cli: slice said
'pass', the regate said 'concerns').

Checks (temp STATE_ROOT/LOG_DIR -- never the live state, never the queue):
  - a DONE slice whose regate is done with a different settled verdict is updated,
    keeps the prior verdict, and its STATUS is unchanged;
  - the record is found in archive/ too (the archived-parent class);
  - a pending regate, an unsettled verdict, a non-DONE slice, an equal verdict are
    all left alone;
  - load_state() persists the update to disk (field-scoped save).

Revert-test: SLICE_SRC=<backup> python3 test-slice-late-regate-verdict.py -> FAILs.
"""
import importlib.util
import json
import os
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

SRC = Path(os.environ.get("SLICE_SRC") or Path(__file__).resolve().parent / "ollama-dispatch-slice")
FAILS = []


def check(n, ok):
    print(("ok  " if ok else "FAIL") + ": " + n)
    if not ok:
        FAILS.append(n)


def main():
    ld = SourceFileLoader("sl_late", str(SRC))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("sl_late", ld))
    sys.argv = [str(SRC)]
    ld.exec_module(m)
    with tempfile.TemporaryDirectory() as td:
        m.STATE_ROOT = os.path.join(td, "runs")
        m.LOG_DIR = os.path.join(td, "logs")
        os.makedirs(os.path.join(m.LOG_DIR, "archive"))
        os.makedirs(m.STATE_ROOT)

        def gate(jid, verdict, regate, archived=False):
            d = os.path.join(m.LOG_DIR, "archive") if archived else m.LOG_DIR
            Path(d, f"{jid}.gate.json").write_text(json.dumps({"verdict": verdict, "regate": regate}))

        gate("J7", "concerns", "done")             # late authoritative concerns
        gate("J8", "fail", "done", archived=True)  # same, parent cleared to archive/
        gate("J2", "pass", "done")                 # equal verdict
        gate("J3", "concerns", "pending")          # regate not landed yet
        gate("J4", "fc-pending", "done")           # unsettled verdict
        gate("J5", "fail", "done")                 # slice not DONE
        sl = {
            "s7": {"status": "done", "job_id": "J7", "gate_verdict": "pass"},
            "s8": {"status": "done", "job_id": "J8", "gate_verdict": "pass"},
            "s2": {"status": "done", "job_id": "J2", "gate_verdict": "pass"},
            "s3": {"status": "done", "job_id": "J3", "gate_verdict": "pass"},
            "s4": {"status": "done", "job_id": "J4", "gate_verdict": "pass"},
            "s5": {"status": "enqueued", "job_id": "J5", "gate_verdict": None},
        }
        order = ["s7", "s8", "s2", "s3", "s4", "s5"]
        st = {"label": "p", "order": order, "slices": json.loads(json.dumps(sl))}
        fn = getattr(m, "refresh_done_gate_verdicts", None)
        check("refresh_done_gate_verdicts exists", callable(fn))
        if callable(fn):
            got = fn(st)
            check("only the two late-regate DONE slices change", sorted(got) == ["s7", "s8"])
            s7 = st["slices"]["s7"]
            check("s7 takes the authoritative 'concerns'", s7["gate_verdict"] == "concerns")
            check("s7 keeps the prior 'pass'", s7.get("gate_verdict_prior") == "pass")
            check("s7 status unchanged (no re-land)", s7["status"] == "done")
            check("archived record is honoured (s8 -> fail)", st["slices"]["s8"]["gate_verdict"] == "fail")
            for sid in ("s2", "s3", "s4"):
                check(f"{sid} left alone", st["slices"][sid]["gate_verdict"] == "pass"
                      and "gate_verdict_prior" not in st["slices"][sid])
            check("non-DONE slice untouched", st["slices"]["s5"]["gate_verdict"] is None)
        # through load_state -> persisted on disk
        Path(m.STATE_ROOT, "p.json").write_text(json.dumps(
            {"label": "p", "order": order, "slices": sl, "repo": td, "target": "x.py"}))
        plan = {"label": "p", "repo": td, "target": "x.py",
                "slices": [{"id": s, "title": s, "intent": s} for s in order]}
        try:
            m.load_state(plan)
            disk = json.loads(Path(m.STATE_ROOT, "p.json").read_text())
            check("load_state persists the late verdict to disk",
                  disk["slices"]["s7"].get("gate_verdict") == "concerns"
                  and disk["slices"]["s7"]["status"] == "done")
        except Exception as e:  # noqa: BLE001
            check(f"load_state ran ({type(e).__name__}: {e})", False)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
