#!/usr/bin/env python3
"""Scaffolded TS/JS fixtures' chk() prints the ACTUAL value on FAIL (2026-10-02,
Rivian s5: a stub that never incremented fetchCallCount read as "too many calls"
for 7 attempts because chk printed only pass/fail). Extracts the chk definition
from the REAL TS_FIXTURE / EXPR_FIXTURE templates and runs it under node.
--revert-check restores the old 2-arg chk in each template -> RED."""
import importlib.util, os, re, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("SCAFFOLD_SRC") or HERE / "ollama-dispatch-scaffold")
FAILS = []


def check(name, ok, extra=""):
    print(("ok  " if ok else "FAIL") + ": " + name + ("" if ok else "  " + extra))
    if not ok:
        FAILS.append(name)


def chk_def(tmpl):
    m = re.search(r"let fails = 0;.*?\n\}\};\n", tmpl, re.S)
    return m.group(0).replace("{{", "{").replace("}}", "}") if m else ""


def run(code, ext):
    f = Path(tempfile.mkdtemp()) / f"t.{ext}"
    f.write_text(code)
    r = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=60)
    return r.stdout + r.stderr


def main():
    ld = SourceFileLoader("scaf_chk", str(SRC))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("scaf_chk", ld))
    sys.argv = [str(SRC)]
    ld.exec_module(m)
    body = ("let n = 0;\nchk('count is 1', n === 1, n);\nchk('passes', true, 5);\n"
            "chk('legacy two-arg', false);\nconsole.log('fails=' + fails);\n")
    for name, tmpl, ext in (("TS", m.TS_FIXTURE, "mts"), ("JS expr", m.EXPR_FIXTURE, "mjs")):
        d = chk_def(tmpl)
        check(f"{name}: chk definition found", bool(d))
        out = run(d + body, ext)
        check(f"{name}: FAIL prints the actual value", "FAIL - count is 1  (got: 0)" in out, out[-300:])
        check(f"{name}: a pass prints no value", "ok - passes\n" in out, out[-300:])
        check(f"{name}: legacy 2-arg chk still works", "FAIL - legacy two-arg\n" in out and "fails=2" in out, out[-300:])
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


def revert_check():
    src = SRC.read_text()
    bad = 0
    old_ts = ("const chk = (name: string, cond: boolean) => {{\n  checks++;\n"
              "  console.log((cond ? 'ok - ' : 'FAIL - ') + name);\n  if (!cond) fails++;\n}};")
    old_js = ("const chk = (name, cond) => {{\n  checks++;\n"
              "  console.log((cond ? 'ok - ' : 'FAIL - ') + name);\n  if (!cond) fails++;\n}};")
    pat_ts = re.compile(r"const chk = \(name: string, cond: boolean, \.\.\.got: unknown\[\]\) => \{\{.*?\n\}\};", re.S)
    pat_js = re.compile(r"const chk = \(name, cond, \.\.\.got\) => \{\{.*?\n\}\};", re.S)
    for name, pat, old in (("TS template", pat_ts, old_ts), ("JS expr template", pat_js, old_js)):
        assert len(pat.findall(src)) == 1, name
        with tempfile.NamedTemporaryFile("w", suffix="-scaf", delete=False, dir=str(HERE)) as f:
            f.write(pat.sub(lambda _: old, src))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "SCAFFOLD_SRC": f.name},
                           capture_output=True, text=True, timeout=300)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert {name} chk -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
