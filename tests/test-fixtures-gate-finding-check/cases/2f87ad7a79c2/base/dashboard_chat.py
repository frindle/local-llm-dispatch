"""dashboard_chat: chat front end for the Ollama-queue dashboard (stdlib only)."""

import json
import os
import re
import tempfile
from pathlib import Path

_SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_DEFAULT_BASE_DIR = Path.home() / ".ollama-dispatch" / "chat"


def validate_session_id(session_id: str) -> None:
    """Validate session_id against ^[0-9a-f]{32}$, raising ValueError on mismatch."""
    if not _SESSION_ID_RE.match(session_id):
        raise ValueError("session_id must match ^[0-9a-f]{32}$")


def get_base_dir() -> str:
    """Return the base directory from DASHBOARD_CHAT_HOME env or the default."""
    return os.environ.get("DASHBOARD_CHAT_HOME", str(_DEFAULT_BASE_DIR))


def write_session(base_dir: str, session_id: str, session_data: dict) -> None:
    """Write a session file atomically.

    Validates session_id against ^[0-9a-f]{32}$, raises ValueError on mismatch.
    Writes JSON to <base_dir>/sessions/<id>.json.
    """
    validate_session_id(session_id)

    sessions_dir = Path(base_dir) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # Build the session data structure
    messages = session_data.get("messages", [])
    session = {
        "id": session_id,
        "project": session_data.get("project", ""),
        "created": session_data.get("created", ""),
        "messages": [
            {
                "role": msg.get("role", ""),
                "content": msg.get("content", ""),
                "images": msg.get("images", []),
                "files": msg.get("files", []),
                "ts": msg.get("ts", ""),
                "status": msg.get("status", ""),
            }
            for msg in messages
        ],
    }

    target_file = sessions_dir / f"{session_id}.json"

    # Atomic write via temp file + os.replace
    fd, tmp_path = tempfile.mkstemp(dir=sessions_dir, suffix=".tmp")  # relevance: unobservable
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(session, f)
        os.replace(tmp_path, target_file)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def validate_image(data: bytes) -> str | None:
    """Check magic bytes for png, jpeg, webp, gif. Returns extension string or None."""
    if data[:4] == b'\x89\x50\x4e\x47':  # 89504e47
        return "png"
    if data[:3] == b'\xff\xd8\xff':  # ffd8ff
        return "jpeg"
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':  # 52494646...57454250
        return "webp"
    if data[:4] == b'GIF8':  # 47494638
        return "gif"
    return None  # relevance: unobservable


def read_project_file(base_dir: str, project_name: str, path: str) -> str:
    """Read a file from a project, with security checks.

    Resolves os.path.realpath, rejects '..' path components, absolute paths,
    and symlinks escaping the allowlisted root loaded from <base_dir>/projects.json
    as [{name, path}], skips directories .git, node_modules, .venv, __pycache__,
    rejects binary files containing NUL in the first 8KB, and rejects files over 256KB.
    """
    # Load projects.json
    projects_file = os.path.join(base_dir, "projects.json")
    with open(projects_file, "r") as f:
        projects = json.load(f)

    # Find the project
    project = None
    for proj in projects:
        if proj["name"] == project_name:
            project = proj
            break

    if project is None:
        raise ValueError(f"Project '{project_name}' not found")

    # Get the project root
    project_root = os.path.realpath(project["path"])

    # Reject absolute paths
    if os.path.isabs(path):
        raise ValueError("Absolute paths are not allowed")

    # Reject '..' path components
    if ".." in path.split(os.sep):
        raise ValueError("Path traversal is not allowed")

    # Build the target path
    target_path = os.path.realpath(os.path.join(project_root, path))

    # Reject symlinks escaping the root
    if not target_path.startswith(project_root + os.sep) and target_path != project_root:
        raise ValueError("Path escapes project root")

    # Check if it's a file
    if not os.path.isfile(target_path):
        raise FileNotFoundError(f"File not found: {path}")

    # Read the file
    with open(target_path, "rb") as f:
        content = f.read()

    # Reject files over 256KB
    if len(content) > 256 * 1024:
        raise ValueError("File exceeds 256KB limit")

    # Reject binary files containing NUL in the first 8KB
    if b"\x00" in content[:8192]:
        raise ValueError("Binary file rejected (contains NUL in first 8KB)")

    return content.decode("utf-8")


def list_project_tree(base_dir: str, project_name: str, path: str = '') -> list[str]:
    """Return the file tree listing for the project."""
    # Load projects.json
    projects_file = os.path.join(base_dir, "projects.json")
    with open(projects_file, "r") as f:
        projects = json.load(f)

    # Find the project
    project = None
    for proj in projects:
        if proj["name"] == project_name:
            project = proj
            break

    if project is None:
        raise ValueError(f"Project '{project_name}' not found")

    # Get the project root
    project_root = os.path.realpath(project["path"])

    # Build the target path
    target_path = os.path.realpath(os.path.join(project_root, path))

    # Reject symlinks escaping the root
    if not target_path.startswith(project_root + os.sep) and target_path != project_root:
        raise ValueError("Path escapes project root")

    # List the tree
    result = []
    for root, dirs, files in os.walk(target_path):
        # Skip directories .git, node_modules, .venv, __pycache__
        dirs[:] = [d for d in dirs if d not in {'.git', 'node_modules', '.venv', '__pycache__'}]

        # Get relative path
        rel_root = os.path.relpath(root, target_path)

        # Add files
        for fname in sorted(files):
            if rel_root == '.':
                result.append(fname)
            else:
                result.append(os.path.join(rel_root, fname))

    return result


def check_image_limits(images: list[bytes]) -> bool:
    """Check that all images are valid (magic bytes) and within limits (8MB, 6 per message)."""
    if len(images) > 6:
        return False
    MAX_SIZE = 8 * 1024 * 1024  # 8MB
    for img in images:
        if len(img) > MAX_SIZE:
            return False
        if validate_image(img) is None:
            return False
    return True


def stores_image(base_dir: str, session_id: str, data: bytes) -> str:
    """Store a valid image under <base_dir>/images/<session_id>/<n>.<ext>.

    Validates magic bytes, enforces 8MB max per image, and 6 per message.
    Returns the file path on success, or an error string on failure.
    """
    try:
        validate_session_id(session_id)
    except ValueError:
        return "error: invalid session_id"

    ext = validate_image(data)
    if ext is None:
        return "error: invalid image format"

    MAX_SIZE = 8 * 1024 * 1024
    if len(data) > MAX_SIZE:
        return "error: image exceeds 8MB limit"

    images_dir = Path(base_dir) / "images" / session_id
    images_dir.mkdir(parents=True, exist_ok=True)

    existing = sorted([f for f in images_dir.iterdir() if f.is_file()])
    n = len(existing)

    if n >= 6:
        return "error: maximum 6 images per message reached"

    target_file = images_dir / f"{n}.{ext}"
    target_file.write_bytes(data)

    return str(target_file)


def build_request(project_name: str, messages: list[dict], files: list[str], images: list[dict]) -> dict:
    """Construct an API request with system prompt, files, messages, and images."""
    system_prompt = f"Project: {project_name}. You are a read-only assistant. Answer only with fenced ```diff blocks."

    content = [{"type": "text", "text": system_prompt}]

    for f in files:
        content.append({"type": "text", "text": f"[FILE: {f}]"})

    for msg in messages:
        content.append({"type": "text", "text": f"{msg.get('role', 'user')}: {msg.get('content', '')}"})

    for img in images:
        mime = img.get("mime", "image/png")
        encoded = img.get("encoded", "")
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{encoded}"}
        })

    return {"messages": [{"role": "system", "content": content}], "files": files}


from fastapi import FastAPI, HTTPException, Body

app = FastAPI()


@app.post("/build_request")
def build_request_endpoint(project_name: str = Body(...), messages: list[dict] = Body([]), files: list[str] = Body([]), images: list[dict] = Body([])):
    if not project_name:
        raise HTTPException(status_code=400, detail="project_name is required")
    return build_request(project_name, messages, files, images)


import urllib.error
import urllib.request


def _default_http(url, headers, body, timeout):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def call_model(request: dict, http_func=None) -> dict:
    """POST request["messages"] to the local Darkbloom chat endpoint; never raises."""
    key = None
    try:
        if not isinstance(request, dict) or not isinstance(request.get("messages"), list):
            return {"status": "error", "text": "invalid request"}
        with open(os.path.expanduser("~/.darkbloom/local.json")) as f:
            key = json.load(f)["api_key"]
        if not isinstance(key, str) or not key:
            return {"status": "error", "text": "no api key"}
        body = json.dumps({
            "model": "qwen3.6-35b-a3b-vl-mtp-mxfp8",
            "messages": request["messages"],
            "chat_template_kwargs": {"enable_thinking": False},
            "stream": False,
        }).encode()
        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
        status, raw = (http_func or _default_http)(
            "http://127.0.0.1:8000/v1/chat/completions", headers, body, 60)
        if status != 200:
            return {"status": "error", "text": "http " + str(status)}
        text = json.loads(raw)["choices"][0]["message"]["content"]
        if not isinstance(text, str):
            return {"status": "error", "text": "bad response"}
        return {"status": "done", "text": text}
    except Exception as e:
        msg = (type(e).__name__ + ": " + str(e)).replace(key, "[key]") if key else type(e).__name__
        return {"status": "error", "text": msg[:200]}
