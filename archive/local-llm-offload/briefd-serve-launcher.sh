#!/bin/zsh
# Wrapper so com.penn.briefd-serve.plist never holds a plaintext secret: the
# plist is a static, world-readable file, so tokens go through Keychain here
# instead (see feedback_keychain_for_secrets memory).
set -euo pipefail

export BRIEFD_API_TOKEN="$(security find-generic-password -a briefd -s "briefd-api-token" -w)"
export BRIEFD_GIT_TOKEN="$(gh auth token)"

cd "/Users/penn/Desktop/GitHub Projects/briefd-trial"
exec ./bin/briefd serve \
  -source https://github.com/frindle/briefd-knowledge.git \
  -db /Users/penn/.briefd/vault.db \
  -listen :7788
