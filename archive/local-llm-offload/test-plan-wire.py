#!/usr/bin/env python3
"""test-plan-wire.py -- CROSS-MODULE tests for the resolved-lane PLAN (hardening Wave 2).

Runs the REAL queue _build_cmd + plan_runtime launch hooks (exactly what the daemon runs), then the REAL
ollama-worker.py with that argv against a STUB OpenAI/Darkbloom server that records every request. Sandboxed
HOME (no real ~/.darkbloom), no GPU, no model, no real queue.

  (b) ENFORCE worker, every vendor file DELETED for the whole run: right host/path/model/fields/Bearer key on
      every request, and an audit hook proves no vendor file was opened after the plan loaded.
  (c) HTTP 401 with a rotated key: exactly one refresh_key, one uncharged retry, new fp logged, key never.
  (d) changed base on 401: paused exit, lane_cause plan_expired (the queue relaunches with a fresh plan).
  (e) the key literal is absent from ps argv, the child env, the job log, the plan file, the conformance
      files and the mismatch file; the daemon-style launch (env -i, Popen pass_fds) delivers it.
  (f) pid mismatch local.json vs daemon-state: shadow logs would_block, enforce raises LaneUnavailable
      (the queue turns that into an INFRA WAIT).
  S   SHADOW: legacy decides; a rotated key on disk is logged as info; zero 'mismatch' rows when consistent.
  K   kill switches: LANE_PLAN=0 / WORKER_PLAN=0 / CONFORMANCE_LOG=0.
  R   reap_check: a clean attempt is ok; injected host/model/key drift is classified; GAP when records are lost.

Usage: python3 test-plan-wire.py [--only NAME]      prints PLAN_WIRE_OK"""
import http.server
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(os.environ.get("BIN", Path(__file__).resolve().parent)).resolve()
TMP = Path(tempfile.mkdtemp(prefix="plan-wire-"))
HOME = TMP / "home"
(HOME / ".darkbloom").mkdir(parents=True)
(HOME / ".config" / "darkbloom").mkdir(parents=True)
(HOME / ".ollama-dispatch").mkdir(parents=True)
os.environ["HOME"] = str(HOME)                       # BEFORE any import: lane_plan/queue read ~ at import
for _k in list(os.environ):
    if _k.startswith(("DARKBLOOM_", "BONSAI_", "OLLAMA_DISPATCH", "LANE_PLAN", "WORKER_PLAN", "CONFORMANCE")):
        del os.environ[_k]
sys.path.insert(0, str(BIN))
import conformance_log as CL                          # noqa: E402
import lane_plan as LP                                # noqa: E402
import plan_runtime as PR                             # noqa: E402
q = SourceFileLoader("q_planwire", str(BIN / "ollama-queue.py")).load_module()

LOCAL = HOME / ".darkbloom" / "local.json"
DSTATE = HOME / ".darkbloom" / "daemon-state.json"
LOADED = HOME / ".darkbloom" / "loaded-models.json"
TOML = HOME / ".config" / "darkbloom" / "provider.toml"
CONF = CL.DEFAULT_DIR
PLANS = PR.PLANS_DIR
KEY1 = "SENTINEL-PLANWIRE-KEY-ONE-aaaaaaaaaaaaaaaa"
KEY2 = "SENTINEL-PLANWIRE-KEY-TWO-bbbbbbbbbbbbbbbb"
BARE = "qwen3.6-35b-a3b-vl-mtp-mxfp8"
FAILS = []
ONLY = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None


def chk(name, cond, detail=""):
    if not cond:
        FAILS.append(name)
    print(("ok   - " if cond else "FAIL - ") + name + ("" if cond else f": {str(detail)[:500]}"))


class Stub(http.server.BaseHTTPRequestHandler):
    rec = []                 # (path, authorization, body)
    key = None               # required Bearer key on /v1/*
    chat_n = 0
    on_first_chat = None     # callable run on the first /v1/chat/completions that is NOT the preflight; answers 401
    delay_s = 0.0

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        Stub.rec.append((self.path, self.headers.get("Authorization"), None))
        if self.path.startswith("/health"):
            return self._send({"status": "ok"})
        self._send({"data": [{"id": BARE}], "models": [{"name": BARE, "model": BARE, "size": 1, "size_vram": 1}]})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        auth = self.headers.get("Authorization")
        Stub.rec.append((self.path, auth, body))
        if Stub.delay_s:
            time.sleep(Stub.delay_s)
        if Stub.key and auth != "Bearer " + Stub.key:
            return self._send({"error": {"message": "unauthorized"}}, 401)
        if self.path.startswith("/v1/chat/completions"):
            Stub.chat_n += 1
            if Stub.on_first_chat and Stub.chat_n == 2:        # 1 = preflight, 2 = the first dispatch turn
                cb, Stub.on_first_chat = Stub.on_first_chat, None
                cb()
                return self._send({"error": {"message": "unauthorized (rotated)"}}, 401)
            tc = {"id": "c1", "type": "function",
                  "function": {"name": "task_complete", "arguments": json.dumps({"summary": "done"})}}
            return self._send({"model": body.get("model"), "choices": [{"message": {"role": "assistant", "content": "",
                                                                                    "tool_calls": [tc]},
                                                                        "finish_reason": "tool_calls"}],
                               "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}})
        self._send({"status": "ok"})


class TS(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


srv = TS(("127.0.0.1", 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()
STUB = f"http://127.0.0.1:{srv.server_address[1]}"


def write_vendor(key=KEY1, pid=4242, base=None, gen_pid=None):
    LOCAL.write_text(json.dumps({"api_key": key, "base_url": (base or STUB) + "/v1", "port": 8000, "pid": pid,
                                 "version": "0.9.20", "updated_at": "2026-10-14T00:00:00Z"}))
    DSTATE.write_text(json.dumps({"schema": 1, "version": "0.9.20", "pid": gen_pid or pid,
                                  "process_identity": {"pid": gen_pid or pid, "start_time_micros": 1791934508807052},
                                  "advertised_models": [BARE]}))
    TOML.write_text(f"[backend]\nenabled_models = [ '{BARE}' ]\npreload_models = [ '{BARE}' ]\n")
    LOADED.write_text(json.dumps({"models": [BARE], "schema": 1}))


def wipe_vendor():
    for p in (LOCAL, DSTATE, LOADED, TOML):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def reset(key=KEY1):
    Stub.rec, Stub.key, Stub.chat_n, Stub.on_first_chat, Stub.delay_s = [], key, 0, None, 0.0
    for d in (CONF, PLANS):
        if d.exists():
            for p in d.glob("*"):
                try:
                    p.unlink()
                except OSError:
                    pass


JOBN = [0]


def make_job(**kw):
    JOBN[0] += 1
    tf = TMP / "task.txt"
    tf.write_text("Do nothing; call task_complete.")
    cwd = TMP / "cwd"
    cwd.mkdir(exist_ok=True)
    j = {"id": f"wire{JOBN[0]:02d}", "model": BARE, "cwd": str(cwd), "task_file": str(tf), "num_ctx": 24576,
         "host_pref": "auto", "api": "ollama", "task_kind": "coding", "max_iters": 3, "role": "author"}
    j.update(kw)
    return j


def launch(job, env=None, key_fd=False, plan=True, after_plan=None, timeout=150, popen_env=None):
    """What the daemon does: snapshot -> plan -> legacy _build_cmd -> prepare_launch -> Popen(pass_fds).
    Returns (rc, output, lp, cmd)."""
    att = "att" + job["id"][-4:].ljust(7, "0")
    senv = {**os.environ, **(env or {})}
    snap = LP.take_snapshot(None, sticky_s=0)
    lp = PR.plan_for_launch(job, STUB, snap, att, env=senv) if plan else None
    cmd = q._build_cmd(dict(job), STUB)
    cmd[1] = str(BIN / "ollama-worker.py")
    cmd, fds = PR.prepare_launch(lp, cmd, job, att, "studio-db", lambda f: True, env=senv)
    if after_plan:
        after_plan()
    wenv = {"HOME": str(HOME), "PATH": os.environ.get("PATH", ""), "OLLAMA_DISPATCH_VIA_QUEUE": job["id"],
            "OLLAMA_DISPATCH_ATTEMPT": att, "SLOT_BUSY_WAIT_S": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "DARKBLOOM_KEY_ROTATE_WAIT_S": "3", **senv, **(popen_env or {})}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=wenv, cwd=str(TMP), pass_fds=fds)
    finally:
        for fd in fds:
            os.close(fd)
    return p.returncode, p.stdout + p.stderr, lp, cmd


def conf_records(job):
    return list(CL.iter_records(CONF, job=job["id"]))


def mismatches(kinds=None):
    rows = CL.read_mismatches(CONF)
    return [r for r in rows if kinds is None or r.get("kind") in kinds]


def all_text_of_dirs():
    out = ""
    for d in (CONF, PLANS):
        for p in sorted(d.glob("*")):
            if p.is_file():
                out += p.read_text(errors="replace")
    return out


def posts(prefix="/v1/chat/completions"):
    return [r for r in Stub.rec if r[2] is not None and r[0].startswith(prefix)]


AUDIT = TMP / "audit"
AUDIT.mkdir()
(AUDIT / "usercustomize.py").write_text('''
import os, sys
def _h(event, args):
    if event == "open":
        p = args[0]
        if isinstance(p, str) and ("/.darkbloom/" in p or "provider.toml" in p):
            pr = sys.modules.get("plan_runtime")
            wp = getattr(pr, "_WP", [None])[0] if pr else None
            try:
                with open(os.environ["AUDIT_OUT"], "a") as f:
                    f.write(("AFTER_PLAN " if wp is not None else "BEFORE_PLAN ") + p + "\\n")
            except Exception:
                pass
sys.addaudithook(_h)
''')


# ==================================================================================================
def t_shadow_consistent():
    write_vendor()
    reset()
    job = make_job()
    rc, out, lp, cmd = launch(job)
    chk("S: plan built in shadow", lp is not None and lp.plan.lane == "studio-db" and lp.plan.model == BARE, lp)
    chk("S: --plan/--plan-sha on the argv, no --key-fd by default",
        "--plan" in cmd and "--plan-sha" in cmd and "--key-fd" not in cmd, cmd[-6:])
    chk("S: worker completes", rc == 0, out[-600:])
    chat = posts()
    chk("S: every request carried the Bearer key (legacy decided)", chat and all(r[1] == "Bearer " + KEY1 for r in chat),
        [r[1] for r in chat])
    recs = conf_records(job)
    chk("S: one conformance record per request (preflight + turn)", len(recs) >= 2 and len(recs) == len(
        [r for r in Stub.rec if r[0].startswith("/v1/chat/completions")]), (len(recs), len(Stub.rec)))
    chk("S: records carry the plan id + kfp + host/model/api",
        all(r["plan"] == lp.plan.plan_id and r["kfp"] == LP.key_fp(KEY1)[7:] and r["host"] == STUB and r["model"] == BARE
            and r["api"] == "openai" and r["path"] == "/v1/chat/completions" for r in recs), recs[:1])
    chk("S: shadow with a consistent world logs ZERO mismatch rows", mismatches({"mismatch"}) == [], mismatches())
    v = PR.reap_check({"id": job["id"], "plan": PR.job_plan_summary(lp), "launched_at": "2026-10-14T00:00:00+00:00"})
    chk("R: reap_check on a clean attempt is ok (no drift, GAP-free)", v and v["ok"] is True and v["calls"] == 1 and v["n"] >= 2, v)
    chk("S: summary row written", any(r.get("job") == job["id"] for r in CL.read_summaries(CONF)))


def t_enforce_no_vendor_reads():
    write_vendor()
    reset()
    job = make_job()
    env = {"LANE_PLAN_ENFORCE": "1", "LANE_PLAN_KEY_FD": "1", "AUDIT_OUT": str(TMP / "audit.out"),
           "PYTHONPATH": str(AUDIT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    (TMP / "audit.out").write_text("")
    rc, out, lp, cmd = launch(job, env=env, after_plan=wipe_vendor)
    chk("b: --key-fd on the argv, no key literal in argv", "--key-fd" in cmd and not any(KEY1 in a for a in cmd), cmd[-8:])
    chk("b: ENFORCE worker completes with EVERY vendor file deleted for the whole run", rc == 0, out[-800:])
    chat = posts()
    chk("b: every request Bearer key from the fd", chat and all(r[1] == "Bearer " + KEY1 for r in chat), [r[1] for r in chat])
    chk("b: every body model/path right", chat and all(r[2].get("model") == BARE for r in chat))
    # NOTHINK_RUN is DEFAULT ON for the author role (2026-10-15): the dispatch body is the author profile with
    # enable_thinking=false (the worker is launched with NOTHINK_RUN unset here). Every other profile field still must match.
    assert "NOTHINK_RUN" not in os.environ, "test must run with the shipped default"
    exp = __import__("model_profile").build_request_fields(BARE, "author", "openai", think=False)
    chk("b: default NOTHINK_RUN: the author dispatch body has thinking OFF",
        (chat[-1][2].get("chat_template_kwargs") or {}).get("enable_thinking") is False, chat[-1][2].get("chat_template_kwargs"))
    main_body = chat[-1][2]
    bad = {k: (main_body.get(k), v) for k, v in exp.items() if main_body.get(k) != v}
    chk("b: the dispatch body carries the profile fields", not bad, bad)
    audit = (TMP / "audit.out").read_text()
    chk("b: no vendor-file open AFTER the plan loaded (audit hook)", "AFTER_PLAN" not in audit, audit)
    recs = conf_records(job)
    chk("b: records ok under the reap comparison",
        (PR.reap_check({"id": job["id"], "plan": PR.job_plan_summary(lp), "launched_at": "2026-10-14T00:00:00+00:00"}) or {}).get("ok") is True)
    return lp


def t_rotation_one_refresh():
    write_vendor()
    reset()
    job = make_job()

    def rotate():
        write_vendor(key=KEY2, pid=4243)                 # the provider restarted: new key, new gen
        Stub.key = KEY2
    Stub.on_first_chat = rotate
    env = {"LANE_PLAN_ENFORCE": "1", "LANE_PLAN_KEY_FD": "1"}
    rc, out, lp, cmd = launch(job, env=env, after_plan=wipe_vendor)
    chat = posts()
    auths = [r[1] for r in chat]
    chk("c: worker completes after the rotation", rc == 0, out[-700:])
    chk("c: sequence old,old(401),NEW", auths[-3:] == ["Bearer " + KEY1, "Bearer " + KEY1, "Bearer " + KEY2], auths)
    chk("c: exactly one PLAN KEY ROTATED line", out.count("PLAN KEY ROTATED") == 1, out[-800:])
    chk("c: the retry is uncharged (no 'retry 1/' accounting line)", "retry 1/" not in out, out[-500:])
    chk("c: log carries fingerprints, never the key", KEY1 not in out and KEY2 not in out
        and LP.key_fp(KEY2) in out, [l for l in out.splitlines() if "ROTATED" in l or KEY1 in l or KEY2 in l])
    recs = conf_records(job)
    chk("c: a key_rotated event record with the NEW fp", any(r.get("ev") == "key_rotated" and r["kfp"] == LP.key_fp(KEY2)[7:] for r in recs), recs[-2:])
    v = PR.reap_check({"id": job["id"], "plan": PR.job_plan_summary(lp), "launched_at": "2026-10-14T00:00:00+00:00"})
    chk("c: reap comparison accepts the rotation (no KEY_DRIFT)", v and v["ok"] is True, v)


def t_expiry_changed_base():
    write_vendor()
    reset()
    job = make_job()

    def move():
        write_vendor(key=KEY2, pid=4244, base="http://127.0.0.1:1")     # provider came back on ANOTHER endpoint
        Stub.key = KEY2
    Stub.on_first_chat = move
    env = {"LANE_PLAN_ENFORCE": "1", "LANE_PLAN_KEY_FD": "1"}
    rc, out, lp, cmd = launch(job, env=env, after_plan=wipe_vendor)
    chk("d: changed base -> paused exit (rc 3)", rc == 3, (rc, out[-700:]))
    chk("d: lane_cause names plan_expired", "plan_expired" in out and "endpoint_changed" in out, out[-700:])
    chk("d: no retry went to the old plan host with a new key", [r[1] for r in posts()].count("Bearer " + KEY2) == 0)


def t_key_mismatch_enforce():
    write_vendor()
    reset()
    job = make_job()
    # a plan made for KEY1, but the pipe carries a different key (a rotation between plan and spawn)
    snap = LP.take_snapshot(None, sticky_s=0)
    lp = PR.plan_for_launch(job, STUB, snap, "attkm00001", env={**os.environ, "LANE_PLAN_ENFORCE": "1"})
    write_vendor(key=KEY2, pid=4245)
    snap2 = LP.take_snapshot(None, sticky_s=0)
    lp2 = PR.LaunchPlan(lp.plan, lp.path, snap2)               # the pipe would carry KEY2
    cmd = q._build_cmd(dict(job), STUB)
    cmd[1] = str(BIN / "ollama-worker.py")
    env = {**os.environ, "LANE_PLAN_ENFORCE": "1", "LANE_PLAN_KEY_FD": "1"}
    cmd, fds = PR.prepare_launch(lp2, cmd, job, "attkm00001", "studio-db", lambda f: True, env=env)
    wenv = {"HOME": str(HOME), "PATH": os.environ.get("PATH", ""), "OLLAMA_DISPATCH_VIA_QUEUE": job["id"],
            "OLLAMA_DISPATCH_ATTEMPT": "attkm00001", "PYTHONDONTWRITEBYTECODE": "1", **env}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=wenv, cwd=str(TMP), pass_fds=fds)
    finally:
        for fd in fds:
            os.close(fd)
    out = p.stdout + p.stderr
    chk("k: enforce + key fp != plan fp -> paused (rc 3) with plan_key_mismatch", p.returncode == 3 and "plan_key_mismatch" in out, (p.returncode, out[-500:]))
    chk("k: no request was sent with the wrong key", not [r for r in posts() if r[1] == "Bearer " + KEY2])
    chk("k: mismatch row KEY_DRIFT", any(r["class"] == "KEY_DRIFT" for r in mismatches({"mismatch"})), mismatches())


def t_shadow_logs_rotation_as_info():
    write_vendor()
    reset()
    job = make_job()
    snap = LP.take_snapshot(None, sticky_s=0)
    lp = PR.plan_for_launch(job, STUB, snap, "attsh00001")
    write_vendor(key=KEY2, pid=4246)                           # disk rotated after the plan was made
    Stub.key = KEY2
    cmd = q._build_cmd(dict(job), STUB)
    cmd[1] = str(BIN / "ollama-worker.py")
    cmd, fds = PR.prepare_launch(lp, cmd, job, "attsh00001", "studio-db", lambda f: True)
    wenv = {"HOME": str(HOME), "PATH": os.environ.get("PATH", ""), "OLLAMA_DISPATCH_VIA_QUEUE": job["id"],
            "OLLAMA_DISPATCH_ATTEMPT": "attsh00001", "PYTHONDONTWRITEBYTECODE": "1"}
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=wenv, cwd=str(TMP))
    chk("S2: shadow: the LEGACY path decided (new disk key accepted, job completes)", p.returncode == 0, (p.stdout + p.stderr)[-400:])
    rows = mismatches({"info"})
    chk("S2: the key difference is logged as info KEY_DRIFT auth.fp", any(r["class"] == "KEY_DRIFT" and r["field"] == "auth.fp" for r in rows), rows)
    chk("S2: ... and is not counted as a 'mismatch'", mismatches({"mismatch"}) == [], mismatches())


def t_pid_mismatch_waits():
    write_vendor(pid=4300, gen_pid=4301)                       # local.json pid != daemon-state pid: restart in flight
    reset()
    job = make_job()
    snap = LP.take_snapshot(None, sticky_s=0)
    chk("f: snapshot reads the pid mismatch as restarting", snap.darkbloom.state == "restarting", snap.darkbloom)
    lp = PR.plan_for_launch(job, STUB, snap, "attpid0001")
    chk("f: SHADOW: no plan, launch continues, a would_block row is logged",
        lp is None and any(r["kind"] == "would_block" and r["field"] == "darkbloom_restarting" for r in mismatches()), mismatches())
    try:
        PR.plan_for_launch(job, STUB, snap, "attpid0002", env={**os.environ, "LANE_PLAN_ENFORCE": "1"})
        raised = None
    except LP.LaneUnavailable as e:
        raised = e.cause
    chk("f: ENFORCE: LaneUnavailable('darkbloom_restarting') (the queue INFRA-WAITs, never fails the job)", raised == "darkbloom_restarting", raised)
    # the C10 shape: a Darkbloom-only model aimed at an Ollama host
    write_vendor()
    snap = LP.take_snapshot(None, sticky_s=0)
    try:
        PR.plan_for_launch(make_job(), "http://10.0.7.143:11434", snap, "attc100001", env={**os.environ, "LANE_PLAN_ENFORCE": "1"})
        r2 = None
    except LP.LaneUnavailable as e:
        r2 = e.cause
    chk("f: ENFORCE: Darkbloom-only model on an Ollama host is refused (model_lane_mismatch)", r2 == "model_lane_mismatch", r2)


def t_keyfd_daemon_env():
    """(e) + the launchd gap: spawn the worker the way the daemon does -- Popen(pass_fds) from a process started
    under `env -i` with stdin /dev/null -- and look at ps argv/env while it runs."""
    write_vendor()
    reset()
    Stub.delay_s = 2.5                                      # keep the child alive long enough to inspect
    job = make_job()
    runner = TMP / "daemon_sim.py"
    runner.write_text(f'''
import json, os, subprocess, sys, time
sys.path.insert(0, {str(BIN)!r})
import lane_plan as LP, plan_runtime as PR
from importlib.machinery import SourceFileLoader
q = SourceFileLoader("qd", {str(BIN / "ollama-queue.py")!r}).load_module()
job = json.loads(os.environ["SIM_JOB"])
snap = LP.take_snapshot(None, sticky_s=0)
lp = PR.plan_for_launch(job, {STUB!r}, snap, "attdaemon1")
cmd = q._build_cmd(dict(job), {STUB!r})
cmd[1] = {str(BIN / "ollama-worker.py")!r}
cmd, fds = PR.prepare_launch(lp, cmd, job, "attdaemon1", "studio-db", lambda f: True)
log = open(os.environ["SIM_LOG"], "w")
env = {{**os.environ, "OLLAMA_DISPATCH_VIA_QUEUE": job["id"], "OLLAMA_DISPATCH_ATTEMPT": "attdaemon1"}}
p = subprocess.Popen(cmd, cwd={str(TMP)!r}, stdout=log, stderr=subprocess.STDOUT, env=env, pass_fds=fds, stdin=subprocess.DEVNULL)
for fd in fds:
    os.close(fd)
time.sleep(1.2)
ps_args = subprocess.run(["ps", "-o", "args=", "-p", str(p.pid)], capture_output=True, text=True).stdout
ps_env = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(p.pid)], capture_output=True, text=True).stdout
open(os.environ["SIM_PS"], "w").write(json.dumps({{"args": ps_args, "env": ps_env, "fds_passed": len(fds), "argv_has_keyfd": "--key-fd" in cmd}}))
sys.exit(p.wait())
''')
    simenv = {"HOME": str(HOME), "PATH": os.environ.get("PATH", ""), "LANE_PLAN_KEY_FD": "1", "LANE_PLAN_ENFORCE": "1",
              "SIM_JOB": json.dumps(job), "SIM_LOG": str(TMP / "sim.log"), "SIM_PS": str(TMP / "sim.ps"),
              "PYTHONDONTWRITEBYTECODE": "1"}
    p = subprocess.run(["env", "-i", *[f"{k}={v}" for k, v in simenv.items()], sys.executable, str(runner)],
                       capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
    log = (TMP / "sim.log").read_text(errors="replace")
    ps = json.loads((TMP / "sim.ps").read_text()) if (TMP / "sim.ps").exists() else {}
    chk("e: daemon-style (env -i, Popen pass_fds, stdin /dev/null) worker completes", p.returncode == 0, (p.returncode, p.stderr[-600:], log[-600:]))
    chk("e: the key fd was used (argv has --key-fd, one fd passed)", ps.get("argv_has_keyfd") and ps.get("fds_passed") == 1, ps)
    chk("e: key absent from ps argv", ps and KEY1 not in ps.get("args", ""), ps.get("args"))
    chk("e: key absent from the child env as ps shows it", ps and KEY1 not in ps.get("env", ""), "env leak")
    chk("e: every request Bearer key (delivered through the fd)", posts() and all(r[1] == "Bearer " + KEY1 for r in posts()))
    blob = all_text_of_dirs() + log
    chk("e: key absent from job log, plan file, conformance + mismatch + summary files", KEY1 not in blob and KEY2 not in blob)


def t_kill_switches():
    write_vendor()
    reset()
    job = make_job()
    rc, out, lp, cmd = launch(job, env={"LANE_PLAN": "0"})
    chk("K: LANE_PLAN=0: no plan, no --plan on the argv, legacy launch works",
        lp is None and "--plan" not in cmd and rc == 0, (cmd[-4:], rc, out[-300:]))
    reset()
    job = make_job()
    rc, out, lp, cmd = launch(job, env={"WORKER_PLAN": "0"})
    chk("K: WORKER_PLAN=0: plan exists but the worker ignores it; job completes", lp is not None and rc == 0 and mismatches() == [], (rc, out[-300:]))
    reset()
    job = make_job()
    rc, out, lp, cmd = launch(job, env={"CONFORMANCE_LOG": "0"})
    chk("K: CONFORMANCE_LOG=0: no records, no summary", rc == 0 and conf_records(job) == [] and CL.read_summaries(CONF) == [], rc)
    v = PR.reap_check({"id": job["id"], "plan": PR.job_plan_summary(lp), "launched_at": "2026-10-14T00:00:00+00:00"},
                      env={**os.environ, "CONFORMANCE_CHECK": "0"})
    chk("K: CONFORMANCE_CHECK=0: reap_check is a no-op", v is None, v)
    reset()
    job = make_job()
    rc, out, lp, cmd = launch(job, env={}, popen_env={})
    ok_disable = (HOME / ".ollama-dispatch" / "lane-plan.disabled")
    ok_disable.write_text("")
    LP.DISABLE_FILE = ok_disable
    chk("K: ~/.ollama-dispatch/lane-plan.disabled turns the plan layer off", PR.mode() == "off" and PR.worker_mode() == "off")
    ok_disable.unlink()
    LP.DISABLE_FILE = Path("/nonexistent/lane-plan.disabled")


def t_reap_drift_classes():
    write_vendor()
    reset()
    job = make_job()
    snap = LP.take_snapshot(None, sticky_s=0)
    lp = PR.plan_for_launch(job, STUB, snap, "attrp00001")
    summ = PR.job_plan_summary(lp)
    j = {"id": job["id"], "plan": summ, "launched_at": "2026-10-14T00:00:00+00:00"}
    rec = CL.Recorder(directory=CONF)
    base = dict(job=job["id"], att="attrp00001", plan=lp.plan, role="author", host=STUB, path="/v1/chat/completions",
                api="openai", model=BARE, status=200, rmodel=BARE)
    flds = __import__("model_profile").build_request_fields(BARE, "author", "openai")
    body = {"model": BARE, "messages": [{"role": "user", "content": "x"}], **flds}
    rec.record(body=body, key=KEY1, n=1, **base)
    v = PR.reap_check(j)
    chk("R: one good record -> ok", v["ok"] is True, v)
    rec.record(body=body, key=KEY1, n=2, **{**base, "host": "http://10.0.7.143:11434"})
    rec.record(body=body, key=None, n=3, **base)
    rec.record(body={**body, "model": "other"}, key=KEY1, n=4, **{**base, "model": "other"})
    bad = {k: v for k, v in body.items() if k != "top_k"}
    rec.record(body=bad, key=KEY1, n=5, **base)
    v = PR.reap_check(j)
    classes = {d["class"] for d in v["drift"]}
    chk("R: injected host/key/model/param drift all classified", {"HOST_DRIFT", "KEY_DRIFT", "MODEL_DRIFT", "PARAM_DRIFT"} <= classes, classes)
    chk("R: drift rows are also in the mismatch file as kind=drift", {r["class"] for r in mismatches({"drift"})} >= classes, mismatches({"drift"}))
    CL.write_summary({"job": job["id"], "att": "attrp00001", "calls": 99, "recorded": 99}, directory=CONF)
    v = PR.reap_check(j)
    chk("R: more worker calls than records -> GAP", "GAP" in {d["class"] for d in v["drift"]}, v)
    chk("R: alert_cmd is silent unless CONFORMANCE_ALERT=1", PR.alert_cmd(job, v) is None)
    os.environ["CONFORMANCE_ALERT"] = "1"
    a = PR.alert_cmd({**job, "plan": summ}, v)
    del os.environ["CONFORMANCE_ALERT"]
    chk("R: alert_cmd names notify-penn with a dedupe key", a and "notify-penn.py" in a[1] and "--dedupe-key" in a, a)


def t_compare_cmd_unit():
    write_vendor()
    snap = LP.take_snapshot(None, sticky_s=0)
    lp = PR.plan_for_launch(make_job(), STUB, snap, "attcc00001")
    good = ["python3", "w", "--task", "t", "--model", BARE, "--host", STUB, "--num-ctx", "24576", "--api", "openai"]
    chk("cmp: a faithful legacy argv has no differences", PR.compare_cmd(lp.plan, good, "studio-db") == [], PR.compare_cmd(lp.plan, good, "studio-db"))
    bad = ["python3", "w", "--task", "t", "--model", "x", "--host", "http://10.0.7.143:11434", "--num-ctx", "999"]
    cls = {c for c, *_ in PR.compare_cmd(lp.plan, bad, "unraid")}
    chk("cmp: wrong model/host/api/ctx/lane all flagged", {"MODEL_DRIFT", "HOST_DRIFT", "API_DRIFT", "CTX_DRIFT"} <= cls, cls)
    runner = ["python3", "r.py", "--model", "zzz", "--host", STUB, "--num-ctx", "24576", "--cwd", "c", "--task-file", "f"]
    chk("cmp: a runner argv is compared on host+ctx only (its model/api are its own)", PR.compare_cmd(lp.plan, runner) == [])
    enf = PR.apply_plan_to_cmd(lp, bad)
    chk("cmp: apply_plan_to_cmd rewrites host/model/api/ctx from the plan",
        PR._argv_val(enf, "--host") == STUB and PR._argv_val(enf, "--model") == BARE
        and PR._argv_val(enf, "--api") == "openai" and PR._argv_val(enf, "--num-ctx") == "24576", enf)
    chk("cmp: bad argv is untouched by apply (returns a copy)", PR._argv_val(bad, "--model") == "x")
    ov = PR.override_names(temperature=0.2, top_p=None, repetition_penalty=1.1, think=False)
    chk("ov: names only the non-None overrides, with ollama options.* and the think aliases",
        "temperature" in ov and "options.temperature" in ov and "top_p" not in ov and "repeat_penalty" in ov
        and "chat_template_kwargs.enable_thinking" in ov, ov)


def t_sticky_through_torn_file():
    """Item 4: the plan path keeps the sticky window the legacy caches have -- an empty (mid-rewrite) local.json
    inside the window still plans on the last good record; outside it (sticky 0) the lane is 'restarting'."""
    write_vendor()
    reset()
    job = make_job()
    snap1 = LP.take_snapshot(None, sticky_s=60)
    LOCAL.write_text("")
    snap2 = LP.take_snapshot(snap1, sticky_s=60)
    lp = PR.plan_for_launch(job, STUB, snap2, "attstk0001", env={**os.environ, "LANE_PLAN_ENFORCE": "1"})
    chk("stk: torn file inside the sticky window still plans on the last good key/base",
        lp is not None and lp.plan.host == STUB and lp.plan.auth_fp == LP.key_fp(KEY1) if hasattr(lp.plan, "auth_fp") else lp is not None and lp.plan.host == STUB, lp)
    snap3 = LP.take_snapshot(snap1, sticky_s=0)
    try:
        PR.plan_for_launch(job, STUB, snap3, "attstk0002", env={**os.environ, "LANE_PLAN_ENFORCE": "1"})
        cause = None
    except LP.LaneUnavailable as e:
        cause = e.cause
    chk("stk: with the window disabled the same torn file is darkbloom_restarting (the sticky is what holds it)", cause == "darkbloom_restarting", cause)
    # a MISSING file is never masked, even inside the window
    LOCAL.unlink()
    snap4 = LP.take_snapshot(snap1, sticky_s=60)
    chk("stk: a missing local.json is 'off', never masked", snap4.darkbloom.state == "off", snap4.darkbloom.state)


TESTS = [t_sticky_through_torn_file, t_compare_cmd_unit, t_shadow_consistent, t_enforce_no_vendor_reads, t_rotation_one_refresh,
         t_expiry_changed_base, t_key_mismatch_enforce, t_shadow_logs_rotation_as_info, t_pid_mismatch_waits,
         t_keyfd_daemon_env, t_kill_switches, t_reap_drift_classes]


def main():
    LP.DISABLE_FILE = Path("/nonexistent/lane-plan.disabled")
    for t in TESTS:
        if ONLY and ONLY not in t.__name__:
            continue
        try:
            t()
        except Exception as e:           # noqa: BLE001
            import traceback
            traceback.print_exc()
            chk(f"{t.__name__} crashed", False, repr(e))
    print("PLAN_WIRE_OK" if not FAILS else f"PLAN_WIRE_FAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
