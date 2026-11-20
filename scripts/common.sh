# Shared by the hook scripts. FPP runs hooks as root; the daemon must run as the FPP user
# (owner of the media directory) so that the normal login can stop it and read its files.
MEDIADIR="${MEDIADIR:-/home/fpp/media}"
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$MEDIADIR/config/fpp-smart-bulbs.json"
DATA_DIR="$MEDIADIR/plugindata/SmartBulbBridge"
RUN_DIR="$DATA_DIR/run"
CYNC_DIR="$DATA_DIR/cync"                       # private (0700): venv, auth.json, socket
PIDFILE="$RUN_DIR/bridge.pid"
CYNC_PIDFILE="$RUN_DIR/cync.pid"
STATUS="$RUN_DIR/status.json"
LOG="${LOGDIR:-$MEDIADIR/logs}/fpp-smart-bulbs.log"
OWNER="$(stat -c %U "$MEDIADIR" 2>/dev/null || echo root)"

# Run a command as the FPP user when we are root; otherwise run it directly.
as_owner() {
  if [ "$(id -u)" = 0 ] && [ "$OWNER" != root ]; then runuser -u "$OWNER" -- "$@"; else "$@"; fi
}
ensure_dirs() {
  as_owner mkdir -p "$RUN_DIR" "$(dirname "$LOG")"
}
running() { [ -f "$1" ] && kill -0 "$(cat "$1" 2>/dev/null)" 2>/dev/null; }
stop_pidfile() {   # stop_pidfile FILE : SIGTERM, wait up to 5 s, then remove the file
  local f="$1" pid i
  [ -f "$f" ] || return 0
  pid="$(cat "$f" 2>/dev/null)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null
    for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
  fi
  rm -f "$f"
}

# launch LOGFILE CMD...   start a detached daemon as the FPP user; its log is opened by that user too
launch() {
  local log="$1"; shift
  as_owner bash -c 'log="$1"; shift; exec setsid nohup "$@" >>"$log" 2>&1' _ "$log" "$@" &
}
