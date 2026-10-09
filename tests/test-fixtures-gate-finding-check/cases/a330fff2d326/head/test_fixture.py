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



def _roles(r):
    return [m["role"] for m in r["messages"]]


def _img(mime, enc):
    return {"mime": mime, "encoded": enc}


T3 = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}, {"role": "user", "content": "q2"}]


def _parts(m):
    return m["content"] if isinstance(m["content"], list) else None


CASES = [
    ("turns are top-level messages with real roles",
     lambda: _roles(target.build_request("p", T3, [], [])), ["system", "user", "assistant", "user"]),
    ("turn contents are plain strings, in order",
     lambda: [m["content"] for m in target.build_request("p", T3, [], [])["messages"][1:]], ["q1", "a1", "q2"]),
    ("system message stays first and keeps its text parts",
     lambda: (lambda r: (r["messages"][0]["role"], isinstance(r["messages"][0]["content"], list),
                         any("p" in x.get("text", "") for x in r["messages"][0]["content"])))(
         target.build_request("p", T3, [], [])), ("system", True, True)),
    ("turns are NOT nested inside the system message",
     lambda: [x for x in target.build_request("p", T3, [], [])["messages"][0]["content"] if "role" in x], []),
    ("no history: only the system message",
     lambda: _roles(target.build_request("p", [], [], [])), ["system"]),
    ("files key still passed through unchanged",
     lambda: target.build_request("p", T3, ["x.py"], [])["files"], ["x.py"]),
    ("image attaches to LAST user message as [text, image_url] parts",
     lambda: (lambda m: ([x["type"] for x in _parts(m)], _parts(m)[0]["text"], _parts(m)[1]["image_url"]["url"]))(
         target.build_request("p", T3, [], [_img("image/png", "SGVsbG8=")])["messages"][3]),
     (["text", "image_url"], "q2", "data:image/png;base64,SGVsbG8=")),
    ("two images give two image_url parts in order, mime respected",
     lambda: [x["image_url"]["url"] for x in _parts(target.build_request(
         "p", T3, [], [_img("image/png", "AAAA"), _img("image/webp", "d2Vi")])["messages"][3])[1:]],
     ["data:image/png;base64,AAAA", "data:image/webp;base64,d2Vi"]),
    ("earlier user message stays a plain string when images exist",
     lambda: target.build_request("p", T3, [], [_img("image/png", "AAAA")])["messages"][1]["content"], "q1"),
    ("images go to the last USER turn even when an assistant turn is last",
     lambda: (lambda r: (isinstance(r["messages"][1]["content"], list), r["messages"][2]["content"]))(
         target.build_request("p", T3[:2], [], [_img("image/png", "AAAA")])), (True, "a1")),
    ("no images: last user content stays a plain string",
     lambda: target.build_request("p", T3, [], [])["messages"][3]["content"], "q2"),
    ("images with empty history do not raise",
     lambda: _roles(target.build_request("p", [], [], [_img("image/png", "AAAA")])), ["system"]),
    ("images with only assistant turns do not raise or alter them",
     lambda: target.build_request("p", T3[1:2], [], [_img("image/png", "AAAA")])["messages"][1]["content"], "a1"),
    ("missing role defaults to user",
     lambda: _roles(target.build_request("p", [{"content": "x"}], [], [])), ["system", "user"]),
    ("input messages are not mutated",
     lambda: (lambda msgs: (target.build_request("p", msgs, [], [_img("image/png", "AAAA")]), msgs)[1])(
         [{"role": "user", "content": "q"}]), [{"role": "user", "content": "q"}]),
]


def main():
    if len(CASES) < 3:
        print("  SCAFFOLD_INCOMPLETE")
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
