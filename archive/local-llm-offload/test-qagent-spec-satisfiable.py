#!/usr/bin/env python3
"""Canary seam: a fix spec of the form "Only edit A and create the new file B" must be SATISFIABLE by qagent's scope
guard.  Before 2026-10-12 parse_scope() dropped "create ..." as prose and scope_violations() flagged every new file, so
the numlock spec (rt-sidecar-nl-numlock*, 5 attempts) had no landable patch: every correct solution was UNVERIFIED.
usage: test-qagent-spec-satisfiable.py [--bin DIR]   prints QAGENT_SPEC_SATISFIABLE_OK on success."""
import argparse, importlib.machinery, importlib.util, sys
from pathlib import Path

ap = argparse.ArgumentParser(); ap.add_argument("--bin", default=str(Path.home() / "bin")); a = ap.parse_args()
ld = importlib.machinery.SourceFileLoader("qagent_mod", str(Path(a.bin) / "qagent"))
q = importlib.util.module_from_spec(importlib.util.spec_from_loader("qagent_mod", ld)); ld.exec_module(q)

SPEC = ("Make NumLock reliably ON.\n\nOnly edit docker/start-x11vnc.sh and create the new file docker/numlock-watch.sh. "
        "Do not edit docker/verify-numlock.sh (protected check).\n\n1. New docker/numlock-watch.sh (bash)...\n")
GOOD = [("A", "docker/numlock-watch.sh"), ("M", "docker/start-x11vnc.sh")]
bad = []
def check(name, got, want):
    if got != want:
        bad.append(f"{name}: got {got!r}, want {want!r}")

scope = q.parse_scope(SPEC)
creates = q.parse_creates(SPEC)
check("scope", scope, ["docker/start-x11vnc.sh"])
check("creates", creates, ["docker/numlock-watch.sh"])
check("reference solution has no scope violation", q.scope_violations(scope, GOOD, creates), [])
v = q.scope_violations(scope, GOOD + [("A", "docker/extra.sh")], creates)
check("undeclared new file still refused", len(v) == 1 and "docker/extra.sh" in v[0], True)
v = q.scope_violations(scope, GOOD + [("M", "docker/verify-numlock.sh")], creates)
check("protected/out-of-scope edit still refused", len(v) == 1 and "verify-numlock.sh" in v[0], True)
check("no create -> no authorised files", q.parse_creates("Only edit a.py"), [])
# arr-webhook s3 (2026-10-12): the files after "; do not edit" were parsed as part of the SCOPE, so an edit of the
# protected fixture/verify/TASK.md counted as in scope.
AW = "## Scope\n\nOnly edit `arr-webhook.py`; do not edit `verify.sh`, `test_fixture.py` or `TASK.md`.\ntest_fixture.py is the fixture.\n"
check("negated files are not scope", q.parse_scope(AW), ["arr-webhook.py"])
check("edit of a negated file is a violation", len(q.scope_violations(q.parse_scope(AW), [("M", "arr-webhook.py"), ("M", "test_fixture.py")], [])), 1)
check("plain list scope unchanged", q.parse_scope("Only edit a.py, b.py and c/d.py."), ["a.py", "b.py", "c/d.py"])
# a job the queue parked as needs_opus is finished for qagent: wait returns (1) instead of polling forever
import io, contextlib
q.job_or_sidecar = lambda i: {"id": i, "status": "needs_opus", "exit_code": 2}
q.enforce_wall = lambda j: None
w = type("W", (), {})(); w.id = "x"; w.timeout = 1; w.poll = 0.01
with contextlib.redirect_stdout(io.StringIO()):
    check("needs_opus job: wait returns 1 (terminal), not 2 (timeout)", q.cmd_wait(w), 1)
if bad:
    print("\n".join("FAIL " + b for b in bad)); sys.exit(1)
print("QAGENT_SPEC_SATISFIABLE_OK")
