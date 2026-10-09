#!/usr/bin/env python3
"""test-cpu-stage.py -- the pipeline's CPU-lane wrapper (cpu_stage.run_stage), Phase 6, 2026-10-08.

Everything runs against stubbed seams in a temp HOME / temp CPU_LANE_DIR: the real runner, the real
queue API and the real cpu-jobs store are NEVER contacted (cpu_stage._runner_online/_submit are
replaced; the store is a temp dir).

  1  KILL SWITCH   CPU_LANE=0 and ~/.ollama-dispatch/cpu-lane.disabled each force the caller's local path:
                   the remote seam is never touched and NO lane marker is registered
  2  FALLBACK      runner unreachable / _submit raising / ineligible tree -> the caller's local_fn result,
                   the reason is logged, a local marker is registered and cleared
  3  STALE         a worktree change while the runner "ran" the stage discards the remote result and
                   re-runs locally (reason STALE SNAPSHOT); an unchanged tree keeps the remote result
  4  HAPPY PATH    exit code / stdout / stderr pass through unchanged; the snapshot hash travels in the
                   job env and label; local_fn is not called
  5  NO LANE HOLD  while a stage runs (remote row OR local marker) outstanding_by_bundle lists the bundle and
                   ollama-queue.bundle_commit_status says "waiting"; afterwards nothing is outstanding
  6  CALL SITES    every pipeline file that owns a CPU stage routes it through cpu_stage with a local fallback

Usage: test-cpu-stage.py [--bin DIR]   (DIR holds cpu_stage.py, cpu_lane.py, ollama-queue.py ...)
Exit 0 + "CPU_STAGE_TEST_OK" = all pass.
"""
import argparse
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--bin", default=str(Path.home() / "bin"))
A = ap.parse_args()
BIN = Path(A.bin).resolve()

TMP = Path(tempfile.mkdtemp(prefix="test-cpu-stage-"))
os.environ["HOME"] = str(TMP / "home")
os.environ["CPU_LANE_DIR"] = str(TMP / "cpu-jobs")
os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
os.environ.pop("CPU_LANE", None)
(TMP / "home").mkdir()
sys.path.insert(0, str(BIN))
import cpu_lane          # noqa: E402
import cpu_stage as cs   # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   <<< " + str(detail)))


def sh(*a, cwd=None):
    return subprocess.run(a, cwd=cwd, capture_output=True, text=True, check=True).stdout


def make_repo(name, lock=True):
    r = TMP / name
    r.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=r)
    sh("git", "config", "user.email", "t@t", cwd=r)
    sh("git", "config", "user.name", "t", cwd=r)
    (r / "package.json").write_text('{"name":"t","private":true}\n')
    if lock:
        (r / "package-lock.json").write_text('{"name":"t","lockfileVersion":3,"packages":{}}\n')
    (r / "verify.sh").write_text('#!/bin/bash\necho VERIFY_OK\n')
    (r / ".gitignore").write_text("node_modules\n")
    sh("git", "add", "-A", cwd=r)
    sh("git", "commit", "-qm", "base", cwd=r)
    return str(r)


calls = {"submit": 0, "marker_begin": 0, "marker_end": 0, "online": 0}
state = {"online": True, "submit": None, "env": None, "label": None, "cmd": None, "markers": []}


def fake_online():
    calls["online"] += 1
    return state["online"], "http://stub", object()


def fake_submit(cj, api, wt, cmd, timeout_s, stage, bundle_id, tools, env, cwd_rel, label, claim_timeout_s):
    calls["submit"] += 1
    state["env"], state["label"], state["cmd"], state["tools"] = env, label, cmd, tools
    if state["submit"] is None:
        raise RuntimeError("stub: no submit behaviour set")
    return state["submit"](wt)


def fake_marker(stage, bundle_id, timeout_s):
    calls["marker_begin"] += 1
    state["markers"].append((stage, bundle_id))
    return lambda code=None: calls.__setitem__("marker_end", calls["marker_end"] + 1)


cs._runner_online, cs._submit, cs._marker = fake_online, fake_submit, fake_marker


def res(exit_code=0, out="", err="", timed_out=False):
    return types.SimpleNamespace(exit_code=exit_code, stdout_tail=out, stderr_tail=err, timed_out=timed_out,
                                 job_id="jobX", timings={"run_s": 1.5}, ran_on="runner")


def reset():
    for k in calls:
        calls[k] = 0
    state.update(online=True, submit=None, markers=[])
    os.environ.pop("CPU_LANE", None)
    try:
        cs.DISABLE_FILE.unlink()
    except OSError:
        pass


LOCAL = lambda: (3, "LOCAL-OUT", "LOCAL-ERR", False)   # noqa: E731
wt = make_repo("wt")

# ---- 1. kill switch ----------------------------------------------------------------------------
reset()
os.environ["CPU_LANE"] = "0"
state["submit"] = lambda w: res(0, "REMOTE")
r = cs.run_stage(wt, "bash verify.sh", 60, "ks-env", "b1", LOCAL)
check("1 kill switch env CPU_LANE=0: local path, remote never touched",
      r.ran_on == "local" and r.stdout == "LOCAL-OUT" and calls["submit"] == 0 and calls["online"] == 0, (r, calls))
check("1 kill switch env: NO lane marker (pre-lane behaviour exactly)", calls["marker_begin"] == 0, calls)
check("1 kill switch env: reason says so", "kill switch" in r.why, r.why)
reset()
cs.DISABLE_FILE.parent.mkdir(parents=True, exist_ok=True)
cs.DISABLE_FILE.write_text("")
r = cs.run_stage(wt, "bash verify.sh", 60, "ks-file", "b1", LOCAL)
check("1 kill switch FILE: local path, remote never touched, no marker",
      r.ran_on == "local" and calls["submit"] == 0 and calls["online"] == 0 and calls["marker_begin"] == 0, (r, calls))
reset()
state["submit"] = lambda w: res(0, "REMOTE")
r = cs.run_stage(wt, "bash verify.sh", 60, "ks-off", "b1", LOCAL)
check("1 control: switch off -> remote used", r.ran_on == "runner" and r.stdout == "REMOTE", r)

# ---- 2. fallback -------------------------------------------------------------------------------
reset()
state["online"] = False
r = cs.run_stage(wt, "bash verify.sh", 60, "fb-offline", "b2", LOCAL)
check("2 runner unreachable: caller's local result (exit/out/err untouched)",
      r.ran_on == "local" and (r.exit_code, r.stdout, r.stderr) == (3, "LOCAL-OUT", "LOCAL-ERR"), r)
check("2 runner unreachable: reason logged, NO marker by default (bundle keeps its lane = pre-lane behaviour)",
      "no CPU runner online" in r.why and calls["marker_begin"] == 0, (r.why, calls))
os.environ["CPU_LANE_LOCAL_MARKER"] = "1"
r = cs.run_stage(wt, "bash verify.sh", 60, "fb-offline-opt", "b2", LOCAL)
check("2 opt-in CPU_LANE_LOCAL_MARKER=1: marker registered AND cleared around the local run",
      calls["marker_begin"] == 1 and calls["marker_end"] == 1 and state["markers"] == [("fb-offline-opt", "b2")], calls)
os.environ.pop("CPU_LANE_LOCAL_MARKER")
check("2 runner unreachable: never submitted", calls["submit"] == 0, calls)
reset()


def boom(w):
    raise RuntimeError("cpu runner unavailable: upload put http 500")


state["submit"] = boom
r = cs.run_stage(wt, "bash verify.sh", 60, "fb-submit", "b2", LOCAL)
check("2 submit failure: falls back to local with the error as reason",
      r.ran_on == "local" and r.stdout == "LOCAL-OUT" and "upload put http 500" in r.why, r)
reset()
r = cs.run_stage(make_repo("nolock", lock=False), "bash verify.sh", 60, "fb-inel", "b2", LOCAL)
check("2 ineligible tree (package.json without lockfile): local, never submitted",
      r.ran_on == "local" and "ineligible" in r.why and calls["submit"] == 0, (r.why, calls))
nonode = TMP / "nonode"
nonode.mkdir()
sh("git", "init", "-q", "-b", "main", cwd=nonode)
(nonode / "a.py").write_text("x=1\n")
sh("git", "add", "-A", cwd=nonode)
sh("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b", cwd=nonode)
reset()
r = cs.run_stage(str(nonode), "python3 a.py", 60, "fb-nonode", "b2", LOCAL)
check("2 non-node tree stays local (runner python/toolchain differs)", r.ran_on == "local" and calls["submit"] == 0, r.why)
w2 = make_repo("hostpath")
Path(w2, "verify.sh").write_text("#!/bin/bash\ncat /Users/someone/secret.txt\n")
reset()
r = cs.run_stage(w2, "bash verify.sh", 60, "fb-host", "b2", LOCAL)
check("2 verify naming a host-only path stays local", r.ran_on == "local" and "host-only path" in r.why, r.why)
w3 = make_repo("netneed")
Path(w3, "verify.sh").write_text("#!/bin/bash\nnpx --yes some-unlisted-tool --run\n")
reset()
r = cs.run_stage(w3, "bash verify.sh", 60, "fb-npx", "b2", LOCAL)
check("2 `npx --yes <tool not in the lockfile>` (needs network) stays local",
      r.ran_on == "local" and "needs network" in r.why, r.why)

# ---- 3. stale snapshot -------------------------------------------------------------------------
reset()


def mutate_then_return(w):
    Path(w, "late-write.txt").write_text("a writer that ignored the tree lock\n")
    return res(0, "REMOTE-STALE-OUT")


state["submit"] = mutate_then_return
r = cs.run_stage(make_repo("stale"), "bash verify.sh", 60, "stale", "b3", LOCAL)
check("3 worktree changed while the runner ran: remote result DISCARDED, local re-run",
      r.ran_on == "local" and r.stdout == "LOCAL-OUT" and "STALE SNAPSHOT" in r.why, r)
check("3 stale: local re-run happened (no marker by default)", calls["marker_begin"] == 0, calls)
reset()
state["submit"] = lambda w: res(0, "REMOTE-OK")
r = cs.run_stage(make_repo("fresh"), "bash verify.sh", 60, "fresh", "b3", LOCAL)
check("3 control: unchanged tree keeps the remote result", r.ran_on == "runner" and r.stdout == "REMOTE-OK", r)

# ---- 4. happy path -----------------------------------------------------------------------------
reset()
w = make_repo("happy")
state["submit"] = lambda x: res(7, "VERIFY-OUT\nVERIFY_OK\n", "an err line\n")
seen_local = []
r = cs.run_stage(w, "bash verify.sh", 60, "happy", "b4", lambda: (seen_local.append(1), (0, "", "", False))[1])
snap = cs.snapshot(w)
check("4 remote exit code / stdout / stderr pass through unchanged",
      (r.exit_code, r.stdout, r.stderr, r.timed_out, r.ran_on) == (7, "VERIFY-OUT\nVERIFY_OK\n", "an err line\n", False, "runner"), r)
check("4 local_fn is NOT called and no marker is registered by us on the remote path",
      not seen_local and calls["marker_begin"] == 0, (seen_local, calls))
check("4 HEAD+diff hash travels in the job env and label",
      state["env"].get("DISPATCH_SNAPSHOT") == snap and snap in (state["label"] or ""), (state["env"], state["label"], snap))
check("4 job env carries the caller's env + the sandbox marker is passed by callers",
      state["env"].get("DISPATCH_VERIFY_SANDBOX") == "1", state["env"])
check("4 shipped tools include the relevance tools under bin/ and the ollama-queue basename stub",
      all(k in state["tools"] for k in ("bin/verify-relevance.py", "bin/pgrun.py", "bin/cpu_vr_measure.py")),
      sorted(state["tools"]))
check("4 the remote command links $HOME/bin to the shipped tools", "$JOB_TOOLS/bin" in state["cmd"], state["cmd"])
Path(w, "new.txt").write_text("x\n")
check("4 snapshot changes with an untracked file", cs.snapshot(w) != snap)
sh("git", "checkout", "--", ".", cwd=w) if False else None
reset()
state["submit"] = lambda x: res(None, "", "", True)
r = cs.run_stage(make_repo("tmo"), "bash verify.sh", 5, "timeout", "b4", LOCAL)
check("4 remote timeout is reported as timed_out (callers map it to their own timeout handling)",
      r.ran_on == "runner" and r.timed_out, r)
reset()
big = cs.GZB64_SUFFIX and None
import base64, gzip   # noqa: E402,E401
payload = json.dumps({"verdict": "relevant", "x": "y" * 500000})
b64 = base64.b64encode(gzip.compress(payload.encode())).decode()
state["submit"] = lambda x: res(0, b64)
r = cs.run_stage(make_repo("gz"), "echo", 5, "gz", "b4", LOCAL, gzb64=True,
                 check=lambda rr: None if json.loads(rr.stdout)["verdict"] == "relevant" else "bad")
check("4 gzip+base64 transport decodes a large JSON record intact", r.ran_on == "runner" and json.loads(r.stdout)["x"] == "y" * 500000, r.why)
state["submit"] = lambda x: res(0, "not-base64-!!")
r = cs.run_stage(make_repo("gz2"), "echo", 5, "gz2", "b4", LOCAL, gzb64=True)
check("4 undecodable remote record -> local fallback", r.ran_on == "local" and "undecodable" in r.why, r)
reset()
state["submit"] = lambda x: res(0, "{}")
r = cs.run_stage(make_repo("veto"), "echo", 5, "veto", "b4", LOCAL, check=lambda rr: "no verdict")
check("4 check() veto -> local fallback", r.ran_on == "local" and "vetoed" in r.why, r)

# ---- 5. no lane hold ---------------------------------------------------------------------------
reset()
os.environ["CPU_LANE_DIR"] = str(TMP / "cpu-jobs")
store = cpu_lane.Store(cpu_lane.BASE)
loader = importlib.machinery.SourceFileLoader("oq_cs", str(BIN / "ollama-queue.py"))
q = importlib.util.module_from_spec(importlib.util.spec_from_loader("oq_cs", loader))
sys.modules["oq_cs"] = q
loader.exec_module(q)
pk = lambda j: j.get("bundle")   # noqa: E731
BK = "bundle-nolanehold"
saw = {}
# real markers via the REAL cpu_dispatch marker (temp store)
del cs._marker
cs._marker = __import__("cpu_dispatch")._marker


def local_watch():
    out = cpu_lane.Store(cpu_lane.BASE).outstanding_by_bundle()
    saw["local"] = (BK in out, q.bundle_commit_status(BK, [], pk, {}, {}, cpu_wait=out.get(BK))[0])
    return 0, "ok", "", False


state["online"] = False
os.environ["CPU_LANE_LOCAL_MARKER"] = "1"
r = cs.run_stage(wt, "bash verify.sh", 60, "nolane-local", BK, local_watch)
os.environ.pop("CPU_LANE_LOCAL_MARKER")
check("5 local-fallback stage in flight: bundle listed outstanding AND commit status 'waiting' (opt-in: holds no GPU lane)",
      saw.get("local") == (True, "waiting"), saw)
check("5 ...and nothing is outstanding after it ends", BK not in store.outstanding_by_bundle(), store.outstanding_by_bundle())


def remote_watch(w):
    jid = store.create({"cmd": "x", "timeout_s": 60}, label="l", stage="nolane-remote", bundle_id=BK)
    out = cpu_lane.Store(cpu_lane.BASE).outstanding_by_bundle()
    saw["remote"] = (BK in out, q.bundle_commit_status(BK, [], pk, {}, {}, cpu_wait=out.get(BK))[0])
    if jid:
        store.cancel(jid)
    return res(0, "R")


state["online"] = True
state["submit"] = remote_watch
r = cs.run_stage(wt, "bash verify.sh", 60, "nolane-remote", BK, LOCAL)
check("5 remote stage in flight: bundle outstanding AND commit status 'waiting'",
      saw.get("remote") == (True, "waiting"), saw)
check("5 control: a RUNNING row of the bundle keeps it 'working' (the lane hold is unchanged for real work)",
      q.bundle_commit_status(BK, [{"bundle": BK, "status": "running"}], pk, {}, {}, cpu_wait=["j"])[0] == "working")

# ---- 6. call sites ------------------------------------------------------------------------------
site = {
    "ollama-dispatch-auto": ["cpu_stage", "_cpu_lane_run(", "\"harness-check\""],
    "ollama-dispatch-preflight": ["cpu_stage", "sh_lane(", "\"baseline-verify\"", "\"refimpl-verify\"", "measure_relevance_lane("],
    "ollama-dispatch-slice": ["cpu_stage", "_lane_stage(", "_verify_sh_lane(", "\"harness-check\""],
    "gate-on-complete.py": ["cpu_stage", "_relevance_run(", "\"relevance\""],
}
for f, needles in site.items():
    t = (BIN / f).read_text()
    miss = [n for n in needles if n not in t]
    check(f"6 call site {f} routes its CPU stage through cpu_stage", not miss, miss)
t = (BIN / "ollama-dispatch-preflight").read_text()
check("6 preflight: raw self.sh(self.a.verify) no longer used for the whole-stage verifies",
      "self.sh(self.a.verify)" not in t)
t = (BIN / "gate-on-complete.py").read_text()
check("6 gate: measure_relevance no longer runs verify-relevance with a bare subprocess.run",
      "r = subprocess.run(cmd, capture_output=True, text=True,\n                           timeout=RELEVANCE_BUDGET_S + 900)\n        rec = json.loads" not in t)

print()
print("CPU_STAGE_TEST_OK" if all(RESULTS) else "CPU_STAGE_TEST_FAILED (%d of %d failed)" % (RESULTS.count(False), len(RESULTS)))
sys.exit(0 if all(RESULTS) else 1)
