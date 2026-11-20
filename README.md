# FPP Smart Bulb Bridge (lean) — WiZ · Matter · Cync

Drives individual RGB smart bulbs from FPP channel data. Each bulb = 3 consecutive FPP channels (R,G,B).

```
FPP sequence/playlist ──► SmartBulbBridge channel output (C++, in fppd)
                              │  DDP over 127.0.0.1:4051 (changed chunks only)
                              ▼
                        bridge/bulbbridge.py  (one small asyncio daemon)
                              ├─► WiZ   : UDP setPilot to bulb IP :38899
                              ├─► Matter: local python-matter-server WebSocket
                              └─► Cync  : pycync (cloud)
```

Latest value wins, per-bulb rate limits, a slow/offline bulb never blocks the others, failures retry with backoff,
and every 30 s the current color is re-sent (recovers bulbs that were power-cycled).

> **Folder name matters:** FPP finds a plugin's channel-output library by looking in a plugin folder named exactly like the output type, so this plugin's folder must be `SmartBulbBridge` (the Plugin Manager uses `repoName` from `pluginInfo.json` for this).


## Status (read this first)

| Part | State |
|---|---|
| WiZ bulbs | **Tested** on FPP 10.4 (Raspberry Pi): FPP channel output -> bridge -> bulb |
| Setup page (add/remove/save, input validation) | **Tested** on FPP 10.4 |
| Matter bulbs | *Experimental.* The command sequence worked against a real bulb and a local Matter server in a standalone test; it has **not** yet been run through the plugin during playback |
| Cync bulbs | *Experimental.* Worker/session code is unit-tested only; not yet run through the plugin on real hardware |
| Install through the FPP Plugin Manager | **Not yet tested** |

Not included (and not needed for FSEQ playback): making each bulb its own DDP controller in xLights.

## Setup
1. Install from the FPP Plugin Manager (after you publish the repo) or copy this folder to `/home/fpp/media/plugins/SmartBulbBridge`, then run `scripts/fpp_install.sh`.
2. Restart fppd. Open **Content Setup → Smart Bulbs**, add bulbs, **Save**.
3. **Input/Output Setup → Channel Outputs**: add a `SmartBulbBridge` output whose start channel/count covers exactly your bulb channels (count must be a multiple of 3).
4. Test with a sequence that colors those channels.

Config is `media/config/fpp-smart-bulbs.json` (see `examples/`). Logs: `media/logs/fpp-smart-bulbs.log`.

- **WiZ**: needs the bulb's IP (reserve it in DHCP). Enable "Local Communication" in the WiZ app.
- **Matter**: run `python-matter-server` on the Pi (ws://127.0.0.1:5580/ws), commission bulbs there, enter each bulb's node ID.
- **Cync**: runs as a separate worker in its own Python venv, because `pycync` reportedly needs Python 3.13+. FPP 10.4 ships Python 3.13.5, so `python3` works there; older FPP versions need a newer Python installed first. Run once, as the `fpp` user (not root):
  ```
  D=/home/fpp/media/plugindata/SmartBulbBridge/cync; mkdir -p $D; chmod 700 $D
  python3 bridge/cync_setup.py --private-dir $D --prepare-with-network-downloads
  $D/venv/bin/python3 bridge/cync_onboard.py --auth-file $D/auth.json     # asks for email, password, emailed 2FA code
  ```
  Then add Cync bulbs on the page (device IDs are printed by onboarding). The worker starts with fppd. If Cync demands a new email code it stops (exit 78) instead of retrying; re-run onboarding.
- **Port 4051**, not 4049, so it can coexist with an older WizMatterBridge plugin. Don't map the same channels in both.

## First test (no FPP needed)
`python3 tools/wiz_test.py BULB_IP 255 0 0` sends red straight to one bulb (`0 0 0` = off; `--discover` finds WiZ bulbs). Do this before anything else.

## Tests
`python3 tests/test_bridge.py` (14 tests) and `python3 tests/test_cync_support.py` (7, from the pilot). Bridge tests cover: packet parsing, channel mapping, rate limit / latest-wins, dedupe, malformed packets, retry, isolation between slow and fast bulbs, config validation. Uses a fake WiZ bulb on loopback.

## Not yet verified (needs your FPP + bulbs)
- Compiling against real FPP headers and loading in fppd (only built against a mock of `ChannelOutput`).
- That FPP passes `m_startChannel` 0-based (code assumes this; if the first bulb lights one channel off, adjust `abs` in `SmartBulbOutput.cpp`).
- `postStart.sh` / `preStop.sh` hook names, `menu.inc` format and `bulbs.php` on your FPP version (PHP not syntax-checked here).
- The pinned `pycync==0.6.1` / `aiohttp==3.14.5` versions in `bridge/cync_setup.py` (copied from the pilot, unchecked: edit the constants if pip can't find them) and the 3.13 requirement.
- Matter and Cync with real devices; WiZ with a real bulb.
- Practical frame rates: WiZ over UDP handles ~10 updates/s per bulb; Cync (cloud) and Matter are much slower.

License: MIT (see `LICENSE`). Issues: https://github.com/smoothsa/SmartBulbBridge/issues
