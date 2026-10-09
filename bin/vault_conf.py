#!/usr/bin/env python3
"""Shared vault endpoint/token resolution for the owner's vault callers (stdlib only).

Cutover design (Basic Memory + vault-shim, 2026-10-01): the shim keeps the Obsidian-compatible
REST on :27123 with a bearer token, so callers only need a base URL and a token source.
Resolution order, per key:
  VAULT_URL         env VAULT_URL -> ~/.config/vault/env -> legacy env OBSIDIAN_URL -> caller default
  VAULT_TOKEN_FILE  env VAULT_TOKEN_FILE -> ~/.config/vault/env -> ~/.config/ollama-worker/obsidian-token
  token             legacy env OBSIDIAN_TOKEN (wins, as before) -> contents of the token file
~/.config/vault/env is KEY=VALUE lines (# comments ok). It does not exist until cutover, so with
no env file every caller behaves exactly as before. The token is returned to the caller only;
this module never logs it; the only CLI mode that emits it is `auth` (stdout, for capture).

  vault_conf.py url [DEFAULT]       -> prints the resolved base URL
  vault_conf.py auth                -> prints 'Bearer <token>' on stdout (capture it; never pass it in argv)
  vault_conf.py --self-test
"""
import os
import sys
from pathlib import Path

CONF_PATH = Path.home() / ".config" / "vault" / "env"
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "ollama-worker" / "obsidian-token"
LEGACY_URL = "http://198.51.100.74:27123"


def read_env_file(path=None) -> dict:
    out = {}
    try:
        for ln in Path(path or CONF_PATH).read_text().splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, _, v = ln.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def vault_url(default=LEGACY_URL, env=None, conf_path=None) -> str:
    e = os.environ if env is None else env
    url = e.get("VAULT_URL") or read_env_file(conf_path).get("VAULT_URL") or e.get("OBSIDIAN_URL") or default
    return url.rstrip("/")


def token_file(env=None, conf_path=None) -> Path:
    e = os.environ if env is None else env
    p = e.get("VAULT_TOKEN_FILE") or read_env_file(conf_path).get("VAULT_TOKEN_FILE")
    return Path(os.path.expanduser(p)) if p else DEFAULT_TOKEN_FILE


def vault_token(env=None, conf_path=None) -> str:
    """Raw bearer token (no 'Bearer ' prefix), or '' if unavailable."""
    e = os.environ if env is None else env
    tok = e.get("OBSIDIAN_TOKEN", "")
    if not tok:
        try:
            tok = token_file(env, conf_path).read_text().strip()
        except OSError:
            tok = ""
    return tok[7:].strip() if tok.lower().startswith("bearer ") else tok


def _self_test() -> bool:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        if got == want:
            print(f"PASS {name}")
        else:
            ok = False
            print(f"FAIL {name}: got {got!r} want {want!r}")

    td = Path(tempfile.mkdtemp(prefix="vault-conf-selftest-"))
    conf = td / "env"
    tf = td / "tok"
    tf.write_text("Bearer s3cret\n")
    check("no env/file -> caller default (pre-cutover behaviour)", vault_url("http://x:1", {}, td / "none"), "http://x:1")
    check("no env/file -> default token file", token_file({}, td / "none"), DEFAULT_TOKEN_FILE)
    conf.write_text(f"# c\nVAULT_URL=http://shim:27123/\nVAULT_TOKEN_FILE={tf}\n")
    check("env file URL used (trailing slash stripped)", vault_url("http://x:1", {}, conf), "http://shim:27123")
    check("env var beats env file", vault_url("d", {"VAULT_URL": "http://e:2"}, conf), "http://e:2")
    check("env file token file used", token_file({}, conf), tf)
    check("token read from file, 'Bearer ' prefix stripped", vault_token({}, conf), "s3cret")
    check("legacy OBSIDIAN_TOKEN env still wins", vault_token({"OBSIDIAN_TOKEN": "abc"}, conf), "abc")
    check("missing token file -> empty, no raise", vault_token({"VAULT_TOKEN_FILE": str(td / "nope")}, td / "none"), "")
    import shutil
    shutil.rmtree(td, ignore_errors=True)
    print("SELF_TEST_OK" if ok else "SELF_TEST_FAILED")
    return ok


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["--self-test"]:
        sys.exit(0 if _self_test() else 1)
    if a[:1] == ["url"]:
        print(vault_url(a[1] if len(a) > 1 else LEGACY_URL))
    elif a[:1] == ["auth"]:
        t = vault_token()
        if not t:
            sys.exit(1)
        print(f"Bearer {t}")
    else:
        sys.exit(__doc__)
