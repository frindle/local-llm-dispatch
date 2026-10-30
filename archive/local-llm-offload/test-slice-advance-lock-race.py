#!/usr/bin/env python3
"""test-slice-advance-lock-race.py -- two --execute drivers must never both own a plan.

Found by pipeline-canary.py (2026-10-05). Two drivers were started together and
BOTH took the per-plan advance lock. The second one crashed in
ensure_chain_worktree on `git branch` ("reference already exists").

Root cause: acquire_advance_lock() published the lock with O_CREAT|O_EXCL, which
leaves it EMPTY, and only then wrote the payload. A concurrent acquire read "",
which lock_is_stale() treats as stale. It unlinked the winner's lock and took its
own, so both drivers held the lock. _write_lock_owner() had the same window,
because write_text() truncates before it writes.

Deterministic replay: driver A is paused INSIDE its payload write and driver B
acquires during the pause. Exactly one of them may own the lock. A real
multi-process race also runs, as a smoke test.

Usage:  test-slice-advance-lock-race.py [--slice PATH]   (default ~/bin/ollama-dispatch-slice)
The revert test passes the .bak and must FAIL. Exit 0 = all checks pass.
"""
import argparse
import importlib.machinery
import importlib.util
import json
import multiprocessing
import os
import sys
import tempfile
import threading
from pathlib import Path

RESULTS = []


def check(name, ok, note=""):
    RESULTS.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -- {note}"))


def load(path, root):
    ld = importlib.machinery.SourceFileLoader("slice_mod_" + str(os.getpid()), path)
    spec = importlib.util.spec_from_loader(ld.name, ld)
    m = importlib.util.module_from_spec(spec)
    ld.exec_module(m)
    m.STATE_ROOT = root
    return m


def _racer(path, root, label, barrier, q, done):
    m = load(path, root)
    barrier.wait()
    q.put(m.acquire_advance_lock(label) is not None)
    # stay alive until every racer has tried: a winner that exits leaves a lock
    # with a DEAD pid, which a late racer rightly treats as stale and takes over
    try:
        done.wait(30)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slice", default=str(Path.home() / "bin" / "ollama-dispatch-slice"))
    a = ap.parse_args()
    root = tempfile.mkdtemp(prefix="lockrace-")
    m = load(a.slice, root)

    # 1. deterministic: A paused mid-payload-write, B acquires in the window
    real_dump = json.dump
    in_dump, go = threading.Event(), threading.Event()
    got = {}

    def slow_dump(obj, fh, *k, **kw):
        if threading.current_thread().name == "A":
            in_dump.set()
            go.wait(5)
        return real_dump(obj, fh, *k, **kw)

    m.json.dump = slow_dump
    try:
        ta = threading.Thread(name="A", target=lambda: got.__setitem__("A", m.acquire_advance_lock("p1")))
        ta.start()
        in_dump.wait(5)
        got["B"] = m.acquire_advance_lock("p1")
        go.set()
        ta.join(5)
    finally:
        m.json.dump = real_dump
    winners = [k for k in ("A", "B") if got.get(k)]
    check("deterministic: a driver paused mid-acquire and a second driver -> exactly ONE owns the lock",
          len(winners) == 1, f"winners={winners}")
    lk = Path(m.lock_path("p1"))
    try:
        payload = json.loads(lk.read_text())
    except Exception as e:
        payload = repr(e)
    check("the lock on disk carries a parseable payload naming a live pid",
          isinstance(payload, dict) and payload.get("pid") == os.getpid(), f"{payload}")
    m.release_advance_lock("p1")

    # 2. _write_lock_owner never exposes an empty lock to a reader
    p = m.acquire_advance_lock("p2")
    stop = threading.Event()
    seen_bad = []

    def reader():
        while not stop.is_set():
            try:
                t = Path(p).read_text()
            except FileNotFoundError:
                continue
            if not t.strip():
                seen_bad.append(t)

    tr = threading.Thread(target=reader)
    tr.start()
    for _ in range(3000):
        m._write_lock_owner(p, os.getpid())
    stop.set()
    tr.join()
    check("re-pointing the lock (_write_lock_owner) never exposes an empty/partial lock",
          not seen_bad, f"{len(seen_bad)} empty reads")
    m.release_advance_lock("p2")
    leftovers = [x for x in os.listdir(root) if x.endswith(".tmp")]
    check("no temp files left behind", not leftovers, f"{leftovers}")

    # 3. multi-process smoke: 8 drivers released together, 15 rounds
    multi = []
    ctx = multiprocessing.get_context("fork")
    for i in range(15):
        b = ctx.Barrier(8)
        done = ctx.Barrier(8)
        q = ctx.Queue()
        ps = [ctx.Process(target=_racer, args=(a.slice, root, f"r{i}", b, q, done)) for _ in range(8)]
        for x in ps:
            x.start()
        for x in ps:
            x.join(20)
        n = sum(q.get() for _ in ps)
        multi.append(n)
    check("8 drivers started together, 15 rounds: exactly one winner each round",
          all(n == 1 for n in multi), f"winners per round={multi}")
    ok = all(RESULTS)
    print("ALL PASS" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
