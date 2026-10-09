#!/usr/bin/env python3
"""Tests for the slicer's --review-notes path (escalation-autonomy ladder, 2026-10-02).

dispatch-self-heal re-authors a slice after an esc-review verdict (b) spec
under-specified / (c) harness defect without a usable patch / (d) capability by
running `ollama-dispatch-slice <plan> --retry-slice <sid> --review-notes FILE`.
The property: the NEXT authoring attempt sees the review's findings, instead of
repeating the same attempt blind.

  * retry_slice(notes=...) stores the notes on the slice, persisted through the real
    save_state (field-scoped merge), bounded to the last REVIEW_NOTES_KEEP;
  * authoring_intent() appends them to the intent (and is the bare intent without);
  * the AUTO authoring call passes authoring_intent(s), not s["intent"] (source-level
    wiring check -- the AUTO call sits deep inside --execute);
  * --review-notes is a CLI flag read in the --retry-slice branch.
Run: python3 test-slice-review-notes.py [--revert-check]
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
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
    loader = SourceFileLoader("slice_t", str(SRC))
    spec = importlib.util.spec_from_loader("slice_t", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    # HERMETIC (2026-10-06): never let a fixture reach the REAL queue. QUEUE ran
    # `ollama-queue.py status` / `plan-clear --group slice-alpha` against the live
    # daemon state on fake plans. python3 <devnull> is an empty script: exit 0, no rows.
    m.QUEUE = os.devnull
    return m


def main():
    m = load()
    root = Path(tempfile.mkdtemp(prefix="slnotes-")).resolve()
    m.STATE_ROOT = str(root)
    m.slice_job_inflight = lambda st, sid: None
    m.ensure_chain_worktree = lambda st: str(root / "chain")
    m.remove_worktree = lambda cwt, wt: None

    s = {"intent": "Add f().", "status": "failed", "job_id": "j1"}
    check("no notes -> the bare intent", m.authoring_intent(s), "Add f().")
    st = {"label": "p", "order": ["s1"], "slices": {"s1": dict(s)}}
    m.retry_slice(st, "s1", "VERDICT: (b) -- say what f returns for []")
    disk = json.loads((root / "p.json").read_text())["slices"]["s1"]
    check("retry resets to pending", (disk["status"], disk["job_id"]), (m.PENDING, None))
    check("the notes are persisted on the slice",
          disk.get("review_notes"), ["VERDICT: (b) -- say what f returns for []"])
    ai = m.authoring_intent(disk)
    check("the authoring intent starts with the intent", ai.startswith("Add f()."), True)
    check("...and carries the review's findings", "say what f returns for []" in ai, True)

    st["slices"]["s1"]["status"] = "failed"
    m.retry_slice(st, "s1", "second")
    st["slices"]["s1"]["status"] = "failed"
    m.retry_slice(st, "s1", "third " + "x" * 5000)
    notes = json.loads((root / "p.json").read_text())["slices"]["s1"]["review_notes"]
    check("bounded to the last REVIEW_NOTES_KEEP notes", (len(notes), notes[0]), (2, "second"))
    check("each note capped at REVIEW_NOTES_MAX chars", len(notes[1]), m.REVIEW_NOTES_MAX)
    st["slices"]["s1"]["status"] = "failed"
    m.retry_slice(st, "s1", None)
    check("a plain --retry-slice keeps the existing notes",
          len(json.loads((root / "p.json").read_text())["slices"]["s1"]["review_notes"]), 2)

    src = SRC.read_text()
    check("the AUTO authoring call uses authoring_intent(s)",
          ('"--intent", authoring_intent(s)' in src, '"--intent", s["intent"]' in src), (True, False))
    check("--review-notes is a CLI flag", '"--review-notes"' in src, True)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("notes not stored", '    if notes and notes.strip():\n        s["review_notes"]',
     '    if False:\n        s["review_notes"]'),
    ("notes not appended", '    if not notes:\n        return s["intent"]', '    if True:\n        return s["intent"]'),
    ("unbounded", ")[-REVIEW_NOTES_KEEP:]", ")"),
    ("AUTO call ignores notes", '"--intent", authoring_intent(s)', '"--intent", s["intent"]'),
]


def revert_check():
    bad = 0
    src = SRC.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        d = Path(tempfile.mkdtemp(prefix="slnmut-"))
        dst = d / "ollama-dispatch-slice"
        dst.write_text(src.replace(old, new))
        for sib in os.listdir(HERE):
            if sib.endswith(".py"):
                try:
                    os.symlink(HERE / sib, d / sib)
                except OSError:
                    pass
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "SLICE_SRC": str(dst)},
                           capture_output=True, text=True, timeout=120)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
