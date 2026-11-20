#!/bin/bash
# Used by the web page after saving config.
D="$(dirname "$0")"
"$D/preStop.sh"; sleep 1; "$D/postStart.sh"
