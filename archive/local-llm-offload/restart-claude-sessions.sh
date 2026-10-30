#!/bin/zsh
# restart-claude-sessions.sh — fired by com.penn.restart-sessions LaunchAgent at 14:10 (one-shot).
# Purpose: after a Claude usage-limit window resets, bring the Studio's agent sessions + dispatch
# daemon back up WITHOUT a human present. Recreates fresh tmux sessions (fresh agent context) that
# Penn re-attaches to via the existing ttyd tunnels. Self-removes so it fires exactly once.
# NOTE: this KILLS the current claude-mac-studio session (fresh context; the outgoing session should
# have filed its state to the Obsidian vault + auto-memory first — see the handoff note).
set -u
LOG=/tmp/restart-claude-sessions.log
UID_N=$(id -u)
DIR="$HOME/Desktop/GitHub Projects"
echo "=== $(date '+%F %T %Z') restart-claude-sessions firing ===" >> "$LOG"

# 1) ollama-queue run daemon (dispatch orchestration) — restart if not running.
if ! pgrep -f "ollama-queue.py run" >/dev/null 2>&1; then
  nohup /opt/homebrew/bin/python3 "$HOME/bin/ollama-queue.py" run --poll-interval 15 \
    >> /tmp/ollama-queue-run.log 2>&1 &
  echo "  restarted ollama-queue run daemon (pid $!)" >> "$LOG"
else
  echo "  ollama-queue run daemon already up" >> "$LOG"
fi

# 2) Agent sessions — kill the stale/limited tmux session, recreate it fresh + detached so a new
#    agent process is running and ready; Penn's browser (ttyd) attaches via `new-session -A`.
tmux kill-session -t claude-mac-studio 2>/dev/null
tmux new-session -d -s claude-mac-studio -c "$DIR" claude \
  && echo "  restarted claude session" >> "$LOG" || echo "  FAILED to restart claude" >> "$LOG"

tmux kill-session -t goose-mac-studio 2>/dev/null
tmux new-session -d -s goose-mac-studio -c "$DIR" goose session \
  && echo "  restarted goose session" >> "$LOG" || echo "  FAILED to restart goose" >> "$LOG"

# qwen session is a log-tail MONITOR, not an agent — only recreate if it died.
tmux has-session -t qwen-mac-studio 2>/dev/null \
  || tmux new-session -d -s qwen-mac-studio -c "$DIR" "tail -f -n 200 $HOME/qwen-dispatch.log"

# 3) Bounce the ttyd LaunchAgents so any open browser tab reconnects onto the fresh sessions.
for a in ttyd-claude ttyd-goose ttyd-qwen; do
  launchctl kickstart -k "gui/$UID_N/com.penn.$a" 2>>"$LOG"
done
echo "  bounced ttyd agents" >> "$LOG"

# 4) One-shot: remove this LaunchAgent so it never fires again.
launchctl bootout "gui/$UID_N/com.penn.restart-sessions" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/com.penn.restart-sessions.plist"
echo "  self-removed (one-shot) — done" >> "$LOG"
