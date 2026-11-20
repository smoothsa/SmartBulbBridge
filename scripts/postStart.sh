#!/bin/bash
# Run by FPP (as root) after fppd starts: start the Cync worker (if set up) and the bridge daemon,
# both as the FPP user.
. "$(dirname "$0")/common.sh"
[ -f "$CONFIG" ] || exit 0
ensure_dirs
if grep -q '"cync"' "$CONFIG" && [ -x "$CYNC_DIR/venv/bin/python3" ] && [ -f "$CYNC_DIR/auth.json" ] \
   && ! running "$CYNC_PIDFILE"; then
  launch "$LOG" "$CYNC_DIR/venv/bin/python3" -B "$PLUGIN_DIR/bridge/cync_worker_entry.py" \
    --config "$CONFIG" --auth-file "$CYNC_DIR/auth.json" --socket "$CYNC_DIR/bridge.sock" \
    --pidfile "$CYNC_PIDFILE"
fi
if ! running "$PIDFILE"; then
  launch "$LOG" python3 -B "$PLUGIN_DIR/bridge/bulbbridge.py" "$CONFIG" "$STATUS" --pidfile "$PIDFILE"
fi
exit 0
