# TASK: chat-fixes-s6c-mode-routing

## Confirmed defect (observed, not suspected)

The POST /api/chat/sessions/<id>/messages route always returns status "queued"
regardless of whether the message is a job request or a chat message. There is
no way to distinguish between the two modes, and no way to specify a preferred
mode. The response does not include mode or message_index.

## Entry point

dashboard_chat.py:470

## Required change

All in dashboard_chat.py. Today the POST /api/chat/sessions/<id>/messages route always appends an assistant placeholder with status "queued" and calls an `enqueue` function that is defined INSIDE `handle`, so nothing can substitute it. Change it to this contract:

1. Add `import threading` at module level.
2. Add two MODULE-LEVEL functions, defined before `handle`, and make `handle` call them by bare name at call time (so a test can replace `dashboard_chat.enqueue_job` / `dashboard_chat.start_direct`):
   - `enqueue_job(session_id, message_index)`: the existing nested `enqueue` body moved out of `handle` unchanged (it shells out to ollama-queue.py enqueue with `--bundle chat`). Delete the nested `enqueue`.
   - `start_direct(session_id, message_index)`: starts `threading.Thread(target=job, args=(session_id, message_index), daemon=True)` and returns None. `job` is the existing module-level function, looked up when the thread is created.
3. In the messages route, after the existing body validation, read the optional body field `mode`:
   - absent: mode is "job" when `is_job_request(text)` is true, otherwise "chat".
   - present: it must be the str "chat" or the str "job"; any other value (including non-str) returns status 400 with a JSON {"error": ...} body BEFORE anything is appended or written.
4. The assistant placeholder message gets key `"mode": <mode>` and status "running" when mode is "chat", status "queued" when mode is "job". The user message is unchanged (status "completed", no mode key).
5. After writing the session: mode "job" calls `enqueue_job(session_id, message_index)`; mode "chat" calls `start_direct(session_id, message_index)`; exactly one of them, exactly once. `message_index` is the index of the assistant message.
6. Return status 202 with JSON body exactly {"mode": <mode>, "message_index": <int>, "status": <"queued" or "running">}.
7. `write_session` must persist a per-message "mode" key when (and only when) the message dict has one; messages without "mode" are written exactly as before.

Behaviour that must NOT change:
- is_job_request(text) still works as before
- validate_session_id still validates against ^[0-9a-f]{32}$
- write_session still writes session files atomically
- call_model still posts to the local Darkbloom chat endpoint
- job(session_id, message_index) still processes a single chat message
- handle() still routes all existing endpoints correctly

## Must contain

- `enqueue_job`
- `start_direct`
- `is_job_request`
- `daemon=True`
- `threading`
- `"mode"`

(The gate holds the reference impl against this list. If the verify goes green
while one of these is absent from the changed files, the verify does not
enforce the spec -- that is a benign verify, caught mechanical.)

(A bare bullet checks the default target. To PIN a literal to a specific file --
useful when a fix spans a helper file and the route/wiring that calls it --
prefix the bullet with `in <path>:`, e.g.
`- in app/api/x/route.ts: ` followed by a backtick-quoted token. Then that
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

## Environment
- `DASHBOARD_CHAT_HOME`: read by the existing `get_base_dir()`; `handle` uses it for the sessions directory. The fixture sets it to a temp directory. No new env var is read by the change.
