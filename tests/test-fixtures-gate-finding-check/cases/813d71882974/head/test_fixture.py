"""Adversarial fixture for: bg-brokers-s1-load-s1-url-else-valueerror-norm

>>> THE ONE THING THE GENERATOR CANNOT WRITE FOR YOU <<<

CASES is empty and the verify FAILS until you fill it in. That is deliberate.
A generator can emit a verify that DISCRIMINATES (fails at baseline, passes on
a fix). It cannot decide whether the verify is RELEVANT -- whether it tests the
property the task actually asked for. A benign case passes broken work.

Pick inputs that separate "did the job" from "made the test go green":
  * the exact boundary the defect is about, and one on each side of it
  * the degenerate inputs (missing key, None, empty, wrong type) that must NOT
    raise
  * at least one case that a plausible WRONG fix would fail
  * the regression half: things that already work and must keep working

Each case: (description, callable_returning_actual, expected)
"""
import json
import os
import sys
import tempfile
import importlib.util

spec = importlib.util.spec_from_file_location("target", 'broker_guard/brokers.py')
target = importlib.util.module_from_spec(spec)
# REGISTER BEFORE EXEC. Not optional: a module loaded this way has no entry in
# sys.modules, so sys.modules[cls.__module__] is None -- and on Python 3.14 (the
# Studio worker) dataclasses resolves string annotations through exactly that
# lookup. A target with `from __future__ import annotations` + @dataclass then
# dies at IMPORT with AttributeError: 'NoneType' object has no attribute
# '__dict__', so the fixture fails for a reason that has nothing to do with
# the task and the dispatch reads as a model failure.
sys.modules["target"] = target
spec.loader.exec_module(target)


def _write(doc):
    """Write a JSON document (dict/list, or raw text) to a temp file; return its path."""
    fd, p = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        if isinstance(doc, str):
            fh.write(doc)
        else:
            json.dump(doc, fh)
    return p


def _err(fn):
    """Run fn and classify the outcome so a case can assert on the error type."""
    try:
        fn()
    except ValueError:
        return "ValueError"
    except Exception as e:
        return type(e).__name__ + "(wrong)"
    return "no-error"


CASES = [
    ("padded strings are stripped and id lowercased",
     lambda: target.load_brokers(_write({"brokers": [{"id": "  ABC ",
         "name": " Example People Search ", "url": " https://example.com/ ",
         "category": " people-search "}]})),
     [{"id": "abc", "name": "Example People Search",
       "url": "https://example.com/", "category": "people-search"}]),

    ("multiple entries normalize independently; non-string values pass through",
     lambda: target.load_brokers(_write({"brokers": [
         {"id": "A1", "name": " One ", "url": " u1 ", "count": 2},
         {"id": "b2", "name": "Two", "url": "u2"}]})),
     [{"id": "a1", "name": "One", "url": "u1", "count": 2},
      {"id": "b2", "name": "Two", "url": "u2"}]),

    ("empty brokers list returns [] without raising",
     lambda: target.load_brokers(_write({"brokers": []})),
     []),

    ("top-level object lacking 'brokers' raises ValueError",
     lambda: _err(lambda: target.load_brokers(_write({"other": []}))),
     "ValueError"),

    ("bare top-level list is not a brokers document -> ValueError",
     lambda: _err(lambda: target.load_brokers(
         _write('[{"id":"a","name":"n","url":"u"}]'))),
     "ValueError"),

    ("entry missing 'name' raises ValueError",
     lambda: _err(lambda: target.load_brokers(
         _write({"brokers": [{"id": "a", "url": "u"}]}))),
     "ValueError"),

    ("entry missing both 'id' and 'url' raises ValueError",
     lambda: _err(lambda: target.load_brokers(
         _write({"brokers": [{"name": "n"}]}))),
     "ValueError"),
]


def main():
    if len(CASES) < 3:
        print("  SCAFFOLD_INCOMPLETE: {} adversarial case(s) authored, need >= 3."
              .format(len(CASES)))
        print("  A generated scaffold is not a verify. Author the cases in "
              "test_fixture.py.")
        return 1
    fails = 0
    for desc, thunk, want in CASES:
        try:
            got = thunk()
        except Exception as e:
            print("  FAIL {} -- raised {}: {}".format(desc, type(e).__name__, e))
            fails += 1
            continue
        if got != want:
            print("  FAIL {} -- got {!r}, want {!r}".format(desc, got, want))
            fails += 1
    print("  {}/{} case(s) passed".format(len(CASES) - fails, len(CASES)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
