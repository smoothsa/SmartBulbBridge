#!/usr/bin/env python3
"""FPP Smart Bulb Bridge daemon (WiZ / Matter / Cync).

Receives DDP-style packets from the plugin's FPP channel output on
127.0.0.1:<listen_port>, keeps the latest RGB per bulb, and sends it to the
bulb at a safe rate. Latest value wins: if a bulb is slower than the show,
intermediate colors are dropped, never queued.
"""
import asyncio, colorsys, json, os, signal, socket, struct, sys, time, uuid

DEFAULT_INTERVAL = {"wiz": 0.10, "matter": 0.25, "cync": 0.30}
MATTER_CONCURRENCY = 4


# ---------------------------------------------------------------- config
def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("listen_port", 4051)
    cfg.setdefault("refresh_seconds", 30)
    cfg.setdefault("idle_release_seconds", 120)   # stop re-sending after this long with no FPP data; 0 = never
    cfg.setdefault("matter_url", "ws://127.0.0.1:5580/ws")
    cfg.setdefault("cync_socket", "")
    bulbs, seen = [], set()
    for i, b in enumerate(cfg.get("bulbs", [])):
        if not b.get("enabled", True):
            continue
        proto = b.get("protocol")
        if proto not in DEFAULT_INTERVAL:
            raise ValueError(f"bulb {i}: protocol must be wiz, matter or cync")
        ch = b.get("channel")
        if type(ch) is not int or ch < 1:
            raise ValueError(f"bulb {i}: channel must be an integer >= 1")
        for c in range(ch, ch + 3):
            if c in seen:
                raise ValueError(f"bulb {i}: channel {c} used by another bulb")
            seen.add(c)
        if proto == "wiz" and not b.get("ip"):
            raise ValueError(f"bulb {i}: wiz needs ip")
        if proto == "matter" and type(b.get("node_id")) is not int:
            raise ValueError(f"bulb {i}: matter needs integer node_id")
        if proto == "cync" and type(b.get("cync_id")) is not int:
            raise ValueError(f"bulb {i}: cync needs integer cync_id")
        b = dict(b)
        b.setdefault("name", f"{proto}-{ch}")
        b.setdefault("endpoint_id", 1)
        b.setdefault("min_interval", DEFAULT_INTERVAL[proto])
        bulbs.append(b)
    cfg["bulbs"] = bulbs
    return cfg


# ---------------------------------------------------------------- senders
class WizSender:
    async def __call__(self, bulb, rgb):
        params = ({"state": False} if rgb == (0, 0, 0) else
                  {"state": True, "r": rgb[0], "g": rgb[1], "b": rgb[2], "dimming": 100})
        data = json.dumps({"method": "setPilot", "params": params},
                          separators=(",", ":")).encode()
        loop = asyncio.get_running_loop()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setblocking(False)
            await loop.sock_sendto(s, data, (bulb["ip"], bulb.get("port", 38899)))


class MatterSender:
    """Talks to a local python-matter-server over WebSocket (device_command)."""

    def __init__(self, url):
        self.url, self.ws, self.pending = url, None, {}
        self.lock, self.sem = asyncio.Lock(), asyncio.Semaphore(MATTER_CONCURRENCY)

    async def _connect(self):
        if self.ws is not None:
            return self.ws
        async with self.lock:
            if self.ws is None:
                import websockets
                ws = await asyncio.wait_for(websockets.connect(self.url), 5)
                await asyncio.wait_for(ws.recv(), 5)  # server greeting
                self.ws = ws
                asyncio.create_task(self._reader(ws))
        return self.ws

    async def _reader(self, ws):
        try:
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                fut = self.pending.pop(m.get("message_id"), None) if isinstance(m, dict) else None
                if fut and not fut.done():
                    if "error_code" in m:
                        fut.set_exception(RuntimeError(f"matter error {m['error_code']}"))
                    else:
                        fut.set_result(m.get("result"))
        except Exception:
            pass
        finally:
            if self.ws is ws:
                self.ws = None
            for f in self.pending.values():
                if not f.done():
                    f.set_exception(ConnectionError("matter socket closed"))
            self.pending.clear()

    async def _cmd(self, bulb, cluster, name, payload=None):
        ws = await self._connect()
        mid = str(uuid.uuid4())
        fut = asyncio.get_running_loop().create_future()
        self.pending[mid] = fut
        await ws.send(json.dumps({"message_id": mid, "command": "device_command", "args": {
            "node_id": bulb["node_id"], "endpoint_id": bulb["endpoint_id"],
            "cluster_id": cluster, "command_name": name, "payload": payload or {}}}))
        try:
            return await asyncio.wait_for(fut, 5)
        finally:
            self.pending.pop(mid, None)

    async def __call__(self, bulb, rgb):
        async with self.sem:
            if rgb == (0, 0, 0):
                return await self._cmd(bulb, 6, "Off")
            h, s, v = colorsys.rgb_to_hsv(*(x / 255 for x in rgb))
            opts = {"transitionTime": 0, "optionsMask": 0, "optionsOverride": 0}
            await self._cmd(bulb, 6, "On")
            await self._cmd(bulb, 768, "MoveToHueAndSaturation",
                            {"hue": round(h * 254), "saturation": round(s * 254), **opts})
            return await self._cmd(bulb, 8, "MoveToLevelWithOnOff",
                                   {"level": max(1, round(v * 254)), **opts})


class CyncSender:
    """Sends to the isolated Cync worker (cync_worker_entry.py) over a Unix socket.

    pycync needs a newer Python than FPP ships, so it lives in its own venv/process.
    A reply of 'queued' means the worker accepted it, not that the bulb changed.
    """

    def __init__(self, socket_path):
        self.path = socket_path

    async def __call__(self, bulb, rgb):
        if not self.path:
            raise RuntimeError("cync_socket not configured")
        reader, writer = await asyncio.open_unix_connection(self.path, limit=1024)
        try:
            writer.write((json.dumps({"device_id": bulb["cync_id"], "rgb": list(rgb)},
                                     separators=(",", ":")) + "\n").encode())
            await writer.drain()
            msg = json.loads(await reader.readline() or b"{}")
            if msg.get("status") not in ("queued", "duplicate"):
                raise RuntimeError("cync worker rejected command")
        finally:
            writer.close()


# ---------------------------------------------------------------- core
class Slot:
    def __init__(self, bulb):
        self.bulb, self.wanted, self.delivered = bulb, None, None
        self.sent_at, self.event = 0.0, asyncio.Event()
        self.ok = self.fail = 0
        self.last_error = ""


class Bridge:
    def __init__(self, cfg, senders=None, log=print):
        self.cfg, self.log = cfg, log
        self.slots = [Slot(b) for b in cfg["bulbs"]]
        self.buf = bytearray(max([s.bulb["channel"] + 2 for s in self.slots] + [3]))
        self.senders = senders or {
            "wiz": WizSender(),
            "matter": MatterSender(cfg["matter_url"]),
            "cync": CyncSender(cfg["cync_socket"]),
        }
        self.packets = self.bad_packets = 0
        self.last_packet = 0.0
        self.tasks = []

    # --- DDP ingest (header: flags, seq, type, id, offset[4], length[2])
    def handle_packet(self, pkt):
        if len(pkt) < 10 or (pkt[0] >> 6) != 1:
            self.bad_packets += 1
            return
        off, length = struct.unpack(">IH", pkt[4:10])
        start = 14 if pkt[0] & 0x10 else 10          # optional timecode
        data = pkt[start:start + length]
        if len(data) != length or off >= len(self.buf):
            self.bad_packets += 1
            return
        data = data[:len(self.buf) - off]
        self.buf[off:off + len(data)] = data
        self.packets += 1
        self.last_packet = time.monotonic()
        for s in self.slots:
            i = s.bulb["channel"] - 1
            if off < i + 3 and i < off + len(data):      # packet touches this bulb's R,G,B
                rgb = tuple(self.buf[i:i + 3])
                if rgb != s.wanted:
                    s.wanted = rgb
                    s.event.set()

    # --- per-bulb worker: latest value wins, bounded rate, retry w/ backoff
    async def worker(self, s):
        refresh = self.cfg["refresh_seconds"]
        backoff = 0.5
        while True:
            try:
                await asyncio.wait_for(s.event.wait(), timeout=refresh or None)
            except asyncio.TimeoutError:
                # Periodic re-send (recovers power-cycled bulbs) -- but only while FPP is
                # actually sending data. When FPP goes idle we let go of the bulbs so people
                # can use them normally (e.g. from the WiZ app) between shows.
                idle = self.cfg["idle_release_seconds"]
                if idle and time.monotonic() - self.last_packet > idle:
                    continue
                if s.delivered is not None:
                    s.sent_at = 0.0
                    s.wanted = s.delivered
            s.event.clear()
            rgb = s.wanted
            if rgb is None or (rgb == s.delivered and
                               (not refresh or time.monotonic() - s.sent_at < refresh)):
                continue
            try:
                await asyncio.wait_for(self.senders[s.bulb["protocol"]](s.bulb, rgb), 8)
                s.delivered, s.sent_at, backoff = rgb, time.monotonic(), 0.5
                s.ok += 1
                s.first_fail = None
            except Exception as e:
                s.fail += 1
                s.last_error = f"{type(e).__name__}: {e}"[:200]
                now = time.monotonic()
                if getattr(s, "first_fail", None) is None:
                    s.first_fail = now
                # The Cync worker opens its socket about a second after the bridge
                # starts; that is a normal start-up wait, not worth logging unless
                # it lasts.
                starting = (s.bulb["protocol"] == "cync" and
                            isinstance(e, (FileNotFoundError, ConnectionRefusedError)) and
                            now - s.first_fail < 20)
                if not starting and (s.fail <= 3 or s.fail % 100 == 0):
                    self.log(f"{s.bulb['name']}: send failed ({s.last_error})")
                s.event.set()                        # retry with latest value
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5)
            await asyncio.sleep(s.bulb["min_interval"])

    def status(self):
        return {"time": time.time(), "packets": self.packets, "bad_packets": self.bad_packets,
                "bulbs": [{"name": s.bulb["name"], "protocol": s.bulb["protocol"],
                           "channel": s.bulb["channel"], "rgb": s.delivered,
                           "ok": s.ok, "fail": s.fail, "last_error": s.last_error}
                          for s in self.slots]}

    async def start(self, host="127.0.0.1"):
        loop = asyncio.get_running_loop()
        bridge = self

        class Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                bridge.handle_packet(data)

        self.transport, _ = await loop.create_datagram_endpoint(
            Proto, local_addr=(host, self.cfg["listen_port"]))
        self.port = self.transport.get_extra_info("sockname")[1]
        self.tasks = [asyncio.create_task(self.worker(s)) for s in self.slots]

    async def stop(self):
        self.transport.close()
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


async def amain(path, status_path, pidfile=None):
    def log(m):
        print(time.strftime("%F %T"), m, flush=True)
    cfg = load_config(path)
    bridge = Bridge(cfg, log=log)
    await bridge.start()
    if pidfile:
        with open(pidfile, "w") as f:
            f.write(str(os.getpid()))
    log(f"listening 127.0.0.1:{bridge.port}, {len(bridge.slots)} bulbs")
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    while not stop.is_set():
        if status_path:
            tmp = status_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(bridge.status(), f)
            os.replace(tmp, status_path)
        try:
            await asyncio.wait_for(stop.wait(), 2)
        except asyncio.TimeoutError:
            pass
    await bridge.stop()
    if pidfile:
        try:
            os.unlink(pidfile)
        except OSError:
            pass
    log("stopped")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="FPP smart bulb bridge daemon")
    ap.add_argument("config")
    ap.add_argument("status", nargs="?")
    ap.add_argument("--pidfile")
    a = ap.parse_args()
    asyncio.run(amain(a.config, a.status, a.pidfile))
