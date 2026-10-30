#!/usr/bin/env python3
"""ollama-dispatch-auto must hand the scaffold a --symbol for TS/JS symbol-kind
runs. Regression: every TS slice plan (ev-service-screen-1 s1, 2026-09-27)
ESCALATED at scaffold rc=2 "--symbol is required ... tsx runner" because
do_scaffold never passed one."""
import importlib.machinery, importlib.util, os, sys
from types import SimpleNamespace
here = os.path.dirname(os.path.abspath(__file__))
ld = importlib.machinery.SourceFileLoader("auto", os.path.join(here, "ollama-dispatch-auto"))
m = importlib.util.module_from_spec(importlib.util.spec_from_loader("auto", ld)); ld.exec_module(m)
fails = 0
def chk(name, ok):
    global fails
    print(("  ok   " if ok else "  FAIL ") + name); fails += 0 if ok else 1

seen = []
m.run = lambda cmd, *a, **k: seen.append(cmd) or 0
def scaffold(**kw):
    seen.clear()
    a = SimpleNamespace(label="l", lang="python", intent="", interface=None, symbol=None,
                        new_project=None, repo="/r", dest=None, force=False)
    for k, v in kw.items(): setattr(a, k, v)
    m.do_scaffold(a, "/wt", "lib/x.ts")
    c = seen[0]
    return c[c.index("--symbol") + 1] if "--symbol" in c else None

chk("TS intent 'Export function foo(' -> --symbol foo",
    scaffold(lang="ts", intent="Export function mapServiceRequestStatus(raw: unknown): 'a'") == "mapServiceRequestStatus")
chk("interface wins over intent prose",
    scaffold(lang="typescript", intent="calls function helper(x) inside", interface="export async function fetchIt(o)") == "fetchIt")
chk("explicit --symbol wins", scaffold(lang="ts", intent="Export function a()", symbol="b") == "b")
chk("export const", scaffold(lang="js", intent="add export const EV_ROUTE = '/x'") == "EV_ROUTE")
chk("python run gets no --symbol", scaffold(lang="python", intent="Export function foo(") is None)
chk("TS with no nameable symbol passes none (scaffold decides/errs)", scaffold(lang="ts", intent="tweak the layout") is None)
print(f"--- {fails} failed ---"); print("TS_SYMBOL_OK" if not fails else ""); sys.exit(1 if fails else 0)
