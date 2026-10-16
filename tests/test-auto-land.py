#!/usr/bin/env python3
"""Behavioural test: gate-on-complete AUTO-LAND (2026-10-09).

A coding PASS lands on the origin repo's default branch ONLY when ALL hold:
clean PASS, second opinion AGREES, code-only diff (no scaffold), repo HEAD still the
chain baseline, repo validate command passes before push. Any miss -> nothing moves
(never partial). Landing is integrate --stage + ff-only (never a branch merge).
AUTO_LAND=0 opts out. Hermetic: temp git repos + a temp bare origin; no queue.

Usage: python3 test-auto-land.py [--bin DIR | path/to/gate-on-complete.py]
Prints AUTO_LAND_OK when every check passes.
"""
import hashlib, importlib.machinery, importlib.util, json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

arg = sys.argv[1:]
if arg and arg[0] == "--bin":
    target = Path(arg[1]) / "gate-on-complete.py"
else:
    target = Path(arg[0] if arg else Path(__file__).with_name("gate-on-complete.py"))
os.environ.pop("GATE_TEST_MODE", None)
os.environ.pop("AUTO_LAND", None)
_ld = importlib.machinery.SourceFileLoader("goc_autoland", str(target))
g = importlib.util.module_from_spec(importlib.util.spec_from_loader("goc_autoland", _ld))
_ld.exec_module(g)
g.TEST_MODE = False
g.BIN = target.parent
g._job_field = lambda *a, **k: None
g.job_facts = lambda *a, **k: {}

ok = True


def check(name, got, want):
    global ok
    good = got == want
    ok &= good
    print(("PASS " if good else "FAIL ") + name + ("" if good else f": got {got!r} want {want!r}"))


env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(*a, cwd):
    r = subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True, env=env)
    return r.stdout.strip()


td = Path(tempfile.mkdtemp(prefix="auto-land-t-"))
_n = [0]


def make_case(validate="test -f src/app.js", scaffold_only_target=False):
    """origin repo (main, remote bare) + a worktree branch with code + scaffold."""
    _n[0] += 1
    base = td / f"c{_n[0]}"
    base.mkdir()
    bare = base / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], env=env)
    repo = base / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "README").write_text("r\n")
    (repo / ".dispatch-validate").write_text(f"# test\n{validate}\n")
    git("add", "-A", cwd=repo); git("commit", "-qm", "init", cwd=repo)
    git("remote", "add", "origin", str(bare), cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    git("branch", "--set-upstream-to=origin/main", "main", cwd=repo)
    wt = base / "wt"
    git("worktree", "add", "-q", "-b", "dispatch-x", str(wt), "main", cwd=repo)
    (wt / "src").mkdir()
    (wt / "src" / "app.js").write_text("export const x = 1;\n")
    (wt / "TASK.md").write_text("scaffold\n")
    (wt / "verify.sh").write_text("#!/bin/sh\nexit 0\n")
    git("add", "-A", cwd=wt); git("commit", "-qm", "dispatch", cwd=wt)
    mb = git("merge-base", "main", "HEAD", cwd=wt)
    (wt / ".preflight-state.json").write_text(json.dumps(
        {"declared_scope": ["src/app.js"], "baseline_commit": mb}))
    h = hashlib.sha256((wt / "src" / "app.js").read_bytes()).hexdigest()
    payload = {"verdict": "pass", "issues": [], "review_verdict": "PASS", "cwd": str(wt),
               "changed_files": ["src/app.js", "TASK.md", "verify.sh"],
               "auto_pipeline_action": "apply", "auto_pipeline_stage": "code",
               "second_opinion_agreement": "agree", "second_opinion": {"review": "done"},
               "judged": {"files": {"src/app.js": h}, "toplevel": str(wt)}}
    gj = base / "j.gate.json"
    gj.write_text(json.dumps(payload))
    return dict(repo=repo, wt=wt, payload=payload, gj=gj, bare=bare)


def land(c, jid="job1"):
    g.auto_land_consider(jid, c["payload"], c["gj"])
    return json.loads(c["gj"].read_text()).get("auto_land") or {}


def held(name, c, needle):
    n0 = git("rev-list", "--count", "main", cwd=c["repo"])
    r = land(c)
    check(f"{name}: held, nothing landed",
          (r.get("status"), needle in " ".join(r.get("reasons") or []), (c["repo"] / "src").exists(),
           git("rev-list", "--count", "main", cwd=c["repo"]) == n0,
           git("branch", "--list", "integrate/*", cwd=c["repo"])),
          ("held", True, False, True, ""))


# --- all conditions hold -> landed -------------------------------------------------
c = make_case()
r = land(c)
check("all conditions hold -> landed", r.get("status"), "landed")
check("main has the code file, content intact", (c["repo"] / "src" / "app.js").read_text(),
      "export const x = 1;\n")
check("scaffold NOT on main", [(c["repo"] / f).exists() for f in ("TASK.md", "verify.sh")], [False, False])
check("pushed: bare origin main == local main",
      git("rev-parse", "main", cwd=c["bare"]), git("rev-parse", "main", cwd=c["repo"]))
check("linear history, no merge commit (never --no-ff of the worktree branch)",
      git("rev-list", "--merges", "--count", "main", cwd=c["repo"]), "0")
check("scratch integrate/* branch cleaned up", git("branch", "--list", "integrate/*", cwd=c["repo"]), "")
check("landing recorded in AUTO-FIX-QUEUE.md",
      "auto-land landed" in (c["gj"].parent / "AUTO-FIX-QUEUE.md").read_text(), True)
head = git("rev-parse", "main", cwd=c["repo"])
land(c)
check("idempotent: second pass is a no-op", git("rev-parse", "main", cwd=c["repo"]), head)

# --- (1) clean PASS --------------------------------------------------------------------
c = make_case(); c["payload"]["verdict"] = "concerns"
held("verdict concerns", c, "not a clean PASS")
c = make_case(); c["payload"]["issues"] = [{"category": "code", "severity": "low"}]
held("code finding on a pass", c, "not a clean PASS")
# --- (2) second opinion ----------------------------------------------------------------
c = make_case(); c["payload"]["second_opinion_agreement"] = "disagree"
c["payload"]["second_opinion_disagreement"] = True
held("second opinion disagrees", c, "DISAGREES")
c = make_case(); c["payload"].pop("second_opinion_agreement")
c["payload"]["second_opinion"] = {"review": "pending"}
held("second opinion not finished", c, "not agreed")
# --- (3) code-only ------------------------------------------------------------------------
c = make_case(); c["payload"]["changed_files"] = ["src/other.js"]
held("staged file the gate never judged", c, "never judged")
check("pure: a scaffold file in the staged diff is refused",
      any("scaffold" in w for w in g.auto_land_stage_reasons(
          {"overall": "clean", "merge_base": "a", "main_tip": "a", "behind": 0},
          {"staged": True, "diff_files": ["src/app.js", "TASK.md"], "scaffold_leaked": ["TASK.md"]},
          ["src/app.js", "TASK.md"])), True)
# --- (4) baseline ----------------------------------------------------------------------------
c = make_case()
(c["repo"] / "later.txt").write_text("x\n"); git("add", "-A", cwd=c["repo"])
git("commit", "-qm", "main advanced", cwd=c["repo"]); git("push", "-q", "origin", "main", cwd=c["repo"])
r = land(c)
check("main advanced past the baseline -> held, src not landed",
      (r.get("status"), "baseline" in " ".join(r.get("reasons") or []), (c["repo"] / "src").exists()),
      ("held", True, False))
# --- (5) validate ------------------------------------------------------------------------------
c = make_case(validate="false")
held("validate fails", c, "validate")
c = make_case(validate="")
(c["repo"] / ".dispatch-validate").unlink(); git("add", "-A", cwd=c["repo"]); git("commit", "-qm", "rm", cwd=c["repo"])
held("no validate command known -> fail closed", c, "no validate command")
# --- push failure rewinds ----------------------------------------------------------------------------
c = make_case()
hook = c["bare"] / "hooks" / "pre-receive"
hook.write_text("#!/bin/sh\nexit 1\n"); hook.chmod(0o755)
r = land(c)
check("push rejected -> default branch rewound, nothing landed",
      (r.get("status"), (c["repo"] / "src").exists(), git("rev-list", "--count", "main", cwd=c["repo"])),
      ("held", False, "1"))
# --- opt-out ----------------------------------------------------------------------------------------------
os.environ["AUTO_LAND"] = "0"
c = make_case()
r = land(c)
check("AUTO_LAND=0 -> disabled, nothing landed", (r.get("status"), (c["repo"] / "src").exists()),
      ("disabled", False))
os.environ.pop("AUTO_LAND")
# --- dirty target in main ------------------------------------------------------------------------------------
c = make_case()
(c["repo"] / "src").mkdir(); (c["repo"] / "src" / "app.js").write_text("human edit\n")
r = land(c)
check("human's uncommitted edit of the target -> held, edit preserved",
      ((c["repo"] / "src" / "app.js").read_text(), r.get("status")), ("human edit\n", "held"))

shutil.rmtree(td, ignore_errors=True)
print("AUTO_LAND_OK" if ok else "AUTO_LAND_FAILED")
sys.exit(0 if ok else 1)
