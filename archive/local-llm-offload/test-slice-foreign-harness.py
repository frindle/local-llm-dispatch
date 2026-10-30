#!/usr/bin/env python3
"""The slicer refuses to author on a base that TRACKS another task's dispatch harness
(2026-10-05, s3fix: replay-endorse-20261003 tracked a leaked bfmr-split-wiring
TASK.md/verify.sh/refimpl.py/check_literals.py, so every s3 author job launched
'dirty (4 paths)' and started from the wrong task's harness).

Usage:  python3 test-slice-foreign-harness.py          -> ALL PASS
        SLICE=<slice .bak> python3 ...                 -> FAILs (revert-check)
"""
import importlib.util
import inspect
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SLICE = Path(os.environ.get("SLICE") or HERE / "ollama-dispatch-slice")
fails = []


def chk(name, got, want):
    ok = got == want
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        fails.append(name)


ld = SourceFileLoader("slice_fh", str(SLICE))
sp = importlib.util.spec_from_loader("slice_fh", ld)
sl = importlib.util.module_from_spec(sp)
ld.exec_module(sl)
fth = getattr(sl, "foreign_tracked_harness", lambda tree, label: [])


def repo(files, untracked=()):
    d = tempfile.mkdtemp()
    g = lambda *a: subprocess.run(["git", "-C", d, *a], capture_output=True, check=True)
    g("init", "-q")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (Path(d) / "lib.ts").write_text("x\n")
    for p, c in files.items():
        (Path(d) / p).write_text(c)
    g("add", "-A")
    g("commit", "-qm", "base")
    for p, c in dict(untracked).items():
        (Path(d) / p).write_text(c)
    return d


H4 = {"TASK.md": "# TASK: bfmr-split-wiring\n", "verify.sh": "x", "refimpl.py": "x",
      "check_literals.py": "x"}
r = repo(H4)
chk("foreign tracked harness (TASK bfmr-split-wiring, plan replay-endorse) is detected",
    sorted(fth(r, "replay-endorse")), sorted(H4))
r = repo({"TASK.md": "# TASK: bg-captcha-s1-chain\n", "verify.sh": "x"})
chk("a sub-plan's OWN parent harness (TASK bg-captcha-s1-chain, plan bg-captcha-s1) is not foreign",
    fth(r, "bg-captcha-s1"), [])
r = repo({}, untracked={"TASK.md": "# TASK: other\n", "verify.sh": "x"})
chk("UNTRACKED harness files (the normal scaffold) are not foreign", fth(r, "replay-endorse"), [])
r = repo({})
chk("a clean repo -> []", fth(r, "replay-endorse"), [])
chk("not a repo -> [] (fail-open, never blocks on an error)", fth("/nonexistent-dir", "x"), [])

ex = inspect.getsource(sl.execute)
i_chk, i_launch = ex.find("foreign_tracked_harness("), ex.find("off chain tip ===")
chk("execute() checks for a foreign tracked harness BEFORE every authoring launch",
    0 <= i_chk < i_launch, True)

# the real fixture, after the s3fix strip, is clean
T = Path.home() / ".ollama-dispatch" / "testprojects" / "replay-endorse-20261003"
if T.is_dir():
    chk("replay-endorse-20261003 no longer tracks the foreign harness", fth(T, "replay-endorse"), [])

print()
print("ALL PASS" if not fails else f"{len(fails)} FAIL")
sys.exit(1 if fails else 0)
