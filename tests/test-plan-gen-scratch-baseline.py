#!/usr/bin/env python3
"""plan-gen scratch baseline is sealed before each round (2026-10-03).

146bd205e5bb / 5f865e95009e were parked needs_opus as "untrusted baseline: launch
baseline was dirty (2 path(s))" -- the two paths were PLAN-TASK.md and plan.json,
both written by ollama-dispatch-plan itself AFTER the scratch repo's only baseline
commit. Asserts, with the queue's REAL measure_baseline():

  * after dispatch_round() writes PLAN-TASK.md (and a previous round left
    plan.json / a rewritten verify.sh), the launch baseline is CLEAN (dirty == 0)
  * round 2 is clean too (the seal repeats per round)
  * REVERT-CHECK (the guard still bites): a genuinely stray path in the scratch
    dir after the seal -> dirty == 1, i.e. the queue's dirty-baseline check is
    not weakened, only the pipeline's own files are sealed

Run: python3 ~/bin/test-plan-gen-scratch-baseline.py   (no queue, no GPU)
"""
import os
import subprocess
import sys
import tempfile
import types
from importlib.machinery import SourceFileLoader
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLAN = Path(os.environ.get("PLAN_SRC") or HERE / "ollama-dispatch-plan")
QUEUE = HERE / "ollama-queue.py"
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def main():
    plan = load(PLAN, "plan_gen_seal")
    q = load(QUEUE, "queue_seal")
    with tempfile.TemporaryDirectory() as td:
        wt = Path(td) / "scratch"
        wt.mkdir()
        # exactly what do_generate does: verify.sh, git init + one baseline commit,
        # THEN plan.json -- the order that produced the dirty launch baseline.
        (wt / "verify.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        subprocess.run(["git", "init", "-q", str(wt)], check=True)
        subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(wt), "commit", "-q", "-m", "plan-gen scratch baseline"],
                       check=True, env=env)
        (wt / "plan.json").write_text("{}\n")
        a = types.SimpleNamespace(drafter_cmd="true", timeout=30)

        ok, why = plan.dispatch_round(wt, "PROMPT r1", "plan-gen-x-r1", a)
        check("round 1 dispatched (drafter stands in for the model)", ok, True)
        check("round 1 launch baseline is CLEAN under the queue's measure_baseline",
              (q.measure_baseline(wt) or {}).get("dirty"), 0)

        # the "model" rewrites plan.json; the pipeline rewrites verify.sh; round 2
        (wt / "plan.json").write_text('{"slices": []}\n')
        (wt / "verify.sh").write_text("#!/usr/bin/env bash\nexit 1\n")
        ok, why = plan.dispatch_round(wt, "PROMPT r2 with defects", "plan-gen-x-r2", a)
        check("round 2 launch baseline is CLEAN (seal repeats per round)",
              (q.measure_baseline(wt) or {}).get("dirty"), 0)

        # REVERT-CHECK: a genuinely stray file is still dirty -> the gate still bites
        (wt / "stray-notes.txt").write_text("not the pipeline's\n")
        check("a genuinely stray path after the seal still reads dirty (guard intact)",
              (q.measure_baseline(wt) or {}).get("dirty"), 1)
    print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAIL'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
