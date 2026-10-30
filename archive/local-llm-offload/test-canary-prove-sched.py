#!/usr/bin/env python3
"""test-canary-prove-sched.py -- canary_prove_sched.py seams (queue-run nightly full --prove).
Fake clock, fake prove runner. Prints PROVE_SCHED_OK.   python3 test-canary-prove-sched.py [--bin DIR]"""
from __future__ import annotations
import importlib.util, os, sys, tempfile
from pathlib import Path

BIN = Path(__file__).resolve().parent
if "--bin" in sys.argv:
    BIN = Path(sys.argv[sys.argv.index("--bin") + 1]).resolve()
spec = importlib.util.spec_from_file_location("cps_under_test", str(BIN / "canary_prove_sched.py"))
M = importlib.util.module_from_spec(spec); sys.modules["cps_under_test"] = M; spec.loader.exec_module(M)

FAILS = []
H = 3600.0
# a local-time 03:00 so the off-peak window is open; NIGHT/DAY are real epochs with a known local hour
import time as _t
def at(hour, day=20):
    return _t.mktime((2026, 10, day, hour, 0, 0, 0, 0, -1))
NIGHT, DAY = at(3), at(14)


def check(name, ok, note=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  -- {note}"))
    if not ok:
        FAILS.append(name)


class P:
    def __init__(self): self.pid = 4242; self.rc = None
    def poll(self): return self.rc


class IO:
    def __init__(self, ts, load=0.5):
        self.ts, self.ld, self.ext, self.spawned, self.notes, self.kills, self.hard = ts, load, False, [], [], 0, 0
        self.proc = None
    def full_prove_ts(self): return self.ts
    def load(self): return self.ld
    def ncpu(self): return 8
    def hour(self, now): return _t.localtime(now).tm_hour
    def external_running(self): return self.ext
    def spawn(self): self.proc = P(); self.spawned.append(1); return self.proc
    def kill(self, p): self.kills += 1
    def hardkill(self, p): self.hard += 1
    def notify(self, t, m, k): self.notes.append((t, m, k))
    def pid_alive(self, pid): return False


tmp = Path(tempfile.mkdtemp())
def mk(io, env=None, n=[0]):
    n[0] += 1
    return M.Scheduler(io=io, state_path=tmp / f"s{n[0]}.json", env=env if env is not None else {})

# 1 runs when stale (in window)
io = IO(NIGHT - 24 * H); s = mk(io)
check("stale + in window => starts", s.tick(NIGHT) == "start" and len(io.spawned) == 1)
# 2 skips when fresh
io = IO(NIGHT - 10 * H); s = mk(io)
check("fresh (10 h) => no run", s.tick(NIGHT) == "fresh" and not io.spawned)
check("22.9 h => still fresh", mk(IO(NIGHT - 22.9 * H)).tick(NIGHT) == "fresh")
# 3 never proven => runs
io = IO(None); s = mk(io)
check("never proven => starts", s.tick(NIGHT) == "start")
# 4 single flight: our own child, an external prove
io = IO(NIGHT - 24 * H); s = mk(io); s.tick(NIGHT)
check("second tick while running => no second spawn", s.tick(NIGHT + 300) == "running" and len(io.spawned) == 1)
io = IO(NIGHT - 24 * H); io.ext = True; s = mk(io)
check("an external prove running => skip", s.tick(NIGHT) == "external-running" and not io.spawned)
# 5 kill switch
io = IO(NIGHT - 30 * H); s = mk(io, {"QUEUE_CANARY_PROVE": "0"})
check("QUEUE_CANARY_PROVE=0 => never runs", s.tick(NIGHT) == "disabled" and not io.spawned)
# 6 window + force + load
io = IO(DAY - 24 * H); s = mk(io)
check("24 h old, daytime => waits for the window", s.tick(DAY) == "outside-window" and not io.spawned)
io = IO(DAY - 25.2 * H); s = mk(io)
check("25.2 h old, daytime => runs anyway (daily guarantee)", s.tick(DAY) == "start")
io = IO(NIGHT - 24 * H, load=9.0); s = mk(io)
check("heavy load defers", s.tick(NIGHT) == "load-deferred" and not io.spawned)
io = IO(NIGHT - 25.8 * H, load=9.0); s = mk(io)
check("heavy load never defers past 25.5 h", s.tick(NIGHT) == "start")
# 7 success: ts advances -> ok, no alert, then fresh
io = IO(NIGHT - 24 * H); s = mk(io); s.tick(NIGHT)
io.proc.rc = 0; io.ts = NIGHT + 600
check("exit 0 + ts advanced => ok, no alert", s.tick(NIGHT + 600) == "finished" and not io.notes and s.proc is None)
check("after success => fresh", s.tick(NIGHT + 600 + 300) == "fresh")
# 8 failure alerts; retry backoff
io = IO(NIGHT - 24 * H); s = mk(io); s.tick(NIGHT)
io.proc.rc = 2; io.ts = NIGHT - 24 * H + 50   # ts advanced: only the exit code can raise the alarm
s.tick(NIGHT + 100)
check("non-zero exit => alert via notify", len(io.notes) == 1 and "failed" in io.notes[0][1] and io.notes[0][2] == "canary-prove-queue")
check("failure does not hot-loop (retry wait 1 h)", s.tick(NIGHT + 400) == "retry-wait" and len(io.spawned) == 1)
check("retries after the backoff", s.tick(NIGHT + 2 * H) == "start" and len(io.spawned) == 2)
# 9 exit 0 but nothing recorded
io = IO(NIGHT - 24 * H); s = mk(io); s.tick(NIGHT); io.proc.rc = 0
s.tick(NIGHT + 100)
check("exit 0 without full_prove_ts advancing => alert", len(io.notes) == 1 and "did not advance" in io.notes[0][1])
# 10 timeout: SIGTERM at 3 h, SIGKILL after 20 s, alert, loop never blocked
io = IO(NIGHT - 24 * H); s = mk(io); s.tick(NIGHT)
check("running under the limit => untouched", s.tick(NIGHT + 2.9 * H) == "running" and io.kills == 0)
s.tick(NIGHT + 3.1 * H)
check("past 3 h => SIGTERM", io.kills == 1)
s.tick(NIGHT + 3.1 * H + 30)
check("still alive 30 s later => SIGKILL", io.hard == 1)
io.proc.rc = -9
s.tick(NIGHT + 3.1 * H + 40)
check("timeout => alert", len(io.notes) == 1 and "timeout" in io.notes[0][1] and s.proc is None)
# 11 never raises / never blocks
class Boom(IO):
    def full_prove_ts(self): raise RuntimeError("x")
s = mk(Boom(None))
check("an io error never escapes tick()", s.tick(NIGHT) == "error")
# 12 throttle
io = IO(NIGHT - 10 * H); s = mk(io); s.tick(NIGHT)
check("decision is throttled between ticks", s.tick(NIGHT + 5) == "throttled")
# 13 adopt a prove that outlived a daemon restart (state file pid alive)
class IOA(IO):
    def pid_alive(self, pid): return True
sp = tmp / "adopt.json"; sp.write_text('{"pid": 777, "started": %f, "base_ts": %f}' % (NIGHT, NIGHT - 24 * H))
io = IOA(NIGHT - 24 * H); s = M.Scheduler(io=io, state_path=sp, env={})
check("adopts a live prove from before a restart (no second run)", s.tick(NIGHT + 60) == "running" and not io.spawned)
# 14 the real module loads in the daemon without side effects on import
check("queue daemon wires the scheduler", "_CANARY_PROVE.tick()" in (BIN / "ollama-queue.py").read_text()
      and "QUEUE_CANARY_PROVE" in (BIN / "ollama-queue.py").read_text())

print("PROVE_SCHED_OK" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
