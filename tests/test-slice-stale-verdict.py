#!/usr/bin/env python3
"""Behavioural test: ollama-dispatch-slice.verdict_stale_reasons (2026-10-06).
A gate record stamped (gate_identity.py) with the product-file hashes it judged
must be reported STALE once a judged file changes in the slice worktree; an
unstamped legacy record, an unchanged tree, and a vanished worktree are not
'stale' (legacy behaviour: the live verify re-run decides). Also asserts the
land route actually calls it before landing.

Usage: python3 test-slice-stale-verdict.py [path/to/ollama-dispatch-slice]
(pass the .bak to prove the test bites)."""
import hashlib, importlib.machinery, importlib.util, sys, tempfile
from pathlib import Path
target = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("ollama-dispatch-slice"))
ld = importlib.machinery.SourceFileLoader("slice_under_test", str(target))
sp = importlib.util.spec_from_loader("slice_under_test", ld)
m = importlib.util.module_from_spec(sp)
ld.exec_module(m)
ok = True
def check(name, fn, want):
    global ok
    try: got = fn()
    except Exception as e: got = f"<raised {type(e).__name__}: {e}>"
    good = got == want; ok &= good
    print(("PASS " if good else "FAIL ") + name + ("" if good else f": got {got!r} want {want!r}"))
td = Path(tempfile.mkdtemp(prefix="slice-stale-"))
(td / "a.py").write_text("judged\n")
rec = {"verdict": "pass", "judged": {"files": {"a.py": hashlib.sha256(b"judged\n").hexdigest()},
                                      "toplevel": str(td)}}
f = lambda r, w: [bool(x) for x in m.verdict_stale_reasons(r, w)][:1]
check("unchanged judged file -> not stale", lambda: f(rec, td), [])
check("unstamped legacy record -> not stale", lambda: f({"verdict": "pass"}, td), [])
(td / "a.py").write_text("edited after the gate\n")
check("judged file changed -> STALE", lambda: f(rec, td), [True])
check("vanished worktree -> not 'stale'", lambda: f(dict(rec, judged=dict(rec["judged"], toplevel=str(td / "x"))), td / "x"), [])
src = target.read_text()
i_land = src.find("ok, msg = _land_green_slice(st, sid, s, cwt, require_relevance=True)\n                if not ok:\n                    s[\"status\"] = FAILED")
i_chk = src.find("_stale = verdict_stale_reasons(rec, s.get(\"worktree\"))")
check("land route checks staleness before landing", lambda: 0 <= i_chk < i_land, True)
print("ALL PASS" if ok else "SOME FAILED"); sys.exit(0 if ok else 1)
