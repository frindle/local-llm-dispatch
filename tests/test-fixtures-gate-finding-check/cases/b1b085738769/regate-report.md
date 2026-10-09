# Review: dashboard_chat.py

## VERDICT: PASS WITH CAVEATS
_1 finding(s) the harness could not settle either way._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `dashboard_chat.py` | The collect_job_results function modifies the session object in memory but fails to persist the upda |

## Uncertain — needs a human

### [HIGH] The collect_job_results function modifies the session object in memory but fails to persist the updated messages to disk, causing the state change to be lost when the session is written.
`dashboard_chat.py`

```
job_dir = Path(base_dir) / "jobs" / f"{session_id}-{i}"
            answer_file = job_dir / "ANSWER.md"
            if answer_file.exists():
                text = answer_file.read_text()
                if text:
                    msg["content"] = text
                    msg["status"] = "done"
                    updated += 1
```
**How it fails:** 1. A session has messages [user_0, assistant_0]. 2. A job is started for assistant_0 (index 1), writing to <base_dir>/jobs/<id>-1/ANSWER.md. 3. A new message is inserted at index 0, shifting assistant_0 to index 2. 4. The job completes and writes ANSW.md to <base_dir>/jobs/<id>-1/. 5. The user requests the session. 6. collect_job_results looks for <base_dir>/jobs/<id>-2/ANSWER.md. 7. The file is not found. 8. assistant_0 remains in 'queued' status with empty content, despite the job having completed.
**Verified trigger:** Session with session_id='a'*32. Initial messages: [{'role': 'user', 'content': 'hi', 'status': 'done'}, {'role': 'assistant', 'content': '', 'status': 'queued'}]. Job started for assistant at index 1. A new message inserted at index 0, shifting assistant to index 2. Job writes ANSWER.md to <base_dir>/jobs/<session_id>-1/. collect_job_results looks for <base_dir>/jobs/<session_id>-2/ANSWER.md. File not found. Assistant message remains 'queued'.

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 2 calls, 56s of model time
- Wall clock 56s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*