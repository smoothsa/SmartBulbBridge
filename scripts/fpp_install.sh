#!/bin/bash
# FPP runs this when the plugin is installed/updated. Builds the native output only.
DIR="$(cd "$(dirname "$0")/.." && pwd)"
# zip/git extraction can drop execute bits; FPP needs callbacks.sh and the hook scripts executable
chmod +x "$DIR/callbacks.sh" "$DIR"/scripts/*.sh "$DIR"/tools/*.sh "$DIR"/tools/*.py "$DIR"/bridge/*.py 2>/dev/null
make -C "$DIR" SRCDIR="${SRCDIR:-/opt/fpp/src}"
mkdir -p "${MEDIADIR:-/home/fpp/media}/config"
echo "Installed. Restart fppd, then add a 'SmartBulbBridge' channel output and configure bulbs on the plugin page."
