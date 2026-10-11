#!/usr/bin/env python3
"""g3: three facts from ~/bin/queue-abort: default --wait seconds, its env vars, the 'refused' exit code."""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import *

ans = answer_line(read_answer())
if ans is None:
    verdict(False, "no final 'ANSWER:' line")
src = (BIN / "queue-abort").read_text()
wait = float(re.search(r'"--wait".*?default=([\d.]+)', src).group(1))
envs = set(re.findall(r'\b(QUEUE_ABORT_[A-Z_]+)\b', src))
refused = re.search(r"(\d)\s*=\s*refused", src).group(1)
got = kv(ans)
bad = []
try:
    if float(got.get("wait", "x")) != wait:
        bad.append(f"wait: said {got.get('wait')}, truth {wait:g}")
except ValueError:
    bad.append("wait: missing/non-numeric")
if names(got.get("env")) != envs:
    bad.append(f"env: said {sorted(names(got.get('env')))}, truth {sorted(envs)}")
if got.get("refused_exit") != refused:
    bad.append(f"refused_exit: said {got.get('refused_exit')}, truth {refused}")
verdict(not bad, "; ".join(bad) if bad else f"wait={wait:g} env={sorted(envs)} refused_exit={refused}")
