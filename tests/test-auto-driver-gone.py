#!/usr/bin/env python3
"""Regression test for the ORPHANED-AUTO guard (2026-09-23).

ollama-dispatch-slice --execute runs ollama-dispatch-auto as a long blocking
child (author -> preflight -> refine rounds). If that --execute dies (a killed
foreground shell, a timed-out Bash tool call, a crash), AUTO used to carry on
alone: polling its job, then enqueuing further refine rounds against the slice
worktree -- while the slicer's completion hook, which is the ONLY thing still
driving the chain, started a fresh detached --execute on the SAME worktree. Two
drivers, one tree (live: AUTO pid 44602 orphaned by --execute 44599, s9).

driver_gone(a) is the PURE decision: under a slicer, either tell (re-parented to
pid 1, or the plan's advance lock names a dead pid) means "stop". Not under a
slicer -> never.

Red-on-revert:
  - make driver_gone() return None unconditionally -> the two "stops" fail.
  - drop the _under_slicer() guard -> the "plain AUTO is its own driver" case fails.
  - stop wiring it into the refine loop / poll loop -> the source assertions fail.

Run: python3 test-auto-driver-gone.py
"""
import importlib.util
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

AUTO = Path(__file__).resolve().parent / "ollama-dispatch-auto"


def _load():
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("oda_drivergone", str(AUTO))
    spec = importlib.util.spec_from_loader("oda_drivergone", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


failures = []


def ok(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        failures.append(name)


def main():
    m = _load()
    under = SimpleNamespace(slice_plan="alpha", slice_id="s1")
    plain = SimpleNamespace(slice_plan=None, slice_id=None)
    os.environ.pop("OLLAMA_DISPATCH_NO_SPLIT", None)

    ok("a plain AUTO (no slicer) is its own driver -- never 'gone', even at ppid 1",
       m.driver_gone(plain, ppid=1, lock_alive=False) is None)
    ok("under a slicer with a live parent and a live lock owner -> not gone",
       m.driver_gone(under, ppid=4242, lock_alive=True) is None)
    ok("under a slicer, re-parented to pid 1 -> gone",
       bool(m.driver_gone(under, ppid=1, lock_alive=True)))
    ok("under a slicer, the plan lock names a DEAD pid -> gone",
       bool(m.driver_gone(under, ppid=4242, lock_alive=False)))
    ok("a MISSING lock file is not a tell (hand-run AUTO under an old slicer)",
       m.driver_gone(under, ppid=4242, lock_alive=None) is None)

    # _slice_lock_owner_alive against a real lock file in a scratch HOME.
    with tempfile.TemporaryDirectory() as td:
        saved = os.environ.get("HOME")
        os.environ["HOME"] = td
        try:
            d = Path(td) / ".ollama-dispatch" / "slice-runs"
            d.mkdir(parents=True)
            ok("no lock file -> None (unknown), not False",
               m._slice_lock_owner_alive("alpha") is None)
            (d / "alpha.advance.lock").write_text(json.dumps({"pid": os.getpid()}))
            ok("lock naming THIS live pid -> True",
               m._slice_lock_owner_alive("alpha") is True)
            (d / "alpha.advance.lock").write_text(json.dumps({"pid": 999999999}))
            ok("lock naming a dead pid -> False",
               m._slice_lock_owner_alive("alpha") is False)
            (d / "alpha.advance.lock").write_text("garbage")
            ok("an unreadable lock -> None (fail toward 'unknown', never 'gone')",
               m._slice_lock_owner_alive("alpha") is None)
        finally:
            if saved is not None:
                os.environ["HOME"] = saved

    src_auto = inspect.getsource(m.run_auto) if hasattr(m, "run_auto") else ""
    if not src_auto:
        # the refine loop lives in do_auto in some versions
        src_auto = inspect.getsource(m.do_auto) if hasattr(m, "do_auto") else ""
    if hasattr(m, "_preflight_loop"):
        # 2026-10-05: the preflight/refine loop was split out of do_auto so
        # --resume-harness can enter it; the per-round check lives there now.
        src_auto += inspect.getsource(m._preflight_loop)
    ok("the refine loop checks driver_gone before EVERY round",
       "gone = driver_gone(a)" in src_auto and "EXIT_ORPHANED" in src_auto)
    ok("the job poller stops when the driver is gone (does not keep polling)",
       "gone = driver_gone(a)" in inspect.getsource(m.dispatch_model))
    ok("the orphan exit code is distinct from a plain failure (1)",
       getattr(m, "EXIT_ORPHANED", 1) not in (0, 1))

    if failures:
        print(f"\n{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("\nall driver-gone tests passed")


if __name__ == "__main__":
    main()
