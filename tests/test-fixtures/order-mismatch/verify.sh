#!/usr/bin/env bash
# verify for: smoke-order-mismatch (--ts-runner node-test)
# Counting idiom, NOT `set -e` -- an aborting verify never prints why it failed.
cd "$(dirname "$0")" || exit 1

fails=0
NODE=$(command -v node)     # the queue daemon runs under launchd's PATH
[ -n "$NODE" ] || { echo "  FAIL: node not on PATH"; exit 1; }

echo "=== env bootstrap ==="
# A DANGLING node_modules symlink (the scaffold linked the worktree to the
# source, then the source's node_modules went away) is `[ ! -d ]`-true, so the
# old check fell through to `npm ci`, which then errors on the pre-existing
# symlink path. Remove a broken symlink first so npm can install cleanly.
if [ -L ./node_modules ] && [ ! -e ./node_modules ]; then
  echo "  node_modules is a dangling symlink -- removing so npm can install"
  rm -f ./node_modules
fi
# NO package.json (a fresh --new-project TS build, 2026-10-02 idle-test-tsslug):
# there are no deps to install. `npm install` there FAILS (rc 254) yet leaves an
# empty package-lock.json, so every later run took the `npm ci` branch and failed
# too -- one permanent verify failure no harness could clear. tsx/tsc come from PATH.
if [ ! -f package.json ]; then
  echo "  ok: no package.json -- no npm deps to install (tsx/tsc from PATH)"
elif [ ! -d ./node_modules ]; then
  echo "  node_modules absent -- npm ci for env parity (tsc/tests need deps)"
  if [ -f package-lock.json ]; then _NPM="npm ci"; else _NPM="npm install"; fi
  if $_NPM >/tmp/_verify_npm.$$.log 2>&1; then echo "  ok: $_NPM"; else echo "  FAIL: $_NPM failed"; tail -20 /tmp/_verify_npm.$$.log; fails=$((fails+1)); fi
  rm -f /tmp/_verify_npm.$$.log
fi
_SCHEMA=""
[ -f prisma/schema.prisma ] && _SCHEMA=prisma/schema.prisma
[ -z "$_SCHEMA" ] && _SCHEMA=$(ls prisma/schema/*.prisma 2>/dev/null | head -1)
if [ -n "$_SCHEMA" ] && grep -q "generator" "$_SCHEMA" 2>/dev/null; then
  _OUT=$(grep -oE 'output[[:space:]]*=[[:space:]]*"[^"]+"' "$_SCHEMA" | head -1 | sed -E 's/.*"([^"]+)"/\1/')
  _SDIR=$(dirname "$_SCHEMA")
  _SKIP_PRISMA=""
  case "$_OUT" in
    /*) _ABS="$_OUT" ;;
    "") _ABS="node_modules/.prisma/client"
        # Default output lands INSIDE node_modules. When that is symlinked to the
        # source checkout, `prisma generate` writes into the SOURCE repo (outside
        # this worktree, and wrong if the schemas differ). Skip rather than
        # corrupt the source -- generate in the source repo, or set a
        # worktree-local `output` in the schema's generator block.
        if [ -L node_modules ]; then _SKIP_PRISMA=1; fi ;;
    *)  _ABS="$_SDIR/$_OUT" ;;
  esac
  # STALE CLIENT (2026-10-05, rt-egift-link-s1-s0-db-schema 35803fc92ce8): a
  # client that is merely PRESENT was generated from the HEAD schema. A dispatch
  # that ADDS a model (or a refimpl that does) left `client.newModel` untyped, so
  # tsc failed a CORRECT target with TS2339 on every run -- unsatisfiable, and no
  # harness edit could clear it. Regenerate whenever the schema differs from what
  # the client was generated from: the stamp verify wrote last time, else HEAD.
  _SSRC="$_SCHEMA"; [ -f prisma/schema.prisma ] || _SSRC="$_SDIR"
  _sig() { if command -v shasum >/dev/null 2>&1; then shasum; else sha1sum; fi | cut -c1-40; }
  _CUR_SIG=$(cat "$_SDIR"/*.prisma 2>/dev/null | _sig)
  _STALE=""
  if [ -f "$_ABS/.verify-schema-sig" ]; then
    [ "$(cat "$_ABS/.verify-schema-sig")" = "$_CUR_SIG" ] || _STALE=1
  elif ! git diff --quiet HEAD -- "$_SSRC" 2>/dev/null; then
    _STALE=1
  fi
  if [ -n "$_SKIP_PRISMA" ]; then
    echo "  WARN: node_modules is symlinked to the source; skipping default-output prisma generate (would write into the source repo). Generate there, or set a worktree-local output in the schema."
  elif [ -d "$_ABS" ] && [ -n "$(ls -A "$_ABS" 2>/dev/null)" ] && [ -z "$_STALE" ]; then
    echo "  ok: prisma client present ($_ABS)"
  else
    if [ -n "$_STALE" ]; then echo "  prisma client STALE vs $_SSRC (schema changed) -- npx prisma generate"
    else echo "  prisma client missing ($_ABS) -- npx prisma generate"; fi
    if npx --yes prisma generate >/tmp/_verify_prisma.$$.log 2>&1; then echo "  ok: prisma generate"; echo "$_CUR_SIG" > "$_ABS/.verify-schema-sig" 2>/dev/null; else echo "  WARN: prisma generate failed (continuing; tsc may flood with TS7006)"; tail -20 /tmp/_verify_prisma.$$.log; fi
    rm -f /tmp/_verify_prisma.$$.log
  fi
fi

# --- prisma migration apply gate (the owner 2026-09-17) --------------------------
# WHY: a schema-only dispatch (rt-saved-address-schema, job 4ee80100599a) shipped
# a migration whose SQL was INVALID SQLite -- it declared `UNIQUE INDEX ... (...)`
# INSIDE `CREATE TABLE(...)` (valid form is a SEPARATE `CREATE UNIQUE INDEX`).
# `prisma validate` gates the SCHEMA, not the migration file, so the gate went
# GREEN on a migration that `sqlite3` rejects with a parse error. General fix:
# whenever THIS dispatch creates or changes any prisma/migrations/**/migration.sql,
# replay the WHOLE migration chain (in timestamp order, so earlier migrations that
# a new one depends on are present) into a throwaway sqlite db and FAIL on any
# error; then assert no drift vs schema.prisma via `prisma migrate diff`.
# No-op when the dispatch touched no migration -- safe to run in every verify.
if command -v git >/dev/null 2>&1 && command -v sqlite3 >/dev/null 2>&1; then
  # --untracked-files=all: git otherwise COLLAPSES a wholly-new migration dir to
  # `?? prisma/migrations/<dir>/`, hiding the migration.sql filename the grep keys
  # on -- the common case (a dispatch adds a brand-new migration directory).
  _CHANGED_MIG=$(git status --porcelain --untracked-files=all -- prisma/migrations 2>/dev/null | cut -c4- | grep -E '(^|/)migration\.sql$' || true)
  # SCHEMA CHANGED WITHOUT A MIGRATION (2026-10-05, rt-egift-link-s1-s0): the slice
  # added a model to schema.prisma and its TASK said "no migration.sql"; nothing
  # here ran (no migration changed), so a model the deployed DB never gets
  # (Dockerfile: prisma migrate deploy) would have gone green. A changed schema in
  # a repo that uses migrations also runs the replay + drift check below.
  _SCHEMA_CHG=""
  if [ -d prisma/migrations ] && [ -f prisma/schema.prisma ] && ! git diff --quiet HEAD -- prisma/schema.prisma 2>/dev/null; then _SCHEMA_CHG=1; fi
  if [ -n "$_CHANGED_MIG" ] || [ -n "$_SCHEMA_CHG" ]; then
    echo "=== prisma migrations apply to throwaway sqlite ==="
    if [ -n "$_CHANGED_MIG" ]; then echo "  changed/created migration(s) this dispatch:"; printf '%s\n' "$_CHANGED_MIG" | sed 's/^/    /'
    else echo "  prisma/schema.prisma changed and NO migration was added/changed -- the drift check below must still be empty"; fi
    _MIG_DB="/tmp/_verify_mig.$$.db"; rm -f "$_MIG_DB"
    _mig_ok=1
    for _m in $(find prisma/migrations -name migration.sql 2>/dev/null | LC_ALL=C sort); do
      if ! sqlite3 -bail "$_MIG_DB" < "$_m" >/tmp/_verify_mig.$$.log 2>&1; then
        echo "  FAIL: migration does not apply to sqlite: $_m"; sed 's/^/    /' /tmp/_verify_mig.$$.log; _mig_ok=0; break
      fi
    done
    if [ "$_mig_ok" = 1 ]; then echo "  ok: all migrations apply cleanly to sqlite"; else fails=$((fails+1)); fi
    rm -f "$_MIG_DB" /tmp/_verify_mig.$$.log
    # Bonus (drift): the replayed migration chain must reproduce schema.prisma.
    if [ -f prisma/schema.prisma ]; then
      if [ -x ./node_modules/.bin/prisma ]; then _PRISMA="./node_modules/.bin/prisma"; else _PRISMA="npx --yes prisma"; fi
      echo "=== prisma migrate diff (migrations vs schema.prisma, expect empty) ==="
      # Prisma 7 REMOVED --to-schema-datamodel (now --to-schema): the old flag made
      # this exit 1 -> "WARN could not run" on every Prisma-7 repo, so drift was
      # never asserted (2026-10-05). Pick the flag the installed CLI accepts.
      _DIFF_TO="--to-schema"
      if $_PRISMA migrate diff --help 2>&1 | grep -q -- '--to-schema-datamodel'; then _DIFF_TO="--to-schema-datamodel"; fi
      $_PRISMA migrate diff --from-migrations prisma/migrations $_DIFF_TO prisma/schema.prisma --exit-code >/tmp/_verify_diff.$$.log 2>&1
      _dc=$?
      if [ "$_dc" = 0 ]; then echo "  ok: no drift between migrations and schema.prisma"
      elif [ "$_dc" = 2 ]; then echo "  FAIL: migrations drift from schema.prisma (migrate diff non-empty)"; [ -z "$_CHANGED_MIG" ] && echo "    schema.prisma changed but no migration was added: create prisma/migrations/<YYYYMMDDHHMMSS>_<name>/migration.sql with the SQL below (the deploy runs prisma migrate deploy)"; grep -v -e '^Loaded Prisma config' -e '[│┌└]' /tmp/_verify_diff.$$.log | sed 's/^/    /' | head -40; fails=$((fails+1))
      else echo "  WARN: prisma migrate diff could not run (rc=$_dc) -- skipping drift assertion"; sed 's/^/    /' /tmp/_verify_diff.$$.log; fi
      rm -f /tmp/_verify_diff.$$.log
    fi
  fi
fi

if [ -x ./node_modules/.bin/tsx ]; then TSX="./node_modules/.bin/tsx"; else TSX="npx --yes tsx"; fi
if [ -x ./node_modules/.bin/tsc ]; then TSC="./node_modules/.bin/tsc"; else TSC="npx --yes tsc"; fi

# The new/edited test file(s) this dispatch's fix must make pass.
TEST_FILES="verify.test.ts"

# RUNNER selection: if tsconfig declares compilerOptions.paths (e.g. `@/*`),
# run tests under tsx so the alias resolves; otherwise use the repo's native
# node --test with type-stripping.
#
# We force `--test-reporter=tap` on BOTH runners. WHY: `tsx --test` (the
# path-alias branch) defaults to the SPEC reporter, which prints the count as
# `ℹ tests N`, NOT the TAP `# tests N` the summary grep below keys on. Without
# this a fully-green path-alias run parsed _ntests=0 and mis-fired
# SCAFFOLD_INCOMPLETE -- a false NO-GO on correct work, on every TS path-alias
# repo (resell-tracker, any Next.js repo). Forcing TAP makes the summary line
# deterministic regardless of which runner (or reporter default) is in play.
#
# `require("./tsconfig.json")` THROWS on the
# `// comment`s and trailing commas that `tsc --init` emits (JSONC, not JSON) --
# a throw made HAS_PATHS="0", picked the wrong runner, and left `@/` imports
# unresolved. Parse tolerantly (strip comments + trailing commas) and follow
# one level of `extends` (relative, absolute, or a package like @tsconfig/next).
HAS_PATHS=$("$NODE" -e 'const fs=require("fs"),path=require("path");const strip=s=>{let o="",i=0,N=s.length,q="";while(i<N){const c=s[i],d=s[i+1];if(q){o+=c;if(c==="\\"){o+=s[i+1]||"";i+=2;continue}if(c===q)q="";i++;continue}if(c===String.fromCharCode(34)||c===String.fromCharCode(39)){q=c;o+=c;i++;continue}if(c==="/"&&d==="/"){i+=2;while(i<N&&s[i]!=="\n")i++;continue}if(c==="/"&&d==="*"){i+=2;while(i<N&&!(s[i]==="*"&&s[i+1]==="/"))i++;i+=2;continue}o+=c;i++}return o.replace(/,\s*([}\]])/g,"$1")};const load=f=>{try{return JSON.parse(strip(fs.readFileSync(f,"utf8")))}catch(e){return null}};const hp=(f,d)=>{if(!f||d>5)return false;const c=load(f);if(!c)return false;if(c.compilerOptions&&c.compilerOptions.paths&&Object.keys(c.compilerOptions.paths).length)return true;if(c.extends){let b;if(c.extends.startsWith(".")||path.isAbsolute(c.extends)){b=path.resolve(path.dirname(f),c.extends.endsWith(".json")?c.extends:c.extends+".json")}else{try{b=require.resolve(c.extends,{paths:[path.dirname(f)]})}catch(e){return false}}return hp(b,d+1)}return false};process.stdout.write(hp("./tsconfig.json",0)?"1":"0")' 2>/dev/null)
if [ "$HAS_PATHS" = "1" ]; then
  RUNNER="$TSX --test --test-reporter=tap --experimental-test-module-mocks"
  echo "  runner: tsx --test --test-reporter=tap --experimental-test-module-mocks (tsconfig paths present -- resolves @/ aliases)"
else
  RUNNER="$NODE --experimental-strip-types --test --test-reporter=tap --experimental-test-module-mocks"
  echo "  runner: node --experimental-strip-types --test --test-reporter=tap --experimental-test-module-mocks (no path aliases)"
fi

echo "=== target parses ==="
if "$NODE" '/Users/user/bin/ts-mutator/ts-parse.mjs' 'lib/payoutMismatch.ts' 2>/tmp/_verify_parse.$$.log; then
  echo "  ok: lib/payoutMismatch.ts parses"
elif grep -qiE "ERR_MODULE_NOT_FOUND|Cannot find (package|module) 'typescript'" /tmp/_verify_parse.$$.log; then
  echo "  WARN: ts-parse sidecar not installed (needs 'typescript' in bin/ts-mutator) -- relying on tsc --noEmit below"
else
  echo "  FAIL: lib/payoutMismatch.ts does not parse"; head -5 /tmp/_verify_parse.$$.log; fails=$((fails+1))
fi
rm -f /tmp/_verify_parse.$$.log

echo "=== types (tsc --noEmit) ==="
if $TSC --noEmit -p tsconfig.json >/tmp/_verify_tsc.$$.log 2>&1; then
  echo "  ok: tsc --noEmit clean"
elif grep -qE 'lib/payoutMismatch\.ts[(:]' /tmp/_verify_tsc.$$.log; then
  echo "  FAIL: tsc --noEmit reports errors in lib/payoutMismatch.ts"; grep -E 'lib/payoutMismatch\.ts[(:]' /tmp/_verify_tsc.$$.log | head -15; fails=$((fails+1))
else
  echo "  WARN: tsc --noEmit has pre-existing errors OUTSIDE lib/payoutMismatch.ts (not this task's) -- passing type gate"
fi
rm -f /tmp/_verify_tsc.$$.log

echo "=== spec literals ==="
if python3 ./check_literals.py; then
  echo "  ok: every Must-contain literal present"
else
  echo "  FAIL: a Must-contain literal is missing"; fails=$((fails+1))
fi

echo "=== behavioural tests ($RUNNER) ==="
# The adversarial cases live in the repo's OWN test file(s), authored by you and
# copied in -- the model is told not to edit them (SHA-checked like any fixture).
# `node --test` with ZERO tests exits 0 (green-on-nothing), so -- matching the
# Python/Swift `>= 3` discipline -- we also require >= 3 tests actually RAN, read
# from the count summary line. RUNNER forces `--test-reporter=tap` above so the
# line is the TAP `# tests N`; the grep also accepts the SPEC reporter's
# `ℹ tests N` as a belt-and-suspenders fallback should the reporter flag ever
# not take. The count keyword is anchored to a line-leading `# ` / `ℹ ` marker
# (never mid-line) so a test NAMED "... tests 5 ..." cannot false-match.
if [ -z "$TEST_FILES" ]; then
  echo "  SCAFFOLD_INCOMPLETE: no TEST_FILES set -- name the new/edited test file(s) in verify.sh"; fails=$((fails+1))
else
  # --test-timeout: a test that never settles (an open handle, a .listen(), an
  # un-awaited fetch) must FAIL BY NAME here ("test timed out after 120000ms"),
  # not hang verify.sh into the worker's 300s kill -- which the model only ever
  # saw as "(verify command timed out after 300s)" every iteration, with nothing
  # to act on, until the budget was gone and the run read as nonconvergence.
  # node --test honours it; tsx forwards it (both verified 2026-09-22).
  # >>> dispatch-nonet-guard v3
  # PER-TEST timeout 10s (was 120s, 2026-10-06 rt-bfmr-tls-fingerprint): it is per TEST, so N hung
  # tests cost N x the value; 4 hung tests at 120s froze the self-check ~8 min. Override with
  # DISPATCH_TEST_TIMEOUT_MS. NETWORK GUARD: a test that makes a REAL outbound call hangs on a
  # blackholed network; the preload makes global fetch and non-loopback sockets fail FAST.
  # mktemp -d (BSD mktemp only randomises TRAILING X's, so a ".cjs" suffix collided and failed); if the
  # guard cannot be created the verify runs WITHOUT it rather than failing every run.
  _nd=$(mktemp -d "${TMPDIR:-/tmp}/dispatch-nonet.XXXXXX" 2>/dev/null); _nets="$_nd/nonet.cjs"; _noneto=""
  # NO HEREDOC (2026-10-08): a >512-byte heredoc is written through a pipe, and when the kernel is
  # short of pipe memory (macOS then hands out 512-byte pipes -- seen with a leaking app holding
  # ~2000 pipe fds) bash 5.3 blocks forever in heredoc_write BEFORE exec: verify.sh hung with 0% CPU
  # until the self-check wall (SELFCHECK_HANG at baseline). printf is a builtin writing a FILE.
  [ -n "$_nd" ] && printf '%s
'     'const _deny = (what) => { const e = new Error("network denied in dispatch verify (" + what + "): real outbound calls are not allowed; mock the module"); e.code = "EDISPATCH_NONET"; return e; };'     'globalThis.fetch = async (u) => { throw _deny("fetch " + String((u && u.url) || u)); };'     'const net = require("net");'     'const _conn = net.Socket.prototype.connect;'     'const _local = (h) => h === "localhost" || h === "::1" || h === "[::1]" || h === "0.0.0.0" || h.startsWith("127.");'     'net.Socket.prototype.connect = function (...a) {'     '  const o = Array.isArray(a[0]) ? a[0][0] : a[0]; const host = (o && typeof o === "object") ? o.host : (typeof a[1] === "string" ? a[1] : undefined);'     '  if (host && !_local(String(host))) { process.nextTick(() => this.destroy(_deny("connect " + host))); return this; }'     '  return _conn.apply(this, a);'     '};' > "$_nets"
  [ -n "$_nd" ] && [ -s "$_nets" ] && _noneto="--require $_nets"
  _tout=$(NODE_OPTIONS="${NODE_OPTIONS:-} $_noneto" $RUNNER --test-timeout="${DISPATCH_TEST_TIMEOUT_MS:-10000}" $TEST_FILES 2>&1); _trc=$?
  [ -n "$_nd" ] && rm -rf "$_nd"
  # <<< dispatch-nonet-guard
  echo "$_tout" | tail -30
  # The tail above shows only the LAST ~30 lines: with N failing cases the model saw
  # just the final case (rt-egift-link-s1-s4, 2026-10-06: 5 of 6 failed, the tail showed
  # `not ok 6`, so the authoring model chased one case for 9 jobs while the real
  # cause -- shared by all 5 -- was never on screen). List EVERY failing case by name.
  printf '%s
' "$_tout" | grep -E '^not ok [0-9]+ ' | head -40 | sed 's/^/  FAILED CASE: /'
  _ntests=$(printf '%s
' "$_tout" | grep -oE '^(# |ℹ )tests [0-9]+' | grep -oE '[0-9]+' | tail -1)
  [ -z "$_ntests" ] && _ntests=0
  if [ "$_trc" -ne 0 ]; then
    echo "  FAIL: new test file(s) failed (exit $_trc)"; fails=$((fails+1))
  elif [ "$_ntests" -lt 3 ]; then
    echo "  SCAFFOLD_INCOMPLETE: only $_ntests test(s) ran; need >= 3 (a passing suite of 0 tests is vacuous)"; fails=$((fails+1))
  else
    echo "  ok: new test file(s) pass ($_ntests tests)"
  fi
fi

echo "=== repo suite (npm test; FAIL only on this dispatch's own test files) ==="
# A broader safety net. WARN not FAIL in general: a repo suite is often red at
# baseline for reasons this task never touched, so it cannot gate; the scoped
# TEST_FILES above are the authoritative behavioural pin. EXCEPT a failing test
# FILE this dispatch added or changed -- that is the dispatch's own regression.
# Live 2026-10-02 (bfmr-replace-tracking): the model's new lib/X.test.ts imported
# './X' without the `.ts` the repo's `node --experimental-strip-types` runner
# needs -- green under tsx above, red under `npm test`, and the WARN let it pass
# the gate. Set REPO_TEST=skip to silence.
if [ "${REPO_TEST:-run}" = "skip" ]; then
  echo "  (skipped)"
elif ! grep -q '"test"' package.json 2>/dev/null; then
  echo "  (no npm test script)"
elif npm test --silent >/tmp/_verify_npmtest.$$.log 2>&1; then
  echo "  ok: npm test green"
else
  _own=""
  for _f in $(grep -E '^[[:space:]]*(✖|not ok [0-9]+ -|test at|FAIL) ' /tmp/_verify_npmtest.$$.log | grep -oE '[A-Za-z0-9_./@-]+\.(test|spec)\.[cm]?[jt]sx?' | sort -u); do
    if git status --porcelain --untracked-files=all -- "$_f" 2>/dev/null | grep -q .; then _own="$_own $_f"; fi
  done
  if [ -n "$_own" ]; then
    echo "  FAIL: npm test fails in test file(s) this dispatch added/changed:$_own"; tail -15 /tmp/_verify_npmtest.$$.log; fails=$((fails+1))
  else
    echo "  WARN: npm test not green (may be pre-existing -- scoped tests above gate)"; tail -15 /tmp/_verify_npmtest.$$.log
  fi
fi
rm -f /tmp/_verify_npmtest.$$.log

echo "--- $fails failed ---"
[ "$fails" -eq 0 ] && echo VERIFY_OK || exit 1
