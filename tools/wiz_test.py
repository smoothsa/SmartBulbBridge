#!/usr/bin/env python3
"""Send one color straight to a WiZ bulb. No FPP, no daemon, no config.

  python3 tools/wiz_test.py 192.168.1.50 255 0 0     # red
  python3 tools/wiz_test.py 192.168.1.50 0 0 0       # off
  python3 tools/wiz_test.py 192.168.1.50 --discover  # find WiZ bulbs on the LAN (broadcast getPilot)
"""
import json, socket, sys, time

def main(a):
    if len(a) == 2 and a[1] == "--discover":
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1); s.settimeout(0.5)
        s.sendto(b'{"method":"getPilot","params":{}}', ("255.255.255.255", 38899))
        end, seen = time.time() + 3, set()
        while time.time() < end:
            try:
                d, addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            if addr[0] not in seen:
                seen.add(addr[0]); print("WiZ bulb:", addr[0], d.decode(errors="replace")[:120])
        print(f"{len(seen)} bulb(s) found"); return 0
    if len(a) != 5:
        print(__doc__); return 2
    ip, rgb = a[1], tuple(int(x) for x in a[2:5])
    if any(not 0 <= x <= 255 for x in rgb):
        print("RGB values must be 0-255"); return 2
    params = {"state": False} if rgb == (0, 0, 0) else {"state": True, "r": rgb[0], "g": rgb[1], "b": rgb[2], "dimming": 100}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(2)
    s.sendto(json.dumps({"method": "setPilot", "params": params}).encode(), (ip, 38899))
    try:
        print("bulb replied:", s.recvfrom(1024)[0].decode(errors="replace"))
    except socket.timeout:
        print("sent, but no reply: check IP, and that Local Communication is enabled in the WiZ app")
        return 1
    return 0

sys.exit(main(sys.argv))
