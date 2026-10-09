#!/usr/bin/env python3
"""Reference impl for: chat-fixes-s6c-mode-routing (hand-written by Main)."""
import pathlib
import sys

wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
p = wt / "dashboard_chat.py"
t = p.read_text()


def sub(old, new, label):
    global t
    assert t.count(old) == 1, "refimpl anchor not found/unique: " + label
    t = t.replace(old, new, 1)


sub("import tempfile\n", "import tempfile\nimport threading\n", "import")

sub('''    # Atomic write via temp file + os.replace''',
    '''    for out, src in zip(session["messages"], messages):
        if "mode" in src:
            out["mode"] = src["mode"]

    # Atomic write via temp file + os.replace''', "write_session mode")

sub('''def handle(method: str, path: str, query: dict, body_bytes: bytes)''',
    '''def enqueue_job(session_id, message_index):
    import subprocess as _sub
    _sub.run(
        ["python3", str(Path.home() / "bin" / "ollama-queue.py"), "enqueue",
         "--bundle", "chat"],
        check=False,
    )


def start_direct(session_id, message_index):
    threading.Thread(target=job, args=(session_id, message_index), daemon=True).start()


def handle(method: str, path: str, query: dict, body_bytes: bytes)''', "module funcs")

sub('''                try:
                    session = read_session(base_dir, session_id)
                except (ValueError, FileNotFoundError) as e:
                    return (400, {"Content-Type": "application/json"}, json.dumps({"error": str(e)}).encode())

                image_paths = []''',
    '''                mode = body.get("mode")
                if mode is None:
                    mode = "job" if is_job_request(text) else "chat"
                elif mode not in ("chat", "job") or not isinstance(mode, str):
                    return (400, {"Content-Type": "application/json"}, json.dumps({"error": "mode must be 'chat' or 'job'"}).encode())

                try:
                    session = read_session(base_dir, session_id)
                except (ValueError, FileNotFoundError) as e:
                    return (400, {"Content-Type": "application/json"}, json.dumps({"error": str(e)}).encode())

                image_paths = []''', "mode parse")

sub('''                    "ts": "",
                    "status": "queued",
                }''',
    '''                    "ts": "",
                    "status": "running" if mode == "chat" else "queued",
                    "mode": mode,
                }''', "assistant msg")

a = t.index("                def enqueue(session_id, message_index):")
b = t.index("            # GET /api/chat/sessions/<id>\n")
t = t[:a] + '''                if mode == "chat":
                    start_direct(session_id, message_index)
                else:
                    enqueue_job(session_id, message_index)

                status = "running" if mode == "chat" else "queued"
                return (202, {"Content-Type": "application/json"}, json.dumps({"mode": mode, "message_index": message_index, "status": status}).encode())

''' + t[b:]
p.write_text(t)
print("refimpl applied")
