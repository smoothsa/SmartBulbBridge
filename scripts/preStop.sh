#!/bin/bash
# Run by FPP before fppd stops (or by you): stop the daemon and the Cync worker.
. "$(dirname "$0")/common.sh"
stop_pidfile "$PIDFILE"
stop_pidfile "$CYNC_PIDFILE"
rm -f "$STATUS"      # so a stale status file can never look like a running daemon
exit 0
