#!/usr/bin/env python3
"""Every dispatch entry point defaults to the SAME model, and it is one Darkbloom
serves unaliased (2026-10-02, the owner approved qwen3.6-35b-a3b-vl-mtp-mxfp8): a
':'-tagged legacy default (qwen3.8:27b-q4_K_M) was silently aliased on the lane, so
logs, ladders and bake-off records named a model that never ran.
--revert-check plants the legacy default in each tool (BIN_DIR env -> mutated copy)."""
import json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIN = Path(os.environ.get("BIN_DIR") or HERE)
DEFAULTS = Path(os.environ.get("DEFAULTS_JSON") or Path.home() / ".ollama-dispatch" / "defaults.json")
FAILS = []
PATTERNS = {
    "ollama-dispatch-auto": r'^FALLBACK_MODEL = "([^"]+)"',
    "ollama-dispatch-draft": r'^DEFAULT_MODEL = "([^"]+)"',
    "ollama-dispatch-slice": r'ap\.add_argument\("--model", default="([^"]+)"\)',
    "ollama-dispatch-plan": r'ap\.add_argument\("--model", default="([^"]+)"\)',
    "ollama-queue.py": r'"DARKBLOOM_DEFAULT_MODEL",\s*\n\s*"([^"]+)"\)',
}


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def main():
    want = json.loads(DEFAULTS.read_text()).get("model")
    check("defaults.json model is served unaliased (no ':' tag)", ":" in (want or ":"), False)
    for tool, pat in PATTERNS.items():
        m = re.findall(pat, (BIN / tool).read_text(), re.M)
        check(f"{tool} default == defaults.json model", m, [want])
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


def revert_check():
    bad = 0
    for tool in PATTERNS:
        tmp = Path(tempfile.mkdtemp(prefix="dm-"))
        for t in PATTERNS:
            shutil.copy2(BIN / t, tmp / t)
        src = (tmp / tool).read_text()
        want = json.loads(DEFAULTS.read_text())["model"]
        (tmp / tool).write_text(src.replace(f'"{want}"', '"qwen3.8:27b-q4_K_M"', 1))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "BIN_DIR": str(tmp)},
                           capture_output=True, text=True, timeout=120)
        shutil.rmtree(tmp, ignore_errors=True)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": legacy default in {tool} -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    tmpd = Path(tempfile.mkdtemp()) / "defaults.json"
    tmpd.write_text(json.dumps({"model": "qwen3.8:27b-q4_K_M", "host": "studio"}))
    r = subprocess.run([sys.executable, __file__], env={**os.environ, "DEFAULTS_JSON": str(tmpd)},
                       capture_output=True, text=True, timeout=120)
    red = r.returncode != 0
    print(("bites" if red else "INERT") + f": legacy defaults.json -> suite {'RED' if red else 'green'}")
    bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
