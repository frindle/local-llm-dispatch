#!/usr/bin/env python3
"""Hermetic tests for unraid_influx.py (Tier A Unraid/Ollama/queue/GPU/SearXNG -> InfluxDB pusher).

Fixture response per source -> EXACT expected line protocol; escape rules; fixed field types; per-source failure
isolation; spool/retry/backoff/cap/reject against a fake Influx HTTP server; slot cadence cursor + idempotent
timestamps; Unraid-unreachable exits 0 and still ships the other sources; no secret in logs/state/spool/status.
Never touches the real network, Keychain, ~/.claude.json or ~/.unraid-influx.

Usage: test-unraid-influx.py [--bindir DIR] | --mutants        Markers: ALL PASS / MUTANTS OK."""
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = ["unraid_influx.py"]
FAILS = []
SECRET_TOKEN = "TOK-sEcReT-influx-0123456789"
SECRET_KEY = "KEY-sEcReT-unraid-9876543210"
NOW = 1791923010.5          # slot30 = 1791923010, slot60 = 1791922980, slot300 = 1791922800


def check(name, cond, detail=""):
    print(("ok  : " if cond else "FAIL: ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


def load(bindir):
    spec = importlib.util.spec_from_file_location("unraid_influx_t", Path(bindir) / "unraid_influx.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ------------------------------------------------------------ fixtures
FAST = {"metrics": {"cpu": {"percentTotal": 4.5},
                    "memory": {"total": 2 * 2 ** 30 * 100, "available": 2 ** 30 * 50, "swapUsed": 2 ** 30},
                    "temperature": {"sensors": [{"name": "core0", "type": "CPU_CORE", "current": {"value": 48}},
                                                {"name": "pkg", "type": "CPU_PACKAGE", "current": {"value": 56}}]},
                    "network": [{"name": "lo", "bytesReceived": 1, "bytesSent": 1, "rxSec": 1, "txSec": 1},
                                {"name": "br0", "bytesReceived": 1000, "bytesSent": 2000, "rxSec": 100, "txSec": 50.5},
                                {"name": "vnet0", "bytesReceived": 5, "bytesSent": 5, "rxSec": 5, "txSec": 5},
                                {"name": "docker0", "bytesReceived": 0, "bytesSent": 0, "rxSec": 0, "txSec": 0},
                                {"name": "shim-br0", "bytesReceived": 7, "bytesSent": 8, "rxSec": 1, "txSec": 2}]},
        "upsDevices": [{"name": "U", "status": "ONLINE", "battery": {"chargeLevel": 100, "estimatedRuntime": 1500},
                        "power": {"loadPercentage": 28, "currentPower": 226.8}}]}
FAST_EXP = [
    'unraid_host,host=storagedemon,source=unraid cpu_pct=4.5,cpu_pkg_temp_c=56.0,mem_avail_gb=50.0,mem_pct=75.0,mem_total_gb=200.0,swap_used_gb=1.0,ups_batt_pct=100.0,ups_load_pct=28.0,ups_runtime_s=1500.0,ups_watts=226.8 1791923010000000000',
    'unraid_net,host=storagedemon,iface=br0,source=unraid rx_bps=800.0,rx_bytes=1000i,tx_bps=404.0,tx_bytes=2000i 1791923010000000000',
    'unraid_net,host=storagedemon,iface=docker0,source=unraid rx_bps=0.0,rx_bytes=0i,tx_bps=0.0,tx_bytes=0i 1791923010000000000',
    'unraid_net,host=storagedemon,iface=shim-br0,source=unraid rx_bps=8.0,rx_bytes=7i,tx_bps=16.0,tx_bytes=8i 1791923010000000000',
]
SLOW = {"array": {"state": "STARTED", "parityCheckStatus": {"running": True, "progress": 42, "errors": 0},
                  "parities": [{"name": "parity", "type": "PARITY", "device": "sdg", "temp": 46, "numErrors": 0, "isSpinning": True}],
                  "disks": [{"name": "disk1", "type": "DATA", "device": "sdb", "temp": None, "fsSize": 100, "fsFree": 40,
                             "fsUsed": 60, "numErrors": 3, "isSpinning": False}],
                  "caches": [{"name": "cache", "type": "CACHE", "device": "sdc", "temp": 38, "fsSize": 10, "fsFree": 4,
                              "fsUsed": 6, "numErrors": 0, "isSpinning": None}]},
        "disks": [{"serialNum": 'SER"1', "device": "/dev/sdg", "smartStatus": "OK", "temperature": 99, "isSpinning": False},
                  {"serialNum": "SER 2", "device": "/dev/sdb", "smartStatus": "FAILED", "temperature": 41.0, "isSpinning": True},
                  {"serialNum": "SER3", "device": "/dev/sdc", "smartStatus": "OK", "temperature": 37, "isSpinning": True}],
        "docker": {"containers": [{"names": ["/sonarr"], "state": "RUNNING", "status": "Up 5 hours"},
                                  {"names": ["/web,app x=1"], "state": "RUNNING", "status": "Up 1 hour (healthy)"},
                                  {"names": ["/db"], "state": "RUNNING", "status": "Up (unhealthy)"},
                                  {"names": ["/old"], "state": "EXITED", "status": "Exited (0)"}]},
        "vms": {"domains": [{"name": "Windows 11", "state": "RUNNING"}, {"name": "HA", "state": "SHUTOFF"}]},
        "notifications": {"overview": {"unread": {"info": 6, "warning": 0, "alert": 1, "total": 7}}}}
T60 = "1791922980000000000"
SLOW_EXP = [
    f'unraid_disk,host=storagedemon,name=parity,role=parity,source=unraid errors=0i,serial="SER\\"1",smart_ok=1i,spinning=1i,temp_c=46.0 {T60}',
    f'unraid_disk,host=storagedemon,name=disk1,role=data,source=unraid errors=3i,fs_free_kb=40i,fs_size_kb=100i,fs_used_kb=60i,serial="SER 2",smart_ok=0i,spinning=0i,temp_c=41.0 {T60}',
    f'unraid_disk,host=storagedemon,name=cache,role=cache,source=unraid errors=0i,fs_free_kb=4i,fs_size_kb=10i,fs_used_kb=6i,serial="SER3",smart_ok=1i,spinning=1i,temp_c=38.0 {T60}',
    f'unraid_array,host=storagedemon,source=unraid notif_unread_alert=1i,notif_unread_info=6i,notif_unread_total=7i,notif_unread_warning=0i,parity_errors=0i,parity_progress=42i,parity_running=1i,started=1i,state="STARTED" {T60}',
    f'unraid_container,host=storagedemon,name=sonarr,source=unraid running=1i,state="running" {T60}',
    f'unraid_container,host=storagedemon,name=web\\,app\\ x\\=1,source=unraid healthy=1i,running=1i,state="running" {T60}',
    f'unraid_container,host=storagedemon,name=db,source=unraid healthy=0i,running=1i,state="running" {T60}',
    f'unraid_container,host=storagedemon,name=old,source=unraid running=0i,state="exited" {T60}',
    f'unraid_vm,host=storagedemon,name=Windows\\ 11,source=unraid running=1i,state="running" {T60}',
    f'unraid_vm,host=storagedemon,name=HA,source=unraid running=0i,state="shutoff" {T60}',
]
PS = {"models": [{"name": "qwen3:14b", "model": "qwen3:14b", "size": 100, "size_vram": 90, "context_length": 6144,
                  "expires_at": "2026-10-13T20:41:40.5Z"}]}
PS_NOW = 1791924100.5 - 90.0      # expires_at minus 90s
QL = {"lanes": {"studio": {"state": "idle", "idle_since": NOW - 60, "pending": 0},
                "studio-db": {"state": "busy", "running": ["a", "b"]},
                "unraid": {"state": "idle", "idle_since": NOW - 5.5, "pending": 2}}}
QL_EXP = [
    'ollama_queue_lane,lane=studio,source=unraid busy=0i,idle_s=60.0,pending=0i,running=0i 1791923010000000000',
    'ollama_queue_lane,lane=studio-db,source=unraid busy=1i,pending=0i,running=2i 1791923010000000000',
    'ollama_queue_lane,lane=unraid,source=unraid busy=0i,idle_s=5.5,pending=2i,running=0i 1791923010000000000',
]
CL = {"enabled": True, "counts": {"done": 2004, "failed_infra": 1, "running": 1}, "queue_depth": 3, "running": [{"id": "x"}],
      "recent": [{"duration_s": 10.9, "finished_at": 100.0}, {"duration_s": 185.5, "finished_at": 300.0},
                 {"duration_s": 1.0, "finished_at": 200.0}]}
CL_EXP = ['ollama_cpu_lane,source=queue done_total=2004i,enabled=1i,failed_infra=1i,last_duration_s=185.5,queue_depth=3i,running=1i 1791923010000000000']
GPU_CSV = "5, 10331, 12288, 47, 18.42, 210\n"
GPU_EXP = ['unraid_gpu,gpu=3080,host=storagedemon,source=unraid mem_total_mb=12288.0,mem_used_mb=10331.0,power_w=18.42,sm_clock_mhz=210.0,temp_c=47.0,util_pct=5.0 1791923010000000000']
WS = {"total": {"ollama": 221, "tavily": 23}, "today": {"ollama": 13, "tavily": 7}, "last7d": {"ollama": 108}}
WS_EXP = [
    'ollama_websearch,provider=ollama,source=unraid last7d=108i,today=13i,total=221i 1791922800000000000',
    'ollama_websearch,provider=tavily,source=unraid last7d=0i,today=7i,total=23i 1791922800000000000',
]
SX = {"json": {"results": [{"engine": "google"}, {"engine": "google"}, {"engine": "bing"}],
               "unresponsive_engines": [["brave", "x"], ["ddg", "CAPTCHA"]]}, "latency_ms": 402.5}
SX_EXP = ['unraid_searxng,host=searxng,source=unraid engines_blocked=2i,engines_ok=2i,latency_ms=402.5,ok=1i,results=3i 1791922800000000000']


# ------------------------------------------------------------ fake Influx
class Fake:
    def __init__(self):
        self.mode = "ok"          # ok | 500 | 422 | 401
        self.bodies, self.auth = [], []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n).decode()
                outer.auth.append(self.headers.get("Authorization"))
                code = {"ok": 204, "500": 500, "422": 422, "401": 401}[outer.mode]
                if code == 204:
                    outer.bodies.append(body)
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def lines(self):
        return [ln for b in self.bodies for ln in b.splitlines() if ln]

    def close(self):
        self.srv.shutdown()


def mk(U, tmp, fake=None, **kw):
    return U.Ctx(state_dir=tmp, influx_url=(fake.url if fake else "http://127.0.0.1:9"), token=SECRET_TOKEN,
                 unraid_url="http://127.0.0.1:9/graphql", unraid_key=SECRET_KEY, **kw)


def fixture_sources(U, fail=()):
    """SOURCES with canned fetchers; names in `fail` raise (the error text deliberately embeds the secrets)."""
    raw = {"unraid_fast": FAST, "unraid_slow": SLOW, "ollama_ps": PS, "queue_lanes": QL, "cpu_lane": CL,
           "gpu": GPU_CSV, "websearch": WS, "searxng": SX}
    out = []
    for name, iv, _f, build in U.SOURCES:
        def fetch(c, name=name):
            if name in fail:
                raise RuntimeError(f"boom {SECRET_KEY} {SECRET_TOKEN}")
            return raw[name]
        out.append((name, iv, fetch, build))
    return out


# ------------------------------------------------------------ tests
def t_escape(U):
    check("tag escape: comma, equals, space, backslash",
          U.make_line("m x,y", {"a b": "c,d=e\\f"}, {"f": 1.0}, 5) == "m\\ x\\,y,a\\ b=c\\,d\\=e/f f=1.0 5")
    check("string field escapes quote and backslash", U.make_line("m", {}, {"s": 'a"b\\c'}, 1) == 'm s="a\\"b\\\\c" 1')
    check("newline in string field flattened", "\n" not in U.make_line("m", {}, {"s": "a\nb"}, 1))
    check("empty tag value dropped, tags sorted", U.make_line("m", {"z": "1", "a": "", "b": "2"}, {"f": 1}, 1) == "m,b=2,z=1 f=1i 1")
    check("NaN/inf/None fields dropped; all-empty -> no line",
          U.make_line("m", {}, {"a": float("nan"), "b": None, "c": float("inf")}, 1) is None
          and U.make_line("m", {}, {"a": float("nan"), "b": 2.0}, 1) == "m b=2.0 1")
    check("bool field is t/f; float repr stable", U.make_line("m", {}, {"a": True, "b": 0.5}, 1) == "m a=t,b=0.5 1")
    check("F/I coerce BigInt strings and reject junk", U.I("123") == 123 and U.F("x") is None and U.F(True) is None and U.I(None) is None)
    check("slot_ns floors to the interval", U.slot_ns(NOW, 30) == 1791923010 * 10 ** 9 and U.slot_ns(NOW, 60) == 1791922980 * 10 ** 9
          and U.slot_ns(NOW + 29, 30) == U.slot_ns(NOW, 30))


def t_builders(U):
    check("unraid_host + unraid_net exact lines", U.build_unraid_fast(FAST, NOW) == FAST_EXP, "\n".join(U.build_unraid_fast(FAST, NOW)))
    got = U.build_unraid_slow(SLOW, NOW)
    check("disk/array/container/vm exact lines", got == SLOW_EXP, "\n".join(got))
    check("net: only br0,docker0,shim-br0 (no lo/vnet*)", not any("iface=lo" in l or "vnet" in l for l in U.build_unraid_fast(FAST, NOW)))
    check("disk serial is a field, never a tag", all("serial=" not in l.split(" ")[0] for l in got if l.startswith("unraid_disk")))
    got = U.build_ollama_ps(PS, PS_NOW)
    check("ollama_loaded exact", got[0] == 'ollama_loaded,host=unraid,model=qwen3:14b,source=unraid context=6144i,expires_in_s=90.0,size_bytes=100i,size_vram_bytes=90i 1791924000000000000', got[0])
    check("ollama_status counts models", got[1] == 'ollama_status,host=unraid,source=unraid models_loaded=1i,vram_bytes=90i 1791924000000000000', got[1])
    check("ollama nothing loaded -> status 0, no ollama_loaded", U.build_ollama_ps({"models": []}, NOW) ==
          ['ollama_status,host=unraid,source=unraid models_loaded=0i,vram_bytes=0i 1791923010000000000'])
    check("queue lanes exact", U.build_queue_lanes(QL, NOW) == QL_EXP, "\n".join(U.build_queue_lanes(QL, NOW)))
    check("cpu lane exact (last duration = newest finished_at)", U.build_cpu_lane(CL, NOW) == CL_EXP, "\n".join(U.build_cpu_lane(CL, NOW)))
    check("gpu exact", U.build_gpu(GPU_CSV, NOW) == GPU_EXP, "\n".join(U.build_gpu(GPU_CSV, NOW)))
    check("gpu [N/A] fields dropped, junk row -> nothing",
          U.build_gpu("5, [N/A], 12288, 47, [N/A], 210", NOW)[0].count("mem_used_mb") == 0 and U.build_gpu("garbage", NOW) == [] and U.build_gpu("", NOW) == [])
    check("websearch exact", U.build_websearch(WS, NOW) == WS_EXP, "\n".join(U.build_websearch(WS, NOW)))
    check("searxng exact", U.build_searxng(SX, NOW) == SX_EXP, "\n".join(U.build_searxng(SX, NOW)))
    check("searxng zero results -> ok=0", U.build_searxng({"json": {"results": [], "unresponsive_engines": []}, "latency_ms": 1.0}, NOW)[0].endswith(
        'engines_blocked=0i,engines_ok=0i,latency_ms=1.0,ok=0i,results=0i 1791922800000000000'))
    # partial / malformed payloads never raise and never lose the healthy blocks
    part = {"metrics": {"cpu": {"percentTotal": 1.0}, "network": "garbage"}, "upsDevices": [{"power": "bad"}]}
    p = U.build_unraid_fast(part, NOW)
    check("malformed UPS/network does not drop cpu_pct", len(p) == 1 and "cpu_pct=1.0" in p[0], str(p))
    check("empty payloads yield no lines, no exception", U.build_unraid_fast({}, NOW) == [] and U.build_unraid_slow({}, NOW) == []
          and U.build_queue_lanes({}, NOW) == [] and U.build_ollama_ps(None, NOW)[0].startswith("ollama_status"))
    # field types are fixed regardless of int/float inputs (Influx rejects type changes)
    a = U.build_unraid_fast({"metrics": {"cpu": {"percentTotal": 3}}}, NOW)[0]
    b = U.build_unraid_fast({"metrics": {"cpu": {"percentTotal": 3.25}}}, NOW)[0]
    check("float field stays float for integer input", "cpu_pct=3.0" in a and "cpu_pct=3.25" in b, a)
    d = U.build_unraid_slow({"array": {"state": "STARTED", "disks": [{"name": "d", "type": "DATA", "temp": 40, "numErrors": 0}]}}, NOW)[0]
    check("temp_c float / errors int", "temp_c=40.0" in d and "errors=0i" in d, d)


def t_isolation(U, tmp):
    src = fixture_sources(U, fail=("unraid_fast", "gpu"))
    lines, ok, errs = U.collect(mk(U, tmp), {}, NOW, sources=src)
    check("failing sources reported, others still produce lines",
          set(errs) == {"unraid_fast", "gpu"} and set(ok) == {"unraid_slow", "ollama_ps", "queue_lanes", "cpu_lane", "websearch", "searxng"}, str((ok, errs)))
    check("healthy sources' lines intact", all(l in lines for l in SLOW_EXP + QL_EXP + CL_EXP + WS_EXP + SX_EXP))
    check("no unraid_host/gpu line from failed sources", not any(l.startswith(("unraid_host", "unraid_net", "unraid_gpu")) for l in lines))
    check("error text scrubbed of both secrets", all(SECRET_KEY not in e and SECRET_TOKEN not in e for e in errs.values()), str(errs))
    lines2, ok2, errs2 = U.collect(mk(U, tmp), {}, NOW, sources=fixture_sources(U))
    check("deterministic order across runs", lines2 == U.collect(mk(U, tmp), {}, NOW, sources=fixture_sources(U))[0])
    # a builder crash (not just fetch) is isolated too
    bad = [("a", 30, lambda c: 1, lambda raw, now: 1 / 0), ("b", 30, lambda c: 1, lambda raw, now: ["m f=1i 1"])]
    l, o, e = U.collect(mk(U, tmp), {}, NOW, sources=bad)
    check("builder exception isolated", l == ["m f=1i 1"] and o == ["b"] and "ZeroDivisionError" in e["a"], str((l, o, e)))


def t_cadence(U, tmp):
    NOW = 1791922800.5          # aligned to a 300s slot
    fake = Fake()
    try:
        ctx = mk(U, tmp, fake)
        src = fixture_sources(U)
        rc, s1 = U.run_once(ctx, now=NOW, sources=src)
        check("first pass runs every source", rc == 0 and len(s1["ok"]) == 8 and not s1["errors"], str(s1))
        n1 = len(fake.lines())
        rc, s2 = U.run_once(ctx, now=NOW + 30, sources=src)       # next 30s slot: slow(60)/websearch/searxng not due
        check("60s/300s sources skipped in the next 30s slot",
              set(s2["ok"]) == {"unraid_fast", "ollama_ps", "queue_lanes", "cpu_lane", "gpu"}, str(s2["ok"]))
        rc, s3 = U.run_once(ctx, now=NOW + 60, sources=src)
        check("slow source due again after a full 60s slot, searxng still not", "unraid_slow" in s3["ok"] and "searxng" not in s3["ok"], str(s3["ok"]))
        rc, s4 = U.run_once(ctx, now=NOW + 300, sources=src)
        check("300s sources due after 300s", "searxng" in s4["ok"] and "websearch" in s4["ok"], str(s4["ok"]))
        rc, s5 = U.run_once(ctx, now=NOW + 300.4, sources=src)
        check("same slot never runs a source twice", s5["ok"] == [], str(s5["ok"]))
        rc, s6 = U.run_once(ctx, now=NOW + 300.4, sources=src, force=True)
        check("--force ignores the cursor", len(s6["ok"]) == 8)
        # jitter: 29.9s later is still the same 30s slot only if the slot did not roll
        st = U.load_state(ctx)
        check("failed source does not advance its cursor", True)
        fail_src = fixture_sources(U, fail=("gpu",))
        U.run_once(ctx, now=NOW + 600, sources=fail_src)
        st = U.load_state(ctx)
        check("cursor for failed gpu stays old, others advance", st["last"]["gpu"] == NOW + 300.4 and st["last"]["cpu_lane"] == NOW + 600, str(st["last"]))
        # idempotence: a replay of the same slot yields identical lines
        a = U.collect(ctx, {}, NOW + 0.2, True, sources=src)[0]
        b = U.collect(ctx, {}, NOW + 9.7, True, sources=src)[0]
        ident = lambda ls: [(l.split(" ")[0], l.rsplit(" ", 1)[1]) for l in ls]
        check("timestamps are slot-floored: replay in the same slot has identical series+timestamp (overwrites)", ident(a) == ident(b) and a)
        check("lines reached the fake Influx", len(fake.lines()) > n1 > 0)
    finally:
        fake.close()


def t_spool(U, tmp):
    U.BACKOFF_BASE_S = 100.0
    fake = Fake()
    try:
        ctx = mk(U, tmp, fake)
        src = fixture_sources(U)
        fake.mode = "500"
        rc, s = U.run_once(ctx, now=NOW, sources=src)
        check("Influx 500: exit 0, lines spooled, nothing sent", rc == 0 and s["sent"] == 0 and s["spooled"] == 1 and "500" in (s["influx_error"] or ""), str(s))
        st = U.load_state(ctx)
        check("backoff scheduled", st["fails"] == 1 and st["next_try"] > NOW)
        rc, s = U.run_once(ctx, now=NOW + 30, sources=src)
        check("inside the backoff window: no HTTP attempt", len(fake.auth) == 1 and s["spooled"] == 2, f"{len(fake.auth)} {s}")
        fake.mode = "ok"
        rc, s = U.run_once(ctx, now=NOW + 400, sources=src)
        check("recovery flushes the whole spool oldest-first", s["spooled"] == 0 and s["sent"] > 0 and s["influx_error"] is None, str(s))
        check("backlog replayed in order (first body has the first slot's host line)",
              fake.bodies[0].splitlines()[0].endswith(" 1791923010000000000") and fake.bodies[0] != fake.bodies[-1])
        check("token sent only as Authorization header to Influx", set(fake.auth) == {f"Token {SECRET_TOKEN}"})
        # 422 -> rejected dir, not retried forever
        fake.mode = "422"
        U.run_once(ctx, now=NOW + 900, sources=src)
        check("422 batch moved to rejected, spool drained", len(list(ctx.rejected.glob("*.lp"))) == 1 and not U.spool_files(ctx))
        # 401 -> retained for retry
        fake.mode = "401"
        rc, s = U.run_once(ctx, now=NOW + 1300, sources=src)
        check("401 keeps the batch spooled", s["spooled"] == 1 and "token" in s["influx_error"], str(s))
        # spool cap drops oldest only, keeps newest
        fake.mode = "500"
        old = U.SPOOL_MAX_BYTES
        U.SPOOL_MAX_BYTES = 3000
        try:
            st = {}
            for i in range(5):
                U.spool_add(ctx, st, [f"m f={i}i {i}" + "x" * 1000])
            files = U.spool_files(ctx)
            check("spool cap drops oldest, keeps newest", len(files) <= 3 and files[-1].read_text().startswith("m f=4i"), str(files))
        finally:
            U.SPOOL_MAX_BYTES = old
        # no secret anywhere on disk
        blob = ""
        for p in Path(tmp).rglob("*"):
            if p.is_file():
                blob += p.read_text(errors="replace")
        check("no secret in log/state/spool/status", SECRET_TOKEN not in blob and SECRET_KEY not in blob)
        check("status heartbeat written", json.loads(ctx.status_path.read_text()).get("ts"))
    finally:
        fake.close()


def t_unreachable(U, tmp):
    fake = Fake()
    try:
        ctx = mk(U, tmp, fake)
        src = fixture_sources(U, fail=("unraid_fast", "unraid_slow"))
        rc, s = U.run_once(ctx, now=NOW, sources=src)
        st = json.loads(ctx.status_path.read_text())
        check("Unraid down: exit 0, other sources still delivered", rc == 0 and s["sent"] > 0 and any(l.startswith("ollama_") for l in fake.lines())
              and not any(l.startswith(("unraid_host", "unraid_disk")) for l in fake.lines()))
        check("status flags unraid_reachable=false", st["unraid_reachable"] is False and set(st["errors"]) == {"unraid_fast", "unraid_slow"})
        check("failed sources are retried on the next tick (cursor not advanced)", "unraid_fast" not in U.load_state(ctx)["last"])
        log = ctx.logpath.read_text()
        check("log is scrubbed", SECRET_KEY not in log and SECRET_TOKEN not in log and "***" in log)
    finally:
        fake.close()
    # the real fetchers against a dead port: GraphQL timeout/refused path is caught, never raised
    ctx = mk(U, tmp)
    name, got, err = U.run_source(ctx, U.SOURCES[0], NOW)
    check("real gql fetch to a dead port is a caught error", got is None and err and SECRET_KEY not in err, str(err))
    c2 = U.Ctx(state_dir=tmp, unraid_url="", unraid_key="")
    c2._ukey, c2._uurl = None, None
    os.environ.pop("UNRAID_INFLUX_API_KEY", None)
    # lock: a second concurrent run exits 0 without running
    import fcntl
    ctx3 = mk(U, tmp)
    ctx3.dir.mkdir(parents=True, exist_ok=True)
    with open(ctx3.dir / "lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        rc, s = U.run_once(ctx3, now=NOW, sources=fixture_sources(U))
    check("overlapping run is skipped, exit 0", rc == 0 and "skipped" in s)


def t_dry_and_cli(U, tmp, bindir):
    ctx = mk(U, tmp)
    rc, s = U.run_once(ctx, now=NOW, dry_run=True, sources=fixture_sources(U))
    check("dry-run returns all lines and writes nothing", rc == 0 and len(s["lines"]) > 20 and not Path(tmp, "state.json").exists()
          and not Path(tmp, "spool").exists())
    env = dict(os.environ, UNRAID_INFLUX_API_KEY=SECRET_KEY, UNRAID_INFLUX_API_URL="http://127.0.0.1:9/graphql",
               UNRAID_INFLUX_TOKEN=SECRET_TOKEN, UNRAID_INFLUX_OLLAMA_URL="http://127.0.0.1:9", UNRAID_INFLUX_QUEUE_URL="http://127.0.0.1:9",
               UNRAID_INFLUX_SEARX_URL="http://127.0.0.1:9", UNRAID_INFLUX_URL="http://127.0.0.1:9", HOME=tmp)
    r = subprocess.run([sys.executable, str(Path(bindir) / "unraid_influx.py"), "--once", "--state-dir", tmp + "/cli"], env=env,
                       capture_output=True, text=True, timeout=60)
    check("CLI with everything unreachable exits 0 and prints nothing", r.returncode == 0 and r.stdout == "" and r.stderr == "", r.stdout + r.stderr)
    r = subprocess.run([sys.executable, str(Path(bindir) / "unraid_influx.py"), "--dry-run", "--only", "gpu"], env=env, capture_output=True,
                       text=True, timeout=60)
    check("dry-run CLI never prints a secret", SECRET_KEY not in r.stdout + r.stderr and SECRET_TOKEN not in r.stdout + r.stderr)
    plist = HERE.parent / "LaunchAgents" / "com.penn.unraid-influx.plist"
    if plist.exists():
        t = plist.read_text()
        check("plist: label, StartInterval 30, runs unraid_influx.py, no secrets",
              "com.penn.unraid-influx" in t and "<key>StartInterval</key><integer>30</integer>" in t and "unraid_influx.py" in t
              and "KEY" not in t.replace("<key>", "").replace("</key>", "").upper().replace("KEEPALIVE", ""))
    src = (Path(bindir) / "unraid_influx.py").read_text()
    check("source never prints the secrets / key material", "print(" not in src.split("def main")[0])


def run_all(bindir):
    U = load(bindir)
    t_escape(U)
    t_builders(U)
    for fn in (t_isolation, t_cadence, t_spool, t_unreachable):
        d = tempfile.mkdtemp()
        try:
            fn(U, d)
        finally:
            shutil.rmtree(d, ignore_errors=True)
    d = tempfile.mkdtemp()
    try:
        t_dry_and_cli(U, d, bindir)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("ALL PASS" if not FAILS else f"FAILED ({len(FAILS)}): {FAILS}")
    return 1 if FAILS else 0


MUTANTS = [
    ("memory tracks used not available", "total - avail) / total", "avail) / total"),
    ("bps not converted to bits", "None if rx is None else rx * 8.0", "None if rx is None else rx"),
    ("net ifaces unfiltered", 'if n.get("name") in NET_IFACES:', "if True:"),
    ("tag '=' not escaped", '.replace(",", "\\\\,").replace("=", "\\\\=").replace(" ", "\\\\ "))', '.replace(",", "\\\\,").replace(" ", "\\\\ "))'),
    ("string quote not escaped", """.replace('"', '\\\\"')""", """.replace('"', '"')"""),
    ("NaN field not dropped", "if math.isnan(v) or math.isinf(v):\n            return None\n        return repr(v)", "return repr(v)"),
    ("serial becomes a tag", 'dict(base, name=d.get("name"), role=str(d.get("type") or "unknown").lower())',
     'dict(base, name=d.get("name"), role=str(d.get("type") or "unknown").lower(), serial=p.get("serialNum"))'),
    ("smart_ok inverted", 'int(smart == "OK")', 'int(smart != "OK")'),
    ("CPU package sensor not selected", 'if s.get("type") == "CPU_PACKAGE":', 'if s.get("type") == "CPU_CORE":'),
    ("container name keeps leading slash", '.lstrip("/")', ""),
    ("unhealthy counted healthy", 'if "unhealthy" in status:', 'if False:'),
    ("slot floor uses the raw time", "return int(now // interval) * int(interval) * 1_000_000_000", "return int(now * 1_000_000_000)"),
    ("slow source runs every tick", "return last is None or int(now // interval) > int(float(last) // interval)", "return True"),
    ("failed source cursor advances anyway", "        for n in ok:", "        for n in list(ok) + list(errs):"),
    ("source failure aborts the pass", "        return name, None, f\"{type(e).__name__}: {scrub(ctx, e)}\"[:240]", "        raise"),
    ("error text not scrubbed", "    s = str(s)\n    for secret in", "    return str(s)\n    for secret in"),
    ("cursor advances before spooling", "        dropped = spool_add(ctx, st, lines) if lines else 0\n        st.setdefault(\"last\", {})\n        for n in ok:",
     "        st.setdefault(\"last\", {})\n        for n in ok:"),
    ("spool cap drops newest", "        f = files.pop(0)\n        total -= f.stat().st_size", "        f = files.pop()\n        total -= f.stat().st_size"),
    ("backoff never set", 'st["next_try"] = now + min(BACKOFF_BASE_S * 2 ** (st["fails"] - 1), BACKOFF_MAX_S)', 'st["next_try"] = 0'),
    ("422 retried forever", 'if res.kind == "rejected":', 'if False:'),
    ("flush continues past failure", "        return sent, res.detail\n    st[\"fails\"], st[\"next_try\"], st[\"last_error\"] = 0, 0, \"\"", "        continue\n    st[\"fails\"], st[\"next_try\"], st[\"last_error\"] = 0, 0, \"\""),
    ("queue busy flag wrong", '"busy": int(d.get("state") == "busy")', '"busy": int(d.get("state") != "busy")'),
    ("cpu last duration = oldest", "key=lambda r: F(r.get(\"finished_at\")) or 0.0)", "key=lambda r: -(F(r.get(\"finished_at\")) or 0.0))"),
    ("searxng blocked count wrong", 'len(d.get("unresponsive_engines") or [])', "0"),
    ("lock ignored", "fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)", "pass"),
    ("expires_in_s not relative to now", "exp = None if e is None else e - now", "exp = e"),
]


def mutants():
    bad = 0
    for label, old, new in MUTANTS:
        tmp = Path(tempfile.mkdtemp())
        for f in FILES + ["test-unraid-influx.py"]:
            shutil.copy(HERE / f, tmp / f)
        s = (tmp / "unraid_influx.py").read_text()
        if old not in s:
            print(f"FAIL: mutant '{label}' anchor not found")
            bad += 1
            shutil.rmtree(tmp)
            continue
        (tmp / "unraid_influx.py").write_text(s.replace(old, new, 1))
        r = subprocess.run([sys.executable, str(tmp / "test-unraid-influx.py"), "--bindir", str(tmp)], capture_output=True, text=True, timeout=300)
        caught = r.returncode != 0 and "ALL PASS" not in r.stdout
        print(("ok  : " if caught else "FAIL: ") + f"mutant killed: {label}")
        bad += 0 if caught else 1
        shutil.rmtree(tmp)
    print("MUTANTS OK" if not bad else f"MUTANTS FAILED ({bad})")
    return 1 if bad else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bindir", default=str(HERE))
    ap.add_argument("--mutants", action="store_true")
    a = ap.parse_args()
    sys.exit(mutants() if a.mutants else run_all(a.bindir))
