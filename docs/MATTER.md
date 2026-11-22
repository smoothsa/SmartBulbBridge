# Matter setup notes

The plugin does **not** install a Matter controller or pair bulbs. It talks to a Matter server that is already running, over a WebSocket on `ws://127.0.0.1:5580/ws`, and sends commands to bulbs that are already paired ("commissioned") to it.

This page records what worked on one setup. I have **not** tested a fresh install by following it, so treat it as a starting point.

## What was used (tested)

- FPP 10.4 on a Raspberry Pi, with the bulb on 2.4 GHz Wi-Fi.
- [Matter.js Server](https://github.com/matter-js/matterjs-server) 1.5.5 (npm package `matter-server`), run on Node.js 24, as a systemd service under the `fpp` user. No Home Assistant, hub or Thread border router.
- The bulb was paired over the Pi's own Bluetooth, using the bulb's Matter setup code.
- Two bulbs of the same model (OREiN A19 RGBTW), as nodes 5 and 6, endpoint 1. Node 5 was paired over Bluetooth, node 6 over the network after being set up with Alexa.

## The service

Service file (`/etc/systemd/system/fpp-matter-controller.service`), with the paths adjusted to wherever you installed it:

    [Unit]
    Description=Matter.js Server
    Wants=network-online.target
    After=network-online.target

    [Service]
    Type=simple
    User=fpp
    Group=fpp
    WorkingDirectory=/home/fpp/matter-runtime/server
    Environment=PATH=/home/fpp/matter-runtime/node24/bin:/usr/local/bin:/usr/bin:/bin
    Environment=NOBLE_BINDINGS=dbus
    ExecStart=/home/fpp/matter-runtime/server/node_modules/.bin/matter-server --storage-path /home/fpp/matter-runtime/data --port 5580 --listen-address 127.0.0.1 --primary-interface wlan0 --log-level info --bluetooth-adapter 0
    Restart=on-failure
    RestartSec=10

    [Install]
    WantedBy=multi-user.target

Notes:

- **`--listen-address 127.0.0.1` matters.** By default the server listens on every network interface, and the plugin's connection has no password, so anyone on your network could control your bulbs.
- `--primary-interface wlan0` is this Pi's Wi-Fi interface. Use yours.
- `--bluetooth-adapter 0` is `hci0`. List adapters with `ls /sys/class/bluetooth/`.
- `NOBLE_BINDINGS=dbus` is the Bluetooth backend setting used here. It is not in the project's own documentation that I read, so it may not be needed on your system. It was set in a drop-in file on the setup this page describes.
- `--storage-path` is where paired bulbs are remembered. Back it up.

## Check it and pair a bulb

    python3 tools/matter_pair.py --check      # server version, Bluetooth and Wi-Fi status (read-only)
    python3 tools/matter_pair.py --set-wifi   # UNTESTED: send your 2.4 GHz Wi-Fi name and password to the server
    python3 tools/matter_pair.py --pair       # asks for the bulb's setup code (hidden), pairs it over Bluetooth, prints the node ID

`--pair` refuses to run unless `--check` reports Bluetooth and Wi-Fi credentials as ready. On the setup above, the Wi-Fi credentials were supplied during the original install and I did not record the step, which is why `--set-wifi` was added afterwards without being tested. If it does not work, set them through your Matter server's own interface.

### If the bulb is already on your Wi-Fi (for example set up with Alexa)

Matter bulbs can be paired to more than one controller. In the app that already has the bulb (Alexa, for example), ask for a code to add it to another app. The code expires after a few minutes, so start the tool first:

    python3 tools/matter_pair.py --pair --network-only

It asks for the code with hidden input, then pairs over your network. No Bluetooth and no Wi-Fi password are needed. **Tested once:** an OREiN bulb set up with Alexa was paired this way (it got its own node ID) and then followed a sequence. The bulb and the Matter server must be on the same network segment, because the server finds the bulb with multicast DNS, which does not cross subnets.

The same pairing is available on the **Smart Bulbs** page (Matter bulbs section), without a terminal.

Then add the bulb on the **Smart Bulbs** page with type Matter and the node ID that `--pair` printed. The endpoint defaults to 1.

## What the plugin sends

For a non-black color: On, then MoveToHueAndSaturation, then MoveToLevelWithOnOff. For black: Off. Other bulbs may behave differently; the plugin uses hue and saturation because the XY color command showed wrong colors on the bulb tested.
