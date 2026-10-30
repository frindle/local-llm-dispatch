#!/usr/bin/env python3
"""End-to-end regression test: a preflight SIGKILLed with the refimpl applied must
not strand it in the target (Rivian s3, 2026-10-02: a verify-relevance mutant was
left in lib/rivian.ts; every later round NO-GO'd on `baseline-clean` with a
byte-identical target -> 3 rounds -> escalated).

  1. preflight runs on a real git worktree whose verify HANGS once the refimpl is
     applied; it is SIGKILLed mid-verify (what auto's group timeout does);
  2. the target is now dirty (the fixture reproduces the strand);
  3. the next preflight RECOVERS (reverts the target to HEAD, says so) before
     `baseline-clean`, which therefore passes; the journal is gone afterwards;
  4. a journal whose owner is ALIVE is never acted on.
Run: python3 test-preflight-refimpl-journal.py [--revert-check]
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
GATE = Path(os.environ.get("PREF_SRC") or HERE / "ollama-dispatch-preflight")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def g(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)


def fixture(root):
    wt = root / "wt"
    wt.mkdir()
    g(wt, "init", "-q")
    (wt / "t.py").write_text("def f():\n    return 0\n")
    g(wt, "add", "t.py")
    g(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    (wt / "TASK.md").write_text("# Task\nMake f() return 1 in `t.py`.\n\n## Must contain\n\n- `return 1`\n\nOnly edit `t.py`.\n"
                                "Run `bash verify.sh` after every edit until it prints VERIFY_OK.\n")
    # hangs (sleeps) once the refimpl is in, so the kill lands mid-verify
    (wt / "verify.sh").write_text(
        'fails=0\nif grep -q "return 1" t.py; then [ -f HANG ] && sleep 300; '
        'else echo "  FAIL: f"; fails=$((fails+1)); fi\n'
        'echo "--- $fails failed ---"\n[ "$fails" -eq 0 ] && echo VERIFY_OK || exit 1\n')
    (wt / "refimpl.py").write_text(
        "from pathlib import Path\nPath('t.py').write_text('def f():\\n    return 1\\n')\n")
    return wt


def run_gate(wt, jroot, extra_env=None):
    return subprocess.Popen(
        [sys.executable, str(GATE), str(wt), "--refimpl-cmd", "python3 refimpl.py",
         "--no-relevance", "--target", "t.py"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "HOME": str(jroot), **(extra_env or {})},
        start_new_session=True)


def main():
    root = Path(tempfile.mkdtemp(prefix="pfj-")).resolve()
    home = root / "home"
    (home / ".ollama-dispatch").mkdir(parents=True)
    wt = fixture(root)
    (wt / "HANG").write_text("1")
    p = run_gate(wt, home)
    jdir = home / ".ollama-dispatch" / "preflight-journal"
    for _ in range(300):                      # wait until the refimpl is IN the target
        if "return 1" in (wt / "t.py").read_text() and jdir.exists() and any(jdir.iterdir()):
            break
        time.sleep(0.1)
    time.sleep(0.5)
    os.killpg(p.pid, signal.SIGKILL)          # what auto's group timeout does
    p.wait()
    check("fixture: the SIGKILL strands the refimpl in the target",
          "return 1" in (wt / "t.py").read_text(), True)
    check("fixture: a journal names the dead run", len(list(jdir.iterdir())), 1)
    (wt / "HANG").unlink()
    q = run_gate(wt, home)
    out, err = q.communicate(timeout=300)
    check("the next preflight says it RECOVERED the interrupted run",
          "RECOVERED an interrupted run" in err, True)
    check("...baseline-clean is not failed by the stranded refimpl",
          "[FAIL ] baseline-clean" in out or "FAIL  baseline-clean" in out, False)
    check("...the target is back to HEAD afterwards", (wt / "t.py").read_text(),
          "def f():\n    return 0\n")
    check("...and the journal is gone", list(jdir.iterdir()), [])

    # a journal whose owner is ALIVE is never touched
    (wt / "t.py").write_text("def f():\n    return 1\n")
    jp = next(iter([]), None)
    sleeper = subprocess.Popen(["sleep", "60"])
    try:
        import hashlib
        jp = jdir / (hashlib.sha1(str(wt.resolve()).encode()).hexdigest()[:16] + ".json")
        jp.write_text(json.dumps({"wt": str(wt), "pid": sleeper.pid}))
        r = run_gate(wt, home)
        out, err = r.communicate(timeout=300)
        check("a live owner's journal is never acted on",
              ("RECOVERED" in err, jp.exists()), (False, True))
    finally:
        sleeper.kill()
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("no recovery before baseline-clean",
     "        self.recover_interrupted_refimpl()\n        base_ok", "        base_ok"),
    ("no journal written", "        self._journal_open()\n        ok, why = self.apply_refimpl()",
     "        ok, why = self.apply_refimpl()"),
    ("recover even with a live owner",
     '        if _pid_alive(j.get("pid")) and j.get("pid") != os.getpid():\n            return []',
     '        if False:\n            return []'),
]


def revert_check():
    bad = 0
    src = GATE.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        d = Path(tempfile.mkdtemp(prefix="pfjmut-"))
        dst = d / "ollama-dispatch-preflight"
        dst.write_text(src.replace(old, new))
        for sib in ("pgrun.py", "verify-relevance.py", "verify-quality.py", "dispatch_progress.py"):
            if (HERE / sib).exists():
                os.symlink(HERE / sib, d / sib)
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "PREF_SRC": str(dst)},
                           capture_output=True, text=True, timeout=600)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
