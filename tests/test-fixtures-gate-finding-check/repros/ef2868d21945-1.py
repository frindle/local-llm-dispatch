# Claim (ef2868d21945, MEDIUM/uncertain): the actual execution of ollama-queue.py
# was removed; if enqueue_job lacks equivalent logic, the job is never queued.
from unittest import mock
import dashboard_chat


def reproduce():
    with mock.patch("subprocess.run") as run:
        dashboard_chat.enqueue_job("0" * 32, 1)
    assert run.called, "enqueue_job never invoked the queue"
    argv = run.call_args[0][0]
    assert any(str(a).endswith("ollama-queue.py") for a in argv) and "enqueue" in argv
