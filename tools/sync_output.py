#!/usr/bin/env python3
"""Make FPP's channel-output entry for this plugin match the configured bulbs.

  python3 tools/sync_output.py CONFIG.json CHANNELOUTPUTS.json [--dry-run]

Adds/updates exactly one {"type": "SmartBulbBridge"} entry that covers every enabled bulb
(rounded up to whole RGB triples). Never touches other outputs. FPP must be restarted
(Restart FPPD) for a changed entry to take effect. Prints one human-readable line.
"""
import json, os, shutil, sys, tempfile

TYPE = "SmartBulbBridge"
MAX_COUNT = 65535          # the native output refuses larger ranges


def wanted_range(cfg):
    chans = [b["channel"] for b in cfg.get("bulbs", []) if b.get("enabled", True)]
    if not chans:
        return None
    lo, hi = min(chans), max(chans) + 2
    count = hi - lo + 1
    count += (-count) % 3                      # whole RGB triples
    return lo, count


def sync(config_path, co_path, dry=False):
    cfg = json.load(open(config_path))
    co = json.load(open(co_path))
    outs = co.setdefault("channelOutputs", [])
    mine = [o for o in outs if o.get("type") == TYPE]
    rng = wanted_range(cfg)
    if rng and rng[1] > MAX_COUNT:
        return 2, (f"Your bulbs span {rng[1]} channels; the maximum is {MAX_COUNT}. "
                   "Move the bulbs closer together in channel numbers.")
    if rng is None:
        if not mine:
            return 0, "No enabled bulbs and no channel output: nothing to do."
        new = [o for o in outs if o.get("type") != TYPE]
        msg = "No enabled bulbs: removed the SmartBulbBridge channel output."
    else:
        start, count = rng
        entry = {"type": TYPE, "enabled": 1, "startChannel": start, "channelCount": count}
        if len(mine) == 1 and all(mine[0].get(k) == v for k, v in entry.items()):
            return 0, f"Channel output already correct (start {start}, {count} channels)."
        new = [o for o in outs if o.get("type") != TYPE] + [entry]
        msg = f"Channel output set to start {start}, {count} channels."
    if dry:
        return 0, "(dry run) " + msg
    shutil.copy2(co_path, co_path + ".smartbulb.bak")
    co["channelOutputs"] = new
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(co_path) or ".", prefix=".co-")
    with os.fdopen(fd, "w") as f:
        json.dump(co, f, indent=2); f.write("\n"); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, co_path)
    return 0, msg + " Restart FPPD (button at the bottom of the page) for this to take effect."


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if x != "--dry-run"]
    if len(a) != 2:
        sys.exit(__doc__)
    try:
        rc, msg = sync(a[0], a[1], "--dry-run" in sys.argv)
    except Exception as e:
        rc, msg = 1, f"Could not update the channel output ({type(e).__name__}: {e})"
    print(msg)
    sys.exit(rc)
