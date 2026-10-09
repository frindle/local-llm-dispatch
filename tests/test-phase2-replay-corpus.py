#!/usr/bin/env python3
"""Phase 2 REPLAY CORPUS: the real parked cases (2026-10-06/07), rebuilt as temp fixtures,
must now be caught/handled by the new gates. Each block is one escalation.

  rt-egift-link-s1-s4-api-route : a RAW scaffold (TODO placeholders, SCAFFOLD_INCOMPLETE) is
                                  NOT harness-complete -> never enqueued.
  5b6b052149a7 (payoutMismatch) : stub `export {};` target in HEAD and tree == baseline.
  8a4941aa8521 / a580c8793aa7   : must_contain MANUAL_SYNC_INIT with 0 occurrences refused.
  invariant slices              : all literals present -> already-satisfied skip reason.
  retry-storm                   : attempts cap -> one bounded re-spec, then park.
  conditional scaffold guard    : a FINISHED fixture that keeps `if (!CASES_AUTHORED)` is fine.
Run: python3 test-phase2-replay-corpus.py   (prints ALL PASS)"""
import importlib.util, json, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    ld = SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    old = sys.argv
    sys.argv = [str(path)]
    ld.exec_module(mod)
    sys.argv = old
    return mod


hg = load(HERE / "dispatch_harness_gates.py", "hg_replay")
sl = load(HERE / "ollama-dispatch-slice", "sl_replay")
git = lambda cwd, *a: subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)
root = Path(tempfile.mkdtemp(prefix="replay-"))

# --- rt-egift-link-s1-s4-api-route: raw scaffold ----------------------------------------
repo = root / "repo"
(repo / "app/api/orders/[id]/egift-link").mkdir(parents=True)
git(repo, "init", "-q"); git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
(repo / "app/api/orders/[id]/egift-link/route.ts").write_text("export async function GET() { return null; }\n")
(repo / "package.json").write_text('{"scripts":{"test":"node --test"}}\n')
git(repo, "add", "-A"); git(repo, "commit", "-qm", "i")
wt = root / "scf"
subprocess.run([sys.executable, str(HERE / "ollama-dispatch-scaffold"), "--label", "rt-egift", "--repo", str(repo),
                "--lang", "typescript", "--target", "app/api/orders/[id]/egift-link/route.ts", "--dest", str(wt),
                "--defect", "x", "--property", "y"], capture_output=True, text=True, env=dict(os.environ, HOME=str(root)))
ok, fs = hg.harness_complete(wt)
check("egift-link: raw scaffold rejected before enqueue", ok, False)
check("egift-link: placeholder + no-literal both named",
      any("placeholder" in f or "TODO" in f for f in fs) and any("Must contain" in f for f in fs), True)

# --- 5b6b052149a7: stub baseline -----------------------------------------------------------
for name, txt in (("export {};", "export {};\n"), ("creation stub", "// Stub for lib/payoutMismatch.ts -- implement per TASK.md.\nexport {};\n")):
    check(f"payoutMismatch: {name} is a stub", hg.is_stub_text(txt), True)
check("payoutMismatch: a real impl is not", hg.is_stub_text("export function f() { return 1; }\n"), False)
# (the preflight baseline-clean half is test-preflight-harness-gates.py case B, real CLI)

# --- 8a4941aa8521 / a580c8793aa7: 0-occurrence literal ----------------------------------------
rt = root / "rt"
(rt / "lib").mkdir(parents=True)
(rt / "lib/bfmrSync.ts").write_text("export function syncResult() {}\nconst MANUAL_SYNC = 1;\n")
st = {"target": "lib/bfmrSync.ts"}
s = {"id": "s2", "intent": "surface push failures", "must_contain": ["MANUAL_SYNC_INIT"]}
check("bfmr: 0-occurrence literal MANUAL_SYNC_INIT rejected pre-author",
      "MANUAL_SYNC_INIT" in (sl.spec_satisfiability_reason(st, s, str(rt)) or ""), True)

# --- invariant slices ------------------------------------------------------------------------------
inv = {"id": "s9", "kind": "invariant", "intent": "never throws", "must_contain": ["syncResult", "MANUAL_SYNC"]}
check("invariant slice with all literals present is auto-skipped (reason -> SKIPPED)",
      "already satisfied" in (sl.vacuous_literals(st, inv, str(rt)) or ""), True)

# --- retry storm ----------------------------------------------------------------------------------------
s5 = {"id": "s4", "intent": "x", "author_attempts": sl.MAX_AUTHOR_ATTEMPTS}
check("retry-storm: cap -> re-spec once", sl.respec_on_cap(s5, "changing sig 1", 1, 5), True)
s5["author_attempts"] = sl.MAX_AUTHOR_ATTEMPTS
check("retry-storm: cap again -> park", sl.respec_on_cap(s5, "changing sig 2", 1, 5), False)

# --- conditional guard retained in a finished fixture is NOT a placeholder -------------------------------
fin = "const CASES_AUTHORED = true;\nchk('a', 1);\nif (!CASES_AUTHORED) {\n  console.log('SCAFFOLD_INCOMPLETE: x');\n}\n"
check("finished fixture keeping its conditional guard is complete", hg.placeholder_findings("", "", {"verify_impl.mts": fin}), [])
raw = fin.replace("= true", "= false")
check("same fixture with the flag still off is a placeholder", len(hg.placeholder_findings("", "", {"verify_impl.mts": raw})), 1)
bare = "console.log('SCAFFOLD_INCOMPLETE: author me');\n"
check("an unconditional marker is a placeholder", len(hg.placeholder_findings("", "", {"verify_impl.mts": bare})), 1)

print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
