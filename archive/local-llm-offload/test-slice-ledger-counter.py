#!/usr/bin/env python3
"""The slicer's author-job counter must come from the never-pruned queue sidecars (via
failure_ledger), so a --retry-slice state reset / queue pruning cannot hide burned jobs
(2026-10-06 rt-egift-link-s1-s4: dashboard "attempt 14" vs slicer "3/8 jobs").
--revert-check: SLICE_SRC mutated copy (drops the ledger union)."""
import importlib.util, json, os, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("SLICE_SRC") or HERE / "ollama-dispatch-slice")
T = Path(tempfile.mkdtemp(prefix="slc-"))
(T/"q").mkdir(); (T/"l").mkdir()
os.environ.update({"FAILURE_LEDGER_QUEUE_LOGS": str(T/"q"), "FAILURE_LEDGER_LIVELOGS": str(T/"l"),
                   "FAILURE_LEDGER_STATE": str(T/"state.json"), "FAILURE_LEDGER": str(T/"f.jsonl")})
for i, lbl in enumerate(["auto-author-runX-s4", "auto-author-runX-s4-c1", "auto-refine-runX-s4-r1", "auto-author-runX-s5"]):
    (T/"q"/f"jj{i:010d}.done.json").write_text(json.dumps({"id": f"jj{i:010d}", "label": lbl, "status": "failed", "persisted_at": f"2026-10-06T10:0{i}:00Z"}))
FAILS = []
def check(n, ok):
    print(("ok  " if ok else "FAIL") + ": " + n)
    if not ok: FAILS.append(n)
ld = SourceFileLoader("sl_lc", str(SRC)); m = importlib.util.module_from_spec(importlib.util.spec_from_loader("sl_lc", ld))
sys.argv = [str(SRC)]; ld.exec_module(m)
st = {"label": "runX", "slices": {"s4": {"author_job_ids": [], "author_jobs_at_retry": 0}}}
n = m.refresh_author_job_ids(st, "s4", st["slices"]["s4"], rows=[])
check("state lost its ids, queue rows pruned: the 3 s4 jobs still counted from sidecars", n == 3 and len(st["slices"]["s4"]["author_job_ids"]) == 3)
check("another slice's job is not counted", "jj0000000003" not in st["slices"]["s4"]["author_job_ids"])
st["slices"]["s4"]["author_jobs_at_retry"] = 3
check("a human retry window forgives (window 0) but the ids stay in lifetime", m.refresh_author_job_ids(st, "s4", st["slices"]["s4"], rows=[]) == 0
      and len(st["slices"]["s4"]["author_job_ids"]) == 3)
sys.exit(1 if FAILS else 0)
