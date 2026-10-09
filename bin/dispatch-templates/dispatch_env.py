"""dispatch_env.py -- SEALED, vetted environment helpers for Python harness fixtures.

Source of truth: ~/bin/dispatch-templates/dispatch_env.py, proven by dispatch_env_test.py
next to it. The scaffold drops a READ-ONLY copy at the worktree ROOT as `dispatch_env.py`;
auto-harness-check restores it byte-for-byte if it is edited, so do not edit it. IMPORT it,
never re-write it:

    from dispatch_env import memory_db, FakeClock, StubHttp, StubResponse, env, tmp_cwd

  memory_db(*sql, migrations_dir=None)   in-memory sqlite3 (Row factory, foreign keys ON);
                                         each sql arg is executed as a script; migrations_dir
                                         replays <dir>/<name>/migration.sql in sorted order
  FakeClock(start=1_700_000_000.0)       .time() / .now() (UTC datetime) / .sleep(s) / .advance(s)
                                         -- inject clock.time, do not patch the time module
  StubHttp(route)                        records .calls; route(method, url, **kw) returns a
                                         StubResponse | (status, body) | (status, body, headers);
                                         .get/.post/.put/.patch/.delete/.request like requests
  StubResponse(status, body, headers)    .ok .status_code .text .content .json() .raise_for_status()
  env(**vars)                            context manager: set (str) or unset (None) os.environ
  tmp_cwd()                              context manager: chdir into a fresh temp dir, yields Path

No network, no real clocks, no sleeping: every helper is deterministic.
"""
import contextlib
import datetime as _dt
import json as _json
import os
import sqlite3
import tempfile
from pathlib import Path


def memory_db(*sql, migrations_dir=None):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    if migrations_dir is not None:
        d = Path(migrations_dir)
        if not d.is_dir():
            raise FileNotFoundError("dispatch_env: migrations_dir not found: %s" % d)
        for sub in sorted(p for p in d.iterdir() if p.is_dir()):
            f = sub / "migration.sql"
            if f.is_file():
                db.executescript(f.read_text())
    for s in sql:
        db.executescript(s)
    return db


class FakeClock:
    def __init__(self, start=1_700_000_000.0):
        self._t = float(start)
        self.sleeps = []

    def time(self):
        return self._t

    def now(self):
        return _dt.datetime.fromtimestamp(self._t, _dt.timezone.utc)

    def advance(self, seconds):
        self._t += float(seconds)
        return self._t

    def sleep(self, seconds):
        self.sleeps.append(float(seconds))
        self._t += float(seconds)


class StubResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = int(status)
        self.headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        self._body = {} if body is None else body
        self.url = ""

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    @property
    def text(self):
        return self._body if isinstance(self._body, str) else _json.dumps(self._body)

    @property
    def content(self):
        return self.text.encode()

    def json(self):
        return _json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d for %s" % (self.status_code, self.url))


class StubHttp:
    def __init__(self, route=None):
        self.route = route or (lambda method, url, **kw: (200, {}))
        self.calls = []

    def request(self, method, url, **kw):
        method = str(method).upper()
        self.calls.append({"method": method, "url": url, **kw})
        r = self.route(method, url, **kw)
        if not isinstance(r, StubResponse):
            r = StubResponse(*r) if isinstance(r, tuple) else StubResponse(200, r)
        r.url = url
        return r

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def put(self, url, **kw):
        return self.request("PUT", url, **kw)

    def patch(self, url, **kw):
        return self.request("PATCH", url, **kw)

    def delete(self, url, **kw):
        return self.request("DELETE", url, **kw)


@contextlib.contextmanager
def env(**variables):
    saved = {k: os.environ.get(k) for k in variables}
    try:
        for k, v in variables.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def tmp_cwd():
    old = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="dispatch-env-") as d:
        os.chdir(d)
        try:
            yield Path(d)
        finally:
            os.chdir(old)
