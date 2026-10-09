#!/usr/bin/env python3
"""test-cpu-lane-api.py -- the queue-side /api/cpu/* routes served by the REAL dashboard
Handler (ollama-queue-api.py) against a throwaway store + token (CPU LANE, 2026-10-08).

Proves: bearer token on every route but health (401 without / wrong / empty-token-file),
requests carrying Cloudflare-edge headers or a public Host are refused even WITH a valid
token (403), the full producer/runner flow, atomic oldest-first claim, heartbeat cancel,
409 on a stale lease, lease expiry -> pending -> (3rd) failed_infra, release, cancel of
pending jobs, durability across an API restart (jobs, blobs, leases), the read-only
dashboard feed leaks neither token nor command, and the token file is created 0600.

Usage: test-cpu-lane-api.py [--api PATH]. Exit 0 = all pass.
"""
import argparse
import http.client
import importlib.util
import json
import os
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append(ok)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"\n     got={got!r}\n    want={want!r}"))


class Srv:
    def __init__(self, api_mod):
        self.api = api_mod
        self.srv = api_mod.ThreadingServer(("127.0.0.1", 0), api_mod.Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def call(port, method, path, body=None, raw=None, token="TOKEN", headers=None):
    h = {}
    if token:
        h["Authorization"] = "Bearer " + token
    h.update(headers or {})
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if data is not None:
        h["Content-Length"] = str(len(data))
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    b = r.read()
    c.close()
    try:
        return r.status, json.loads(b) if b else None
    except ValueError:
        return r.status, b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=str(Path.home() / "bin" / "ollama-queue-api.py"))
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="cpulane-api-"))
    tokfile = tmp / "cfg" / "token"
    os.environ["CPU_LANE_DIR"] = str(tmp / "cpu-jobs")
    os.environ["CPU_RUNNER_TOKEN_FILE"] = str(tokfile)
    sys.path.insert(0, str(Path(a.api).parent))
    import cpu_lane
    spec = importlib.util.spec_from_file_location("oqapi_cpulane", a.api)
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)

    # token file creation: 0600, 64 hex chars, never overwritten
    tok = cpu_lane.read_token(create=True)
    mode = stat.S_IMODE(os.stat(tokfile).st_mode)
    check("token: auto-created 0600 with 32 random bytes (hex)", (mode, len(tok)), (0o600, 64))
    tokfile.write_text("TOKEN\n")
    os.chmod(tokfile, 0o600)
    check("token: an existing file is kept and re-read", cpu_lane.read_token(create=True), "TOKEN")

    S = Srv(api)
    p = S.port
    spec_ = {"cmd": "echo hi", "cwd": ".", "timeout_s": 60, "env": {"SECRET_X": "do-not-leak"},
             "payload_kind": "bundle", "stage": "verify", "network": "none"}

    # ---- auth -----------------------------------------------------------------------------
    check("auth: health is open", call(p, "GET", "/api/cpu/health", token=None)[0], 200)
    check("auth: no token -> 401", call(p, "POST", "/api/cpu/jobs", {"spec": spec_}, token=None)[0], 401)
    check("auth: wrong token -> 401", call(p, "POST", "/api/cpu/jobs", {"spec": spec_}, token="nope")[0], 401)
    check("auth: claim without token -> 401", call(p, "POST", "/api/cpu/claim", {"runner_id": "r"}, token=None)[0], 401)
    check("auth: payload download without token -> 401", call(p, "GET", "/api/cpu/jobs/" + "0" * 32 + "/payload", token=None)[0], 401)
    check("auth: a token with a matching PREFIX is not enough", call(p, "GET", "/api/cpu/runners", token="TOKENX")[0], 401)
    for hdr in ({"Cf-Ray": "abc-LAX"}, {"Cf-Connecting-Ip": "1.2.3.4"}, {"X-Forwarded-For": "1.2.3.4"},
                {"Cf-Access-Jwt-Assertion": "x"}, {"Host": "queue.example.com"}):
        check("cloudflare path %s: refused even WITH a valid token" % list(hdr)[0],
              call(p, "POST", "/api/cpu/jobs", {"spec": spec_}, headers=hdr)[0], 403)
    check("cloudflare path: and without a token it is also refused (never a 200)",
          call(p, "POST", "/api/cpu/claim", {"runner_id": "r"}, token=None, headers={"Cf-Ray": "x"})[0] in (401, 403), True)
    # an unrelated dashboard route is untouched by the lane (still served, no token needed)
    check("dashboard routes keep their old trust model (no cpu token)", call(p, "GET", "/api/cpu-lane", token=None)[0], 200)
    tokfile.write_text("")
    check("auth: an EMPTY token file rejects everything (even an empty bearer)",
          call(p, "POST", "/api/cpu/jobs", {"spec": spec_}, token="x")[0], 401)
    tokfile.write_text("TOKEN\n")

    # ---- enrollment (single-use code -> token, LAN-only, rate limited) ----------------------
    st_ = cpu_lane.get_store()
    code = st_.mint_enroll(3600)
    c, b = call(p, "POST", "/api/cpu/enroll", {"code": code, "runner_id": "u1"}, token=None)
    check("enroll: a live code returns the token + runner config (no bearer needed)",
          (c, b.get("token"), b.get("config", {}).get("concurrency")), (200, "TOKEN", 2))
    check("enroll: the same code a second time is refused (single use)",
          call(p, "POST", "/api/cpu/enroll", {"code": code, "runner_id": "u1"}, token=None)[0], 403)
    check("enroll: an unknown code is refused", call(p, "POST", "/api/cpu/enroll", {"code": "x" * 12}, token=None)[0], 403)
    code2 = st_.mint_enroll(3600)
    check("enroll: Cloudflare-edge request is refused even with a valid code",
          call(p, "POST", "/api/cpu/enroll", {"code": code2}, token=None, headers={"Cf-Ray": "x"})[0], 403)
    check("enroll: ...and that refusal did NOT consume the code",
          call(p, "POST", "/api/cpu/enroll", {"code": code2}, token=None)[0], 200)
    code3 = st_.mint_enroll(3600)
    real = st_.clock
    st_.clock = lambda: time.time() + 7200
    expired = call(p, "POST", "/api/cpu/enroll", {"code": code3}, token=None)[0]
    st_.clock = real
    check("enroll: an expired code is refused", expired, 403)
    check("enroll: the code is stored hashed only", code3 in open(st_.db, "rb").read().decode("latin1"), False)
    check("config: needs the bearer", call(p, "GET", "/api/cpu/config", token=None)[0], 401)
    check("config: runner tunables served", call(p, "GET", "/api/cpu/config")[1]["config"]["lease_s"], 60)
    cpu_lane._enroll_fail[:] = []
    codes = [call(p, "POST", "/api/cpu/enroll", {"code": "bad-code-%d" % i}, token=None)[0] for i in range(6)]
    check("enroll: repeated failures are rate limited (429 after 5)", codes, [403] * 5 + [429])
    good = st_.mint_enroll(3600)
    check("enroll: while limited even a valid code is refused (and stays unspent)",
          call(p, "POST", "/api/cpu/enroll", {"code": good}, token=None)[0], 429)
    cpu_lane._enroll_fail[:] = []
    check("enroll: after the window the unspent code works",
          call(p, "POST", "/api/cpu/enroll", {"code": good}, token=None)[0], 200)
    cpu_lane.rotate_token()
    check("rotate: the old token is rejected immediately",
          call(p, "GET", "/api/cpu/runners", token="TOKEN")[0], 401)
    newtok = cpu_lane.read_token()
    check("rotate: the new token (0600) works", (len(newtok), call(p, "GET", "/api/cpu/runners", token=newtok)[0]), (64, 200))
    tokfile.write_text("TOKEN\n")
    os.chmod(tokfile, 0o600)

    # ---- flow -----------------------------------------------------------------------------
    def make(bundle="bA", label="l1", payload=b"PAYLOAD", patch=None, tools=None, ready=True):
        c, b = call(p, "POST", "/api/cpu/jobs", {"spec": spec_, "label": label, "stage": "verify", "bundle_id": bundle})
        assert c == 201, c
        jid = b["id"]
        if payload is not None:
            assert call(p, "PUT", f"/api/cpu/jobs/{jid}/payload", raw=payload)[0] == 200
        if patch:
            call(p, "PUT", f"/api/cpu/jobs/{jid}/patch", raw=patch)
        if tools:
            call(p, "PUT", f"/api/cpu/jobs/{jid}/tools", raw=tools)
        if ready:
            call(p, "POST", f"/api/cpu/jobs/{jid}/ready", {})
        return jid

    c, b = call(p, "POST", "/api/cpu/jobs", {"spec": spec_, "bundle_id": "bA"})
    j0 = b["id"]
    check("flow: ready without a payload -> 400", call(p, "POST", f"/api/cpu/jobs/{j0}/ready", {})[0], 400)
    check("flow: created job is 'uploading'", call(p, "GET", f"/api/cpu/jobs/{j0}")[1]["status"], "uploading")
    check("flow: nothing claimable while uploading", call(p, "POST", "/api/cpu/claim", {"runner_id": "r1"})[0], 204)
    call(p, "DELETE", f"/api/cpu/jobs/{j0}")
    check("flow: cancelling an uploading job -> cancelled", call(p, "GET", f"/api/cpu/jobs/{j0}")[1]["status"], "cancelled")

    big = os.urandom(3 * 1024 * 1024 + 17)
    j1 = make(label="first", payload=big, patch=b"PATCH", tools=b"TOOLS")
    j2 = make(label="second", bundle="bB")
    check("flow: ready -> pending", call(p, "GET", f"/api/cpu/jobs/{j1}")[1]["status"], "pending")
    c, claim = call(p, "POST", "/api/cpu/claim", {"runner_id": "r1", "lease_s": 30})
    job = claim["job"]
    check("claim: oldest pending first, attempt 1, has_patch/has_tools derived server-side",
          (c, job["id"], job["attempt"], job["spec"]["has_patch"], job["spec"]["has_tools"]), (200, j1, 1, True, True))
    check("claim: spec comes back intact", job["spec"]["cmd"], "echo hi")
    check("blob: a multi-MB payload round-trips byte for byte",
          call(p, "GET", f"/api/cpu/jobs/{j1}/payload")[1] == big, True)
    check("blob: patch + tools served", (call(p, "GET", f"/api/cpu/jobs/{j1}/patch")[1],
                                         call(p, "GET", f"/api/cpu/jobs/{j1}/tools")[1]), (b"PATCH", b"TOOLS"))
    check("blob: j2 has no patch -> 404", call(p, "GET", f"/api/cpu/jobs/{j2}/patch")[0], 404)
    check("blob: upload to a job that is no longer 'uploading' -> 409",
          call(p, "PUT", f"/api/cpu/jobs/{j1}/payload", raw=b"x")[0], 409)
    c, claim2 = call(p, "POST", "/api/cpu/claim", {"runner_id": "r2", "lease_s": 30})
    check("claim: a second runner gets the NEXT job (atomic, no double claim)", claim2["job"]["id"], j2)
    check("claim: queue empty -> 204", call(p, "POST", "/api/cpu/claim", {"runner_id": "r1"})[0], 204)
    st = call(p, "GET", f"/api/cpu/jobs/{j1}")[1]
    check("status: running with runner id", (st["status"], st["runner_id"], st["attempt"]), ("running", "r1", 1))
    check("heartbeat: ok + cancel false",
          call(p, "POST", f"/api/cpu/jobs/{j1}/heartbeat", {"runner_id": "r1", "lease_token": job["lease_token"], "lease_s": 30})[1],
          {"ok": True, "cancel": False})
    check("heartbeat: wrong lease token -> 409",
          call(p, "POST", f"/api/cpu/jobs/{j1}/heartbeat", {"runner_id": "r1", "lease_token": "bad"})[0], 409)
    check("cancel: DELETE on a running job -> heartbeat reports cancel:true",
          (call(p, "DELETE", f"/api/cpu/jobs/{j2}")[0],
           call(p, "POST", f"/api/cpu/jobs/{j2}/heartbeat", {"lease_token": claim2["job"]["lease_token"]})[1]["cancel"]), (200, True))
    check("result: stale token -> 409 and nothing recorded",
          call(p, "POST", f"/api/cpu/jobs/{j1}/result", {"lease_token": "bad", "exit_code": 0})[0], 409)
    check("result: ok", call(p, "POST", f"/api/cpu/jobs/{j1}/result",
                             {"runner_id": "r1", "lease_token": job["lease_token"], "exit_code": 3, "timed_out": False,
                              "stdout_tail": "out", "stderr_tail": "err", "timings": {"run_s": 1.5}})[0], 200)
    st = call(p, "GET", f"/api/cpu/jobs/{j1}")[1]
    check("result: done, exit code + tails + timings stored, lease token NOT echoed",
          (st["status"], st["result"]["exit_code"], st["result"]["stdout_tail"], st["result"]["timings"],
           "lease_token" in st["result"]), ("done", 3, "out", {"run_s": 1.5}, False))
    check("result: a second result for the same lease -> 409 (idempotent-safe)",
          call(p, "POST", f"/api/cpu/jobs/{j1}/result", {"lease_token": job["lease_token"], "exit_code": 0})[0], 409)
    call(p, "POST", f"/api/cpu/jobs/{j2}/result", {"lease_token": claim2["job"]["lease_token"], "exit_code": 0})
    check("cancel: a cancelled running job ends 'cancelled', not done",
          call(p, "GET", f"/api/cpu/jobs/{j2}")[1]["status"], "cancelled")

    # ---- release + cancel pending ---------------------------------------------------------
    j3 = make(label="rel")
    c3 = call(p, "POST", "/api/cpu/claim", {"runner_id": "r1", "lease_s": 30})[1]["job"]
    check("release: ok", call(p, "POST", f"/api/cpu/jobs/{j3}/release", {"lease_token": c3["lease_token"]})[0], 200)
    check("release: job back to pending", call(p, "GET", f"/api/cpu/jobs/{j3}")[1]["status"], "pending")
    call(p, "DELETE", f"/api/cpu/jobs/{j3}")
    check("cancel: a pending job is cancelled and never claimed",
          (call(p, "GET", f"/api/cpu/jobs/{j3}")[1]["status"], call(p, "POST", "/api/cpu/claim", {"runner_id": "r1"})[0]),
          ("cancelled", 204))

    # ---- lease expiry + 3 attempts --------------------------------------------------------
    j4 = make(label="dies", bundle="bL")
    seen = []
    for n in (1, 2, 3):
        c, cl = call(p, "POST", "/api/cpu/claim", {"runner_id": "rdead", "lease_s": 1})
        seen.append((c, cl["job"]["attempt"] if cl else None))
        time.sleep(1.3)
        cpu_lane.get_store().reap()
    check("lease expiry: claimed 3 times (attempts 1,2,3)", seen, [(200, 1), (200, 2), (200, 3)])
    st = call(p, "GET", f"/api/cpu/jobs/{j4}")[1]
    check("3 attempts exhausted -> failed_infra with an infra_error",
          (st["status"], "lease expired" in st["result"]["infra_error"]), ("failed_infra", True))
    check("...and it is never offered again", call(p, "POST", "/api/cpu/claim", {"runner_id": "r1"})[0], 204)
    check("lease expiry: a late heartbeat from the dead runner -> 409",
          call(p, "POST", f"/api/cpu/jobs/{j4}/heartbeat", {"lease_token": "whatever"})[0], 409)

    # an infra_error result is failed_infra at once (isolation_unavailable is retried)
    j5 = make(label="infra")
    cl = call(p, "POST", "/api/cpu/claim", {"runner_id": "r1", "lease_s": 30})[1]["job"]
    call(p, "POST", f"/api/cpu/jobs/{j5}/result", {"lease_token": cl["lease_token"], "exit_code": None, "infra_error": "isolation_unavailable"})
    check("result: isolation_unavailable on attempt 1 -> re-queued", call(p, "GET", f"/api/cpu/jobs/{j5}")[1]["status"], "pending")
    cl = call(p, "POST", "/api/cpu/claim", {"runner_id": "r1", "lease_s": 30})[1]["job"]
    call(p, "POST", f"/api/cpu/jobs/{j5}/result", {"lease_token": cl["lease_token"], "exit_code": None, "infra_error": "disk full"})
    check("result: any other infra_error -> failed_infra", call(p, "GET", f"/api/cpu/jobs/{j5}")[1]["status"], "failed_infra")

    # ---- durability across an API restart ---------------------------------------------------
    j6 = make(label="pend-surviving", payload=b"P6", patch=b"D6", bundle="bR")
    j7 = make(label="run-surviving", payload=b"P7", bundle="bR")
    cl7 = call(p, "POST", "/api/cpu/claim", {"runner_id": "rX", "lease_s": 120})[1]["job"]
    # (j6 is older, so it was claimed first -- remember who got what)
    first = cl7["id"]
    other = j7 if first == j6 else j6
    S.stop()
    cpu_lane._store = None                       # forget the in-memory singleton: a fresh process
    S = Srv(api)
    p = S.port
    st_first = call(p, "GET", f"/api/cpu/jobs/{first}")[1]
    st_other = call(p, "GET", f"/api/cpu/jobs/{other}")[1]
    check("restart: the running job is still running with its runner", (st_first["status"], st_first["runner_id"]), ("running", "rX"))
    check("restart: the pending job is still pending", st_other["status"], "pending")
    check("restart: the runner's old lease token still heartbeats (no 409 after an API restart)",
          call(p, "POST", f"/api/cpu/jobs/{first}/heartbeat", {"lease_token": cl7["lease_token"], "lease_s": 120})[0], 200)
    check("restart: blobs survived", (call(p, "GET", f"/api/cpu/jobs/{j6}/payload")[1], call(p, "GET", f"/api/cpu/jobs/{j6}/patch")[1]),
          (b"P6", b"D6"))
    check("restart: the pending job can still be claimed after the restart",
          call(p, "POST", "/api/cpu/claim", {"runner_id": "rY", "lease_s": 30})[1]["job"]["id"], other)
    check("restart: finished jobs keep their results", call(p, "GET", f"/api/cpu/jobs/{j1}")[1]["result"]["exit_code"], 3)
    check("restart: outstanding_by_bundle sees the in-flight jobs (the queue's lane-release input)",
          sorted(cpu_lane.get_store().outstanding_by_bundle().get("bR", [])), sorted([j6, j7]))

    # ---- dashboard feed -------------------------------------------------------------------
    c, d = call(p, "GET", "/api/cpu-lane", token=None)
    blob = json.dumps(d)
    check("dashboard: counts + running (with runner id) + recent durations present",
          (c, d["enabled"], {r["runner"] for r in d["running"]} >= {"rX", "rY"}, bool(d["recent"]),
           "queue_depth" in d), (200, True, True, True, True))
    check("dashboard: read-only feed never exposes the command, env or token",
          ("echo hi" in blob, "do-not-leak" in blob, "TOKEN" in blob), (False, False, False))
    check("dashboard: the HTML carries the CPU lane section", "cpuLanePanel" in api.FRONTEND_HTML, True)

    # prune: finished rows/blobs older than the window go; live ones stay
    s = cpu_lane.get_store()
    real_clock = s.clock
    s.clock = lambda: time.time() + 30 * 86400
    n = s.prune()
    s.clock = real_clock
    check("prune: old finished rows are deleted, in-flight ones kept",
          (n >= 3, call(p, "GET", f"/api/cpu/jobs/{j1}")[0], call(p, "GET", f"/api/cpu/jobs/{first}")[0]), (True, 404, 200))
    S.stop()

    bad = RESULTS.count(False)
    print("CPU_LANE_API_TEST_OK" if not bad else f"{bad} FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
