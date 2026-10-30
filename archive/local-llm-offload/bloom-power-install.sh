#!/bin/bash
# Install the Darkbloom power-log + earnings-archive LaunchAgents as USER agents. No sudo, ever.
#   bloom-power-install.sh            show what will happen, ask y/N (no tty: show plan and stop)
#   bloom-power-install.sh --yes      do it without asking
#   bloom-power-install.sh --uninstall   unload + remove the agents (data in ~/.darkbloom is kept)
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UID_=$(id -u)
LA="$HOME/Library/LaunchAgents"
LABELS="com.penn.bloom-power-log com.penn.bloom-earnings-archive com.penn.bloom-power-workbook com.penn.bloom-influx-push"
BINS="bloom_power_lib.py bloom-power-log.py bloom-earnings-archive.py bloom-power-report.py bloom-power-workbook.py bloom_influx.py bloom-influx-push.py bloom-power-install.sh"
[ "$(id -u)" = 0 ] && { echo "refusing to run as root: these are user agents"; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  for l in $LABELS; do launchctl bootout "gui/$UID_/$l" 2>/dev/null; rm -f "$LA/$l.plist"; echo "removed $l"; done
  exit 0
fi

cat <<PLAN
This will (user-level only, no sudo):
  1. brew install macmon                      (if missing; sudoless power sensor reader)
  2. copy $BINS to ~/bin (if the repo copy is newer)
  3. install LaunchAgents in $LA:
       com.penn.bloom-power-log        KeepAlive sampler -> ~/.darkbloom/power/power-log-YYYY-MM.csv (1 row/min)
       com.penn.bloom-earnings-archive every 15 min       -> ~/.darkbloom/earnings-archive/earnings-YYYY-MM.jsonl
       com.penn.bloom-power-workbook   daily 01:17 local  -> monthly .xlsx in Proton Drive <root>/Darkbloom/<YYYY>/ (+ local copy)
                                       (current month DRAFT; previous month FINAL on UTC days 1-3)
       com.penn.bloom-influx-push      every 60 s         -> InfluxDB v2 (http://10.0.6.61:8086 org home bucket metrics, token from Keychain
                                       service influxdb-home); spools locally if Influx is unreachable; ~/.darkbloom/influx/
  4. python3 -m venv ~/.darkbloom/venv + pip install openpyxl   (user-level; the workbook agent runs from it)
  5. launchctl bootstrap gui/$UID_ all agents
It reads Darkbloom state files only; it never runs darkbloom start/stop/switch, never edits provider.toml,
opens no listener, and sends nothing off the machine except one GET to api.darkbloom.dev (earnings) and the metric writes to the LAN InfluxDB.
PLAN
if [ "${1:-}" != "--yes" ]; then
  if [ -t 0 ]; then read -r -p "Proceed? [y/N] " a; [ "$a" = y ] || [ "$a" = Y ] || { echo aborted; exit 1; }
  else echo "(no tty: re-run with --yes to apply)"; exit 1; fi
fi

if ! command -v macmon >/dev/null 2>&1; then
  brew install macmon || { echo "brew install macmon FAILED. Not using any sudo workaround. The sampler would log 'power unavailable'; fix brew and re-run."; exit 1; }
fi
mkdir -p ~/bin "$LA" ~/.darkbloom/power ~/.darkbloom/earnings-archive ~/.darkbloom/influx
if ! "$HOME/.darkbloom/venv/bin/python" -c "import openpyxl" 2>/dev/null; then
  python3 -m venv "$HOME/.darkbloom/venv" && "$HOME/.darkbloom/venv/bin/pip" install -q openpyxl \
    || { echo "venv/openpyxl install FAILED: the workbook agent will not run until fixed"; }
fi
for f in $BINS; do
  if [ "$REPO/bin/$f" -nt "$HOME/bin/$f" ] || [ ! -e "$HOME/bin/$f" ]; then cp -p "$REPO/bin/$f" "$HOME/bin/$f"; fi
done
chmod +x ~/bin/bloom-influx-push.py ~/bin/bloom-power-log.py ~/bin/bloom-earnings-archive.py ~/bin/bloom-power-report.py ~/bin/bloom-power-workbook.py ~/bin/bloom-power-install.sh
for l in $LABELS; do
  cp "$REPO/LaunchAgents/$l.plist" "$LA/$l.plist"
  launchctl bootout "gui/$UID_/$l" 2>/dev/null
  launchctl bootstrap "gui/$UID_" "$LA/$l.plist" && echo "loaded $l"
done
echo "done. Check: tail ~/.darkbloom/power/power-log-\$(date -u +%Y-%m).csv"
