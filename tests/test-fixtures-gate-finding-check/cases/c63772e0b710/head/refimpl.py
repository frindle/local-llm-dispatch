#!/usr/bin/env python3
"""Reference impl for: chat-fixes2-s1-enqueue-argv

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

OLD = '''def enqueue_job(session_id, message_index):
    """Enqueue a job via ollama-queue.py --bundle chat."""
    import subprocess
    subprocess.run(
        ["python3", str(Path.home() / "bin" / "ollama-queue.py"), "enqueue",
         session_id, str(message_index),
         "--bundle", "chat"],
        check=False,
    )'''

NEW = r'''def enqueue_job(session_id, message_index):
    """Enqueue a job via ollama-queue.py --bundle chat."""
    import subprocess
    validate_session_id(session_id)
    base_dir = get_base_dir()
    sessions_dir = Path(base_dir) / "sessions"
    session_file = sessions_dir / f"{session_id}.json"
    session = json.loads(session_file.read_text())
    messages = session["messages"]
    user_msg = messages[message_index - 1]
    user_text = user_msg["content"]
    task_file = sessions_dir / f"job-{message_index}.task.md"
    task_file.write_text(user_text)
    subprocess.run(
        ["python3", str(Path.home() / "bin" / "ollama-queue.py"), "enqueue",
         "--model", "qwen3.6-35b-a3b-vl-mtp-mxfp8",
         "--task-file", str(task_file),
         "--task-kind", "research",
         "--bundle", "chat",
         "--label", f"chat-{session_id}-{message_index}"],
        check=False,
    )'''

assert OLD in t, "refimpl anchor not found -- did the target change?"
p.write_text(t.replace(OLD, NEW, 1))
print("refimpl applied")
