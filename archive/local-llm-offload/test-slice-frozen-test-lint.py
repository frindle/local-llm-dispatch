#!/usr/bin/env python3
"""Phase 2 item 4c: a slice that needs NEW code paths (must_contain identifiers absent from the
target) but whose only test files in scope are read-only (`reads`, i.e. frozen) and never mention
them is unsatisfiable -- refused before any author job. An editable/new test, a test that already
names the literal, `lint_ack`, or no test in `reads` is fine.   Prints FROZEN_TEST_LINT_OK."""
import importlib.util, os, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
HERE = Path(__file__).resolve().parent
SRC = Path(sys.argv[sys.argv.index("--src") + 1]) if "--src" in sys.argv else HERE / "ollama-dispatch-slice"
FAILS = []
def check(n, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {n}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok: FAILS.append(n)
ld = SourceFileLoader("ods_ft", str(SRC))
m = importlib.util.module_from_spec(importlib.util.spec_from_loader("ods_ft", ld))
sys.argv = [str(SRC)]
ld.exec_module(m)
root = tempfile.mkdtemp(prefix="ft-")
os.makedirs(os.path.join(root, "tests"))
Path(root, "lib.py").write_text("def old_fn():\n    return 1\n")
Path(root, "tests/test_lib.py").write_text("from lib import old_fn\ndef test_old():\n    assert old_fn() == 1\n")
Path(root, "tests/test_new.py").write_text("from lib import new_fn\n")
st = {"target": "lib.py", "label": "x"}
base = {"id": "s1", "intent": "add new_fn", "must_contain": ["new_fn"], "reads": ["tests/test_lib.py"]}
f = m.frozen_test_new_path_reason
r = f(st, base, root)
check("frozen test never names the new literal -> refused", bool(r) and "new_fn" in r and "tests/test_lib.py" in r, True)
check("test file editable (edit_files) -> ok", f(st, dict(base, edit_files=["tests/test_lib.py"]), root), None)
check("frozen test already names the literal -> ok", f(st, dict(base, reads=["tests/test_new.py"]), root), None)
check("literal already in target (not a new path) -> ok", f(st, dict(base, must_contain=["old_fn"]), root), None)
check("no test file in reads -> ok", f(st, dict(base, reads=["README.md"]), root), None)
check("lint_ack overrides", f(st, dict(base, lint_ack=True), root), None)
check("multi-token literal is not a bare identifier -> ok", f(st, dict(base, must_contain=["if (x) return"]), root), None)
check("wired into the satisfiability path of execute", "frozen_test_new_path_reason(" in SRC.read_text().split("_unsat = ", 1)[1][:200], True)
print("\nFROZEN_TEST_LINT_OK" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
