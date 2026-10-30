#!/usr/bin/env python3
"""node:test mock.module lint in the auto-harness-check self-check + author prompt rule
(2026-10-06; rt-bg-commitments-fix-sync-guard(-v2), rt-egift-link-s1-s5-component,
rt-bfmr-tls-fingerprint: ~20 author/refine jobs at their iteration cap).

Measured on Node 26.7: mock.module SILENTLY ignores unknown option keys (`factory`, a
bare exports object) and mocks the module with NO exports; a static import of the
target hoists above every mock so the target binds the REAL deps; re-mocking per test
leaves the cached target on the first fake. The run output never names any of these,
and the self-check sent the model after a DOM `FakeXHR` instead.

Behavioural: real worktrees driven by the REAL HARNESS_CHECK template from
ollama-dispatch-auto (AUTO_SRC overrides, so the suite can be pointed at the .bak to
prove it goes RED there)."""
import importlib.util, json, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")
FAILS = []

ASSERT_OUT = ("=== behavioural tests ===\nnot ok 1 - x\n  error: 'AssertionError: 500 !== 400'\n"
              "# tests 1\n# fail 1\n")

BARE = """import { test, mock } from 'node:test';
import assert from 'node:assert/strict';
test('PUT rejects', async () => {
  mock.module<typeof import('./lib/auth')>('./lib/auth.ts', {
    getSessionUserId: async () => 1,
  });
  mock.module('@/lib/db', { factory: () => ({ prisma: {} }) });
  const { PUT } = await import('./lib/x.ts');
  assert.equal(1, 1);
});
"""
STATIC = """import { test, mock } from 'node:test';
import assert from 'node:assert/strict';
import { helper } from './lib/x';
mock.module('@/lib/db', { exports: { prisma: { a: { findMany: async () => [] } } } });
test('t', async () => { const { POST } = await import('./lib/x.ts'); assert.ok(POST); });
"""
REMOCK = """import { test, mock } from 'node:test';
import assert from 'node:assert/strict';
test('a', async (t) => {
  t.mock.module('node:https', { namedExports: { request: () => 1 } });
  const m = await import('./lib/x.ts'); assert.ok(m);
});
test('b', async (t) => {
  t.mock.module('node:https', { namedExports: { request: () => 2 } });
  const m = await import('./lib/x.ts'); assert.ok(m);
});
"""
MIXED = """import { test, mock } from 'node:test';
import assert from 'node:assert/strict';
mock.module('node:https', { cache: false, moduleExports: { request: () => 1 } });
test('t', async () => { const m = await import('./lib/x.ts'); assert.ok(m); });
"""
GOOD = """import { test, mock } from 'node:test';
import assert from 'node:assert/strict';
let rows: unknown[] = [];
mock.module('@/lib/db', { exports: { prisma: { a: { findMany: async () => rows, upsert: async (x: unknown) => x } } } });
mock.module('@/lib/auth', { namedExports: { getSessionUserId: async () => 1 } });
mock.module('@/lib/flag', { defaultExport: true, cache: false });
test('t', async () => { rows = [1]; const { POST } = await import('./lib/x.ts'); assert.ok(POST); });
"""


def check(name, ok, extra=""):
    print(("ok  " if ok else "FAIL") + ": " + name + ("" if ok else "  " + str(extra)[-900:]))
    if not ok:
        FAILS.append(name)


def load(path=AUTO, name="oda_mmlint"):
    ld = SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    sys.argv = [str(path)]
    ld.exec_module(m)
    return m


def mk_wt(m, fixture, out_text=ASSERT_OUT):
    wt = Path(tempfile.mkdtemp(prefix="mmlint-")) / "wt"
    (wt / "lib").mkdir(parents=True)
    g = lambda *a: subprocess.run(["git", "-C", str(wt), *a], check=True, capture_output=True)
    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (wt / "lib" / "x.ts").write_text("export const x = 1;\n")
    g("add", "."); g("commit", "-qm", "b")
    (wt / "TASK.md").write_text("## Must contain\n- `MARK`\nOnly edit `lib/x.ts`\n")
    (wt / "verify.test.ts").write_text(fixture)
    (wt / "out.txt").write_text(out_text)
    (wt / "verify.sh").write_text(
        'grep -q MARK lib/x.ts || { echo "FAIL - no MARK"; exit 1; }\n'
        'cat out.txt; exit 1\n')
    (wt / "refimpl.py").write_text("open('lib/x.ts','a').write('// MARK\\n')\n")
    (wt / ".dispatch-harness.json").write_text(json.dumps({"target": "lib/x.ts"}))
    m.write_harness_check(wt, "ts")
    return wt


def run(wt):
    return subprocess.run([sys.executable, "auto-harness-check.py"], cwd=wt,
                          capture_output=True, text=True, timeout=120).stdout


def main():
    os.environ.setdefault("OLLAMA_DISPATCH_HOME", tempfile.mkdtemp(prefix="mmlint-home-"))
    m = load()

    o = run(mk_wt(m, BARE))
    check("bare exports object flagged with file:line",
          "verify.test.ts:4 `mock.module('./lib/auth.ts', { getSessionUserId })`" in o, o)
    check("factory option flagged", "verify.test.ts:7 `mock.module('@/lib/db', { factory })`" in o
          and "IGNORES it" in o, o)
    check("names the fix", "{ exports: { ... } }" in o, o)
    check("generic DOM/FakeXHR hint replaced", "FakeXHR" not in o, o)

    o = run(mk_wt(m, STATIC))
    check("static import of the target flagged",
          "verify.test.ts:3 static `import ... from './lib/x'`" in o and "the target" in o, o)
    check("no options false positive on `exports`", "IGNORES" not in o, o)

    o = run(mk_wt(m, REMOCK))
    check("per-test re-mock flagged", "mocks 'node:https' more than once" in o
          and "CACHED" in o, o)

    # rt-bfmr-tls-fingerprint: a valid `cache` key beside an ignored `moduleExports`
    o = run(mk_wt(m, MIXED))
    check("unknown key beside a valid one flagged",
          "verify.test.ts:3 `mock.module('node:https', { cache, moduleExports })`" in o
          and "`moduleExports` is not a mock.module option" in o, o)

    o = run(mk_wt(m, GOOD))
    check("control: correct pattern -> no lint", "mock.module lint" not in o, o)
    check("control: correct pattern (server fixture, no browser globals) gets the server/aliasing hint, NOT the browser one",
          "STATE ALIASING" in o and "suspect the FAKE ENVIRONMENT" not in o, o)

    o = run(mk_wt(m, "import { test } from 'node:test';\ntest('x', () => {});\n"))
    check("control: no mock.module -> no lint", "mock.module lint" not in o, o)

    # already-mocked hint no longer recommends per-test t.mock.module as the fix
    mock_out = ("not ok 2 - b\n  error: \"Invalid state: Cannot mock '@/lib/auth'. The module "
                "is already mocked.\"\n# tests 2\n# fail 1\n")
    o = run(mk_wt(m, GOOD, mock_out))
    check("already-mocked hint warns the target is cached", "CACHED after its" in o
          and "is restored when that test" not in o, o)

    # author prompt carries the rule for TS, not for Python
    rule = getattr(m, "MOCK_MODULE_RULE", "")
    check("MOCK_MODULE_RULE exists", "{ exports: { name: value } }" in rule, rule)
    import inspect
    src = inspect.getsource(m)
    check("author prompt appends MOCK_MODULE_RULE for jsish",
          "import_rule += MOCK_MODULE_RULE" in src, "")

    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
