#!/usr/bin/env bash
# Try the plugin on a real FPP with ONE WiZ bulb on channels 300001-300003 (away from a show).
#   bash tools/try_on_fpp.sh start 192.168.1.50     # enable + restart fppd
#   bash tools/try_on_fpp.sh stop                    # fully undo
# Never touches other channel outputs, other plugins, or existing bridge services.
set -u
PLUGIN="$(cd "$(dirname "$0")/.." && pwd)"
MEDIA="${MEDIA:-/home/fpp/media}"
CO="${CO:-$MEDIA/config/channeloutputs.json}"
CFG="$MEDIA/config/fpp-smart-bulbs.json"
RESTART="${RESTART_FPPD:-sudo systemctl restart fppd}"
START_CH=300001
. "$PLUGIN/scripts/common.sh"
die() { echo "STOPPED, nothing changed: $*"; exit 1; }

edit_co() {   # edit_co add|remove
python3 - "$CO" "$1" "$START_CH" <<'PY'
import json, os, sys, tempfile
p, mode, start = sys.argv[1], sys.argv[2], int(sys.argv[3])
d = json.load(open(p)); outs = d.setdefault("channelOutputs", [])
mine = [o for o in outs if o.get("type") == "SmartBulbBridge"]
if mode == "add":
    if mine: sys.exit("a SmartBulbBridge output already exists")
    outs.append({"type": "SmartBulbBridge", "enabled": 1, "startChannel": start, "channelCount": 3})
else:
    d["channelOutputs"] = [o for o in outs if o.get("type") != "SmartBulbBridge"]
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".co-")
with os.fdopen(fd, "w") as f: json.dump(d, f, indent=2); f.write("\n"); f.flush(); os.fsync(f.fileno())
os.replace(tmp, p)
PY
}

case "${1:-}" in
start)
  IP="${2:-}"; [[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "usage: try_on_fpp.sh start BULB_IPV4"
  [ -f "$PLUGIN/libfpp-co-SmartBulbBridge.so" ] || die "run make first (library not built)"
  [ -f "$CO" ] || die "missing $CO"
  grep -q SmartBulbBridge "$CO" && die "SmartBulbBridge output already present (run 'stop' first)"
  [ -e "$CFG" ] && die "$CFG already exists; refusing to overwrite it"
  BK="$CO.before-smartbulb-$(date +%Y%m%d-%H%M%S)"; cp -p "$CO" "$BK" || die "backup failed"
  echo "backup of channeloutputs.json: $BK"
  mkdir -p "$(dirname "$CFG")"
  python3 - "$CFG" "$IP" "$START_CH" <<'PY'
import json, sys
json.dump({"listen_port": 4051, "refresh_seconds": 30, "bulbs": [
  {"name": "Test WiZ", "protocol": "wiz", "ip": sys.argv[2], "channel": int(sys.argv[3])}]},
  open(sys.argv[1], "w"), indent=2)
PY
  [ -f "$PLUGIN/callbacks.sh.disabled" ] && mv "$PLUGIN/callbacks.sh.disabled" "$PLUGIN/callbacks.sh"
  chmod +x "$PLUGIN/callbacks.sh" "$PLUGIN"/scripts/*.sh "$PLUGIN"/tools/*.sh "$PLUGIN"/tools/*.py "$PLUGIN"/bridge/*.py 2>/dev/null
  edit_co add || { cp -p "$BK" "$CO"; rm -f "$CFG"; die "could not edit channeloutputs.json (restored)"; }
  echo "restarting fppd..."; $RESTART
  sleep 6
  if ! (ss -lun 2>/dev/null | grep -q ':4051\b'); then
     echo "NOTE: FPP did not start the bridge daemon by itself (postStart.sh not run); starting it by hand."
     bash "$PLUGIN/scripts/postStart.sh"; sleep 2
  fi
  ss -lun 2>/dev/null | grep -q ':4051\b' && echo "bridge daemon: listening on 4051" || echo "WARNING: nothing listening on 4051; see $LOG"
  echo
  echo "Now in the FPP web UI: Status/Control > Display Testing (or Tools > Test), set channel range"
  echo "$START_CH to $((START_CH+2)), choose RGB Cycle or a solid color, and enable the test."
  echo "Also run:  python3 $PLUGIN/tools/wiz_test.py $IP 255 0 0    (proves the bulb answers)"
  echo "Undo with: bash $PLUGIN/tools/try_on_fpp.sh stop"
  ;;
stop)
  bash "$PLUGIN/scripts/preStop.sh"
  [ -f "$CO" ] && edit_co remove
  [ -f "$PLUGIN/callbacks.sh" ] && mv "$PLUGIN/callbacks.sh" "$PLUGIN/callbacks.sh.disabled"
  echo "restarting fppd..."; $RESTART
  echo "Plugin deactivated. Your test config ($CFG) was left in place; delete it if you like."
  ;;
*) echo "usage: try_on_fpp.sh start BULB_IP | stop"; exit 2 ;;
esac
