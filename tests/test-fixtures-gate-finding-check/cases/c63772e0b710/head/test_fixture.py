"""Adversarial fixture for: chat-fixes2-s1-enqueue-argv

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
from pathlib import Path

spec = __import__('importlib.util').util.spec_from_file_location("target", 'dashboard_chat.py')
target = __import__('importlib.util').util.module_from_spec(spec)
sys.modules["target"] = target
spec.loader.exec_module(target)

SID = "0123456789abcdef" * 2  # 32 hex chars


def _make_session(sid, msg_text):
    """Helper: create a temp session dir and write a session file."""
    base = tempfile.mkdtemp(prefix="dctf_")
    sessions_dir = Path(base) / "sessions"
    sessions_dir.mkdir(parents=True)
    session = {
        "id": sid,
        "project": "testproj",
        "created": "2025-01-01",
        "messages": [
            {"role": "user", "content": msg_text, "files": [], "images": []},
            {"role": "assistant", "content": "", "status": "queued", "files": [], "images": []},
        ],
    }
    (sessions_dir / f"{sid}.json").write_text(json.dumps(session))
    return base



CASES = [
    ("valid session_id writes task file and calls subprocess",
     lambda: _do_enqueue(SID, 1),
     True),

    ("invalid session_id raises ValueError (not 500)",
     lambda: _do_enqueue("zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz", 1),
     "ValueError"),

    ("message_index out of range raises IndexError",
     lambda: _do_enqueue(SID, 99),
     "IndexError"),
]


def _do_enqueue(session_id, message_index):
    """Call enqueue_job and return (True, task_file_path) or raise."""
    base = _make_session(session_id, "hello world message")
    os.environ["DASHBOARD_CHAT_HOME"] = base
    try:
        target.enqueue_job(session_id, message_index)
    except Exception as e:
        return type(e).__name__
    # Check that task file was written
    sessions_dir = Path(base) / "sessions"
    task_file = sessions_dir / f"job-{message_index}.task.md"
    if task_file.exists():
        content = task_file.read_text()
        return (True, content)
    return False


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
        if isinstance(want, str):
            # Expecting an exception name
            if got != want:
                print("  FAIL {} -- got {!r}, want {!r}".format(desc, got, want))
                fails += 1
        elif got is True or (isinstance(got, tuple) and got[0] is True):
            # Happy path: task file was written
            if not (isinstance(got, tuple) and got[0] is True):
                print("  FAIL {} -- got {!r}, want {!r}".format(desc, got, want))
                fails += 1
            else:
                # Check task file content matches the message
                content = got[1]
                if content != "hello world message":
                    print("  FAIL {} -- task file content {!r}, want 'hello world message'".format(desc, content))
                    fails += 1
        else:
            print("  FAIL {} -- got {!r}, want {!r}".format(desc, got, want))
            fails += 1
    print("  {}/{} case(s) passed".format(len(CASES) - fails, len(CASES)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
