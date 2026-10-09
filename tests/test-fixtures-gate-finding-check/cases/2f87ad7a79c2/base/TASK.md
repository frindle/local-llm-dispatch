# TASK: chat-frontend-plan-s6-routes

## Confirmed defect (observed, not suspected)

The existing dashboard_chat.py has utility functions (write_session, read_project_file, list_project_tree, etc.) and a FastAPI app, but lacks a server-independent routing function `handle(method, path, query, body_bytes)` that can serve all chat endpoints without a running server. This means the chat UI cannot be tested or served independently of the FastAPI app.

## Entry point

dashboard_chat.py: line after the existing functions — add `handle` and `read_session`.

## Required change

Create `handle(method: str, path: str, query: dict, body_bytes: bytes) -> tuple[int, dict, bytes]` implementing server-independent routing:

- `GET /chat` serves `chat.html` from the base directory
- `GET /api/chat/projects` returns the project list from `projects.json`
- `GET /api/chat/projects/<name>/tree?path=` returns the file tree listing via `list_project_tree`
- `GET /api/chat/projects/<name>/file?path=` returns file content via `read_project_file`
- `POST /api/chat/sessions {project}` creates a session (generates 32-char hex session_id, calls `write_session`, returns session data)
- `GET /api/chat/sessions` lists session files
- `GET /api/chat/sessions/<id>` returns a session via `read_session`
- `POST /api/chat/sessions/<id>/messages {text, files, images: [{data_base64}]}` appends the user message plus an assistant placeholder with status 'queued', calls an injectable `enqueue(session_id, message_index)` defaulting to shell out to `python3 ~/bin/ollama-queue.py enqueue` with bundle tag `chat`, and returns 202
- Unknown route returns 404
- Bad input returns 400 with `{error}`
- `chat_template_kwargs {'enable_thinking': False}` is passed to the model
- STDLIB ONLY: do not import fastapi, requests, flask, or any non-standard-library module, and do not use or extend the existing FastAPI `app`/endpoints in the file; use only functions already defined in dashboard_chat.py plus the standard library
- Sessions are read with `read_session(base_dir, session_id) -> dict` (also define it in this slice if it is not already defined: it validates the id with `validate_session_id` and raises `FileNotFoundError`/`ValueError`, mirroring `write_session`)

Behaviour that must NOT change:
- All existing functions (validate_session_id, get_base_dir, write_session, validate_image, read_project_file, list_project_tree, check_image_limits, stores_image, build_request, call_model) remain unchanged
- The FastAPI app and its /build_request endpoint remain unchanged
- All existing imports remain unchanged

## Must contain

- `handle`
- `read_session`
- `validate_session_id`
- `write_session`
- `get_base_dir`
- `list_project_tree`
- `read_project_file`
- `chat.html`
- `projects.json`
- `session_id`
- `session_data`
- `messages`
- `assistant`
- `queued`
- `enqueue`
- `ollama-queue.py`
- `bundle`
- `chat_template_kwargs`
- `enable_thinking`
- `404`
- `400`
- `error`
- `202`

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
