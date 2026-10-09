# Claim (c63772e0b710, MEDIUM/uncertain): enqueue_job crashes (KeyError/IndexError/
# FileNotFoundError) when the session file is missing.
import os, tempfile
from unittest import mock
import dashboard_chat


def reproduce():
    os.environ["DASHBOARD_CHAT_HOME"] = tempfile.mkdtemp()
    with mock.patch("subprocess.run"):
        dashboard_chat.enqueue_job("0" * 32, 1)     # no session file exists
