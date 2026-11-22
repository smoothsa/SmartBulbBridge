#!/usr/bin/env python3
"""Check a local Matter controller, and commission one Wi-Fi Matter bulb over Bluetooth.

  python3 tools/matter_pair.py --check     # read-only: is the server up, is Bluetooth/Wi-Fi ready?
  python3 tools/matter_pair.py --set-wifi  # UNTESTED: gives the server your 2.4 GHz Wi-Fi name and password (hidden)
  python3 tools/matter_pair.py --pair      # asks for the bulb's Matter setup code (hidden), then pairs it over Bluetooth
  python3 tools/matter_pair.py --pair --network-only
                                           # tested on one bulb: the bulb is already on your Wi-Fi
                                           # (e.g. set up in Alexa); paste the temporary code Alexa gives you.
                                           # No Bluetooth or Wi-Fi password needed. Start it, then get the code.

Talks to the Matter server WebSocket (default ws://127.0.0.1:5580/ws; override with --url).
On success it prints the bulb's node ID: enter that on the Smart Bulbs page.
Does not touch FPP outputs, the bridge or its services. Needs a controller that is already running.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import re
import sys
import uuid
from pathlib import Path

SERVER = "ws://127.0.0.1:5580/ws"   # overridden by --url
KNOWN_REASONS = {
    "not-discovered", "passcode-mismatch", "invalid-payload",
    "attestation-rejected", "timeout", "other",
}


def clean_code(code: str) -> str:
    code = code.strip()
    if re.fullmatch(r"(?:\d{11}|\d{21}|MT:[A-Z0-9.\-]{1,509})", code):
        return code
    raise ValueError("Invalid setup code format (expected 11/21 digits or Matter MT: QR payload)")


def summarize_error(reply: dict) -> dict:
    reason = reply.get("reason")
    if reason not in KNOWN_REASONS:
        reason = "not-provided"
    code = reply.get("error_code")
    return {
        "commissioned": False,
        "error_code": code if type(code) is int else "unspecified",
        "reason": reason,
        "details_suppressed": True,
    }


async def run(pair: bool, server: str = SERVER, set_wifi: bool = False, network_only: bool = False) -> int:
    try:
        import websockets
    except ImportError:
        print("WebSocket dependency is not available to this Python interpreter")
        return 2

    # Discover Bluetooth HCI interfaces without printing addresses.
    hci = sorted(p.name for p in Path('/sys/class/bluetooth').glob('hci*'))
    print("Host Bluetooth adapters:", ', '.join(hci) if hci else '(none detected)')

    try:
        async with websockets.connect(server, open_timeout=10, max_size=1024*1024) as ws:
            greeting = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if not isinstance(greeting, dict):
                print("Unexpected Matter Server greeting")
                return 2
            bluetooth = greeting.get('bluetooth_enabled') is True
            wifi = greeting.get('wifi_credentials_set') is True
            print("Matter Server:", str(greeting.get('sdk_version', 'unknown'))[:80])
            print("Bluetooth commissioning enabled:", bluetooth)
            print("Wi-Fi credentials set:", wifi)
            if set_wifi:
                # Uses the python-matter-server style "set_wifi_credentials" command, which
                # Matter.js Server is documented to follow. Not verified on a real server.
                ssid = input('Wi-Fi name (2.4 GHz): ').strip()
                password = getpass.getpass('Wi-Fi password (hidden): ')
                if not ssid or not password:
                    print("Both a Wi-Fi name and password are required")
                    return 2
                mid = uuid.uuid4().hex
                await ws.send(json.dumps({"message_id": mid, "command": "set_wifi_credentials",
                                          "args": {"ssid": ssid, "credentials": password}}))
                del password
                while True:
                    reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
                    if isinstance(reply, dict) and reply.get('message_id') == mid:
                        break
                ok = 'error_code' not in reply and 'error' not in reply
                print(json.dumps({"wifi_credentials_set": ok}))
                return 0 if ok else 1
            if not pair:
                print("CHECK_ONLY: no pairing request sent")
                return 0
            if not network_only and (not hci or not bluetooth or not wifi):
                print("BLOCKED: Bluetooth adapter, Bluetooth commissioning, or Wi-Fi credentials missing")
                print("(If the bulb is already on your Wi-Fi, for example set up in Alexa, use --network-only)")
                return 2

            code = clean_code(getpass.getpass('Matter code (hidden): '))
            print("Starting one network-only commissioning attempt; this can take a minute or two." if network_only else
                  "Starting one Bluetooth-assisted commissioning attempt; response may take several minutes.", flush=True)
            mid = uuid.uuid4().hex
            await ws.send(json.dumps({
                "message_id": mid,
                "command": "commission_with_code",
                "args": {"code": code, "network_only": network_only},
            }))
            # Drop our only reference after sending; never print it.
            del code
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 360
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    print(json.dumps({"commissioned": False, "reason": "client-timeout-360s"}))
                    return 1
                reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
                if not isinstance(reply, dict) or reply.get('message_id') != mid:
                    continue
                if 'error_code' in reply or 'error' in reply:
                    print(json.dumps(summarize_error(reply), sort_keys=True))
                    return 1
                if 'result' in reply:
                    result = reply['result']
                    node_id = result.get('node_id') if isinstance(result, dict) else result
                    if isinstance(node_id, (int, str)) and str(node_id).isdigit():
                        print(json.dumps({"commissioned": True, "node_id": str(node_id)}))
                    else:
                        print(json.dumps({"commissioned": True, "node_id": "verify-with-list"}))
                    return 0
                print(json.dumps({"commissioned": False, "reason": "unexpected-response"}))
                return 1
    except (TimeoutError, asyncio.TimeoutError):
        print(json.dumps({"commissioned": False, "reason": "connection-or-request-timeout"}))
        return 1
    except ValueError as exc:
        # This is our own input syntax validation, not a server exception.
        print(str(exc))
        return 2
    except Exception as exc:
        # Avoid printing third-party exception strings which might contain secrets.
        print(json.dumps({"commissioned": False, "error_type": type(exc).__name__, "details_suppressed": True}))
        return 1


def main():
    parser = argparse.ArgumentParser(description='Private, local Matter commissioning diagnostics')
    parser.add_argument('--url', default=SERVER, help='Matter server WebSocket URL')
    parser.add_argument('--network-only', action='store_true', help='with --pair: bulb is already on Wi-Fi, no Bluetooth')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--check', action='store_true', help='Read-only checks')
    group.add_argument('--set-wifi', action='store_true', help='UNTESTED: send Wi-Fi credentials to the server (hidden input)')
    group.add_argument('--pair', action='store_true', help='Prompt privately and attempt BLE pairing')
    args = parser.parse_args()
    if (args.pair or args.set_wifi) and not sys.stdin.isatty():
        parser.error('--pair and --set-wifi need an interactive terminal for hidden input')
    if args.network_only and not args.pair:
        parser.error('--network-only is only used with --pair')
    return asyncio.run(run(args.pair, args.url, args.set_wifi, args.network_only))


if __name__ == '__main__':
    sys.exit(main())
