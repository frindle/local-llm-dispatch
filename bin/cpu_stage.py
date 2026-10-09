#!/usr/bin/env python3
"""cpu_stage.py -- the pipeline's single entry point for moving a WHOLE CPU stage onto the
Unraid CPU runner, with an automatic, loud fallback to the stage's own local code.

    import cpu_stage
    r = cpu_stage.run_stage(wt, "bash verify.sh", 600, stage="preflight-verify",
                            bundle_id=cpu_stage.bundle_key(wt=wt),
                            local_fn=lambda: (rc, stdout, stderr, timed_out))
    r.exit_code, r.stdout, r.stderr, r.timed_out, r.ran_on, r.why

Contract (Phase 6, 2026-10-08):
  * ONE log line per stage on stderr (+ JSONL ~/.ollama-dispatch/cpu-lane.log):
        [cpu-lane] stage=X path=runner|local reason=... snap=... exit=... wall=...
  * KILL SWITCH: env CPU_LANE=0|off|false|no, or the file ~/.ollama-dispatch/cpu-lane.disabled,
    forces the local path with NO lane markers at all (exactly the pre-lane behaviour).
  * The local path is the CALLER'S OWN code (`local_fn`), never a re-implementation, so a
    fallback is byte-for-byte what the stage always produced.
  * ELIGIBILITY (the runner has NO network and a different toolchain): only node projects
    with a package-lock.json whose verify needs nothing but what the runner has. Anything
    that needs network, secrets, host paths we cannot ship, sqlite3 or a venv/pip install runs
    local (see eligibility()). First `npm ci` happens inside the runner's dep phase (network
    allowed there ONLY, cached by lockfile hash); the stage itself never installs.
  * TREE-LOCK / STALE SNAPSHOTS: callers keep holding their worktree tree lock around
    run_stage (the runner works on a shipped copy, the lock keeps protecting the real
    target meanwhile). A snapshot hash = sha256(HEAD sha + `git diff --binary` of a throwaway
    index) is taken BEFORE shipping and re-checked AFTER the result lands; if the worktree
    moved meanwhile (a writer ignoring the lock) the remote result is DISCARDED and the stage
    re-runs locally. The hash also travels in the job env (DISPATCH_SNAPSHOT) and label.
  * Never raises for lane/infrastructure reasons.

Seams are test-patchable: `_submit`, `_runner_online`, `_marker`, `snapshot`.
"""
import base64
import gzip
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path.home()
STATE_DIR = HOME / ".ollama-dispatch"
DISABLE_FILE = STATE_DIR / "cpu-lane.disabled"
LOG_FILE = STATE_DIR / "cpu-lane.log"
HOST_BIN = str(HOME / "bin")

# Tools every remote stage may need, shipped under $JOB_TOOLS/bin (and $HOME/bin -> that dir
# on the runner, so the stages' own `Path.home()/"bin"/X` lookups resolve unchanged).
_TOOL_FILES = ("verify-relevance.py", "pgrun.py", "source_text_harness.py", "plan_lint.py",
               "ollama-dispatch-scaffold", "cpu_vr_measure.py")
_TS_FILES = ("ts-mutator/ts-parse.mjs", "ts-mutator/ts-mutator.mjs")
_TS_PKG = ("ts-mutator/node_modules/typescript/package.json",
           "ts-mutator/node_modules/typescript/lib/typescript.js")

# Host paths a verify may legitimately name are exactly the ones host_tools() ships (anything else
# under /Users, $HOME or ~ makes the stage ineligible).
_NEVER_RE = re.compile(r"security\s+find-|osascript|launchctl|\bbrew\b|\bpip3?\s+install|\bcurl\b|\bwget\b|"
                       r"\bgit\s+(push|fetch|clone|pull)\b|\bssh\b|keychain", re.I)
_HOSTPATH_RE = re.compile(r"(/Users/[^\s'\"):;]+|\$HOME[/\w.-]*|\$\{HOME\}[/\w.-]*|~/[\w./-]*)")
_TEXT_HARNESS = ("verify.sh", "check_literals.py", "auto-harness-check.py", "refimpl.py")


class StageResult:
    def __init__(self, exit_code, stdout="", stderr="", timed_out=False, ran_on="local", why="",
                 job_id=None, snap=None, wall=0.0, timings=None):
        self.exit_code, self.stdout, self.stderr, self.timed_out = exit_code, stdout, stderr, timed_out
        self.ran_on, self.why, self.job_id, self.snap, self.wall = ran_on, why, job_id, snap, wall
        self.timings = timings or {}

    @property
    def fell_back(self):
        return self.ran_on == "local" and bool(self.why)

    def __repr__(self):
        return "StageResult(exit=%r ran_on=%s why=%r)" % (self.exit_code, self.ran_on, self.why)


# ----------------------------------------------------------------------------- switches
def kill_switch():
    """(True, why) when the lane is disabled."""
    v = (os.environ.get("CPU_LANE") or "").strip().lower()
    if v in ("0", "off", "false", "no", "disabled"):
        return True, "kill switch: CPU_LANE=%s" % v
    try:
        if DISABLE_FILE.exists():
            return True, "kill switch: %s exists" % DISABLE_FILE
    except OSError:
        pass
    return False, ""


def _log(stage, bundle, path, why, snap, exit_code, wall, job_id=None):
    line = "[cpu-lane] stage=%s bundle=%s path=%s reason=%s snap=%s exit=%s wall=%.1fs%s" % (
        stage, bundle or "-", path, why or "ok", (snap or "-")[:14], exit_code, wall,
        (" job=" + job_id) if job_id else "")
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        pass
    if os.environ.get("CPU_LANE_LOG_OFF") == "1":      # canary/tests must not pollute the real lane log
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps({"t": int(time.time()), "stage": stage, "bundle": bundle, "path": path,
                                "reason": why, "snap": snap, "exit": exit_code,
                                "wall": round(wall, 2), "job": job_id}) + "\n")
    except Exception:
        pass


# ----------------------------------------------------------------------------- bundle key
def bundle_key(label=None, explicit=None, wt=None):
    """The queue bundle key (what `pk(j)` / job_group_key return) so the lane release matches
    the bundle's rows. explicit tag wins; $DISPATCH_BUNDLE next; else job_group_key(label);
    else a label guessed from the worktree dir name (wt-slice-<plan>-<sid>-..., wt-<label>)."""
    tag = (explicit or os.environ.get("DISPATCH_BUNDLE") or "").strip()
    if tag:
        return tag
    if not label and wt:
        n = Path(str(wt)).name
        for p in ("wt-slice-", "wt-"):
            if n.startswith(p):
                n = n[len(p):]
                break
        label = n
    if not label:
        return None
    try:
        import importlib.util as iu
        sp = iu.spec_from_file_location("_olq_bundle_key", str(HERE / "ollama-queue.py"))
        m = iu.module_from_spec(sp)
        sp.loader.exec_module(m)
        return m.job_group_key({"label": label}) or label
    except Exception:
        return label


# ----------------------------------------------------------------------------- snapshot
def _git(wt, *args, env=None):
    r = subprocess.run(["git", "-C", str(wt)] + list(args), capture_output=True, env=env, timeout=120)
    if r.returncode:
        raise RuntimeError("git %s: %s" % (args[0], r.stderr[-200:].decode("utf-8", "replace")))
    return r.stdout


def snapshot(wt):
    """HEAD sha + hash of (tracked + untracked non-ignored) changes, via a throwaway index --
    the same view cpu_job.make_payload ships. Never touches the real index."""
    head = _git(wt, "rev-parse", "--verify", "HEAD").decode().strip()
    with tempfile.TemporaryDirectory() as td:
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(td, "idx"))
        _git(wt, "read-tree", "HEAD", env=env)
        _git(wt, "add", "-A", "--", ".", env=env)
        patch = _git(wt, "diff", "--cached", "--binary", "HEAD", env=env)
    return "%s:%s" % (head[:12], hashlib.sha256(patch).hexdigest()[:24])


# ----------------------------------------------------------------------------- eligibility
_SHIPPED_CACHE = []


def _shipped_host_paths():
    """Host paths (under ~/bin) the runner gets via host_tools(), so a verify naming them is fine."""
    if not _SHIPPED_CACHE:
        _SHIPPED_CACHE.append({HOST_BIN + "/" + n for n in _TOOL_FILES + _TS_FILES + _TS_PKG}
                              | {HOST_BIN + "/ollama-queue.py"})
    return _SHIPPED_CACHE[0]


def _find_pkg_dir(wt, cwd_rel):
    d = (Path(wt) / (cwd_rel or ".")).resolve()
    root = Path(wt).resolve()
    while True:
        if (d / "package.json").is_file():
            return d
        if d == root or root not in d.parents:
            return None
        d = d.parent


_BIN_PKG = {"tsc": "typescript", "tsserver": "typescript"}     # binary name -> npm package
_SHIPPED_NPX = {"tsx"}          # tools we can put in node_modules/.bin on the runner (see tsx_bundle)


def _lock_packages(pkg_dir):
    try:
        d = json.loads((pkg_dir / "package-lock.json").read_text())
        return {k.split("node_modules/")[-1] for k in (d.get("packages") or {}) if k}
    except Exception:
        return set()


def tsx_needed(wt, cmd, cwd_rel=None):
    """True when a verify text falls back to `npx --yes tsx` and the lockfile does not carry tsx."""
    pkg = _find_pkg_dir(wt, cwd_rel)
    if pkg is None:
        return False
    if "tsx" in _lock_packages(pkg):
        return False
    for n in _TEXT_HARNESS:
        p = Path(wt) / n
        try:
            if p.is_file() and re.search(r"npx\s+(?:--yes|-y)\s+tsx\b", p.read_text(errors="replace")):
                return True
        except OSError:
            pass
    return bool(re.search(r"npx\s+(?:--yes|-y)\s+tsx\b", cmd if isinstance(cmd, str) else " ".join(cmd)))


TSX_BUNDLE = STATE_DIR / "cpu-lane" / "tsx-linux-x64.tgz"


def tsx_bundle():
    """Path of a linux-x64 tsx (+esbuild) tarball, built once on the host with network (cached).
    None when it cannot be built -> the stage stays local."""
    if TSX_BUNDLE.is_file() and TSX_BUNDLE.stat().st_size > 1_000_000:
        return str(TSX_BUNDLE)
    try:
        TSX_BUNDLE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["npm", "init", "-y"], cwd=td, capture_output=True, timeout=60, check=True)
            subprocess.run(["npm", "i", "--no-audit", "--no-fund", "--os=linux", "--cpu=x64", "tsx"],
                           cwd=td, capture_output=True, timeout=240, check=True)
            tmp = str(TSX_BUNDLE) + ".tmp"
            subprocess.run(["tar", "czf", tmp, "-C", td, "node_modules"], capture_output=True,
                           timeout=120, check=True)
            os.replace(tmp, str(TSX_BUNDLE))
        return str(TSX_BUNDLE)
    except Exception:
        return None


def eligibility(wt, cmd, cwd_rel=None, shipped_extra=()):
    """(ok, why). Conservative: anything the runner cannot reproduce faithfully stays local.
    shipped_extra: tool names ('bin/vr-args.json') the caller ships beside host_tools(); a cmd naming
    $HOME/bin/<that> is fine (2026-10-09: the relevance stage named its own shipped args file and was
    declared host-only -> ran 17 min locally)."""
    extra = {HOST_BIN + "/" + n[4:] for n in (shipped_extra or ()) if n.startswith("bin/")}
    wt = Path(wt)
    pkg = _find_pkg_dir(wt, cwd_rel)
    if pkg is None:
        return False, "not a node project (no package.json): runner python/toolchain differs from the host"
    if not (pkg / "package-lock.json").is_file():
        return False, "package.json without package-lock.json: the runner cannot install offline"
    texts = {"cmd": cmd if isinstance(cmd, str) else " ".join(cmd)}
    for n in _TEXT_HARNESS:
        p = wt / n
        if p.is_file():
            try:
                texts[n] = p.read_text(errors="replace")
            except OSError:
                pass
    for _ft in sorted(wt.glob("verify.test.*")):
        try:
            _s = _ft.read_text(errors="replace")
        except OSError:
            continue
        if re.search(r"\bmock\.module\(", _s):
            # 2026-10-09 (rt-walmart-cancel-import): the runner has node 22, whose mock.module takes
            # namedExports/defaultExport; these fixtures use the node 23+ `{ exports }` shape, so every
            # case 500s on the runner while passing on the host (node 26). Keep these stages local.
            return False, "%s uses node:test mock.module (runner node 22 API differs from host node 26)" % _ft.name
    for n, t in texts.items():
        # comments are not behaviour: drop whole-line comments before scanning
        t = "\n".join(l for l in t.splitlines() if not l.lstrip().startswith(("#", "//")))
        m = _NEVER_RE.search(t)
        if m:
            return False, "%s needs network/secrets/host services (%s)" % (n, m.group(0).strip())
        for hp in _HOSTPATH_RE.findall(t):
            ship = _shipped_host_paths() | extra
            if hp in ship or (
                    hp.startswith(("~/bin/", "$HOME/bin/", "${HOME}/bin/")) and
                    HOST_BIN + "/" + hp.split("/bin/", 1)[1] in ship):
                continue
            return False, "%s names a host-only path (%s)" % (n, hp[:80])
    # `npx --yes X` fallbacks: the runner has no network, so X must come from the lockfile install
    # (or be a tool we ship: tsx).
    lock_pk = _lock_packages(pkg)
    for n, t in texts.items():
        for tool in re.findall(r"npx\s+(?:--yes|-y)\s+(@?[\w./-]+)", t):
            if tool in lock_pk or _BIN_PKG.get(tool) in lock_pk or (tool in _SHIPPED_NPX and tsx_bundle()):
                continue
            return False, "%s runs `npx --yes %s` but %s is not in package-lock.json (needs network)" % (n, tool, tool)
    # the prisma migration gate in the scaffold's verify.sh shells out to sqlite3 (not in the image)
    # only when migrations/schema changed; replicate its trigger.
    if "sqlite3" in texts.get("verify.sh", "") and not os.environ.get("CPU_RUNNER_HAS_SQLITE"):
        try:
            st = _git(wt, "status", "--porcelain", "--untracked-files=all", "--", "prisma").decode()
        except Exception:
            st = "?"
        if st.strip():
            return False, "prisma schema/migrations changed and the runner image has no sqlite3"
    return True, ""


# ----------------------------------------------------------------------------- tools + cmd
def _scaffold_basenames_stub():
    """A stand-in ollama-queue.py (verify-relevance imports it for _SCAFFOLD_BASENAMES) built
    from the REAL value, so the runner copy agrees with the host byte for byte."""
    try:
        import importlib.util as iu
        sp = iu.spec_from_file_location("_olq_stub_src", str(HERE / "ollama-queue.py"))
        m = iu.module_from_spec(sp)
        sp.loader.exec_module(m)
        names = sorted(getattr(m, "_SCAFFOLD_BASENAMES", None) or [])
        if names:
            return ("_SCAFFOLD_BASENAMES = frozenset(%r)\n" % (names,)).encode()
    except Exception:
        pass
    return None


def host_tools(extra=None):
    """{name: path|bytes} for cpu_job.make_tools_tgz; every name lives under bin/ (-> $HOME/bin)."""
    t = {}
    for n in _TOOL_FILES:
        p = HERE / n
        if p.is_file():
            t["bin/" + n] = str(p)
    for n in _TS_FILES + _TS_PKG:
        p = HERE / n
        if p.is_file():
            t["bin/" + n] = str(p)
    stub = _scaffold_basenames_stub()
    if stub:
        t["bin/ollama-queue.py"] = stub
    t.update(extra or {})
    return t


def remote_cmd(cmd, rewrite=True, group=False, harness=False):
    """Wrap `cmd` for the runner: $HOME/bin -> the shipped tools; host /Users/<u>/bin/ paths in the
    harness files of the (shipped copy only) rewritten to the same place. Plain POSIX sh/bash."""
    pre = ('mkdir -p "$HOME" && ln -sfn "$JOB_TOOLS/bin" "$HOME/bin"; ')
    if harness:
        # .dispatch-harness.json is git-excluded (info/exclude) so the snapshot never carries it, yet
        # verify-relevance --applied reads it (creation_task target) -> on the runner it saw "no tracked
        # diff" (rc=3) for every creation task. Restore the shipped copy at the checkout root.
        pre += ('_r=$(git rev-parse --show-toplevel 2>/dev/null); [ -n "$_r" ] && [ ! -e "$_r/.dispatch-harness.json" ] '
                '&& cp "$JOB_TOOLS/bin/dispatch-harness.json" "$_r/.dispatch-harness.json"; ')
    if "auto-harness-check.py" in (cmd if isinstance(cmd, str) else " ".join(cmd)):
        # auto-harness-check.py is git-excluded too (info/exclude), so the snapshot never carries it:
        # on the runner `python3 auto-harness-check.py` died "No such file" (rc=2) and --resume-harness
        # refused a converged harness. Restore the shipped copy at the checkout root.
        pre += ('_r=$(git rev-parse --show-toplevel 2>/dev/null); [ -n "$_r" ] && [ ! -e "$_r/auto-harness-check.py" ] '
                '&& cp "$JOB_TOOLS/bin/auto-harness-check.py" "$_r/auto-harness-check.py"; ')
    if rewrite:
        hb = shlex.quote(HOST_BIN + "/")
        pre += ('for _f in $(grep -lIsF %s ./* 2>/dev/null); do '
                'sed -i "s#%s#$HOME/bin/#g" "$_f"; done; ' % (hb, HOST_BIN + "/"))
    body = cmd if isinstance(cmd, str) else " ".join(shlex.quote(c) for c in cmd)
    return pre + (("( " + body + "\n)") if group else body)


def passthrough_env(extra=None):
    """The env a local run would see that the runner will accept (VERIFY_*, DISPATCH_*, TEST_*, TZ...)."""
    e = {k: v for k, v in os.environ.items() if re.match(r"^(VERIFY_|DISPATCH_|TEST_)", k)}
    e.update({k: str(v) for k, v in (extra or {}).items()})
    return e


# ----------------------------------------------------------------------------- remote seam
def _runner_online():
    try:
        import cpu_dispatch as cd
        cj = cd._load_client()
        api = os.environ.get("CPU_RUNNER_API") or cd.DEFAULT_API
        return cd._runner_online(api, cj._token()), api, cj
    except Exception:
        return False, None, None


def _marker(stage, bundle_id, timeout_s):
    try:
        import cpu_dispatch as cd
        return cd._marker(stage, bundle_id, timeout_s)
    except Exception:
        return lambda code=None: None


def _submit(cj, api, wt, cmd, timeout_s, stage, bundle_id, tools, env, cwd_rel, label, claim_timeout_s):
    """Raises RuntimeError when the runner path is unavailable (cpu_job fallback=False)."""
    return cj.submit_cpu_job(wt, cmd, timeout_s, cwd_rel=cwd_rel, env=env, stage=stage, label=label,
                             bundle_id=bundle_id, tools=tools, mode="bundle", api=api,
                             claim_timeout_s=claim_timeout_s, fallback=False)


def decode_gzb64(s):
    return gzip.decompress(base64.b64decode((s or "").strip())).decode("utf-8", "replace")


GZB64_SUFFIX = (' | python3 -c "import sys,gzip,base64;'
                'sys.stdout.write(base64.b64encode(gzip.compress(sys.stdin.buffer.read())).decode())"')


# ----------------------------------------------------------------------------- the entry point
def _norm_local(v):
    if isinstance(v, StageResult):
        return v
    rc, so, se, to = (list(v) + [False])[:4] if len(v) == 3 else v
    return StageResult(rc, so or "", se or "", bool(to), "local")


def run_stage(wt, cmd, timeout_s, stage, bundle_id, local_fn, *, tools=None, env=None, cwd_rel=None,
              check=None, claim_timeout_s=60, gzb64=False, tolerate_nonzero=True):
    """Run one CPU stage remotely when it can faithfully run there, else via `local_fn()` (a callable
    returning (exit_code, stdout, stderr, timed_out) or a StageResult). `check(StageResult)` may veto
    a remote result (return a reason string) -> local re-run. gzb64: the command's stdout is
    gzip+base64 transported (big JSON) and decoded here."""
    t0 = time.time()
    bundle_id = bundle_id or bundle_key(wt=wt)
    off, why = kill_switch()
    if off:
        r = _norm_local(local_fn())
        r.why = why
        _log(stage, bundle_id, "local", why, None, r.exit_code, time.time() - t0)
        return r
    why = None
    snap0 = None
    job_id = None
    try:
        ok, why_e = eligibility(wt, cmd, cwd_rel, shipped_extra=tuple((tools or {}).keys()))
        if not ok:
            why = "ineligible: " + why_e
        else:
            online, api, cj = _runner_online()
            if not online or cj is None:
                why = "no CPU runner online"
            else:
                snap0 = snapshot(wt)
    except Exception as e:                               # noqa: BLE001
        why = "lane precheck error: %s: %s" % (type(e).__name__, e)
    if why is None:
        try:
            xt = dict(tools or {})
            pre = ""
            if tsx_needed(wt, cmd, cwd_rel):
                xt["bin/tsx-linux-x64.tgz"] = tsx_bundle()
                pre = ('mkdir -p "$JOB_TOOLS/tsx" && tar xzf "$JOB_TOOLS/bin/tsx-linux-x64.tgz" -C "$JOB_TOOLS/tsx" '
                       '&& mkdir -p node_modules/.bin && { [ -e node_modules/.bin/tsx ] || '
                       'ln -s "$JOB_TOOLS/tsx/node_modules/.bin/tsx" node_modules/.bin/tsx; }; ')
            hf = Path(wt) / ".dispatch-harness.json"
            if hf.is_file() and "bin/dispatch-harness.json" not in xt:
                try:
                    xt["bin/dispatch-harness.json"] = hf.read_bytes()
                except OSError:
                    pass
            ahc = Path(wt) / "auto-harness-check.py"
            if ahc.is_file() and "bin/auto-harness-check.py" not in xt and "auto-harness-check.py" in str(cmd):
                try:
                    xt["bin/auto-harness-check.py"] = ahc.read_bytes()
                except OSError:
                    pass
            full = pre + remote_cmd(cmd, group=gzb64, harness="bin/dispatch-harness.json" in xt) + (GZB64_SUFFIX if gzb64 else "")
            if gzb64:
                full = "set -o pipefail; " + full
            res = _submit(cj, api, str(wt), full, timeout_s, stage, bundle_id, host_tools(xt),
                          passthrough_env(dict(env or {}, DISPATCH_SNAPSHOT=snap0)), cwd_rel,
                          "%s@%s" % (stage, snap0), claim_timeout_s)
            job_id = getattr(res, "job_id", None)
            snap1 = snapshot(wt)
            if snap1 != snap0:
                why = "STALE SNAPSHOT: worktree changed while the runner ran it (%s -> %s) -- result discarded" % (
                    snap0[:20], snap1[:20])
            else:
                out = res.stdout_tail or ""
                if gzb64 and not res.timed_out:
                    try:
                        out = decode_gzb64(out)
                    except Exception as e:                # noqa: BLE001
                        why = "remote result undecodable (%s)" % type(e).__name__
                r = StageResult(res.exit_code, out, res.stderr_tail or "", bool(res.timed_out), "runner",
                                "", job_id, snap0, time.time() - t0, getattr(res, "timings", None))
                if why is None and check is not None:
                    veto = check(r)
                    if veto:
                        why = "remote result vetoed after %.0fs of runner work (re-running locally): %s" % (
                            time.time() - t0, veto)
                if why is None:
                    _log(stage, bundle_id, "runner", "", snap0, r.exit_code, r.wall, job_id)
                    return r
        except RuntimeError as e:
            why = str(e)
        except Exception as e:                            # noqa: BLE001
            why = "%s: %s" % (type(e).__name__, e)
    # ---- local path: the caller's own code, with a lane marker so we still do not hold GPU lanes
    # Default: NO marker on the local path -- the bundle holds its lane exactly as it did before the
    # lane existed (bundle-to-completion; a foreign bundle must not interleave just because the runner
    # was down). CPU_LANE_LOCAL_MARKER=1 opts in to freeing the lane during a local-fallback stage.
    if os.environ.get("CPU_LANE_LOCAL_MARKER") == "1":
        end = _marker(stage, bundle_id, timeout_s)
    else:
        end = (lambda c=None: None)
    code = None
    try:
        r = _norm_local(local_fn())
        code = r.exit_code
    finally:
        end(code)
    r.why = why or "local"
    r.snap = snap0
    _log(stage, bundle_id, "local", why, snap0, r.exit_code, time.time() - t0)
    return r
