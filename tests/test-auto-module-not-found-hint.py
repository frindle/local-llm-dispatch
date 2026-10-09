#!/usr/bin/env python3
"""auto-harness-check + the continuation prompt name an unresolved IMPORT instead of
blaming the fake environment (2026-10-06, rt-bfmr-tls-fingerprint 06f5c70bd83e: the
fixture imported '../lib/apiCallLog.ts' from the worktree root, every case died with
ERR_MODULE_NOT_FOUND, and the "# fail 10" JS branch told the model to suspect its
fakes; the continuation prompt then pasted the fake-browser helper because
`needsBrowserLikeTls` matched the bare `browser` keyword).

Behavioural: builds real worktrees from the REAL HARNESS_CHECK template.
--revert-check removes the module-not-found block from the template -> RED."""
import importlib.util, json, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")
FAILS = []


def check(name, ok, extra=""):
    print(("ok  " if ok else "FAIL") + ": " + name + ("" if ok else "  " + extra[-600:]))
    if not ok:
        FAILS.append(name)


def load():
    ld = SourceFileLoader("oda_mnf", str(AUTO))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("oda_mnf", ld))
    sys.argv = [str(AUTO)]
    ld.exec_module(m)
    return m


def mk_wt(m, err_line):
    wt = Path(tempfile.mkdtemp(prefix="mnf-")) / "wt"
    (wt / "lib").mkdir(parents=True)
    g = lambda *a: subprocess.run(["git", "-C", str(wt), *a], check=True, capture_output=True)
    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (wt / "lib" / "apiCallLog.ts").write_text("export const x = 1;\n")
    g("add", "."); g("commit", "-qm", "b")
    (wt / "TASK.md").write_text("## Must contain\n- `MARK`\nOnly edit `lib/apiCallLog.ts`\n")
    (wt / "verify.test.ts").write_text("// fixture\n")
    (wt / "err.txt").write_text(err_line)
    (wt / "verify.sh").write_text(
        'grep -q MARK lib/apiCallLog.ts || { echo "FAIL - no MARK"; exit 1; }\n'
        'cat err.txt; echo "# tests 10"; echo "# fail 10"; exit 1\n')
    (wt / "refimpl.py").write_text("open('lib/apiCallLog.ts','a').write('// MARK\\n')\n")
    (wt / ".dispatch-harness.json").write_text(json.dumps({"target": "lib/apiCallLog.ts"}))
    m.write_harness_check(wt, "ts")
    return wt


def run(wt):
    return subprocess.run([sys.executable, "auto-harness-check.py"], cwd=wt,
                          capture_output=True, text=True, timeout=120).stdout


def main():
    m = load()
    # 1) relative import resolved outside the worktree
    wt = mk_wt(m, "")
    outside = str(wt.parent / "lib" / "apiCallLog.ts")
    (wt / "err.txt").write_text(
        "  error: \"Cannot find module '" + outside + "' imported from "
        + str(wt / "verify.test.ts") + "\"\n  code: 'ERR_MODULE_NOT_FOUND'\n")
    o = run(wt)
    check("path MNF: import-path hint", "import PATH bug" in o, o)
    check("path MNF: names the root-relative target", "`./lib/apiCallLog.ts`" in o, o)
    check("path MNF: says it resolves outside the worktree", "OUTSIDE this worktree" in o, o)
    check("path MNF: names the importing file", "`verify.test.ts` imports" in o, o)
    check("path MNF: no fake-environment blame", "suspect the FAKE ENVIRONMENT" not in o, o)
    st = subprocess.run(["git", "-C", str(wt), "status", "--porcelain", "--", "lib"],
                        capture_output=True, text=True).stdout
    check("target left at baseline", st.strip() == "", st)

    # 2) bare package that is not installed
    wt2 = mk_wt(m, "Error: Cannot find package 'undici' imported from /x/lib/apiCallLog.ts\n")
    o2 = run(wt2)
    check("package MNF: not-installed hint", "is NOT installed in this worktree" in o2
          and "`undici`" in o2, o2)
    check("package MNF: no fake-environment blame", "suspect the FAKE ENVIRONMENT" not in o2, o2)

    # 3) control: a real assertion failure still gets the fake-environment hint
    wt3 = mk_wt(m, "AssertionError: 0 !== 2\n")
    o3 = run(wt3)
    check("control: assertion failure keeps the fake hint", "suspect the FAKE ENVIRONMENT" in o3, o3)
    check("control: no import hint", "import PATH bug" not in o3, o3)

    # 4) continuation prompt: no fake-browser paste, import note instead
    last = ("FAIL: the reference impl (refimpl.py) does not make verify.sh print VERIFY_OK\n"
            "error: \"Cannot find module '/a/lib/apiCallLog.ts' imported from /a/wt/verify.test.ts\"\n"
            "code: 'ERR_MODULE_NOT_FOUND'\n# fail 10\n")
    note = m.refimpl_fails_own_tests_note(last, "ts")
    check("note: says no case ran", "no test case ran" in note, note)
    check("note: no fake blame", "fake environment is the usual" not in note, note)
    a_browser = SimpleNamespace(lang="ts", intent="drives a browser page via page.evaluate",
                                interface="", edit_file=None, verify_shape=None)
    try:
        p = m.author_continue_prompt(a_browser, "lib/x.ts", last)
        check("prompt: sealed-fixture section NOT repeated on MNF", "Sealed browser/environment fixture" not in p, p[:400])
        p2 = m.author_continue_prompt(a_browser, "lib/x.ts", last.replace(
            "Cannot find module '/a/lib/apiCallLog.ts' imported from /a/wt/verify.test.ts", "0 !== 2")
            .replace("code: 'ERR_MODULE_NOT_FOUND'", ""))
        check("prompt control: sealed-fixture section still given for a real fake failure",
              "Sealed browser/environment fixture" in p2, p2[:400])
    except Exception as e:  # noqa
        check("author_continue_prompt callable", False, repr(e))

    # 5) browser keyword: identifier substring is not a browser target
    tls = SimpleNamespace(lang="ts", intent="Export needsBrowserLikeTls(url) and bfmrTlsConnectOptions()",
                          interface="")
    check("is_browserish: needsBrowserLikeTls is NOT browser-ish",
          not m.is_browserish(tls, "lib/apiCallLog.ts"))
    real = SimpleNamespace(lang="ts", intent="the init script runs in the browser", interface="")
    check("is_browserish: 'in the browser' still browser-ish", m.is_browserish(real, "lib/x.ts"))
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


def revert_check():
    src = AUTO.read_text()
    a = src.index("    # MODULE NOT FOUND OUTRANKS EVERY BEHAVIOURAL HINT")
    b = src.index("    # REPEATED CASE -> SUSPECT THE FIXTURE", a)
    with tempfile.NamedTemporaryFile("w", suffix="-auto", delete=False, dir=str(HERE)) as f:
        f.write(src[:a] + src[b:])
    r = subprocess.run([sys.executable, __file__], env={**os.environ, "AUTO_SRC": f.name},
                       capture_output=True, text=True, timeout=300)
    os.unlink(f.name)
    red = r.returncode != 0
    print(("bites" if red else "INERT") + ": remove the MNF block -> suite " + ("RED" if red else "green"))
    print("REVERT-CHECK OK" if red else "REVERT-CHECK FAILED")
    return 0 if red else 1


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
