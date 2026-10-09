# TASK: chat-fixes-s4b-build-request-turns

## Confirmed defect (observed, not suspected)

The `build_request` function in `dashboard_chat.py` concatenates all prior turns
into the system message's `content` list as plain text strings (e.g.
`"user: q1"`), rather than creating separate message objects with their actual
`role` and `content` fields. This means the model never sees individual turns
with correct roles — it only sees a single system message with a concatenated
content array. Verified by calling `build_request` with multiple turns and
observing that `messages[1:]` all have `role == "system"` instead of the
expected `["user", "assistant", "user"]` spread across separate message objects.

## Entry point

dashboard_chat.py:274

## Required change

In dashboard_chat.py change build_request so the returned dict is {"messages": [system_message] + turn_messages, "files": files}.
- `system_message` is unchanged: first, role "system", content a list of text parts (as today).
- Each prior turn in `messages` becomes its OWN TOP-LEVEL message, in order, {"role": <its role, default "user">, "content": <its content string>}. Turns must NOT be placed inside the system message's content list.
- `images` ({mime, encoded}) attach to the LAST user turn only: keep its index in a local variable named `last_user`, and replace that turn's content with a list: one {"type": "text", "text": <original text>} part followed by one {"type": "image_url", "image_url": {"url": "data:<mime>;base64,<encoded>"}} part per image, in order. Other turns keep plain string content. With no images, or no user turn, nothing is attached and nothing raises.
- Do not mutate the input `messages` list or its dicts.
- Do not add any new key to the returned dict.

Behaviour that must NOT change:
- `build_request` returns a dict with exactly the keys `messages` and `files`
- The system message is always `messages[0]` with `role == "system"`
- `files` parameter is passed through unchanged in the return value
- `call_model` and `job` continue to work with the returned request shape
- `test_safety.py` passes without regression

## Must contain

- `build_request`
- `last_user`
- `image_url`
- `data:`
- `base64,`

(The gate holds the reference impl against this list. If the verify goes green
while one of these is absent from the changed files, the verify does not
enforce the spec -- that is a benign verify, caught mechanically.)

(A bare bullet checks the default target. To PIN a literal to a specific file --
useful when a fix spans a helper file and the route/wiring that calls it --
prefix the bullet with `in <path>: ` followed by a backtick-quoted token. Then that
token is required in THAT file, not the target.)

## Scope

Only edit `dashboard_chat.py`; do not edit `verify.sh`, `test_fixture.py` or `TASK.md`.
test_fixture.py is the test fixture -- changing it invalidates the check.

## Keep every changed line exercised (relevance)

After the job runs, a mutation check flips/deletes each line you changed and
asks the verify to catch it. A changed line whose every mutant survives --
because no test asserts it -- FAILS the gate even when the fix is correct, and
the review never runs. So do NOT emit an isolated, untested line:
- Fold an unavoidable constant onto a line the test already exercises. Put a
  `timeout=` / a `daemon=True` flag / a small tuning number on the SAME line as
  a header dict, URL, or argument the fixture checks -- never on its own line.
- Prefer falling through to an implicit `return None` over a standalone
  `return None` in an `except:` the tests do not assert.
- If a line genuinely cannot be asserted and cannot be folded, it usually
  should not be a separate line at all -- restructure so it isn't.
This is not about adding bogus assertions for constants; it is about not
leaving a lone line that carries no tested behaviour.

## Loop instruction

Run `bash verify.sh` after every edit and keep editing until it prints
`VERIFY_OK`.
