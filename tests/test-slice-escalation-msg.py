#!/usr/bin/env python3
"""The authoring-escalation line must print the reason that actually tripped
(2026-10-02: Rivian s5 hit MAX_AUTHOR_ATTEMPTS with streak=1 and the log said
"failed 1 times in a row for the IDENTICAL reason"). Runs the real retry path
with a stubbed state and checks stdout. --revert-check restores the old print."""
import contextlib, importlib.util, io, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("SLICE_SRC") or HERE / "ollama-dispatch-slice")


def main():
    ld = SourceFileLoader("sl_em", str(SRC))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("sl_em", ld))
    sys.argv = [str(SRC)]
    ld.exec_module(m)
    m.QUEUE = os.devnull   # HERMETIC (2026-10-06): never the live `ollama-queue.py status`
    fn = m.bound_stale_worktree_retry
    m.slice_job_awaiting_gate = lambda st, sid: None
    m.note_author_failure = lambda s, wt: (1, "ERROR: refine round 2 did not converge")
    m.save_state = lambda st: None
    s = {"author_attempts": m.MAX_AUTHOR_ATTEMPTS, "respec_count": m.RESPEC_CAP}
    st = {"slices": [s]}
    import inspect
    params = list(inspect.signature(fn).parameters)
    args = {"st": st, "sid": "sX", "s": s, "wt": Path(tempfile.mkdtemp()), "cwt": Path(tempfile.mkdtemp()), "origin": "test"}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn(*[args[p] for p in params])
    txt = out.getvalue()
    ok = ("IDENTICAL reason" not in txt) and ("launched" in txt) and s.get("status") == m.ESCALATED
    print(("ok  " if ok else "FAIL") + ": attempt-cap escalation prints the attempt-cap reason")
    if not ok:
        print(txt)
    return 0 if ok else 1


def revert_check():
    src = SRC.read_text()
    old = '''        print(f"# {sid}: ESCALATED -- {s['escalation_reason']}\\n"'''
    new = '''        print(f"# {sid}: ESCALATED -- authoring has now failed "
              f"{streak} times in a row for the IDENTICAL reason:\\n"
              f"#     {sig}\\n"'''
    assert src.count(old) == 1
    with tempfile.NamedTemporaryFile("w", suffix="-slice", delete=False, dir=str(HERE)) as f:
        f.write(src.replace(old, new))
    r = subprocess.run([sys.executable, __file__], env={**os.environ, "SLICE_SRC": f.name},
                       capture_output=True, text=True, timeout=300)
    os.unlink(f.name)
    red = r.returncode != 0
    print(("bites" if red else "INERT") + ": revert -> suite " + ("RED" if red else "green"))
    print("REVERT-CHECK OK" if red else "REVERT-CHECK FAILED")
    return 0 if red else 1


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
