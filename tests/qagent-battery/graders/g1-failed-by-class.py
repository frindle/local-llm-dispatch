#!/usr/bin/env python3
"""g1: count FAILED queue jobs by failure_class from ollama-queue-state.json.
Ground truth recomputed now; +/-TOL per class because the live queue keeps moving."""
import collections, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import *

TOL = int(os.environ.get("BATTERY_G1_TOL", "1"))
CLASSES = ("model", "context", "operator", "nonconvergence_progressing")

ans = answer_line(read_answer())
if ans is None:
    verdict(False, "no final 'ANSWER:' line")
state = os.environ.get("QAGENT_STATE", str(BIN / "ollama-queue-state.json"))
jobs = json.load(open(state)).get("jobs", [])
truth = collections.Counter(j.get("failure_class") for j in jobs if j.get("status") == "failed")
got = kv(ans)
bad = []
for c in CLASSES:
    try:
        g = int(got[c])
    except Exception:
        bad.append(f"{c}: missing/non-int in answer"); continue
    if abs(g - truth.get(c, 0)) > TOL:
        bad.append(f"{c}: said {g}, truth {truth.get(c, 0)}")
verdict(not bad, "; ".join(bad) if bad else f"all classes within +/-{TOL} of {dict(truth)}")
