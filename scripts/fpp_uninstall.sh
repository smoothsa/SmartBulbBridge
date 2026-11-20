#!/bin/bash
DIR="$(cd "$(dirname "$0")/.." && pwd)"
"$DIR/scripts/preStop.sh" || true
make -C "$DIR" clean || true
echo "Your bulb config (config/fpp-smart-bulbs*.json) was left in place."
