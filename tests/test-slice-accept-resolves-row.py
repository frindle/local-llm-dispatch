#!/usr/bin/env python3
"""--accept-slice resolves the slice's own needs_opus queue row (2026-10-02,
Rivian s4 5983078bfe69 stayed needs_opus after the accept until a manual
`ollama-queue.py resolve`). Checks the helper against injected statuses and
that accept_slice calls it. --revert-check drops the call -> RED."""
import importlib.util, inspect, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("SLICE_SRC") or HERE / "ollama-dispatch-slice")
FAILS = []


def check(n, ok):
    print(("ok  " if ok else "FAIL") + ": " + n)
    if not ok:
        FAILS.append(n)


def main():
    ld = SourceFileLoader("sl_ar", str(SRC))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("sl_ar", ld))
    sys.argv = [str(SRC)]
    ld.exec_module(m)
    calls = []
    run = lambda cmd, **kw: (calls.append(cmd), SimpleNamespace(returncode=0))[1]
    st = ({"aaa": "needs_opus", "bbb": "done", "ccc": "needs_opus"}, {})
    got = m._resolve_parked_rows({"job_id": "aaa", "author_job_id": "bbb"}, statuses=st, run=run)
    check("needs_opus coding row resolved", got == ["aaa"] and calls and calls[0][-2:] == ["resolve", "aaa"])
    calls.clear()
    m._resolve_parked_rows({"job_id": "bbb"}, statuses=st, run=run)
    check("a done row is left alone", calls == [])
    check("another slice's needs_opus row is never touched", all("ccc" not in c for c in calls))
    check("accept_slice calls it after DONE", "_resolve_parked_rows(s)" in inspect.getsource(m.accept_slice))
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED")
    return 0 if not FAILS else 1


def revert_check():
    src = SRC.read_text()
    line = "    _resolve_parked_rows(s)\n    return True\n"
    assert src.count(line) == 1
    with tempfile.NamedTemporaryFile("w", suffix="-slice", delete=False, dir=str(HERE)) as f:
        f.write(src.replace(line, "    return True\n"))
    r = subprocess.run([sys.executable, __file__], env={**os.environ, "SLICE_SRC": f.name},
                       capture_output=True, text=True, timeout=300)
    os.unlink(f.name)
    red = r.returncode != 0
    print(("bites" if red else "INERT") + ": drop the call -> suite " + ("RED" if red else "green"))
    print("REVERT-CHECK OK" if red else "REVERT-CHECK FAILED")
    return 0 if red else 1


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
