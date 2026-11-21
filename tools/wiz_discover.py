#!/usr/bin/env python3
"""Find WiZ bulbs on the local network. Prints JSON: [{"ip": "...", "mac": "..."}].

  python3 tools/wiz_discover.py                 # broadcast on every IPv4 interface
  python3 tools/wiz_discover.py --targets 192.168.1.255 --wait 3

WiZ bulbs answer a UDP broadcast on port 38899 (Local Communication must be enabled in the WiZ app).
"""
import json, re, select, socket, subprocess, sys, time


def broadcast_targets():
    out = []
    try:
        txt = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=3).stdout
        out = re.findall(r"\bbrd (\d+\.\d+\.\d+\.\d+)", txt)
    except Exception:
        pass
    return sorted(set(out)) or ["255.255.255.255"]


def discover(targets, wait=3.0, port=38899):
    found = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.setblocking(False)
    msg = b'{"method":"getPilot","params":{}}'
    for t in targets:
        try:
            s.sendto(msg, (t, port))
        except OSError:
            pass
    end = time.time() + wait
    while time.time() < end:
        r, _, _ = select.select([s], [], [], 0.2)
        if not r:
            continue
        try:
            data, addr = s.recvfrom(4096)
            res = json.loads(data).get("result", {})
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(res, dict) and addr[0] not in found:
            found[addr[0]] = {"ip": addr[0], "mac": str(res.get("mac", ""))[:17]}
    s.close()
    return sorted(found.values(), key=lambda b: tuple(int(x) for x in b["ip"].split(".")))


if __name__ == "__main__":
    a = sys.argv[1:]
    targets, wait, port = None, 3.0, 38899
    while a:
        k = a.pop(0)
        if k == "--targets": targets = a.pop(0).split(",")
        elif k == "--wait": wait = float(a.pop(0))
        elif k == "--port": port = int(a.pop(0))
    print(json.dumps(discover(targets or broadcast_targets(), wait, port)))
