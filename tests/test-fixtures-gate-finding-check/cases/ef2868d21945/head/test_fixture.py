"""Adversarial fixture for: chat-fixes-s6c-mode-routing

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
import json
import os
import importlib.util
import tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("target", 'dashboard_chat.py')
target = importlib.util.module_from_spec(spec)
sys.modules["target"] = target
spec.loader.exec_module(target)

import json
import os
import shutil
import tempfile
import threading

SID = "0123456789abcdef" * 2
_HOME = tempfile.mkdtemp(prefix="s6c")


def _fresh(prior=0):
    shutil.rmtree(_HOME, ignore_errors=True)
    os.makedirs(_HOME)
    os.environ["DASHBOARD_CHAT_HOME"] = _HOME
    msgs = []
    for i in range(prior):
        msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": "m%d" % i, "files": [], "images": [], "ts": "", "status": "completed"})
    target.write_session(_HOME, SID, {"project": "p", "created": "c", "messages": msgs})


def _post(body, prior=0):
    """POST a message with enqueue_job/start_direct replaced; return (status, parsed body, calls, session)."""
    _fresh(prior)
    calls = []
    oe, od = target.enqueue_job, target.start_direct
    target.enqueue_job = lambda sid, idx: calls.append(("enqueue", sid, idx))
    target.start_direct = lambda sid, idx: calls.append(("direct", sid, idx))
    try:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        st, hd, rb = target.handle("POST", "/api/chat/sessions/%s/messages" % SID, {}, raw)
    finally:
        target.enqueue_job, target.start_direct = oe, od
    try:
        parsed = json.loads(rb)
    except Exception:
        parsed = None
    return st, parsed, calls, target.read_session(_HOME, SID)


def c_chat():
    st, b, calls, s = _post({"text": "hello", "mode": "chat"})
    return st, b, calls
def c_job():
    st, b, calls, s = _post({"text": "hello", "mode": "job"})
    return st, b, calls
def c_auto_job():
    st, b, calls, s = _post({"text": "please fix the bug in parser"})
    return st, b["mode"], calls
def c_auto_chat():
    st, b, calls, s = _post({"text": "what does this function do"})
    return st, b["mode"], calls
def c_override_chat():
    return _post({"text": "please fix the bug", "mode": "chat"})[1]["mode"]
def c_override_job():
    return _post({"text": "hello there", "mode": "job"})[1]["mode"]
def c_stored_chat():
    s = _post({"text": "hi", "mode": "chat", "files": ["a.py"]})[3]["messages"]
    u, a = s
    return (u["role"], u["status"], u["files"], "mode" in u), (a["role"], a["status"], a["mode"], a["content"])
def c_stored_job():
    a = _post({"text": "hi", "mode": "job"})[3]["messages"][1]
    return a["status"], a["mode"]
def c_index_prior():
    st, b, calls, s = _post({"text": "x", "mode": "job"}, prior=2)
    return b["message_index"], calls, len(s["messages"]), s["messages"][3]["role"]
def c_bad_mode():
    out = []
    for m in ("bogus", "", "CHAT", 5, ["chat"], True):
        st, b, calls, s = _post({"text": "hello", "mode": m})
        out.append((st, isinstance(b, dict) and "error" in b, calls, len(s["messages"])))
    return out
def c_null_mode_is_auto():
    st, b, calls, s = _post({"text": "please fix this", "mode": None})
    return st, b["mode"]
def c_exactly_one():
    return [len(_post({"text": "t", "mode": m})[2]) for m in ("chat", "job")]
def c_direct_thread():
    _fresh()
    seen = {}
    ev = threading.Event()
    oj = target.job
    def fake(sid, idx):
        seen["args"] = (sid, idx)
        seen["daemon"] = threading.current_thread().daemon
        seen["main"] = threading.current_thread() is threading.main_thread()
        ev.set()
    target.job = fake
    try:
        r = target.start_direct(SID, 1)
        ok = ev.wait(5)
    finally:
        target.job = oj
    return r, ok, seen.get("args"), seen.get("daemon"), seen.get("main")
def c_enqueue_job_exists():
    import inspect
    return callable(target.enqueue_job), list(inspect.signature(target.enqueue_job).parameters), list(inspect.signature(target.start_direct).parameters)
def c_write_mode():
    _fresh()
    target.write_session(_HOME, SID, {"project": "p", "created": "c", "messages": [
        {"role": "user", "content": "u"}, {"role": "assistant", "content": "", "status": "running", "mode": "chat"}]})
    m = target.read_session(_HOME, SID)["messages"]
    return sorted(m[0]), m[1]["mode"]
def c_enqueue_argv():
    import subprocess
    from pathlib import Path
    seen = []
    orig = subprocess.run
    subprocess.run = lambda *a, **k: seen.append((a, k))
    try:
        r = target.enqueue_job(SID, 1)
    finally:
        subprocess.run = orig
    argv = seen[0][0][0] if seen and seen[0][0] else None
    return r, len(seen), argv == ["python3", str(Path.home() / "bin" / "ollama-queue.py"), "enqueue", "--bundle", "chat"], seen[0][1] if seen else None


def c_nested_gone():
    src = open("dashboard_chat.py").read()
    return "def enqueue(" in src
def c_regress_get():
    _fresh(2)
    st, hd, rb = target.handle("GET", "/api/chat/sessions/%s" % SID, {}, b"")
    return st, len(json.loads(rb)["messages"])
def c_regress_body_validation():
    return [_post(b)[0] for b in (b"[]", {"text": 5}, b"not json")]


CASES = [
    ("mode chat: 202, running, direct called once, enqueue not", c_chat,
     (202, {"mode": "chat", "message_index": 1, "status": "running"}, [("direct", SID, 1)])),
    ("mode job: 202, queued, enqueue called once, direct not", c_job,
     (202, {"mode": "job", "message_index": 1, "status": "queued"}, [("enqueue", SID, 1)])),
    ("absent mode + 'please fix the bug' -> job", c_auto_job, (202, "job", [("enqueue", SID, 1)])),
    ("absent mode + question -> chat", c_auto_chat, (202, "chat", [("direct", SID, 1)])),
    ("explicit chat overrides the classifier", c_override_chat, "chat"),
    ("explicit job overrides the classifier", c_override_job, "job"),
    ("stored chat turn: user unchanged, assistant running+mode", c_stored_chat,
     (("user", "completed", ["a.py"], False), ("assistant", "running", "chat", ""))),
    ("stored job turn: queued + mode job", c_stored_job, ("queued", "job")),
    ("message_index is the assistant index after prior turns", c_index_prior, (3, [("enqueue", SID, 3)], 4, "assistant")),
    ("invalid mode values -> 400 {error}, nothing appended, nothing called", c_bad_mode,
     [(400, True, [], 0)] * 6),
    ("mode null falls back to the classifier", c_null_mode_is_auto, (202, "job")),
    ("exactly one dispatch call per message", c_exactly_one, [1, 1]),
    ("start_direct runs job(session_id, idx) on a daemon thread, returns None", c_direct_thread,
     (None, True, (SID, 1), True, False)),
    ("enqueue_job/start_direct are module-level with (session_id, message_index)", c_enqueue_job_exists,
     (True, ["session_id", "message_index"], ["session_id", "message_index"])),
    ("enqueue_job shells out to ollama-queue.py enqueue --bundle chat (argv list, check=False)", c_enqueue_argv,
     (None, 1, True, {"check": False})),
    ("write_session persists mode only when present", c_write_mode,
     (["content", "files", "images", "role", "status", "ts"], "chat")),
    ("nested enqueue removed from handle", c_nested_gone, False),
    ("GET session still works", c_regress_get, (200, 2)),
    ("body validation still 400", c_regress_body_validation, [400, 400, 400]),
]


def main():
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
