#!/usr/bin/env python3
"""worker_levers.py -- three OPT-IN convergence levers for ollama-worker.py CODING runs (2026-10-10).

WHY. Two failed A/B-baseline jobs (absamp-10102153-aA-s1, qwen3.6-35b-a3b via Darkbloom):
  * 1f9ef6805037 (30 iters): 20 read_file calls on a 521-line file in 10-40 line windows, never ran the
    verify mid-run, ended 0/6 tests because the edits never carried the new counters.
  * 76d747232905 (20 iters): ran `python3 refimpl.py .` (the sealed REFERENCE SOLUTION), `bash verify.sh`
    passed at iteration 5, then `git checkout -- dashboard_chat.py` reverted its own passing state and spent
    the remaining iterations editing refimpl.py instead of the target.

THE SWITCHES (all default OFF; unset == byte-identical to the pre-lever worker). Each is an env var read AT
CALL TIME, and a per-job passthrough with no queue change exists: a `Worker-flags: K=1 K=1` line in the task
text (like `Sampling-profile:`) sets the env for that worker process (allow-listed names, "0"/"1" only,
never overrides an env var that is already set).
  VERIFY_ON_EDIT=1   after a successful edit_file/write_file to a non-harness file, run the task verify on every
                     2nd such edit (max 6 runs/run; skipped once any verify took > 120 s) and append a compact
                     failing-cases summary (<= 1500 chars) to that tool result. On PASS the result says so and
                     tells the model to call task_complete. Uses the worker's _quick_verify, so the existing
                     EDIT_CHECKPOINT snapshot hook fires on a pass exactly as before (not duplicated).
  PROTECT_HARNESS=1  ONLY for a coding job over a SEALED harness (HEAD subject "seal dispatch harness...", a
                     verify.sh plus refimpl.py / a fixture in the tree, not an authoring/refine task, not the
                     harness self-check verify): refimpl.py is invisible (list_files / read_file / edit / write /
                     run_bash discovery) and `python3 refimpl.py` is refused; edit_file / write_file / shell
                     writes / git checkout|restore|stash against the harness files (verify.sh, verify.test.*,
                     test_fixture.py, check_literals.py, TASK.md, refimpl.py) are refused with an instructive
                     message; whole-tree reverts (git stash / reset --hard / clean / checkout .) are refused, and
                     so is reverting any file while the last verify run PASSED (the 76d747232905 failure).
                     (refimpl.py is NOT moved out of the tree: a tracked deletion would show in every
                     worktree-diff guard; verify.sh never needs it -- checked on all 21 replay tasks.)
  READ_MIN_WINDOW=1  read_file with 0 < length < 120 is served >= 120 lines (or to EOF) and the result notes the
                     real range.

PURE apart from the three module-level state dicts (reset per run by arm_run) and the one `git log` in
sealed_coding_harness. No import of the worker; the worker owns the (tiny, gated) call sites."""
import fnmatch
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

FLAGS = ("VERIFY_ON_EDIT", "PROTECT_HARNESS", "READ_MIN_WINDOW")
MIN_WINDOW_LINES = 120
VOE_EVERY = 2
VOE_MAX_RUNS = 6
VOE_SKIP_IF_SLOWER_S = 120.0
VOE_MAX_CHARS = 1500

HARNESS_FILES = ("verify.sh", "test_fixture.py", "check_literals.py", "TASK.md", "refimpl.py",
                 ".dispatch-harness.json")
HARNESS_GLOBS = ("verify.test.*",)
HIDDEN_FILES = ("refimpl.py",)


def flag(name, environ=None) -> bool:
    return (os.environ if environ is None else environ).get(name) == "1"


# --------------------------------------------------------------------------- task-text passthrough
_MARKER_RE = re.compile(r"(?im)^[ \t]*Worker-flags:[ \t]*(.+?)[ \t]*$")


def apply_task_flags(task, environ=None, log=None):
    """`Worker-flags: VERIFY_ON_EDIT=1 PROTECT_HARNESS=1` line(s) in the task text -> env (allow-listed, 0/1).
    An env var that is already set wins. -> the list of KEY=VAL actually applied."""
    environ = os.environ if environ is None else environ
    applied = []
    for line in _MARKER_RE.findall(task or ""):
        for tok in re.split(r"[,\s]+", line.strip()):
            if "=" not in tok:
                continue
            k, v = tok.split("=", 1)
            k, v = k.strip().upper(), v.strip()
            if k not in FLAGS or v not in ("0", "1") or k in environ:
                continue
            environ[k] = v
            applied.append(f"{k}={v}")
    if applied and log:
        log(f"[worker] WORKER FLAGS {' '.join(applied)} (from the task's Worker-flags line).")
    return applied


# --------------------------------------------------------------------------- L3 READ_MIN_WINDOW
def min_window_length(ln):
    """The effective read_file length: raised to MIN_WINDOW_LINES when READ_MIN_WINDOW=1 and the call asked
    for 0 < length < MIN. None / 0 / negative (= 'default page') and every length >= MIN pass through."""
    if flag("READ_MIN_WINDOW") and ln is not None and 0 < ln < MIN_WINDOW_LINES:
        return MIN_WINDOW_LINES
    return ln


def min_window_note(asked, start, last, total) -> str:
    return (f"[READ_MIN_WINDOW: you asked for {asked} line(s); reads are served at least "
            f"{MIN_WINDOW_LINES}, so lines {start}-{last} of {total} are shown. Ask for >= "
            f"{MIN_WINDOW_LINES} lines per read, or use run_bash `grep -n` to locate a line first.]")


# --------------------------------------------------------------------------- shared run state
_VOE = {"edits": 0, "runs": 0, "last_secs": 0.0}
_PH = {"cwd": None, "verify": None, "passing": False}
_LAST_VERIFY_SECS = [0.0]


def note_verify_secs(secs):
    """Called by the worker's _quick_verify (always; pure bookkeeping) so L1 can skip a slow verify."""
    try:
        _LAST_VERIFY_SECS[0] = float(secs)
    except Exception:
        pass


def _norm(cwd):
    try:
        return str(Path(cwd).resolve())
    except Exception:
        return str(cwd)


# --------------------------------------------------------------------------- L2 PROTECT_HARNESS
def is_harness_rel(rel) -> bool:
    rel = str(rel).replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    if "/" in rel:
        return False                      # the sealed harness lives at the worktree root
    return rel in HARNESS_FILES or any(fnmatch.fnmatch(rel, g) for g in HARNESS_GLOBS)


def sealed_coding_harness(cwd, task, task_kind, verify, runner=None) -> bool:
    """A coding job over a SEALED harness (the replay/slicer coding path): not an authoring/refine/continue
    task, the verify is not the harness self-check, HEAD is the 'seal dispatch harness' commit, and the tree
    holds verify.sh plus the reference solution or a fixture. Anything unclear -> False (lever stays off)."""
    try:
        if task_kind != "coding" or not verify:
            return False
        head = (task or "").lstrip()[:80]
        if head.startswith(("# AUTHORING TASK", "# REFINE TASK", "# CONTINUE --")):
            return False
        if "auto-harness-check" in str(verify):
            return False
        c = Path(cwd)
        if not (c / "verify.sh").is_file():
            return False
        if not ((c / "refimpl.py").is_file() or (c / "test_fixture.py").is_file()
                or any(c.glob("verify.test.*"))):
            return False
        run = runner or subprocess.run
        r = run(["git", "log", "-1", "--format=%s"], cwd=str(c), capture_output=True, text=True, timeout=15)
        return r.returncode == 0 and r.stdout.strip().startswith("seal dispatch harness")
    except Exception:
        return False


def ph_active(cwd) -> bool:
    return bool(flag("PROTECT_HARNESS") and _PH["cwd"] and _PH["cwd"] == _norm(cwd))


def _rel(cwd, path_arg):
    try:
        p = (Path(cwd) / str(path_arg)).resolve() if not os.path.isabs(str(path_arg)) else Path(path_arg).resolve()
        return str(p.relative_to(Path(cwd).resolve())).replace("\\", "/")
    except Exception:
        return None


def ph_hidden(cwd, child) -> bool:
    """list_files filter: is this directory entry (a Path under cwd) invisible?"""
    try:
        return ph_active(cwd) and _rel(cwd, child) in HIDDEN_FILES
    except Exception:
        return False


def ph_file_verdict(cwd, path_arg, op):
    """read_file / edit_file / write_file gate. op is 'read' or 'write'. -> an ERROR/REFUSED string or None."""
    try:
        if not ph_active(cwd):
            return None
        rel = _rel(cwd, path_arg)
        if rel is None:
            return None
        if rel in HIDDEN_FILES:
            return f"ERROR: file not found: {path_arg} -- use list_files to see what exists."
        if op == "write" and is_harness_rel(rel):
            return (f"REFUSED: {rel} is part of the sealed verification harness (verify.sh, verify.test.*, "
                    f"test_fixture.py, check_literals.py, TASK.md). It is NOT what you are asked to change: "
                    f"editing it invalidates the check. Read it if you need to, then fix the TARGET source "
                    f"file named in the task so the unchanged verify passes.")
        return None
    except Exception:
        return None


_SEG_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")
_REDIR_HARNESS = re.compile(
    r"(?<![0-9&<])>>?\s*['\"]?(?:\./)?(?:verify\.sh|verify\.test\.[\w.]+|test_fixture\.py|check_literals\.py|TASK\.md|refimpl\.py)\b")
_MUTATORS = re.compile(r"\b(?:sed\s+(?:-\w*i|--in-place)|tee|mv|rm|cp|truncate|chmod|perl\s+-\w*i|ed|install)\b")
_HARNESS_WORD = re.compile(r"(?:\bverify\.sh|\bverify\.test\.[\w.]+|\btest_fixture\.py|\bcheck_literals\.py|\bTASK\.md|\brefimpl\.py)")
_GIT_REVERT_SUBS = ("checkout", "restore", "stash", "reset", "clean", "switch")


def _git_segments(command):
    for seg in _SEG_SPLIT.split(command or ""):
        try:
            toks = shlex.split(seg)
        except ValueError:
            toks = seg.split()
        if "git" not in toks:
            continue
        rest = toks[toks.index("git") + 1:]
        i = 0
        while i < len(rest) and rest[i].startswith("-"):      # git -C path / -c k=v / --no-pager
            i += 2 if rest[i] in ("-C", "-c") else 1
        if i < len(rest):
            yield rest[i], rest[i + 1:]


def _git_revert_verdict(sub, args, passing):
    """-> a REFUSED string or None for one `git <sub> <args>` that could discard working-tree state."""
    if sub not in _GIT_REVERT_SUBS:
        return None
    paths = [a for a in args if not a.startswith("-") and a != "--"]
    if sub == "stash":
        if paths and paths[0] in ("list", "show"):
            return None
        return ("REFUSED: `git stash` would set your own edits aside and is not available in this task. Use "
                "`git diff` to inspect your changes and edit_file to adjust them.")
    if sub == "clean":
        return "REFUSED: `git clean` deletes files and is not available in this task."
    if sub == "reset":
        if "--hard" in args or "--merge" in args:
            return ("REFUSED: `git reset --hard` would discard all of your edits and is not available in this "
                    "task. Use `git diff` to inspect them and edit_file to change them.")
        return None
    if sub == "switch" or (sub == "checkout" and any(a in ("-b", "-B", "--orphan") for a in args)):
        return None
    if sub == "checkout" and paths and "--" not in args and not any(
            p in ("." , ":/") or _HARNESS_WORD.search(p) or "/" in p or "." in p for p in paths):
        return None                                        # `git checkout somebranch`
    harness = [p for p in paths if _HARNESS_WORD.search(p)]
    if harness:
        return (f"REFUSED: `git {sub}` of {', '.join(harness)} is not allowed: the verification harness is "
                f"sealed and must stay exactly as committed. Change the TARGET source file instead.")
    if any(p in (".", ":/", "*") for p in paths) or not paths:
        return (f"REFUSED: `git {sub}` over the whole tree would discard ALL of your edits and is not "
                f"available in this task. Use `git diff` to inspect your changes and edit_file to adjust them.")
    if passing:
        return (f"REFUSED: `git {sub} {' '.join(paths)}` would revert a file while the last verify run PASSED. "
                f"Your tree currently satisfies the verify -- do NOT discard it. Call task_complete (or, if "
                f"you really must change something, make a small edit_file and re-run the verify).")
    return None


def ph_bash_verdict(cwd, command):
    """run_bash gate. -> a REFUSED string or None."""
    try:
        if not ph_active(cwd):
            return None
        cmd = str(command or "")
        if re.search(r"refimpl", cmd, re.I):
            return ("REFUSED: refimpl.py is not one of your inputs in this task (it is the reference "
                    "solution the gate uses and it is not available to you). Implement the change yourself "
                    "in the target source file named in the task, then check it with `bash verify.sh`.")
        for seg in _SEG_SPLIT.split(cmd):
            if _HARNESS_WORD.search(seg) and (_REDIR_HARNESS.search(seg) or _MUTATORS.search(seg)):
                return ("REFUSED: that command would modify the sealed verification harness (verify.sh, "
                        "verify.test.*, test_fixture.py, check_literals.py, TASK.md). Do not change it -- fix "
                        "the TARGET source file named in the task so the unchanged verify passes.")
        if _REDIR_HARNESS.search(cmd):
            return ("REFUSED: that command would overwrite a sealed harness file. Do not change it -- fix "
                    "the TARGET source file named in the task.")
        for sub, args in _git_segments(cmd):
            v = _git_revert_verdict(sub, args, _PH["passing"])
            if v:
                return v
        return None
    except Exception:
        return None


_SCRUB_TAIL = re.compile(r"(?:^|[\s/])refimpl\.py$")
_SCRUB_GREP = re.compile(r"^(?:\./)?refimpl\.py[:\-]\d+")


def ph_scrub(cwd, text):
    """Drop directory-listing / grep-hit lines that name refimpl.py from run_bash output (ls, find, git status,
    grep -r). Code lines that merely mention it in a comment are left alone."""
    try:
        if not text or not ph_active(cwd) or "refimpl" not in text:
            return text
        keep = []
        for ln in text.splitlines(keepends=True):
            s = ln.strip()
            if "refimpl" in s and (_SCRUB_GREP.match(s) or (len(s.split()) <= 10 and _SCRUB_TAIL.search(s))):
                continue
            keep.append(ln)
        return "".join(keep)
    except Exception:
        return text


def ph_note_bash(cwd, command, rc):
    """A run_bash that CONTAINS the task verify and exits 0 -> the tree currently passes."""
    try:
        if not ph_active(cwd) or rc != 0:
            return
        v = " ".join(str(_PH.get("verify") or "").split())
        c = " ".join(str(command or "").replace("2>&1", " ").split())
        if v and c == v:          # exactly the verify (a pipe / && chain would mask its exit status)
            _PH["passing"] = True
    except Exception:
        pass


def ph_diag_verdict(cwd, requests) -> str | None:
    """request_diagnostics could `file`/`grep` refimpl.py: refuse any request that names it."""
    try:
        if ph_active(cwd) and "refimpl" in repr(requests):
            return ("ERROR: refimpl.py is not available in this task. Implement the change in the target "
                    "source file and check it with `bash verify.sh`.")
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- L1 VERIFY_ON_EDIT
_FAIL_HEAD = re.compile(
    r"^\s*(?:not ok\b|[✖✕×]\s|FAIL(?:ED)?\b|--- FAIL\b|ERROR:|ERROR\b|AssertionError\b|Traceback\b|"
    r"\s*(?:\w+Error|\w+Exception):)", re.I)
_ASSERT_LINE = re.compile(r"(assert|expected|actual|!==|===|should|got\b|\bwanted\b|\w+Error)", re.I)


def summarize_failures(output, limit=VOE_MAX_CHARS) -> str:
    """PURE. Compact 'which cases failed + the first assertion line of each' from a verify's output."""
    lines = (output or "").splitlines()
    cases, seen = [], set()
    i = 0
    while i < len(lines) and len(cases) < 14:
        raw = lines[i]
        s = raw.strip()
        if s and _FAIL_HEAD.search(raw) and not s.lower().startswith(("fails=", "failed: 0")):
            head = s[:200]
            if head not in seen:
                seen.add(head)
                item = "  - " + head
                for nxt in lines[i + 1:i + 7]:
                    t = nxt.strip()
                    if t and t != s and _ASSERT_LINE.search(t) and not _FAIL_HEAD.search(nxt):
                        item += "\n      " + t[:200]
                        break
                cases.append(item)
        i += 1
    if not cases:
        tail = (output or "").strip()[-600:]
        return "(no recognizable failing-case lines; verify output tail)\n" + tail
    body = "\n".join(cases)
    if len(body) > limit:
        body = body[:limit - 20].rstrip() + "\n  ... (truncated)"
    return body


def arm_run(cwd, task, task_kind, verify, log=None):
    """Called once per run_task after the verify/task are known. Resets per-run state; arms L2 when the job is
    a sealed coding job. Never raises."""
    try:
        _VOE.update(edits=0, runs=0, last_secs=0.0)
        _PH.update(cwd=None, verify=verify, passing=False)
        if flag("PROTECT_HARNESS"):
            if sealed_coding_harness(cwd, task, task_kind, verify):
                _PH["cwd"] = _norm(cwd)
                if log:
                    log("[worker] PROTECT_HARNESS: sealed coding harness detected -- refimpl.py hidden, harness "
                        "files read-only, harness/whole-tree git reverts refused.")
            elif log:
                log("[worker] PROTECT_HARNESS requested but this is not a sealed coding job -- lever not armed.")
        if flag("VERIFY_ON_EDIT") and log:
            log(f"[worker] VERIFY_ON_EDIT armed: verify every {VOE_EVERY}nd successful non-harness edit "
                f"(max {VOE_MAX_RUNS} runs, skipped once a verify took > {int(VOE_SKIP_IF_SLOWER_S)}s).")
        if flag("READ_MIN_WINDOW") and log:
            log(f"[worker] READ_MIN_WINDOW armed: read_file windows < {MIN_WINDOW_LINES} lines are widened.")
    except Exception:
        pass


def after_edit(cwd, verify, path, result, runner, task_kind="coding", log=None, clock=time.monotonic) -> str:
    """Hook for a finished write_file/edit_file. -> text to append to the tool result ('' = nothing).
    Handles the L2 'edit invalidates the passing state' bookkeeping and the whole of L1."""
    try:
        if not isinstance(result, str) or not result.startswith("OK: "):
            return ""
        if ph_active(cwd):
            _PH["passing"] = False
        if not flag("VERIFY_ON_EDIT") or task_kind != "coding" or not verify:
            return ""
        rel = _rel(cwd, path) if path is not None else None
        if rel is None or is_harness_rel(rel):
            return ""
        _VOE["edits"] += 1
        if _VOE["edits"] % VOE_EVERY:
            return ""
        if _VOE["runs"] >= VOE_MAX_RUNS:
            return ""
        if max(_VOE["last_secs"], _LAST_VERIFY_SECS[0]) > VOE_SKIP_IF_SLOWER_S:
            return ""
        t0 = clock()
        ok, out = runner(verify, cwd)
        secs = clock() - t0
        _VOE["runs"] += 1
        _VOE["last_secs"] = secs
        if log:
            log(f"[worker] VERIFY_ON_EDIT run {_VOE['runs']}/{VOE_MAX_RUNS} after edit of {rel}: "
                f"{'PASS' if ok else 'FAIL'} in {secs:.0f}s")
        if ok:
            if ph_active(cwd):
                _PH["passing"] = True
            return ("\n\n[VERIFY ON EDIT: the task's verify command now PASSES (exit 0). The work is done -- do "
                    "NOT edit further and do NOT revert anything; call task_complete now.]")
        return (f"\n\n[VERIFY ON EDIT (run {_VOE['runs']}/{VOE_MAX_RUNS}): after this edit the task's verify "
                f"command still FAILS. Failing cases:\n{summarize_failures(out)}\n"
                f"Informational only: finish the edits you planned, then fix what is still listed.]")
    except Exception:
        return ""
