#!/usr/bin/env python3
"""dispatch_env_fixture.py -- the pipeline's SEALED environment fixtures (2026-10-09).

WHY. Smoke job bbafd0efed38 (auto-author-smoke-costco-login-confirm) burned all 24 iterations
debugging a model-written fake browser in verify.test.ts (page.window missing; querySelectorAll
that ignored comma selectors). A hand-written double is a second program to debug, and
agents over-mock (arxiv 2602.00409). So the scaffold drops a vetted, tested double into every
harness worktree and the self-check refuses a fixture that ships its own.

THIS MODULE (pure; shared by ollama-dispatch-scaffold, ollama-dispatch-auto and the rendered
auto-harness-check.py, which loads it from ~/bin like source_text_harness.py -- fail-open):
  * fixture_names(lang_or_fixture)  which sealed file(s) a harness of that language gets
  * drop(wt, name)                  write the template READ-ONLY (0444); idempotent
  * ensure(wt, names)               restore it byte-for-byte when missing/edited/writable
  * lint_fake_env(wt, files)        model-defined Fake Page/Window/Document/Browser classes,
                                    factories or hand-rolled querySelector in a JS/TS fixture
  * JS_API_DOC / PY_API_DOC         the API listing handed to the author prompt
  * PASS_THEN_INVERT                the authoring flow text (AssertFlip), shared by prompt+messages
Templates live in ~/bin/dispatch-templates/ and are proven by dispatch-env.test.ts /
dispatch_env_test.py there (and by test-dispatch-env-fixture.py + the `envfixture` canary seam).
`python3 dispatch_env_fixture.py selftest` runs both template suites."""
import hashlib
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
JS_NAME = "dispatch-env.ts"
PY_NAME = "dispatch_env.py"
NAMES = (JS_NAME, PY_NAME)
_JSISH = ("ts", "typescript", "js", "javascript")
_JS_EXT = (".ts", ".mts", ".js", ".mjs", ".tsx", ".cts", ".cjs")
IMPORT_LINE = ("import { installFakeBrowser, makeFakePage, makeFakeContext, installFetchStub, "
               "installChromeStub, openMemoryDb } from './dispatch-env.ts';")


def template_dir(bin_dir=None) -> Path:
    return Path(bin_dir or HERE) / "dispatch-templates"


def template_path(name, bin_dir=None) -> Path:
    return template_dir(bin_dir) / name


def sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fixture_names(lang_or_fixture) -> list:
    """Sealed files for a harness: --lang ts/js (or a .ts/.js/.mts/.mjs fixture name) ->
    dispatch-env.ts; python (or a .py fixture name) -> dispatch_env.py; anything else none."""
    s = str(lang_or_fixture or "").strip()
    low = s.lower()
    if low in _JSISH or low.endswith(_JS_EXT):
        return [JS_NAME]
    if low in ("python", "py") or low.endswith(".py"):
        return [PY_NAME]
    return []


def drop(wt, name, bin_dir=None) -> str:
    """Write the template to <wt>/<name>, read-only. Returns "created" | "restored" | "ok" |
    "no-template". A target that already matches (bytes AND read-only mode) is left alone."""
    src = template_path(name, bin_dir)
    if not src.is_file():
        return "no-template"
    data = src.read_bytes()
    dst = Path(wt) / name
    status = "created"
    if dst.exists() or dst.is_symlink():
        try:
            same = dst.read_bytes() == data
            readonly = not (dst.stat().st_mode & 0o222)
        except OSError:
            same = readonly = False
        if same and readonly:
            return "ok"
        status = "ok" if same else "restored"
        try:
            dst.chmod(0o644)
        except OSError:
            pass
        dst.unlink()
    dst.write_bytes(data)
    dst.chmod(0o444)
    return status


def ensure(wt, names=NAMES, bin_dir=None, only_if_present_or_used=False) -> list:
    """Make each sealed file in `names` present, byte-identical to its template and read-only.
    Returns [(name, status)] where status is drop()'s. With only_if_present_or_used a file that
    is absent AND not referenced by any fixture is not created (old worktrees stay untouched)."""
    out = []
    for n in names:
        p = Path(wt) / n
        if only_if_present_or_used and not p.exists() and not _referenced(wt, n):
            out.append((n, "absent"))
            continue
        out.append((n, drop(wt, n, bin_dir)))
    return out


def _referenced(wt, name) -> bool:
    stem = name.rsplit(".", 1)[0]
    for f in Path(wt).glob("*"):
        if f.is_file() and f.name != name and f.suffix in (".ts", ".mts", ".js", ".mjs", ".py"):
            try:
                if stem in f.read_text(errors="replace")[:200000]:
                    return True
            except OSError:
                pass
    return False


# ---------------------------------------------------------------------------
# LINT: model-defined fake DOM / page / window / browser
# ---------------------------------------------------------------------------
_COMMENT_RE = re.compile(r"/\*.*?\*/|(?<![:'\"`\\])//[^\n]*", re.S)
_DOMWORD = r"(?:Page|Window|Document|Browser|DOM|Dom)"
# class FakePage / MockWindow / StubDocument / class Page / class FakeBrowserContext ...
_CLASS_RE = re.compile(
    r"\bclass\s+((?:Fake|Mock|Stub|Dummy|Test|Simple|Mini|Tiny|Stubbed)\w*" + _DOMWORD + r"\w*|"
    r"(?:Page|Window|Document|Browser|DOM|Dom)\w{0,12})\b(?!\s*\()")
# function makeFakePage( / const createDocument = / let fakeWindow = (factory or object)
_FACTORY_RE = re.compile(
    r"\b(?:function\s*\*?|const|let|var)\s+((?:fake|mock|stub|make|create|build|new|dummy|test)\w*" + _DOMWORD + r"\w*)\s*(?:\(|=)",
    re.I)
# hand-rolled selector engine: a *definition* of querySelector[All], never a call (`.querySelectorAll(`)
_QS_DEF_RE = re.compile(r"(?<![.\w$])(querySelector(?:All)?)\s*(?::|=(?!=)|\([^)\n]*\)\s*\{)")
# globalThis.document = ... / window = { ... } / Object.defineProperty(globalThis, 'document', ...)
_GLOBAL_ASSIGN_RE = re.compile(
    r"\b(?:globalThis|global)\s*\.\s*(document|window)\s*=(?!=)|"
    r"\bObject\.defineProperty\(\s*(?:globalThis|global)\s*,\s*['\"](document|window)['\"]")


def _imported_names(src) -> set:
    names = set()
    for m in re.finditer(r"\bimport\s*(?:type\s*)?\{([^}]*)\}\s*from\s*['\"][^'\"]*dispatch-env[^'\"]*['\"]", src):
        for part in m.group(1).split(","):
            part = part.strip()
            if part:
                names.add(part.split(" as ")[-1].strip())
    return names


def lint_fake_env(wt, fixture_files) -> list:
    """Messages (one per offence, <= 6) for hand-built DOM/page/window/browser doubles in the JS/TS
    fixture file(s). Static text checks on comment-blanked source; [] when clean or unreadable.
    Names imported from dispatch-env are never flagged."""
    out = []
    for f in list(fixture_files)[:6]:
        p = Path(f)
        if not p.is_absolute():
            p = Path(wt) / f
        if p.name in NAMES or not str(p).endswith(_JS_EXT):
            continue
        try:
            src = p.read_text(errors="replace")
        except OSError:
            continue
        src = _COMMENT_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), src)
        ok = _imported_names(src)
        name = p.name
        line = lambda pos: src.count("\n", 0, pos) + 1
        for m in _CLASS_RE.finditer(src):
            if m.group(1) in ok:
                continue
            out.append("%s:%d defines `class %s` -- a hand-written fake page/window/document/browser."
                       % (name, line(m.start()), m.group(1)))
        for m in _FACTORY_RE.finditer(src):
            if m.group(1) in ok:
                continue
            out.append("%s:%d defines `%s` -- a hand-written fake page/window/document/browser factory."
                       % (name, line(m.start()), m.group(1)))
        for m in _QS_DEF_RE.finditer(src):
            out.append("%s:%d defines `%s` -- a hand-rolled selector engine (comma lists, attribute operators "
                       "and combinators are exactly what hand-rolled ones get wrong)."
                       % (name, line(m.start()), m.group(1)))
        for m in _GLOBAL_ASSIGN_RE.finditer(src):
            out.append("%s:%d installs its own `%s` global by hand."
                       % (name, line(m.start()), m.group(1) or m.group(2)))
    return out[:6]


def lint_message(findings) -> str:
    return ("FAKE-DOM LINT: " + "\n  ".join(findings) + "\n"
            "  FIX: delete the hand-written double and use the vetted, sealed fixture that is already in "
            "this worktree:\n    " + IMPORT_LINE + "\n"
            "  `installFakeBrowser({html, url, route})` installs a REAL element tree (window, document, "
            "location, fetch, XHR, localStorage) with a correct selector engine (comma lists, [attr*=..], "
            "combinators, :not/:nth-child); `makeFakePage({url})` is a Playwright-shaped page whose "
            "evaluate/$eval/waitFor*/goto/on/frames/locator work against it and which exposes "
            "page.window / page.document. Call restore() in t.after(). Do NOT edit dispatch-env.ts "
            "(sealed; the self-check restores it) and do NOT re-implement any of it.")


# ---------------------------------------------------------------------------
# prompt text
# ---------------------------------------------------------------------------
JS_API_DOC = """\
`dispatch-env.ts` is ALREADY in this worktree (sealed, read-only, tested). IMPORT it; never paste it,
edit it or write your own fake page / window / document / querySelector (the self-check REJECTS that).
  """ + IMPORT_LINE + """
- `installFakeBrowser({html?, url?, route?, selectors?, cookie?})` -> `{window, document, location, fetchCalls, xhrCalls,
  navigations, FakeXHR, restore}`. A real element tree: `window === globalThis` (so `window.__x`, bare `document`, `fetch`,
  `XMLHttpRequest`, `location`, `localStorage`, `addEventListener` all work for code run via page.evaluate). `route(url,
  {method, headers, body})` returns `{status?, body?, headers?}` for every fetch/XHR. Call `restore()` in `t.after()`; it
  also drops any `window.__state` the code stashed.
- `document.querySelector/All/getElementById/createElement/...`, elements with `getAttribute/dataset/classList/closest/
  matches/click()/addEventListener/innerHTML/textContent/value`. Selectors support comma lists, `#id .class tag`,
  `[a]`/`[a=v]`/`[a*=v]`/`[a^=v]`/`[a$=v]`/`[a~=v]`, `> + ~` combinators, `:not() :first-child :last-child :nth-child()
  :checked :disabled :empty`. An unsupported selector THROWS. querySelectorAll returns a NodeList (no .map/.some; use Array.from).
- `makeFakePage({url?, html?, gotoThrowsAfter?, gotoHtml?})` -> Playwright-shaped page: `evaluate(fn,arg)` (runs fn IN NODE against
  the installed fake window), `$ $$ $eval $$eval`, `goto` (records `page.gotoCalls`, updates `url()`, re-runs init scripts),
  `addInitScript(fn|string)`, `waitForSelector/Function/URL/LoadState/Timeout/Navigation/Response` (resolve when satisfied now,
  reject with a TimeoutError otherwise -- never hang), `click fill type check locator() content() title() screenshot()`,
  `on/once/off/emit`, `frames() mainFrame()`, `context()`, `close()/isClosed()`, `page.window`, `page.document`, `page.setUrl(u)`.
  `makeFakeContext(pages)` -> `{pages, newPage, addInitScript, cookies, storageState, ...}`. `loadHtml(html)` -> a detached document.
- `installFetchStub(route)` -> `{calls, restore}` (no DOM); `fakeResponse(status, body, headers)`, `jsonResponse(body, status)`.
- `installChromeStub({storage:{local,sync,session}, tabs, manifest})` -> `{chrome, calls, tabs, restore}`: chrome.storage.local/
  sync/session (+onChanged), runtime.sendMessage/onMessage/getURL/getManifest/onInstalled, tabs, scripting, alarms, permissions,
  windows, action; every event has `.emit(...)`.
- `openMemoryDb({sql?, migrationsDir?})` -> in-memory sqlite (`exec`, `prepare(sql).all/get/run`); `migrationsDir` replays
  `<dir>/<name>/migration.sql` in sorted order like prisma migrate.
"""

PY_API_DOC = """\
`dispatch_env.py` is ALREADY in this worktree (sealed, read-only, tested). `from dispatch_env import ...`; never edit or re-write it.
- `memory_db(*sql, migrations_dir=None)` -> in-memory sqlite3 (Row factory, foreign keys ON); `migrations_dir` replays
  `<dir>/<name>/migration.sql` in sorted order.
- `FakeClock(start)` with `.time() .now() .sleep(s) .advance(s)` (inject `clock.time`; never sleep for real).
- `StubHttp(route)` records `.calls`; `route(method, url, **kw)` returns a StubResponse | `(status, body[, headers])`;
  `.get/.post/.put/.patch/.delete/.request`. `StubResponse(status, body, headers)` has `.ok .status_code .text .json()`.
- `env(**vars)` (set/unset os.environ for a block) and `tmp_cwd()` (chdir into a fresh temp dir).
"""

PASS_THEN_INVERT = """\
## FIXTURE FLOW: PASS FIRST, THEN INVERT (the order the self-check expects)
LLMs write passing tests far more reliably than failing ones, and a test that is red for a SETUP reason (bad import, broken double,
wrong mock shape) proves nothing. So author the fixture in two moves:
1. CHARACTERISE: write the cases so they PASS against the CURRENT code (the baseline/stub): import the target, build the
   environment with the sealed fixture, call the code and assert what it does TODAY. Run `python3 auto-harness-check.py`;
   it will say "verify.sh PASSES at baseline" -- that is the expected result of this move (the imports, mocks and environment are
   now proven to work). If it is red for a SetUp/Import/TypeError reason, fix THAT first: nothing else is trustworthy yet.
2. INVERT: now change ONLY the assertions for the behaviour this task adds or fixes to the REQUIRED behaviour (the values the
   refimpl produces). They must now FAIL at baseline for a behavioural reason (an AssertionError naming the wrong value), and PASS
   once `python3 refimpl.py` has run. Keep the assertions that characterise unchanged behaviour as over-trigger guards.
Do not skip move 1: a fixture that was never green cannot tell you whether red means "behaviour" or "broken harness".
"""

BASELINE_PASSES_NOTE = (
    "\nPASS-THEN-INVERT: this green-at-baseline result is move 1 (CHARACTERISE) of the flow, so your imports, mocks and "
    "environment work -- good. Now do move 2 (INVERT): change the assertions for the behaviour this TASK adds or fixes to the "
    "REQUIRED values (what refimpl.py produces) so that they FAIL here for a behavioural reason (an AssertionError naming the "
    "wrong value) and PASS after `python3 refimpl.py`. Keep the assertions that characterise unchanged behaviour.")

SETUP_RED_NOTE = (
    "\nPASS-THEN-INVERT: if the failures above are setup errors (ReferenceError, 'is not a function' on the fixture's own double or "
    "mock, MODULE_NOT_FOUND, a SyntaxError in the fixture) rather than AssertionErrors naming a wrong value, step back: make the "
    "fixture PASS against the baseline behaviour first (assert what the code does TODAY), then invert only the changed assertions. "
    "Use the sealed `dispatch-env` fixture for any browser/chrome/fetch/sqlite environment instead of a hand-written double.")


def selftest(bin_dir=None) -> int:
    """Run both template suites; 0 when green."""
    td = template_dir(bin_dir)
    rc = 0
    node = subprocess.run(["node", "--experimental-strip-types", "--test", "dispatch-env.test.ts"], cwd=td,
                          capture_output=True, text=True, timeout=300)
    print(node.stdout[-600:] if node.returncode else "dispatch-env.test.ts: ok")
    rc |= node.returncode
    py = subprocess.run([sys.executable, "dispatch_env_test.py"], cwd=td, capture_output=True, text=True, timeout=120)
    print(py.stderr[-400:] if py.returncode else "dispatch_env_test.py: ok")
    return rc | py.returncode


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(selftest())
    print(__doc__)
