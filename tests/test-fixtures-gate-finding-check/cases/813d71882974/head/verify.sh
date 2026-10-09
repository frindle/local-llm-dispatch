#!/usr/bin/env bash
# verify for: bg-brokers-s1-load-s1-url-else-valueerror-norm
# Counting idiom, NOT `set -e` -- an aborting verify never prints why it failed.
cd "$(dirname "$0")" || exit 1

fails=0

# --- env parity ------------------------------------------------------------
# System pip is PEP-668 blocked; the worker runs verify locally, so a venv is
# fine. Without this, a verify that cannot import the target fails for a reason
# that has nothing to do with the task -- on every iteration.
PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  if [ -f requirements.txt ]; then
    python3 -m venv .venv >/dev/null 2>&1 \
      && .venv/bin/pip install -q -r requirements.txt >/dev/null 2>&1
  fi
fi
[ -x "$PY" ] || PY=python3

echo "=== target parses ==="
if "$PY" -c "import ast,sys; ast.parse(open('broker_guard/brokers.py').read())"; then
  echo "  ok: broker_guard/brokers.py parses"
else
  echo "  FAIL: broker_guard/brokers.py does not parse"; fails=$((fails+1))
fi

echo "=== spec literals ==="
if "$PY" ./check_literals.py; then
  echo "  ok: every Must-contain literal present"
else
  echo "  FAIL: a Must-contain literal is missing"; fails=$((fails+1))
fi

echo "=== behavioural cases ==="
if "$PY" ./test_fixture.py; then
  echo "  ok: adversarial cases pass"
else
  echo "  FAIL: adversarial cases failed"; fails=$((fails+1))
fi

echo "--- $fails failed ---"
[ "$fails" -eq 0 ] && echo VERIFY_OK || exit 1
