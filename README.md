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
| Matter bulbs | *Experimental.* Verified through the plugin with hand-sent colors on one real bulb (node 5) and a local Matter server. Not yet verified during sequence playback |
| Cync bulbs | **Experimental.** Sign-in and device discovery verified on real hardware. The plugin now saves the login session (owner-only file) so restarts should not need a new emailed code. This is covered by offline tests only; not yet proven on real hardware over a long run |
| Install through the FPP Plugin Manager | **Not yet tested** |

Not included (and not needed for FSEQ playback): making each bulb its own DDP controller in xLights.

## Setup
1. Install from the FPP Plugin Manager (after you publish the repo) or copy this folder to `/home/fpp/media/plugins/SmartBulbBridge`, then run `scripts/fpp_install.sh`.
2. Open the **Smart Bulbs** page (it appears in the FPP menus). For WiZ bulbs, click **Find WiZ bulbs** to list them on your network and **Add as a bulb**; each bulb gets three FPP channels (the next free block is filled in for you). Click **Save & restart bridge**; the plugin registers its channel output for you.
3. Click **Restart FPPD** when the page says so.
4. Test with FPP's Display Testing page (or a sequence) on your bulb channels. In xLights, give each bulb its own 3 channels; the bulbs do not need to be reachable by xLights, because FSEQ playback goes through FPP.

Config is `media/config/fpp-smart-bulbs.json` (see `examples/`). Logs: `media/logs/fpp-smart-bulbs.log`.

- **WiZ**: needs the bulb's IP (reserve it in DHCP). Enable "Local Communication" in the WiZ app.
- **Matter**: run `python-matter-server` on the Pi (ws://127.0.0.1:5580/ws), commission bulbs there, enter each bulb's node ID.
- **Cync**: runs as a separate worker in its own Python venv, because `pycync` reportedly needs Python 3.13+. FPP 10.4 ships Python 3.13.5, so `python3` works there; older FPP versions need a newer Python installed first. Run once, as the `fpp` user (not root):
  ```
  D=/home/fpp/media/plugindata/SmartBulbBridge/cync; mkdir -p $D; chmod 700 $D
  python3 bridge/cync_setup.py --private-dir $D --prepare-with-network-downloads
  $D/venv/bin/python3 bridge/cync_onboard.py --auth-file $D/auth.json     # asks for email, password, emailed 2FA code
  ```
  Then add Cync bulbs on the page (device IDs are printed by onboarding). The worker starts with fppd. The login session is saved in `cync-session.json` next to `auth.json` and refreshed automatically. If Cync ever demands a new email code (for example after a long outage or a password change) it stops (exit 78) instead of retrying; re-run onboarding.
- **Port 4051**, not 4049, so it can coexist with an older WizMatterBridge plugin. Don't map the same channels in both.

## First test (no FPP needed)
`python3 tools/wiz_test.py BULB_IP 255 0 0` sends red straight to one bulb (`0 0 0` = off; `--discover` finds WiZ bulbs). Do this before anything else.

## Tests
`python3 tests/test_bridge.py` (21 tests) and `python3 tests/test_cync_support.py` (12, including saved-session restore, token rotation and stale-session fallback). Bridge tests cover: packet parsing, channel mapping, rate limit / latest-wins, dedupe, malformed packets, retry, isolation between slow and fast bulbs, config validation. Uses a fake WiZ bulb on loopback.

## What has been verified, and what has not

Verified on FPP 10.4 (Raspberry Pi 5):
- Install from GitHub through the FPP Plugin Manager; the native library builds and loads in fppd.
- FPP starts and stops the bridge daemon; the daemon runs as the FPP user.
- Setup page: add/remove/save, input validation, automatic channel-output registration.
- A WiZ bulb follows FPP channel data (Display Testing). FPP's start channel is 0-based and the plugin's channel numbers match.
- Cync: the private Python environment installs (Python 3.13, `pycync 0.6.1`, `aiohttp 3.14.5`), sign-in with the emailed code works and lists your bulbs.

Not yet verified:
- Cync during playback, and unattended restarts (see the status table). Cync allows **only one connection per account**, so this plugin cannot run alongside another program that is signed in to the same Cync account.
- Matter during sequence playback (it works with hand-sent colors through the plugin).
- Other FPP versions and hardware; the plugin declares FPP 10.4 or newer.
- Practical frame rates: WiZ over UDP handles about 10 updates/s per bulb; Cync (cloud) and Matter are much slower.

License: MIT (see `LICENSE`). Issues: https://github.com/smoothsa/SmartBulbBridge/issues

## Tuning Cync speed

All Cync commands share one cloud connection and are spaced `cync_interval_seconds` apart (default `0.25`). Lower it for faster response, for example `0.1`. Too low and the Cync service may start refusing commands; if the log shows repeated `command ... failed` lines, raise it again. Add it at the top level of `fpp-smart-bulbs.json`, then restart the bridge:

    "cync_interval_seconds": 0.1

The setup page keeps this setting when you save.
