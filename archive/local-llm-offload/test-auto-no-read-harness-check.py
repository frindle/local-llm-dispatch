#!/usr/bin/env python3
"""Both the first-author prompt and the CONTINUE prompt must tell the model not to read
auto-harness-check.py (rt-egift-link-s1-s4 c1, 2026-10-06: 24 iterations burned reading
its 25KB source). AUTO_SRC overrides the file under test (--revert-check)."""
import importlib.util, os, sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
AUTO = Path(os.environ.get("AUTO_SRC") or Path(__file__).resolve().parent / "ollama-dispatch-auto")
ld = SourceFileLoader("auto_nr", str(AUTO))
m = importlib.util.module_from_spec(importlib.util.spec_from_loader("auto_nr", ld))
sys.argv = [str(AUTO)]; ld.exec_module(m)
a = SimpleNamespace(intent="do x", lang="ts", verify_shape="", edit_file=None, interface=None,
                    slice_must=None, require=[], new_project=False, verify_cmd=None)
fails = []
def chk(n, ok):
    print(("ok  " if ok else "FAIL") + " - " + n)
    if not ok: fails.append(n)
for name, fn in (("author_prompt", lambda: m.author_prompt(a, "lib/x.ts")),
                 ("author_continue_prompt", lambda: m.author_continue_prompt(a, "lib/x.ts", "not ok 1"))):
    try: txt = fn()
    except Exception as e:
        print("FAIL - %s raised %r" % (name, e)); fails.append(name); continue
    chk(name + " says do NOT read auto-harness-check.py", "Do NOT read `auto-harness-check.py`" in txt)
sys.exit(1 if fails else 0)
