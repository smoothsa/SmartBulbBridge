#!/bin/bash
# Installs the private Cync Python environment (called from the Smart Bulbs page). Usage: cync_install.sh CYNC_DIR
D="$1"
PLUGIN="$(cd "$(dirname "$0")/.." && pwd)"
[ -n "$D" ] && [ -d "$D" ] || exit 2
echo running > "$D/install.state"
if python3 "$PLUGIN/bridge/cync_setup.py" --private-dir "$D" --prepare-with-network-downloads > "$D/install.log" 2>&1; then
  echo ok > "$D/install.state"
else
  echo failed > "$D/install.state"
fi
