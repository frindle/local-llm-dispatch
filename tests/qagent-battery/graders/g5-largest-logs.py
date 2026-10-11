#!/usr/bin/env python3
"""g5: 5 largest *.log files directly in ~/bin/ollama-queue-logs (by bytes). Pass if >=4 of the true top-5 are named
(the logs are live and grow) and nothing named is outside the true top-8."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import *

ans = answer_line(read_answer())
if ans is None:
    verdict(False, "no final 'ANSWER:' line")
d = Path(os.environ.get("QAGENT_LOGDIR", str(BIN / "ollama-queue-logs")))
files = sorted((p for p in d.glob("*.log") if p.is_file()), key=lambda p: (-p.stat().st_size, p.name))
top5 = [p.name for p in files[:5]]; top8 = {p.name for p in files[:8]}
got = {n.split("/")[-1] for n in names(ans)}
hit = got & set(top5)
stray = sorted(got - top8)
verdict(len(hit) >= 4 and not stray, f"top5={top5} hits={len(hit)}/5 stray={stray}")
