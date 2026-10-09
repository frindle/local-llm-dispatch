#!/usr/bin/env python3
"""Hermetic tests for bloom_control.py and the queue-side hooks (ollama-queue.py BLOOM CONTROL
block, focus_drop_parked, fit_hold release).

NEVER touches the real queue daemon, Darkbloom, BloomGauge or provider.toml: a fake BloomGauge
HTTP server (control API with controlVersion), a fake Darkbloom HTTP stub, a fake `darkbloom` CLI
shim first on PATH, a temp HOME/state.

Usage:  test-bloom-control.py [--queue PATH] [--bc PATH] [--only NAME]
Marker: ALL PASS. Exit 0 = all checks pass. The pipeline-canary seam runs this with --queue/--bc
pointing at the sandbox copy; a revert of the feature must go red."""
import argparse
import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAIR = ["Qwen3.5-9B", "qwen3.6-35b-a3b-vl-mtp-mxfp8"]
GEMMA = ["gemma-4-26b-qat-4bit"]
KEY = "SECRETKEYSECRETKEYSECRETKEYSECRETKEY123"
FAILS = []
SEL = []


def check(name, cond, extra=""):
    print(("ok  : " if cond else "FAIL: ") + name + ("" if cond else "  -- " + str(extra)))
    if not cond:
        FAILS.append(name)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class World:
    def __init__(self, bc_path, serving=GEMMA, mode="on", pinned=False, shape="ok", auto_update=True,
                 start_rc=0, refuse_manual=False, bloom_up=True, slots=2):
        self.d = Path(tempfile.mkdtemp(prefix="bctl-"))
        self.home = self.d / "dispatch"
        self.home.mkdir()
        self.dbdir = self.d / "db"
        self.dbdir.mkdir()
        self.bin = self.d / "bin"
        self.bin.mkdir()
        self.posts = []
        self.sv = {"mode": mode, "pinned": pinned, "shape": shape, "version": 1,
                   "refuse_manual": refuse_manual, "gets": 0}
        self.toml = self.d / "provider.toml"
        self.toml.write_text(
            "[backend]\nenabled_models = [ '%s' ]\nidle_timeout_mins = 0\nmax_model_slots = %d\n"
            "preload_models = [ 'qwen3.6-35b-a3b-vl-mtp-mxfp8', 'qwen3.5-9b' ]\n\n"
            "[provider]\nauto_update = %s\n" % ("', '".join(serving), slots, "true" if auto_update else "false"))
        self.loaded = self.d / "loaded-models.json"
        self.loaded.write_text(json.dumps({"models": serving}))
        (self.dbdir / "start_rc").write_text(str(start_rc))
        self.start_delay = self.d / "start_delay"
        w = self

        class Bloom(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if not w.bloom_up:
                    return self._send(503, {})
                w.sv["gets"] += 1
                if self.path != "/api/optimizer/control":
                    return self._send(404, {})
                if w.sv["shape"] == "drift":
                    return self._send(200, {"version": 9, "state": "auto"})
                self._send(200, {"controlVersion": "v%d" % w.sv["version"], "providerVersion": "p1",
                                 "providerRunning": True, "currentModel": "x",
                                 "automatic": {"mode": w.sv["mode"], "canEnable": True},
                                 "manager": {"pinned": w.sv["pinned"]}})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.headers.get("X-Bloom-Action") != "optimizer":
                    return self._send(403, {"error": "bad header"})
                if body.get("expectedControl") != "v%d" % w.sv["version"]:
                    return self._send(409, {"error": "The control status changed. Refresh it."})
                a = body.get("action")
                w.posts.append((a, body.get("enabled")))
                if a == "set-automatic" and body.get("enabled") is False and w.sv["refuse_manual"]:
                    return self._send(409, {"error": "The saved plan changed."})
                if a == "set-automatic":
                    w.sv["mode"] = "on" if body.get("enabled") else "manual"
                elif a == "release-pin":
                    w.sv["pinned"] = False
                w.sv["version"] += 1
                self._send(200, {"controlVersion": "v%d" % w.sv["version"]})

        self.bloom_up = bloom_up
        self.bsrv = ThreadingHTTPServer(("127.0.0.1", 0), Bloom)
        threading.Thread(target=self.bsrv.serve_forever, daemon=True).start()

        class DB(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                ms = json.loads(w.loaded.read_text()).get("models", [])
                if self.path == "/health":
                    b = json.dumps({"status": "ok"}).encode()
                else:
                    b = json.dumps({"data": [{"id": m} for m in ms]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.dsrv = ThreadingHTTPServer(("127.0.0.1", 0), DB)
        threading.Thread(target=self.dsrv.serve_forever, daemon=True).start()
        (self.d / "local.json").write_text(json.dumps(
            {"api_key": KEY, "base_url": "http://127.0.0.1:%d/v1" % self.dsrv.server_address[1]}))
        shim = self.bin / "darkbloom"
        shim.write_text(
            "#!/usr/bin/env python3\n"
            "import sys, json, os, re, time, pathlib\n"
            "d = pathlib.Path(%r); toml = pathlib.Path(%r); loaded = pathlib.Path(%r)\n"
            "open(d/'calls.log','a').write(' '.join(sys.argv[1:]) + '\\n')\n"
            "cmd = sys.argv[1] if len(sys.argv) > 1 else ''\n"
            "if cmd == 'autoupdate':\n"
            "    v = 'true' if sys.argv[2] == 'enable' else 'false'\n"
            "    t = toml.read_text()\n"
            "    toml.write_text(re.sub(r'(?m)^auto_update = .*$', 'auto_update = ' + v, t))\n"
            "    sys.exit(0)\n"
            "if cmd == 'start':\n"
            "    dl = pathlib.Path(%r)\n"
            "    if dl.exists(): time.sleep(float(dl.read_text()))\n"
            "    rc = int((d/'start_rc').read_text())\n"
            "    if rc: print('boom token ' + %r); sys.exit(rc)\n"
            "    ms = [sys.argv[i+1] for i,a in enumerate(sys.argv) if a == '--model']\n"
            "    t = toml.read_text()\n"
            "    toml.write_text(re.sub(r'(?m)^enabled_models = .*$', 'enabled_models = [ ' + ', '.join(repr(m) for m in ms) + ' ]', t))\n"
            "    loaded.write_text(json.dumps({'models': ms}))\n"
            "sys.exit(0)\n" % (str(self.dbdir), str(self.toml), str(self.loaded), str(self.start_delay), KEY))
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
        self.env = {
            "BLOOMCTL_HOME": str(self.home), "BLOOMCTL_BLOOM_URL": "http://127.0.0.1:%d" % self.bsrv.server_address[1],
            "BLOOMCTL_PROVIDER_TOML": str(self.toml), "BLOOMCTL_LOCAL_JSON": str(self.d / "local.json"),
            "BLOOMCTL_LOADED_JSON": str(self.loaded), "BLOOMCTL_POLL_S": "0.1", "BLOOMCTL_WAIT_S": "20",
            "BLOOMCTL_DRAIN_S": "5", "BLOOMCTL_PAIR": ",".join(PAIR),
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
        }
        for k in ("BLOOMCTL_DARKBLOOM_BIN", "BLOOM_CONTROL", "BLOOMCTL_DRY_RUN", "BLOOMCTL_STATE"):
            os.environ.pop(k, None)
        os.environ.update(self.env)

    def calls(self):
        p = self.dbdir / "calls.log"
        return p.read_text().splitlines() if p.exists() else []

    def starts(self):
        return [c for c in self.calls() if c.startswith("start")]

    def state(self):
        try:
            return json.loads((self.home / "bloom-control-state.json").read_text())
        except OSError:
            return {}

    def close(self):
        self.bsrv.shutdown()
        self.dsrv.shutdown()
        shutil.rmtree(self.d, ignore_errors=True)


def scenario(name):
    def deco(fn):
        fn._name = name
        SEL.append(fn)
        return fn
    return deco


@scenario("hold")
def t_hold(bc, q):
    w = World(bc.__file__)
    try:
        r = bc.hold_for_queue()
        check("hold ok", r["ok"] and not r["noop"], r)
        check("bloom set Manual", w.sv["mode"] == "manual" and ("set-automatic", False) in w.posts, w.posts)
        st = w.starts()
        check("one darkbloom start with BOTH pair models + idle-timeout 0 + local-endpoint + drain timeout",
              len(st) == 1 and all(("--model " + m) in st[0] for m in PAIR) and "--idle-timeout 0" in st[0]
              and "--local-endpoint" in st[0] and "--timeout 5" in st[0], st)
        txt = w.toml.read_text()
        check("toml: preload case fixed + slots 2 + idle 0",
              "'Qwen3.5-9B'" in txt and "qwen3.5-9b" not in txt and "max_model_slots = 2" in txt
              and "idle_timeout_mins = 0" in txt, txt)
        check("auto-update disabled and remembered", "auto_update = false" in txt and w.state().get("autoupdate_was_on"))
        check("state phase ready/queue", w.state().get("phase") == "ready" and w.state().get("mode") == "queue", w.state())
        check("no secret in log", KEY not in (w.home / "bloom-control.log").read_text())
    finally:
        w.close()


@scenario("idempotent")
def t_idem(bc, q):
    w = World(bc.__file__)
    try:
        bc.hold_for_queue()
        n_posts, n_calls = len(w.posts), len(w.calls())
        r = bc.hold_for_queue()
        check("second hold is a no-op", r["ok"] and r["noop"], r)
        check("no new POST / CLI call", len(w.posts) == n_posts and len(w.calls()) == n_calls, (w.posts, w.calls()))
        # pair already served from the start + Manual already -> no darkbloom start at all
        w2 = World(bc.__file__, serving=PAIR, mode="manual", auto_update=False)
        try:
            r2 = bc.hold_for_queue()
            check("pair served + Manual: no start, no POST", r2["ok"] and not w2.starts() and not w2.posts, (r2, w2.calls(), w2.posts))
        finally:
            w2.close()
    finally:
        w.close()


@scenario("release")
def t_release(bc, q):
    w = World(bc.__file__)
    try:
        bc.hold_for_queue()
        w.sv["pinned"] = True
        r = bc.release_to_bloom()
        check("release ok", r["ok"] and not r["noop"], r)
        check("release-pin then set-automatic true", [p[0] for p in w.posts][-2:] == ["release-pin", "set-automatic"]
              and w.sv["mode"] == "on" and not w.sv["pinned"], w.posts)
        check("auto-update re-enabled", "auto_update = true" in w.toml.read_text() and "autoupdate enable" in " ".join(w.calls()))
        check("state back to bloom", w.state().get("mode") == "bloom", w.state())
        n = len(w.posts)
        r2 = bc.release_to_bloom()
        check("second release is a no-op", r2["noop"] and len(w.posts) == n, (r2, w.posts))
    finally:
        w.close()


@scenario("release-retry")
def t_release_retry(bc, q):
    w = World(bc.__file__)
    try:
        bc.hold_for_queue()
        w.bloom_up = False
        r = bc.release_to_bloom()
        check("release with Bloom down is NOT ok and stays pending", not r["ok"] and w.state().get("phase") == "release-pending", (r, w.state()))
        w.bloom_up = True
        r2 = bc.release_to_bloom()
        check("retry succeeds", r2["ok"] and w.sv["mode"] == "on", (r2, w.sv))
    finally:
        w.close()


@scenario("api-drift")
def t_drift(bc, q):
    w = World(bc.__file__, shape="drift")
    try:
        r = bc.hold_for_queue()
        check("API shape drift: degraded but the pair still served via darkbloom start",
              r["ok"] and r.get("degraded") and len(w.starts()) == 1 and not w.posts, (r, w.posts, w.calls()))
        check("state flags degraded", w.state().get("degraded") is True, w.state())
    finally:
        w.close()
    w = World(bc.__file__, refuse_manual=True)
    try:
        r = bc.hold_for_queue()
        check("set-automatic refused -> degraded, start still runs", r["ok"] and r.get("degraded") and len(w.starts()) == 1, r)
    finally:
        w.close()
    w = World(bc.__file__, bloom_up=False)
    try:
        r = bc.hold_for_queue()
        check("BloomGauge down -> degraded hold works", r["ok"] and r.get("degraded") and len(w.starts()) == 1, r)
    finally:
        w.close()


@scenario("warn-once")
def t_warn_once(bc, q):
    """Drift-guard WARN de-noising (smoke 2026-10-09): a persistent DEGRADED condition logs ONCE
    per state change plus a heartbeat at most every 10 min; the guard's behaviour (noop verify,
    degraded flag) is unchanged."""
    bc.clear_log_seen()
    w = World(bc.__file__, bloom_up=False)
    try:
        def n_warn():
            t = (w.home / "bloom-control.log").read_text() if (w.home / "bloom-control.log").exists() else ""
            return t.count("BloomGauge unusable")
        r1 = bc.hold_for_queue()
        r2 = bc.hold_for_queue()      # the ~67s verify: already held, still degraded
        r3 = bc.hold_for_queue()
        r4 = bc.hold_for_queue()
        check("verify path behaviour unchanged (noop, degraded, ok)",
              r1["ok"] and r2["ok"] and r2.get("noop") and r2.get("degraded") and r4.get("noop"), (r1, r2, r4))
        check("4 degraded checks in a row log the WARN ONCE", n_warn() == 1, n_warn())
        cfg = bc.load_cfg()
        t0 = time.time()
        bc.clear_log_seen("hb")
        check("log_on_change: first occurrence logs", bc.log_on_change(cfg, "hb", "a", "x", now=t0) is True)
        check("...same signature inside the heartbeat window is silent",
              bc.log_on_change(cfg, "hb", "a", "x", now=t0 + 540) is False)
        check("...heartbeat after 10 min", bc.log_on_change(cfg, "hb", "a", "x", now=t0 + 601) is True)
        check("...a CHANGED signature logs immediately",
              bc.log_on_change(cfg, "hb", "b", "x2", now=t0 + 602) is True)
        bc.clear_log_seen("hb")
        check("...a cleared condition logs again", bc.log_on_change(cfg, "hb", "b", "x2", now=t0 + 603) is True)
    finally:
        w.close()


@scenario("concurrent")
def t_conc(bc, q):
    w = World(bc.__file__)
    try:
        w.start_delay.write_text("1.0")
        out = []
        ths = [threading.Thread(target=lambda: out.append(bc.hold_for_queue())) for _ in range(5)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        check("5 concurrent holds -> exactly ONE darkbloom start", len(w.starts()) == 1, w.calls())
        check("all callers ok", all(r["ok"] for r in out) and sum(1 for r in out if r["noop"]) == 4, out)
        check("exactly one Manual POST", [p for p in w.posts if p[0] == "set-automatic"] == [("set-automatic", False)], w.posts)
    finally:
        w.close()


@scenario("start-fail")
def t_fail(bc, q):
    w = World(bc.__file__, start_rc=3)
    try:
        r = bc.hold_for_queue()
        check("darkbloom start failure -> not ok, phase failed", not r["ok"] and w.state().get("phase") == "failed", (r, w.state()))
        check("secret from CLI output scrubbed from the log", KEY not in (w.home / "bloom-control.log").read_text())
    finally:
        w.close()


@scenario("disabled")
def t_disabled(bc, q):
    w = World(bc.__file__)
    try:
        (w.home / "bloom-control.disabled").write_text("")
        r = bc.hold_for_queue()
        r2 = bc.release_to_bloom()
        check("kill-switch file: both no-op, nothing touched", r.get("disabled") and r2.get("disabled") and not w.calls() and not w.posts, (r, w.calls()))
        (w.home / "bloom-control.disabled").unlink()
        os.environ["BLOOM_CONTROL"] = "0"
        r = bc.hold_for_queue()
        check("BLOOM_CONTROL=0: no-op", r.get("disabled") and not w.calls(), r)
        check("queue sync inert when disabled", q.bloom_queue_sync({"jobs": [{"id": "a", "status": "pending"}]}, lambda j: "b", ctl=bc, background=False) is None)
        os.environ.pop("BLOOM_CONTROL")
    finally:
        w.close()


@scenario("baseurl")
def t_baseurl(bc, q):
    """Darkbloom rewrites local.json WITHOUT base_url on restart: bloom_control must fall back to the
    DARKBLOOM_BASE_URL override (like the rest of the pipeline), not guess port 8000."""
    w = World(bc.__file__)
    try:
        cfg = bc.load_cfg()
        cfg["local_json"].write_text(json.dumps({"api_key": "k"}))
        os.environ["DARKBLOOM_BASE_URL"] = "http://127.0.0.1:55555/v1"
        base, key = bc.db_base_key(cfg)
        check("no base_url in local.json -> DARKBLOOM_BASE_URL (/v1 stripped)", base == "http://127.0.0.1:55555" and key == "k", (base, key))
        os.environ.pop("DARKBLOOM_BASE_URL")
        base, _ = bc.db_base_key(cfg)
        check("no override either -> default port 8000", base == "http://127.0.0.1:8000", base)
    finally:
        os.environ.pop("DARKBLOOM_BASE_URL", None)
        w.close()


@scenario("dry-run")
def t_dry(bc, q):
    w = World(bc.__file__)
    try:
        os.environ["BLOOMCTL_DRY_RUN"] = "1"
        r = bc.hold_for_queue()
        os.environ.pop("BLOOMCTL_DRY_RUN")
        check("dry-run hold changes nothing", r["ok"] and not w.posts and not w.calls()
              and "qwen3.5-9b" in w.toml.read_text() and w.sv["mode"] == "on", (r, w.posts, w.calls()))
    finally:
        w.close()


def job(i, st, **kw):
    d = {"id": "%012d" % i, "status": st, "host_pref": "auto"}
    d.update(kw)
    return d


@scenario("hold-gate")
def t_holdgate(bc, q):
    w = World(bc.__file__)
    try:
        check("no hold when state absent", q.bloom_control_hold() is None)
        bc.write_state(bc.load_cfg(), mode="queue", phase="switching")
        check("switching + fresh heartbeat -> launches WAIT (infra-wait)", bool(q.bloom_control_hold()))
        check("bloom_idle_hold surfaces it", bool(q.bloom_idle_hold()))
        check("darkbloom_model_ready False while switching",
              q.darkbloom_model_ready("Qwen3.5-9B", time.time(), probe=lambda m: True, cache={}) is False)
        check("stale heartbeat never freezes the queue", q.bloom_control_hold(now=time.time() + 4000) is None)
        bc.write_state(bc.load_cfg(), phase="ready")
        check("ready -> no hold", q.bloom_control_hold() is None)
    finally:
        w.close()


class FakeCtl:
    """Records what the queue asks of bloom_control; state file drives the decisions."""

    def __init__(self, bc):
        self.bc = bc
        self.calls = []
        self.fail = False

    def enabled(self, cfg=None):
        return True

    def load_cfg(self):
        return self.bc.load_cfg()

    def read_state(self, cfg):
        return self.bc.read_state(cfg)

    def write_state(self, cfg, **kw):
        self.bc.write_state(cfg, **kw)

    def hold_for_queue(self, cfg=None):
        self.calls.append("hold")
        self.phase_at_call = self.bc.read_state(cfg).get("phase")
        if self.fail:
            return {"ok": False, "why": "boom"}
        self.bc.write_state(cfg, mode="queue", phase="ready")
        return {"ok": True, "why": "held"}

    def release_to_bloom(self, cfg=None):
        self.calls.append("release")
        self.bc.write_state(cfg, mode="bloom", phase="idle", autoupdate_was_on=False)
        return {"ok": True, "why": "released"}


@scenario("queue-sync")
def t_sync(bc, q):
    w = World(bc.__file__)
    try:
        pk = lambda j: j.get("bundle") or j["id"]
        ctl = FakeCtl(bc)
        q._BLOOM_SYNC.update(thread=None, last_fail=0.0, last_verify=0.0)
        idle = {"jobs": []}
        check("idle + already with Bloom: nothing happens (no timer, no pin)", q.bloom_queue_sync(idle, pk, ctl=ctl, background=False) is None and not ctl.calls)
        busy = {"jobs": [job(1, "pending")]}
        k = q.bloom_queue_sync(busy, pk, ctl=ctl, background=False)
        check("empty -> non-empty: hold fires once", k == "hold" and ctl.calls == ["hold"], (k, ctl.calls))
        check("switching marker written synchronously BEFORE the hold runs (same-tick launches wait)", getattr(ctl, "phase_at_call", None) == "switching", getattr(ctl, "phase_at_call", None))
        q.bloom_queue_sync(busy, pk, ctl=ctl, background=False)
        check("steady busy: no second hold", ctl.calls == ["hold"], ctl.calls)
        # pending behind a USER-HELD dependency is not work
        held = {"jobs": [job(2, "held", user_hold=True), job(3, "pending", after="000000000002")]}
        check("pending behind a held dependency is not lane work", q.bloom_lane_work(held["jobs"]) == [], q.bloom_lane_work(held["jobs"]))
        done = {"jobs": [job(1, "done")]}
        k = q.bloom_queue_sync(done, pk, ctl=ctl, background=False)
        check("queue done: release fires immediately", k == "release" and ctl.calls == ["hold", "release"], (k, ctl.calls))
        k = q.bloom_queue_sync(done, pk, ctl=ctl, background=False)
        check("after release: idempotent, nothing more", k is None and ctl.calls == ["hold", "release"], (k, ctl.calls))
        # failure backoff
        ctl.fail = True
        bc.write_state(bc.load_cfg(), mode="bloom", phase="idle")
        q.bloom_queue_sync(busy, pk, ctl=ctl, background=False)
        n = len(ctl.calls)
        k = q.bloom_queue_sync(busy, pk, ctl=ctl, background=False)
        check("failed hold: backs off, does not hammer", k is None and len(ctl.calls) == n, ctl.calls)
    finally:
        w.close()


@scenario("queue-done-signals")
def t_done(bc, q):
    pk = lambda j: j.get("bundle") or j["id"]
    td = Path(tempfile.mkdtemp(prefix="bctl-q-"))
    try:
        base = {"jobs": [job(1, "done", bundle="B")]}
        rem, why = q.bloom_queue_work_remaining(base, pk, runs_dir=td, chain_dir=td, hooks={})
        check("nothing live: queue done", rem is False, why)
        rem, why = q.bloom_queue_work_remaining({"jobs": [job(1, "pending", bundle="B")]}, pk, runs_dir=td, chain_dir=td, hooks={})
        check("pending row: work remains", rem is True, why)
        hooks = {"j1": {"bundle": "B", "at": time.time(), "proc": None}}
        rem, why = q.bloom_queue_work_remaining(base, pk, runs_dir=td, chain_dir=td, hooks=hooks)
        check("gate-on-complete hook in flight: work remains", rem is True, why)
        (td / "B.advance.lock").write_text(json.dumps({"pid": os.getpid()}))
        (td / "B.json").write_text(json.dumps({"label": "B", "order": ["s1"], "slices": {"s1": {"status": "pending"}}}))
        rem, why = q.bloom_queue_work_remaining(base, pk, runs_dir=td, chain_dir=td, hooks={})
        check("slicer advance in flight (live lock): work remains", rem is True, why)
        (td / "B.advance.lock").unlink()
        (td / "B.json").unlink()
        st = {"jobs": [job(1, "done", bundle="B")], "_bundle_parked": {"B": {"kind": "blocked"}}}
        rem, why = q.bloom_queue_work_remaining(st, pk, runs_dir=td, chain_dir=td, hooks={})
        check("a parked/blocked bundle with nothing runnable does not hold Darkbloom", rem is False, why)
        st = {"jobs": [job(1, "held", bundle="B", fit_hold=True)]}
        rem, why = q.bloom_queue_work_remaining(st, pk, runs_dir=td, chain_dir=td, hooks={})
        check("a fit-held row does not hold Darkbloom", rem is False, why)
    finally:
        shutil.rmtree(td, ignore_errors=True)


@scenario("parked-lanes")
def t_parked(bc, q):
    parked = {"P": {"kind": "blocked"}}
    check("parked sticky bundle does not keep the lanes", q.focus_drop_parked("P", parked, None, None, ["P", "N"]) == "N")
    check("parked + nothing else -> None", q.focus_drop_parked("P", parked, None, None, ["P"]) is None)
    check("running parked bundle keeps its lane", q.focus_drop_parked("P", parked, "P", None, ["N"]) == "P")
    check("human override on a parked bundle is honoured", q.focus_drop_parked("P", parked, None, "P", ["N"]) == "P")
    check("non-parked untouched", q.focus_drop_parked("N", parked, None, None, ["P"]) == "N")
    pk = lambda j: j.get("bundle")
    jobs = [job(1, "held", bundle="E", fit_hold=True), job(2, "pending", bundle="E")]
    st, why, _m = q.bundle_commit_status("E", [jobs[0]], pk, {}, {})
    check("bundle whose only live row is fit-held is BLOCKED (parks, releases lanes), not working", st == "blocked", (st, why))
    jobs2 = [job(1, "held", bundle="E", user_hold=True)]
    st, why, _m = q.bundle_commit_status("E", jobs2, pk, {}, {})
    check("user-held only -> blocked", st == "blocked", (st, why))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default=str(HERE / "ollama-queue.py"))
    ap.add_argument("--bc", default=str(HERE / "bloom_control.py"))
    ap.add_argument("--only", action="append", default=[])
    a = ap.parse_args()
    os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
    os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
    bc = load(a.bc, "bloom_control_t")
    q = load(a.queue, "oq_t")
    # the queue loads bloom_control.py from its own dir; point it at the one under test
    q._BLOOM_CTL_MOD[:] = [bc]
    for fn in SEL:
        if a.only and fn._name not in a.only:
            continue
        print("--- " + fn._name)
        try:
            fn(bc, q)
        except Exception as e:   # an API the reverted code lacks is a FAIL, not a crash
            import traceback
            traceback.print_exc()
            check(fn._name + " ran without exception", False, repr(e))
    if FAILS:
        print("\nFAILED: %d\n  " % len(FAILS) + "\n  ".join(FAILS))
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
