# FPP Smart Bulb Bridge (lean) — WiZ · Matter · Cync

Drives individual RGB smart bulbs from FPP channel data. Each bulb = 3 consecutive FPP channels (R,G,B).

```
FPP sequence/playlist ──► SmartBulbBridge channel output (C++, in fppd)
                              │  DDP over 127.0.0.1:4051 (changed chunks only)
                              ▼
                        bridge/bulbbridge.py  (one small asyncio daemon)
                              ├─► WiZ   : UDP setPilot to bulb IP :38899
                              ├─► Matter: a local Matter controller's WebSocket (port 5580)
                              └─► Cync  : pycync (cloud)
```

Latest value wins, per-bulb rate limits, a slow/offline bulb never blocks the others, failures retry with backoff,
and every 30 s the current color is re-sent (recovers bulbs that were power-cycled).

> **Folder name matters:** FPP finds a plugin's channel-output library by looking in a plugin folder named exactly like the output type, so this plugin's folder must be `SmartBulbBridge` (the Plugin Manager uses `repoName` from `pluginInfo.json` for this).


## Status (read this first)

| Part | State |
|---|---|
| WiZ bulbs | **Tested** on FPP 10.4 (Raspberry Pi), including sequence playback |
| Setup page (add/remove/save, input validation) | **Tested** on FPP 10.4 |
| Matter bulbs | **Tested on two bulbs** (the same Wi-Fi Matter RGBTW model) during sequence playback, using Matter.js Server 1.5.5. Other controllers and bulbs are untested. The plugin does **not** install a Matter controller or commission bulbs for you (see Setup) |
| Cync bulbs | **Tested** with five bulbs during sequence playback. The login session is saved, and bridge restarts reconnected without a new emailed code in repeated tests. Not yet tested over a multi-hour unattended run |
| Install through the FPP Plugin Manager | **Tested** by pasting this repository's GitHub URL. Not yet in the official catalog |

Not included (and not needed for FSEQ playback): making each bulb its own DDP controller in xLights.

## Setup
1. Install from the FPP Plugin Manager (after you publish the repo) or copy this folder to `/home/fpp/media/plugins/SmartBulbBridge`, then run `scripts/fpp_install.sh`.
2. Open the **Smart Bulbs** page (it appears in the FPP menus). For WiZ bulbs, click **Find WiZ bulbs** to list them on your network and **Add as a bulb**; each bulb gets three FPP channels (the next free block is filled in for you). Click **Save & restart bridge**; the plugin registers its channel output for you.
3. Click **Restart FPPD** when the page says so.
4. Test with FPP's Display Testing page (or a sequence) on your bulb channels. In xLights, give each bulb its own 3 channels; the bulbs do not need to be reachable by xLights, because FSEQ playback goes through FPP.

Config is `media/config/fpp-smart-bulbs.json` (see `examples/`). Logs: `media/logs/fpp-smart-bulbs.log`.

- **WiZ**: needs the bulb's IP (reserve it in DHCP). Add each bulb to the WiZ app first (that is how it joins your Wi-Fi) and enable "Local Communication" there. **Tip:** in the app, set the bulb's fade-in and fade-out to 0. A bulb that was never set up in the app keeps its default fade-out, which showed as roughly 2 seconds of lag when a bulb is switched off at the end of a sequence.
- **Matter**: you need a Matter controller running on the FPP that exposes the Matter server WebSocket on `ws://127.0.0.1:5580/ws`. The plugin does not install one (see `docs/MATTER.md`). It was tested with Matter.js Server 1.5.5 on Node.js, without Home Assistant or a hub. Once it is running, the **Matter bulbs** section of the Smart Bulbs page lists the controller's paired bulbs with an **Add as a bulb** button, and can pair a new bulb:
  - Easiest: set the bulb up in Alexa (or another Matter app), ask that app for a code to add the bulb to another app, and paste the code into the page right away (it expires after a few minutes). No Bluetooth needed.
  - A brand-new bulb that is not on Wi-Fi yet can be paired over the Pi's Bluetooth, if the controller has Bluetooth and your Wi-Fi details set (the page shows a checkbox only then).

  The command-line tool `tools/matter_pair.py` does the same from a terminal. Tested on two bulbs of one model; `--set-wifi` has never been run. The plugin sends On, then hue/saturation, then level.
- **Cync**: set up entirely on the Smart Bulbs page, in the **Cync bulbs** section. It runs as a separate worker in its own private Python environment, because `pycync` needs Python 3.13+ (FPP 10.4 ships it; older FPP versions need a newer Python first).
  1. Click **Install Cync support** (downloads a few small packages from the internet; a few minutes).
  2. Enter your Cync email and password and click **Sign in to Cync**. Cync emails you a code the first time; enter it on the page.
  3. Your color-capable bulbs are listed. Click **Add as a bulb** for each, then **Save & restart bridge**.

  The login session is saved in `cync-session.json` next to `auth.json` (owner-only files) and refreshed automatically. If Cync ever demands a new emailed code (for example after a long outage or a password change) the worker stops (exit 78) instead of retrying; sign in again on the page. Cync allows **only one connection per account**, so signing in can log you out of the Cync phone app. The page is usually served over plain HTTP, so your password crosses your local network unencrypted.

  Command-line alternative (same result), as the `fpp` user:
  ```
  D=/home/fpp/media/plugindata/SmartBulbBridge/cync; mkdir -p $D; chmod 700 $D
  python3 bridge/cync_setup.py --private-dir $D --prepare-with-network-downloads
  $D/venv/bin/python3 bridge/cync_onboard.py --auth-file $D/auth.json
  ```
- **Port 4051**, not 4049, so it can coexist with an older WizMatterBridge plugin. Don't map the same channels in both.

## First test (no FPP needed)
`python3 tools/wiz_test.py BULB_IP 255 0 0` sends red straight to one bulb (`0 0 0` = off; `--discover` finds WiZ bulbs). Do this before anything else.

## Tests
`python3 tests/test_bridge.py` (21 tests) and `python3 tests/test_cync_support.py` (13, including saved-session restore, token rotation and stale-session fallback) `python3 tests/test_cync_web.py` (4, the page's Cync sign-in helper) and `python3 tests/test_matter_web.py` (7, the page's Matter helper, using a fake server). Bridge tests cover: packet parsing, channel mapping, rate limit / latest-wins, dedupe, malformed packets, retry, isolation between slow and fast bulbs, config validation. Uses a fake WiZ bulb on loopback.

## What has been verified, and what has not

Verified on FPP 10.4 (Raspberry Pi):
- Install from GitHub through the FPP Plugin Manager; the native library builds and loads in fppd.
- FPP starts and stops the bridge daemon; the daemon runs as the FPP user.
- Setup page: add/remove/save, input validation, automatic channel-output registration.
- A WiZ bulb follows FPP channel data (Display Testing). FPP's start channel is 0-based and the plugin's channel numbers match.
- Cync: the private Python environment installs (Python 3.13, `pycync 0.6.1`, `aiohttp 3.14.5`), sign-in with the emailed code works and lists your bulbs, five bulbs follow a sequence, and restarts reuse the saved session.
- Matter: one bulb follows a sequence through the plugin.

Not yet verified:
- A multi-hour unattended Cync run. Cync allows **only one connection per account**, so this plugin cannot run alongside another program that is signed in to the same Cync account.
- Matter with controllers other than Matter.js Server, with other bulb models, and with many bulbs at once.
- Other FPP versions and hardware; the plugin declares FPP 10.4 or newer.
- Practical frame rates: WiZ over UDP handles about 10 updates/s per bulb; Cync (cloud) and Matter are much slower.

License: MIT (see `LICENSE`). Issues: https://github.com/smoothsa/SmartBulbBridge/issues

## Tuning Cync speed

All Cync commands share one cloud connection and are spaced `cync_interval_seconds` apart (default `0.25`). Lower it for faster response, for example `0.1`. Too low and the Cync service may start refusing commands; if the log shows repeated `command ... failed` lines, raise it again. Add it at the top level of `fpp-smart-bulbs.json`, then restart the bridge:

    "cync_interval_seconds": 0.1

The setup page keeps this setting when you save.
