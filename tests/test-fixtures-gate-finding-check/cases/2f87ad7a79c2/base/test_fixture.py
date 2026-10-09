"""Adversarial fixture for: chat-frontend-plan-s6-routes

CASES is empty and the verify FAILS until you fill it in. That is deliberate.
"""
import sys
import importlib.util
import os
import json
import tempfile
import shutil
import pathlib

spec = importlib.util.spec_from_file_location("target", 'dashboard_chat.py')
target = importlib.util.module_from_spec(spec)
sys.modules["target"] = target
spec.loader.exec_module(target)


def _setup_test_dir():
    """Create a temporary test directory with projects.json and chat.html."""
    d = tempfile.mkdtemp()
    projects = [{"name": "testproj", "path": d}]
    with open(os.path.join(d, "projects.json"), "w") as f:
        json.dump(projects, f)
    with open(os.path.join(d, "chat.html"), "w") as f:
        f.write("<html><body>chat</body></html>")
    os.makedirs(os.path.join(d, "sessions"), exist_ok=True)
    return d


def _make_handle():
    """Return a handle function bound to a test base_dir."""
    base_dir = _setup_test_dir()
    orig_get_base_dir = target.get_base_dir
    target.get_base_dir = lambda: base_dir
    return target.handle, base_dir, orig_get_base_dir


def _cleanup(base_dir, orig_get_base_dir):
    shutil.rmtree(base_dir, ignore_errors=True)
    target.get_base_dir = orig_get_base_dir


def _case_chat_html():
    handle, base_dir, orig = _make_handle()
    try:
        status, headers, body = handle("GET", "/chat", {}, b"")
        return (status, dict(headers))
    finally:
        _cleanup(base_dir, orig)


def _case_projects_list():
    handle, base_dir, orig = _make_handle()
    try:
        status, headers, body = handle("GET", "/api/chat/projects", {}, b"")
        return (status, dict(headers))
    finally:
        _cleanup(base_dir, orig)


def _case_tree_listing():
    handle, base_dir, orig = _make_handle()
    try:
        status, headers, body = handle("GET", "/api/chat/projects/testproj/tree", {"path": ""}, b"")
        return (status, dict(headers))
    finally:
        _cleanup(base_dir, orig)


def _case_file_content():
    handle, base_dir, orig = _make_handle()
    try:
        status, headers, body = handle("GET", "/api/chat/projects/testproj/file", {"path": "projects.json"}, b"")
        return (status, dict(headers))
    finally:
        _cleanup(base_dir, orig)


def _case_create_session():
    handle, base_dir, orig = _make_handle()
    try:
        status, headers, body = handle("POST", "/api/chat/sessions", {}, json.dumps({"project": "testproj"}).encode())
        return (status, dict(headers))
    finally:
        _cleanup(base_dir, orig)


def _case_create_session_no_project():
    handle, base_dir, orig = _make_handle()
    try:
        status, headers, body = handle("POST", "/api/chat/sessions", {}, json.dumps({"foo": "bar"}).encode())
        return (status, dict(headers))
    finally:
        _cleanup(base_dir, orig)


def _case_create_session_invalid_json():
    handle, base_dir, orig = _make_handle()
    try:
        status, headers, body = handle("POST", "/api/chat/sessions", {}, b"not json")
        return (status, dict(headers))
    finally:
        _cleanup(base_dir, orig)


def _case_list_sessions():
    handle, base_dir, orig = _make_handle()
    try:
        status, headers, body = handle("GET", "/api/chat/sessions", {}, b"")
        return (status, dict(headers))
    finally:
        _cleanup(base_dir, orig)


def _case_send_messages():
    handle, base_dir, orig = _make_handle()
    try:
        # First create a session to get a valid session_id
        status, headers, body = handle("POST", "/api/chat/sessions", {}, json.dumps({"project": "testproj"}).encode())
        if status != 200:
            return (status, dict(headers))
        session = json.loads(body)
        sid = session["id"]
        # Now send messages to that session
        status, headers, body = handle("POST", "/api/chat/sessions/" + sid + "/messages", {}, json.dumps({"text": "hello", "files": [], "images": []}).encode())
        return (status, dict(headers))
    finally:
        _cleanup(base_dir, orig)


def _case_unknown_route():
    handle, base_dir, orig = _make_handle()
    try:
        status, headers, body = handle("GET", "/unknown", {}, b"")
        return (status, dict(headers))
    finally:
        _cleanup(base_dir, orig)


def _case_invalid_session_get():
    handle, base_dir, orig = _make_handle()
    try:
        status, headers, body = handle("GET", "/api/chat/sessions/invalid_id", {}, b"")
        return (status, dict(headers))
    finally:
        _cleanup(base_dir, orig)


def _case_invalid_session_messages():
    handle, base_dir, orig = _make_handle()
    try:
        status, headers, body = handle("POST", "/api/chat/sessions/invalid_id/messages", {}, json.dumps({"text": "hi"}).encode())
        return (status, dict(headers))
    finally:
        _cleanup(base_dir, orig)


def _case_session_detail():
    handle, base_dir, orig = _make_handle()
    try:
        # First create a session
        status, headers, body = handle("POST", "/api/chat/sessions", {}, json.dumps({"project": "testproj"}).encode())
        if status != 200:
            return (status, dict(headers))
        session = json.loads(body)
        sid = session["id"]
        # Then get it
        status, headers, body = handle("GET", "/api/chat/sessions/" + sid, {}, b"")
        return (status, dict(headers))
    finally:
        _cleanup(base_dir, orig)


CASES = [
    ("GET /chat serves chat.html", _case_chat_html, (200, {"Content-Type": "text/html"})),
    ("GET /api/chat/projects returns project list", _case_projects_list, (200, {"Content-Type": "application/json"})),
    ("GET /api/chat/projects/<name>/tree returns tree listing", _case_tree_listing, (200, {"Content-Type": "application/json"})),
    ("GET /api/chat/projects/<name>/file returns file content", _case_file_content, (200, {"Content-Type": "text/plain"})),
    ("POST /api/chat/sessions creates session with valid project", _case_create_session, (200, {"Content-Type": "application/json"})),
    ("POST /api/chat/sessions without project returns 400", _case_create_session_no_project, (400, {"Content-Type": "application/json"})),
    ("POST /api/chat/sessions with invalid JSON returns 400", _case_create_session_invalid_json, (400, {"Content-Type": "application/json"})),
    ("GET /api/chat/sessions lists sessions", _case_list_sessions, (200, {"Content-Type": "application/json"})),
    ("POST /api/chat/sessions/<id>/messages returns 202", _case_send_messages, (202, {"Content-Type": "application/json"})),
    ("GET unknown route returns 404", _case_unknown_route, (404, {"Content-Type": "application/json"})),
    ("GET /api/chat/sessions/<bad_id> returns 400", _case_invalid_session_get, (400, {"Content-Type": "application/json"})),
    ("POST /api/chat/sessions/<id>/messages with bad session returns 400", _case_invalid_session_messages, (400, {"Content-Type": "application/json"})),
    ("GET /api/chat/sessions/<id> returns session detail", _case_session_detail, (200, {"Content-Type": "application/json"})),
]


def main():
    if len(CASES) < 3:
        print("  SCAFFOLD_INCOMPLETE: {} adversarial case(s) authored, need >= 3."
              .format(len(CASES)))
        print("  A generated scaffold is not a verify. Author the cases in "
              "test_fixture.py.")
        return 1
    fails = 0
    for desc, thunk, want in CASES:
        try:
            got = thunk()
        except Exception as e:
            print("  FAIL {} -- raised {}: {}".format(desc, type(e).__name__, e))
            fails += 1
            continue
        if got != want:
            print("  FAIL {} -- got {!r}, want {!r}".format(desc, got, want))
            fails += 1
    print("  {}/{} case(s) passed".format(len(CASES) - fails, len(CASES)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
