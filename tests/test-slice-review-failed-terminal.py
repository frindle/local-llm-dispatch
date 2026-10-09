#!/usr/bin/env python3
"""A gate record at pass-pending-review whose review FAILED (review="failed (...)")
is TERMINAL for the slicer: nothing re-runs a failed review, so waiting for it only
burned the whole wait -- 20 min (awaiting-gate grace) on a failed authoring slice,
2 h (GATE_VERDICT_WAIT_S) on a done coding job (2026-10-02, Rivian s1 r2
fc6eec003057 held the bundle 27 min). A review still "pending" keeps waiting.
--revert-check mutates the slicer (SLICE_SRC env) and requires RED."""
import importlib.util, os, subprocess, sys, tempfile
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


def main():
    ld = SourceFileLoader("sl_rft", str(SRC))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("sl_rft", ld))
    sys.argv = [str(SRC)]
    ld.exec_module(m)
    st = {"label": "plan", "slices": {"s1": {}}}
    stat = ({}, {"auto-refine-plan-s1-r2": ("j1", "done")})
    failed = {"verdict": "pass-pending-review", "review": "failed (no report produced)"}
    pending = {"verdict": "pass-pending-review", "review": "pending"}
    aw = lambda rec: m.slice_job_awaiting_gate(st, "s1", statuses=stat, gate=lambda j: rec,
                                               age=lambda j: 60.0)
    check("awaiting-gate: failed review -> NOT awaiting (no verdict is coming)", aw(failed), None)
    check("awaiting-gate: pending review -> still awaiting", bool(aw(pending)), True)
    check("coding route: failed review -> land now (not after 2h)",
          m.coding_land_route("pass-pending-review", 60, "failed (no report produced)"), "land")
    check("coding route: pending review -> wait", m.coding_land_route("pass-pending-review", 60, "pending"), "wait")
    check("coding route: legacy 2-arg call still waits", m.coding_land_route("pass-pending-review", 60), "wait")
    src = SRC.read_text()
    check("both coding_land_route call sites pass the review",
          src.count('rec.get("review"))') >= 2, True)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("awaiting-gate ignores failed review",
     "        if gate_review_failed(rec):\n            continue  # its review died",
     "        if False:\n            continue  # its review died"),
    ("coding route ignores failed review",
     '    if gate_review_failed({"verdict": v, "review": review}):\n        return "land"',
     '    if False:\n        return "land"'),
    ("predicate too broad (pending counts)",
     '            and str(rec.get("review") or "").startswith("failed"))',
     '            )'),
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
