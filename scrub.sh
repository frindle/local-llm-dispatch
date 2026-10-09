#!/usr/bin/env bash
# Scrub personal data from the tree (default: the repo). Called by SYNC.sh after the rsync step; idempotent
# (running it twice changes nothing). Never touches anything outside the given directory.
#   1. owner's name (prose -> "the owner", identifiers -> owner/user), account name, home dir
#   2. launchd label prefixes -> com.example.   (files are renamed to match, see the rename step)
#   3. private IPv4 (10/8, 172.16/12, 192.168/16) -> RFC 5737 documentation addresses, via a stable hash
#      (same input -> same output, in every file and every sync; no real address is stored in this script)
#   4. personal host names -> generic;  *.<private domain> -> *.example.com
# Usage: ./scrub.sh [dir]
set -euo pipefail
D="${1:-.}"
cd "$D"

PL="$(mktemp "${TMPDIR:-/tmp}/scrub.XXXXXX")"; trap 'rm -f "$PL"' EXIT
cat > "$PL" <<'PERL'
use strict; use warnings;
use Digest::MD5 qw(md5_hex);
my @blocks = ('192.0.2', '198.51.100', '203.0.113');
sub mapip {
  my $ip = shift;
  my $h = hex(substr(md5_hex("lld-scrub-v1:$ip"), 0, 8));
  return $blocks[$h % 3] . '.' . (int($h / 3) % 253 + 1);
}
sub is_private {
  my ($a, $b, $c, $d) = @_;
  return 0 if grep { $_ > 255 } ($a, $b, $c, $d);
  return 1 if $a == 10;
  return 1 if $a == 192 && $b == 168;
  return 1 if $a == 172 && $b >= 16 && $b <= 31;
  return 0;
}
sub who {   # name used as a person in prose
  my ($start, $sep) = @_;
  my $w = ($sep // '') =~ /^[-\/]/ ? 'owner' : 'the owner';
  $w = ucfirst($w) if defined $start;
  return $w . ($sep // '');
}
while (<>) {
  # private domain -> example.com
  s/storage[d]emon\.[p]enndalton\.com/unraid.example.com/g;
  s/ollama-queue\.[p]enndalton\.com/queue.example.com/g;
  s/qwen\.[p]enndalton\.com/qwen.example.com/g;
  s/[A-Za-z0-9.-]*\.[p]enndalton\.com/host.example.com/g;
  # tests that load a sibling script by absolute live path: resolve via $HOME (the sandbox overlay puts bin/ there)
  s{"/Users/(?:[p]enn|user)/bin/([A-Za-z0-9_.-]+\.py)"}{__import__("os").path.expanduser("~/bin/$1")}g
    if $ARGV =~ m{^(?:\./)?tests/[^/]+\.py$};
  # launchd labels, home dir, account, host names, identifiers
  s/\bcom\.[p]enn(?:dalton)?\b/com.example/g;
  s{/Users/[p]enn\b}{/Users/user}g;
  s/Users-[p]enn\b/Users-user/g;
  s/[p]enndalton/user/g;
  s/[p]ennsmacstudio/mac-host/g;
  s/storage[d]emon/unraid-host/g;
  s/notify-[p]enn/notify-owner/g;
  s/notify_[p]enn/notify_owner/g;
  s/timemachine-[p]enn/timemachine-owner/g;
  s/chown [p]enn:/chown user:/g;
  s/owned by [p]enn\b/owned by the user/g;
  s/(\bUSER"\) or )"[p]enn"/$1"user"/g;
  s/\b[P]ENN\b/OWNER/g;
  s/\ba [P]enn-/an owner-/g;
  s/(^\s*(?:[#\/*>|"'`\-]+\s*)*|[.!?]\s+)?\b[P]enn\b(-|\/)?/(defined $1 ? $1 : '') . who($1, $2)/ge;
  s/\b[p]enn\b/owner/g;
  # private IPv4 -> documentation range (stable hash)
  s{(?<![\w.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\w]|\.\d)}{ is_private($1,$2,$3,$4) ? mapip("$1.$2.$3.$4") : "$1.$2.$3.$4" }ge;
} continue { print }
PERL

# 1. content (only files that contain something to scrub)
grep -rIlE -e '[Pp]enn|[P]ENN|storage[d]emon|"/Users/user/bin/[A-Za-z0-9_.-]+\.py"|(^|[^0-9.])(10\.[0-9]+\.[0-9]+\.[0-9]+|192\.168\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+)' . \
    --exclude-dir=.git --exclude-dir=node_modules --exclude=scrub.sh 2>/dev/null | while IFS= read -r f; do
  perl -i "$PL" "$f"
done

# 2. rename files/dirs whose NAME carries a personal label (SYNC re-copies the live names every time;
#    the freshly scrubbed copy replaces any earlier renamed one).
find . -depth -path ./.git -prune -o -path '*/node_modules' -prune -o \( -name '*com.[p]enn*' -o -name '*notify-[p]enn*' -o -iname '*[p]enndalton*' \) -print 2>/dev/null | while IFS= read -r p; do
  dir="$(dirname "$p")"; base="$(basename "$p")"
  new="$(printf '%s' "$base" | sed -e 's/com\.[p]enn\(dalton\)\{0,1\}\./com.example./' -e 's/notify-[p]enn/notify-owner/' -e 's/[p]enndalton/user/')"
  [ "$new" = "$base" ] && continue
  if [ -d "$p" ] && [ -d "$dir/$new" ]; then cp -R "$p"/. "$dir/$new"/ && rm -rf "$p"; else mv -f "$p" "$dir/$new"; fi
done
