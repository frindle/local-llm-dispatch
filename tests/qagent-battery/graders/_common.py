"""Shared helpers for qagent-battery graders.

Contract: a grader is run as `python3 gN-*.py [answer-file]` (or answer text on stdin).
It recomputes ground truth from the live system AT GRADING TIME, prints one line
`PASS|FAIL: <reason>`, and exits 0 on PASS, 1 on FAIL.
"""
import os, re, sys
from pathlib import Path

HOME = Path(os.environ.get("QAGENT_BATTERY_HOME", str(Path.home())))
BIN = HOME / "bin"


def read_answer():
    return Path(sys.argv[1]).read_text() if len(sys.argv) > 1 else sys.stdin.read()


def answer_line(txt):
    """Last line that starts with 'ANSWER:' (markdown emphasis/backticks tolerated). None if absent."""
    found = None
    for ln in txt.splitlines():
        m = re.match(r"^\s*[`*>\-\s]*ANSWER\s*:\s*(.*?)[`*\s]*$", ln)
        if m:
            found = m.group(1)
    return found


def kv(ans):
    """'a=1; b=x,y' -> {'a': '1', 'b': 'x,y'}"""
    out = {}
    for part in re.split(r"[;\n]", ans or ""):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v.strip().strip("`'\"")
    return out


def names(s):
    return {x.strip().strip("`'\"") for x in re.split(r"[,\s]+", s or "") if x.strip()}


def verdict(ok, reason):
    print(("PASS: " if ok else "FAIL: ") + reason)
    sys.exit(0 if ok else 1)
