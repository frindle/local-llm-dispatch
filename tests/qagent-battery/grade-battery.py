#!/usr/bin/env python3
"""grade-battery.py <job-id ...> | <task=job-id ...>  [--file last-run.txt]

Grades finished qagent battery jobs and prints a pass/fail table plus wall times.
Positional ids map to tasks in tasks.json order; or use g3=abcdef123456 pairs; or --file with
'<task> <job>' lines (what run-battery.sh writes). Answers come from `qagent result <id>`.
Env: QAGENT (default ~/bin/qagent), QAGENT_STATE / QAGENT_LOGDIR (as qagent). Exit 0 iff all pass."""
import json, os, re, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
QAGENT = os.environ.get("QAGENT", str(Path.home() / "bin" / "qagent"))
STATE = Path(os.environ.get("QAGENT_STATE", str(Path.home() / "bin" / "ollama-queue-state.json")))
LOGDIR = Path(os.environ.get("QAGENT_LOGDIR", str(Path.home() / "bin" / "ollama-queue-logs")))


def extract_answer(out):
    m = re.search(r"--- final answer ---\n(.*?)\n(?:--- (?:WARNING|diff)|worker log:|served model:|={20,})", out, re.S)
    return (m.group(1) if m else out).strip()


def wall(jid):
    for src in (lambda: next((j for j in json.loads(STATE.read_text()).get("jobs", []) if j.get("id", "").startswith(jid)), None),
                lambda: json.loads((LOGDIR / f"{jid}.done.json").read_text())):
        try:
            j = src()
            if j and j.get("active_s") is not None:
                return f"{int(j['active_s'])}s"
        except Exception:
            pass
    return "?"


def main(argv):
    tasks = json.loads((HERE / "tasks.json").read_text())["tasks"]
    pairs, pos = {}, []
    it = iter(argv)
    for a in it:
        if a == "--file":
            for ln in Path(next(it)).read_text().split("\n"):
                if ln.strip():
                    t, j = ln.split()[:2]; pairs[t] = j
        elif "=" in a:
            t, j = a.split("=", 1); pairs[t] = j
        else:
            pos.append(a)
    for t, j in zip(tasks, pos):
        pairs.setdefault(t["id"], j)
    rows, ok_all = [], True
    for t in tasks:
        jid = pairs.get(t["id"])
        if not jid:
            rows.append((t["id"], t["name"], "-", "-", "SKIP", "no job id")); continue
        r = subprocess.run([QAGENT, "result", jid], capture_output=True, text=True)
        out = r.stdout
        if "(job not finished yet" in out:
            rows.append((t["id"], t["name"], jid, wall(jid), "PENDING", "not finished")); ok_all = False; continue
        g = subprocess.run([sys.executable, str(HERE / t["grader"])], input=extract_answer(out), capture_output=True, text=True)
        verdict = (g.stdout.strip() or g.stderr.strip().splitlines()[-1:] and g.stderr.strip().splitlines()[-1] or "grader error")
        passed = g.returncode == 0
        ok_all &= passed
        warn = " [worker truncated tool output]" if "TRUNCATED by the worker" in out else ""
        rows.append((t["id"], t["name"], jid, wall(jid), "PASS" if passed else "FAIL", verdict.split(": ", 1)[-1] + warn))
    print(f"{'task':<4} {'name':<16} {'job':<13} {'wall':>6}  {'result':<7} detail")
    for r in rows:
        print(f"{r[0]:<4} {r[1]:<16} {r[2]:<13} {r[3]:>6}  {r[4]:<7} {r[5][:160]}")
    n = sum(1 for r in rows if r[4] == "PASS")
    print(f"\n{n}/{len(rows)} passed")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
