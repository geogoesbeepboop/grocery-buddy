#!/usr/bin/env bash
#
# grocery-buddy local dev launcher (macOS / Terminal.app).
#
# Boots the whole local stack in its own Terminal windows and lets you bounce the
# app (worker + webhook) in place as you edit — the inner loop for testing.
#
#   ./scripts/dev.sh up [--ngrok]   # start Temporal, then worker + webhook (+ ngrok)
#   ./scripts/dev.sh restart        # bounce worker + webhook after a code change
#   ./scripts/dev.sh down           # stop everything (worker, webhook, ngrok, Temporal)
#   ./scripts/dev.sh status         # what's running
#
# Services and the order they come up:
#   1. Temporal   docker compose up -d        (UI http://localhost:8088, gRPC :7233)
#   2. worker     uv run grocery-buddy worker
#   3. webhook    uv run grocery-buddy webhook --port 8080
#   4. ngrok      ngrok http 8080             (only with --ngrok; for live Telegram)
#
# Non-macOS / no-GUI: run the services yourself from `make help` (worker, webhook, temporal).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="${TMPDIR:-/tmp}"
WEBHOOK_PORT=8080
TEMPORAL_PORT=7233
TEMPORAL_UI="http://localhost:8088"

say()  { printf '\033[36m▶ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(uname)" = "Darwin" ] || die "This launcher uses macOS Terminal.app. Elsewhere, start the services from 'make help'."

# Open a Terminal window running <cmd>, titled GB·<tag>. The command is staged in a
# temp script so osascript never has to escape it; `exec` keeps the title stable
# (no interactive shell to reset it) so restart/down can close the window by title.
launch() {
  local tag="$1" cmd="$2"
  local f="$TMP/gb-dev-$tag.command"
  cat > "$f" <<EOF
#!/usr/bin/env bash
printf '\033]0;GB·$tag\007'
cd "$ROOT" || exit 1
echo "── GB·$tag ──  $cmd"
$cmd
echo
echo "[GB·$tag exited — ./scripts/dev.sh restart to relaunch]"
EOF
  chmod +x "$f"
  osascript >/dev/null 2>&1 <<OSA
tell application "Terminal"
  activate
  do script "exec '$f'"
end tell
OSA
  say "window up: GB·$tag"
}

# Close any windows we opened (matched by the GB· title). Best-effort.
close_windows() {
  osascript >/dev/null 2>&1 <<'OSA' || true
tell application "Terminal"
  repeat with w in (every window)
    try
      if (name of w) contains "GB·" then close w saving no
    end try
  end repeat
end tell
OSA
}

kill_svc() { pkill -f "$1" >/dev/null 2>&1 || true; }

# Wait until a local TCP port accepts connections (bash /dev/tcp, no nc needed).
wait_port() {
  local port="$1" timeout="${2:-60}" i=0
  until (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; do
    i=$((i + 1)); [ "$i" -ge "$timeout" ] && return 1; sleep 1
  done
  exec 3>&- 2>/dev/null || true
  return 0
}

start_temporal() {
  command -v docker >/dev/null || die "docker not found — needed for Temporal."
  say "starting Temporal (docker compose up -d) …"
  (cd "$ROOT" && docker compose up -d) || die "docker compose up failed — is Docker running?"
  printf '   waiting for Temporal on :%s ' "$TEMPORAL_PORT"
  if wait_port "$TEMPORAL_PORT" 90; then echo "✓"; else echo; warn "not reachable yet — the worker will retry."; fi
}

start_app() {
  kill_svc "grocery-buddy worker"
  kill_svc "grocery-buddy webhook"
  launch worker "uv run grocery-buddy worker"
  launch webhook "uv run grocery-buddy webhook --port $WEBHOOK_PORT"
}

cmd_up() {
  local with_ngrok=0
  [ "${1:-}" = "--ngrok" ] && with_ngrok=1
  close_windows
  start_temporal
  start_app
  if [ "$with_ngrok" = 1 ]; then
    if command -v ngrok >/dev/null; then
      launch ngrok "ngrok http $WEBHOOK_PORT"
      warn "Set WEBHOOK_BASE_URL in .env to the ngrok https URL, then re-register the Telegram webhook (see .env.example)."
    else
      warn "ngrok not installed — skipping."
    fi
  fi
  echo
  say "up. Temporal UI: $TEMPORAL_UI   webhook: http://localhost:$WEBHOOK_PORT"
  say "edit code → ./scripts/dev.sh restart   ·   stop all → ./scripts/dev.sh down"
}

cmd_restart() {
  say "bouncing worker + webhook …"
  kill_svc "grocery-buddy worker"
  kill_svc "grocery-buddy webhook"
  close_windows
  start_app
  say "restarted. (Temporal left running.)"
}

cmd_down() {
  say "stopping app + Temporal …"
  kill_svc "grocery-buddy worker"
  kill_svc "grocery-buddy webhook"
  kill_svc "ngrok http $WEBHOOK_PORT"
  close_windows
  (cd "$ROOT" && docker compose down) || true
  say "down."
}

cmd_status() {
  echo "Temporal (docker compose):"
  (cd "$ROOT" && docker compose ps 2>/dev/null) || echo "  (not running)"
  echo
  echo "Processes:"
  for p in "grocery-buddy worker" "grocery-buddy webhook" "ngrok http $WEBHOOK_PORT"; do
    if pgrep -f "$p" >/dev/null 2>&1; then echo "  ✅ $p"; else echo "  ·  $p (stopped)"; fi
  done
  echo
  echo "Ports:"
  for pr in "$TEMPORAL_PORT Temporal" "$WEBHOOK_PORT webhook" "8088 Temporal-UI"; do
    set -- $pr
    if (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; then echo "  ✅ :$1 ($2)"; exec 3>&-; else echo "  ·  :$1 ($2) closed"; fi
  done
}

case "${1:-up}" in
  up) shift || true; cmd_up "${1:-}" ;;
  restart) cmd_restart ;;
  down) cmd_down ;;
  status) cmd_status ;;
  *) die "usage: $(basename "$0") {up [--ngrok]|restart|down|status}" ;;
esac
