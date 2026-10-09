#!/usr/bin/env python3
"""Guards the bug fixed 2026-09-19 (SECOND occurrence of the unwinnable-verify class).

Live evidence: job 0f3f0e7af69a (auto-refine-esim-global-s1-parse-global-r1) and job
acf67b79baf1 (auto-refine-bg-captcha-s3-vision-r1). Both were enqueued with
`--verify python3 auto-harness-check.py` and cwd = their slice worktree; both ended
DID-NOT-CONVERGE because that script was GONE from the worktree by the time the job
actually ran. Both worktrees were left with neither auto-harness-check.py nor
AUTO-TASK.md -- the fingerprint of ollama-dispatch-slice.clean_and_seal().

Root cause, two parts:

  A. clean_and_seal() unlinked AUTO-TASK.md + auto-harness-check.py unconditionally.
     A queue job runs LONG after its enqueue (depth-first queue, waiting on the GPU),
     so the enqueue-side repair in ollama-dispatch-auto.dispatch_model() -- which
     regenerates a missing script -- cannot help: the deletion happens afterwards.
     The verify can then only ENOENT, the worker rejects every task_complete, the
     model burns its whole budget, and the round is misread as model incapacity.

  B. slice_job_inflight() exact-matched only `auto-author-<label>-<sid>` and
     `<label>-<sid>`. It was blind to the third label shape the auto flow enqueues,
     `auto-refine-<label>-<sid>-r<N>` -- which is how execute() reached its
     PENDING/resume branch, and clean_and_seal(), with a refine job LIVE in the queue.

The fix keeps the clean launch baseline the deletion existed to provide, but gets it
the non-destructive way seal_baseline() already uses for .preflight-state.json: a
local `.git/info/exclude` entry. git then ignores the files entirely -- not tracked,
not untracked, absent from `git status --porcelain` -- so they are also immune to the
untracked-file revert()s in the self-check, the preflight and the gate.

Run: python3 test-slice-harness-check-survives-seal.py
"""
import importlib.machinery
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

BIN = Path(__file__).resolve().parent


def _load(name, fname):
    p = BIN / fname
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, str(p)))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ods = _load("ods", "ollama-dispatch-slice")
oda = _load("oda", "ollama-dispatch-auto")

fails = []


def check(name, cond, detail=""):
    print(("  ok: " if cond else "  FAIL: ") + name + ("" if cond else " -- " + detail))
    if not cond:
        fails.append(name)


def build_worktree(wt: Path):
    """A minimal SEALED slice worktree: a real harness the self-check can run."""
    (wt / "pkg").mkdir()
    (wt / "pkg" / "mod.py").write_text("def f(x):\n    return None\n")
    (wt / "TASK.md").write_text(
        "# TASK\nOnly edit `pkg/mod.py`\n## Must contain\n- in pkg/mod.py: `return x * 2`\n")
    (wt / "refimpl.py").write_text(
        'import pathlib, os\nos.chdir(os.path.dirname(os.path.abspath(__file__)))\n'
        'pathlib.Path("pkg/mod.py").write_text("def f(x):\\n    return x * 2\\n")\n')
    (wt / "test_fixture.py").write_text(
        "import pkg.mod as m\nassert m.f(2) == 4\nprint('cases ok')\n")
    (wt / "verify.sh").write_text(
        'cd "$(dirname "$0")" || exit 1\npython3 test_fixture.py || exit 1\necho VERIFY_OK\n')
    (wt / "check_literals.py").write_text("LITERALS = []\n")
    (wt / ".dispatch-harness.json").write_text('{"target": "pkg/mod.py"}')
    for c in (["git", "init", "-q", "."], ["git", "config", "user.email", "t@t"],
              ["git", "config", "user.name", "t"], ["git", "add", "-A"],
              ["git", "commit", "-qm", "baseline"]):
        subprocess.run(c, cwd=wt, check=True, capture_output=True)


def porcelain(wt):
    return subprocess.run(["git", "status", "--porcelain"], cwd=wt,
                          capture_output=True, text=True).stdout.strip()


# ------------------------------------------------- defect A: the deletion
print("=== defect A: clean_and_seal must not destroy a live job's verify script ===")
with tempfile.TemporaryDirectory() as td:
    wt = Path(td) / "wt"
    wt.mkdir()
    build_worktree(wt)

    # The state ollama-dispatch-auto.dispatch_model() leaves behind at enqueue.
    oda.write_harness_check(wt, "python")
    (wt / "AUTO-TASK.md").write_text("PROMPT")
    check("precondition: the auto scratch is present after enqueue",
          (wt / "auto-harness-check.py").is_file() and (wt / "AUTO-TASK.md").is_file())

    ods.clean_and_seal(str(wt))

    # THE REGRESSION. Pre-fix both of these are gone and the job is unwinnable.
    check("auto-harness-check.py SURVIVES clean_and_seal",
          (wt / "auto-harness-check.py").is_file(),
          "the acceptance test of any queued author/refine job in this worktree "
          "was just deleted -- its verify can now only ENOENT")
    check("AUTO-TASK.md SURVIVES clean_and_seal",
          (wt / "AUTO-TASK.md").is_file(),
          "the queued job's --task-file was just deleted")

    # ...and the property the deletion existed to provide is still delivered.
    st = porcelain(wt)
    check("the launch tree is still CLEAN (the point of the old deletion)",
          st == "",
          "git status shows: " + repr(st) + " -- the queue would record a DIRTY "
          "launch baseline and the gate would flag the diff as unattributable")

    # Excluded, not tracked: the harness scratch must not ride into the baseline
    # commit either (that is the scaffold-leak false-fail the gate raises).
    tracked = subprocess.run(["git", "ls-files"], cwd=wt,
                             capture_output=True, text=True).stdout.split()
    check("the scratch is EXCLUDED, not committed into the baseline",
          "auto-harness-check.py" not in tracked and "AUTO-TASK.md" not in tracked,
          "tracked: " + repr(tracked))

    # Being git-ignored is what makes it immune to every untracked-file revert()
    # in the self-check / preflight / gate.
    check("the scratch is invisible to the untracked scan the revert()s use",
          not any(l[:2] == "??" and "auto-harness-check.py" in l
                  for l in porcelain(wt).splitlines()))

    # clean_and_seal is re-entrant (execute() calls it on every resume).
    ods.clean_and_seal(str(wt))
    check("still present after a SECOND clean_and_seal (resume is re-entrant)",
          (wt / "auto-harness-check.py").is_file() and (wt / "AUTO-TASK.md").is_file())

    # ---- the verify command the job was enqueued with actually RUNS now -------
    r = subprocess.run("python3 auto-harness-check.py", shell=True, cwd=str(wt),
                       capture_output=True, text=True)
    check("the enqueued verify command runs in the job's cwd",
          "No such file or directory" not in (r.stdout + r.stderr),
          (r.stdout + r.stderr).strip()[-300:])
    check("the self-check reaches a real verdict (not ENOENT)",
          r.returncode == 0 and "VERIFY_OK" in r.stdout,
          (r.stdout + r.stderr).strip()[-300:])

    # ---- SELF-LOCATION: it must measure ITS OWN tree, from any cwd ------------
    # Case 2 (bg-captcha-s3-vision) was verification re-run from a different path.
    other = Path(td) / "elsewhere"
    other.mkdir()
    r2 = subprocess.run([sys.executable, str(wt / "auto-harness-check.py")],
                        cwd=str(other), capture_output=True, text=True)
    check("the self-check self-locates when invoked from a FOREIGN cwd",
          r2.returncode == 0 and "VERIFY_OK" in r2.stdout,
          "cwd=" + str(other) + " -> " + (r2.stdout + r2.stderr).strip()[-300:])
    check("running it from a foreign cwd does not litter that cwd",
          not any(other.iterdir()), repr(list(other.iterdir())))

# ------------------------------------------------- defect B: the blind guard
print("=== defect B: slice_job_inflight sees a live auto-refine round ===")
st = {"label": "esim-global"}
sid = "s1-parse-global"
REFINE = f"auto-refine-{st['label']}-{sid}-r1"

real = ods.all_job_statuses


def fake(labels):
    return lambda: ({}, labels)


try:
    # The exact production shape: only a refine round is live.
    ods.all_job_statuses = fake({REFINE: ("0f3f0e7af69a", "running")})
    got = ods.slice_job_inflight(st, sid)
    check("a live auto-refine round IS reported in flight", got is not None,
          "execute() would fall through to its resume branch and clean_and_seal() "
          "the worktree of a running job -- this is the esim-global failure")
    check("it reports the refine job id/label",
          got is not None and got[0] == "0f3f0e7af69a" and got[2] == REFINE, repr(got))

    # A terminal refine round is NOT in flight.
    ods.all_job_statuses = fake({REFINE: ("0f3f0e7af69a", "failed")})
    check("a FAILED refine round is not reported in flight",
          ods.slice_job_inflight(st, sid) is None)

    # Highest round wins when several exist.
    ods.all_job_statuses = fake({
        f"auto-refine-{st['label']}-{sid}-r1": ("aaaaaaaaaaaa", "done"),
        f"auto-refine-{st['label']}-{sid}-r2": ("bbbbbbbbbbbb", "running")})
    got = ods.slice_job_inflight(st, sid)
    check("the live round is picked out of several rounds",
          got is not None and got[0] == "bbbbbbbbbbbb", repr(got))

    # It must not match a DIFFERENT slice whose sid shares a prefix.
    ods.all_job_statuses = fake({
        f"auto-refine-{st['label']}-{sid}-extra-r1": ("cccccccccccc", "running")})
    check("a different slice's refine round does not match this slice",
          ods.slice_job_inflight(st, sid) is None,
          repr(ods.slice_job_inflight(st, sid)))

    # The pre-existing label shapes still work.
    ods.all_job_statuses = fake({f"{st['label']}-{sid}": ("dddddddddddd", "running")})
    got = ods.slice_job_inflight(st, sid)
    check("the coding label still matches", got is not None and got[0] == "dddddddddddd")
    ods.all_job_statuses = fake({
        f"auto-author-{st['label']}-{sid}": ("eeeeeeeeeeee", "queued")})
    got = ods.slice_job_inflight(st, sid)
    check("the auto-author label still matches", got is not None and got[0] == "eeeeeeeeeeee")
finally:
    ods.all_job_statuses = real

check("a live refine round heals the slice to PENDING, not ENQUEUED",
      ods.heal_status_for_live_job(REFINE) == ods.PENDING,
      "ENQUEUED would claim a coding dispatch that does not exist yet")
check("a live coding job still heals to ENQUEUED",
      ods.heal_status_for_live_job(f"{st['label']}-{sid}") == ods.ENQUEUED)

# REVERT-TEST (run by hand, recorded here). Both directions were proven:
#   * restore the `for scratch in ("AUTO-TASK.md", "auto-harness-check.py"): unlink()`
#     loop in clean_and_seal() -> the two "SURVIVES clean_and_seal" checks FAIL and
#     "the enqueued verify command runs in the job's cwd" FAILS with exactly the
#     production error, "can't open file ... auto-harness-check.py: [Errno 2]".
#   * drop the AUTO-TASK.md / auto-harness-check.py names from seal_baseline()'s
#     info/exclude list -> "the launch tree is still CLEAN" FAILS (the scratch shows
#     up as `??`), i.e. the test also pins the reason the deletion existed.
#   * drop the refine-prefix branch from slice_job_inflight() -> all four defect-B
#     "refine" checks FAIL while the two pre-existing label checks still pass.

print()
if fails:
    print("SLICE_HARNESS_SEAL_FAIL: " + ", ".join(fails))
    sys.exit(1)
print("SLICE_HARNESS_SEAL_OK: the auto self-check survives clean_and_seal, the launch "
      "tree stays clean, the script self-locates from any cwd, and a live refine "
      "round is visible to the in-flight guard")
