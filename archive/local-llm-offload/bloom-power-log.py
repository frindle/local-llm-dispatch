#!/usr/bin/env python3
"""Darkbloom power + state sampler (stdlib only; user LaunchAgent, no sudo, no listener).

Every minute appends ONE row to ~/.darkbloom/power/power-log-YYYY-MM.csv (new file each month):
  system watts from `macmon pipe` (SMC whole-system sensor, no sudo; RAW, calibration is applied only
  by bloom-power-report.py), interval Wh, and the Darkbloom state needed to tie energy to "running":
    dbk_up                provider process alive AND daemon-state.json fresh
    dbk_state             off | idle (online, waiting) | serving (see docs/bloom-power.md)
    inference_active_frac share of 5 s polls where daemon-state.json said inference_active=true
    requests_delta/tokens_delta   change of stats.requests_served / tokens_generated
    warm_models, load_transition_active, gpu_pct (macmon gpu_active_ratio)
    other_work/other_procs  Penn's own LLM work (ollama-worker, ollama runner, mlx, local-endpoint
                          clients of 127.0.0.1:8000, any non-Darkbloom process >150% CPU). Those
                          intervals are excluded from the incremental method.
READ-ONLY toward Darkbloom: reads daemon-state.json, lsof/ps; never calls the darkbloom CLI or writes
provider.toml. macmon missing/crashing never exits the loop: rows say power_ok=0 note='power unavailable'.

Usage: bloom-power-log.py            run forever (launchd KeepAlive)
       bloom-power-log.py --once [--seconds N]   sample N s (default 20), write one row, print it"""
import argparse
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bloom_power_lib as L  # noqa: E402

POLL_S = 5.0
ROW_S = 60.0
MAX_DT = 5.0           # a gap between macmon samples longer than this is sleep/stall, not measured energy


def find_macmon():
    p = os.environ.get("BLOOM_MACMON")
    if p:
        return p if os.access(p, os.X_OK) else None
    return shutil.which("macmon") or ("/opt/homebrew/bin/macmon" if os.access("/opt/homebrew/bin/macmon", os.X_OK) else None)


class Window:
    """Accumulates one row's worth of observations. Pure: no IO."""

    def __init__(self, start_ts):
        self.start = start_ts
        self.samples = []            # (ts, watts, gpu)
        self.last_sample_ts = None
        self.energy_ws = 0.0         # watt-seconds, trapezoid-free: watts * capped dt
        self.covered_s = 0.0
        self.polls = 0
        self.active_polls = 0
        self.up_polls = 0
        self.other = set()
        self.warm = None
        self.load_transition = False
        self.counters_first = None
        self.counters_last = None
        self.notes = set()

    def add_power(self, ts, watts, gpu):
        dt = 1.0 if self.last_sample_ts is None else min(max(ts - self.last_sample_ts, 0.0), MAX_DT)
        if self.last_sample_ts is not None and ts - self.last_sample_ts > MAX_DT * 2:
            self.notes.add("power gap (sleep/stall)")
        self.last_sample_ts = ts
        self.samples.append((ts, watts, gpu))
        self.energy_ws += watts * dt
        self.covered_s += dt

    def add_poll(self, up, ds, other_names):
        self.polls += 1
        if up:
            self.up_polls += 1
            if ds.get("ok"):
                if ds.get("inference_active"):
                    self.active_polls += 1
                self.warm = ds.get("warm_models")
                self.load_transition = self.load_transition or ds.get("load_transition_active", False)
                c = (ds.get("requests"), ds.get("tokens"))
                self.counters_first = self.counters_first or c
                self.counters_last = c
            else:
                self.notes.add("daemon-state stale/missing")
        self.other.update(other_names)

    def row(self, end_ts, prev_counters):
        n = len(self.samples)
        up = self.up_polls * 2 >= max(self.polls, 1) and self.up_polls > 0
        frac = (self.active_polls / self.up_polls) if self.up_polls else 0.0
        c_last = self.counters_last
        rd = td = 0
        if c_last and prev_counters:
            rd = L.counter_delta(prev_counters[0], c_last[0])
            td = L.counter_delta(prev_counters[1], c_last[1])
        elif c_last and self.counters_first:       # no previous window yet: delta within this window
            rd = L.counter_delta(self.counters_first[0], c_last[0])
            td = L.counter_delta(self.counters_first[1], c_last[1])
        notes = set(self.notes)
        if n:
            ws = [s[1] for s in self.samples]
            gs = [s[2] for s in self.samples if s[2] is not None]
            r_avg, r_max = sum(ws) / n, max(ws)
            gpu = round(100 * sum(gs) / len(gs), 1) if gs else ""
            wh = self.energy_ws / 3600.0
            power_ok = 1
        else:
            r_avg = r_max = wh = gpu = ""
            power_ok = 0
            notes.add("power unavailable")
        other = sorted(self.other)
        return {
            "ts_utc": L.utc_iso(end_ts),
            "interval_s": round(self.covered_s, 1) if power_ok else round(end_ts - self.start, 1),
            "samples": n,
            "power_ok": power_ok,
            "avg_w": round(r_avg, 2) if power_ok else "",
            "max_w": round(r_max, 2) if power_ok else "",
            "wh": round(wh, 4) if power_ok else "",
            "dbk_up": int(up),
            "dbk_state": L.classify_state(up, frac, rd, td),
            "inference_active_frac": round(frac, 3),
            "requests_delta": rd,
            "tokens_delta": td,
            "warm_models": ";".join(self.warm or []),
            "load_transition_active": int(self.load_transition),
            "gpu_pct": gpu,
            "other_work": int(bool(other)),
            "other_procs": ";".join(other),
            "note": "; ".join(sorted(notes)),
        }


# ---------------------------------------------------------------- environment probes (all read-only)
def _run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def dbk_process_up(ds_pid):
    """Provider process alive: pid from daemon-state.json (or provider.pid) and the pid exists."""
    pids = []
    if ds_pid:
        pids.append(int(ds_pid))
    try:
        pids.append(int((L.HOME_DIR / "provider.pid").read_text().strip()))
    except (OSError, ValueError):
        pass
    for pid in pids:
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except (OSError, ProcessLookupError):
            continue
    return False


def probe_other_work():
    rows = []
    for ln in _run(["ps", "-axo", "pid=,pcpu=,command="]).splitlines():
        parts = ln.strip().split(None, 2)
        if len(parts) == 3:
            try:
                rows.append((int(parts[0]), float(parts[1]), parts[2]))
            except ValueError:
                pass
    mine = os.getpid()
    rows = [r for r in rows if r[0] != mine]
    clients = L.parse_lsof_clients(_run(["lsof", "-nP", f"-iTCP:{L.DBK_LOCAL_PORT}", "-sTCP:ESTABLISHED"]))
    return L.detect_other_work(rows, clients)


class MacmonReader:
    """Keeps `macmon pipe` running in a background thread; restarts it with backoff; never raises."""

    def __init__(self, binary):
        self.binary = binary
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.proc = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        if self.binary:
            self.thread.start()

    def _run(self):
        backoff = 5
        while not self.stop.is_set():
            try:
                self.proc = subprocess.Popen([self.binary, "pipe", "-i", "1000"], stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, text=True)
                for line in self.proc.stdout:
                    parsed = L.parse_macmon_line(line)
                    if parsed:
                        self.q.put((time.time(), parsed[0], parsed[1]))
                        backoff = 5
                self.proc.wait()
            except OSError:
                pass
            if self.stop.wait(backoff):
                break
            backoff = min(backoff * 2, 300)

    def drain(self):
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                return out

    def close(self):
        self.stop.set()
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        except OSError:
            pass


def sample_once(win, reader):
    now = time.time()
    for ts, w, g in reader.drain():
        win.add_power(ts, w, g)
    ds = L.read_daemon_state(L.HOME_DIR / "daemon-state.json", now)
    up = dbk_process_up(ds.get("pid"))
    try:
        others = probe_other_work()
    except Exception as e:  # noqa: BLE001 - a probe failure must never kill the sampler
        others = []
        win.notes.add(f"other-work probe failed: {type(e).__name__}")
    win.add_poll(up, ds, others)


def write_row(row):
    path = L.power_log_path(L.HOME_DIR, row["ts_utc"])
    L.append_csv_row(path, L.POWER_COLUMNS, row)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--no-write", action="store_true", help="with --once: print the row, do not append")
    a = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    binary = find_macmon()
    if not binary:
        print("bloom-power-log: macmon not found (brew install macmon); logging 'power unavailable' rows",
              file=sys.stderr, flush=True)
    reader = MacmonReader(binary)
    reader.start()
    prev = None
    try:
        if a.once:
            win = Window(time.time())
            end = time.time() + a.seconds
            while time.time() < end:
                sample_once(win, reader)
                time.sleep(min(POLL_S, max(0.1, end - time.time())))
            sample_once(win, reader)
            row = win.row(time.time(), prev)
            if not a.no_write:
                write_row(row)
            print(row)
            return 0
        win = Window(time.time())
        next_flush = (int(time.time() // ROW_S) + 1) * ROW_S
        while True:
            time.sleep(POLL_S)
            sample_once(win, reader)
            now = time.time()
            if now >= next_flush:
                row = win.row(now, prev)
                try:
                    write_row(row)
                except OSError as e:
                    print(f"bloom-power-log: write failed: {e}", file=sys.stderr, flush=True)
                prev = win.counters_last or prev
                win = Window(now)
                next_flush = (int(now // ROW_S) + 1) * ROW_S
    finally:
        reader.close()


if __name__ == "__main__":
    sys.exit(main())
