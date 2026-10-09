# Review: dashboard_chat.py

## VERDICT: PASS WITH CAVEATS
_1 finding(s) the harness could not settle either way._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `dashboard_chat.py` | The new implementation crashes with a KeyError or IndexError if the session file is missing, malform |

## Uncertain — needs a human

### [HIGH] The new implementation crashes with a KeyError or IndexError if the session file is missing, malformed, or the message_index is out of bounds, whereas the previous implementation silently ignored these errors by passing invalid arguments to the subprocess.
`dashboard_chat.py`

```
validate_session_id(session_id)
+    base_dir = get_base_dir()
+    sessions_dir = Path(base_dir) / "sessions"
+    session_file = sessions_dir / f"{session_id}.json"
+    session = json.loads(session_file.read_text())
+    messages = session["messages"]
+    user_msg = messages[message_index - 1]
+    user_text = user_msg["content"]
+    task_file = sessions_dir / f"job-{message_index}.task.md"
+    task_file.write_text(user_text)
```
**How it fails:** A user calls enqueue_job with a session_id that does not exist in the sessions directory. The previous code would run `subprocess.run` with the invalid ID (likely failing silently or in the worker). The new code raises a FileNotFoundError when reading session_file, crashing the request handler and returning a 500 error to the user instead of enqueuing a job (or failing gracefully).
**Verified trigger:** Calling enqueue_job(session_id='00000000000000000000000000000000', message_index=1) when the file <DASHBOARD_CHAT_HOME>/sessions/00000000000000000000000000000000.json does not exist.

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 2 calls, 29s of model time
- Wall clock 29s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*