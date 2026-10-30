#!/usr/bin/env python3
"""The TS verify must name EVERY failing test case, not only those inside the last 30
lines of runner output (rt-egift-link-s1-s4, 2026-10-06). Renders the real
NODE_TEST_VERIFY template and runs its test section with a fake runner emitting 6
cases (5 failing) followed by 60 lines of tail noise. --revert-check: SCAFFOLD_SRC."""
import importlib.util, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
SRC = Path(os.environ.get("SCAFFOLD_SRC") or Path(__file__).resolve().parent / "ollama-dispatch-scaffold")
ld = SourceFileLoader("scaf_fc", str(SRC))
m = importlib.util.module_from_spec(importlib.util.spec_from_loader("scaf_fc", ld))
sys.argv = [str(SRC)]; ld.exec_module(m)
txt = m.NODE_TEST_VERIFY.format(label="t", target="lib/a.ts", ts_parse="/x", bootstrap="",
                                mark="SCAFFOLD_INCOMPLETE", target_err="lib/a", test_files="v.test.ts")
a = txt.index('_tout=$(NODE_OPTIONS'); b = txt.index('echo "=== repo suite', a)
sec = txt[a:b]
script = 'fails=0\nRUNNER=./fakerun\nTEST_FILES=v.test.ts\n' + sec
d = Path(tempfile.mkdtemp())
(d/"fakerun").write_text("#!/bin/sh\n" + "".join(
    f"echo 'not ok {i} - case number {i}'\n" if i != 3 else "echo 'ok 3 - fine'\n" for i in range(1, 7))
    + "i=0; while [ $i -lt 60 ]; do echo \"# noise $i\"; i=$((i+1)); done\necho '# tests 6'\nexit 1\n")
os.chmod(d/"fakerun", 0o755)
out = subprocess.run(["bash", "-c", script], cwd=d, capture_output=True, text=True).stdout
fails = []
for i in (1, 2, 4, 5, 6):
    ok = f"FAILED CASE: not ok {i} - case number {i}" in out
    print(("ok  " if ok else "FAIL") + f" - failing case {i} named in output")
    if not ok: fails.append(i)
ok = "FAILED CASE: not ok 3" not in out
print(("ok  " if ok else "FAIL") + " - passing case not listed")
if not ok: fails.append(3)
sys.exit(1 if fails else 0)
