# Review: dashboard_chat.py

## VERDICT: PASS WITH CAVEATS
_1 finding(s) the harness could not settle either way._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `dashboard_chat.py` | The `enqueue` function is defined inside the `handle` function and ignores the `session_id` and `mes |

## Uncertain — needs a human

### [HIGH] The `enqueue` function is defined inside the `handle` function and ignores the `session_id` and `message_index` arguments, passing them to the subprocess call incorrectly (or not at all).
`dashboard_chat.py`

```
def enqueue(session_id, message_index):
                    import subprocess as _sub
                    _sub.run(
                        ["python3", str(Path.home() / "bin" / "ollama-queue.py"), "enqueue",
                         "--bundle", "chat"],
                        check=False,
                    )

                enqueue(session_id, message_index)
```
**How it fails:** The `enqueue` function is called with `session_id` and `message_index`, but the `subprocess.run` call does not include these arguments. The queue script `ollama-queue.py` receives only `enqueue --bundle chat`. If the queue script requires the session ID and message index to know where to store the result, the message will be lost or stored incorrectly, leading to a broken chat flow where the assistant response is never written back to the correct session/message slot.

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 2 calls, 19s of model time
- Wall clock 19s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*