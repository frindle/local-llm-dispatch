#!/usr/bin/env python3
"""Canary seam: qagent land_message() builds the PUBLIC commit message ONLY from verified facts (job/task label, changed
file basenames, diff stat, Verified-by/Job trailers) -- never from the model's free-text final answer.  Before 2026-10-13
subjects like "arr-webhook: The problem is DEFAULTSESSION is a frozen copy of the or..." (an unverified, wrong root cause
stated as fact) and "Slice 4: ... accept: The verify command exited w..." (cut mid-sentence) landed on public repos.
usage: test-qagent-landmsg.py [--bin DIR]   prints QAGENT_LANDMSG_OK on success."""
import argparse, importlib.machinery, importlib.util, re, sys, tempfile, os
from pathlib import Path

ap = argparse.ArgumentParser(); ap.add_argument("--bin", default=str(Path.home() / "bin")); a = ap.parse_args()
os.environ.setdefault("QAGENT_LOGDIR", tempfile.mkdtemp())
ld = importlib.machinery.SourceFileLoader("qagent_mod", str(Path(a.bin) / "qagent"))
q = importlib.util.module_from_spec(importlib.util.spec_from_loader("qagent_mod", ld)); ld.exec_module(q)

PROSE = "The problem is DEFAULTSESSION is a frozen copy of the original session. The verify command exited with 1."
bad = []
def check(name, ok, detail=""):
    if not ok:
        bad.append(f"{name}: {detail}")

CASES = [
    ("Slice 4: Amazon's confirmLoggedIn accept the login banner when the verify command exits", ["M\tsidecar/amazon.js"]),
    ("arr-webhook", ["M\tsrc/session.py", "M\ttests/test_session.py"]),
    ("Add durable in-repo regression tests for: Two test files (sidecar/tests/a.test.js and b.test.js) must exist", ["A\tsidecar/tests/a.test.js", "A\tsidecar/tests/b.test.js"]),
    ("Symptom: after a container restart, side effects are lost and the daemon hangs forever", ["M\tdocker/start.sh"]),
    ("Tighten the validation of subscription renewal reminders in the notifier", ["M\tnotify/remind.js"]),
    ("# TASK: rt-add-fix\n\nlong spec text here", ["M\tcalc.py"]),
]
for i, (problem, files) in enumerate(CASES):
    jid = f"abc123def45{i}"
    # a prose-bearing final answer sits where the old code read it (<LOGDIR>/<job>.answer.md): it must never surface
    (Path(os.environ["QAGENT_LOGDIR"]) / f"{jid}.answer.md").write_text("## Root Cause\n" + PROSE + "\n")
    m = dict(job=jid, problem=problem, verify="python3 test_calc.py", baseline={"exit": 1},
             report={"after": 0, "files": files, "stat": f" {len(files)} file(s) changed, 3 insertions(+), 1 deletion(-)\n"})
    msg = q.land_message(m)
    subj, _, rest = msg.partition("\n\n")
    check(f"{i} subject one line", "\n" not in subj, repr(subj))
    check(f"{i} subject <=72", len(subj) <= 72, f"{len(subj)} {subj!r}")
    check(f"{i} no ellipsis/fragment", "..." not in subj and "…" not in subj and not subj.endswith((".", ",", ":")), subj)
    for w in ("DEFAULTSESSION", "frozen copy", "The problem is", "verify command", "exited"):
        check(f"{i} no model prose {w!r}", w not in msg, msg)
    check(f"{i} no '# TASK'", "TASK:" not in msg, msg)
    check(f"{i} lists a changed basename", Path(files[0].split("\t")[1]).name in msg, msg)
    check(f"{i} trailers", "Verified by: python3 test_calc.py (before exit 1, after exit 0)" in rest and f"Job: {jid}" in rest, rest)
    check(f"{i} no attribution", not re.search(r"Co-Authored|Claude|claude\.ai|Generated with|anthropic", msg, re.I), msg)
    check(f"{i} no mid-word cut", set(re.findall(r"[A-Za-z'0-9]+", subj.split(": dispatched fix")[0])) <= set(re.findall(r"[A-Za-z'0-9]+", problem)), subj)
if bad:
    print("FAIL:\n  " + "\n  ".join(bad)); sys.exit(1)
print("QAGENT_LANDMSG_OK")
