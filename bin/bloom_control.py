#!/usr/bin/env python3
"""bloom_control: event-driven hand-off of the Darkbloom provider between the ollama queue
and BloomGauge (earnings). Replaces the 10-minute idle timer of bloom-idle-switch.py.

  hold_for_queue()    the queue has work. BloomGauge -> Manual (so it cannot switch models
                      away), Darkbloom auto-update -> off, provider.toml -> max_model_slots=2 /
                      idle_timeout_mins=0 / preload_models=pair, `darkbloom start --model A
                      --model B --idle-timeout 0 --local-endpoint --timeout <drain>` (skipped when
                      the pair is already served), then wait for /health + /v1/models +
                      loaded-models.json to agree on exactly the pair.
  release_to_bloom()  the queue is done. BloomGauge release-pin + set-automatic enabled:true,
                      Darkbloom auto-update restored if hold_for_queue turned it off.
  get_state()         read-only snapshot (never mutates).

Both mutators are idempotent (already in the wanted state -> no-op), serialised by one flock so
concurrent callers never double-switch (the second caller waits, re-reads, and no-ops), and have
bounded timeouts. Transitions are driven by the queue daemon (ollama-queue.py bloom_queue_sync);
there is NO timer here.

BloomGauge's control API (POST /api/optimizer/control, X-Bloom-Action: optimizer; actions
set-automatic{enabled}, release-pin; every call needs the controlVersion of a fresh
GET /api/optimizer/control) is UNDOCUMENTED and may change when the app auto-updates. Every
response is shape-checked (controlVersion str, automatic.mode in {on, manual}); on any mismatch
the Bloom half is skipped (DEGRADED: `darkbloom start` only), state.degraded is set and the
queue daemon prints a loud alert. The queue is never blocked by a Bloom problem.

Kill switch (integration OFF -> every function is a no-op returning {"disabled": True}):
  env BLOOM_CONTROL=0, or the file ~/.ollama-dispatch/bloom-control.disabled.
State: ~/.ollama-dispatch/bloom-control-state.json (heartbeat-stamped; ollama-queue.py's
bloom_idle_hold makes launches WAIT as infra-wait while phase == "switching").
Log:   ~/.ollama-dispatch/bloom-control.log (secrets scrubbed).
Env for tests: BLOOMCTL_HOME, BLOOMCTL_BLOOM_URL, BLOOMCTL_DARKBLOOM_BIN, BLOOMCTL_PROVIDER_TOML,
BLOOMCTL_LOCAL_JSON, BLOOMCTL_LOADED_JSON, BLOOMCTL_PAIR (a,b), BLOOMCTL_DRAIN_S, BLOOMCTL_WAIT_S,
BLOOMCTL_POLL_S, BLOOMCTL_FORCE=1 (darkbloom start --force), BLOOMCTL_DRY_RUN=1.
"""
import argparse
import datetime
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DEFAULT_PAIR = ["Qwen3.5-9B", "qwen3.6-35b-a3b-vl-mtp-mxfp8"]
_SECRETISH = re.compile(r"[A-Za-z0-9_\-]{28,}")


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def load_cfg():
    home = Path(_env("BLOOMCTL_HOME", str(Path.home() / ".ollama-dispatch")))
    pair = [m for m in _env("BLOOMCTL_PAIR", ",".join(DEFAULT_PAIR)).split(",") if m]
    return {
        "home": home,
        "state": home / "bloom-control-state.json",
        "log": home / "bloom-control.log",
        "lock": home / "bloom-control.lock",
        "disabled_file": home / "bloom-control.disabled",
        "bloom": _env("BLOOMCTL_BLOOM_URL", "http://127.0.0.1:8765").rstrip("/"),
        "bin": _env("BLOOMCTL_DARKBLOOM_BIN") or shutil.which("darkbloom")
               or str(Path.home() / ".darkbloom/bin/darkbloom"),
        "toml": Path(_env("BLOOMCTL_PROVIDER_TOML", str(Path.home() / ".config/darkbloom/provider.toml"))),
        "local_json": Path(_env("BLOOMCTL_LOCAL_JSON", str(Path.home() / ".darkbloom/local.json"))),
        "loaded_json": Path(_env("BLOOMCTL_LOADED_JSON", str(Path.home() / ".darkbloom/loaded-models.json"))),
        "pair": pair,
        "drain_s": int(float(_env("BLOOMCTL_DRAIN_S", 60))),   # darkbloom start --timeout
        "wait_s": float(_env("BLOOMCTL_WAIT_S", 900)),         # pair must be served within this
        "poll_s": float(_env("BLOOMCTL_POLL_S", 3)),
        "lock_wait_s": float(_env("BLOOMCTL_LOCK_WAIT", 1200)),
        "force": _env("BLOOMCTL_FORCE") == "1",
        "dry_run": _env("BLOOMCTL_DRY_RUN") == "1",
        "http_timeout": float(_env("BLOOMCTL_HTTP_TIMEOUT", 8)),
    }


def enabled(cfg=None):
    cfg = cfg or load_cfg()
    return _env("BLOOM_CONTROL", "1") != "0" and not cfg["disabled_file"].exists()


def scrub(text, limit=300):
    return _SECRETISH.sub("[redacted]", str(text))[:limit].replace("\n", " ")


def log(cfg, msg, level="INFO"):
    line = "%s %-5s %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, scrub(msg, 600))
    print(line, flush=True)
    try:
        cfg["log"].parent.mkdir(parents=True, exist_ok=True)
        with open(cfg["log"], "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# Drift-guard WARN de-noising (2026-10-09 smoke): the queue re-verifies the hold every ~67s,
# and a DEGRADED hold (BloomGauge closed) re-logged the same WARN each time for hours. The
# guard's BEHAVIOUR is untouched; only the LOGGING of an unchanged condition is limited: log
# when the condition (key, signature) changes, plus a heartbeat at most every
# WARN_HEARTBEAT_S while it persists. `_LOG_SEEN` is per process (the queue daemon holds one).
WARN_HEARTBEAT_S = 600.0
_LOG_SEEN = {}


def log_on_change(cfg, key, sig, msg, level="WARN", now=None, heartbeat_s=None):
    """log() once per state change of `key` (signature `sig`) and at most every
    `heartbeat_s` while the same signature persists. -> True if a line was written."""
    now = time.time() if now is None else now
    hb = WARN_HEARTBEAT_S if heartbeat_s is None else heartbeat_s
    last = _LOG_SEEN.get(key)
    if last is not None and last[0] == sig and now - last[1] < hb:
        return False
    _LOG_SEEN[key] = (sig, now)
    log(cfg, msg + (" [still: heartbeat]" if last is not None and last[0] == sig else ""), level)
    return True


def clear_log_seen(key=None):
    """Forget a condition (it cleared) so its next occurrence logs immediately."""
    if key is None:
        _LOG_SEEN.clear()
    else:
        _LOG_SEEN.pop(key, None)


# ------------------------------------------------------------------ state file
def read_state(cfg):
    try:
        st = json.loads(cfg["state"].read_text())
        if isinstance(st, dict):
            return st
    except (OSError, ValueError):
        pass
    return {"mode": "bloom", "phase": "idle"}


def write_state(cfg, **upd):
    """Merge `upd` into the state file (atomic) and stamp the heartbeat."""
    if cfg["dry_run"]:
        return
    st = read_state(cfg)
    st.update(upd)
    st["heartbeat"] = time.time()
    cfg["state"].parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg["state"].with_name(cfg["state"].name + ".tmp-%d" % os.getpid())
    tmp.write_text(json.dumps(st, indent=1, sort_keys=True))
    os.replace(tmp, cfg["state"])


# ------------------------------------------------------------------ BloomGauge API
class BloomDown(Exception):
    pass


class BloomShape(Exception):
    """The control API answered but not in the shape this module understands (API drift)."""


def _http(cfg, method, path, body=None, headers=None):
    req = urllib.request.Request(cfg["bloom"] + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=cfg["http_timeout"]) as r:
            raw = r.read() or b"{}"
            return r.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except ValueError:
            payload = {}
        return e.code, payload
    except ValueError as e:                    # non-JSON 200
        raise BloomShape("non-JSON response: %s" % scrub(e))
    except (urllib.error.URLError, OSError) as e:
        raise BloomDown(scrub(e))


def bloom_view(cfg):
    """-> {"control_version", "mode" ('on'|'manual'), "can_enable", "provider_running",
    "provider_version", "pinned", "serving"}. Raises BloomDown / BloomShape."""
    s, ctl = _http(cfg, "GET", "/api/optimizer/control")
    if s != 200:
        raise BloomShape("GET /api/optimizer/control HTTP %s" % s)
    auto = ctl.get("automatic") if isinstance(ctl, dict) else None
    if (not isinstance(ctl, dict) or not isinstance(ctl.get("controlVersion"), str)
            or not isinstance(auto, dict) or auto.get("mode") not in ("on", "manual")):
        raise BloomShape("control view lacks controlVersion/automatic.mode (BloomGauge API changed?)")
    mgr = ctl.get("manager") if isinstance(ctl.get("manager"), dict) else {}
    return {
        "control_version": ctl["controlVersion"],
        "mode": auto["mode"],
        "can_enable": bool(auto.get("canEnable")),
        "provider_running": bool(ctl.get("providerRunning")),
        "provider_version": ctl.get("providerVersion"),
        "pinned": bool(mgr.get("pinned")),
        "serving": ctl.get("currentModel"),
    }


def bloom_post(cfg, body):
    """POST a control action. -> (ok, why). Shape-checks the answer."""
    s, r = _http(cfg, "POST", "/api/optimizer/control", body,
                 {"X-Bloom-Action": "optimizer", "Content-Type": "application/json"})
    if not isinstance(r, dict):
        raise BloomShape("POST answer is not an object")
    err = r.get("error")
    if s == 200 and not err:
        if not isinstance(r.get("controlVersion"), str):
            raise BloomShape("POST answer lacks controlVersion")
        return True, "ok"
    return False, scrub(err or "HTTP %s" % s)


def bloom_set(cfg, action, **extra):
    """One control call with a fresh controlVersion. -> (ok, why)."""
    v = bloom_view(cfg)
    body = {"action": action, "requestId": str(uuid.uuid4()), "expectedControl": v["control_version"]}
    body.update(extra)
    if action == "set-automatic" and extra.get("enabled") and not v["provider_running"]:
        body["expectedProvider"] = v["provider_version"]
    if cfg["dry_run"]:
        log(cfg, "DRY-RUN: POST /api/optimizer/control %s" % json.dumps(
            {k: body[k] for k in body if k not in ("requestId", "expectedControl", "expectedProvider")}))
        return True, "dry-run"
    return bloom_post(cfg, body)


# ------------------------------------------------------------------ Darkbloom
def db_base_key(cfg):
    try:
        rec = json.loads(cfg["local_json"].read_text())
    except (OSError, ValueError):
        return None, None
    # local.json loses base_url when Darkbloom restarts (canary models this): fall back to the same
    # DARKBLOOM_BASE_URL override the rest of the pipeline honours before guessing port 8000.
    base = str(rec.get("base_url") or _env("DARKBLOOM_BASE_URL") or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return (base or "http://127.0.0.1:%s" % (rec.get("port") or 8000)), rec.get("api_key")


def db_health(cfg):
    base, _ = db_base_key(cfg)
    if not base:
        return False
    try:
        with urllib.request.urlopen(base + "/health", timeout=5) as r:
            return r.status == 200 and json.loads(r.read()).get("status") == "ok"
    except (urllib.error.URLError, OSError, ValueError):
        return False


def db_listed(cfg):
    base, key = db_base_key(cfg)
    if not base:
        return None
    try:
        req = urllib.request.Request(base + "/v1/models", headers={"Authorization": "Bearer " + str(key or "")})
        with urllib.request.urlopen(req, timeout=5) as r:
            return [str(m.get("id")) for m in json.loads(r.read()).get("data", [])]
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        return None


def toml_text(cfg):
    try:
        return cfg["toml"].read_text()
    except OSError:
        return ""


def toml_enabled(cfg):
    out = []
    for m in re.finditer(r"(?m)^\s*enabled_models\s*=\s*\[([^\]]*)\]", toml_text(cfg)):
        out += re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))
    return out


def toml_auto_update(cfg):
    m = re.search(r"(?m)^\s*auto_update\s*=\s*(true|false)", toml_text(cfg))
    return None if not m else m.group(1) == "true"


def loaded_models(cfg):
    try:
        return [str(x) for x in (json.loads(cfg["loaded_json"].read_text()).get("models") or [])]
    except (OSError, ValueError, AttributeError):
        return []


def same_models(a, b):
    return {str(x).lower() for x in a} == {str(x).lower() for x in b}


def pair_ready(cfg):
    """(ok, why): provider.toml, /health, /v1/models and loaded-models.json agree on exactly the pair."""
    pair = cfg["pair"]
    if not same_models(toml_enabled(cfg), pair):
        return False, "provider.toml enabled_models=%s" % toml_enabled(cfg)
    if not db_health(cfg):
        return False, "/health not ok"
    listed = db_listed(cfg)
    if listed is None or not same_models(listed, pair):
        return False, "/v1/models=%s" % listed
    if not {m.lower() for m in pair} <= {m.lower() for m in loaded_models(cfg)}:
        return False, "loaded-models.json=%s" % loaded_models(cfg)
    return True, "ok"


def desired_toml(text, pair):
    """PURE. provider.toml text with [backend] max_model_slots=2, idle_timeout_mins=0 and
    preload_models=pair (canonical-case ids). Returns (new_text, [changes])."""
    changes = []

    def setkey(src, key, new_line):
        pat = re.compile(r"(?m)^(\s*)%s\s*=\s*.*$" % re.escape(key))
        m = pat.search(src)
        if m:
            if m.group(0).strip() != new_line:
                changes.append("%s: %s -> %s" % (key, m.group(0).strip(), new_line))
                return pat.sub(lambda mm: mm.group(1) + new_line, src, count=1)
            return src
        changes.append("%s: (absent) -> %s" % (key, new_line))
        return re.sub(r"(?m)^(\[backend\]\s*\n)", lambda mm: mm.group(1) + new_line + "\n", src, count=1)

    text = setkey(text, "max_model_slots", "max_model_slots = 2")
    text = setkey(text, "idle_timeout_mins", "idle_timeout_mins = 0")
    text = setkey(text, "preload_models", "preload_models = [ %s ]" % ", ".join("'%s'" % m for m in pair))
    return text, changes


def run_cli(cfg, args, timeout):
    """Run the darkbloom CLI. -> (rc, scrubbed tail). Never raises."""
    cmd = [cfg["bin"]] + list(args)
    if cfg["dry_run"]:
        log(cfg, "DRY-RUN: " + " ".join([Path(cfg["bin"]).name] + list(args)))
        return 0, "dry-run"
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, scrub((p.stdout + p.stderr).strip()[-300:])
    except subprocess.TimeoutExpired:
        return 124, "timed out after %ss" % timeout
    except OSError as e:
        return 127, scrub(e)


# ------------------------------------------------------------------ get_state
def get_state(cfg=None):
    """Read-only snapshot. Never mutates anything."""
    cfg = cfg or load_cfg()
    st = read_state(cfg)
    out = {"enabled": enabled(cfg), "control": {k: st.get(k) for k in
                                                  ("mode", "phase", "degraded", "last_error", "since")},
           "bloom": None, "bloom_error": None}
    try:
        out["bloom"] = bloom_view(cfg)
    except BloomDown as e:
        out["bloom_error"] = "down: %s" % e
    except BloomShape as e:
        out["bloom_error"] = "shape: %s" % e
    ok, why = pair_ready(cfg)
    out.update(pair_ready=ok, pair_why=why, auto_update=toml_auto_update(cfg))
    return out


# ------------------------------------------------------------------ locking
class _Lock:
    """One flock for every mutator. Waits (bounded) so a concurrent caller re-reads state
    after the first finishes and no-ops instead of double-switching."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.fh = None

    def __enter__(self):
        c = self.cfg
        c["lock"].parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(c["lock"], "w")
        end = time.monotonic() + c["lock_wait_s"]
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() > end:
                    self.fh.close()
                    raise TimeoutError("bloom-control lock busy")
                time.sleep(0.2)

    def __exit__(self, *a):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()


def _beat(cfg, **kw):
    write_state(cfg, **kw)


# ------------------------------------------------------------------ hold_for_queue
def hold_for_queue(cfg=None):
    """Make Darkbloom serve exactly the queue's pair, loaded, with BloomGauge on Manual.
    -> {"ok": bool, "noop": bool, "degraded": bool, "why": str}. Never raises."""
    cfg = cfg or load_cfg()
    if not enabled(cfg):
        return {"ok": True, "noop": True, "disabled": True, "why": "integration disabled"}
    try:
        with _Lock(cfg):
            return _hold_locked(cfg)
    except TimeoutError as e:
        return {"ok": False, "noop": False, "why": str(e)}
    except Exception as e:      # never take the caller down
        log(cfg, "hold_for_queue crashed: %r" % e, "ERROR")
        write_state(cfg, phase="failed", last_error=scrub(repr(e)))
        return {"ok": False, "noop": False, "why": scrub(repr(e))}


def _hold_locked(cfg):
    t0 = time.monotonic()
    degraded, notes = False, []
    view = None
    try:
        view = bloom_view(cfg)
    except (BloomDown, BloomShape) as e:
        degraded = True
        notes.append("bloom %s" % e)
        log_on_change(cfg, "bloom-unusable", str(e),
                      "BloomGauge unusable (%s): DEGRADED -- darkbloom start only" % e)
    else:
        clear_log_seen("bloom-unusable")
    ready, why = pair_ready(cfg)
    manual = view is not None and view["mode"] == "manual"
    plan_edits = desired_toml(toml_text(cfg), cfg["pair"])[1]
    au_on = toml_auto_update(cfg)
    if ready and (manual or degraded) and not plan_edits and au_on is not True \
            and read_state(cfg).get("mode") == "queue" and read_state(cfg).get("phase") == "ready":
        return {"ok": True, "noop": True, "degraded": degraded, "why": "already held"}
    log(cfg, "HOLD: begin (pair ready=%s [%s], bloom=%s, toml edits=%d, auto_update=%s)" % (
        ready, why, "unreachable" if view is None else view["mode"], len(plan_edits), au_on))
    _beat(cfg, mode="queue", phase="switching", since=time.time(), degraded=degraded, last_error=None)

    # 1) BloomGauge -> Manual (before touching the provider, so it cannot fight the switch)
    if view is not None and view["mode"] != "manual":
        try:
            ok, w = bloom_set(cfg, "set-automatic", enabled=False)
            if ok and not cfg["dry_run"]:
                v2 = bloom_view(cfg)
                ok, w = (v2["mode"] == "manual"), "mode still %s after set-automatic" % v2["mode"]
            if not ok:
                degraded = True
                notes.append("set-automatic false refused: %s" % w)
                log_on_change(cfg, "set-automatic-refused", str(w),
                              "set-automatic enabled:false failed (%s): continuing, DEGRADED" % w)
        except (BloomDown, BloomShape) as e:
            degraded = True
            notes.append("manual: %s" % e)
            log_on_change(cfg, "set-automatic-down", str(e),
                          "set-automatic enabled:false: %s: continuing, DEGRADED" % e)

    # 2) Darkbloom auto-update off for the busy period (remember it was on)
    if au_on:
        rc, tail = run_cli(cfg, ["autoupdate", "disable"], 30)
        if rc == 0:
            _beat(cfg, autoupdate_was_on=True)
            log(cfg, "darkbloom auto-update disabled for the busy period")
        else:
            log(cfg, "darkbloom autoupdate disable failed rc=%s %s" % (rc, tail), "WARN")

    # 3) provider.toml + start (only when needed)
    if plan_edits and not cfg["dry_run"]:
        new, changes = desired_toml(toml_text(cfg), cfg["pair"])
        bak = cfg["toml"].with_name(cfg["toml"].name + ".bak-bloomctl-%d" % int(time.time()))
        try:
            shutil.copy2(cfg["toml"], bak)
            tmp = cfg["toml"].with_name(cfg["toml"].name + ".tmp-bloomctl")
            tmp.write_text(new)
            os.replace(tmp, cfg["toml"])
            log(cfg, "provider.toml: " + "; ".join(changes))
        except OSError as e:
            log(cfg, "provider.toml edit failed: %s" % scrub(e), "WARN")
    elif plan_edits:
        log(cfg, "DRY-RUN: provider.toml edits: " + "; ".join(plan_edits))
    ready, why = pair_ready(cfg)
    if not ready:
        args = ["start"] + [a for m in cfg["pair"] for a in ("--model", m)] + [
            "--idle-timeout", "0", "--local-endpoint", "--timeout", str(cfg["drain_s"])]
        if cfg["force"]:
            args.append("--force")
        log(cfg, "pair not served (%s): darkbloom start (drain timeout %ss)" % (why, cfg["drain_s"]))
        rc, tail = run_cli(cfg, args, cfg["drain_s"] + 300)
        log(cfg, "darkbloom start rc=%s %s" % (rc, tail), "INFO" if rc == 0 else "ERROR")
        if rc != 0 and not cfg["dry_run"]:
            write_state(cfg, mode="queue", phase="failed", last_error="darkbloom start rc=%s %s" % (rc, tail))
            return {"ok": False, "noop": False, "degraded": degraded, "why": "darkbloom start failed: %s" % tail}
    # 4) wait for the pair
    if cfg["dry_run"]:
        return {"ok": True, "noop": False, "degraded": degraded, "why": "dry-run", "notes": notes}
    deadline = time.monotonic() + cfg["wait_s"]
    while True:
        ready, why = pair_ready(cfg)
        if ready:
            break
        if time.monotonic() > deadline:
            write_state(cfg, mode="queue", phase="failed", last_error="pair not served: %s" % why)
            log(cfg, "HOLD FAILED: pair not served within %ds (%s)" % (cfg["wait_s"], why), "ERROR")
            return {"ok": False, "noop": False, "degraded": degraded, "why": "pair not served: %s" % why}
        _beat(cfg)
        time.sleep(cfg["poll_s"])
    write_state(cfg, mode="queue", phase="ready", degraded=degraded, last_error="; ".join(notes) or None,
                ready_at=time.time(), hold_seconds=round(time.monotonic() - t0, 1))
    log(cfg, "HOLD: pair served and Bloom %s in %.1fs%s" % (
        "unmanaged (DEGRADED)" if degraded else "Manual", time.monotonic() - t0,
        " [DEGRADED: " + "; ".join(notes) + "]" if degraded else ""))
    return {"ok": True, "noop": False, "degraded": degraded, "why": "held", "notes": notes}


# ------------------------------------------------------------------ release_to_bloom
def release_to_bloom(cfg=None):
    """Hand control back to BloomGauge (earnings) immediately. Idempotent.
    -> {"ok": bool, "noop": bool, "why": str}. Never raises. On a Bloom failure the state
    stays phase=release-pending so the caller can retry on its next event."""
    cfg = cfg or load_cfg()
    if not enabled(cfg):
        return {"ok": True, "noop": True, "disabled": True, "why": "integration disabled"}
    try:
        with _Lock(cfg):
            return _release_locked(cfg)
    except TimeoutError as e:
        return {"ok": False, "noop": False, "why": str(e)}
    except Exception as e:
        log(cfg, "release_to_bloom crashed: %r" % e, "ERROR")
        return {"ok": False, "noop": False, "why": scrub(repr(e))}


def _release_locked(cfg):
    st = read_state(cfg)
    try:
        view = bloom_view(cfg)
    except (BloomDown, BloomShape) as e:
        view = None
        log(cfg, "release: BloomGauge unusable (%s)" % e, "WARN")
    au_was_on = bool(st.get("autoupdate_was_on"))
    bloom_ok = view is not None and view["mode"] == "on" and not view["pinned"]
    if st.get("mode") != "queue" and not au_was_on and (view is None or bloom_ok):
        return {"ok": True, "noop": True, "why": "already with BloomGauge"}
    log(cfg, "RELEASE: begin (bloom=%s pinned=%s auto_update_restore=%s)" % (
        "unreachable" if view is None else view["mode"], None if view is None else view["pinned"], au_was_on))
    _beat(cfg, phase="releasing")
    err = None
    if view is None:
        err = "BloomGauge unreachable"
    else:
        try:
            if view["pinned"]:
                ok, w = bloom_set(cfg, "release-pin")
                if not ok:
                    log(cfg, "release-pin refused (%s): continuing" % w, "WARN")
            v2 = bloom_view(cfg)
            if v2["mode"] != "on":
                ok, w = bloom_set(cfg, "set-automatic", enabled=True)
                if not ok:
                    err = "set-automatic true refused: %s" % w
                elif not cfg["dry_run"]:
                    v3 = bloom_view(cfg)
                    if v3["mode"] != "on":
                        err = "mode still %s after set-automatic true" % v3["mode"]
        except (BloomDown, BloomShape) as e:
            err = "bloom: %s" % e
    if au_was_on:
        rc, tail = run_cli(cfg, ["autoupdate", "enable"], 30)
        if rc == 0:
            _beat(cfg, autoupdate_was_on=False)
            log(cfg, "darkbloom auto-update re-enabled")
        else:
            log(cfg, "darkbloom autoupdate enable failed rc=%s %s" % (rc, tail), "WARN")
            err = err or "autoupdate enable failed"
    if err:
        write_state(cfg, mode="queue", phase="release-pending", last_error=err)
        log(cfg, "RELEASE incomplete (%s); will retry on the next queue event" % err, "WARN")
        return {"ok": False, "noop": False, "why": err}
    write_state(cfg, mode="bloom", phase="idle", last_error=None, released_at=time.time(), degraded=False)
    log(cfg, "RELEASE: BloomGauge is Automatic again")
    return {"ok": True, "noop": False, "why": "released"}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=("state", "hold", "release"))
    ap.add_argument("--dry-run", action="store_true", help="log intended actions, change nothing")
    a = ap.parse_args(argv)
    if a.dry_run:
        os.environ["BLOOMCTL_DRY_RUN"] = "1"
    cfg = load_cfg()
    if a.cmd == "state":
        print(json.dumps(get_state(cfg), indent=1, sort_keys=True, default=str))
        return 0
    r = hold_for_queue(cfg) if a.cmd == "hold" else release_to_bloom(cfg)
    print(json.dumps(r, sort_keys=True))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
