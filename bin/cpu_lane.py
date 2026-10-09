#!/usr/bin/env python3
"""cpu_lane.py -- queue side of the Unraid CPU runner (dispatch-cpu-runner).

A durable job store (sqlite WAL + payload blobs on disk under
~/.ollama-dispatch/cpu-jobs/) and the `/api/cpu/*` HTTP routes, mounted into the
dashboard API (ollama-queue-api.py).  Contract = dispatch-cpu-runner README /
reference/queue_server.py:

  producer: POST /api/cpu/jobs {spec,label,stage,bundle_id} -> 201 {id}
            PUT  /api/cpu/jobs/<id>/payload|patch|tools   POST .../ready
            GET  /api/cpu/jobs/<id>   DELETE /api/cpu/jobs/<id> (cancel)
  runner:   POST /api/cpu/claim {runner_id,lease_s} -> 200 {job} | 204
            GET  jobs/<id>/payload|patch|tools   POST jobs/<id>/heartbeat|result|release
  open:     GET  /api/cpu/health
  enroll:   POST /api/cpu/enroll {code,runner_id} -> {token,config} (single-use short-lived code, no bearer)
            GET  /api/cpu/config  (bearer) -> {config} runner tunables (concurrency, lease_s, ...)

SECURITY.  The dashboard API normally trusts every caller because Cloudflare
Access fronts it.  The runner calls over the LAN, so /api/cpu/* enforces its OWN
bearer token (constant-time compare; token file auto-created 0600) on every route
but health, and ALSO refuses any request that looks like it came through the
Cloudflare edge (Cf-* / X-Forwarded-For headers, or a public Host name): the
runner is LAN-only, so the tunnel can never be used to reach these routes even
with a valid token, and a missing/empty token file rejects everything.

The queue daemon uses `outstanding_by_bundle()` (read-only) so a bundle that is
only waiting on a CPU stage does not hold the GPU lanes (see
ollama-queue.py bundle_commit_status `cpu_wait`).  Local-fallback stages register
a `local` marker row (begin_local/end_local) so they count the same way.
"""
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
import uuid
from pathlib import Path

BASE = Path(os.environ.get("CPU_LANE_DIR") or (Path.home() / ".ollama-dispatch" / "cpu-jobs"))
TOKEN_FILE = Path(os.environ.get("CPU_RUNNER_TOKEN_FILE")
                  or (Path.home() / ".config" / "dispatch-cpu-runner" / "token"))
MAX_ATTEMPTS = 3
PRUNE_DAYS = float(os.environ.get("CPU_LANE_PRUNE_DAYS") or 7)      # rows
BLOB_DAYS = float(os.environ.get("CPU_LANE_BLOB_DAYS") or 1)        # payload blobs of finished jobs
UPLOAD_TTL_S = 1800            # an `uploading` job nobody finished
PENDING_TTL_S = 6 * 3600       # a `pending` job nobody ever claimed
MAX_BLOB = 1 << 30
MAX_JSON = 8 << 20
OUTSTANDING = ("uploading", "pending", "running")
TERMINAL = ("done", "failed_infra", "cancelled", "expired")
BLOB_NAMES = ("payload", "patch", "tools")
ENROLL_TTL_S = 3600
DEFAULT_RUNNER_CONFIG = {"concurrency": 2, "lease_s": 60, "max_job_timeout_s": 3600, "cache_max_entries": 8,
                         "node_options": "--max-old-space-size=6144"}
_enroll_fail = []              # monotonic timestamps of recent failed enrollments (global rate limit)
_ENROLL_MAX_FAILS, _ENROLL_WINDOW_S = 5, 60

# Headers only the Cloudflare edge / a reverse proxy adds.  Any one => not the LAN runner.
_PROXY_HEADERS = ("cf-ray", "cf-connecting-ip", "cf-visitor", "cf-ipcountry",
                  "cf-access-jwt-assertion", "cf-access-authenticated-user-email",
                  "x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded")


# ------------------------------------------------------------------ token
_tok_cache = {"mtime": None, "path": None, "val": ""}


def read_token(create=False, path=None):
    """Token string ('' when missing/empty).  create=True makes a random 32-byte hex
    token (0600, parent 0700) if the file is absent.  Never logged or returned by an API."""
    p = Path(path or TOKEN_FILE)
    try:
        if create and not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(secrets.token_hex(32) + "\n")
        m = p.stat().st_mtime_ns
        if _tok_cache["path"] != str(p) or _tok_cache["mtime"] != m:
            _tok_cache.update(path=str(p), mtime=m, val=p.read_text().strip())
        return _tok_cache["val"]
    except OSError:
        return ""


def rotate_token(path=None):
    """Write a NEW random token atomically (0600). Every runner holding the old one gets 401 and must re-enroll."""
    p = Path(path or TOKEN_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".new")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(secrets.token_hex(32) + "\n")
    os.replace(tmp, p)
    return p


def token_ok(header_value, token=None):
    tok = read_token() if token is None else token
    if not tok or not header_value or not header_value.startswith("Bearer "):
        return False
    return hmac.compare_digest(header_value[7:].strip().encode(), tok.encode())


def _host_is_lan(host):
    h = (host or "").strip().lower()
    if h.startswith("["):                     # [::1]:7684
        return True
    h = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    if not h or re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", h) or "." not in h or h == "localhost":
        return True
    return h.endswith((".local", ".lan", ".home.arpa", ".internal"))


def via_proxy(headers):
    """True when the request carries any sign of the Cloudflare edge / a proxy hop."""
    try:
        keys = {k.lower() for k in headers.keys()}
    except Exception:
        return True
    if any(k in keys for k in _PROXY_HEADERS):
        return True
    return not _host_is_lan(headers.get("Host", ""))


# ------------------------------------------------------------------ store
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL DEFAULT 'remote',
  status TEXT NOT NULL, spec TEXT, label TEXT, stage TEXT, bundle_id TEXT,
  attempt INTEGER NOT NULL DEFAULT 0, runner_id TEXT, lease_token TEXT, lease_expires REAL DEFAULT 0,
  cancel INTEGER NOT NULL DEFAULT 0, result TEXT, created REAL, ready_at REAL, claimed_at REAL,
  finished_at REAL, expires_at REAL, has_patch INTEGER DEFAULT 0, has_tools INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, seq);
CREATE TABLE IF NOT EXISTS enroll(code_hash TEXT PRIMARY KEY, expires REAL NOT NULL, used REAL, created REAL);
CREATE TABLE IF NOT EXISTS runners(runner_id TEXT PRIMARY KEY, last_seen REAL, claims INTEGER DEFAULT 0);
"""


class Store:
    def __init__(self, base=None, clock=time.time, max_attempts=MAX_ATTEMPTS):
        self.base = Path(base or BASE)
        self.clock = clock
        self.max_attempts = max_attempts
        self.blobs = self.base / "blobs"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.db = str(self.base / "jobs.sqlite")
        self.lock = threading.RLock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    # one short-lived autocommit connection per operation: safe across threads and
    # across processes (the queue daemon reads the same file).
    @contextlib.contextmanager
    def _conn(self):
        c = sqlite3.connect(self.db, timeout=30, isolation_level=None)
        try:
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            yield c
        finally:
            c.close()

    # ---- enrollment codes (single-use, short-lived; only a sha256 is stored) and runner tunables
    @staticmethod
    def _chash(code):
        return hashlib.sha256((code or "").strip().encode()).hexdigest()

    def mint_enroll(self, ttl_s=ENROLL_TTL_S):
        code = secrets.token_urlsafe(9)
        now = self.clock()
        with self._conn() as c:
            c.execute("DELETE FROM enroll WHERE expires < ? OR used IS NOT NULL", (now - 86400,))
            c.execute("INSERT INTO enroll(code_hash,expires,used,created) VALUES(?,?,NULL,?)",
                      (self._chash(code), now + float(ttl_s), now))
        return code

    def redeem_enroll(self, code):
        """True exactly once for a live code (constant-time compare over live hashes, atomic consume)."""
        h = self._chash(code)
        now = self.clock()
        with self._conn() as c:
            rows = c.execute("SELECT code_hash FROM enroll WHERE used IS NULL AND expires >= ?", (now,)).fetchall()
            hit = None
            for r in rows:
                if hmac.compare_digest(r["code_hash"].encode(), h.encode()):
                    hit = r["code_hash"]
            if hit is None:
                return False
            return c.execute("UPDATE enroll SET used=? WHERE code_hash=? AND used IS NULL", (now, hit)).rowcount == 1

    def clear_enroll(self):
        with self._conn() as c:
            c.execute("DELETE FROM enroll")

    def runner_config(self):
        conf = dict(DEFAULT_RUNNER_CONFIG)
        try:
            conf.update(json.loads((self.base / "runner-config.json").read_text()))
        except (OSError, ValueError):
            pass
        return conf

    def set_runner_config(self, **kv):
        conf = self.runner_config()
        conf.update(kv)
        tmp = self.base / "runner-config.json.tmp"
        tmp.write_text(json.dumps(conf, indent=1))
        os.replace(tmp, self.base / "runner-config.json")
        return conf

    # ---- helpers
    def _blob_dir(self, jid):
        return self.blobs / jid

    @staticmethod
    def _row(r):
        if r is None:
            return None
        d = dict(r)
        for k in ("spec", "result"):
            if d.get(k):
                d[k] = json.loads(d[k])
        return d

    def get(self, jid):
        with self._conn() as c:
            return self._row(c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())

    # ---- producer
    def create(self, spec, label=None, stage=None, bundle_id=None):
        jid = uuid.uuid4().hex
        now = self.clock()
        with self.lock, self._conn() as c:
            c.execute("INSERT INTO jobs(id,status,spec,label,stage,bundle_id,created) VALUES(?,?,?,?,?,?,?)",
                      (jid, "uploading", json.dumps(spec), label, stage or (spec or {}).get("stage"),
                       bundle_id, now))
        return jid

    def put_blob(self, jid, name, reader, length):
        """Stream `length` bytes from reader.read(n) to disk (atomic rename). -> bytes | None (no job)
        | -1 (job not accepting uploads)."""
        j = self.get(jid)
        if j is None:
            return None
        if j["status"] != "uploading":
            return -1
        d = self._blob_dir(jid)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / (name + ".part")
        n = 0
        with open(tmp, "wb") as fh:
            while n < length:
                chunk = reader.read(min(1 << 20, length - n))
                if not chunk:
                    break
                fh.write(chunk)
                n += len(chunk)
        if n != length:
            tmp.unlink(missing_ok=True)
            raise ConnectionError("short upload %d/%d" % (n, length))
        os.replace(tmp, d / name)
        col = {"patch": "has_patch", "tools": "has_tools"}.get(name)
        if col:
            with self._conn() as c:
                c.execute("UPDATE jobs SET %s=1 WHERE id=?" % col, (jid,))
        return n

    def blob_path(self, jid, name):
        p = self._blob_dir(jid) / name
        return p if p.is_file() else None

    def ready(self, jid):
        """-> 'ok' | 'nojob' | 'nopayload'"""
        with self.lock, self._conn() as c:
            j = c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
            if not j:
                return "nojob"
            if self.blob_path(jid, "payload") is None:
                return "nopayload"
            c.execute("UPDATE jobs SET status='pending', ready_at=? WHERE id=? AND status='uploading'",
                      (self.clock(), jid))
        return "ok"

    def cancel(self, jid):
        with self.lock, self._conn() as c:
            r = c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
            if not r:
                return False
            if r["status"] in ("uploading", "pending"):
                c.execute("UPDATE jobs SET status='cancelled', cancel=1, finished_at=? WHERE id=?",
                          (self.clock(), jid))
            elif r["status"] == "running":
                c.execute("UPDATE jobs SET cancel=1 WHERE id=?", (jid,))
        return True

    # ---- runner
    def reap(self):
        """Lease expiry + housekeeping.  Returns number of rows changed."""
        now = self.clock()
        n = 0
        with self.lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                for r in c.execute("SELECT id,attempt,cancel FROM jobs WHERE kind='remote' AND status='running' "
                                   "AND lease_expires<?", (now,)).fetchall():
                    if r["cancel"]:
                        c.execute("UPDATE jobs SET status='cancelled',finished_at=?,lease_token=NULL,runner_id=NULL "
                                  "WHERE id=?", (now, r["id"]))
                    elif r["attempt"] >= self.max_attempts:
                        c.execute("UPDATE jobs SET status='failed_infra',finished_at=?,lease_token=NULL,"
                                  "runner_id=NULL,result=? WHERE id=?",
                                  (now, json.dumps({"exit_code": None, "infra_error":
                                                    "lease expired %d times" % r["attempt"]}), r["id"]))
                    else:
                        c.execute("UPDATE jobs SET status='pending',lease_token=NULL,runner_id=NULL WHERE id=?",
                                  (r["id"],))
                    n += 1
                n += c.execute("UPDATE jobs SET status='cancelled',finished_at=? WHERE status='uploading' "
                               "AND created<?", (now, now - UPLOAD_TTL_S)).rowcount
                n += c.execute("UPDATE jobs SET status='cancelled',finished_at=? WHERE status='pending' "
                               "AND created<?", (now, now - PENDING_TTL_S)).rowcount
                n += c.execute("UPDATE jobs SET status='expired',finished_at=? WHERE kind='local' "
                               "AND status='running' AND expires_at<?", (now, now)).rowcount
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return n

    def claim(self, runner_id, lease_s=60):
        """Atomic: oldest pending remote job -> running.  -> job dict | None."""
        now = self.clock()
        lease_s = int(lease_s or 60)
        self.reap()
        with self.lock, self._conn() as c:
            c.execute("INSERT INTO runners(runner_id,last_seen,claims) VALUES(?,?,0) "
                      "ON CONFLICT(runner_id) DO UPDATE SET last_seen=excluded.last_seen", (runner_id, now))
            c.execute("BEGIN IMMEDIATE")
            try:
                r = c.execute("SELECT * FROM jobs WHERE kind='remote' AND status='pending' AND cancel=0 "
                              "ORDER BY seq LIMIT 1").fetchone()
                if not r:
                    c.execute("COMMIT")
                    return None
                tok = secrets.token_hex(8)
                c.execute("UPDATE jobs SET status='running',attempt=attempt+1,runner_id=?,lease_token=?,"
                          "lease_expires=?,claimed_at=? WHERE id=?", (runner_id, tok, now + lease_s, now, r["id"]))
                c.execute("UPDATE runners SET claims=claims+1 WHERE runner_id=?", (runner_id,))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        spec = json.loads(r["spec"])
        spec.update(has_patch=bool(r["has_patch"]), has_tools=bool(r["has_tools"]))
        return {"id": r["id"], "spec": spec, "attempt": r["attempt"] + 1, "lease_token": tok}

    def _lease_op(self, jid, lease_token):
        j = self.get(jid)
        if j is None:
            return None, "nojob"
        if j["status"] != "running" or not lease_token or j["lease_token"] != lease_token:
            return j, "lost"
        return j, "ok"

    def heartbeat(self, jid, lease_token, lease_s=60, runner_id=None):
        """-> ('ok', cancel) | ('lost', None) | ('nojob', None)"""
        with self.lock:
            j, st = self._lease_op(jid, lease_token)
            if st != "ok":
                return st, None
            now = self.clock()
            with self._conn() as c:
                c.execute("UPDATE jobs SET lease_expires=? WHERE id=?", (now + int(lease_s or 60), jid))
                if runner_id:
                    c.execute("INSERT INTO runners(runner_id,last_seen,claims) VALUES(?,?,0) "
                              "ON CONFLICT(runner_id) DO UPDATE SET last_seen=excluded.last_seen", (runner_id, now))
            return "ok", bool(j["cancel"])

    def release(self, jid, lease_token):
        with self.lock:
            j, st = self._lease_op(jid, lease_token)
            if st != "ok":
                return st
            with self._conn() as c:
                if j["cancel"]:
                    c.execute("UPDATE jobs SET status='cancelled',finished_at=?,lease_token=NULL,runner_id=NULL "
                              "WHERE id=?", (self.clock(), jid))
                else:
                    c.execute("UPDATE jobs SET status='pending',lease_token=NULL,runner_id=NULL WHERE id=?", (jid,))
            return "ok"

    def result(self, jid, lease_token, body):
        with self.lock:
            j, st = self._lease_op(jid, lease_token)
            if st != "ok":
                return st
            res = {k: v for k, v in body.items() if k not in ("lease_token",)}
            infra = body.get("infra_error")
            now = self.clock()
            if j["cancel"] and not infra:
                status = "cancelled"
            elif infra and infra == "isolation_unavailable" and j["attempt"] < self.max_attempts:
                status, res = "pending", None
            else:
                status = "failed_infra" if infra else "done"
            with self._conn() as c:
                if status == "pending":
                    c.execute("UPDATE jobs SET status='pending',lease_token=NULL,runner_id=NULL WHERE id=?", (jid,))
                else:
                    c.execute("UPDATE jobs SET status=?,result=?,finished_at=?,lease_token=NULL WHERE id=?",
                              (status, json.dumps(res), now, jid))
            return "ok"

    # ---- local-stage markers (so a local CPU stage counts like a remote one)
    def begin_local(self, stage, bundle_id, timeout_s, label=None):
        jid = uuid.uuid4().hex
        now = self.clock()
        with self._conn() as c:
            c.execute("INSERT INTO jobs(id,kind,status,spec,label,stage,bundle_id,created,claimed_at,expires_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (jid, "local", "running", json.dumps({"stage": stage}), label, stage, bundle_id, now, now,
                       now + float(timeout_s or 600) + 120))
        return jid

    def end_local(self, jid, exit_code=None):
        with self._conn() as c:
            c.execute("UPDATE jobs SET status='done',finished_at=?,result=? WHERE id=? AND kind='local'",
                      (self.clock(), json.dumps({"exit_code": exit_code}), jid))

    # ---- queries
    def outstanding_by_bundle(self):
        """{bundle_id: [job ids]} of CPU work a bundle is waiting on (remote pending/running/
        uploading + live local markers).  Capped by age so a crashed caller cannot wedge a bundle."""
        now = self.clock()
        out = {}
        with self._conn() as c:
            for r in c.execute("SELECT id,kind,spec,bundle_id,created,expires_at FROM jobs "
                               "WHERE status IN ('uploading','pending','running') AND bundle_id IS NOT NULL"):
                if r["kind"] == "local":
                    if (r["expires_at"] or 0) < now:
                        continue
                else:
                    try:
                        t = float(json.loads(r["spec"] or "{}").get("timeout_s") or 600)
                    except Exception:
                        t = 600.0
                    if now - (r["created"] or now) > t + 1800:
                        continue
                out.setdefault(r["bundle_id"], []).append(r["id"])
        return out

    def summary(self, recent=15):
        now = self.clock()
        with self._conn() as c:
            counts = {r[0]: r[1] for r in c.execute("SELECT status,COUNT(*) FROM jobs WHERE kind='remote' "
                                                    "GROUP BY status")}
            local_active = c.execute("SELECT COUNT(*) FROM jobs WHERE kind='local' AND status='running' "
                                     "AND expires_at>=?", (now,)).fetchone()[0]
            running = [dict(id=r["id"], label=r["label"], stage=r["stage"], bundle=r["bundle_id"],
                            runner=r["runner_id"], attempt=r["attempt"],
                            running_s=round(now - (r["claimed_at"] or now), 1))
                       for r in c.execute("SELECT * FROM jobs WHERE kind='remote' AND status='running' ORDER BY seq")]
            pending = [dict(id=r["id"], label=r["label"], stage=r["stage"], bundle=r["bundle_id"],
                            waiting_s=round(now - (r["ready_at"] or r["created"] or now), 1))
                       for r in c.execute("SELECT * FROM jobs WHERE kind='remote' AND status='pending' ORDER BY seq LIMIT 20")]
            done = []
            for r in c.execute("SELECT * FROM jobs WHERE kind='remote' AND finished_at IS NOT NULL "
                               "ORDER BY finished_at DESC LIMIT ?", (recent,)):
                res = json.loads(r["result"]) if r["result"] else {}
                done.append(dict(id=r["id"], label=r["label"], stage=r["stage"], bundle=r["bundle_id"],
                                 status=r["status"], runner=(res or {}).get("runner_id") or r["runner_id"],
                                 exit_code=(res or {}).get("exit_code"),
                                 duration_s=round(r["finished_at"] - r["claimed_at"], 1) if r["claimed_at"] else None,
                                 queue_wait_s=round(r["claimed_at"] - (r["ready_at"] or r["created"]), 1)
                                 if r["claimed_at"] else None, finished_at=r["finished_at"]))
            runners = [dict(runner=r["runner_id"], seen_s_ago=round(now - r["last_seen"], 1), claims=r["claims"])
                       for r in c.execute("SELECT * FROM runners ORDER BY last_seen DESC LIMIT 10")]
        waiting = self.outstanding_by_bundle()
        return {"enabled": True, "counts": counts, "queue_depth": counts.get("pending", 0),
                "running": running, "pending": pending, "recent": done, "runners": runners,
                "local_stages_active": local_active,
                "bundles_waiting": {k: len(v) for k, v in waiting.items()}}

    def prune(self):
        now = self.clock()
        n = 0
        with self.lock, self._conn() as c:
            for r in c.execute("SELECT id FROM jobs WHERE status IN ('done','failed_infra','cancelled','expired') "
                               "AND finished_at<?", (now - BLOB_DAYS * 86400,)).fetchall():
                d = self._blob_dir(r["id"])
                if d.exists():
                    _rmtree(d)
            for r in c.execute("SELECT id FROM jobs WHERE status IN ('done','failed_infra','cancelled','expired') "
                               "AND finished_at<?", (now - PRUNE_DAYS * 86400,)).fetchall():
                _rmtree(self._blob_dir(r["id"]))
                c.execute("DELETE FROM jobs WHERE id=?", (r["id"],))
                n += 1
        # blob dirs with no row at all
        try:
            with self._conn() as c:
                known = {r[0] for r in c.execute("SELECT id FROM jobs")}
            for d in self.blobs.iterdir():
                if d.name not in known and now - d.stat().st_mtime > 3600:
                    _rmtree(d)
        except OSError:
            pass
        return n


def _rmtree(p):
    import shutil
    shutil.rmtree(p, ignore_errors=True)


# ------------------------------------------------------------------ singleton + reaper
_store = None
_store_lock = threading.Lock()


def get_store():
    global _store
    with _store_lock:
        if _store is None:
            _store = Store(BASE)
        return _store


def start_reaper(store=None, interval=10, prune_every=3600):
    """Daemon thread: lease reaper tick + hourly prune.  Never raises out of the thread."""
    s = store or get_store()

    def loop():
        last_prune = 0.0
        while True:
            try:
                s.reap()
                if time.time() - last_prune > prune_every:
                    s.prune()
                    last_prune = time.time()
            except Exception as e:      # noqa: BLE001
                print("[cpu-lane] reaper error: %r" % (e,), flush=True)
            time.sleep(interval)
    t = threading.Thread(target=loop, name="cpu-lane-reaper", daemon=True)
    t.start()
    return t


def outstanding_by_bundle(base=None):
    """Read-only for the queue daemon.  {} on ANY problem (fail toward 'no cpu wait')."""
    try:
        b = Path(base or BASE)
        if not (b / "jobs.sqlite").exists():
            return {}
        s = Store(b)
        return s.outstanding_by_bundle()
    except Exception:
        return {}


def lan_ip():
    """This Mac's LAN IPv4 (what a runner should put in CPU_RUNNER_API), '' if undiscoverable.
    Prefers an RFC1918 address on a physical interface (en*) over the routing-table guess, which
    returns a WARP/CGNAT 100.x tunnel address when Cloudflare WARP is up."""
    import ipaddress
    import subprocess
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
        iface = None
        for line in out.splitlines():
            if line and not line[0].isspace():
                iface = line.split(":")[0]
            elif iface and iface.startswith("en") and line.strip().startswith("inet "):
                ip = line.split()[1]
                if ipaddress.ip_address(ip).is_private and not ip.startswith("169.254."):
                    return ip
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("203.0.113.129", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return ""


# ------------------------------------------------------------------ HTTP
_ID_RE = re.compile(r"^/api/cpu/jobs/([0-9a-f]{32})(?:/(\w+))?$")


def _send(h, code, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    h.send_response(code)
    h.send_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    if data:
        h.wfile.write(data)


def _json_body(h):
    n = int(h.headers.get("Content-Length") or 0)
    if n > MAX_JSON:
        raise ValueError("body too large")
    raw = h.rfile.read(n) if n else b""
    return json.loads(raw) if raw else {}


def _enroll(h, store, token):
    """POST /api/cpu/enroll: trade a single-use code for the bearer token.  LAN-only (caller already passed the
    proxy check), globally rate limited after repeated failures, failures are slowed, codes are consumed once."""
    now = time.monotonic()
    _enroll_fail[:] = [t for t in _enroll_fail if now - t < _ENROLL_WINDOW_S]
    if len(_enroll_fail) >= _ENROLL_MAX_FAILS:
        return _send(h, 429, {"error": "too many failed enrollments; wait a minute"})
    try:
        b = _json_body(h)
    except (ValueError, json.JSONDecodeError):
        return _send(h, 400, {"error": "bad json"})
    code = str(b.get("code") or "")
    tok = read_token() if token is None else token
    S = store or get_store()
    if not tok or len(code) < 8 or len(code) > 128 or not S.redeem_enroll(code):
        _enroll_fail.append(time.monotonic())
        time.sleep(0.5)
        return _send(h, 403, {"error": "invalid, expired or already used enrollment code"})
    return _send(h, 200, {"token": tok, "runner_id": str(b.get("runner_id") or "")[:64], "config": S.runner_config()})


def handle(h, method, store=None, token=None):
    """Serve one /api/cpu/* request on handler `h`.  Always responds."""
    path = h.path.split("?")[0]
    if path == "/api/cpu/health" and method == "GET":
        return _send(h, 200, {"ok": True})
    if via_proxy(h.headers):
        return _send(h, 403, {"error": "cpu lane is LAN-only"})
    if path == "/api/cpu/enroll" and method == "POST":
        return _enroll(h, store, token)
    if not token_ok(h.headers.get("Authorization", ""), token):
        time.sleep(0.25)               # slow a brute-force; handler threads are cheap
        return _send(h, 401, {"error": "unauthorized"})
    S = store or get_store()
    try:
        if method == "POST" and path == "/api/cpu/jobs":
            b = _json_body(h)
            if not isinstance(b.get("spec"), dict) or not b["spec"].get("cmd"):
                return _send(h, 400, {"error": "spec.cmd required"})
            return _send(h, 201, {"id": S.create(b["spec"], b.get("label"), b.get("stage"), b.get("bundle_id"))})
        if method == "POST" and path == "/api/cpu/claim":
            b = _json_body(h)
            if not b.get("runner_id"):
                return _send(h, 400, {"error": "runner_id required"})
            job = S.claim(str(b["runner_id"]), b.get("lease_s"))
            return _send(h, 200, {"job": job}) if job else _send(h, 204)
        if method == "GET" and path == "/api/cpu/config":
            return _send(h, 200, {"config": S.runner_config()})
        if method == "GET" and path == "/api/cpu/runners":
            return _send(h, 200, {"runners": S.summary()["runners"]})
        m = _ID_RE.match(path)
        if not m or S.get(m.group(1)) is None:
            return _send(h, 404, {"error": "not found"})
        jid, sub = m.group(1), m.group(2)
        if sub is None and method == "GET":
            j = S.get(jid)
            return _send(h, 200, {"id": jid, "status": j["status"], "attempt": j["attempt"],
                                  "result": j["result"], "runner_id": j["runner_id"]})
        if sub is None and method == "DELETE":
            S.cancel(jid)
            return _send(h, 200, {"ok": True})
        if sub in BLOB_NAMES:
            if method == "PUT":
                n = int(h.headers.get("Content-Length") or 0)
                if n > MAX_BLOB:
                    return _send(h, 413, {"error": "blob too large"})
                r = S.put_blob(jid, sub, h.rfile, n)
                if r == -1:
                    return _send(h, 409, {"error": "job not accepting uploads"})
                return _send(h, 200, {"ok": True, "bytes": r})
            if method == "GET":
                p = S.blob_path(jid, sub)
                if p is None:
                    return _send(h, 404, {"error": "no blob"})
                size = p.stat().st_size
                h.send_response(200)
                h.send_header("Content-Type", "application/octet-stream")
                h.send_header("Content-Length", str(size))
                h.end_headers()
                with open(p, "rb") as fh:
                    while True:
                        chunk = fh.read(1 << 20)
                        if not chunk:
                            break
                        h.wfile.write(chunk)
                return None
        if sub == "ready" and method == "POST":
            r = S.ready(jid)
            return _send(h, 400, {"error": "payload missing"}) if r == "nopayload" else _send(h, 200, {"ok": True})
        if sub in ("heartbeat", "result", "release") and method == "POST":
            b = _json_body(h)
            lt = b.get("lease_token")
            if sub == "heartbeat":
                st, cancel = S.heartbeat(jid, lt, b.get("lease_s"), b.get("runner_id"))
                return _send(h, 200, {"ok": True, "cancel": cancel}) if st == "ok" else _send(h, 409, {"error": "lease lost"})
            st = S.release(jid, lt) if sub == "release" else S.result(jid, lt, b)
            return _send(h, 200, {"ok": True}) if st == "ok" else _send(h, 409, {"error": "lease lost"})
        return _send(h, 404, {"error": "no route"})
    except (ValueError, json.JSONDecodeError) as e:
        return _send(h, 400, {"error": str(e)[:200]})
    except ConnectionError:
        return None
    except Exception as e:      # noqa: BLE001
        return _send(h, 500, {"error": type(e).__name__})


if __name__ == "__main__":
    # tiny CLI: `cpu_lane.py token` (create if absent, print PATH only), `cpu_lane.py ip`, `cpu_lane.py api-url`, `cpu_lane.py summary`
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"
    if cmd == "token":
        read_token(create=True)
        print(TOKEN_FILE)
    elif cmd == "ip":
        print(lan_ip())
    elif cmd == "api-url":          # the exact CPU_RUNNER_API value for the Unraid .env
        print("http://%s:7684" % (lan_ip() or "<mac-lan-ip>"))
    elif cmd == "enroll-code":      # single-use code for a new Unraid runner: paste it + the API URL into the template
        ttl = ENROLL_TTL_S
        if "--ttl" in sys.argv:
            ttl = int(sys.argv[sys.argv.index("--ttl") + 1])
        read_token(create=True)
        code = get_store().mint_enroll(ttl)
        print("CPU_RUNNER_API=http://%s:7684" % (lan_ip() or "<mac-lan-ip>"))
        print("CPU_RUNNER_ENROLL_CODE=%s" % code)
        print("(single use, expires in %d min; the container stores the token itself and never needs the code again)" % (ttl // 60))
    elif cmd == "rotate-token":     # revoke every runner: new token, outstanding codes cleared
        rotate_token()
        get_store().clear_enroll()
        print("token rotated; all runners are now rejected (401). Mint a new code: cpu_lane.py enroll-code")
    elif cmd == "config":           # `config` shows, `config key=value ...` sets (concurrency, lease_s, ...)
        st = get_store()
        kv = {}
        for a in sys.argv[2:]:
            k, _, v = a.partition("=")
            kv[k] = int(v) if v.isdigit() else v
        print(json.dumps(st.set_runner_config(**kv) if kv else st.runner_config(), indent=1))
    else:
        print(json.dumps(get_store().summary(), indent=1))
