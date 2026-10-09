#!/usr/bin/env python3
"""Tests for ollama-worker.py --api-key-file (2026-10-05, Strata bake-off arm).

The flag lets a bake-off arm talk to a key-gated OpenAI-style server that is not
Darkbloom (Strata on claude-sandbox via an ssh tunnel) WITHOUT the key ever being
on argv. Properties asserted, all against a local http.server stub (no network,
no GPU, no Darkbloom):

  1. call_ollama(api_style="openai") sends `Authorization: Bearer <file contents>`
     to the host the file was bound to.
  2. the key is read at CALL time (rotate the file -> the next call sends the new key).
  3. a DIFFERENT host gets no Authorization header (the key never leaks sideways).
  4. unset (every queue-launched job) -> no Authorization header at all.
  5. the key never appears in the worker's log output.
  6. main() refuses --api-key-file without --api openai / an explicit --host.

Red-on-revert: make _openai_auth_headers() return {} unconditionally -> 1/2 go red;
drop the host comparison -> 3 goes red.

Run: python3 test-worker-api-key-file.py
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WORKER = Path(os.environ.get("WORKER_UNDER_TEST") or
              (Path(__file__).resolve().parent / "ollama-worker.py"))
failures = []
SEEN = []


def ok(name, cond, detail=""):
    print(f"  ok   {name}" if cond else f"  FAIL {name} {detail}")
    if not cond:
        failures.append(name)


def _load():
    os.environ["HOME"] = tempfile.mkdtemp(prefix="apikeyfile-home-")
    os.environ.pop("OBSIDIAN_TOKEN", None)
    spec = importlib.util.spec_from_file_location("ollama_worker_apikeyfile", WORKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        SEEN.append(self.headers.get("Authorization"))
        body = json.dumps({"choices": [{"index": 0, "finish_reason": "stop",
                                        "message": {"role": "assistant", "content": "hi"}}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                     "total_tokens": 2}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _server():
    s = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, f"http://127.0.0.1:{s.server_address[1]}"


def main():
    m = _load()
    s1, h1 = _server()
    s2, h2 = _server()
    d = tempfile.mkdtemp(prefix="apikeyfile-")
    kf = Path(d) / "key"
    kf.write_text("sekrit-AAA\n")
    os.chmod(kf, 0o600)

    def call(host):
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            m.call_ollama(host, "stub", [{"role": "user", "content": "x"}], 0.2, 4096,
                          timeout=10, tools=False, api_style="openai", max_tokens=8)
        return buf.getvalue()

    # 4: unset -> no header
    SEEN.clear(); call(h1)
    ok("unset: no Authorization header", SEEN == [None], SEEN)

    m.set_openai_key_file(str(kf), h1)
    SEEN.clear(); out = call(h1)
    ok("bound host gets Bearer <file contents>", SEEN == ["Bearer sekrit-AAA"], SEEN)
    ok("key never in worker output", "sekrit" not in out)
    # /v1 suffix form of the same host is the same host
    SEEN.clear(); call(h1 + "/")
    ok("trailing slash is the same host", SEEN == ["Bearer sekrit-AAA"], SEEN)

    kf.write_text("sekrit-BBB")
    SEEN.clear(); call(h1)
    ok("key read at call time (rotation picked up)", SEEN == ["Bearer sekrit-BBB"], SEEN)

    SEEN.clear(); call(h2)
    ok("a different host gets NO key", SEEN == [None], SEEN)

    m.set_openai_key_file(None, None)
    SEEN.clear(); call(h1)
    ok("cleared -> no header again", SEEN == [None], SEEN)
    s1.shutdown(); s2.shutdown()

    # 6: argv validation (exit 4 before any network)
    env = {**os.environ, "OLLAMA_DISPATCH_VIA_QUEUE": "test"}
    r = subprocess.run([sys.executable, str(WORKER), "--cwd", d, "--task", "x",
                        "--api-key-file", str(kf), "--host", "http://127.0.0.1:9"],
                       capture_output=True, text=True, env=env, timeout=60)
    ok("--api-key-file without --api openai -> exit 4", r.returncode == 4, (r.returncode, r.stderr[-200:]))
    r = subprocess.run([sys.executable, str(WORKER), "--cwd", d, "--task", "x", "--api", "openai",
                        "--api-key-file", str(Path(d) / "nope"), "--host", "http://127.0.0.1:9"],
                       capture_output=True, text=True, env=env, timeout=60)
    ok("missing key file -> exit 4", r.returncode == 4, (r.returncode, r.stderr[-200:]))

    print("ALL OK" if not failures else f"{len(failures)} FAILURE(S): {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
