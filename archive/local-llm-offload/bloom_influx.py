#!/usr/bin/env python3
"""Darkbloom + Mac power -> InfluxDB v2 (stdlib only). Library used by bloom-influx-push.py.

Sources (read-only, never modified): ~/.darkbloom/power/power-log-YYYY-MM.csv (sampler, 1 row/min),
~/.darkbloom/earnings-archive/earnings-YYYY-MM.jsonl + totals.jsonl (earnings archiver).
Measurements (bucket `metrics`, org `home`; all timestamps UTC nanoseconds):
  mac_power          tags host,source=macmon,dbk_state   fields avg_w,max_w,wh,gpu_pct,interval_s,power_ok
  darkbloom_minute   tags host,source=sampler            fields class(str),paid_frac,wh,wh_<class> x6,requests_delta,
                     tokens_delta,other_work,own_tier,own_tokens_est,warm_models
                     (class is a FIELD, not a tag: a minute is re-classified as ledger rows settle, and a changed tag
                     would leave a second series behind; as a field the re-push overwrites the same point)
  darkbloom_earnings tags host,kind=base|job,model       fields usd,micro_usd,prompt_tokens,completion_tokens,ledger_id,job_id
  darkbloom_lifetime tags host,source=account            fields count,total_usd,available_usd,withdrawable_usd,recent_count,new
Idempotency: a point is identified by measurement+tags+timestamp, so re-sending overwrites. Backfills and
spool replays are therefore safe. Earnings ledger ids are NOT tags (cardinality); the entry's own microsecond
timestamp + kind + model is the identity, with a deterministic +1ns bump if two entries collide.
Delivery: new lines are written to a bounded on-disk spool BEFORE the cursors advance; the spool is flushed in
order; failures back off. The token (Keychain `influxdb-home`) is never printed or logged."""
import datetime as dt
import fcntl
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bloom_power_lib as L  # noqa: E402

DEFAULT_URL = "http://10.0.6.61:8086"
DEFAULT_ORG = "home"
DEFAULT_BUCKET = "metrics"
KEYCHAIN_SERVICE = "influxdb-home"
HOST_TAG = "studio"
BATCH_MAX = 5000                    # points per HTTP write
HTTP_TIMEOUT = 8.0
SPOOL_MAX_BYTES = 64 * 1024 * 1024
REJECT_KEEP = 20
LOG_MAX_BYTES = 512 * 1024
LAG_S = 1200.0                      # minute rows are classified only after the ledger had time to settle
BACKOFF_BASE_S = 30.0
BACKOFF_MAX_S = 900.0
EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
CLASSES = L.CLASSES
STATE_DIRNAME = "influx"


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
    """One line, or None if there is no usable field. Tags sorted by key; empty tag values dropped."""
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


def ts_ns(d):
    """datetime -> integer UTC nanoseconds (integer maths: no float rounding)."""
    return (d - EPOCH) // dt.timedelta(microseconds=1) * 1000


def _num(v, kind=float):
    try:
        if v in (None, ""):
            return None
        return kind(float(v)) if kind is int else float(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------ source -> lines
def read_new_bytes(path, offset):
    """Complete lines after `offset`. Returns (text, new_offset); a trailing partial line is left unconsumed.
    A file that shrank (truncated/replaced) restarts from 0: re-sending is harmless."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return "", offset
    if size < offset:
        offset = 0
    if size == offset:
        return "", offset
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read()
    cut = data.rfind(b"\n")
    if cut < 0:
        return "", offset
    return data[:cut + 1].decode("utf-8", "replace"), offset + cut + 1


def csv_header(path):
    try:
        with open(path, "rb") as fh:
            first = fh.readline()
    except OSError:
        return None, 0
    if not first.endswith(b"\n"):
        return None, 0
    import csv as _csv
    cols = next(_csv.reader([first.decode("utf-8", "replace")]), None)
    return cols, len(first)


def mac_power_lines(path, offset, host=HOST_TAG):
    """Lines for CSV rows after `offset` (header skipped). Returns (lines, new_offset, rows_read)."""
    import csv as _csv
    cols, hlen = csv_header(path)
    if cols is None:
        return [], offset, 0
    if offset < hlen:
        offset = hlen
    text, new_off = read_new_bytes(path, offset)
    if new_off < offset:                       # shrank: restart after the header
        text, new_off = read_new_bytes(path, hlen)
    lines, n = [], 0
    for rec in _csv.reader(text.splitlines()):
        if len(rec) != len(cols):
            continue                           # torn row
        r = dict(zip(cols, rec))
        try:
            ts = ts_ns(L.parse_iso(r["ts_utc"]))
        except (KeyError, ValueError):
            continue
        n += 1
        ln = make_line("mac_power", {"host": host, "source": "macmon", "dbk_state": r.get("dbk_state") or "unknown"},
                       {"avg_w": _num(r.get("avg_w")), "max_w": _num(r.get("max_w")), "wh": _num(r.get("wh")),
                        "gpu_pct": _num(r.get("gpu_pct")), "interval_s": _num(r.get("interval_s")),
                        "power_ok": _num(r.get("power_ok"), int)}, ts)
        if ln:
            lines.append(ln)
    return lines, new_off, n


def split_wh(cls, paid_frac, wh):
    """Energy by class for one minute: {wh_<class>: value}. All seven keys are always present."""
    out = {f"wh_{c}": 0.0 for c in CLASSES if c != "both"}      # 'both' is split into paid + own
    if wh is None:
        return {}
    if cls == "both":
        out["wh_paid"] = wh * paid_frac
        out["wh_own"] = wh * (1.0 - paid_frac)
    elif cls in CLASSES:
        out[f"wh_{cls}"] = wh
    return out


def minute_lines(base, month, after_iso, cutoff, host=HOST_TAG, metrics_path=None, queue_path=None):
    """darkbloom_minute lines for sampled minutes of `month` with after < ts <= cutoff. The whole month is
    classified (neighbours and ledger matching need context). Returns (lines, last_ts_iso, n)."""
    rows = L.load_power_rows(base, month)
    if not rows:
        return [], after_iso, 0
    import csv as _csv
    raw = {}
    try:
        with open(Path(base) / L.POWER_DIRNAME / f"power-log-{month}.csv", newline="") as fh:
            for r in _csv.DictReader(fh):
                raw[r.get("ts_utc")] = r
    except OSError:
        pass
    entries = L.load_earnings(base, month)
    paid = L.paid_entries_from(entries)
    windows = L.load_own_windows(month, metrics_path, queue_path)
    settled = L.archive_settled_until(base)
    cls = L.classify_minutes(rows, windows, paid, settled)
    after = L.parse_iso(after_iso) if after_iso else None
    lines, last, n = [], after_iso, 0
    for r, c in zip(rows, cls):
        if (after and r["ts"] <= after) or r["ts"] > cutoff:
            continue
        rr = raw.get(L.utc_iso(r["ts"].timestamp()), {})
        wh = _num(rr.get("wh"))
        f = {"class": c["cls"], "paid_frac": float(c["paid_frac"]), "wh": wh,
             "requests_delta": int(r.get("req_d") or 0), "tokens_delta": int(r.get("tok_d") or 0),
             "other_work": 1 if r.get("other") else 0, "own_tier": c["tier"],
             "own_tokens_est": float(c["own_tok"] or 0.0), "warm_models": rr.get("warm_models") or None}
        f.update(split_wh(c["cls"], c["paid_frac"], wh))
        ln = make_line("darkbloom_minute", {"host": host, "source": "sampler"}, f, ts_ns(r["ts"]))
        if ln:
            lines.append(ln)
            n += 1
            iso = L.utc_iso(r["ts"].timestamp())
            if last is None or iso > last:
                last = iso
    return lines, last, n


def earnings_lines(path, offset, host=HOST_TAG, seen=None):
    """darkbloom_earnings lines for archive rows after `offset`. `seen` ({(series_head, ts): True}) detects
    same-series same-timestamp collisions inside one call and bumps by 1ns deterministically."""
    text, new_off = read_new_bytes(path, offset)
    seen = {} if seen is None else seen
    lines, n = [], 0
    for ln in text.splitlines():
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        ts = L.entry_ts(e)
        if ts is None:
            continue
        kind = L.entry_kind(e)
        model = str(e.get("model") or "unknown")
        t = ts_ns(ts)
        key = (kind, model)
        while (key, t) in seen:
            t += 1
        seen[(key, t)] = True
        usd = L.entry_usd(e)
        out = make_line("darkbloom_earnings", {"host": host, "kind": kind, "model": model},
                        {"usd": float(usd), "micro_usd": _num(e.get("amount_micro_usd"), int),
                         "prompt_tokens": _num(e.get("prompt_tokens"), int),
                         "completion_tokens": _num(e.get("completion_tokens"), int),
                         "ledger_id": str(e.get("_id") or e.get("id") or "") or None,
                         "job_id": e.get("job_id") or None}, t)
        if out:
            lines.append(out)
            n += 1
    return lines, new_off, n


def totals_lines(path, offset, host=HOST_TAG):
    text, new_off = read_new_bytes(path, offset)
    lines = []
    for ln in text.splitlines():
        try:
            d = json.loads(ln)
            ts = ts_ns(L.parse_iso(d["at"]))
        except (ValueError, KeyError, TypeError):
            continue

        def usd(k):
            v = _num(d.get(k))
            return None if v is None else v / 1e6
        out = make_line("darkbloom_lifetime", {"host": host, "source": "account"},
                        {"count": _num(d.get("count"), int), "total_usd": usd("total_micro_usd"),
                         "available_usd": usd("available_balance_micro_usd"),
                         "withdrawable_usd": usd("withdrawable_balance_micro_usd"),
                         "recent_count": _num(d.get("recent_count"), int), "new": _num(d.get("new"), int)}, ts)
        if out:
            lines.append(out)
    return lines, new_off


# ------------------------------------------------------------ state, log, spool
class Ctx:
    def __init__(self, base=None, url=None, org=None, bucket=None, token=None, host=None):
        self.base = Path(base) if base else L.HOME_DIR
        self.dir = self.base / STATE_DIRNAME
        self.url = (url or os.environ.get("BLOOM_INFLUX_URL") or DEFAULT_URL).rstrip("/")
        self.org = org or os.environ.get("BLOOM_INFLUX_ORG") or DEFAULT_ORG
        self.bucket = bucket or os.environ.get("BLOOM_INFLUX_BUCKET") or DEFAULT_BUCKET
        self.host = host or os.environ.get("BLOOM_INFLUX_HOST") or HOST_TAG
        self._token = token
        self.token_checked = token is not None
        self.metrics_path = None
        self.queue_path = None

    @property
    def state_path(self):
        return self.dir / "state.json"

    @property
    def spool(self):
        return self.dir / "spool"

    @property
    def rejected(self):
        return self.dir / "rejected"

    @property
    def logpath(self):
        return self.dir / "push.log"

    def token(self):
        """Token from the explicit value, BLOOM_INFLUX_TOKEN, else Keychain. None if unavailable."""
        if self._token:
            return self._token
        t = os.environ.get("BLOOM_INFLUX_TOKEN")
        if not t:
            try:
                r = subprocess.run(["security", "find-generic-password", "-w", "-s", KEYCHAIN_SERVICE],
                                   capture_output=True, text=True, timeout=10)
                t = r.stdout.strip() if r.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired):
                t = None
        self._token = t or None
        return self._token


def scrub(ctx, s):
    t = ctx._token or os.environ.get("BLOOM_INFLUX_TOKEN")
    s = str(s)
    return s.replace(t, "***") if t else s


def log(ctx, msg):
    line = f"{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {scrub(ctx, msg)}"
    try:
        ctx.dir.mkdir(parents=True, exist_ok=True)
        p = ctx.logpath
        if p.exists() and p.stat().st_size > LOG_MAX_BYTES:
            os.replace(p, str(p) + ".1")
        L.append_line(p, line)
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


def spool_files(ctx):
    return sorted(ctx.spool.glob("*.lp")) if ctx.spool.exists() else []


def spool_add(ctx, st, lines):
    """Persist lines as ordered batch files (<= BATCH_MAX points each). Enforces the size cap by dropping the
    OLDEST batches, loudly. Returns the number of dropped points."""
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
        sz = f.stat().st_size
        n = f.read_text().count("\n")
        f.unlink()
        total -= sz
        dropped += n
        log(ctx, f"ERROR: SPOOL OVER CAP ({SPOOL_MAX_BYTES} bytes): DROPPED oldest batch {f.name} ({n} points). "
                 "Influx has been unreachable too long; run --backfill after it is back to re-send everything from the sources.")
    return dropped


class WriteResult:
    def __init__(self, kind, code=None, detail=""):
        self.kind, self.code, self.detail = kind, code, detail     # ok | rejected | auth | retry


def influx_write(ctx, body, timeout=HTTP_TIMEOUT):
    tok = ctx.token()
    if not tok:
        return WriteResult("auth", None, f"no InfluxDB token (Keychain service {KEYCHAIN_SERVICE!r} / BLOOM_INFLUX_TOKEN)")
    q = urllib.parse.urlencode({"org": ctx.org, "bucket": ctx.bucket, "precision": "ns"})
    req = urllib.request.Request(f"{ctx.url}/api/v2/write?{q}", data=body.encode(), method="POST",
                                 headers={"Authorization": f"Token {tok}", "Content-Type": "text/plain; charset=utf-8",
                                          "User-Agent": "bloom-influx-push/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return WriteResult("ok", r.status)
    except urllib.error.HTTPError as e:
        try:
            msg = e.read().decode("utf-8", "replace")[:200]
        except OSError:
            msg = ""
        if e.code in (401, 403):
            return WriteResult("auth", e.code, f"HTTP {e.code}: token rejected/rotated or lacks write on bucket")
        if e.code in (400, 413, 422):
            return WriteResult("rejected", e.code, f"HTTP {e.code}: {scrub(ctx, msg)}")
        return WriteResult("retry", e.code, f"HTTP {e.code}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return WriteResult("retry", None, f"{type(e).__name__}: {scrub(ctx, e)}")


def flush_spool(ctx, st, ignore_backoff=False, now=None):
    """Send spool batches oldest-first; stop at the first failure so order is preserved. Returns
    (points_sent, error_or_None)."""
    now = time.time() if now is None else now
    files = spool_files(ctx)
    if not files:
        st["fails"], st["next_try"] = 0, 0
        return 0, None
    if not ignore_backoff and now < float(st.get("next_try", 0)):
        return 0, f"backoff {int(float(st['next_try']) - now)}s (after {st.get('fails', 0)} failures: {st.get('last_error', '')})"
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
            log(ctx, f"ERROR: Influx REJECTED batch {f.name} ({n} points): {res.detail}. Moved to {ctx.rejected} (not retried).")
            continue
        st["fails"] = int(st.get("fails", 0)) + 1
        st["next_try"] = now + min(BACKOFF_BASE_S * 2 ** (st["fails"] - 1), BACKOFF_MAX_S)
        st["last_error"] = res.detail
        return sent, res.detail
    st["fails"], st["next_try"], st["last_error"] = 0, 0, ""
    return sent, None


# ------------------------------------------------------------ one pass
def month_files(base, dirname, pattern):
    d = Path(base) / dirname
    return sorted(d.glob(pattern)) if d.exists() else []


def collect(ctx, st, now, dry=False):
    """Read all sources from the cursors in `st` (mutated in memory). Returns (lines, counts)."""
    counts = {"mac_power": 0, "darkbloom_minute": 0, "darkbloom_earnings": 0, "darkbloom_lifetime": 0, "csv_rows": 0}
    out = []
    st.setdefault("csv", {})
    st.setdefault("earn", {})
    for f in month_files(ctx.base, L.POWER_DIRNAME, "power-log-*.csv"):
        ls, off, n = mac_power_lines(f, int(st["csv"].get(f.name, 0)), ctx.host)
        st["csv"][f.name] = off
        out += ls
        counts["mac_power"] += len(ls)
        counts["csv_rows"] += n
    seen = {}
    for f in month_files(ctx.base, L.ARCHIVE_DIRNAME, "earnings-*.jsonl"):
        ls, off, n = earnings_lines(f, int(st["earn"].get(f.name, 0)), ctx.host, seen)
        st["earn"][f.name] = off
        out += ls
        counts["darkbloom_earnings"] += n
    tp = Path(ctx.base) / L.ARCHIVE_DIRNAME / "totals.jsonl"
    if tp.exists():
        ls, off = totals_lines(tp, int(st.get("totals", 0)), ctx.host)
        st["totals"] = off
        out += ls
        counts["darkbloom_lifetime"] += len(ls)
    settled = L.archive_settled_until(ctx.base)
    if settled is not None:
        cutoff = min(now - dt.timedelta(seconds=LAG_S), settled - dt.timedelta(seconds=L.SETTLE_S))
        first = (st.get("minute_until") or "")[:7]
        for f in month_files(ctx.base, L.POWER_DIRNAME, "power-log-*.csv"):
            month = f.stem.replace("power-log-", "")
            if first and month < first:
                continue
            ls, last, n = minute_lines(ctx.base, month, st.get("minute_until"), cutoff, ctx.host,
                                       ctx.metrics_path, ctx.queue_path)
            if n:
                st["minute_until"] = last
            out += ls
            counts["darkbloom_minute"] += n
    return out, counts


def run_once(ctx, backfill=False, dry_run=False, now=None, sample=0):
    """One pusher pass. Returns (exit_code, summary_dict). Never raises for expected failures."""
    wall = time.time() if now is None else now
    nowdt = dt.datetime.fromtimestamp(wall, dt.timezone.utc)
    ctx.dir.mkdir(parents=True, exist_ok=True) if not dry_run else None
    lock = None
    if not dry_run:
        lock = open(ctx.dir / "lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0, {"skipped": "another run holds the lock"}
    try:
        st = {} if (backfill or dry_run) else load_state(ctx)
        if dry_run:
            prior = load_state(ctx) if not backfill else {}
            st = dict(prior)
        lines, counts = collect(ctx, st, nowdt, dry_run)
        counts["points"] = len(lines)
        if dry_run:
            counts["sample"] = {m: [x for x in lines if x.startswith(m + ",")][:max(sample, 1)]
                                for m in ("mac_power", "darkbloom_minute", "darkbloom_earnings", "darkbloom_lifetime")}
            return 0, counts
        dropped = 0
        if lines:
            dropped = spool_add(ctx, st, lines)
        save_state(ctx, st)                  # cursors advance only after the lines are safely in the spool
        sent, err = flush_spool(ctx, st, ignore_backoff=backfill, now=wall)
        save_state(ctx, st)
        counts.update(sent=sent, spooled=len(spool_files(ctx)), dropped=dropped, error=err)
        tail = f" ERROR: {err}" if err else ""
        log(ctx, f"{'backfill ' if backfill else ''}{'ok' if not err else 'FAIL'} csv_rows={counts['csv_rows']} "
                 f"mac_power={counts['mac_power']} minute={counts['darkbloom_minute']} "
                 f"earnings={counts['darkbloom_earnings']} totals={counts['darkbloom_lifetime']} sent={sent} "
                 f"spool_batches={counts['spooled']} dropped={dropped}{tail}")
        return (0 if not err or err.startswith("backoff") else 1), counts
    finally:
        if lock:
            lock.close()


# ------------------------------------------------------------ query / verify helpers
def flux_query(ctx, flux, timeout=30):
    tok = ctx.token()
    if not tok:
        raise RuntimeError("no InfluxDB token")
    req = urllib.request.Request(f"{ctx.url}/api/v2/query?org={urllib.parse.quote(ctx.org)}",
                                 data=json.dumps({"query": flux, "type": "flux"}).encode(), method="POST",
                                 headers={"Authorization": f"Token {tok}", "Content-Type": "application/json",
                                          "Accept": "application/csv"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"query HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(scrub(ctx, f"query failed: {type(e).__name__}: {e}")) from None


def flux_count(ctx, measurement, field):
    csvtxt = flux_query(ctx, f'from(bucket:"{ctx.bucket}") |> range(start: 2000-01-01T00:00:00Z) '
                             f'|> filter(fn:(r)=> r._measurement=="{measurement}" and r._field=="{field}") '
                             f'|> group() |> count()')
    import csv as _csv
    col = None
    for rec in _csv.reader(csvtxt.splitlines()):
        if not rec or len(rec) < 4:
            continue
        if "_value" in rec:
            col = rec.index("_value")
        elif col is not None and col < len(rec) and rec[col].strip().isdigit():
            return int(rec[col])
    return 0


def unique_points(lines):
    """Distinct (series, timestamp) identities in a list of lines."""
    s = set()
    for ln in lines:
        m = re.search(r"(?<!\\) ", ln)
        s.add((ln[:m.start()] if m else ln, ln.rsplit(" ", 1)[-1]))
    return s


def delete_measurement(ctx, measurement, timeout=30):
    tok = ctx.token()
    req = urllib.request.Request(f"{ctx.url}/api/v2/delete?org={urllib.parse.quote(ctx.org)}&bucket={urllib.parse.quote(ctx.bucket)}",
                                 data=json.dumps({"start": "1970-01-01T00:00:00Z", "stop": "2100-01-01T00:00:00Z",
                                                  "predicate": f'_measurement="{measurement}"'}).encode(), method="POST",
                                 headers={"Authorization": f"Token {tok}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
