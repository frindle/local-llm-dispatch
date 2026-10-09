#!/usr/bin/env python3
"""Echoed review notes are NOT this run's failure (2026-10-02, Rivian s5).
A retried slice's intent carries the previous failure's review notes, which quote
its error lines verbatim; AUTO echoes the intent, so auto_failure_reason read the
stale `ollama-dispatch-scaffold: error: --symbol is required` + `ERROR: scaffold
failed (rc=2)` as THIS run's cause and escalated DETERMINISTICALLY -- although the
retry had scaffolded fine and failed on a verify timeout.
--revert-check mutates the slicer (SLICE_SRC env) and requires RED."""
import importlib.util, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("SLICE_SRC") or HERE / "ollama-dispatch-slice")
FAILS = []

TAIL = """[auto] num_ctx: 65536
[auto] intent : Change the existing fetchRivianServiceState() so it resolves X

NOTES FROM THE REVIEW OF A PREVIOUS FAILED ATTEMPT -- address these; the intent above still defines the property:
**VERDICT: (b)**
   ```
   ollama-dispatch-scaffold: error: --symbol is required for --lang typescript --kind symbol
   ERROR: scaffold failed (rc=2)
   ```
Traceback (most recent call last):
---
proposed fix: none written (no fix proposed)
[auto] target : lib/rivian.ts
[auto] authoring the harness (TASK.md + fixture + refimpl.py)...
    queued 2b78ab3d3c71 (auto-author-x); polling...
    2b78ab3d3c71: failed

ERROR: job 2b78ab3d3c71 failed (exit 1)
"""


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def main():
    ld = SourceFileLoader("sl_nnf", str(SRC))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("sl_nnf", ld))
    sys.argv = [str(SRC)]
    ld.exec_module(m)
    reason, det = m.auto_failure_reason(TAIL)
    check("quoted errors inside the notes do NOT make it deterministic", det, False)
    check("reason is THIS run's error only", reason, "ERROR: job 2b78ab3d3c71 failed (exit 1)")
    real = ("ollama-dispatch-scaffold: error: --symbol is required\nERROR: scaffold failed (rc=2)\n")
    r2, d2 = m.auto_failure_reason(real)
    check("a REAL scaffold error outside notes is still deterministic", d2, True)
    after = TAIL + "[auto] retrying\nERROR: scaffold failed (rc=2)\n"
    check("an error AFTER the notes block still counts", m.auto_failure_reason(after)[1], True)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("notes not dropped", "    lines = _drop_echoed_review_notes(\n        [ln.rstrip()",
     "    lines = (\n        [ln.rstrip()"),
    ("block never ends", '        if skipping and t.startswith("[auto]"):\n            skipping = False',
     '        if False:\n            skipping = False'),
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
