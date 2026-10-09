# Review: dashboard_chat.py

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `dashboard_chat.py:284` | The code mutates the input `messages` list by replacing the content of the last user turn with a lis |

## Defects

### [HIGH] The code mutates the input `messages` list by replacing the content of the last user turn with a list of parts, violating the requirement not to mutate the input.
`dashboard_chat.py:284`

```
turns[last_user]["content"] = parts
```
**How it fails:** The caller passes a list of message dicts to `build_request`. After the call, the original message dict at index `last_user` in the caller's `messages` list now has its `content` field replaced with a list of part objects (text and image_url) instead of the original string. This corrupts the caller's state if they reuse the `messages` list or inspect it after the call.
**Verified trigger:** Call build_request with messages=[{'role': 'user', 'content': 'hello'}] and images=[{'mime': 'image/png', 'encoded': 'abc'}]. After the call, messages[0]['content'] is [{'type': 'text', 'text': 'hello'}, {'type': 'image_url', ...}] instead of 'hello'.

## Rejected by verification

Raised during review, then knocked down when checked against the code by a pass that did not write them. Listed for audit, not for action.

- ~~The deletion removes the logic that appended prior conversation turns as plain text strings to the system message's content list. The new code correctly separates turns into top-level messages, but the deletion of this specific line removes the old (buggy) behavior that the pre-gate findings were complaining about. However, looking at the *intent* of the audit: we are auditing DELETIONS. The deleted line was part of the OLD, BUGGY implementation. The NEW implementation replaces it with a loop that creates separate message objects. The deletion is SAFE because the new code `turns.append({"role": role, "content": msg.get("content", "")})` performs the equivalent (but correct) work of preserving the turn's role and content, just in a different structure. The pre-gate finding was about the OLD code doing this. The deletion of the old code is required for the fix. Is anything still doing it? Yes, the new loop `turns.append(...)` does the work of including the turn's content and role, but correctly as a separate message. The deletion is safe.~~ — The finding claims that the deletion of the old line is a defect or requires specific justification, but correctly identifies that the new code replaces it. The 'finding' itself is not a defect in the new code; it is an 

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 98,304, think=off — 4 calls, 20s of model time
- Wall clock 20s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario, 1 rejected by verification

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*