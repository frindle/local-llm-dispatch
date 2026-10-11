#!/usr/bin/env python3
"""g6: what ~/bin/test-qagent.py::test_bounds_always_set_and_no_sampling asserts (the DEFAULT bounds, not the
later --max-tokens 500 override). Ground truth is parsed from the test source at grading time."""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import *

ans = answer_line(read_answer())
if ans is None:
    verdict(False, "no final 'ANSWER:' line")
src = (BIN / "test-qagent.py").read_text()
body = re.search(r"def test_bounds_always_set_and_no_sampling\(self\):(.*?)(?=\n    def )", src, re.S).group(1)
first = lambda flag: re.search(r'"%s"\) \+ 1\], "(\d+)"' % re.escape(flag), body).group(1)
truth = {"max_tokens": first("--max-tokens"), "max_iters": first("--max-iters"), "chat_timeout": first("--chat-timeout")}
absent = set(re.findall(r'"(--[\w-]+)"', re.search(r"for f in \((.*?)\)", body, re.S).group(1)))
got = kv(ans)
bad = [f"{k}: said {got.get(k)}, truth {v}" for k, v in truth.items() if got.get(k) != v]
if names(got.get("absent")) != absent:
    bad.append(f"absent: said {sorted(names(got.get('absent')))}, truth {sorted(absent)}")
verdict(not bad, "; ".join(bad) if bad else f"{truth} absent={sorted(absent)}")
