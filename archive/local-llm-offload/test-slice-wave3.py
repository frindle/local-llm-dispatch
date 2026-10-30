#!/usr/bin/env python3
"""Wave 3 slicer pins: --baseline-ref forwarding, SPEC_DEFECT routing, --lane plumbing.
SLICE_SRC overrides the file under test."""
import importlib.util, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SLICE = Path(os.environ.get("SLICE_SRC") or HERE / "ollama-dispatch-slice")
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    ld = SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    argv, sys.argv = sys.argv, [str(path)]
    try:
        ld.exec_module(m)
    finally:
        sys.argv = argv
    return m


m = load(SLICE, "slice_w3")
tmp = Path(tempfile.mkdtemp(prefix="sw3-"))
subprocess.run(["git", "init", "-q", str(tmp)], check=True)
(tmp / "f").write_text("x")
subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True)
subprocess.run(["git", "-C", str(tmp), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b"], check=True)
sha = subprocess.run(["git", "-C", str(tmp), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
chk("_baseline_args forwards the chain tip sha", m._baseline_args(tmp), ["--baseline-ref", sha])
chk("_baseline_args on a non-repo is empty (never breaks the launch)", m._baseline_args(tmp / "nope"), [])

sd = "ERROR: SPEC_DEFECT: creation target exists at base: lib/x.ts -- the task CREATES lib/x.ts"
chk("SPEC_DEFECT is a recognised escalation kind", "spec defect" in m.escalation_kind(sd), True)
chk("...already-satisfied has its own kind", "already satisfied" in m.escalation_kind(
    "SPEC_DEFECT: already satisfied at base: lib/x.ts -- nothing left"), True)
chk("is_spec_defect", (m.is_spec_defect(sd), m.is_spec_defect("PLAN SUSPECT: x")), (True, False))
reason, det = m.auto_failure_reason("noise\n" + sd + " [auto] no authoring job was enqueued.\n")
chk("AUTO's SPEC_DEFECT stop is a DETERMINISTIC failure (first sight)", det, True)
chk("...and its reason survives into the slice", "SPEC_DEFECT" in (reason or ""), True)
s = {"intent": "Create lib/x.ts"}
chk("respec applies once on a spec defect", m.respec_on_cap(s, sd, 1, 1), True)
chk("...folding in the contradicts-the-base guidance", "SPEC_DEFECT" in s["intent"] and "BASE tree" in s["intent"], True)
chk("...but only once", m.respec_on_cap(s, sd, 1, 1), False)

chk("default lane forwards nothing to AUTO", m._lane_args_auto({}), [])
chk("--lane fixed is forwarded to AUTO", m._lane_args_auto({"lane": "fixed"}), ["--lane", "fixed"])
os.environ["DISPATCH_LANE"] = "auto"
chk("DISPATCH_LANE env is the fallback", m._lane_args_auto({}), ["--lane", "auto"])
del os.environ["DISPATCH_LANE"]
chk("garbage lane degrades to open", m._lane_of({"lane": "zzz"}), "open")
chk("open lane adds no enqueue args", m._lane_enqueue_args({}, str(tmp), ["f"]), [])
# --- the live handler: SPEC_DEFECT -> re-spec on first sight, then escalate -------------------
m.slice_job_awaiting_gate = lambda st, sid: None
m.retry_guard = lambda st, sid, s, wt, **k: (True, "", [])
m.save_state = lambda st: None
m.author_budget_block = lambda st, sid, s: None
removed = []
m.remove_worktree = lambda cwt, wt: removed.append(wt)
sat = []
m.park_if_already_satisfied = lambda *a: sat.append(a)
st = {"label": "p"}
s = {"intent": "Create lib/x.ts", "last_auto_error": sd, "status": "PENDING"}
chk("SPEC_DEFECT on first sight: another (re-specced) attempt is warranted",
    m.bound_stale_worktree_retry(st, "s1", s, str(tmp), str(tmp), "test"), True)
chk("...the worktree was cleared and the slice re-specced", (len(removed), s.get("respec_count")), (1, 1))
chk("...it did not take the already-satisfied path", sat, [])
s["last_auto_error"] = sd
chk("the SAME defect after the re-spec escalates",
    m.bound_stale_worktree_retry(st, "s1", s, str(tmp), str(tmp), "test"), False)
chk("...as ESCALATED with the SPEC_DEFECT reason", (s["status"] == m.ESCALATED, "SPEC_DEFECT" in s["escalation_reason"]), (True, True))
s2 = {"intent": "x", "last_auto_error": "ERROR: SPEC_DEFECT: already satisfied at base: a.ts -- nothing", "status": "PENDING"}
chk("'already satisfied at base' parks via the already-satisfied path", (
    m.bound_stale_worktree_retry(st, "s2", s2, str(tmp), str(tmp), "test"), len(sat)), (False, 1))
# --- the real AUTO launch argv (what execute() runs) -----------------------------------------
stx = {"label": "plan", "lang": "ts", "target": "lib/x.ts", "bundle": "bun", "slices": []}
sx = {"title": "t", "intent": "do it", "must_contain": ["foo"], "files": []}
argv = m.auto_launch_argv(stx, "s1", sx, str(tmp), "/tmp/wt", False)
chk("AUTO launch argv carries --baseline-ref <chain tip sha>",
    argv[argv.index("--baseline-ref") + 1] if "--baseline-ref" in argv else None, sha)
chk("...no --lane for the default open lane", "--lane" in argv, False)
chk("...the bundle and require flags are still there", ("--bundle" in argv, "--require=foo" in argv), (True, True))
stx["lane"] = "fixed"
chk("--lane fixed reaches AUTO's argv", m.auto_launch_argv(stx, "s1", sx, str(tmp), "/tmp/wt")[-0:].count("fixed") >= 1, True)
chk("--resume-author is appended on resume", m.auto_launch_argv(stx, "s1", sx, str(tmp), "/tmp/wt", True)[-1], "--resume-author")
print()
print(f"FAILED {FAILS}" if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
