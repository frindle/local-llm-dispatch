#!/usr/bin/env python3
"""Reference impl for: chat-frontend-plan-s6-routes

The gate applies this, runs the verify, and reverts it. It proves two things at
once: the task is SATISFIABLE as specified, and the verify actually ENFORCES the
spec (a refimpl that goes green while a "Must contain" literal is absent means
the verify is benign).

Write the SIMPLEST change that makes the verify pass. It doubles as your review
reference when the model's diff comes back.
"""
import pathlib
import sys
import uuid

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / 'dashboard_chat.py'
t = p.read_text()

# We append handle() and read_session() to the existing file.
# The existing file already has all the utility functions we need.

NEW_CODE = r'''import uuid
import base64

def read_session(base_dir: str, session_id: str) -> dict:
    """Read a session file.

    Validates session_id against ^[0-9a-f]{32}$, raises ValueError on mismatch.
    Reads JSON from <base_dir>/sessions/<id>.json.
    Raises FileNotFoundError if the session file does not exist.
    """
    validate_session_id(session_id)

    sessions_dir = Path(base_dir) / "sessions"
    target_file = sessions_dir / f"{session_id}.json"

    if not target_file.exists():
        raise FileNotFoundError(f"Session {session_id} not found")

    with open(target_file, "r") as f:
        return json.load(f)


def handle(method: str, path: str, query: dict, body_bytes: bytes) -> tuple[int, dict, bytes]:
    """Server-independent routing for chat endpoints.

    Returns (status_code, headers_dict, body_bytes).
    """
    base_dir = get_base_dir()

    # GET /chat serves chat.html
    if method == "GET" and path == "/chat":
        chat_html_path = Path(base_dir) / "chat.html"
        if chat_html_path.exists():
            return (200, {"Content-Type": "text/html"}, chat_html_path.read_bytes())
        return (404, {"Content-Type": "application/json"}, json.dumps({"error": "chat.html not found"}).encode())

    # /api/chat/projects
    if method == "GET" and path == "/api/chat/projects":
        projects_file = Path(base_dir) / "projects.json"
        if not projects_file.exists():
            return (404, {"Content-Type": "application/json"}, json.dumps({"error": "projects.json not found"}).encode())
        with open(projects_file, "r") as f:
            projects = json.load(f)
        return (200, {"Content-Type": "application/json"}, json.dumps(projects).encode())

    # /api/chat/projects/<name>/tree?path=
    if method == "GET" and path.startswith("/api/chat/projects/"):
        parts = path.split("/")
        # parts = ['', 'api', 'chat', 'projects', '<name>', 'tree', ...]
        # parts[3]='projects', parts[4]=name, parts[5]='tree'
        if len(parts) >= 6 and parts[5] == "tree":
            name = parts[4]
            tree_path = query.get("path", "")
            try:
                tree = list_project_tree(base_dir, name, tree_path)
                return (200, {"Content-Type": "application/json"}, json.dumps(tree).encode())
            except (ValueError, FileNotFoundError) as e:
                return (404, {"Content-Type": "application/json"}, json.dumps({"error": str(e)}).encode())

        # /api/chat/projects/<name>/file?path=
        if len(parts) >= 6 and parts[5] == "file":
            name = parts[4]
            file_path = query.get("path", "")
            try:
                content = read_project_file(base_dir, name, file_path)
                return (200, {"Content-Type": "text/plain"}, content.encode())
            except (ValueError, FileNotFoundError) as e:
                return (400, {"Content-Type": "application/json"}, json.dumps({"error": str(e)}).encode())

    # /api/chat/sessions
    if path == "/api/chat/sessions":
        if method == "GET":
            # List sessions
            sessions_dir = Path(base_dir) / "sessions"
            if not sessions_dir.exists():
                return (200, {"Content-Type": "application/json"}, json.dumps([]).encode())
            sessions = []
            for f in sorted(sessions_dir.glob("*.json")):
                with open(f, "r") as fh:
                    sessions.append(json.load(fh))
            return (200, {"Content-Type": "application/json"}, json.dumps(sessions).encode())

        if method == "POST":
            # Create session
            try:
                body = json.loads(body_bytes) if body_bytes else {}
            except (json.JSONDecodeError, ValueError):
                return (400, {"Content-Type": "application/json"}, json.dumps({"error": "invalid JSON"}).encode())

            project = body.get("project", "")
            if not project:
                return (400, {"Content-Type": "application/json"}, json.dumps({"error": "project is required"}).encode())

            session_id = uuid.uuid4().hex
            session_data = {
                "project": project,
                "messages": [],
                "created": "",
            }
            try:
                write_session(base_dir, session_id, session_data)
            except ValueError as e:
                return (400, {"Content-Type": "application/json"}, json.dumps({"error": str(e)}).encode())

            result = {"id": session_id, "project": project, "messages": []}
            return (200, {"Content-Type": "application/json"}, json.dumps(result).encode())

    # /api/chat/sessions/<id>
    if path.startswith("/api/chat/sessions/"):
        parts = path.split("/")
        # parts = ['', 'api', 'chat', 'sessions', '<id>', 'messages', ...]
        # parts[3]='sessions', parts[4]=id, parts[5]='messages'
        if len(parts) >= 4:
            session_id = parts[4]

            # /api/chat/sessions/<id>/messages
            if len(parts) >= 6 and parts[5] == "messages":
                if method != "POST":
                    return (404, {"Content-Type": "application/json"}, json.dumps({"error": "unknown route"}).encode())

                # Parse body
                try:
                    body = json.loads(body_bytes) if body_bytes else {}
                except (json.JSONDecodeError, ValueError):
                    return (400, {"Content-Type": "application/json"}, json.dumps({"error": "invalid JSON"}).encode())

                text = body.get("text", "")
                files = body.get("files", [])
                images = body.get("images", [])

                # Validate session
                try:
                    session = read_session(base_dir, session_id)
                except (ValueError, FileNotFoundError) as e:
                    return (400, {"Content-Type": "application/json"}, json.dumps({"error": str(e)}).encode())

                # Store images if any
                image_paths = []
                if images:
                    for img in images:
                        data_b64 = img.get("data_base64", "")
                        if data_b64:
                            try:
                                import base64 as _b64
                                img_data = _b64.b64decode(data_b64)
                                img_path = stores_image(base_dir, session_id, img_data)
                                if not img_path.startswith("error:"):
                                    image_paths.append(img_path)
                            except Exception:
                                pass

                # Append user message
                messages = session.get("messages", [])
                user_msg = {
                    "role": "user",
                    "content": text,
                    "files": files,
                    "images": image_paths,
                    "ts": "",
                    "status": "completed",
                }
                messages.append(user_msg)

                # Append assistant placeholder with status 'queued'
                assistant_msg = {
                    "role": "assistant",
                    "content": "",
                    "files": [],
                    "images": [],
                    "ts": "",
                    "status": "queued",
                }
                messages.append(assistant_msg)

                session["messages"] = messages

                # Write back
                write_session(base_dir, session_id, session)

                # Call enqueue (injectable, defaults to shell out)
                message_index = len(messages) - 1  # index of assistant placeholder

                def enqueue(session_id, message_index):
                    import subprocess as _sub
                    _sub.run(
                        ["python3", str(Path.home() / "bin" / "ollama-queue.py"), "enqueue",
                         "--bundle", "chat"],
                        check=False,
                    )

                enqueue(session_id, message_index)

                return (202, {"Content-Type": "application/json"}, json.dumps({"status": "queued"}).encode())

            # GET /api/chat/sessions/<id>
            if method == "GET":
                try:
                    session = read_session(base_dir, session_id)
                    return (200, {"Content-Type": "application/json"}, json.dumps(session).encode())
                except (ValueError, FileNotFoundError) as e:
                    return (400, {"Content-Type": "application/json"}, json.dumps({"error": str(e)}).encode())

    # Unknown route
    return (404, {"Content-Type": "application/json"}, json.dumps({"error": "unknown route"}).encode())
'''

# Find the end of the existing file and append
if 'def handle' not in t:
    p.write_text(t + NEW_CODE)
    print("refimpl applied: handle() and read_session() added")
else:
    print("refimpl skipped: handle() already exists")
