#!/usr/bin/env python3
"""SEALED ENVIRONMENT FIXTURE + FAKE-DOM LINT + PASS-THEN-INVERT (2026-10-09).

Evidence: smoke job bbafd0efed38 (auto-author-smoke-costco-login-confirm) burned all 24 authoring
iterations debugging a model-written fake browser in verify.test.ts (page.window missing;
querySelectorAll ignoring comma selectors). Pinned here, each against the REAL tool:

  A. both template suites (dispatch-env.test.ts under node --experimental-strip-types,
     dispatch_env_test.py under unittest) via `dispatch_env_fixture.py selftest`.
  B. dispatch_env_fixture: fixture_names, drop/ensure statuses, 0444 mode, tamper -> restored,
     deleted -> created, no-template, only_if_present_or_used.
  C. lint_fake_env: every rule fires on a positive; clean/imported/comment-only/call-only
     negatives stay silent; the message points at the fixture.
  D. scaffold --new-project drops the file read-only and lists it in the manifest
     (typescript and python).
  E. rendered auto-harness-check.py in a temp worktree (HOME -> overlay bin): `class FakePage`
     and a hand-rolled querySelectorAll are bounced with FAKE-DOM LINT; a clean fixture that
     imports dispatch-env passes the lint; a tampered / deleted dispatch-env.ts is restored.
  F. author prompt carries the API doc, the PASS-FIRST-THEN-INVERT flow and the ban; the
     "PASSES at baseline" self-check message carries the invert move.

Env overrides: AUTO_SRC SCAFFOLD_SRC ENVFX_SRC (the module). Hermetic: temp dirs, temp HOME."""
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")
SCAF = Path(os.environ.get("SCAFFOLD_SRC") or HERE / "ollama-dispatch-scaffold")
ENVFX = Path(os.environ.get("ENVFX_SRC") or HERE / "dispatch_env_fixture.py")
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    ld = SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    argv, sys.argv = sys.argv, [str(path)]
    try:
        ld.exec_module(m)
    finally:
        sys.argv = argv
    return m


def tmpdir(p):
    return Path(tempfile.mkdtemp(prefix=p))


def part_a():
    p = subprocess.run([sys.executable, str(ENVFX), "selftest"], capture_output=True, text=True,
                       timeout=600, cwd=str(HERE))
    chk("A template suites (node + unittest) pass via selftest", p.returncode, 0)
    if p.returncode:
        print(p.stdout[-1500:], p.stderr[-1500:])


def part_b(fx):
    chk("B fixture_names ts", fx.fixture_names("typescript"), ["dispatch-env.ts"])
    chk("B fixture_names fixture file .mjs", fx.fixture_names("verify.test.mjs"), ["dispatch-env.ts"])
    chk("B fixture_names python", fx.fixture_names("python"), ["dispatch_env.py"])
    chk("B fixture_names bash -> none", fx.fixture_names("bash"), [])
    wt = tmpdir("envfx-b-")
    try:
        chk("B drop -> created", fx.drop(wt, "dispatch-env.ts", HERE), "created")
        f = wt / "dispatch-env.ts"
        chk("B file is read-only 0444", stat.S_IMODE(f.stat().st_mode), 0o444)
        chk("B bytes == template", f.read_bytes(),
            (HERE / "dispatch-templates" / "dispatch-env.ts").read_bytes())
        chk("B drop again -> ok", fx.drop(wt, "dispatch-env.ts", HERE), "ok")
        f.chmod(0o644)
        f.write_text("// tampered\n")
        chk("B tampered -> restored", fx.drop(wt, "dispatch-env.ts", HERE), "restored")
        chk("B restored bytes == template", f.read_bytes(),
            (HERE / "dispatch-templates" / "dispatch-env.ts").read_bytes())
        f.chmod(0o644)
        f.unlink()
        chk("B deleted -> created", fx.drop(wt, "dispatch-env.ts", HERE), "created")
        chk("B no template -> no-template", fx.drop(wt, "nope.ts", HERE), "no-template")
        chk("B py drop", fx.drop(wt, "dispatch_env.py", HERE), "created")
        w2 = tmpdir("envfx-b2-")
        (w2 / "verify.test.ts").write_text("import x from './other.ts'\n")
        chk("B only_if_present_or_used: unused+absent -> not created",
            fx.ensure(w2, ["dispatch-env.ts"], HERE, only_if_present_or_used=True),
            [("dispatch-env.ts", "absent")])
        (w2 / "verify.test.ts").write_text("import { x } from './dispatch-env.ts'\n")
        chk("B only_if_present_or_used: referenced -> created",
            fx.ensure(w2, ["dispatch-env.ts"], HERE, only_if_present_or_used=True),
            [("dispatch-env.ts", "created")])
        shutil.rmtree(w2, ignore_errors=True)
    finally:
        shutil.rmtree(wt, ignore_errors=True)


LINT_POS = {
    "class FakePage": "class FakePage {\n  constructor() { this.window = {}; }\n}\n",
    "class MockWindow": "class MockWindow {}\n",
    "class StubDocument": "class StubDocument { }\n",
    "bare class Browser": "class Browser {}\n",
    "factory makeFakePage": "function makeFakePage() { return {}; }\n",
    "const createDocument": "const createDocument = () => ({});\n",
    "querySelectorAll def (method)": "const o = { querySelectorAll(sel) { return []; } };\n",
    "querySelector def (prop)": "const o = { querySelector: (s) => null };\n",
    "querySelectorAll def (assign)": "let querySelectorAll = (s) => [];\n",
    "globalThis.document =": "globalThis.document = { body: {} };\n",
    "globalThis.window =": "globalThis.window = {};\n",
    "defineProperty document": "Object.defineProperty(globalThis, 'document', { value: {} });\n",
}
LINT_NEG = {
    "imports the double": "import { installFakeBrowser, makeFakePage } from './dispatch-env.ts';\n"
                          "const page = makeFakePage({url:'http://x/'});\n",
    "calls querySelectorAll": "const n = document.querySelectorAll('a, b');\n"
                              "const m = el.querySelector('a');\n",
    "commented fake": "// class FakePage {}\n/* function makeFakePage() {} */\nconst x = 1;\n",
    "unrelated class": "class Cart { add() {} }\nfunction makeCart() { return new Cart(); }\n",
    "pageSize const": "const pageSize = 10;\nconst windowMs = 5;\n",
}


def part_c(fx):
    wt = tmpdir("envfx-c-")
    try:
        for name, body in LINT_POS.items():
            (wt / "verify.test.ts").write_text(body)
            got = fx.lint_fake_env(wt, ["verify.test.ts"])
            chk(f"C lint FIRES: {name}", bool(got), True)
        for name, body in LINT_NEG.items():
            (wt / "verify.test.ts").write_text(body)
            chk(f"C lint silent: {name}", fx.lint_fake_env(wt, ["verify.test.ts"]), [])
        (wt / "dispatch-env.ts").write_text("class FakePage {}\n")
        chk("C lint skips dispatch-env.ts itself", fx.lint_fake_env(wt, ["dispatch-env.ts"]), [])
        (wt / "t.py").write_text("class FakePage: pass\n")
        chk("C lint ignores python files", fx.lint_fake_env(wt, ["t.py"]), [])
        (wt / "verify.test.ts").write_text(LINT_POS["class FakePage"])
        msg = fx.lint_message(fx.lint_fake_env(wt, ["verify.test.ts"]))
        chk("C message names FAKE-DOM LINT + the import + installFakeBrowser",
            all(s in msg for s in ("FAKE-DOM LINT", "dispatch-env", "installFakeBrowser", "makeFakePage")), True)
    finally:
        shutil.rmtree(wt, ignore_errors=True)


def part_d():
    for lang, want, absent in (("typescript", "dispatch-env.ts", "dispatch_env.py"),
                               ("python", "dispatch_env.py", "dispatch-env.ts")):
        t = tmpdir("envfx-d-")
        try:
            dest = t / "proj"   # --new-project IS the worktree
            cmd = [sys.executable, str(SCAF), "--new-project", str(t / "proj"), "--dest", str(t / "unused"),
                   "--label", "envfx-" + lang, "--lang", lang, "--target",
                   "lib/x.ts" if lang == "typescript" else "x.py"]
            if lang == "typescript":
                cmd += ["--ts-runner", "node-test"]
            env = {**os.environ, "HOME": str(t / "home")}
            (t / "home").mkdir()
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env, cwd=str(t))
            f = dest / want
            chk(f"D scaffold {lang}: {want} dropped", f.is_file(), True)
            if f.is_file():
                chk(f"D scaffold {lang}: read-only", bool(f.stat().st_mode & 0o222), False)
            chk(f"D scaffold {lang}: other-language file absent", (dest / absent).exists(), False)
            try:
                man = json.loads((dest / ".dispatch-harness.json").read_text())
            except Exception:
                man = {}
            chk(f"D scaffold {lang}: listed in manifest authored", want in man.get("authored", []), True)
            if p.returncode:
                print(p.stdout[-800:], p.stderr[-800:])
        finally:
            shutil.rmtree(t, ignore_errors=True)


GOOD_FIXTURE = """import { test } from 'node:test';
import assert from 'node:assert/strict';
import { installFakeBrowser } from './dispatch-env.ts';
test('x', (t) => {
  const env = installFakeBrowser({ html: '<a id="a">1</a>' });
  t.after(env.restore);
  assert.equal(document.querySelectorAll('#a, .b').length, 1);
});
"""


def mk_wt(auto, fixture_text):
    wt = tmpdir("envfx-e-")
    (wt / "lib").mkdir()
    (wt / "lib/x.ts").write_text("export const x = 1;\n")
    (wt / ".dispatch-harness.json").write_text(json.dumps(
        {"target": "lib/x.ts", "fixture": "verify.test.ts", "creation_task": False,
         "authored": ["TASK.md", "check_literals.py", "refimpl.py", "verify.sh", "verify.test.ts"]}))
    (wt / "verify.sh").write_text('#!/usr/bin/env bash\nTEST_FILES="verify.test.ts"\nexit 1\n')
    (wt / "verify.test.ts").write_text(fixture_text)
    (wt / "TASK.md").write_text("# TASK: x\n\n## Required change\nx.\n\n## Must contain\n- `x`\n\n"
                                "## Scope\nOnly edit `lib/x.ts` (via refimpl.py).\n")
    (wt / "refimpl.py").write_text("import pathlib\npathlib.Path('lib/x.ts').write_text('export const x = 2;\\n')\n")
    (wt / "check_literals.py").write_text("print('ok')\n")
    for a in (["init", "-q"], ["add", "lib/x.ts", ".dispatch-harness.json"],
              ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"]):
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(wt), *a],
                       check=True, capture_output=True)
    auto.write_harness_check(wt, "typescript")
    return wt


def run_check(wt, home):
    p = subprocess.run([sys.executable, "auto-harness-check.py"], cwd=str(wt), capture_output=True,
                       text=True, timeout=300, env={**os.environ, "HOME": str(home)})
    return p.returncode, p.stdout + p.stderr


def part_e(auto):
    home = tmpdir("envfx-home-")
    (home / "bin").symlink_to(HERE)
    try:
        for name, body in (("class FakePage", "class FakePage { constructor(){ this.window = {}; } }\n"),
                           ("hand-rolled querySelectorAll",
                            "const doc = { querySelectorAll(s) { return []; } };\n")):
            wt = mk_wt(auto, body)
            try:
                rc, out = run_check(wt, home)
                chk(f"E self-check bounces {name} with FAKE-DOM LINT", rc == 1 and "FAKE-DOM LINT" in out, True)
                chk(f"E {name}: message points at the fixture", "installFakeBrowser" in out, True)
            finally:
                shutil.rmtree(wt, ignore_errors=True)
        wt = mk_wt(auto, GOOD_FIXTURE)
        try:
            rc, out = run_check(wt, home)
            chk("E clean fixture importing dispatch-env is NOT lint-bounced", "FAKE-DOM LINT" in out, False)
            f = wt / "dispatch-env.ts"
            chk("E sealed file was dropped by the self-check", f.is_file(), True)
            chk("E ... read-only", bool(f.stat().st_mode & 0o222), False)
            tmpl = (HERE / "dispatch-templates" / "dispatch-env.ts").read_bytes()
            f.chmod(0o644)
            f.write_text("export const hacked = 1;\n")
            rc, out = run_check(wt, home)
            chk("E tampered dispatch-env.ts is restored byte-for-byte", f.read_bytes(), tmpl)
            chk("E ... with a NOTE telling the model", "SEALED read-only environment fixture" in out, True)
            f.chmod(0o644)
            f.unlink()
            run_check(wt, home)
            chk("E deleted dispatch-env.ts is restored", f.read_bytes() if f.exists() else b"", tmpl)
        finally:
            shutil.rmtree(wt, ignore_errors=True)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def part_f(auto):
    auto.load_attempts = lambda a: []
    tmp = tmpdir("envfx-f-")
    try:
        def prompt(lang, target, intent="Add f(x) to the module."):
            a = SimpleNamespace(intent=intent, interface="", lang=lang, label="x", repo=str(tmp),
                                new_project=None, dest=None, target=target)
            return auto.author_prompt(a, target)
        ts = prompt("typescript", "lib/x.ts")
        chk("F TS prompt: PASS FIRST, THEN INVERT flow", "FIXTURE FLOW: PASS FIRST, THEN INVERT" in ts, True)
        chk("F TS prompt: sealed fixture API doc", "dispatch-env.ts" in ts and "openMemoryDb" in ts, True)
        chk("F TS prompt: bans hand-written fakes", "FAKE-DOM LINT" in ts or "hand-written" in ts.lower(), True)
        br = prompt("typescript", "sidecar/loginFlow.js",
                    "Fix the login flow page.waitForSelector handling in the browser DOM.")
        chk("F browserish prompt: installFakeBrowser documented", "installFakeBrowser" in br, True)
        py = prompt("python", "x.py")
        chk("F python prompt: dispatch_env documented + flow", "dispatch_env" in py
            and "PASS FIRST, THEN INVERT" in py, True)
        chk("F PASS_THEN_INVERT_TEXT module constant", "PASS FIRST, THEN INVERT" in auto.PASS_THEN_INVERT_TEXT, True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # the self-check's baseline-green message
    home = tmpdir("envfx-homef-")
    (home / "bin").symlink_to(HERE)
    wt = mk_wt(auto, GOOD_FIXTURE)
    try:
        (wt / "verify.sh").write_text('#!/usr/bin/env bash\nTEST_FILES="verify.test.ts"\necho VERIFY_OK\nexit 0\n')
        rc, out = run_check(wt, home)
        chk("F 'PASSES at baseline' failure carries PASS-THEN-INVERT move 2",
            "PASSES at baseline" in out and "PASS-THEN-INVERT" in out, True)
    finally:
        shutil.rmtree(wt, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)


def main():
    fx = load(ENVFX, "envfx_t")
    auto = load(AUTO, "auto_envfx")
    part_a()
    part_b(fx)
    part_c(fx)
    part_d()
    part_e(auto)
    part_f(auto)
    print()
    if FAILS:
        print("FAILED: %d -- %s" % (len(FAILS), "; ".join(FAILS)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
