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

In dashboard_chat.py change build_request so after the system message each prior turn becomes its OWN message with its real role (user or assistant) and string content, in order; images ({mime, encoded}) attach to the LAST user message by making that message content a list of one text part plus one image_url part per image, data URL form data:<mime>;base64,<encoded>. With no images, content stays a plain string. Keep every existing public function name and signature working; python3 test_safety.py must not regress. Name the index of the last user turn last_user.

Behaviour that must NOT change:
- `build_request` returns a dict with keys `messages` and `files`
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
