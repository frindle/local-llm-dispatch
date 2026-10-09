# Claim (ef2868d21945, HIGH dashboard_chat.py:533): 'queued' was silently dropped
# from a literal collection that still contains status.
import json, os, tempfile
from unittest import mock
import dashboard_chat


def reproduce():
    base = tempfile.mkdtemp()
    os.environ["DASHBOARD_CHAT_HOME"] = base
    sid = "0" * 32
    dashboard_chat.write_session(base, sid, {"project": "p", "messages": []})
    with mock.patch.object(dashboard_chat, "enqueue_job") as enq, \
         mock.patch.object(dashboard_chat, "start_direct"):
        code, _h, body = dashboard_chat.handle(
            "POST", f"/api/chat/sessions/{sid}/messages", {},
            json.dumps({"text": "hello", "mode": "job"}).encode())
    assert code == 202, (code, body)
    assert json.loads(body)["status"] == "queued"
    assert enq.called
