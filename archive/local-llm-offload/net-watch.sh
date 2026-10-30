#!/bin/bash
# Mac Studio network watchdog: diagnose drops, and fail over to Wi-Fi if Ethernet dies.
#
# 2026-08-24: THE WI-FI-OFF BEHAVIOUR WAS REMOVED. It was built on a theory that was
# later falsified, and it was actively fighting the user -- every manual Wi-Fi enable
# was reverted within 30s by the next tick, with no benefit.
#
# The dead theory: en0 (Ethernet) and en1 (Wi-Fi) both live on the same /20 caused
# macOS to re-elect the primary interface ~155 times in 3 hours, breaking connections.
#
# Why it was wrong (established 2026-08-23):
#   - Turning Wi-Fi off changed the drop rate not at all: 0.93/min -> 0.98/min.
#   - ZERO of that day's ~176 events ever named en1; every one re-elected *to en0*,
#     the interface that was already primary.
#   - The "155 re-elections" was a misread of symptomsd, a diagnostics daemon. Its
#     "primary interface change" means it opened a new measurement epoch -- NOT that
#     routing changed or that any connection broke.
#
# Actual root cause of the drops: Cloudflare WARP holding the default route via utun0
# (DNS 127.0.2.2/.3 is WARP's resolver; when the tunnel stalls, lookups fail). LAN was
# flawless throughout. See memory project-studio-warp-outages.
#
# What remains: failover only. Ethernet down -> Wi-Fi ON, so the machine stays
# reachable. Ethernet healthy -> Wi-Fi is LEFT ALONE, whatever the user set it to.
#
# HYSTERESIS: Wi-Fi is only enabled after ETH_FAIL_THRESHOLD consecutive failed checks,
# so a brief blip does not flap the radio.
LOG="$HOME/bin/net-watch.log"
STATE="$HOME/bin/.net-watch-state"
FAILS="$HOME/bin/.net-watch-ethfails"
ETH_FAIL_THRESHOLD=3          # 3 x 30s = ~90s of dead Ethernet before failing over

eth_status=$(ifconfig en0 2>/dev/null | awk '/status:/{print $2}')
wifi_power=$(networksetup -getairportpower en1 2>/dev/null | awk '{print $NF}')
iface=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')
gw=$(route -n get default 2>/dev/null | awk '/gateway:/{print $2}')

gw_ok=FAIL;  [ -n "$gw" ] && ping -c1 -W2 "$gw" >/dev/null 2>&1 && gw_ok=OK
ext_ok=FAIL; ping -c1 -W2 1.1.1.1 >/dev/null 2>&1 && ext_ok=OK
dns_ok=FAIL; dscacheutil -q host -a name cloudflare.com >/dev/null 2>&1 && dns_ok=OK
now="$(date '+%F %T')"

# --- Ethernet failure enables Wi-Fi; healthy Ethernet NEVER touches it ---------
if [ "$eth_status" = "active" ] && [ "$gw_ok" = "OK" ]; then
  echo 0 > "$FAILS"
  # Deliberately a no-op. Do NOT turn Wi-Fi off here -- see header. Whether Wi-Fi
  # is on is the user's choice, and this script has no evidence-backed reason to
  # override it.
  :
else
  n=$(( $(cat "$FAILS" 2>/dev/null || echo 0) + 1 ))
  echo "$n" > "$FAILS"
  if [ "$n" -ge "$ETH_FAIL_THRESHOLD" ] && [ "$wifi_power" != "On" ]; then
    networksetup -setairportpower en1 on 2>/dev/null \
      && echo "$now FAILOVER  Ethernet down ${n} checks (status=$eth_status gw=$gw_ok) -- Wi-Fi turned ON" >> "$LOG"
  fi
fi

# --- change-only diagnostics ---------------------------------------------------
cur="iface=$iface gw=$gw_ok ext=$ext_ok dns=$dns_ok eth=$eth_status wifi=$wifi_power"
prev=$(cat "$STATE" 2>/dev/null)
if [ "$cur" != "$prev" ]; then
  verdict=""
  [ "$gw_ok" = FAIL ] && verdict="LOCAL: gateway unreachable"
  [ "$gw_ok" = OK ] && [ "$ext_ok" = FAIL ] && verdict="UPSTREAM: gateway fine, internet unreachable"
  [ "$ext_ok" = OK ] && [ "$dns_ok" = FAIL ] && verdict="DNS: connectivity fine, resolution failing"
  echo "$now CHANGE    $cur${verdict:+  <-- $verdict}" >> "$LOG"
  echo "$cur" > "$STATE"
elif [ "$(date +%M)" = "00" ]; then
  echo "$now ok        $cur" >> "$LOG"
fi

# --- sleep-setting drift check -------------------------------------------------
# Idle sleep kills long inference jobs. `pmset sleep 0` persists across reboots but
# macOS updates and the Energy Saver pane can reset it.
SLEEP_VAL=$(pmset -g custom 2>/dev/null | sed -n '/AC Power/,/Battery/p' | awk '/^ *sleep/{print $2; exit}')
if [ -n "$SLEEP_VAL" ] && [ "$SLEEP_VAL" != "0" ]; then
  echo "$now WARNING   idle sleep is back on (sleep=$SLEEP_VAL) -- long jobs will die. Fix: sudo pmset -a sleep 0" >> "$LOG"
fi
