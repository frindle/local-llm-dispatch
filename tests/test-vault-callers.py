#!/usr/bin/env python3
"""Tests for the vault callers' URL/token resolution + PUT-on-404 monthly roll, run against the
real vault-shim on loopback (temp VAULT_ROOT). Never touches the real vault/Unraid.

  test-vault-callers.py                # run the suite
  test-vault-callers.py --revert-tests # break each change in a temp copy; the suite must FAIL
Targets are read from $VC_BIN (default ~/bin) and $VC_MC (machine-config bin, for vault-log).
"""
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SHIM = Path("/Users/user/Desktop/GitHub Projects/vault-stack/shim/vault_shim.py")
BIN = Path(os.environ.get("VC_BIN", str(Path.home() / "bin")))
MC = Path(os.environ.get("VC_MC", "/Users/user/Desktop/GitHub Projects/machine-config/bin"))
SELF = Path(__file__).resolve()
UNREACHABLE = "http://127.0.0.1:9"      # revert mutants must never reach the real vault
TOKEN = "test-token-NOT-REAL-1234567890"


def main():
    if "--revert-tests" in sys.argv:
        return revert_tests()
    return 0 if suite() else 1


def suite() -> bool:
    ok = True
    n = {"pass": 0}

    def check(name, got, want=True):
        nonlocal ok
        if got == want:
            n["pass"] += 1
            print(f"PASS {name}")
        else:
            ok = False
            print(f"FAIL {name}: got {got!r} want {want!r}")

    td = Path(tempfile.mkdtemp(prefix="vault-callers-test-"))
    home = td / "home"
    (home / ".config" / "vault").mkdir(parents=True)
    (home / ".config" / "ollama-worker").mkdir(parents=True)
    root = td / "root"; (root / "Claude").mkdir(parents=True)
    (td / "state").mkdir()
    tokf = td / "shim-token"; tokf.write_text(TOKEN + "\n")
    # legacy token file (pre-cutover location) holds a DIFFERENT value: proves env-file token file wins
    (home / ".config" / "ollama-worker" / "obsidian-token").write_text("legacy-token\n")
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    url = f"http://127.0.0.1:{port}"
    (home / ".config" / "vault" / "env").write_text(f"VAULT_URL={url}\nVAULT_TOKEN_FILE={tokf}\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VAULT_", "OBSIDIAN_"))}
    env.update(HOME=str(home))
    shim = subprocess.Popen(
        [sys.executable, str(SHIM)], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        env={**env, "VAULT_ROOT": str(root), "VAULT_TOKEN_FILE": str(tokf), "SHIM_BIND": "127.0.0.1",
             "SHIM_PORT": str(port), "STATE_DIR": str(td / "state")})
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(url + "/healthz", timeout=1).read(); break
            except Exception:
                time.sleep(0.1)
        else:
            print("FAIL shim did not start"); return False

        month = f"{datetime.now(timezone.utc):%Y-%m}"
        logp = root / "Claude" / f"Agent-Dispatch-Log-{month}.md"

        # ---- 1. worker: env-file URL+token, PUT-on-404 roll, then append ----
        os.environ.clear(); os.environ.update(env)
        sys.path.insert(0, str(BIN))
        sys.modules.pop("vault_conf", None)
        spec = importlib.util.spec_from_file_location("ow_under_test", BIN / "ollama-worker.py")
        ow = importlib.util.module_from_spec(spec); spec.loader.exec_module(ow)
        ow.log = lambda *a, **k: None
        u, t = ow._vault_endpoint()
        check("worker resolves URL from ~/.config/vault/env", u, url)
        check("worker resolves token from the env file's token file (not the legacy file)", t, TOKEN)
        ow.log_dispatch_to_obsidian("m1", "task one", True, True, Path("/tmp/x.json"), dispatch_tag="j1")
        check("worker: missing monthly note is CREATED via PUT-on-404 roll", logp.exists())
        body = logp.read_text() if logp.exists() else ""
        check("rolled note has header + first entry", ("# Agent Dispatch Log" in body) and ("id=`j1`" in body))
        ow.log_dispatch_to_obsidian("m2", "task two", False, False, Path("/tmp/y.json"), dispatch_tag="j2")
        body2 = logp.read_text() if logp.exists() else ""
        check("second dispatch APPENDS (entry 1 kept, entry 2 added)", ("id=`j1`" in body2) and ("id=`j2`" in body2))
        check("append did not re-write the header", body2.count("# Agent Dispatch Log"), 1)
        # non-fatal on failure: dead URL + env var override must not raise
        os.environ["VAULT_URL"] = "http://127.0.0.1:1"
        try:
            ow.log_dispatch_to_obsidian("m3", "t", True, True, Path("/tmp/z"))
            check("worker logging failure is non-fatal", True)
        except Exception as e:
            check("worker logging failure is non-fatal", repr(e), "no raise")
        del os.environ["VAULT_URL"]
        # pre-cutover: no env file -> legacy constants (no network here)
        (home / ".config" / "vault" / "env").rename(td / "env.off")
        os.environ.pop("OBSIDIAN_TOKEN", None)
        u2, t2 = ow._vault_endpoint()
        check("no env file -> legacy URL (nothing breaks before cutover)", u2, "http://198.51.100.74:27123")
        check("no env file -> legacy token file", t2, "legacy-token")
        (td / "env.off").rename(home / ".config" / "vault" / "env")

        # ---- 2. vault-log (machine-config copy) ----
        (root / "Claude" / "Decision-Log.md").write_text("# Decisions\n")
        r = subprocess.run(["bash", str(MC / "vault-log"), "decision", "wired to shim", "because"],
                           env=env, input="", capture_output=True, text=True, timeout=60)
        check("vault-log decision exits 0 against the shim", r.returncode, 0)
        check("vault-log appended the decision", "wired to shim" in (root / "Claude" / "Decision-Log.md").read_text())
        check("vault-log never prints the token", TOKEN in (r.stdout + r.stderr), False)
        src = (MC / "vault-log").read_text()
        check("vault-log keeps the token out of argv (no -H Authorization)", '-H "Authorization' in src, False)

        # ---- 3. vault-semantic list/read through the shim ----
        (root / "Claude" / "n.md").write_text("hello " * 20)
        r = subprocess.run([sys.executable, "-c",
            "import importlib.util,sys;sys.path.insert(0,sys.argv[1]);"
            "s=importlib.util.spec_from_file_location('vs',sys.argv[1]+'/vault-semantic.py');m=importlib.util.module_from_spec(s);"
            "s.loader.exec_module(m);print(m.OBSIDIAN);print(m.list_notes('Claude'))", str(BIN)],
            env=env, capture_output=True, text=True, timeout=60)
        check("vault-semantic uses env-file URL", url in r.stdout)
        check("vault-semantic lists notes through the shim w/ token", "Claude/n.md" in r.stdout)

        # ---- 4. session-summary-index --via-shim PUT ----
        r = subprocess.run([sys.executable, "-c",
            "import importlib.util,sys;s=importlib.util.spec_from_file_location('ss',sys.argv[1]+'/session-summary-index.py');"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m.shim_put('t-1.md','---\\nx: 1\\n---\\nbody')", str(BIN)],
            env=env, capture_output=True, text=True, timeout=60)
        check("session-summary-index shim_put succeeds", r.returncode, 0)
        check("...and the note landed in Claude/SessionIndex", (root / "Claude" / "SessionIndex" / "t-1.md").exists())
    finally:
        shim.terminate(); shim.wait(timeout=10)
        shutil.rmtree(td, ignore_errors=True)
    print(f"{n['pass']} passed")
    print("SUITE_OK" if ok else "SUITE_FAILED")
    return ok


def revert_tests() -> int:
    """Mutate a temp copy of each target; the suite must FAIL for every mutant."""
    muts = [
        ("worker ignores _vault_endpoint (uses legacy URL)", "ollama-worker.py",
         "vault_conf.vault_url(OBSIDIAN_URL)", "OBSIDIAN_URL"),
        ("worker: PUT-on-404 roll removed", "ollama-worker.py", "if he.code == 404:", "if False:"),
        ("worker: append becomes PUT (clobbers log)", "ollama-worker.py",
         'data=entry.encode(), method="POST"', 'data=entry.encode(), method="PUT"'),
        ("vault_conf: env file ignored for URL", "vault_conf.py",
         'or read_env_file(conf_path).get("VAULT_URL")', ""),
        ("vault_conf: token file from env file ignored", "vault_conf.py",
         'or read_env_file(conf_path).get("VAULT_TOKEN_FILE")', ""),
        ("vault-semantic: URL hardwired", "vault-semantic.py",
         'vault_conf.vault_url("http://198.51.100.74:27123")', f'"{UNREACHABLE}"'),
        ("session-summary-index: URL hardwired", "session-summary-index.py",
         "{vault_conf.vault_url()}", UNREACHABLE),
        ("vault-log: token source reverted to ~/.claude.json only", "vault-log",
         'TOK="$(python3 "$CONF" auth 2>/dev/null)"', 'TOK=""'),
        ("vault-log: URL hardwired", "vault-log",
         'HOST="$(python3 "$CONF" url "${OBSIDIAN_HOST:-http://198.51.100.74:27123}" 2>/dev/null)"',
         f'HOST="{UNREACHABLE}"'),
    ]
    bad = 0
    for name, fn, old, new in muts:
        td = Path(tempfile.mkdtemp(prefix="vault-callers-mut-"))
        try:
            for f in ("ollama-worker.py", "vault-semantic.py", "session-summary-index.py", "vault_conf.py"):
                shutil.copy(BIN / f, td / f)
            shutil.copy(MC / "vault-log", td / "vault-log"); shutil.copy(BIN / "vault_conf.py", td / "vault_conf.py")
            for f in td.iterdir():  # no mutant may ever address the real vault
                t = f.read_text()
                if f.name != "vault_conf.py" or True:
                    f.write_text(t.replace("http://198.51.100.74:27123", UNREACHABLE).replace("198.51.100.74:27123", "127.0.0.1:9"))
            p = td / fn
            src = (BIN / fn if fn != "vault-log" else MC / fn).read_text()
            if old.replace("http://198.51.100.74:27123", UNREACHABLE) not in p.read_text() and old not in p.read_text():
                print(f"BROKEN-MUTANT (pattern missing) {name}"); bad += 1; continue
            t = p.read_text()
            t = t.replace(old.replace("http://198.51.100.74:27123", UNREACHABLE), new) if old.replace("http://198.51.100.74:27123", UNREACHABLE) in t else t.replace(old, new)
            p.write_text(t)
            r = subprocess.run([sys.executable, str(SELF)], env={**os.environ, "VC_BIN": str(td), "VC_MC": str(td)},
                               capture_output=True, text=True, timeout=180)
            if r.returncode != 0:
                print(f"REVERT-OK (suite fails) : {name}")
            else:
                print(f"REVERT-MISSED (suite still passes) : {name}"); bad += 1
        finally:
            shutil.rmtree(td, ignore_errors=True)
    print("REVERT_TESTS_OK" if not bad else f"REVERT_TESTS_FAILED ({bad})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
