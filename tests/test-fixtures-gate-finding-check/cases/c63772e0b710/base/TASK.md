# TASK: chat-fixes2-s1-enqueue-argv

## Confirmed defect (observed, not suspected)

The current `enqueue_job(session_id, message_index)` in `dashboard_chat.py`
passes `session_id` and `message_index` as positional arguments to the
subprocess call without validating `session_id` first, and does not write a
task file.  Verified by reading the source: the function body calls
`subprocess.run()` with `session_id` and `str(message_index)` as positional
args (no `--model`, `--task-file`, `--task-kind`, or `--label` flags) and
never calls `validate_session_id` or writes any file to disk.

## Spec-gap

- `DASHBOARD_CHAT_HOME` -- env var that `test_fixture.py` sets to a temp directory
  (created by `tempfile.mkdtemp(prefix="dctf_")`) for test isolation;
  `dashboard_chat.py`'s `get_base_dir()` reads it (falling back to
  `~/.ollama-dispatch/chat` when unset).

## Entry point

dashboard_chat.py:530

## Required change

In dashboard_chat.py rewrite module-level enqueue_job(session_id, message_index) so it first validates session_id with validate_session_id, writes a task file named job-<message_index>.task.md next to the session json (inside the session base dir) containing the user message text of that message index, then runs subprocess.run( with an argument list (never a shell string, never shell=True) starting python3, the path ~/bin/ollama-queue.py, the word enqueue, then --model qwen3.6-35b-a3b-vl-mtp-mxfp8, --task-file with that file path, --task-kind research, --bundle chat and --label chat-<session_id>-<message_index>. Keep every existing public function name and signature working; python3 test_safety.py must not regress.

Behaviour that must NOT change:
- All existing public function names and signatures remain the same
- test_safety.py passes without regression
- The handle() routing logic remains unchanged
- start_direct() remains unchanged

## Must contain

- `validate_session_id`
- `job-`
- `.task.md`
- `--model`
- `qwen3.6-35b-a3b-vl-mtp-mxfp8`
- `--task-file`
- `--task-kind`
- `research`
- `--bundle`
- `chat`
- `--label`
- `chat-`
- `DASHBOARD_CHAT_HOME`

## Scope

Only edit `test_fixture.py`, `refimpl.py` and `TASK.md`; never edit
`dashboard_chat.py` directly (apply changes through `refimpl.py`).
Do not edit `verify.sh`.

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
