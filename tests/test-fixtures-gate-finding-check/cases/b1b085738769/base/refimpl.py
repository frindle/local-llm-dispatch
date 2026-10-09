#!/usr/bin/env python3
"""Reference impl for: chat-fixes3-s2-collect-results

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES the
spec (a refimpl that goes green while a "Must contain" literal is absent means
the verify is benign).

Write the SIMPLEST change that makes the verify pass. It doubles as your review
reference when the model's diff comes back.
"""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'dashboard_chat.py'
t = p.read_text()

# 1) Add collect_job_results function before the handle() function
COLLECT_FUNC = '''
def collect_job_results(base_dir, session_id):
    """Load the session and, for every message whose role is assistant and status
    is queued, look for the file ANSWER.md in <base_dir>/jobs/<session_id>-<message_index>/;
    when that file exists and is non-empty it sets that message's content to the
    file text and its status to done, and writes the session once if anything
    changed. It returns the number of messages updated."""
    try:
        session = read_session(base_dir, session_id)
    except (ValueError, FileNotFoundError):
        return 0
    messages = session.get("messages", [])
    updated = 0
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant" and msg.get("status") == "queued":
            job_dir = Path(base_dir) / "jobs" / f"{session_id}-{i}"
            answer_file = job_dir / "ANSWER.md"
            if answer_file.exists():
                text = answer_file.read_text()
                if text.strip():
                    msg["content"] = text
                    msg["status"] = "done"
                    updated += 1
    if updated > 0:
        write_session(base_dir, session_id, session)
    return updated

'''

# 2) Modify the GET /api/chat/sessions/<id> branch to call collect_job_results
OLD_GET = """            # GET /api/chat/sessions/<id>
            if method == "GET":
                try:
                    session = read_session(base_dir, session_id)
                    return (200, {"Content-Type": "application/json"}, json.dumps(session).encode())
                except (ValueError, FileNotFoundError) as e:
                    return (400, {"Content-Type": "application/json"}, json.dumps({"error": str(e)}).encode())"""

NEW_GET = """            # GET /api/chat/sessions/<id>
            if method == "GET":
                try:
                    collect_job_results(base_dir, session_id)
                    session = read_session(base_dir, session_id)
                    return (200, {"Content-Type": "application/json"}, json.dumps(session).encode())
                except (ValueError, FileNotFoundError) as e:
                    return (400, {"Content-Type": "application/json"}, json.dumps({"error": str(e)}).encode())"""

assert OLD_GET in t, "refimpl anchor not found -- did the target change?"
t = t.replace(OLD_GET, NEW_GET, 1)

# Insert collect_job_results before the handle() function
assert "def handle(" in t, "handle() function not found"
t = t.replace("def handle(", COLLECT_FUNC + "def handle(")

p.write_text(t)
print("refimpl applied")
