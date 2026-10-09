#!/usr/bin/env python3
"""Auto-land a gate-PASSED slice with no driver (2026-09-27, BFMR s2).

The owner: "why isn't that automated if the slice is good?" -- s2 sat ESCALATED with
job_id None while its coding job had PASSED its gate; it predates escalated_at, so
adopt_post_escalation_job could not see it, and the driver had exited.

Asserts:
  * a LEGACY escalated slice (no escalated_at, job_id None) whose newest coding job
    passed its gate is auto-landed through the normal path: shared-tail resolve onto
    a chain that already holds a later slice, every landed slice's verify.sh re-run,
    DONE, committed as "slice s2: ...";
  * a slice whose newest coding job FAILED its gate is NOT landed (an older pass does
    not count), and neither is a job that did not finish clean;
  * a worktree that no longer holds the passed diff is refused with the real reason,
    stays ESCALATED, and is not retried until the chain tip moves;
  * a land whose rebase re-verify fails stays ESCALATED with that reason;
  * the 5-min --sweep kicks a plan that has such a slice (no driver needed);
  * execute() runs the auto-land after harvest (wiring).
Red on revert: AUTOLAND_REVERT=1 disables auto_land_passed -> the landing asserts fail.
Sandboxed: temp HOME, temp git repos, temp LOG_DIR. Run: python3 test-slice-auto-land-passed.py
"""
import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(__file__).resolve().parent
SLICER = os.environ.get("SLICER") or str(BIN / "ollama-dispatch-slice")
failures = []


def ok(name, got, want=True):
    good = got == want
    print(f"  {'ok  ' if good else 'FAIL'} {name}" + ("" if good else f": got {got!r} want {want!r}"))
    if not good:
        failures.append(name)


def load():
    src = Path(SLICER).read_text()
    if os.environ.get("AUTOLAND_REVERT"):
        src = src.replace('    finder = finder or slice_coding_jobs\n',
                          '    return []\n    finder = finder or slice_coding_jobs\n', 1)
    tmp = Path(tempfile.mkdtemp()) / "slicer.py"
    tmp.write_text(src)
    loader = SourceFileLoader("slicer_autoland", str(tmp))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m, src


BASE = ("'use strict';\n\nconst ORDERS_URL = 'u';\n\nfunction isLoggedOut() { return 1; }\n\n"
        "module.exports = { ORDERS_URL, isLoggedOut };\n")
S2 = "function installInterceptor() { return 2; }\n\n"
S3 = "async function confirmLoggedIn() { return 3; }\n\n"


def with_fn(text, fn, name):
    head, tail = text.rsplit("module.exports", 1)
    names = tail.split("{", 1)[1].split("}", 1)[0]
    return head + fn + "module.exports = {" + names.rstrip() + ", " + name + " };\n"


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], check=True,
                          capture_output=True, text=True).stdout.strip()


def verify_sh(must):
    return ("#!/bin/bash\nset -e\n"
            f"node -e \"const m=require('./a.js'); if (typeof m.{must} !== 'function') process.exit(1)\"\n"
            "echo VERIFY_OK\n")


def build(m, logs, s3_verify_extra="", s2_tree=None):
    """Chain: s1 (seed) + s3 landed. s2: legacy ESCALATED, job_id None, worktree holds
    the diff its coding job J2 was gated on. Returns (st, cwt, wts, seed)."""
    # each scenario is its own run: a persisted DONE from the previous one would
    # (correctly) never be undone by merge_terminal_facts
    try:
        os.unlink(m.state_path("bfmr"))
    except OSError:
        pass
    m._STATE_BASE.clear()
    root = Path(tempfile.mkdtemp(prefix="autoland-repo-"))
    cwt = root / "chain"
    cwt.mkdir()
    git(cwt, "init", "-q")
    git(cwt, "config", "user.email", "t@t")
    git(cwt, "config", "user.name", "t")
    (cwt / "a.js").write_text(BASE)
    git(cwt, "add", "a.js")
    git(cwt, "commit", "-qm", "slice s1: base")
    seed = git(cwt, "rev-parse", "HEAD")
    wts = {}
    for sid in ("s1", "s2", "s3"):
        w = root / f"wt-{sid}"
        git(cwt, "worktree", "add", "-q", "--detach", str(w), seed)
        wts[sid] = w
    (wts["s1"] / "verify.sh").write_text(verify_sh("isLoggedOut"))
    (wts["s3"] / "a.js").write_text(with_fn(BASE, S3, "confirmLoggedIn"))
    (wts["s3"] / "verify.sh").write_text(verify_sh("confirmLoggedIn") + s3_verify_extra)
    (cwt / "a.js").write_text(with_fn(BASE, S3, "confirmLoggedIn"))
    git(cwt, "add", "a.js")
    git(cwt, "commit", "-qm", "slice s3: confirmLoggedIn")
    (wts["s2"] / "a.js").write_text(with_fn(BASE, S2, "installInterceptor"))
    (wts["s2"] / "verify.sh").write_text(verify_sh("installInterceptor"))
    diff = git(wts["s2"], "diff", "HEAD", "--", "a.js") + "\n"
    if s2_tree is not None:                     # the tree moved on after the gate
        (wts["s2"] / "a.js").write_text(s2_tree)
    st = {"label": "bfmr", "target": "a.js", "repo": str(root),
          "order": ["s1", "s2", "s3"],
          "slices": {"s1": {"status": "done", "worktree": str(wts["s1"]), "depends_on": [],
                            "title": "base"},
                     "s2": {"status": "escalated", "job_id": None, "depends_on": ["s1"],
                            "worktree": str(wts["s2"]), "title": "installInterceptor",
                            "escalation_reason": "relevance NO-GO (stale)"},
                     "s3": {"status": "done", "worktree": str(wts["s3"]), "depends_on": [],
                            "title": "confirmLoggedIn"}}}
    return st, cwt, wts, seed, diff


def sidecar(logs, jid, label, status="done", exit_code=0, verdict="pass", at="2026-09-27T20:24:32Z",
            head=None, diff=""):
    (logs / f"{jid}-{label}.log").write_text("log")
    (logs / f"{jid}.done.json").write_text(json.dumps({
        "label": label, "status": status, "exit_code": exit_code, "persisted_at": at,
        "launch_baseline": {"head": head, "dirty": 0} if head else None}))
    if verdict is not None:
        (logs / f"{jid}.gate.json").write_text(json.dumps({"verdict": verdict}))
    (logs / f"{jid}.diff").write_text(diff)


def main():
    home = tempfile.mkdtemp(prefix="autoland-home-")
    os.environ["HOME"] = home
    m, src = load()
    no_live = lambda *_a: None                  # noqa: E731 -- no queue in the sandbox

    def fresh_logs():
        d = Path(tempfile.mkdtemp(prefix="autoland-logs-"))
        m.LOG_DIR = str(d)
        return d

    print("pure: passed_job_to_land")
    st0 = {"label": "p", "order": ["s1", "s2"],
           "slices": {"s1": {"status": "done", "depends_on": []},
                      "s2": {"status": "escalated", "job_id": None, "depends_on": ["s1"]}}}
    J = lambda jid, at, st="done", ec=0: (at, jid, {"status": st, "exit_code": ec})  # noqa: E731
    gate = {"new": {"verdict": "pass"}, "old": {"verdict": "pass"}, "bad": {"verdict": "fail"}}.get
    ok("legacy escalated (no escalated_at, no job_id) + newest PASS -> land it",
       m.passed_job_to_land(st0, "s2", [J("new", "2")], gate)[0], "new")
    ok("newest job gate FAIL -> not landed, even with an older pass",
       m.passed_job_to_land(st0, "s2", [J("bad", "3"), J("old", "1")], gate)[0], None)
    ok("newest job did not finish clean -> not landed",
       m.passed_job_to_land(st0, "s2", [J("new", "2", "failed", 1)], gate)[0], None)
    ok("no finished coding job -> nothing", m.passed_job_to_land(st0, "s2", [], gate)[0], None)
    st0["slices"]["s1"]["status"] = "pending"
    ok("dependency not satisfied -> not landed",
       m.passed_job_to_land(st0, "s2", [J("new", "2")], gate)[0], None)
    st0["slices"]["s1"]["status"] = "done"
    st0["slices"]["s2"].update(status="enqueued", job_id="new")
    ok("ENQUEUED on its own job -> left to the poll/harvest",
       m.passed_job_to_land(st0, "s2", [J("new", "2")], gate)[0], None)
    st0["slices"]["s2"].update(job_id="running1")
    ok("ENQUEUED on a job that has not finished -> left alone",
       m.passed_job_to_land(st0, "s2", [J("new", "2")], gate)[0], None)
    st0["slices"]["s2"].update(status="escalated", job_id=None,
                               auto_land_refused={"job": "new", "satisfied": 1})
    ok("refused at this chain tip -> not retried",
       m.passed_job_to_land(st0, "s2", [J("new", "2")], gate)[0], None)
    st0["slices"]["s1b"] = {"status": "done"}
    ok("...retried once the chain tip moves",
       m.passed_job_to_land(st0, "s2", [J("new", "2")], gate)[0], "new")

    if subprocess.run(["which", "node"], capture_output=True).returncode != 0:
        ok("node available for the e2e verify.sh", False)
        return finish()

    print("end to end: legacy escalated s2 with a passed job")
    logs = fresh_logs()
    st, cwt, wts, seed, diff = build(m, logs)
    sidecar(logs, "0a7ff4902d90", "bfmr-s2", head=seed, diff=diff)
    sidecar(logs, "111111111111", "auto-author-bfmr-s2", verdict=None)   # not a coding job
    ok("finder sees only the coding job", [j for _, j, _ in m.slice_coding_jobs(st, "s2")],
       ["0a7ff4902d90"])
    landed = m.auto_land_passed(st, str(cwt), inflight_fn=no_live)
    s2 = st["slices"]["s2"]
    ok("s2 AUTO-LANDED", landed, ["s2"])
    ok("s2 is DONE on the passed job, escalation cleared",
       (s2["status"], s2.get("job_id"), s2.get("escalation_reason")), ("done", "0a7ff4902d90", None))
    ok("committed onto the chain as slice s2",
       git(cwt, "log", "-1", "--format=%s").startswith("slice s2:"))
    tip = (cwt / "a.js").read_text()
    ok("chain holds both functions, s2 before s3 (shared-tail resolved in plan order)",
       0 <= tip.find("installInterceptor()") < tip.find("confirmLoggedIn()"))
    ok("export list unioned",
       "module.exports = { ORDERS_URL, isLoggedOut, installInterceptor, confirmLoggedIn };" in tip)
    ok("persisted DONE", json.loads(Path(m.state_path("bfmr")).read_text())
       ["slices"]["s2"]["status"], "done")
    ok("second pass is a no-op", m.auto_land_passed(st, str(cwt), inflight_fn=no_live), [])

    print("a FAILED newest job is not landed")
    logs = fresh_logs()
    st, cwt, wts, seed, diff = build(m, logs)
    sidecar(logs, "aaaaaaaaaaaa", "bfmr-s2", head=seed, diff=diff, at="2026-09-27T10:00:00Z")
    sidecar(logs, "bbbbbbbbbbbb", "bfmr-s2", head=seed, diff=diff, verdict="fail",
            at="2026-09-27T11:00:00Z")
    head0 = git(cwt, "rev-parse", "HEAD")
    ok("nothing landed", m.auto_land_passed(st, str(cwt), inflight_fn=no_live), [])
    ok("s2 stays ESCALATED with its own reason",
       (st["slices"]["s2"]["status"], st["slices"]["s2"]["escalation_reason"]),
       ("escalated", "relevance NO-GO (stale)"))
    ok("chain untouched", git(cwt, "rev-parse", "HEAD"), head0)

    print("a worktree that moved on is refused with the real reason, once")
    logs = fresh_logs()
    st, cwt, wts, seed, diff = build(m, logs, s2_tree=with_fn(BASE, S2.replace("2", "9"),
                                                              "installInterceptor"))
    sidecar(logs, "cccccccccccc", "bfmr-s2", head=seed, diff=diff)
    calls = []
    land = lambda *a, **k: calls.append(a) or (True, "x")             # noqa: E731
    ok("not landed", m.auto_land_passed(st, str(cwt), inflight_fn=no_live, land_fn=land), [])
    ok("the land path was never reached", calls, [])
    ok("ESCALATED with the mismatch reason",
       (st["slices"]["s2"]["status"], "no longer matches" in st["slices"]["s2"]["escalation_reason"]),
       ("escalated", True))
    ok("not retried at the same chain tip",
       m.passed_job_to_land(st, "s2", m.slice_coding_jobs(st, "s2"))[0], None)

    print("a rebase whose landed-slice re-verify fails stays ESCALATED")
    logs = fresh_logs()
    st, cwt, wts, seed, diff = build(
        m, logs, s3_verify_extra="grep -q installInterceptor a.js && exit 1 || true\n")
    sidecar(logs, "dddddddddddd", "bfmr-s2", head=seed, diff=diff)
    head0 = git(cwt, "rev-parse", "HEAD")
    ok("not landed", m.auto_land_passed(st, str(cwt), inflight_fn=no_live), [])
    r = st["slices"]["s2"]
    ok("ESCALATED with the re-verify reason",
       (r["status"], "verify.sh" in (r.get("escalation_reason") or "")), ("escalated", True))
    ok("chain untouched", git(cwt, "rev-parse", "HEAD"), head0)

    print("the sweep kicks it with no driver")
    logs = fresh_logs()
    st, cwt, wts, seed, diff = build(m, logs)
    sidecar(logs, "eeeeeeeeeeee", "bfmr-s2", head=seed, diff=diff)
    why = m.plan_needs_kick(st, {}, lambda _j: None, None, False, idle_age=0,
                            passed_land=lambda s_, sid_: m.passed_job_to_land(
                                s_, sid_, m.slice_coding_jobs(s_, sid_))[0])
    ok("plan_needs_kick names the passed job", bool(why) and "eeeeeeeeeeee" in why, True)
    ok("...but never while a live driver owns the plan",
       m.plan_needs_kick(st, {}, lambda _j: None, {"pid": 1}, False,
                         passed_land=lambda *_a: "x"), None)
    plan_file = Path(home) / "plan.json"
    plan_file.write_text("{}")
    st["plan_path"] = str(plan_file)
    os.makedirs(m.STATE_ROOT, exist_ok=True)
    Path(m.state_path("bfmr")).write_text(json.dumps(st))
    fired = []
    m.sweep_stranded_advances(fire=lambda s_, p_: fired.append(s_["label"]), statuses={},
                              verbose=False)
    ok("--sweep fires one advance for the plan", fired, ["bfmr"])

    print("wiring")
    tree = ast.parse(src)
    ex = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "execute")
    names = [c.func.id for c in ast.walk(ex) if isinstance(c, ast.Call)
             and isinstance(c.func, ast.Name)]
    ok("execute() calls auto_land_passed after harvest_ready_slices",
       "auto_land_passed" in names and names.index("harvest_ready_slices")
       < names.index("auto_land_passed"))
    return finish()


def finish():
    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'ALL PASSED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
