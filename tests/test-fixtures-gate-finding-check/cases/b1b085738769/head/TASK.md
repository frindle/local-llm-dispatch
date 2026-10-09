# TASK: chat-fixes3-s2-collect-results

## Confirmed defect (observed, not suspected)

When a job message has status "queued", the system enqueues a job via
`ollama-queue.py` which eventually writes an `ANSWER.md` file under
`<base_dir>/jobs/<session_id>-<message_index>/`. However, the GET
`/api/chat/sessions/<id>` endpoint never checks for these completed job
results. A session with queued assistant messages remains stuck in "queued"
state even after the job has completed and written `ANSWER.md`. The symptom
is: a session created with a job request shows `"status": "queued"` for the
assistant message, and even after the background job finishes and writes
`ANSWER.md`, a subsequent GET to that session returns the message still as
"queued" with empty content.

## Entry point

dashboard_chat.py:581 (the `# GET /api/chat/sessions/<id>` branch inside `handle()`)

## Required change

In dashboard_chat.py add a module-level function collect_job_results(base_dir, session_id) that loads the session and, for every message whose role is assistant and status is queued, looks for the file ANSWER.md in <base_dir>/jobs/<session_id>-<message_index>/ ; when that file exists and is non-empty it sets that message's content to the file text and its status to done, and writes the session once if anything changed. It returns the number of messages updated. Call it from the GET /api/chat/sessions/<id> branch of handle() before the session is read and returned. Keep every public function name and signature; python3 test_safety.py must not regress.

Sessions and job directories live under the directory named by the environment variable `DASHBOARD_CHAT_HOME` (the fixture sets it to a temp dir; do not change how it is read).

Behaviour that must NOT change:
- All existing function signatures and names remain identical
- test_safety.py passes without regression
- The handle() routing for all other endpoints is unchanged
- read_session, write_session, and all other public functions keep their exact signatures

## Must contain

- `collect_job_results`
- `base_dir`
- `session_id`
- `ANSWER.md`
- `read_session`
- `write_session`
- `collect_job_results(base_dir, session_id)`

(The gate holds the reference impl against this list. If the verify goes green
while one of these is absent from the changed files, the verify does not
enforce the spec -- that is a benign verify, caught mechanically.)

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
