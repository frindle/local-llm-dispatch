#!/usr/bin/env python3
"""g4: argparse subcommands of ~/bin/ollama-queue.py (sub.add_parser("name")). Recall >=90% AND at most 2 invented names."""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import *

ans = answer_line(read_answer())
if ans is None:
    verdict(False, "no final 'ANSWER:' line")
src = (BIN / "ollama-queue.py").read_text(errors="replace")
truth = set(re.findall(r'\bsub\.add_parser\(\s*"([\w-]+)"', src))
got = names(ans)
extra = got - truth
recall = len(got & truth) / max(1, len(truth))
verdict(recall >= 0.90 and len(extra) <= 2,
        f"truth={len(truth)} recall={recall:.0%} missing={sorted(truth - got)[:8]} invented={sorted(extra)}")
