# Claim (b1b085738769, MEDIUM/uncertain): collect_job_results modifies the session
# in memory but fails to persist the update.
import os, tempfile
from pathlib import Path
import dashboard_chat


def reproduce():
    base = tempfile.mkdtemp()
    sid = "0" * 32
    dashboard_chat.write_session(base, sid, {"project": "p", "messages": [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "status": "queued"}]})
    jd = Path(base) / "jobs" / f"{sid}-1"
    jd.mkdir(parents=True)
    (jd / "ANSWER.md").write_text("the answer")
    assert dashboard_chat.collect_job_results(base, sid) == 1
    again = dashboard_chat.read_session(base, sid)
    assert again["messages"][1]["content"] == "the answer"
    assert again["messages"][1]["status"] == "done"
