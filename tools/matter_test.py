#!/usr/bin/env python3
"""Talk straight to a local Matter server and try to color one bulb. No FPP, no bridge.

  python3 tools/matter_test.py 5                 # node 5, endpoint 1, ws://127.0.0.1:5580/ws
  python3 tools/matter_test.py 5 --endpoint 1 --url ws://127.0.0.1:5580/ws

Prints the server's raw replies so we can see WHY a bulb does or doesn't change.
Watch the bulb: it should go red (hue/saturation), then red again (XY), then off.
"""
import argparse, asyncio, json, sys, uuid

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("node", type=int)
    ap.add_argument("--endpoint", type=int, default=1)
    ap.add_argument("--url", default="ws://127.0.0.1:5580/ws")
    a = ap.parse_args()
    try:
        import websockets
    except ImportError:
        print("python3-websockets is missing. Install it with:  sudo apt install python3-websockets"); return 2
    async with websockets.connect(a.url) as ws:
        print("server hello:", (await asyncio.wait_for(ws.recv(), 5))[:300])

        async def call(command, args):
            mid = str(uuid.uuid4())
            await ws.send(json.dumps({"message_id": mid, "command": command, "args": args}))
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if m.get("message_id") == mid:
                    return m

        def show(label, m):
            ok = "error_code" not in m
            print(f"{'OK  ' if ok else 'FAIL'} {label}: {json.dumps(m)[:400]}")
            return ok

        n = await call("get_node", {"node_id": a.node})
        res = n.get("result") or {}
        attrs = res.get("attributes", {}) if isinstance(res, dict) else {}
        print("node available:", res.get("available") if isinstance(res, dict) else n)
        e = a.endpoint
        for k in sorted(attrs):
            if k.startswith((f"{e}/6/", f"{e}/8/", f"{e}/768/")) and not k.endswith(("/65528", "/65529", "/65531")):
                print("  attr", k, "=", attrs[k])
        caps = attrs.get(f"{e}/768/16400")
        if caps is not None:
            print(f"ColorCapabilities={caps}: HueSat={bool(caps & 1)} XY={bool(caps & 8)} ColorTemp={bool(caps & 16)}")

        def dc(cluster, name, payload=None):
            return call("device_command", {"node_id": a.node, "endpoint_id": e, "cluster_id": cluster,
                                           "command_name": name, "payload": payload or {}})
        opt = {"transitionTime": 0, "optionsMask": 0, "optionsOverride": 0}
        print("\n--- 1. ON, then hue/saturation RED")
        show("On", await dc(6, "On"))
        show("MoveToHueAndSaturation", await dc(768, "MoveToHueAndSaturation", {"hue": 0, "saturation": 254, **opt}))
        show("MoveToLevelWithOnOff", await dc(8, "MoveToLevelWithOnOff", {"level": 254, **opt}))
        await asyncio.sleep(5)
        print("\n--- 2. XY RED")
        show("MoveToColor", await dc(768, "MoveToColor", {"colorX": 41943, "colorY": 21627, **opt}))
        await asyncio.sleep(5)
        print("\n--- 3. OFF")
        show("Off", await dc(6, "Off"))
    print("\nTell me: which step (1 or 2) turned the bulb red, if any?")
    return 0

sys.exit(asyncio.run(main()))
