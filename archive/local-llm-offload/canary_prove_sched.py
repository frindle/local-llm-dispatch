"""canary_prove_sched.py -- the queue daemon's nightly full canary --prove (Penn 2026-10-14).

A daemon-internal MAINTENANCE task, NOT a queued job: it never enters the job table, takes no lane,
no bundle, no GPU. ollama-queue.py calls tick() once per poll (outside the state lock); tick() never
blocks (the prove child is polled, never waited on) and never raises.

Contract with canary-staleness-check.py (unchanged): the age of the last full prove is
coverage.json["full_prove_ts"], written by pipeline-canary.py --prove itself. We only read it.

Rules
  due      age >= DUE_AGE_S (23 h)  AND  (local hour in [WINDOW_START, WINDOW_END)  OR  age >= FORCE_AGE_S (25 h))
  defer    load average > 0.75*ncpu (unless age >= HARD_AGE_S, 25.5 h: the 26 h alarm is near, run anyway)
  skip     QUEUE_CANARY_PROVE=0 | a prove already running (our child, an adopted child after a daemon
           restart, or any `pipeline-canary.py --prove` process) | last attempt < RETRY_S ago
  run      nice -n 15 python3 pipeline-canary.py --prove --jobs 3, own session, output appended to
           ~/bin/ollama-queue-logs/canary-prove-nightly.log
  timeout  TIMEOUT_S (3 h): SIGTERM the process group, SIGKILL after 20 s, alert
  failure  non-zero exit, timeout, or exit 0 with full_prove_ts not advanced -> notify-penn.py (deduped)
State: ~/.ollama-dispatch/canary/prove-sched.json (pid, started, last_attempt, last_result).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
STATE_DIR = HOME / ".ollama-dispatch" / "canary"
STATE_PATH = STATE_DIR / "prove-sched.json"
COVERAGE_PATH = STATE_DIR / "coverage.json"
LOG_PATH = HOME / "bin" / "ollama-queue-logs" / "canary-prove-nightly.log"
BIN = Path(__file__).resolve().parent

DUE_AGE_S = 23 * 3600
FORCE_AGE_S = 25 * 3600
HARD_AGE_S = 25.5 * 3600
TIMEOUT_S = 3 * 3600
RETRY_S = 3600
CHECK_EVERY_S = 120
WINDOW_START, WINDOW_END = 2, 7
LOAD_FRAC = 0.75


class Prove:
    """Real side effects; the seam test replaces this whole object."""

    def full_prove_ts(self):
        try:
            return json.loads(COVERAGE_PATH.read_text()).get("full_prove_ts")
        except Exception:
            return None

    def load(self):
        try:
            return os.getloadavg()[0]
        except OSError:
            return 0.0

    def ncpu(self):
        return os.cpu_count() or 8

    def hour(self, now):
        return time.localtime(now).tm_hour

    def external_running(self):
        """Any `pipeline-canary.py --prove` process (a hand run, an adopted child)."""
        try:
            out = subprocess.run(["ps", "-axo", "args="], capture_output=True, text=True, timeout=10).stdout
        except Exception:
            return True          # cannot tell -> do not stack a second run
        return any("pipeline-canary.py" in l and "--prove" in l and "ps -axo" not in l for l in out.splitlines())

    def spawn(self):
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        lf = open(LOG_PATH, "ab")
        lf.write(f"\n=== queue-scheduled full prove {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode())
        lf.flush()
        env = dict(os.environ, CANARY_NICE="0")
        p = subprocess.Popen(["nice", "-n", "15", sys.executable, str(BIN / "pipeline-canary.py"),
                              "--prove", "--jobs", "3"],
                             stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True, env=env, cwd=str(BIN))
        lf.close()
        return p

    def kill(self, p):
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except OSError:
            pass

    def hardkill(self, p):
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass

    def notify(self, title, msg, key):
        subprocess.Popen([sys.executable, str(BIN / "notify-penn.py"), title, msg[:900],
                          "--dedupe-key", key, "--dedupe-s", "21600"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def pid_alive(self, pid):
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, TypeError, ValueError):
            return False


class Scheduler:
    def __init__(self, io=None, state_path=None, env=None):
        self.io = io or Prove()
        self.state_path = Path(state_path or STATE_PATH)
        self.env = os.environ if env is None else env
        self.proc = None          # our live child
        self.started = None
        self.adopted_pid = None   # a prove from before a daemon restart
        self.last_check = 0.0
        self.term_at = None
        self.base_ts = None       # full_prove_ts when the run started
        self._state = self._load()
        if self._state.get("pid") and self.io.pid_alive(self._state["pid"]):
            self.adopted_pid = self._state["pid"]
            self.started = self._state.get("started")
            self.base_ts = self._state.get("base_ts")

    # -- state ---------------------------------------------------------------------------------
    def _load(self):
        try:
            return json.loads(self.state_path.read_text())
        except Exception:
            return {}

    def _save(self, **kw):
        self._state.update(kw)
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state))
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def enabled(self):
        return self.env.get("QUEUE_CANARY_PROVE", "1") != "0"

    # -- the decision (pure given io) -------------------------------------------------------------
    def decide(self, now):
        if not self.enabled():
            return "disabled"
        if self.proc is not None or self.adopted_pid is not None:
            return "running"
        if self.io.external_running():
            return "external-running"
        ts = self.io.full_prove_ts()
        age = float("inf") if ts is None else now - float(ts)
        if age < DUE_AGE_S:
            return "fresh"
        if now - float(self._state.get("last_attempt") or 0) < RETRY_S:
            return "retry-wait"
        h = self.io.hour(now)
        if not (WINDOW_START <= h < WINDOW_END) and age < FORCE_AGE_S:
            return "outside-window"
        if self.io.load() > LOAD_FRAC * self.io.ncpu() and age < HARD_AGE_S:
            return "load-deferred"
        return "start"

    def _finish(self, result, detail, now):
        self.proc = None
        self.adopted_pid = None
        self.term_at = None
        self._save(pid=None, last_result=result, last_detail=detail, finished=now)
        if result != "ok":
            self.io.notify("Queue canary --prove FAILED",
                           f"nightly full --prove {result}: {detail}. Log: {LOG_PATH}. "
                           f"Run by hand: python3 ~/bin/pipeline-canary.py --prove", "canary-prove-queue")

    def _poll_running(self, now):
        if self.proc is not None:
            rc = self.proc.poll()
            if rc is None:
                if self.term_at is not None:
                    if now - self.term_at > 20:
                        self.io.hardkill(self.proc)
                elif now - self.started > TIMEOUT_S:
                    self.term_at = now
                    self.io.kill(self.proc)
                return
            if self.term_at is not None:
                return self._finish("timeout", f"killed after {TIMEOUT_S // 60} min", now)
            if rc != 0:
                return self._finish("failed", f"exit {rc}", now)
        else:   # adopted
            if self.io.pid_alive(self.adopted_pid):
                if now - float(self.started or now) > TIMEOUT_S:
                    self.io.kill(type("P", (), {"pid": self.adopted_pid})())
                    return self._finish("timeout", "adopted run killed", now)
                return
        ts = self.io.full_prove_ts()
        if self.base_ts is not None and ts is not None and float(ts) <= float(self.base_ts) \
                or (self.base_ts is None and ts is None):
            return self._finish("failed", "exit 0 but full_prove_ts did not advance", now)
        self._finish("ok", "full prove recorded", now)

    def tick(self, now=None):
        """Once per daemon poll. Never raises, never blocks. Returns the decision string (tests)."""
        now = time.time() if now is None else now
        try:
            if self.proc is not None or self.adopted_pid is not None:
                self._poll_running(now)
                return "running" if (self.proc or self.adopted_pid) else "finished"
            if now - self.last_check < CHECK_EVERY_S:
                return "throttled"
            self.last_check = now
            d = self.decide(now)
            if d == "start":
                self.base_ts = self.io.full_prove_ts()
                self.proc = self.io.spawn()
                self.started = now
                self._save(pid=self.proc.pid, started=now, last_attempt=now, base_ts=self.base_ts)
            return d
        except Exception as e:      # noqa: BLE001 -- maintenance must never take the daemon down
            try:
                print(f"[queue] canary-prove scheduler error (ignored): {e!r}", file=sys.stderr)
            except Exception:
                pass
            return "error"
