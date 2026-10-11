#!/usr/bin/env python3
"""g2: which non-backup file(s) directly in ~/bin define `def clip_tool_stream(`. Set-equality on basenames."""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import *

NAME = "clip_tool_stream"
ans = answer_line(read_answer())
if ans is None:
    verdict(False, "no final 'ANSWER:' line")
pat = re.compile(r"^\s*def %s\(" % NAME, re.M)
truth = set()
for p in BIN.iterdir():
    if not p.is_file() or ".bak" in p.name or ".pre-" in p.name or p.stat().st_size > 8_000_000:
        continue
    try:
        if pat.search(p.read_text(errors="replace")):
            truth.add(p.name)
    except Exception:
        pass
got = {n.split("/")[-1] for n in names(ans)}
verdict(got == truth and bool(truth), f"truth={sorted(truth)} got={sorted(got)}")
