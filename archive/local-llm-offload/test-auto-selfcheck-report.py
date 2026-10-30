#!/usr/bin/env python3
"""Wiring test: ollama-dispatch-auto (a) renders a self-check that names WHERE it failed, (b) injects the
heavy-global example + no-invented-defect rule into the author prompt.  --auto PATH (default ~/bin/ollama-dispatch-auto).
Fails on an auto that lacks the apply_auto_patch.py edits. Marker: AUTO_SELFCHECK_REPORT_OK"""
import importlib.util, sys, subprocess, tempfile, os
from importlib.machinery import SourceFileLoader
from pathlib import Path
BIN = Path(__file__).resolve().parent
path = Path(sys.argv[sys.argv.index("--auto") + 1]) if "--auto" in sys.argv else BIN / "ollama-dispatch-auto"
F = []
def check(n, ok, x=""):
    print(("ok   " if ok else "FAIL ") + n + ("" if ok else "  " + str(x)[:300]))
    if not ok: F.append(n)
sys.path.insert(0, str(BIN))
old = sys.argv; sys.argv = [str(path)]
ld = SourceFileLoader("auto_t", str(path))
m = importlib.util.module_from_spec(importlib.util.spec_from_loader("auto_t", ld))
try: ld.exec_module(m)
finally: sys.argv = old
hc = m.render_harness_check("python", [])
check("rendered self-check names WHERE it failed (selfcheck_failure_report wired)", "selfcheck_failure_report" in hc)
check("rendered self-check keeps the legacy 'does not make verify.sh print' text", "does not make verify.sh print" in hc)
check("rendered self-check lints the Confirmed-defect section", "defect_section_findings" in hc)
compile(hc, "hc", "exec")
# run the rendered self-check end to end on a fixture NameError
td = Path(tempfile.mkdtemp(prefix="asr-")); wt = td / "wt"; wt.mkdir()
def sh(*c): return subprocess.run(c, cwd=wt, capture_output=True, text=True)
sh("git","init","-q","-b","main"); sh("git","config","user.email","t@t"); sh("git","config","user.name","t")
(wt/"m.py").write_text("def f(x):\n    return x\n")
(wt/"test_fixture.py").write_text("print('=== behavioural cases ===')\nundefined_name_here\n")
(wt/"verify.sh").write_text("#!/bin/bash\npython3 test_fixture.py || exit 1\necho VERIFY_OK\n"); os.chmod(wt/"verify.sh",0o755)
(wt/"refimpl.py").write_text("def f(x):\n    return x\n")
(wt/"TASK.md").write_text("# T\n## Confirmed defect (observed, not suspected)\n\nx\n\n## Must contain\n- `incoming_name`\n\n## Required change\nOnly edit `m.py`.\n")
(wt/".dispatch-harness.json").write_text('{"target":"m.py"}')
sh("git","add","-A"); sh("git","commit","-qm","b")
(wt/"auto-harness-check.py").write_text(hc)
r = sh(sys.executable, "auto-harness-check.py")
out = r.stdout + r.stderr
check("end to end: a fixture NameError is reported as a FIXTURE error with WHERE IT FAILED",
      "WHERE IT FAILED" in out and "fixture" in out.lower() and "undefined_name_here" in out or "NameError" in out, out[-500:])
a = type("A", (), {"lang": "python"})()
check("author prompt wiring: heavy_target_section + NO_INVENTED_DEFECT_RULE present",
      hasattr(m, "heavy_target_section") and hasattr(m, "NO_INVENTED_DEFECT_RULE") and "Confirmed defect" in m.NO_INVENTED_DEFECT_RULE)
src = path.read_text()
check("author prompt actually embeds both", "{heavy_target_section(a, target)}" in src and "{NO_INVENTED_DEFECT_RULE}" in src)
print(f"\n--- {len(F)} failed ---")
if F: sys.exit(1)
print("AUTO_SELFCHECK_REPORT_OK")
