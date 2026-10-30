#!/usr/bin/env python3
"""plan-gen prompt (2026-10-05): the decomposition prompt must (1) inline the target +
intent-named repo files, bounded, never from nested worktree copies or outside the
repo, (2) demand a draft plan.json early, (3) say plainly when last round wrote NO
plan instead of "repair the defective ones", (4) default --max-iters >= 20.

Root case: bfe0699081c4 burned 12/12 iters exploring the repo and stale worktree
copies, never wrote plan.json; r2's REDO told it to repair an empty plan.

Hermetic: pure functions on a temp repo; enqueues nothing.
Usage:  python3 ~/bin/test-plan-gen-prompt.py
Revert: PLANGEN=~/bin/ollama-dispatch-plan.bak-<ts> python3 this.py  -> must FAIL
"""
import os
import sys
import tempfile
import types
from importlib.machinery import SourceFileLoader
import importlib.util
from pathlib import Path

MOD = os.environ.get("PLANGEN", str(Path.home() / "bin" / "ollama-dispatch-plan"))
ld = SourceFileLoader("pg", MOD)
spec = importlib.util.spec_from_loader("pg", ld)
P = importlib.util.module_from_spec(spec)
ld.exec_module(P)

fails = 0


def chk(name, ok):
    global fails
    print(("ok   - " if ok else "FAIL - ") + name)
    fails += 0 if ok else 1


T = Path(tempfile.mkdtemp(prefix="pgp-"))
repo = T / "repo"
(repo / "lib").mkdir(parents=True)
(repo / "app/api/x").mkdir(parents=True)
(repo / "rt-wt-old/lib").mkdir(parents=True)
(repo / "lib/target.ts").write_text("export const TARGET_MARKER = 1;\n")
(repo / "app/api/x/route.ts").write_text("export const ROUTE_MARKER = 2;\n")
(repo / "rt-wt-old/lib/stale.ts").write_text("STALE_MARKER\n")
(T / "outside.ts").write_text("OUTSIDE_MARKER\n")
(repo / "lib/big.ts").write_text("B" * 50000)
intent = ("Wire lib/target.ts into app/api/x/route.ts; see rt-wt-old/lib/stale.ts and "
          "../outside.ts and lib/missing.ts and lib/big.ts.")

rc = getattr(P, "repo_context", None)
chk("repo_context() exists", callable(rc))
ctx = rc(str(repo), "lib/target.ts", intent) if callable(rc) else ""
chk("target file inlined", "TARGET_MARKER" in ctx)
chk("intent-named file inlined", "ROUTE_MARKER" in ctx)
chk("nested worktree copy NOT inlined", "STALE_MARKER" not in ctx)
chk("path escaping repo NOT inlined", "OUTSIDE_MARKER" not in ctx)
chk("context warns off nested worktree copies", "worktree" in ctx.lower())
chk("context bounded by budget", len(ctx) < getattr(P, "CONTEXT_BUDGET", 0) + 3000)
small = rc(str(repo), "lib/target.ts", intent, budget=450) if callable(rc) else ""
chk("tiny budget: over-budget file listed, not inlined",
    "BBBB" not in small and "big.ts" in small and "not inlined" in small)
mid = rc(str(repo), "lib/target.ts", intent, budget=1500) if callable(rc) else ""
chk("inlined content never exceeds the budget", 0 < mid.count("B") <= 1500)
chk("nothing readable -> empty context",
    callable(rc) and rc(str(repo), "nope.ts", "no paths here") == "")

a = types.SimpleNamespace(repo=str(repo), target="lib/target.ts", lang="ts",
                          intent=intent, max_iters=20)
try:
    p1 = P.build_prompt(a, "lbl", context=ctx)
except TypeError:
    p1 = P.build_prompt(a, "lbl")
chk("prompt carries the inlined context", "ROUTE_MARKER" in p1)
chk("prompt demands an early draft plan.json", "draft" in p1.lower() and "3rd tool call" in p1)

defects = [("EMPTY_PLAN", "plan has no slices")]
p_empty = P.build_prompt(a, "lbl", defects, "{}\n")
chk("REDO after EMPTY plan says no plan was written",
    "wrote no plan" in p_empty.lower() and "repair the defective ones" not in p_empty)
good_prev = '{"slices": [{"id": "s1"}]}'
p_real = P.build_prompt(a, "lbl", defects, good_prev)
chk("REDO after a real plan still asks to repair it",
    "repair the defective ones" in p_real and good_prev in p_real)

src = Path(MOD).read_text()
import re
m = re.search(r'"--max-iters", type=int, default=(\d+)', src)
chk("--max-iters default >= 20", bool(m) and int(m.group(1)) >= 20)
chk("do_generate passes repo context into build_prompt",
    "context=ctx" in src and "repo_context(a.repo" in src)

print("\nALL PASS" if not fails else f"\n{fails} FAIL")
sys.exit(1 if fails else 0)
