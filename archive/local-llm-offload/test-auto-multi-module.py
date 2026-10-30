#!/usr/bin/env python3
"""AUTO refuses a multi-module intent aimed at ONE --target (2026-10-04).

Pinned on the REAL chat-frontend authoring job f089442e2e8b: --new-project with an
--interface listing 7 package files and --target dashboard_chat/routes.py. The
authored TASK.md said "Only edit `dashboard_chat/routes.py`" while the intent needed
projects.py/sessions.py/images.py/messages.py/job.py/static/chat.html -- a
self-contradictory harness. Across 316 historical auto intents only this one trips
the refusal (measured 2026-10-04).

Usage:  python3 ~/bin/test-auto-multi-module.py
Revert: AUTO=~/bin/ollama-dispatch-auto.bak-multimodule python3 this.py  -> FAIL
"""
import os
import re
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUTO = Path(os.environ.get("AUTO", HERE / "ollama-dispatch-auto"))
# frozen copy of the real f089442e2e8b authoring log (hermetic: no live ~/bin/ollama-queue-logs read)
LOG = HERE / "test-fixtures-live-artifacts" / "chat-frontend-auto-author.txt"
fails = 0


def chk(name, actual, expected):
    global fails
    ok = actual == expected
    print(("ok   - " if ok else "FAIL - ") + name
          + ("" if ok else f": expected {expected!r} got {actual!r}"))
    fails += 0 if ok else 1


m = SourceFileLoader("auto_under_test", str(AUTO)).load_module()
fmf = getattr(m, "foreign_module_files", None)
mmc = getattr(m, "multi_module_check", None)
chk("foreign_module_files exists", fmf is not None, True)
chk("multi_module_check exists", mmc is not None, True)
if fmf is None or mmc is None:
    print(f"\n{fails} FAIL")
    sys.exit(1)

log = LOG.read_text(errors="replace")
intent = re.search(r"^INTENT: (.*)$", log, re.M).group(1)
iface = re.search(r"^INTERFACE / CONTRACT NOTES: (.*?)(?=\n## |\Z)", log, re.M | re.S).group(1)
TGT = "dashboard_chat/routes.py"

with tempfile.TemporaryDirectory() as empty:
    got = fmf(intent + "\n" + iface, TGT, empty)
    for f in ("dashboard_chat/projects.py", "dashboard_chat/sessions.py",
              "dashboard_chat/images.py", "dashboard_chat/messages.py",
              "dashboard_chat/job.py", "dashboard_chat/static/chat.html"):
        chk(f"real chat-frontend intent: {f} is a module to build", f in got, True)
    chk("the target itself is never foreign", TGT in got or "routes.py" in got, False)
    chk("a ~/ path (`~/bin/ollama-queue.py enqueue`) is an external reference",
        any("ollama-queue" in g for g in got), False)
    act, msg = mmc(intent, iface, TGT, empty)
    chk("real chat-frontend run (new project) is REFUSED", act, "refuse")
    chk("refusal names the override flag", "--allow-multi-module" in msg, True)
    chk("--allow-multi-module downgrades to a warning",
        mmc(intent, iface, TGT, empty, allow=True)[0], "warn")
    chk("a slicer-driven per-slice run only warns",
        mmc(intent, iface, TGT, empty, lenient=True)[0], "warn")
    chk("ONE other file only warns",
        mmc("Add the route; it calls helpers in lib/newHelper.ts.", None,
            "app/route.ts", empty)[0], "warn")
    chk("a single-file intent passes silently",
        mmc("Add parse_header(raw) to demo/parse.py returning a dict.", None,
            "demo/parse.py", empty), (None, ""))

with tempfile.TemporaryDirectory() as repo:
    for f in ("broker_guard/detection.py", "broker_guard/profile.py",
              "broker_guard/brokers.py", "lib/bfmrSyncScope.ts", "lib/bfmrSyncTrigger.ts"):
        (Path(repo) / f).parent.mkdir(parents=True, exist_ok=True)
        (Path(repo) / f).write_text("x\n")
    chk("bare names of EXISTING sibling files are context, not modules",
        mmc("serpwatch uses detection.py, profile.py and brokers.py unchanged.", None,
            "broker_guard/serpwatch.py", repo)[0], None)
    chk("existing repo-relative paths are context, not modules",
        mmc("route imports lib/bfmrSyncScope.ts and lib/bfmrSyncTrigger.ts.", None,
            "app/api/x/route.ts", repo)[0], None)
    chk("bare names of existing files in ANOTHER dir than the target are context",
        mmc("the route reuses bfmrSyncScope.ts and bfmrSyncTrigger.ts.", None,
            "app/api/x/route.ts", repo)[0], None)
    chk("'Next.js' is a framework name, not a file",
        fmf("A Next.js route handler.", "app/api/x/route.ts", repo), [])
    chk("an absent PascalCase component file still counts",
        fmf("Render it in components/BfmrLinker.tsx.", "app/page.tsx", repo),
        ["components/BfmrLinker.tsx"])

# CLI wiring (no side effects: --help exits before any run state is written)
h = subprocess.run([sys.executable, str(AUTO), "--help"], capture_output=True, text=True,
                   timeout=60)
chk("--allow-multi-module is a CLI flag", "--allow-multi-module" in h.stdout, True)
src = AUTO.read_text()
chk("main() calls multi_module_check and die()s on 'refuse'",
    bool(re.search(r"multi_module_check\(\s*a\.intent.*?if _mm_act == \"refuse\":\s*die\(",
                   src, re.S)), True)

print("\nALL PASS" if not fails else f"\n{fails} FAIL")
sys.exit(1 if fails else 0)
