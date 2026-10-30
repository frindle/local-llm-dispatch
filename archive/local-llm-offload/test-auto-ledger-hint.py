#!/usr/bin/env python3
"""When the same failure signature appears twice in a row for a slice, the next author AND
continue prompt carry a one-line hint (failure ledger). Silent otherwise. Fail-open.
--revert-check: AUTO_SRC mutated copy."""
import importlib.util, json, os, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
HERE = Path(__file__).resolve().parent
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")
T = Path(tempfile.mkdtemp(prefix="alh-")); (T/"q").mkdir(); (T/"l").mkdir()
os.environ.update({"FAILURE_LEDGER_QUEUE_LOGS": str(T/"q"), "FAILURE_LEDGER_LIVELOGS": str(T/"l"),
                   "FAILURE_LEDGER_STATE": str(T/"s.json"), "FAILURE_LEDGER": str(T/"f.jsonl")})
rows = [{"job_id": f"j{i}", "label": lbl, "ts": f"2026-10-06T1{i}:00:00Z", "signature": sig}
        for i, (lbl, sig) in enumerate([("auto-author-runA-s4", "rewrite-loop:refimpl.py"),
                                         ("auto-author-runA-s4-c1", "read-loop:auto-harness-check.py"),
                                         ("auto-author-runA-s4-c2", "read-loop:auto-harness-check.py"),
                                         ("auto-author-runA-s5", "literal-cap")])]
(T/"f.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
ld = SourceFileLoader("auto_lh", str(AUTO)); m = importlib.util.module_from_spec(importlib.util.spec_from_loader("auto_lh", ld))
sys.argv = [str(AUTO)]; ld.exec_module(m)
FAILS = []
def chk(n, ok):
    print(("ok  " if ok else "FAIL") + " - " + n)
    if not ok: FAILS.append(n)
mk = lambda label: SimpleNamespace(label=label, intent="x", lang="ts", verify_shape="", edit_file=None, interface=None,
                                   slice_must=None, require=[], new_project=False, verify_cmd=None)
a4, a5 = mk("runA-s4"), mk("runA-s5")
p = m.author_prompt(a4, "lib/x.ts"); c = m.author_continue_prompt(a4, "lib/x.ts", "not ok 1")
chk("author prompt carries the repeat hint for the slice", "Repeat-failure hint" in p and "read-loop:auto-harness-check.py" in p)
chk("continue prompt carries it too", "Repeat-failure hint" in c)
chk("a slice whose last two failures differ gets no hint", "Repeat-failure hint" not in m.author_prompt(a5, "lib/x.ts"))
chk("no ledger rows -> no hint, no crash", "Repeat-failure hint" not in m.author_prompt(mk("runZ-s9"), "lib/x.ts"))
sys.exit(1 if FAILS else 0)
