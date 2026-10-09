# Review: dashboard_chat.py

## VERDICT: FAIL
_a high-severity defect survived adversarial verification._

| # | severity | where | what |
|---|---|---|---|
| 1 | HIGH | `dashboard_chat.py` | The deletion removes the logic that processed prior conversation turns into the system message's con |
| 2 | HIGH | `dashboard_chat.py:295` | The return value of build_request now includes a 'last_user' key which violates the contract that it |

## Defects

### [HIGH] The deletion removes the logic that processed prior conversation turns into the system message's content list. The replacement code incorrectly appends individual message objects (with 'role' and 'content' keys) to the `content` list, which is then assigned to the system message's `content` field. This violates the API contract where the system message's `content` should be a string or a list of content parts (text/image), not a list of full message objects.
`dashboard_chat.py`

```
for msg in messages:
        content.append({"type": "text", "text": f"{msg.get('role', 'user')}: {msg.get('content', '')}"})
```
**How it fails:** When `build_request` is called with multiple turns (e.g., a user message followed by an assistant message), the resulting `messages` list will contain a single system message whose `content` is a list containing dictionaries like `{'role': 'user', 'content': 'hello'}`. This structure is invalid for most LLM APIs expecting the system message content to be plain text or multimodal parts, causing the API call to fail with a validation error or produce nonsensical model behavior.
**Verified trigger:** Calling build_request with messages=[{'role': 'user', 'content': 'hello'}] and no images. The returned dict has 'messages' containing one item: {'role': 'system', 'content': [{'role': 'user', 'content': 'hello'}]}. The 'content' field of the system message is a list containing a dictionary with 'role' and 'content' keys, rather than a list of content parts (text/image) or a string.

## Uncertain — needs a human

### [HIGH] The return value of build_request now includes a 'last_user' key which violates the contract that it returns a dict with keys 'messages' and 'files'.
`dashboard_chat.py:295`

```
return {"messages": [{"role": "system", "content": content}], "files": files, "last_user": last_user}
```
**How it fails:** The caller (e.g., `call_model` or `job`) expects a dictionary with only 'messages' and 'files' keys. Accessing `request['last_user']` will succeed, but iterating over keys or checking `request.keys() == {'messages', 'files'}` will fail. If the caller blindly unpacks or validates the shape, it will crash or reject the request. Specifically, if `call_model` iterates over `request.items()` to build an API payload, it will send an unexpected 'last_user' field to the model provider, causing a schema validation error or unexpected behavior.
**Verified trigger:** The function `build_request` returns a dictionary containing the key 'last_user' with an integer value (or None). Any caller that strictly validates the keys of the returned dictionary against the set {'messages', 'files'} will raise a KeyError or assertion error. For example, if `call_model` executes `assert set(request.keys()) == {'messages', 'files'}`, it will fail because `request.keys()` is `{'messages', 'files', 'last_user'}.

## How this was produced

- Model `qwen3.6-35b-a3b-vl-mtp-mxfp8`, num_ctx 65,536, think=off — 4 calls, 44s of model time
- Wall clock 44s
- Filtered before you saw them: 0 finding(s) quoting code that is not in the diff, 0 with no concrete failure scenario

*Every quote above was checked as a literal substring of the real input before the finding was allowed to appear, and every finding had to supply a concrete failure scenario and survive an adversarial re-check.*