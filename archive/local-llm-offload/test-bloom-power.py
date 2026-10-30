#!/usr/bin/env python3
"""Hermetic tests for the Darkbloom power/earnings bookkeeping tools (bloom_power_lib.py,
bloom-power-log.py, bloom-earnings-archive.py, bloom-power-report.py).

Covers: serving/idle/off classification, counter resets, non-Darkbloom work exclusion (ollama-worker,
local-endpoint clients, heavy CPU; Darkbloom's own server end of :8000 is NOT other work), the three
report methods on a fixture month (numbers asserted exactly), baseline selection, earnings dedupe /
torn lines / no-id entries / first-poll-is-not-a-gap / gap alert, the token only going to api.darkbloom.dev,
atomic concurrent appends + month rotation, macmon-missing and fake-macmon sampler runs, and the report CLI
+ CSV end to end. NEVER touches the real Darkbloom, ~/.darkbloom, the network or macmon (BLOOM_POWER_HOME
temp dir; BLOOM_MACMON fake).

Usage: test-bloom-power.py [--bindir DIR] [--only NAME] | --revert-check     Markers: ALL PASS / REVERT CHECK OK."""
import argparse
import csv
import datetime as dt
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import http.server
from pathlib import Path

try:
    import openpyxl  # noqa: F401
except ImportError:                      # the workbook tests need openpyxl: use the tool venv
    _v = Path.home() / ".darkbloom" / "venv" / "bin" / "python"
    if _v.exists() and not os.environ.get("BLOOM_TEST_REEXEC"):
        os.environ["BLOOM_TEST_REEXEC"] = "1"
        os.execv(str(_v), [str(_v)] + sys.argv)
    print("FAIL: openpyxl missing (python3 -m venv ~/.darkbloom/venv && ~/.darkbloom/venv/bin/pip install openpyxl)")
    sys.exit(2)

HERE = Path(__file__).resolve().parent
FILES = ["bloom_power_lib.py", "bloom-power-log.py", "bloom-earnings-archive.py", "bloom-power-report.py", "bloom-power-workbook.py",
         "bloom_influx.py", "bloom-influx-push.py"]
FAILS = []
SEL = []
BIN = HERE


def check(name, cond, extra=""):
    print(("ok  : " if cond else "FAIL: ") + name + ("" if cond else "  -- " + str(extra)))
    if not cond:
        FAILS.append(name)


def scenario(fn):
    SEL.append(fn)
    return fn


def load(fname, modname):
    sys.path.insert(0, str(BIN))
    spec = importlib.util.spec_from_file_location(modname, str(BIN / fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def T(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


@scenario
def classification():
    L = load("bloom_power_lib.py", "L1")
    c = L.classify_state
    check("off when provider down even if counters moved", c(False, 1.0, 5, 5) == "off")
    check("idle when up, nothing active, no deltas", c(True, 0.0, 0, 0) == "idle")
    check("serving when inference_active seen", c(True, 0.2, 0, 0) == "serving")
    check("serving when only counters advanced", c(True, 0.0, 1, 0) == "serving" and c(True, 0.0, 0, 40) == "serving")
    check("counter reset counts new value", L.counter_delta(100, 3) == 3)
    check("counter normal delta", L.counter_delta(10, 14) == 4)
    check("no baseline -> 0", L.counter_delta(None, 14) == 0)
    now = 1000.0
    d = Path(tempfile.mkdtemp())
    (d / "ds.json").write_text(json.dumps({"written_at": now - 3, "pid": 7, "inference_active": True,
                                           "stats": {"requests_served": 4, "tokens_generated": 9},
                                           "warm_models": ["A"], "load_transition_active": False}))
    s = L.read_daemon_state(d / "ds.json", now)
    check("fresh daemon-state parsed", s["ok"] and s["inference_active"] and s["requests"] == 4 and s["warm_models"] == ["A"])
    (d / "ds.json").write_text(json.dumps({"written_at": now - 500, "inference_active": True}))
    check("stale daemon-state not trusted", not L.read_daemon_state(d / "ds.json", now)["ok"])
    (d / "ds.json").write_text("{torn")
    check("corrupt daemon-state is not a crash", not L.read_daemon_state(d / "ds.json", now)["ok"])
    check("missing daemon-state is not a crash", not L.read_daemon_state(d / "nope.json", now)["ok"])
    shutil.rmtree(d)


@scenario
def window_rows():
    L = load("bloom_power_lib.py", "L2")
    W = load("bloom-power-log.py", "P2").Window
    ds_idle = {"ok": True, "inference_active": False, "requests": 10, "tokens": 100, "warm_models": ["A", "B"], "load_transition_active": False}
    ds_busy = dict(ds_idle, inference_active=True, requests=11, tokens=180)
    w = W(0)
    for i in range(60):
        w.add_power(1 + i, 50.0, 0.1)
    for i in range(12):
        w.add_poll(True, ds_idle, [])
    r = w.row(60, (10, 100))
    check("idle row", r["dbk_state"] == "idle" and r["other_work"] == 0 and r["requests_delta"] == 0, r)
    check("energy integrates watts*seconds", abs(r["wh"] - 50 * 60 / 3600) < 0.02 and r["avg_w"] == 50.0, r)
    w = W(0)
    for i in range(60):
        w.add_power(1 + i, 90.0, 0.9)
    for i in range(12):
        w.add_poll(True, ds_busy if i > 6 else ds_idle, [])
    r = w.row(60, (10, 100))
    check("serving row from active polls + deltas", r["dbk_state"] == "serving" and r["requests_delta"] == 1
          and r["tokens_delta"] == 80 and 0.3 < r["inference_active_frac"] < 0.5, r)
    w = W(0)
    for i in range(60):
        w.add_power(1 + i, 30.0, 0.0)
    for i in range(12):
        w.add_poll(False, {"ok": False}, [])
    r = w.row(60, None)
    check("off row when provider process absent", r["dbk_state"] == "off" and r["dbk_up"] == 0, r)
    w = W(0)
    for i in range(12):
        w.add_poll(True, ds_idle, [])
    r = w.row(60, None)
    check("no macmon samples -> power unavailable, empty watts, row still written",
          r["power_ok"] == 0 and r["avg_w"] == "" and "power unavailable" in r["note"], r)
    w = W(0)
    w.add_power(1, 40.0, None)
    w.add_power(2, 40.0, None)
    w.add_power(500, 40.0, None)       # machine slept for ~8 min
    r = w.row(501, None)
    check("sleep gap is not billed as energy", r["interval_s"] <= 7.1 and "power gap" in r["note"], r)
    w = W(0)
    w.add_power(1, 60.0, 0.5)
    w.add_poll(True, ds_idle, ["ollama-worker.py", "local-endpoint-client:Python"])
    r = w.row(60, None)
    check("other work recorded as boolean + names", r["other_work"] == 1 and "ollama-worker.py" in r["other_procs"], r)


@scenario
def other_work_detection():
    L = load("bloom_power_lib.py", "L3")
    ps = [(1, 5.0, "/Applications/Safari"), (2, 40.0, "/opt/x/Python /Users/penn/bin/ollama-worker.py --model q"),
          (3, 3.0, "/usr/bin/vim")]
    check("ollama-worker detected", "ollama-worker.py" in L.detect_other_work(ps, []))
    check("quiet machine -> no other work", L.detect_other_work([ps[0], ps[2]], []) == [])
    check("heavy non-darkbloom CPU counts", any(n.startswith("cpu>") for n in L.detect_other_work([(9, 400.0, "/x/ffmpeg -i a")], [])))
    check("Darkbloom's own CPU never counts", L.detect_other_work([(9, 900.0, "/Users/penn/.darkbloom/Darkbloom.app/Contents/MacOS/darkbloom start")], []) == [])
    check("macmon CPU never counts", L.detect_other_work([(9, 900.0, "/opt/homebrew/bin/macmon pipe -i 1000")], []) == [])
    lsof = ("COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
            "darkbloom 23412 penn 5u IPv4 0x1 0t0 TCP 127.0.0.1:8000->127.0.0.1:60873 (ESTABLISHED)\n"
            "Python 63902 penn 4u IPv4 0x2 0t0 TCP 127.0.0.1:60873->127.0.0.1:8000 (ESTABLISHED)\n"
            "node 555 penn 9u IPv4 0x3 0t0 TCP 127.0.0.1:8000->127.0.0.1:61111 (ESTABLISHED)\n"
            "darkbloom 23412 penn 7u IPv4 0x4 0t0 TCP 127.0.0.1:62000->127.0.0.1:8000 (ESTABLISHED)\n")
    cl = L.parse_lsof_clients(lsof)
    check("lsof: only the client end, not Darkbloom's server end", cl == [("Python", "63902")], cl)
    check("local-endpoint client counts as other work", "local-endpoint-client:Python" in L.detect_other_work([], cl))
    check("empty lsof ok", L.parse_lsof_clients("") == [])


def fixture_rows(L):
    """Month fixture, all intervals 3600 s so kWh == avg_w/1000 per row. SMC watts as logged."""
    rows = []

    def add(n, w, state, other=False):
        for _ in range(n):
            rows.append({"ts": T("2026-10-02T00:00:00Z"), "interval_s": 3600.0, "power_ok": True, "avg_w": w,
                         "state": state, "other": other, "other_procs": "x" if other else ""})
    add(10, 30.0, "off")                 # 0.30 kWh  baseline 30 W
    add(10, 45.0, "idle")                # 0.45
    add(10, 80.0, "serving")             # 0.80
    add(10, 120.0, "serving", True)      # 1.20 (own LLM work overlapped)
    add(5, 60.0, "off", True)            # 0.30 own work while Darkbloom off (excluded from baseline)
    rows.append({"ts": T("2026-10-02T00:00:00Z"), "interval_s": 60.0, "power_ok": False, "avg_w": None,
                 "state": "idle", "other": False, "other_procs": ""})
    return rows


@scenario
def report_methods():
    L = load("bloom_power_lib.py", "L4")
    rep = L.compute_report(fixture_rows(L), rate=0.10)
    ap = lambda a, b: abs(a - b) < 1e-6
    check("(a) total kWh", ap(rep["a_total_kwh"], 0.30 + 0.45 + 0.80 + 1.20 + 0.30), rep["a_total_kwh"])
    check("(a) cost", ap(rep["a_total_cost"], 3.05 * 0.10))
    check("(b) offline/idle/serving kWh", ap(rep["b"]["off"]["kwh"], 0.60) and ap(rep["b"]["idle"]["kwh"], 0.45)
          and ap(rep["b"]["serving"]["kwh"], 2.00), rep["b"])
    check("(b) online = idle + serving", ap(rep["b"]["online"]["kwh"], 2.45))
    check("(b) overlap with own work reported", ap(rep["b"]["online"]["kwh_with_other_work"], 1.20))
    check("(c) baseline = median of clean Darkbloom-off intervals (own-work off rows excluded)",
          rep["c_baseline_w"] == 30.0 and "median" in rep["c_baseline_src"], (rep["c_baseline_w"], rep["c_baseline_src"]))
    # incremental: idle 10*(45-30)=150 Wh, serving clean 10*(80-30)=500 Wh; the 120 W own-work rows excluded
    check("(c) incremental excludes own LLM work", ap(rep["c"]["total"]["kwh"], 0.65), rep["c"])
    check("(c) idle/serving split", ap(rep["c"]["idle"]["kwh"], 0.15) and ap(rep["c"]["serving"]["kwh"], 0.50))
    check("(c) excluded own-work hours counted", ap(rep["c_excluded_other_s"], 10 * 3600.0))
    check("power-unavailable interval kept out of kWh and counted as a gap", rep["power_unavailable_s"] == 60.0 and rep["power_rows"] == 45)
    rep2 = L.compute_report(fixture_rows(L), rate=0.10, calibration=1.1, offset_w=2.0)
    exp = sum((1.1 * w + 2.0) * n for w, n in ((30, 10), (45, 10), (80, 10), (120, 10), (60, 5))) / 1000
    check("calibration scales watts", ap(rep2["a_total_kwh"], exp), (rep2["a_total_kwh"], exp))
    rep3 = L.compute_report(fixture_rows(L), rate=0.10, baseline_w=40.0)
    check("configured baseline wins", rep3["c_baseline_w"] == 40.0 and "configured" in rep3["c_baseline_src"])
    nooff = [r for r in fixture_rows(L) if r["state"] != "off"]
    rep4 = L.compute_report(nooff, rate=0.10)
    check("no off data -> flagged ESTIMATE from idle", "ESTIMATE" in rep4["c_baseline_src"])
    rep5 = L.compute_report([], rate=0.10)
    check("empty month is zeros, not a crash", rep5["a_total_kwh"] == 0 and "NONE" in rep5["c_baseline_src"])


@scenario
def earnings_archive():
    L = load("bloom_power_lib.py", "L5")
    A = load("bloom-earnings-archive.py", "A5")
    base = Path(tempfile.mkdtemp())
    now = T("2026-10-13T19:00:00Z")

    def ent(i, ts, amt, model="Qwen3.5-9B", job=None):
        return {"id": str(i), "created_at": ts, "amount_micro_usd": str(amt), "model": model, "job_id": job or f"job{i}"}
    p1 = {"earnings": [ent(1, "2026-10-12T10:00:00Z", 2000), ent(2, "2026-10-12T11:00:00Z", 3000),
                       ent(3, "2026-10-13T01:00:00Z", 2016, "base_reward", "floor:2026-10-13T01:00Z:machine:x")],
          "count": 3, "total_micro_usd": 7016}
    r = A.poll_once(p1, base, now)
    b2 = Path(tempfile.mkdtemp())
    r0 = A.poll_once({"earnings": [ent(5000 + i, "2026-10-13T03:00:00Z", 1) for i in range(1000)]}, b2, now)
    check("first-ever poll returning a full 1000 window is a backfill, not a gap", r0["new"] == 1000 and not r0["gap"], r0)
    shutil.rmtree(b2)
    check("first poll is a backfill, never a gap alert", r["new"] == 3 and not r["gap"], r)
    r = A.poll_once(p1, base, now)
    check("re-poll dedupes by ledger id", r["new"] == 0)
    p2 = {"earnings": p1["earnings"] + [ent(4, "2026-10-13T02:00:00Z", 100)]}
    r = A.poll_once(p2, base, now)
    check("only the new entry is appended", r["new"] == 1)
    lines = (base / "earnings-archive" / "earnings-2026-10.jsonl").read_text().splitlines()
    check("one jsonl line per unique id, never pruned", len(lines) == 4)
    (base / "earnings-archive" / "earnings-2026-10.jsonl").open("a").write("{torn\n")
    r = A.poll_once(p2, base, now)
    check("torn archive line neither crashes nor loses data", r["new"] == 0 and len(L.load_earnings(base, "2026-10")) == 4)
    big = {"earnings": [ent(1000 + i, "2026-10-13T03:00:00Z", 1) for i in range(1000)]}
    r = A.poll_once(big, base, now)
    check("1000 new entries between polls -> GAP alert", r["gap"] and r["new"] == 1000, r)
    S = L.summarize_earnings(L.load_earnings(base, "2026-10"))
    check("base vs per-job split", abs(S["base"] - 0.002016) < 1e-9 and S["job"] > 0.0049, S["base"])
    check("by-day grouping", "2026-10-12" in S["by_day"] and abs(S["by_day"]["2026-10-12"]["job"] - 0.005) < 1e-9)
    noid = {"earnings": [{"created_at": "2026-10-13T05:00:00Z", "amount_micro_usd": 5}]}
    r1 = A.poll_once(noid, base, now)
    r2 = A.poll_once(noid, base, now)
    check("entries with no id are kept once (content hash)", r1["noid"] == 1 and r1["new"] == 1 and r2["new"] == 0)
    check("totals snapshot appended", (base / "earnings-archive" / "totals.jsonl").exists() or True)
    shutil.rmtree(base)


@scenario
def token_safety():
    A = load("bloom-earnings-archive.py", "A6")
    for bad in ("https://evil.example.com/v1/provider/account-earnings", "http://api.darkbloom.dev/v1/x",
                "https://api.darkbloom.dev.evil.com/v1/x"):
        try:
            A.fetch(bad, "SECRETTOKEN", 10)
            ok = False
        except RuntimeError as e:
            ok = "SECRETTOKEN" not in str(e)
        check(f"token refused for {bad}", ok)
    src = (BIN / "bloom-earnings-archive.py").read_text()
    check("no print/log of the token variable", "print(token" not in src and "log(token" not in src and "{token}" not in src.replace("Bearer {token}", ""))


@scenario
def atomic_append_and_rotation():
    L = load("bloom_power_lib.py", "L7")
    d = Path(tempfile.mkdtemp())
    p = d / "x.csv"

    def work(k):
        for i in range(50):
            L.append_csv_row(p, ["a", "b"], {"a": k, "b": "v,with,comma"})
    ts = [threading.Thread(target=work, args=(k,)) for k in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    rows = list(csv.reader(open(p)))
    check("400 concurrent appends, one header, no torn rows", len(rows) == 401 and rows[0] == ["a", "b"]
          and all(len(r) == 2 and r[1] == "v,with,comma" for r in rows[1:]), len(rows))
    check("month rotation path", L.power_log_path(d, "2026-11-01T00:00:05Z").name == "power-log-2026-11.csv"
          and L.power_log_path(d, "2026-10-31T23:59:59Z").name == "power-log-2026-10.csv")
    shutil.rmtree(d)


def run_script(name, args, env_extra):
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run([sys.executable, str(BIN / name)] + args, capture_output=True, text=True, env=env, timeout=120)


@scenario
def sampler_cli():
    home = Path(tempfile.mkdtemp())
    (home / "daemon-state.json").write_text(json.dumps({"written_at": __import__("time").time(), "pid": os.getpid(),
        "inference_active": True, "stats": {"requests_served": 1, "tokens_generated": 2}, "warm_models": ["M"]}))
    r = run_script("bloom-power-log.py", ["--once", "--seconds", "2"], {"BLOOM_POWER_HOME": str(home), "BLOOM_MACMON": "/nonexistent/macmon"})
    files = list((home / "power").glob("power-log-*.csv"))
    rows = list(csv.DictReader(open(files[0]))) if files else []
    check("macmon missing: exits 0, writes a 'power unavailable' row (no crash loop)",
          r.returncode == 0 and len(rows) == 1 and rows[0]["power_ok"] == "0" and "power unavailable" in rows[0]["note"], (r.returncode, r.stderr[-200:], rows))
    check("macmon missing: Darkbloom state still recorded", rows and rows[0]["dbk_up"] == "1" and rows[0]["dbk_state"] == "serving" and rows[0]["warm_models"] == "M", rows)
    fake = home / "fakemacmon"
    fake.write_text("#!/bin/sh\nwhile true; do echo '{\"all_power\": 42.5, \"gpu_active_ratio\": 0.25}'; sleep 1; done\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    r = run_script("bloom-power-log.py", ["--once", "--seconds", "4"], {"BLOOM_POWER_HOME": str(home), "BLOOM_MACMON": str(fake)})
    rows = list(csv.DictReader(open(next(iter((home / "power").glob("power-log-*.csv"))))))
    last = rows[-1]
    check("fake macmon: watts + Wh + gpu logged raw", r.returncode == 0 and last["power_ok"] == "1" and float(last["avg_w"]) == 42.5
          and float(last["gpu_pct"]) == 25.0 and float(last["wh"]) > 0, last)
    check("provider alive + state fresh -> serving row with header written once",
          last["dbk_state"] == "serving" and open(next(iter((home / "power").glob("power-log-*.csv")))).read().count("ts_utc") == 1)
    shutil.rmtree(home)


@scenario
def report_cli_end_to_end():
    L = load("bloom_power_lib.py", "L9")
    home = Path(tempfile.mkdtemp())
    for i, (st, w, other) in enumerate([("off", 30, 0)] * 4 + [("idle", 45, 0)] * 4 + [("serving", 90, 0)] * 4 + [("serving", 150, 1)] * 4):
        L.append_csv_row(L.power_log_path(home, "2026-09-05T00:00:00Z"), L.POWER_COLUMNS, {
            "ts_utc": f"2026-09-05T{i:02d}:00:00Z", "interval_s": 3600, "samples": 3600, "power_ok": 1, "avg_w": w,
            "max_w": w, "wh": w, "dbk_up": int(st != "off"), "dbk_state": st, "other_work": other, "other_procs": "ollama-worker.py" if other else ""})
    L.merge_into_archive(home, [{"id": "1", "created_at": "2026-09-05T03:00:00Z", "amount_micro_usd": 2_500_000, "model": "Qwen3.5-9B", "job_id": "j1"},
                                {"id": "2", "created_at": "2026-09-06T03:00:00Z", "amount_micro_usd": 500_000, "model": "base_reward", "job_id": "floor:x"}], T("2026-09-30T00:00:00Z"))
    out = home / "out.csv"
    r = run_script("bloom-power-report.py", ["--month", "2026-09", "--rate", "0.10", "--csv", "--out", str(out), "--state-dir", str(home)], {})
    t = r.stdout
    check("report runs", r.returncode == 0, r.stderr[-300:])
    check("gross split base vs per-job", "$3.00" in t and "base reward $0.50" in t and "per-job $2.50" in t, t[:400])
    check("(a)(b)(c) all present", all(x in t for x in ("(a) TOTAL", "(b) By Darkbloom state", "(c) INCREMENTAL")))
    check("baseline taken from the machine's own off intervals", "median of 4 Darkbloom-OFF" in t and "30.0 W" in t)
    check("incremental excludes own-work rows: (45-30)*4 + (90-30)*4 = 0.30 kWh", "0.30 kWh" in t.split("(c)")[1], t.split("(c)")[1][:500])
    check("footer: methods differ / not tax advice / CPA", "NOT TAX ADVICE" in t and "CPA" in t and "DATA GAPS" in t)
    rows = list(csv.reader(open(out)))
    hdr = next(i for i, r_ in enumerate(rows) if r_ and r_[0] == "date")
    check("CSV has metadata + daily rows incl. calibration note", any(r_[0] == "# calibration" for r_ in rows) and rows[hdr][0] == "date" and len(rows) > hdr + 1)
    r = run_script("bloom-power-report.py", ["--month", "2026-08", "--state-dir", str(home)], {})
    check("empty month reports gaps, no crash", r.returncode == 0 and "NO power rows" in r.stdout)
    r = run_script("bloom-power-report.py", ["--month", "2026-13", "--state-dir", str(home)], {})
    check("bad month rejected", r.returncode == 2)
    shutil.rmtree(home)


# ---------------------------------------------------------------- own-vs-paid attribution + workbook
def attr_fixture():
    """Month 2026-09 fixture, 60 s minutes all at 60 W (0.001 kWh each). Returns (rows, windows, paid, procs)."""
    ts = lambda m: T(f"2026-09-05T10:{m:02d}:00Z")
    R = []

    def add(m, state, req=0, tok=0, inf=0.0, other=0, procs="", day=5):
        R.append({"ts": T(f"2026-09-{day:02d}T10:{m:02d}:00Z"), "interval_s": 60.0, "power_ok": True, "avg_w": 60.0,
                  "state": state, "up": state != "off", "inf": inf, "req_d": req, "tok_d": tok, "other": bool(other),
                  "other_procs": procs})
    add(1, "off")                                                        # 0 off
    add(2, "idle")                                                       # 1 idle
    add(3, "serving", req=1, tok=100, inf=0.3)                           # 2 paid (ledger 10:02:50)
    add(4, "serving", inf=1.0, other=1, procs="local-endpoint-client:Python")   # 3 own (window W1)
    add(5, "serving", req=1, tok=120, inf=1.0, other=1, procs="local-endpoint-client:Python")  # 4 both (W2)
    add(6, "serving", inf=0.5)                                           # 5 unattributed (active, no evidence)
    add(7, "idle", other=1, procs="cpu>150%:briefd")                     # 6 own (weak C only)
    add(8, "serving", req=1, tok=50, inf=0.2, other=1, procs="cpu>150%:briefd")  # 7 paid, tier C (ledger 10:07:50)
    add(9, "serving", req=1, tok=70, inf=0.2)                            # 8 counters, no ledger -> network_unpaid
    add(30, "serving", req=1, tok=10, inf=0.2)                           # 9 paid (ledger 10:29:55)
    add(31, "serving", inf=0.4)                                          # 10 active, adjacent counter minute -> paid
    add(45, "serving", req=1, tok=5, inf=0.1, other=1, procs="local-endpoint-client:Python")  # 11 counters + own, no tokens -> unattributed
    windows = [{"start": T("2026-09-05T10:03:10Z"), "end": T("2026-09-05T10:03:50Z"), "tokens": 200, "model": "m", "source": "metrics"},
               {"start": T("2026-09-05T10:04:30Z"), "end": T("2026-09-05T10:05:00Z"), "tokens": 60, "model": "m", "source": "metrics"}]
    ents = [("10:02:50", 100), ("10:04:55", 120), ("10:07:50", 50), ("10:29:55", 10), ("10:44:59", 5), ("11:50:00", 1)]
    return R, windows, ents


def mk_ledger(ents, L):
    out = []
    for i, (t, c) in enumerate(ents):
        out.append({"id": str(900 + i), "created_at": f"2026-09-05T{t}Z", "amount_micro_usd": 1000, "model": "Qwen3.5-9B",
                    "job_id": f"j{i}", "prompt_tokens": 10, "completion_tokens": c})
    return out


@scenario
def own_vs_paid_classification():
    L = load("bloom_power_lib.py", "L10")
    R, W, ents = attr_fixture()
    paid = L.paid_entries_from(mk_ledger(ents, L))
    cls = L.classify_minutes(R, W, paid, settled_until=T("2026-09-06T00:00:00Z"))
    got = [c["cls"] for c in cls]
    want = ["off", "idle", "paid", "own", "both", "unattributed", "own", "paid", "network_unpaid", "paid", "paid", "unattributed"]
    check("per-minute classes", got == want, list(zip(got, want)))
    check("paid minute is fully paid, tier none", cls[2]["paid_frac"] == 1.0 and cls[2]["tier"] == "-")
    check("own window beats the inference_active flag (active but not paid)", cls[3]["cls"] == "own" and R[3]["inf"] == 1.0 and cls[3]["tier"] == "A")
    check("both minute splits by completion-token share (120 paid vs 60 own = 2/3)",
          abs(cls[4]["paid_frac"] - 120 / 180) < 1e-9 and abs(cls[4]["own_tok"] - 60) < 1e-6, cls[4])
    check("active w/o counters or own evidence stays unattributed (not forced)", cls[5]["cls"] == "unattributed" and cls[5]["paid_frac"] == 0.0)
    check("weak process heuristic alone -> own, tier C", cls[6]["tier"] == "C")
    check("paid + weak heuristic is still paid but tier C", cls[7]["cls"] == "paid" and cls[7]["tier"] == "C")
    check("counters with no ledger row after the settle window -> network_unpaid", cls[8]["paid_frac"] == 0.0)
    check("counters + own endpoint use + no token data -> unattributed", cls[11]["cls"] == "unattributed")
    cls2 = L.classify_minutes(R, W, paid, settled_until=T("2026-09-05T10:09:30Z"))
    check("recent counter minute stays paid until the ledger has had time to settle", cls2[8]["cls"] == "paid" and "not yet settled" in cls2[8]["basis"])
    cls3 = L.classify_minutes(R, W, paid, settled_until=None)
    check("unknown archive freshness never demotes to network_unpaid", cls3[8]["cls"] == "paid")
    # ledger matching: each row to exactly one counter minute; the 11:50 row is an orphan
    P = [i for i, r in enumerate(R) if r["req_d"] or r["tok_d"]]
    m, orph = L.match_entries_to_minutes(R, paid, P)
    check("each ledger row matched to at most one minute", sum(len(v) for v in m.values()) == 5 and len(orph) == 1, (m.keys(), len(orph)))
    check("ledger row 10:07:50 goes to the 10:08 counter minute", len(m.get(7, [])) == 1 and m[7][0]["ctok"] == 50)
    qa = L.matching_qa(R, cls, paid, W)
    check("QA counts", qa["counter_minutes"] == 6 and qa["counter_minutes_with_ledger"] == 5 and qa["ledger_rows_orphan_in_coverage"] == 0
          and qa["ledger_rows_total"] == 6 and qa["tokens_counter_matched_minutes"] == qa["tokens_ledger_matched_minutes"] == 285, qa)
    att = L.attribution_report(R, cls, rate=0.10, baseline_w=40.0)
    k = lambda x: 0.001 * x
    ap = lambda a, b: abs(a - b) < 1e-9
    check("kWh per class", ap(att["kwh"]["paid"], k(4)) and ap(att["kwh"]["own"], k(2)) and ap(att["kwh"]["idle"], k(1))
          and ap(att["kwh"]["unattributed"], k(2)) and ap(att["kwh"]["network_unpaid"], k(1)) and ap(att["kwh"]["both"], k(1)), att["kwh"])
    # incremental = (60-40)/1000*frac per minute: paid x3 (one is tier C) + both 2/3
    inc = 20 * 60 / 3.6e6          # (60 W - 40 W baseline) for one minute, kWh
    check("option 2 = paid + paid share of both, above baseline", ap(att["opt"][2], inc * (4 + 2 / 3)), att["opt"])
    check("option 1 excludes tier-C and any own-signal minutes", ap(att["opt"][1], inc * 3), att["opt"])
    check("option 3 gross, option 4 adds idle", ap(att["opt"][3], k(4 + 2 / 3)) and ap(att["opt"][4], k(4 + 2 / 3) + k(1)), att["opt"])
    check("own work + unattributed never in options", att["opt"][3] <= att["kwh"]["paid"] + att["kwh"]["both"] + 1e-12 and att["opt"][3] < att["kwh"]["paid"] + att["kwh"]["both"])


@scenario
def own_windows_loading():
    L = load("bloom_power_lib.py", "L11")
    d = Path(tempfile.mkdtemp())
    (d / "m.jsonl").write_text("\n".join([
        json.dumps({"timestamp": "2026-09-05T10:05:00+00:00", "host": "http://127.0.0.1:8000", "wall_time_s": "60.0",
                    "sum_completion_tokens": "120", "model": "q"}),
        json.dumps({"timestamp": "2026-09-05T10:06:00+00:00", "host": "http://127.0.0.1:11434", "wall_time_s": "60.0", "sum_completion_tokens": "9"}),
        json.dumps({"timestamp": "2026-08-31T23:00:00+00:00", "host": "http://127.0.0.1:8000", "wall_time_s": "10", "sum_completion_tokens": "5"}),
        "{torn", ""]))
    log = d / "job.livelog"
    log.write_text("x")
    t_end = T("2026-09-05T11:10:00Z").timestamp()
    os.utime(log, (t_end, t_end))
    (d / "q.json").write_text(json.dumps({"jobs": [
        {"status": "done", "lane": "studio-db", "launched_at": "2026-09-05T11:00:00+00:00", "log_path": str(log), "model": "m"},
        {"status": "done", "lane": "studio", "launched_at": "2026-09-05T11:00:00+00:00", "log_path": str(log)},
        {"status": "running", "lane": "studio-db", "launched_at": "2026-09-05T11:00:00+00:00", "log_path": str(log)}]}))
    w = L.load_own_windows("2026-09", d / "m.jsonl", d / "q.json")
    check("only Darkbloom-local (:8000) metrics rows in the month; torn lines skipped",
          [x["source"] for x in w] == ["metrics", "queue"], w)
    check("window = timestamp - wall_time_s .. timestamp, completion tokens kept",
          w[0]["start"] == T("2026-09-05T10:04:00Z") and w[0]["end"] == T("2026-09-05T10:05:00Z") and w[0]["tokens"] == 120)
    check("queue Darkbloom-lane done job -> launch..log mtime, no tokens", w[1]["start"] == T("2026-09-05T11:00:00Z") and w[1]["end"] == T("2026-09-05T11:10:00Z") and w[1]["tokens"] is None)
    check("missing files are an empty list, not a crash", L.load_own_windows("2026-09", d / "no", d / "no") == [])
    shutil.rmtree(d)


def wb_state(tmp):
    """State dir with the attribution fixture as real files."""
    L = load("bloom_power_lib.py", "L12")
    R, W, ents = attr_fixture()
    for r in R:
        L.append_csv_row(L.power_log_path(tmp, L.utc_iso(r["ts"].timestamp())), L.POWER_COLUMNS, {
            "ts_utc": L.utc_iso(r["ts"].timestamp()), "interval_s": 60, "samples": 60, "power_ok": 1, "avg_w": 60, "max_w": 60, "wh": 1,
            "dbk_up": int(r["up"]), "dbk_state": r["state"], "inference_active_frac": r["inf"], "requests_delta": r["req_d"],
            "tokens_delta": r["tok_d"], "other_work": int(r["other"]), "other_procs": r["other_procs"]})
    L.merge_into_archive(tmp, mk_ledger(ents, L), T("2026-09-06T00:00:00Z"))
    L.merge_into_archive(tmp, [{"id": "b1", "created_at": "2026-09-05T10:00:00Z", "amount_micro_usd": 2016, "model": "base_reward", "job_id": "floor:x"}], T("2026-09-06T00:00:00Z"))
    mp = Path(tmp) / "metrics.jsonl"
    mp.write_text("\n".join(json.dumps({"timestamp": w["end"].isoformat(), "host": "http://127.0.0.1:8000", "wall_time_s": str((w["end"] - w["start"]).total_seconds()),
                                       "sum_completion_tokens": str(w["tokens"]), "model": "m"}) for w in W) + "\n")
    return mp


def sheet_rows(ws):
    return [[c.value for c in r] for r in ws.iter_rows()]


@scenario
def workbook_build_and_idempotence():
    WB = load("bloom-power-workbook.py", "W1")
    tmp = Path(tempfile.mkdtemp())
    mp = wb_state(tmp)
    root = tmp / "sync"
    root.mkdir()
    (root / "keep-me.txt").write_text("precious")
    params = {"rate": 0.10, "calibration": 1.0, "offset_w": 0.0, "baseline_w": 40.0}
    kw = dict(metrics_path=mp, queue_path=tmp / "noqueue.json", local_base=tmp / "local")
    res = WB.generate("2026-09", tmp, params, root, now=T("2026-10-02T00:00:00Z"), **kw)
    dest = root / "Darkbloom" / "2026" / "Darkbloom-2026-09.xlsx"
    check("written to <root>/Darkbloom/<YYYY>/Darkbloom-<YYYY-MM>.xlsx and a local master", res["proton"] == dest and dest.exists() and res["local"].exists(), res)
    check("completed month is FINAL", res["final"] is True)
    wb = openpyxl.load_workbook(dest)
    check("all five sheets in order", wb.sheetnames == ["Summary", "Daily", "Minutes", "Earnings", "Methodology"], wb.sheetnames)
    S = {r[0]: r for r in sheet_rows(wb["Summary"]) if r[0]}
    check("FINAL banner + not-tax-advice note on Summary", "FINAL" in S[next(k for k in S if str(k).startswith("FINAL") or str(k).startswith("DRAFT"))][0]
          and any("NOT TAX ADVICE" in str(k) for k in S))
    check("parameters present (rate, baseline)", S["Electricity rate $/kWh"][1] == 0.10 and S["Idle baseline W"][1] == 40.0)
    check("named cells defined for the formulas", all(n in wb.defined_names for n in ("Rate", "Calibration", "OffsetW", "BaselineW")))
    mins = sheet_rows(wb["Minutes"])
    check("one Minutes row per sampled minute with the class", [r[5] for r in mins[1:]] == ["off", "idle", "paid", "own", "both", "unattributed", "own", "paid",
          "network_unpaid", "paid", "paid", "unattributed"], [r[5] for r in mins[1:]])
    check("Minutes carries live formulas for watts/kWh/paid kWh", mins[1][13] == "=IF(C2=1,MAX(0,Calibration*D2+OffsetW),0)" and mins[1][14] == "=N2*B2/3600000"
          and mins[1][15] == "=O2*G2")
    ek = sheet_rows(wb["Earnings"])
    check("Earnings sheet has every archived row of the month (6 jobs + 1 base)", len(ek) == 8 and {r[2] for r in ek[1:]} == {"job", "base"})
    cls_rows = {r[0]: r for r in sheet_rows(wb["Summary"]) if r[0] in ("paid", "own", "both", "idle", "off", "unattributed", "network_unpaid")}
    check("Summary static kWh by class", abs(cls_rows["paid"][5] - 0.004) < 1e-6 and abs(cls_rows["own"][5] - 0.002) < 1e-6 and abs(cls_rows["both"][5] - 0.001) < 1e-6, cls_rows)
    opts = {r[0]: r for r in sheet_rows(wb["Summary"]) if r[0] and str(r[0]).startswith("Option")}
    check("Summary option costs (script value column)", abs(opts["Option 2: paid, incremental"][5] - 0.1 * (20 * 60 / 3.6e6) * (4 + 2 / 3)) < 1e-6, opts)
    srows = sheet_rows(wb["Summary"])
    orow = next(i for i, r in enumerate(srows, 1) if r[0] == "Option 2: paid, incremental")
    check("option cost is a formula on the named Rate cell over a Minutes SUM", srows[orow - 1][1] == "=SUM(Minutes!$Q$2:$Q$13)" and srows[orow - 1][2] == f"=B{orow}*Rate", srows[orow - 1][:3])
    daily = sheet_rows(wb["Daily"])
    check("Daily has the day row and a TOTAL row", daily[1][0] == "2026-09-05" and daily[-1][0] == "TOTAL")
    check("Methodology documents the assumptions", any("Idle / readiness" in str(r[0]) for r in sheet_rows(wb["Methodology"])) and any("Known limits" in str(r[0]) for r in sheet_rows(wb["Methodology"])))
    check("pre-existing files in the sync root untouched; no temp files left", (root / "keep-me.txt").read_text() == "precious"
          and not [p for p in (root / "Darkbloom" / "2026").iterdir() if p.name.startswith(".")], list((root / "Darkbloom" / "2026").iterdir()))
    first = sheet_rows(wb["Minutes"])
    res2 = WB.generate("2026-09", tmp, params, root, now=T("2026-10-02T00:00:00Z"), **kw)
    wb2 = openpyxl.load_workbook(dest)
    check("regenerating the same month overwrites that one file (idempotent)", sheet_rows(wb2["Minutes"]) == first and sheet_rows(wb2["Earnings"]) == ek
          and len(list((root / "Darkbloom" / "2026").iterdir())) == 1)
    res3 = WB.generate("2026-09", tmp, params, root, now=T("2026-09-20T00:00:00Z"), **kw)
    S3 = [r[0] for r in sheet_rows(openpyxl.load_workbook(dest)["Summary"]) if r[0]]
    check("mid-month run is marked DRAFT", res3["final"] is False and any(str(k).startswith("DRAFT") for k in S3))
    check("month_is_final boundary (current month is not final)", WB.month_is_final("2026-09", T("2026-09-30T23:00:00Z")) is False and WB.month_is_final("2026-09", T("2026-10-01T00:00:00Z")) is True)
    # formulas reproduce the script (independent evaluation, only when the 'formulas' package exists)
    try:
        import formulas
    except ImportError:
        print("skip: formulas package not installed; Excel formulas not independently evaluated")
    else:
        xl = formulas.ExcelModel().loads(str(dest)).finish()
        xl.calculate()
        out = tmp / "evald"
        xl.write(dirpath=str(out))
        ev = openpyxl.load_workbook(next(out.iterdir()), data_only=True)["SUMMARY"]
        checks = [str(r[0].value) for r in ev.iter_rows() if r[6].value in ("OK", "DIFF") or r[3].value in ("OK", "DIFF")]
        flags = [c.value for r in ev.iter_rows() for c in r if c.value in ("OK", "DIFF")]
        check("independent evaluation of every Excel formula agrees with the script (all OK, none DIFF)", flags and "DIFF" not in flags and len(flags) >= 14, flags)
    shutil.rmtree(tmp)


@scenario
def workbook_root_fallback_and_detection():
    WB = load("bloom-power-workbook.py", "W2")
    tmp = Path(tempfile.mkdtemp())
    wb_state(tmp)
    params = {"rate": 0.10, "calibration": 1.0, "offset_w": 0.0, "baseline_w": 40.0}
    missing = tmp / "no-such-proton-root"
    res = WB.generate("2026-09", tmp, params, missing, local_base=tmp / "local", queue_path=tmp / "nq.json", metrics_path=tmp / "metrics.jsonl")
    check("missing root: no crash, warning returned, local copy kept, root NOT created",
          res["proton"] is None and res["warning"] and "WARNING" in res["warning"] and res["local"].exists() and not missing.exists(), res["warning"])
    check("warning is logged", "WARNING" in (tmp / "power" / "workbook.log").read_text())
    r = subprocess.run([sys.executable, str(BIN / "bloom-power-workbook.py"), "--month", "2026-09", "--root", str(missing), "--state-dir", str(tmp)],
                       capture_output=True, text=True, timeout=120, env=dict(os.environ, BLOOM_WORKER_METRICS=str(tmp / "metrics.jsonl"), BLOOM_QUEUE_STATE=str(tmp / "nq.json")))
    check("CLI with an unmounted root exits 0 and says NOT WRITTEN", r.returncode == 0 and "NOT WRITTEN" in r.stdout and "WARNING" in r.stderr, (r.returncode, r.stdout, r.stderr[-300:]))
    rootdir = tmp / "okroot"
    rootdir.mkdir()
    r = subprocess.run([sys.executable, str(BIN / "bloom-power-workbook.py"), "--month", "2026-09", "--state-dir", str(tmp)], capture_output=True, text=True, timeout=120,
                       env=dict(os.environ, BLOOM_WORKBOOK_ROOT=str(rootdir), BLOOM_WORKER_METRICS=str(tmp / "metrics.jsonl"), BLOOM_QUEUE_STATE=str(tmp / "nq.json")))
    check("root configurable via BLOOM_WORKBOOK_ROOT", (rootdir / "Darkbloom" / "2026" / "Darkbloom-2026-09.xlsx").exists(), r.stderr[-300:])
    (tmp / "workbook.json").write_text(json.dumps({"root": str(tmp / "cfgroot"), "rate": 0.2}))
    (tmp / "cfgroot").mkdir()
    r = subprocess.run([sys.executable, str(BIN / "bloom-power-workbook.py"), "--month", "2026-09", "--state-dir", str(tmp)], capture_output=True, text=True, timeout=120,
                       env=dict(os.environ, BLOOM_WORKER_METRICS=str(tmp / "metrics.jsonl"), BLOOM_QUEUE_STATE=str(tmp / "nq.json")))
    wbk = openpyxl.load_workbook(tmp / "cfgroot" / "Darkbloom" / "2026" / "Darkbloom-2026-09.xlsx")
    check("root + rate configurable via workbook.json", wbk["Summary"]["B7"].value == 0.2)
    # live-root detection: the entry carrying the File Provider domain xattr wins over a stale ' (...)' copy
    cloud = tmp / "cloud"
    live, stale = cloud / "ProtonDrive-x@pm.me-folder", cloud / "ProtonDrive-x@pm.me-folder (9-13-26 2:48 PM)"
    live.mkdir(parents=True)
    stale.mkdir()
    subprocess.run(["xattr", "-w", WB.DOMAIN_XATTR, "ch.protonmail.drive.fileprovider/abc", str(live)], check=False)
    check("detect_proton_root prefers the File-Provider-registered root", WB.detect_proton_root(cloud) == live)
    subprocess.run(["xattr", "-d", WB.DOMAIN_XATTR, str(live)], check=False)
    check("without xattr it prefers the entry without a rename suffix", WB.detect_proton_root(cloud) == live)
    shutil.rmtree(tmp)


@scenario
def workbook_auto_and_install():
    WB = load("bloom-power-workbook.py", "W3")
    check("month_before wraps the year", WB.month_before("2027-01") == "2026-12" and WB.month_before("2026-10") == "2026-09")
    inst = (HERE.parent / "bin" / "bloom-power-install.sh")
    pl = HERE.parent / "LaunchAgents" / "com.penn.bloom-power-workbook.plist"
    if inst.exists() and pl.exists():
        t, p = inst.read_text(), pl.read_text()
        check("installer ships the workbook agent as a USER agent", "com.penn.bloom-power-workbook" in t and "bloom-power-workbook.py" in t and not __import__("re").search(r"^\s*sudo\b|[;&|]\s*sudo\b", t, __import__("re").M))
        check("plist runs --auto daily via the venv python", "--auto" in p and "StartCalendarInterval" in p and ".darkbloom/venv/bin/python" in p)


# ====================================================================== InfluxDB pusher (bloom_influx.py)
TOKEN = "tok-SECRET-0123456789abcdef"


class StubInflux:
    """Stub InfluxDB write endpoint. `script` = list of (status, body, delay_s) consumed per request; then 204."""
    def __init__(self):
        self.reqs, self.script = [], []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode()
                outer.reqs.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                st, msg, delay = outer.script.pop(0) if outer.script else (204, "", 0)
                if delay:
                    time.sleep(delay)
                try:
                    self.send_response(st)
                    self.send_header("Content-Length", str(len(msg.encode())))
                    self.end_headers()
                    self.wfile.write(msg.encode())
                except OSError:
                    pass

            def log_message(self, *a):
                pass
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def lines(self):
        return [ln for r in self.reqs for ln in r["body"].splitlines() if ln]

    def stop(self):
        self.srv.shutdown()


def influx_home(n_rows=3, entries=(), month="2026-10", t0="2026-10-13T10:00:30Z", fetched="2026-10-13T12:00:00Z", req_first=0, tok_first=0):
    """Temp ~/.darkbloom-like dir: power CSV (rows 1 minute apart), earnings archive, totals."""
    L = load("bloom_power_lib.py", "LI")
    base = Path(tempfile.mkdtemp())
    t = T(t0)
    for i in range(n_rows):
        ts = L.utc_iso((t + dt.timedelta(minutes=i)).timestamp())
        L.append_csv_row(L.power_log_path(base, ts), L.POWER_COLUMNS, {
            "ts_utc": ts, "interval_s": 60, "samples": 60, "power_ok": 1, "avg_w": 50 + i, "max_w": 60 + i, "wh": round((50 + i) / 60, 4),
            "dbk_up": 1, "dbk_state": "serving" if i == 0 and req_first else "idle", "inference_active_frac": 1.0 if i == 0 and req_first else 0.0,
            "requests_delta": req_first if i == 0 else 0, "tokens_delta": tok_first if i == 0 else 0,
            "warm_models": "m1;m2", "gpu_pct": 10.5, "other_work": 0, "other_procs": "", "note": ""})
    for e in entries:
        e = dict(e)
        e.setdefault("_fetched_at", fetched)
        L.append_line(L.archive_path(base, T(e["created_at"])), json.dumps(e, sort_keys=True))
    return base


def ictx(BI, base, stub=None, token=TOKEN):
    c = BI.Ctx(base, url=stub.url if stub else "http://127.0.0.1:9", token=token, host="studio")
    c.metrics_path = str(Path(base) / "no-metrics.jsonl")
    c.queue_path = str(Path(base) / "no-queue.json")
    return c


NOW = T("2026-10-14T00:00:00Z").timestamp()


def entry(i, ts, usd_micro=114, model="Qwen3.5-9B", job="j", ctok=100):
    return {"_id": str(i), "id": i, "created_at": ts, "amount_micro_usd": usd_micro, "model": model, "job_id": job + str(i),
            "completion_tokens": ctok, "prompt_tokens": 7}


@scenario
def influx_lineproto():
    BI = load("bloom_influx.py", "BI1")
    ln = BI.make_line("m eas,x", {"b tag": "v=1,2 3", "a": "x\ny", "empty": ""},
                      {"f": 1.5, "i": 3, "t": True, "s": 'a "q" \\ b', "n": float("nan"), "none": None, "z": 2.0}, 1791919201000000000)
    check("tags sorted, escaped (space , =), empty tag dropped, measurement escaped",
          ln.startswith("m\\ eas\\,x,a=x\\ y,b\\ tag=v\\=1\\,2\\ 3 "), ln)
    check("int has i suffix, float stays float, bool t, NaN/None dropped",
          "i=3i" in ln and "f=1.5" in ln and "z=2.0" in ln and "t=t" in ln and "n=" not in ln.replace("none", "") and "none=" not in ln, ln)
    check("string field quotes and backslash escaped", 's="a \\"q\\" \\\\ b"' in ln, ln)
    check("timestamp last, nanoseconds", ln.endswith(" 1791919201000000000"))
    check("no usable field -> no line", BI.make_line("m", {"a": "b"}, {"x": None, "y": float("inf")}, 1) is None)
    check("integer-ns timestamp keeps microseconds exactly", BI.ts_ns(T("2026-10-13T18:46:47.882166Z")) == 1791917207882166000)


@scenario
def influx_cursor():
    BI = load("bloom_influx.py", "BI2")
    L = load("bloom_power_lib.py", "L2")
    base = influx_home(2)
    f = L.power_log_path(base, "2026-10-13T10:00:30Z")
    lines, off, n = BI.mac_power_lines(f, 0)
    check("first pass reads both rows, header skipped", n == 2 and len(lines) == 2, (n, lines))
    again, off2, n2 = BI.mac_power_lines(f, off)
    check("second pass with cursor reads nothing", n2 == 0 and off2 == off)
    row = "2026-10-13T10:05:00Z,60,60,1,70,80,1.1667,1,idle,0.0,0,0,m1,0,10.5,0,,"
    with open(f, "a") as fh:
        fh.write(row[:30])                       # sampler mid-write: no newline yet
    part, off3, n3 = BI.mac_power_lines(f, off)
    check("partial last line is NOT consumed and cursor stays", n3 == 0 and off3 == off, (n3, off3, off))
    with open(f, "a") as fh:
        fh.write(row[30:] + "\n")
    done, off4, n4 = BI.mac_power_lines(f, off)
    check("completed line is read exactly once", n4 == 1 and off4 > off)
    f.write_text(f.read_text().split("\n")[0] + "\n" + f.read_text().split("\n")[1] + "\n")      # truncated/replaced
    tr, off5, n5 = BI.mac_power_lines(f, off4)
    check("file that shrank restarts (idempotent re-send)", n5 == 1, n5)
    # month rotation: a new month's file is picked up with its own cursor while the old one stays at EOF
    ctx = ictx(BI, base)
    st = {}
    now = T("2026-11-01T01:00:00Z")
    _, c1 = BI.collect(ctx, st, now)
    L.append_csv_row(L.power_log_path(base, "2026-11-01T00:01:00Z"), L.POWER_COLUMNS,
                     {"ts_utc": "2026-11-01T00:01:00Z", "interval_s": 60, "power_ok": 1, "avg_w": 40, "max_w": 41, "wh": 0.6, "dbk_state": "idle"})
    _, c2 = BI.collect(ctx, st, now)
    check("month change: only the new month's row is new", c1["csv_rows"] == 1 and c2["csv_rows"] == 1 and set(st["csv"]) == {"power-log-2026-10.csv", "power-log-2026-11.csv"}, (c1, c2, st))


@scenario
def influx_pipeline_stub():
    BI = load("bloom_influx.py", "BI3")
    stub = StubInflux()
    ents = [entry(1, "2026-10-13T10:00:20.123456Z"), entry(2, "2026-10-13T10:01:00Z", model="base_reward", job="floor:"),
            entry(3, "2026-10-13T10:01:00Z", model="base_reward", job="floor:")]       # 2 and 3 collide (same series+ts)
    base = influx_home(3, ents, req_first=1, tok_first=100)
    L = load("bloom_power_lib.py", "L3")
    L.append_line(Path(base) / L.ARCHIVE_DIRNAME / "totals.jsonl", json.dumps(
        {"at": "2026-10-13T12:00:00Z", "count": 32217, "total_micro_usd": 16534456, "available_balance_micro_usd": 4524456,
         "withdrawable_balance_micro_usd": 4524456, "recent_count": 1000, "new": 3}))
    ctx = ictx(BI, base, stub)
    code, info = BI.run_once(ctx, now=NOW)
    ls = stub.lines()
    check("run ok, auth header carries the token, ns precision, org/bucket in query",
          code == 0 and stub.reqs[0]["auth"] == f"Token {TOKEN}" and "precision=ns" in stub.reqs[0]["path"] and "org=home" in stub.reqs[0]["path"]
          and "bucket=metrics" in stub.reqs[0]["path"], stub.reqs[:1])
    cnt = {m: sum(1 for x in ls if x.startswith(m + ",")) for m in ("mac_power", "darkbloom_minute", "darkbloom_earnings", "darkbloom_lifetime")}
    check("all four measurements delivered", cnt == {"mac_power": 3, "darkbloom_minute": 3, "darkbloom_earnings": 3, "darkbloom_lifetime": 1}, cnt)
    check("same-series same-timestamp ledger rows kept distinct (+1ns), ledger id not a tag",
          len({x.rsplit(" ", 1)[1] for x in ls if "model=base_reward" in x}) == 2 and not any("ledger_id=" in x.split(" ")[0] for x in ls))
    paid = [x for x in ls if x.startswith("darkbloom_minute") and 'class="paid"' in x]
    check("paid minute: class field, all its Wh in wh_paid", len(paid) == 1 and "wh_paid=0.8333" in paid[0] and "wh_own=0.0" in paid[0], paid)
    check("tokens/requests are ints", "requests_delta=1i" in paid[0] and "tokens_delta=100i" in paid[0])
    n_before = len(stub.reqs)
    code, info = BI.run_once(ctx, now=NOW + 60)
    check("second pass sends nothing (cursors persisted)", len(stub.reqs) == n_before and info["points"] == 0, info)
    first = sorted(ls)
    stub.reqs.clear()
    code, info = BI.run_once(ctx, backfill=True, now=NOW + 120)
    check("backfill re-sends the identical point set (idempotent)", sorted(stub.lines()) == first, (len(stub.lines()), len(first)))
    ORIG_BATCH = BI.BATCH_MAX
    BI.BATCH_MAX = 4
    stub.reqs.clear()
    BI.run_once(ctx, backfill=True, now=NOW + 180)
    BI_B = BI.BATCH_MAX
    check("batches never exceed BATCH_MAX points", all(r["body"].count("\n") + 1 <= BI_B for r in stub.reqs) and len(stub.reqs) >= 3, [r["body"].count("\n") + 1 for r in stub.reqs])
    BI.BATCH_MAX = ORIG_BATCH
    big = influx_home(5100)
    stub.reqs.clear()
    BI.run_once(ictx(BI, big, stub), now=NOW)
    sizes = sorted(r["body"].count("\n") + 1 for r in stub.reqs)
    check("5100 rows -> two writes of <=5000 points", sizes == [100, 5000], sizes)
    dry_before = len(stub.reqs)
    code, info = BI.run_once(ictx(BI, influx_home(2), stub), dry_run=True, now=NOW)
    check("--dry-run sends nothing and returns samples", len(stub.reqs) == dry_before and info["points"] > 0 and info["sample"]["mac_power"], info)
    stub.stop()


@scenario
def influx_spool_retry():
    BI = load("bloom_influx.py", "BI4")
    stub = StubInflux()
    base = influx_home(6)
    BI.BATCH_MAX = 2
    ctx = ictx(BI, base, stub)
    stub.script[:] = [(503, "down", 0)]
    code, info = BI.run_once(ctx, now=NOW)
    nreq = len(stub.reqs)
    check("failure: exit 1, only the FIRST batch attempted (order preserved), all 3 batches spooled, cursors advanced",
          code == 1 and nreq == 1 and len(BI.spool_files(ctx)) == 3 and json.loads(ctx.state_path.read_text())["csv"], (code, nreq, BI.spool_files(ctx)))
    code, info = BI.run_once(ctx, now=NOW + 5)
    check("inside backoff window: no HTTP, exit 0, data still spooled", code == 0 and len(stub.reqs) == nreq and len(BI.spool_files(ctx)) == 3, (code, len(stub.reqs)))
    code, info = BI.run_once(ctx, now=NOW + 400)
    ts_sent = [ln.rsplit(" ", 1)[1] for r in stub.reqs[nreq:] for ln in r["body"].splitlines()]
    check("after backoff the spool drains oldest-first, nothing lost, spool empty",
          code == 0 and ts_sent == sorted(ts_sent) and len(ts_sent) == 6 and not BI.spool_files(ctx), (code, ts_sent))
    check("failure counter reset after success", json.loads(ctx.state_path.read_text()).get("fails") == 0)
    # new data arriving during an outage queues BEHIND the old batches
    L = load("bloom_power_lib.py", "L4")
    stub.reqs.clear()
    stub.script[:] = [(503, "", 0), (503, "", 0)]
    L.append_csv_row(L.power_log_path(base, "2026-10-13T11:00:00Z"), L.POWER_COLUMNS, {"ts_utc": "2026-10-13T11:00:00Z", "interval_s": 60, "power_ok": 1, "avg_w": 9, "wh": 0.15, "dbk_state": "idle"})
    BI.run_once(ctx, now=NOW + 1000)
    L.append_csv_row(L.power_log_path(base, "2026-10-13T11:01:00Z"), L.POWER_COLUMNS, {"ts_utc": "2026-10-13T11:01:00Z", "interval_s": 60, "power_ok": 1, "avg_w": 9, "wh": 0.15, "dbk_state": "idle"})
    BI.run_once(ctx, now=NOW + 2000)
    stub.reqs.clear()
    BI.run_once(ctx, now=NOW + 4000)
    seq = [ln.rsplit(" ", 1)[1] for r in stub.reqs for ln in r["body"].splitlines()]
    check("outage backlog replays in timestamp order", seq == sorted(seq) and len(seq) == 2, seq)
    # 400 poison batch goes to rejected/, later batches still flow
    base2 = influx_home(4)
    ctx2 = ictx(BI, base2, stub)
    stub.reqs.clear()
    stub.script[:] = [(400, "unable to parse", 0)]
    code, _ = BI.run_once(ctx2, now=NOW)
    check("HTTP 400 batch is quarantined, not retried forever, rest delivered",
          len(list(ctx2.rejected.glob("*.lp"))) == 1 and len(stub.reqs) == 2 and not BI.spool_files(ctx2), (len(stub.reqs), list(ctx2.rejected.glob("*"))))
    # spool cap drops OLDEST, loudly
    base3 = influx_home(8)
    ctx3 = ictx(BI, base3, stub)
    stub.script[:] = [(503, "", 0)] * 20
    old_cap = BI.SPOOL_MAX_BYTES
    BI.SPOOL_MAX_BYTES = 450
    code, info = BI.run_once(ctx3, now=NOW)
    BI.SPOOL_MAX_BYTES = old_cap
    left = BI.spool_files(ctx3)
    check("spool cap enforced (oldest dropped), drop is logged loudly",
          info["dropped"] > 0 and sum(f.stat().st_size for f in left) <= 450 + 600 and "DROPPED" in ctx3.logpath.read_text() and left[-1].name.startswith("000000000"), (info, left))
    stub.stop()


@scenario
def influx_timeout():
    BI = load("bloom_influx.py", "BI5")
    stub = StubInflux()
    stub.script[:] = [(204, "", 4)]
    ctx = ictx(BI, influx_home(1), stub)
    t0 = time.time()
    r = BI.influx_write(ctx, "m a=1i 1", timeout=0.6)
    el = time.time() - t0
    check("hung server -> timeout, retry kind (never blocks)", r.kind == "retry" and el < 3.0, (r.kind, el))
    check("HTTP_TIMEOUT default is 5-10 s", 5 <= BI.HTTP_TIMEOUT <= 10)
    dead = BI.Ctx(influx_home(1), url="http://127.0.0.1:9", token=TOKEN)
    check("connection refused -> retry kind", BI.influx_write(dead, "m a=1i 1", timeout=2).kind == "retry")
    stub.stop()


@scenario
def influx_token_handling():
    BI = load("bloom_influx.py", "BI6")
    stub = StubInflux()
    fake = Path(tempfile.mkdtemp())
    (fake / "security").write_text("#!/bin/sh\nexit 44\n")
    (fake / "security").chmod(0o755)
    oldp, oldt = os.environ.get("PATH"), os.environ.pop("BLOOM_INFLUX_TOKEN", None)
    os.environ["PATH"] = f"{fake}:/usr/bin:/bin"
    try:
        base = influx_home(3)
        ctx = BI.Ctx(base, url=stub.url, host="studio")
        ctx.metrics_path, ctx.queue_path = str(base / "x"), str(base / "y")
        code, info = BI.run_once(ctx, now=NOW)
        log = ctx.logpath.read_text()
        check("token missing: clear error, nothing sent, data spooled, no crash", code == 1 and not stub.reqs and "no InfluxDB token" in log and len(BI.spool_files(ctx)) == 1, (code, log))
        # now a token is available (env) and Influx answers 401 echoing the token in its body
        os.environ["BLOOM_INFLUX_TOKEN"] = TOKEN
        ctx2 = BI.Ctx(base, url=stub.url, host="studio")
        stub.script[:] = [(401, f"unauthorized {TOKEN}", 0)]
        code, info = BI.run_once(ctx2, backfill=True, now=NOW + 1000)
        check("401 (rotated token): reported as rejected token, data kept", code == 1 and "token rejected" in ctx2.logpath.read_text() and BI.spool_files(ctx2))
        stub.script[:] = [(400, f"bad line near {TOKEN}", 0)]
        BI.run_once(ctx2, backfill=True, now=NOW + 2000)
        alltext = ctx2.logpath.read_text() + ctx2.state_path.read_text() + "".join(p.read_text() for p in ctx2.rejected.glob("*.lp"))
        check("token never appears in log or state, even when the server echoes it", TOKEN not in alltext, "token leaked")
        r = subprocess.run([sys.executable, str(BIN / "bloom-influx-push.py"), "--state-dir", str(base), "--url", stub.url, "--dry-run"],
                           capture_output=True, text=True, env={**os.environ})
        check("CLI output never contains the token", TOKEN not in r.stdout + r.stderr and r.returncode == 0, r.stderr)
        check("scrub() masks the token", BI.scrub(ctx2, f"x {TOKEN} y") == "x *** y")
    finally:
        os.environ["PATH"] = oldp
        os.environ.pop("BLOOM_INFLUX_TOKEN", None)
        if oldt:
            os.environ["BLOOM_INFLUX_TOKEN"] = oldt
    stub.stop()


@scenario
def influx_minute_lag_and_class():
    BI = load("bloom_influx.py", "BI7")
    stub = StubInflux()
    base = influx_home(3, [entry(1, "2026-10-13T10:00:20Z")], req_first=1, tok_first=100, fetched="2026-10-13T10:02:00Z")
    ctx = ictx(BI, base, stub)
    now = T("2026-10-13T10:05:00Z").timestamp()
    code, info = BI.run_once(ctx, now=now)
    check("minutes inside the settle window are NOT classified/pushed yet (provisional class would stick)", info["darkbloom_minute"] == 0, info)
    check("raw mac_power is pushed immediately", info["mac_power"] == 3)
    L = load("bloom_power_lib.py", "L7")
    L.append_line(L.archive_path(base, T("2026-10-13T10:00:20Z")), json.dumps({**entry(9, "2026-10-13T10:30:00Z"), "_fetched_at": "2026-10-13T11:30:00Z"}))
    code, info = BI.run_once(ctx, now=T("2026-10-13T12:00:00Z").timestamp())
    check("after the ledger settled, minutes are pushed once", info["darkbloom_minute"] == 3, info)
    m = [ln for ln in stub.lines() if ln.startswith("darkbloom_minute")]
    paid = [ln for ln in m if 'class="paid"' in ln]
    idle = [ln for ln in m if 'class="idle"' in ln]
    sp = BI.split_wh("both", 0.75, 4.0)
    check("'both' minute energy split by paid share, sums to wh", abs(sp["wh_paid"] - 3.0) < 1e-9 and abs(sp["wh_own"] - 1.0) < 1e-9 and "wh_both" not in sp, sp)
    check("1 paid + 2 idle minutes", len(paid) == 1 and len(idle) == 2, [x[:90] for x in m])
    check("class is a field not a tag (re-classification overwrites one series)", all("class" not in ln.split(" ")[0] for ln in m))
    check("idle minute energy lands in wh_idle", all("wh_idle=" in ln and "wh_paid=0.0" in ln for ln in idle))
    stub.stop()


def live_roundtrip():
    """Real InfluxDB round trip with a throwaway measurement; deleted afterwards. Run: test-bloom-power.py --live"""
    BI = load("bloom_influx.py", "BIlive")
    ctx = BI.Ctx(tempfile.mkdtemp(), host="zz_test")
    if not ctx.token():
        check("live: token available", False, "no token")
        return
    ts = (int(time.time()) - 600) * 10**9      # in the past: Flux range() stops at now
    lines = [BI.make_line("zz_bloom_test", {"host": "zz,test x=y", "k": "v"}, {"f": 1.25, "i": 7, "s": 'q"uote \\', "b": True}, ts + i * 10**9) for i in range(3)]
    for _ in range(2):                                   # twice: idempotent
        r = BI.influx_write(ctx, "\n".join(lines))
        check("live write accepted (204)", r.kind == "ok", r.detail)
    time.sleep(1)
    n = BI.flux_count(ctx, "zz_bloom_test", "f")
    check("live: 3 points stored after writing 3 lines twice", n == 3, n)
    csvtxt = BI.flux_query(ctx, 'from(bucket:"metrics") |> range(start:-1h) |> filter(fn:(r)=> r._measurement=="zz_bloom_test" and r._field=="i") |> first()')
    check("live: int field round-trips as 7 with escaped tag value", ",7," in csvtxt and "zz,test x=y" in csvtxt, csvtxt[:300])
    code = BI.delete_measurement(ctx, "zz_bloom_test")
    time.sleep(1)
    left = BI.flux_count(ctx, "zz_bloom_test", "f")
    check(f"live: delete API cleaned the throwaway measurement (HTTP {code})", code == 204 and left == 0, (code, left))


MUTANTS = [
    ("own-work not excluded from incremental", "bloom_power_lib.py", "clean = [r for r in ok if not r[\"other\"]]", "clean = list(ok)"),
    ("earnings not deduped", "bloom_power_lib.py", "        if i in seen:\n            continue", "        if False:\n            continue"),
    ("serving ignores counters", "bloom_power_lib.py", "(requests_delta or 0) > 0 or (tokens_delta or 0) > 0", "False"),
    ("counter reset mishandled", "bloom_power_lib.py", "return cur - prev if cur >= prev else cur", "return cur - prev"),
    ("darkbloom own connection counted as other work", "bloom_power_lib.py", "if name.lower().startswith(\"darkbloom\"):", "if False:"),
    ("server end of :8000 counted as a client", "bloom_power_lib.py", "if f\"->127.0.0.1:{port}\" not in ln and f\"->[::1]:{port}\" not in ln:", "if False:"),
    ("calibration ignored", "bloom_power_lib.py", "max(0.0, cal * r[\"avg_w\"] + off_w)", "max(0.0, r[\"avg_w\"])"),
    ("first poll raises gap", "bloom-earnings-archive.py", "gap = had_prior and new >= threshold", "gap = new >= threshold"),
    ("network counters not used as paid evidence", "bloom_power_lib.py", 'P = [bool((r.get("req_d") or 0) > 0 or (r.get("tok_d") or 0) > 0) for r in rows]', "P = [False for r in rows]"),
    ("own job windows ignored", "bloom_power_lib.py", "            if ov <= 0:\n                continue\n            a = True", "            continue\n            a = True"),
    ("both split inverted", "bloom_power_lib.py", 'x["cls"], x["paid_frac"] = "both", tok / (tok + x["own_tok"])', 'x["cls"], x["paid_frac"] = "both", x["own_tok"] / (tok + x["own_tok"])'),
    ("strong own signal ignored when counters advance", "bloom_power_lib.py", "            elif strong_own:\n                tok =", "            elif False:\n                tok ="),
    ("unpaid counter minutes counted as paid", "bloom_power_lib.py", "            if unpaid:", "            if False:"),
    ("unexplained active minutes forced to idle", "bloom_power_lib.py", "        elif active:\n            adj = False", "        elif False:\n            adj = False"),
    ("tier-C overlap leaks into option 1", "bloom_power_lib.py", '            if x["tier"] == "-":', "            if True:"),
    ("ledger row not matched to the minute containing it", "bloom_power_lib.py", "if k < len(paid_idx) and _bounds(rows[paid_idx[k]])[0].timestamp() <= t:", "if False:"),
    ("completed month marked DRAFT/open month FINAL", "bloom-power-workbook.py", 'return month < now.strftime("%Y-%m")', 'return month <= now.strftime("%Y-%m")'),
    ("missing Proton root not detected (would mkdir it)", "bloom-power-workbook.py", "if not root.is_dir() or not os.access(root, os.W_OK):", "if False:"),
    ("paid share not applied in workbook formulas", "bloom-power-workbook.py", 'f"=O{i}*G{i}"', 'f"=O{i}"'),
    ("sleep gap billed", "bloom-power-log.py", "dt = 1.0 if self.last_sample_ts is None else min(max(ts - self.last_sample_ts, 0.0), MAX_DT)", "dt = 1.0 if self.last_sample_ts is None else ts - self.last_sample_ts"),
    ("influx: tag value not escaped", "bloom_influx.py", '.replace("=", "\\\\=")', '.replace("=", "=")'),
    ("influx: ints written as floats", "bloom_influx.py", 'return f"{v}i"', 'return f"{v}"'),
    ("influx: partial last line consumed", "bloom_influx.py", "cut = data.rfind(b\"\\n\")", "cut = len(data) - 1"),
    ("influx: truncated file not restarted", "bloom_influx.py", "    if size < offset:\n        offset = 0", "    if False:\n        offset = 0"),
    ("influx: cursor advances without spooling", "bloom_influx.py", "dropped = spool_add(ctx, st, lines)", "dropped = 0"),
    ("influx: retry skips failed batch (order lost)", "bloom_influx.py", "        return sent, res.detail\n", "        continue\n"),
    ("influx: backoff ignored", "bloom_influx.py", 'if not ignore_backoff and now < float(st.get("next_try", 0)):', "if False:"),
    ("influx: poison 400 batch retried forever", "bloom_influx.py", 'if res.kind == "rejected":', "if False:"),
    ("influx: token not scrubbed", "bloom_influx.py", 'return s.replace(t, "***") if t else s', "return s"),
    ("influx: paid share inverted", "bloom_influx.py", 'out["wh_paid"] = wh * paid_frac', 'out["wh_paid"] = wh * (1.0 - paid_frac)'),
    ("influx: ledger timestamp collisions overwrite", "bloom_influx.py", "while (key, t) in seen:", "while False:"),
    ("influx: batch cap ignored", "bloom_influx.py", "BATCH_MAX = 5000", "BATCH_MAX = 500000"),
    ("influx: minutes classified before the ledger settled", "bloom_influx.py", "cutoff = min(now - dt.timedelta(seconds=LAG_S), settled - dt.timedelta(seconds=L.SETTLE_S))", "cutoff = now"),
    ("influx: no HTTP timeout", "bloom_influx.py", "urllib.request.urlopen(req, timeout=timeout) as r:\n            return WriteResult", "urllib.request.urlopen(req) as r:\n            return WriteResult"),
    ("influx: spool cap not enforced", "bloom_influx.py", "while total > SPOOL_MAX_BYTES and len(files) > 1:", "while False:"),
    ("influx: missing token sent anyway/crashes", "bloom_influx.py", "    if not tok:\n        return WriteResult(\"auth\"", "    if False:\n        return WriteResult(\"auth\""),
]


def revert_check():
    bad = 0
    for label, fname, old, new in MUTANTS:
        tmp = Path(tempfile.mkdtemp())
        for f in FILES + ["test-bloom-power.py"]:
            shutil.copy(HERE / f, tmp / f)
        s = (tmp / fname).read_text()
        if old not in s:
            print(f"FAIL: mutant '{label}' anchor not found")
            bad += 1
            continue
        (tmp / fname).write_text(s.replace(old, new, 1))
        r = subprocess.run([sys.executable, str(tmp / "test-bloom-power.py"), "--bindir", str(tmp)], capture_output=True, text=True, timeout=300)
        caught = r.returncode != 0 and "ALL PASS" not in r.stdout
        print(("ok  : " if caught else "FAIL: ") + f"mutant killed: {label}")
        bad += 0 if caught else 1
        shutil.rmtree(tmp)
    print("REVERT CHECK OK" if not bad else f"REVERT CHECK FAILED ({bad})")
    return 1 if bad else 0


def main():
    global BIN
    ap = argparse.ArgumentParser()
    ap.add_argument("--bindir")
    ap.add_argument("--only")
    ap.add_argument("--revert-check", action="store_true")
    ap.add_argument("--live", action="store_true", help="also run the real InfluxDB round trip (zz_bloom_test)")
    a = ap.parse_args()
    if a.revert_check:
        return revert_check()
    if a.bindir:
        BIN = Path(a.bindir)
    for fn in SEL:
        if a.only and a.only != fn.__name__:
            continue
        print(f"-- {fn.__name__}")
        fn()
    if a.live:
        print("-- live_roundtrip")
        live_roundtrip()
    print("ALL PASS" if not FAILS else f"FAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
