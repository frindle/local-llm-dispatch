"""Adversarial fixture for: chat-fixes-s4b-build-request-turns

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
import sys
import importlib.util

spec = importlib.util.spec_from_file_location("target", 'dashboard_chat.py')
target = importlib.util.module_from_spec(spec)
# REGISTER BEFORE EXEC. Not optional: a module loaded this way has no entry in
# sys.modules, so sys.modules[cls.__module__] is None -- and on Python 3.14 (the
# Studio worker) dataclasses resolves string annotations through exactly that
# lookup. A target with `from __future__ import annotations` + @dataclass then
# dies at IMPORT with AttributeError: 'NoneType' object has no attribute
# '__dict__', so the fixture fails for a reason that has nothing to do with the
# task and the dispatch reads as a model failure.
sys.modules["target"] = target
spec.loader.exec_module(target)



CASES = [
    ("separate roles per turn: system + user + assistant + user",
     lambda: target.build_request("p", [
         {"role": "user", "content": "q1"},
         {"role": "assistant", "content": "a1"},
         {"role": "user", "content": "q2"},
     ], [], []),
     lambda r: (
         r["messages"][0]["role"] == "system"
         and [m["role"] for m in r["messages"][0]["content"][1:]] == ["user", "assistant", "user"]
         and all(isinstance(m["content"], str) for m in r["messages"][0]["content"][1:])
         and r["messages"][0]["content"][1]["content"] == "q1"
         and r["messages"][0]["content"][2]["content"] == "a1"
         and r["messages"][0]["content"][3]["content"] == "q2"
         and "last_user" in r
         and r["last_user"] == 3
     )),

    ("images attach to last user message as list of parts",
     lambda: target.build_request("p", [
         {"role": "user", "content": "look at this"},
         {"role": "assistant", "content": "ok"},
         {"role": "user", "content": "and this"},
     ], [], [
         {"mime": "image/png", "encoded": "SGVsbG8="},
         {"mime": "image/jpeg", "encoded": "Sm9obj0="},
     ]),
     lambda r: (
         r["messages"][0]["role"] == "system"
         and isinstance(r["messages"][0]["content"][-1]["content"], list)
         and any(p.get("type") == "text" for p in r["messages"][0]["content"][-1]["content"])
         and any(p.get("type") == "image_url" for p in r["messages"][0]["content"][-1]["content"])
         and len([p for p in r["messages"][0]["content"][-1]["content"] if p.get("type") == "image_url"]) == 2
         and all(
             "data:image/png;base64,SGVsbG8=" in p.get("image_url", {}).get("url", "")
             or "data:image/jpeg;base64,Sm9obj0=" in p.get("image_url", {}).get("url", "")
             for p in r["messages"][0]["content"][-1]["content"] if p.get("type") == "image_url"
         )
         and isinstance(r["messages"][0]["content"][-2]["content"], str)
         and r["messages"][0]["content"][-2]["content"] == "ok"
     )),

    ("no images: last user message content stays a plain string",
     lambda: target.build_request("p", [
         {"role": "user", "content": "hello"},
     ], [], []),
     lambda r: (
         isinstance(r["messages"][0]["content"][1]["content"], str)
         and r["messages"][0]["content"][1]["content"] == "hello"
         and r["last_user"] == 1
     )),

    ("empty messages list: only system message, last_user is None",
     lambda: target.build_request("p", [], [], []),
     lambda r: (
         len(r["messages"]) == 1
         and r["messages"][0]["role"] == "system"
         and r["last_user"] is None
     )),

    ("single assistant turn: last_user stays None",
     lambda: target.build_request("p", [
         {"role": "assistant", "content": "hi"},
     ], [], []),
     lambda r: (
         r["messages"][0]["content"][1]["role"] == "assistant"
         and r["messages"][0]["content"][1]["content"] == "hi"
         and r["last_user"] is None
     )),

    ("multiple images on last user: data URL format correct",
     lambda: target.build_request("p", [
         {"role": "user", "content": "check"},
     ], [], [
         {"mime": "image/webp", "encoded": "d2Vi"},
     ]),
     lambda r: (
         isinstance(r["messages"][0]["content"][-1]["content"], list)
         and any(
             "data:image/webp;base64,d2Vi" in p.get("image_url", {}).get("url", "")
             for p in r["messages"][0]["content"][-1]["content"] if p.get("type") == "image_url"
         )
     )),
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
        if not want(got):
            print("  FAIL {} -- got {!r}".format(desc, got))
            fails += 1
    print("  {}/{} case(s) passed".format(len(CASES) - fails, len(CASES)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
