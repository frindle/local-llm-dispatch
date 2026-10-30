#!/usr/bin/env python3
"""Unraid + Ollama + queue + GPU + SearXNG -> InfluxDB v2 (stdlib only), the "Tier A" pusher.

One short-lived pass per launchd tick (com.penn.unraid-influx, StartInterval 30). Every source is
fetched in isolation (own thread, own try/except, own timeout) so one dead source never drops the others;
an unreachable Unraid just yields no unraid_* points and exits 0 quietly.

Sources (read-only) and measurements (bucket `metrics`, org `home`, ns timestamps; every measurement carries
tag source=unraid unless noted). Fields have ONE fixed type each (floats are always floats, counts always
ints) because InfluxDB rejects a batch whose field type changes.
  unraid_fast  (30s)  Unraid GraphQL metrics+UPS:
      unraid_host   host=storagedemon  cpu_pct mem_avail_gb mem_total_gb mem_pct swap_used_gb cpu_pkg_temp_c
                                        ups_load_pct ups_watts ups_batt_pct ups_runtime_s   (GiB; mem_pct = used by AVAILABLE)
      unraid_net    host,iface in {br0,docker0,shim-br0}  rx_bytes tx_bytes rx_bps tx_bps  (bps = bits/s; API rxSec is bytes/s)
  unraid_slow  (60s)  Unraid GraphQL array/disks/docker/vms/notifications:
      unraid_disk   host,name,role(data|cache|parity)  temp_c spinning fs_size_kb fs_used_kb fs_free_kb errors smart_ok serial(str)
      unraid_array  host   state(str) started parity_running parity_progress parity_errors notif_unread_{info,warning,alert,total}
      unraid_container host,name   state(str) running healthy(only when the container has a healthcheck)
      unraid_vm     host,name      state(str) running
  ollama_ps (30s)  Ollama /api/ps:   ollama_loaded host=unraid,model  size_bytes size_vram_bytes context expires_in_s ;
                                       ollama_status host=unraid models_loaded vram_bytes
  queue_lanes (30s) Mac queue API /api/queue-wait:  ollama_queue_lane lane  busy pending running idle_s
  cpu_lane (30s)    Mac queue API /api/cpu-lane:    ollama_cpu_lane source=queue  queue_depth running done_total failed_infra last_duration_s
  gpu (30s)         ssh claude-sandbox nvidia-smi:   unraid_gpu host,gpu=3080  util_pct mem_used_mb mem_total_mb temp_c power_w sm_clock_mhz
  websearch (300s)  Mac queue API /api/web-search-usage:  ollama_websearch provider  today total last7d
  searxng (300s)    SearXNG JSON probe:               unraid_searxng host=searxng  engines_ok engines_blocked results latency_ms ok
Slow sources use a per-source slot cursor (floor(now/interval)); a source runs at most once per slot, and its
point timestamps are the slot start, so a re-run/replay overwrites the same point (idempotent).
Delivery: new lines are written to a bounded on-disk spool BEFORE the flush; flush is in order, with backoff.
Secrets: Influx token = Keychain service `influxdb-home`; Unraid API key = ~/.claude.json mcpServers.unraid.env
(or UNRAID_INFLUX_API_KEY). Neither is ever printed, logged, or written to state; all error text is scrubbed."""
import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import fcntl
import json
import math
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

INFLUX_URL = "http://10.0.6.61:8086"
INFLUX_ORG = "home"
INFLUX_BUCKET = "metrics"
KEYCHAIN_SERVICE = "influxdb-home"
UNRAID_HOST = "storagedemon"
SOURCE_TAG = "unraid"
OLLAMA_URL = "http://10.0.7.143:11434"
QUEUE_URL = "http://127.0.0.1:7684"
SEARX_URL = "http://10.0.12.41:8080"
GPU_SSH = "claude-sandbox"
HTTP_TIMEOUT = 10.0
GPU_TIMEOUT = 10.0
NET_IFACES = ("br0", "docker0", "shim-br0")
BATCH_MAX = 5000
SPOOL_MAX_BYTES = 16 * 1024 * 1024
REJECT_KEEP = 20
LOG_MAX_BYTES = 256 * 1024
BACKOFF_BASE_S = 30.0
BACKOFF_MAX_S = 600.0
GIB = 2 ** 30
STATE_DIR = Path.home() / ".unraid-influx"

GQL_FAST = """{ metrics { cpu { percentTotal }
  memory { total available swapUsed }
  temperature { sensors { name type current { value } } }
  network { name bytesReceived bytesSent rxSec txSec } }
  upsDevices { name status battery { chargeLevel estimatedRuntime } power { loadPercentage currentPower } } }"""
GQL_SLOW = """{ array { state parityCheckStatus { running progress errors }
  disks { name type device temp status fsSize fsFree fsUsed numErrors isSpinning }
  caches { name type device temp status fsSize fsFree fsUsed numErrors isSpinning }
  parities { name type device temp status numErrors isSpinning } }
  disks { serialNum device smartStatus temperature isSpinning }
  docker { containers { names state status } }
  vms { domains { name state } }
  notifications { overview { unread { info warning alert total } } } }"""


# ------------------------------------------------------------ line protocol
def esc_name(s):
    return str(s).replace("\\", "/").replace("\n", " ").replace("\r", " ").replace(",", "\\,").replace(" ", "\\ ")


def esc_tag(s):
    return (str(s).replace("\\", "/").replace("\n", " ").replace("\r", " ")
            .replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ "))


def esc_str(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")


def fmt_field(v):
    """Line-protocol field value, or None when it cannot be represented (None, NaN, inf)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "t" if v else "f"
    if isinstance(v, int):
        return f"{v}i"
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return repr(v)
    return '"' + esc_str(v) + '"'


def make_line(measurement, tags, fields, ts_ns):
    """One line, or None if there is no usable field. Tags sorted; empty tag values dropped."""
    fs = []
    for k in sorted(fields):
        f = fmt_field(fields[k])
        if f is not None:
            fs.append(f"{esc_tag(k)}={f}")
    if not fs:
        return None
    ts = [f"{esc_tag(k)}={esc_tag(v)}" for k, v in sorted(tags.items()) if v not in (None, "")]
    head = esc_name(measurement) + ("," + ",".join(ts) if ts else "")
    return f"{head} {','.join(fs)} {int(ts_ns)}"


def F(v):
    """float or None (fixed float field type)."""
    try:
        if v is None or v == "" or isinstance(v, bool):
            return None
        x = float(v)
        return None if math.isnan(x) or math.isinf(x) else x
    except (TypeError, ValueError):
        return None


def I(v):
    """int or None (fixed integer field type)."""
    x = F(v)
    return None if x is None else int(x)


def slot_ns(now, interval):
    """Start of the interval slot containing `now`, in ns (integer maths: identical for any run in the slot)."""
    return int(now // interval) * int(interval) * 1_000_000_000


def emit(out, measurement, tags, fields, ts):
    ln = make_line(measurement, tags, fields, ts)
    if ln:
        out.append(ln)


# ------------------------------------------------------------ builders (pure: raw response -> lines)
def build_unraid_fast(data, now):
    """data = GraphQL `data` dict. Each block is isolated: a malformed UPS never drops host/net."""
    ts = slot_ns(now, 30)
    out = []
    data = data or {}
    base = {"host": UNRAID_HOST, "source": SOURCE_TAG}
    host_f = {}
    try:
        m = data.get("metrics") or {}
        host_f["cpu_pct"] = F((m.get("cpu") or {}).get("percentTotal"))
        mem = m.get("memory") or {}
        total, avail = F(mem.get("total")), F(mem.get("available"))
        if total and avail is not None:
            host_f["mem_total_gb"] = total / GIB
            host_f["mem_avail_gb"] = avail / GIB
            host_f["mem_pct"] = 100.0 * (total - avail) / total
        su = F(mem.get("swapUsed"))
        if su is not None:
            host_f["swap_used_gb"] = su / GIB
        for s in ((m.get("temperature") or {}).get("sensors") or []):
            if s.get("type") == "CPU_PACKAGE":
                host_f["cpu_pkg_temp_c"] = F((s.get("current") or {}).get("value"))
                break
    except Exception:
        pass
    try:
        u = (data.get("upsDevices") or [None])[0] or {}
        host_f["ups_load_pct"] = F((u.get("power") or {}).get("loadPercentage"))
        host_f["ups_watts"] = F((u.get("power") or {}).get("currentPower"))
        host_f["ups_batt_pct"] = F((u.get("battery") or {}).get("chargeLevel"))
        host_f["ups_runtime_s"] = F((u.get("battery") or {}).get("estimatedRuntime"))
    except Exception:
        pass
    emit(out, "unraid_host", base, host_f, ts)
    try:
        for n in ((data.get("metrics") or {}).get("network") or []):
            if n.get("name") in NET_IFACES:
                rx, tx = F(n.get("rxSec")), F(n.get("txSec"))
                emit(out, "unraid_net", dict(base, iface=n["name"]),
                     {"rx_bytes": I(n.get("bytesReceived")), "tx_bytes": I(n.get("bytesSent")),
                      "rx_bps": None if rx is None else rx * 8.0, "tx_bps": None if tx is None else tx * 8.0}, ts)
    except Exception:
        pass
    return out


def build_unraid_slow(data, now):
    ts = slot_ns(now, 60)
    out = []
    data = data or {}
    base = {"host": UNRAID_HOST, "source": SOURCE_TAG}
    arr = data.get("array") or {}
    try:
        phys = {}
        for d in (data.get("disks") or []):
            dev = str(d.get("device") or "").replace("/dev/", "")
            if dev:
                phys[dev] = d
        for grp in ("parities", "disks", "caches"):
            for d in (arr.get(grp) or []):
                try:
                    p = phys.get(str(d.get("device") or ""), {})
                    smart = p.get("smartStatus")
                    spin = d.get("isSpinning")
                    if spin is None:
                        spin = p.get("isSpinning")
                    temp = d.get("temp")
                    if temp is None:
                        temp = p.get("temperature")
                    emit(out, "unraid_disk",
                         dict(base, name=d.get("name"), role=str(d.get("type") or "unknown").lower()),
                         {"temp_c": F(temp), "spinning": None if spin is None else int(bool(spin)),
                          "fs_size_kb": I(d.get("fsSize")), "fs_used_kb": I(d.get("fsUsed")),
                          "fs_free_kb": I(d.get("fsFree")), "errors": I(d.get("numErrors")),
                          "smart_ok": None if smart is None else int(smart == "OK"),
                          "serial": p.get("serialNum") or None}, ts)
                except Exception:
                    pass
        pc = arr.get("parityCheckStatus") or {}
        un = ((data.get("notifications") or {}).get("overview") or {}).get("unread") or {}
        state = arr.get("state")
        emit(out, "unraid_array", base,
             {"state": state, "started": None if state is None else int(state == "STARTED"),
              "parity_running": int(bool(pc.get("running"))) if arr else None,
              "parity_progress": I(pc.get("progress")), "parity_errors": I(pc.get("errors")),
              "notif_unread_info": I(un.get("info")), "notif_unread_warning": I(un.get("warning")),
              "notif_unread_alert": I(un.get("alert")), "notif_unread_total": I(un.get("total"))}, ts)
    except Exception:
        pass
    try:
        for c in ((data.get("docker") or {}).get("containers") or []):
            names = c.get("names") or []
            name = str(names[0] if isinstance(names, list) and names else names or "").lstrip("/")
            if not name:
                continue
            status = str(c.get("status") or "")
            f = {"state": str(c.get("state") or "").lower() or None, "running": int(c.get("state") == "RUNNING")}
            if "unhealthy" in status:
                f["healthy"] = 0
            elif "healthy" in status:
                f["healthy"] = 1
            emit(out, "unraid_container", dict(base, name=name), f, ts)
    except Exception:
        pass
    try:
        for v in ((data.get("vms") or {}).get("domains") or []):
            if v.get("name"):
                emit(out, "unraid_vm", dict(base, name=v["name"]),
                     {"state": str(v.get("state") or "").lower() or None, "running": int(v.get("state") == "RUNNING")}, ts)
    except Exception:
        pass
    return out


def parse_ts(v):
    """RFC3339 (ns fraction, Z) -> epoch seconds, or None."""
    import re
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)$", str(v or ""))
    if not m:
        return None
    frac = float(m.group(2) or 0)
    base = dt.datetime.fromisoformat(m.group(1) + ("+00:00" if m.group(3) == "Z" else m.group(3)))
    return base.timestamp() + frac


def build_ollama_ps(data, now):
    ts = slot_ns(now, 30)
    out = []
    models = (data or {}).get("models") or []
    tot = 0
    for m in models:
        name = m.get("model") or m.get("name")
        if not name:
            continue
        tot += I(m.get("size_vram")) or 0
        exp = None
        try:
            e = parse_ts(m.get("expires_at"))
            exp = None if e is None else e - now
        except (ValueError, TypeError):
            pass
        emit(out, "ollama_loaded", {"host": "unraid", "model": name, "source": SOURCE_TAG},
             {"size_bytes": I(m.get("size")), "size_vram_bytes": I(m.get("size_vram")),
              "context": I(m.get("context_length")), "expires_in_s": None if exp is None else float(exp)}, ts)
    emit(out, "ollama_status", {"host": "unraid", "source": SOURCE_TAG}, {"models_loaded": len(models), "vram_bytes": tot}, ts)
    return out


def build_queue_lanes(data, now):
    ts = slot_ns(now, 30)
    out = []
    for lane, d in sorted(((data or {}).get("lanes") or {}).items()):
        d = d or {}
        idle = d.get("idle_since")
        emit(out, "ollama_queue_lane", {"lane": lane, "source": SOURCE_TAG},
             {"busy": int(d.get("state") == "busy"), "pending": I(d.get("pending")) or 0,
              "running": len(d.get("running") or []),
              "idle_s": None if idle is None or d.get("state") == "busy" else max(0.0, float(now - float(idle)))}, ts)
    return out


def build_cpu_lane(data, now):
    ts = slot_ns(now, 30)
    data = data or {}
    cnt = data.get("counts") or {}
    recent = [r for r in (data.get("recent") or []) if r.get("duration_s") is not None]
    last = max(recent, key=lambda r: F(r.get("finished_at")) or 0.0) if recent else {}
    out = []
    emit(out, "ollama_cpu_lane", {"source": "queue"},
         {"queue_depth": I(data.get("queue_depth")) or 0, "running": len(data.get("running") or []),
          "done_total": I(cnt.get("done")) or 0, "failed_infra": I(cnt.get("failed_infra")) or 0,
          "enabled": None if data.get("enabled") is None else int(bool(data.get("enabled"))),
          "last_duration_s": F(last.get("duration_s"))}, ts)
    return out


def build_gpu(csvtext, now):
    ts = slot_ns(now, 30)
    out = []
    rows = [r for r in csv.reader((csvtext or "").strip().splitlines()) if r]
    if not rows:
        return out
    r = [x.strip() for x in rows[0]]
    if len(r) < 6:
        return out
    emit(out, "unraid_gpu", {"host": UNRAID_HOST, "gpu": "3080", "source": SOURCE_TAG},
         {"util_pct": F(r[0]), "mem_used_mb": F(r[1]), "mem_total_mb": F(r[2]), "temp_c": F(r[3]),
          "power_w": F(r[4]), "sm_clock_mhz": F(r[5])}, ts)
    return out


def build_websearch(data, now):
    ts = slot_ns(now, 300)
    data = data or {}
    out = []
    for prov in sorted(set((data.get("total") or {})) | set((data.get("today") or {}))):
        emit(out, "ollama_websearch", {"provider": prov, "source": SOURCE_TAG},
             {"today": I((data.get("today") or {}).get(prov)) or 0, "total": I((data.get("total") or {}).get(prov)) or 0,
              "last7d": I((data.get("last7d") or {}).get(prov)) or 0}, ts)
    return out


def build_searxng(probe, now):
    """probe = {"json": <SearXNG response>, "latency_ms": float}."""
    ts = slot_ns(now, 300)
    d = (probe or {}).get("json") or {}
    res = d.get("results") or []
    out = []
    emit(out, "unraid_searxng", {"host": "searxng", "source": SOURCE_TAG},
         {"engines_ok": len({r.get("engine") for r in res if r.get("engine")}),
          "engines_blocked": len(d.get("unresponsive_engines") or []), "results": len(res),
          "latency_ms": F((probe or {}).get("latency_ms")), "ok": int(len(res) > 0)}, ts)
    return out


# ------------------------------------------------------------ context, secrets, logging
class Ctx:
    def __init__(self, state_dir=None, influx_url=None, token=None, unraid_url=None, unraid_key=None):
        self.dir = Path(state_dir) if state_dir else STATE_DIR
        self.influx_url = (influx_url or os.environ.get("UNRAID_INFLUX_URL") or INFLUX_URL).rstrip("/")
        self.org, self.bucket = INFLUX_ORG, INFLUX_BUCKET
        self._token = token
        self._ukey = unraid_key
        self._uurl = unraid_url
        self.ollama_url = os.environ.get("UNRAID_INFLUX_OLLAMA_URL", OLLAMA_URL).rstrip("/")
        self.queue_url = os.environ.get("UNRAID_INFLUX_QUEUE_URL", QUEUE_URL).rstrip("/")
        self.searx_url = os.environ.get("UNRAID_INFLUX_SEARX_URL", SEARX_URL).rstrip("/")
        self.gpu_cmd = None          # test hook: replaces the ssh command (list)

    state_path = property(lambda s: s.dir / "state.json")
    spool = property(lambda s: s.dir / "spool")
    rejected = property(lambda s: s.dir / "rejected")
    logpath = property(lambda s: s.dir / "push.log")
    status_path = property(lambda s: s.dir / "status.json")

    def token(self):
        if self._token:
            return self._token
        t = os.environ.get("UNRAID_INFLUX_TOKEN")
        if not t:
            try:
                r = subprocess.run(["security", "find-generic-password", "-w", "-s", KEYCHAIN_SERVICE],
                                   capture_output=True, text=True, timeout=10)
                t = r.stdout.strip() if r.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired):
                t = None
        self._token = t or None
        return self._token

    def _unraid_cfg(self):
        if self._ukey and self._uurl:
            return
        k, u = os.environ.get("UNRAID_INFLUX_API_KEY"), os.environ.get("UNRAID_INFLUX_API_URL")
        if not (k and u):
            try:
                e = json.load(open(Path.home() / ".claude.json"))["mcpServers"]["unraid"]["env"]
                k, u = k or e.get("UNRAID_API_KEY"), u or e.get("UNRAID_API_URL")
            except (OSError, ValueError, KeyError, TypeError):
                pass
        self._ukey, self._uurl = self._ukey or k, self._uurl or u

    def unraid_key(self):
        self._unraid_cfg()
        return self._ukey

    def unraid_url(self):
        self._unraid_cfg()
        return self._uurl


def scrub(ctx, s):
    s = str(s)
    for secret in (ctx._token, ctx._ukey, os.environ.get("UNRAID_INFLUX_TOKEN"), os.environ.get("UNRAID_INFLUX_API_KEY")):
        if secret and len(secret) >= 6:
            s = s.replace(secret, "***")
    return s


def log(ctx, msg):
    line = f"{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {scrub(ctx, msg)}"
    try:
        ctx.dir.mkdir(parents=True, exist_ok=True)
        p = ctx.logpath
        if p.exists() and p.stat().st_size > LOG_MAX_BYTES:
            os.replace(p, str(p) + ".1")
        with open(p, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    return line


def load_state(ctx):
    try:
        d = json.loads(ctx.state_path.read_text())
        if isinstance(d, dict):
            return d
    except (OSError, ValueError):
        pass
    return {}


def save_state(ctx, st):
    ctx.dir.mkdir(parents=True, exist_ok=True)
    tmp = ctx.state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, sort_keys=True))
    os.replace(tmp, ctx.state_path)


# ------------------------------------------------------------ fetchers (impure)
def http_json(url, timeout=HTTP_TIMEOUT, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def gql(ctx, query):
    key, url = ctx.unraid_key(), ctx.unraid_url()
    if not (key and url):
        raise RuntimeError("no Unraid API key/url configured")
    r = http_json(url, data=json.dumps({"query": query}).encode(),
                  headers={"x-api-key": key, "content-type": "application/json"})
    if r.get("data") is None:
        raise RuntimeError("graphql: " + str(r.get("errors"))[:200])
    return r["data"]


def fetch_gpu(ctx):
    cmd = ctx.gpu_cmd or ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", GPU_SSH, "nvidia-smi",
                          "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,clocks.sm",
                          "--format=csv,noheader,nounits"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=GPU_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(f"gpu probe exit {r.returncode}")
    return r.stdout


def fetch_searx(ctx):
    t0 = time.monotonic()
    d = http_json(f"{ctx.searx_url}/search?" + urllib.parse.urlencode({"q": "weather", "format": "json"}))
    return {"json": d, "latency_ms": (time.monotonic() - t0) * 1000.0}


# name, interval_s, fetch(ctx)->raw, build(raw, now)->lines
SOURCES = [
    ("unraid_fast", 30, lambda c: gql(c, GQL_FAST), build_unraid_fast),
    ("unraid_slow", 60, lambda c: gql(c, GQL_SLOW), build_unraid_slow),
    ("ollama_ps", 30, lambda c: http_json(c.ollama_url + "/api/ps"), build_ollama_ps),
    ("queue_lanes", 30, lambda c: http_json(c.queue_url + "/api/queue-wait"), build_queue_lanes),
    ("cpu_lane", 30, lambda c: http_json(c.queue_url + "/api/cpu-lane"), build_cpu_lane),
    ("gpu", 30, fetch_gpu, build_gpu),
    ("websearch", 300, lambda c: http_json(c.queue_url + "/api/web-search-usage"), build_websearch),
    ("searxng", 300, fetch_searx, build_searxng),
]


def run_source(ctx, src, now):
    name, _iv, fetch, build = src
    try:
        return name, build(fetch(ctx), now), None
    except Exception as e:  # noqa: BLE001 - isolation is the point
        return name, None, f"{type(e).__name__}: {scrub(ctx, e)}"[:240]


def due(st, name, interval, now, force=False):
    if force:
        return True
    last = (st.get("last") or {}).get(name)
    return last is None or int(now // interval) > int(float(last) // interval)


def collect(ctx, st, now, force=False, only=None, sources=None):
    """Run every due source concurrently, each isolated. Returns (lines, ok_names, errors{name: msg})."""
    sources = SOURCES if sources is None else sources
    todo = [s for s in sources if (not only or s[0] in only) and due(st, s[0], s[1], now, force)]
    lines, ok, errs = [], [], {}
    if not todo:
        return lines, ok, errs
    with cf.ThreadPoolExecutor(max_workers=len(todo)) as ex:
        futs = [ex.submit(run_source, ctx, s, now) for s in todo]
        for fu in futs:                       # fixed order -> deterministic output
            name, got, err = fu.result()
            if err is None:
                lines += got
                ok.append(name)
            else:
                errs[name] = err
    return lines, ok, errs


# ------------------------------------------------------------ spool + Influx write
def spool_files(ctx):
    return sorted(ctx.spool.glob("*.lp")) if ctx.spool.exists() else []


def spool_add(ctx, st, lines):
    ctx.spool.mkdir(parents=True, exist_ok=True)
    for i in range(0, len(lines), BATCH_MAX):
        st["seq"] = int(st.get("seq", 0)) + 1
        f = ctx.spool / f"{st['seq']:012d}.lp"
        tmp = f.with_suffix(".tmp")
        tmp.write_text("\n".join(lines[i:i + BATCH_MAX]) + "\n")
        os.replace(tmp, f)
    dropped = 0
    files = spool_files(ctx)
    total = sum(f.stat().st_size for f in files)
    while total > SPOOL_MAX_BYTES and len(files) > 1:
        f = files.pop(0)
        total -= f.stat().st_size
        n = f.read_text().count("\n")
        f.unlink()
        dropped += n
        log(ctx, f"ERROR: spool over cap ({SPOOL_MAX_BYTES} bytes): dropped oldest batch {f.name} ({n} points)")
    return dropped


class WriteResult:
    def __init__(self, kind, code=None, detail=""):
        self.kind, self.code, self.detail = kind, code, detail     # ok | rejected | auth | retry


def influx_write(ctx, body, timeout=HTTP_TIMEOUT):
    tok = ctx.token()
    if not tok:
        return WriteResult("auth", None, f"no InfluxDB token (Keychain service {KEYCHAIN_SERVICE!r})")
    q = urllib.parse.urlencode({"org": ctx.org, "bucket": ctx.bucket, "precision": "ns"})
    req = urllib.request.Request(f"{ctx.influx_url}/api/v2/write?{q}", data=body.encode(), method="POST",
                                 headers={"Authorization": f"Token {tok}", "Content-Type": "text/plain; charset=utf-8",
                                          "User-Agent": "unraid-influx/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return WriteResult("ok", r.status)
    except urllib.error.HTTPError as e:
        try:
            msg = e.read().decode("utf-8", "replace")[:200]
        except OSError:
            msg = ""
        if e.code in (401, 403):
            return WriteResult("auth", e.code, f"HTTP {e.code}: token rejected or lacks write on bucket")
        if e.code in (400, 413, 422):
            return WriteResult("rejected", e.code, f"HTTP {e.code}: {scrub(ctx, msg)}")
        return WriteResult("retry", e.code, f"HTTP {e.code}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return WriteResult("retry", None, f"{type(e).__name__}: {scrub(ctx, e)}")


def flush_spool(ctx, st, now=None):
    """Oldest-first; stop at the first failure so order is preserved. Returns (points_sent, error_or_None)."""
    now = time.time() if now is None else now
    files = spool_files(ctx)
    if not files:
        st["fails"], st["next_try"] = 0, 0
        return 0, None
    if now < float(st.get("next_try", 0)):
        return 0, f"backoff {int(float(st['next_try']) - now)}s ({st.get('last_error', '')})"
    sent = 0
    for f in files:
        body = f.read_text()
        res = influx_write(ctx, body.rstrip("\n"))
        n = body.count("\n")
        if res.kind == "ok":
            f.unlink()
            sent += n
            continue
        if res.kind == "rejected":
            ctx.rejected.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(ctx.rejected / f.name))
            for old in sorted(ctx.rejected.glob("*.lp"))[:-REJECT_KEEP]:
                old.unlink()
            log(ctx, f"ERROR: Influx REJECTED batch {f.name} ({n} points): {res.detail}; moved to {ctx.rejected}")
            continue
        st["fails"] = int(st.get("fails", 0)) + 1
        st["next_try"] = now + min(BACKOFF_BASE_S * 2 ** (st["fails"] - 1), BACKOFF_MAX_S)
        st["last_error"] = res.detail
        return sent, res.detail
    st["fails"], st["next_try"], st["last_error"] = 0, 0, ""
    return sent, None


# ------------------------------------------------------------ one pass
def run_once(ctx, now=None, dry_run=False, force=False, only=None, sources=None):
    """Returns (exit_code, summary). Never raises for expected failures; exits 0 when sources are down."""
    wall = time.time() if now is None else now
    if dry_run:
        lines, ok, errs = collect(ctx, {}, wall, True, only, sources)
        return 0, {"lines": lines, "ok": ok, "errors": errs}
    ctx.dir.mkdir(parents=True, exist_ok=True)
    lock = open(ctx.dir / "lock", "w")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0, {"skipped": "another run holds the lock"}
        st = load_state(ctx)
        lines, ok, errs = collect(ctx, st, wall, force, only, sources)
        dropped = spool_add(ctx, st, lines) if lines else 0
        st.setdefault("last", {})
        for n in ok:                          # cursor advances only after the lines are safely spooled
            st["last"][n] = wall
        save_state(ctx, st)
        sent, err = flush_spool(ctx, st, now=wall)
        save_state(ctx, st)
        summ = {"points": len(lines), "ok": ok, "errors": errs, "sent": sent, "spooled": len(spool_files(ctx)),
                "dropped": dropped, "influx_error": err}
        log(ctx, f"{'ok' if not (errs or err) else 'WARN'} points={len(lines)} sent={sent} spool={summ['spooled']} "
                 f"ok={','.join(ok) or '-'}" + (f" failed={json.dumps(errs, sort_keys=True)}" if errs else "")
                 + (f" influx_error={err}" if err else ""))
        status = {"ts": dt.datetime.fromtimestamp(wall, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "unraid_reachable": not any(k.startswith("unraid_") and k != "unraid_searxng" for k in errs),
                  **{k: summ[k] for k in ("points", "ok", "errors", "sent", "spooled", "influx_error")}}
        tmp = ctx.status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, sort_keys=True, indent=1) + "\n")
        os.replace(tmp, ctx.status_path)
        return 0, summ
    finally:
        lock.close()


def flux_count(ctx, measurement, since="-10m"):
    """Row count of one measurement (verification helper)."""
    tok = ctx.token()
    flux = (f'from(bucket:"{ctx.bucket}") |> range(start: {since}) |> filter(fn:(r)=> r._measurement=="{measurement}") '
            f'|> group() |> count()')
    req = urllib.request.Request(f"{ctx.influx_url}/api/v2/query?org={urllib.parse.quote(ctx.org)}",
                                 data=json.dumps({"query": flux, "type": "flux"}).encode(), method="POST",
                                 headers={"Authorization": f"Token {tok}", "Content-Type": "application/json",
                                          "Accept": "application/csv"})
    with urllib.request.urlopen(req, timeout=30) as r:
        txt = r.read().decode()
    col = None
    for rec in csv.reader(txt.splitlines()):
        if "_value" in rec:
            col = rec.index("_value")
        elif col is not None and len(rec) > col and rec[col].strip().isdigit():
            return int(rec[col])
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--once", action="store_true", help="one pass (the default; accepted for clarity)")
    ap.add_argument("--dry-run", action="store_true", help="print line protocol, write nothing, touch no state")
    ap.add_argument("--force", action="store_true", help="ignore the cadence cursor, run every source now")
    ap.add_argument("--only", help="comma-separated source names")
    ap.add_argument("--count", help="print row counts for the given comma-separated measurements and exit")
    ap.add_argument("--state-dir")
    a = ap.parse_args(argv)
    ctx = Ctx(state_dir=a.state_dir)
    if a.count:
        for m in a.count.split(","):
            print(f"{m} {flux_count(ctx, m)}")
        return 0
    only = set(a.only.split(",")) if a.only else None
    rc, summ = run_once(ctx, dry_run=a.dry_run, force=a.force, only=only)
    if a.dry_run:
        for ln in summ["lines"]:
            print(ln)
        for k, v in summ["errors"].items():
            print(f"# source {k} failed: {v}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
