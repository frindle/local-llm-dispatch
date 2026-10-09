"""Adversarial fixture for: chat-fixes3-s2-collect-results

Integration tests for collect_job_results via the handle() route.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import importlib.util
spec = importlib.util.spec_from_file_location("dc", 'dashboard_chat.py')
dc = importlib.util.module_from_spec(spec)
sys.modules["dc"] = spec.loader.exec_module(dc)

SID = "0123456789abcdef" * 2  # 32 hex chars


def _write_session(base_dir, session_id, messages):
    sessions_dir = Path(base_dir) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session = {
        "id": session_id,
        "project": "test",
        "created": "2024-01-01",
        "messages": messages,
    }
    (sessions_dir / f"{session_id}.json").write_text(json.dumps(session))


def _write_answer(base_dir, session_id, message_index, text):
    job_dir = Path(base_dir) / "jobs" / f"{session_id}-{message_index}"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "ANSWER.md").write_text(text)


def _setup(base, messages, answers=None):
    os.environ["DASHBOARD_CHAT_HOME"] = base
    _write_session(base, SID, messages)
    if answers:
        for idx, text in answers:
            _write_answer(base, SID, idx, text)


def _get_session(base):
    status, headers, body = dc.handle("GET", f"/api/chat/sessions/{SID}", {}, b"")
    return status, json.loads(body)


def case_happy_path():
    """Queued assistant message + non-empty ANSWER.md -> updated to done."""
    base = tempfile.mkdtemp()
    messages = [
        {"role": "user", "content": "hello", "status": "completed"},
        {"role": "assistant", "content": "", "status": "queued"},
    ]
    _setup(base, messages, [(1, "Job result text.")])
    status, session = _get_session(base)
    assert status == 200, f"expected 200, got {status}"
    assert session["messages"][1]["status"] == "done"
    assert session["messages"][1]["content"] == "Job result text."
    return True


def case_missing_answer():
    """Queued assistant message without ANSWER.md -> unchanged."""
    base = tempfile.mkdtemp()
    messages = [
        {"role": "user", "content": "hello", "status": "completed"},
        {"role": "assistant", "content": "", "status": "queued"},
    ]
    _setup(base, messages)
    status, session = _get_session(base)
    assert status == 200
    assert session["messages"][1]["status"] == "queued"
    return True


def case_empty_answer():
    """Queued assistant message + empty ANSWER.md -> unchanged (empty file)."""
    base = tempfile.mkdtemp()
    messages = [
        {"role": "user", "content": "hello", "status": "completed"},
        {"role": "assistant", "content": "", "status": "queued"},
    ]
    _setup(base, messages, [(1, "")])
    status, session = _get_session(base)
    assert status == 200
    assert session["messages"][1]["status"] == "queued"
    return True


def case_invalid_session_id():
    """GET with invalid session_id -> 400."""
    base = tempfile.mkdtemp()
    os.environ["DASHBOARD_CHAT_HOME"] = base
    status, headers, body = dc.handle("GET", "/api/chat/sessions/invalid_id", {}, b"")
    assert status == 400
    return True


def case_nonexistent_session():
    """GET with non-existent session_id -> 400."""
    base = tempfile.mkdtemp()
    os.environ["DASHBOARD_CHAT_HOME"] = base
    status, headers, body = dc.handle("GET", f"/api/chat/sessions/{SID}", {}, b"")
    assert status == 400
    return True


def case_multiple_messages_partial():
    """Multiple queued assistant messages, some with ANSWER.md -> only updated ones change."""
    base = tempfile.mkdtemp()
    messages = [
        {"role": "user", "content": "hello", "status": "completed"},
        {"role": "assistant", "content": "", "status": "queued"},
        {"role": "user", "content": "world", "status": "completed"},
        {"role": "assistant", "content": "", "status": "queued"},
    ]
    _setup(base, messages, [(1, "Answer for msg 1.")])
    status, session = _get_session(base)
    assert status == 200
    assert session["messages"][1]["status"] == "done"
    assert session["messages"][1]["content"] == "Answer for msg 1."
    assert session["messages"][3]["status"] == "queued"
    return True


def case_adversarial_wrong_index():
    """ANSWER.md in wrong job directory -> message NOT updated."""
    base = tempfile.mkdtemp()
    messages = [
        {"role": "user", "content": "hello", "status": "completed"},
        {"role": "assistant", "content": "", "status": "queued"},
    ]
    _setup(base, messages)
    job_dir = Path(base) / "jobs" / f"{SID}-99"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "ANSWER.md").write_text("Wrong directory answer.")
    status, session = _get_session(base)
    assert status == 200
    assert session["messages"][1]["status"] == "queued"
    return True


def case_assistant_not_queued():
    """Assistant message with status 'done' -> not touched by collect_job_results."""
    base = tempfile.mkdtemp()
    messages = [
        {"role": "user", "content": "hello", "status": "completed"},
        {"role": "assistant", "content": "already done", "status": "done"},
    ]
    _setup(base, messages, [(1, "New answer.")])
    status, session = _get_session(base)
    assert status == 200
    assert session["messages"][1]["status"] == "done"
    assert session["messages"][1]["content"] == "already done"
    return True


def case_only_user_messages():
    """Session with only user messages -> no change."""
    base = tempfile.mkdtemp()
    messages = [
        {"role": "user", "content": "hello", "status": "completed"},
        {"role": "user", "content": "world", "status": "completed"},
    ]
    _setup(base, messages)
    status, session = _get_session(base)
    assert status == 200
    assert len(session["messages"]) == 2
    return True


def case_session_with_done_assistant_and_queued():
    """Mixed: done assistant + queued assistant -> only queued one updated."""
    base = tempfile.mkdtemp()
    messages = [
        {"role": "user", "content": "hello", "status": "completed"},
        {"role": "assistant", "content": "already done", "status": "done"},
        {"role": "user", "content": "world", "status": "completed"},
        {"role": "assistant", "content": "", "status": "queued"},
    ]
    _setup(base, messages, [(3, "Answer for queued.")])
    status, session = _get_session(base)
    assert status == 200
    assert session["messages"][1]["content"] == "already done"
    assert session["messages"][3]["status"] == "done"
    assert session["messages"][3]["content"] == "Answer for queued."
    return True


def _q2(base, answer_for=None):
    os.environ["DASHBOARD_CHAT_HOME"] = base
    messages = [
        {"role": "user", "content": "hello", "status": "completed"},
        {"role": "assistant", "content": "", "status": "queued"},
        {"role": "user", "content": "more", "status": "completed"},
        {"role": "assistant", "content": "", "status": "running"},
    ]
    _write_session(base, SID, messages)
    if answer_for is not None:
        _write_answer(base, SID, answer_for[0], answer_for[1])


def _disk(base, sid=SID):
    return json.loads((Path(base) / "sessions" / f"{sid}.json").read_text())


def case_direct_return_counts():
    """collect_job_results returns the number updated; a second call returns 0."""
    base = tempfile.mkdtemp()
    _q2(base, (1, "R1"))
    n1 = dc.collect_job_results(base, SID)
    n2 = dc.collect_job_results(base, SID)
    assert n1 == 1, f"first call returned {n1!r}"
    assert n2 == 0, f"second call returned {n2!r}"
    return True


def case_direct_nothing_to_do_returns_zero():
    base = tempfile.mkdtemp()
    _q2(base)
    n = dc.collect_job_results(base, SID)
    assert n == 0, f"returned {n!r}"
    assert _disk(base)["messages"][1]["status"] == "queued"
    return True


def case_persisted_on_disk():
    """The updated message is written back to the session file, not only returned by GET."""
    base = tempfile.mkdtemp()
    _q2(base, (1, "On disk text"))
    dc.collect_job_results(base, SID)
    m = _disk(base)["messages"][1]
    assert m["status"] == "done" and m["content"] == "On disk text", m
    return True


def case_running_chat_message_untouched():
    """A running (chat-mode) assistant message is never collected even if ANSWER.md exists."""
    base = tempfile.mkdtemp()
    _q2(base, (3, "Should be ignored"))
    dc.collect_job_results(base, SID)
    m = _disk(base)["messages"][3]
    assert m["status"] == "running" and m["content"] == "", m
    return True


def case_other_session_answer_never_read():
    """An ANSWER.md in another session's job dir is never used for this session."""
    base = tempfile.mkdtemp()
    _q2(base)
    other = "fedcba9876543210" * 2
    _write_answer(base, other, 1, "other session's answer")
    n = dc.collect_job_results(base, SID)
    assert n == 0, n
    assert _disk(base)["messages"][1]["status"] == "queued"
    return True


def case_missing_session_returns_zero():
    """A session that does not exist (or has an invalid id) -> returns exactly 0, no raise."""
    base = tempfile.mkdtemp()
    os.environ["DASHBOARD_CHAT_HOME"] = base
    r1 = dc.collect_job_results(base, SID)
    r2 = dc.collect_job_results(base, "not-a-valid-id")
    assert r1 == 0 and r1 is not None and type(r1) is int, repr(r1)
    assert r2 == 0 and type(r2) is int, repr(r2)
    return True


def _count_writes(base, fn):
    real = dc.write_session
    calls = []
    def spy(*a, **k):
        calls.append(1)
        return real(*a, **k)
    dc.write_session = spy
    try:
        r = fn()
    finally:
        dc.write_session = real
    return r, len(calls)


def case_no_write_when_nothing_changed():
    """The session is NOT rewritten when no message was updated."""
    base = tempfile.mkdtemp()
    _q2(base)
    r, w = _count_writes(base, lambda: dc.collect_job_results(base, SID))
    assert r == 0 and w == 0, (r, w)
    return True


def case_single_write_for_two_updates():
    """Two finished answers -> both updated, session written exactly once."""
    base = tempfile.mkdtemp()
    os.environ["DASHBOARD_CHAT_HOME"] = base
    messages = [
        {"role": "user", "content": "a", "status": "completed"},
        {"role": "assistant", "content": "", "status": "queued"},
        {"role": "user", "content": "b", "status": "completed"},
        {"role": "assistant", "content": "", "status": "queued"},
    ]
    _write_session(base, SID, messages)
    _write_answer(base, SID, 1, "A1")
    _write_answer(base, SID, 3, "A3")
    r, w = _count_writes(base, lambda: dc.collect_job_results(base, SID))
    assert r == 2 and w == 1, (r, w)
    d = _disk(base)["messages"]
    assert d[1]["content"] == "A1" and d[3]["content"] == "A3"
    return True


def main():
    cases = [
        ("happy_path", case_happy_path),
        ("missing_answer", case_missing_answer),
        ("empty_answer", case_empty_answer),
        ("invalid_session_id", case_invalid_session_id),
        ("nonexistent_session", case_nonexistent_session),
        ("multiple_messages_partial", case_multiple_messages_partial),
        ("adversarial_wrong_index", case_adversarial_wrong_index),
        ("assistant_not_queued", case_assistant_not_queued),
        ("only_user_messages", case_only_user_messages),
        ("session_with_done_assistant_and_queued", case_session_with_done_assistant_and_queued),
        ("missing_session_returns_zero", case_missing_session_returns_zero),
        ("no_write_when_nothing_changed", case_no_write_when_nothing_changed),
        ("single_write_for_two_updates", case_single_write_for_two_updates),
        ("direct_return_counts", case_direct_return_counts),
        ("direct_nothing_to_do_returns_zero", case_direct_nothing_to_do_returns_zero),
        ("persisted_on_disk", case_persisted_on_disk),
        ("running_chat_message_untouched", case_running_chat_message_untouched),
        ("other_session_answer_never_read", case_other_session_answer_never_read),
    ]

    if len(cases) < 3:
        print("  SCAFFOLD_INCOMPLETE: {} adversarial case(s) authored, need >= 3."
              .format(len(cases)))
        return 1
    fails = 0
    for desc, thunk in cases:
        try:
            got = thunk()
        except Exception as e:
            print("  FAIL {} -- raised {}: {}".format(desc, type(e).__name__, e))
            fails += 1
            continue
        if not got:
            print("  FAIL {} -- returned False".format(desc))
            fails += 1
    print("  {}/{} case(s) passed".format(len(cases) - fails, len(cases)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
