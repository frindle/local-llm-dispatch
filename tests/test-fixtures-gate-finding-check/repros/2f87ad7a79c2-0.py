# Claim (2f87ad7a79c2, MEDIUM/uncertain): the enqueue in handle ignores session_id
# and message_index -- the queue is never told which job to run.
import json, os, tempfile
from unittest import mock
import dashboard_chat


def reproduce():
    base = tempfile.mkdtemp()
    os.environ["DASHBOARD_CHAT_HOME"] = base
    sid = "0" * 32
    dashboard_chat.write_session(base, sid, {"project": "p", "messages": []})
    with mock.patch("subprocess.run") as run:
        code, _h, body = dashboard_chat.handle(
            "POST", f"/api/chat/sessions/{sid}/messages", {},
            json.dumps({"text": "please fix the bug"}).encode())
    assert code == 202, (code, body)
    assert run.called
    argv = [str(a) for a in run.call_args[0][0]]
    assert any(sid in a for a in argv), f"session id not passed to the queue: {argv}"
