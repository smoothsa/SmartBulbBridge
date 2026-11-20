import asyncio, json, socket, struct, sys, unittest
sys.path.insert(0, __file__.rsplit("/tests", 1)[0] + "/bridge")
import bulbbridge as bb


def ddp(offset, data, flags=0x41):
    return struct.pack(">BBBBIH", flags, 0, 1, 1, offset, len(data)) + bytes(data)


class FakeWiz(asyncio.DatagramProtocol):
    def __init__(self): self.got = []
    def datagram_received(self, data, addr): self.got.append(json.loads(data))


def cfg(bulbs, **kw):
    return {"listen_port": 0, "refresh_seconds": 0, "matter_url": "", "cync_socket": "",
            "bulbs": bulbs, **kw}


class T(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        loop = asyncio.get_running_loop()
        self.wiz = FakeWiz()
        self.tr, _ = await loop.create_datagram_endpoint(lambda: self.wiz, local_addr=("127.0.0.1", 0))
        self.wport = self.tr.get_extra_info("sockname")[1]

    async def asyncTearDown(self): self.tr.close()

    def wizbulb(self, ch, **kw):
        return {"protocol": "wiz", "ip": "127.0.0.1", "port": self.wport, "channel": ch, **kw}

    async def start(self, bulbs, **kw):
        import tempfile, os
        p = tempfile.mktemp(suffix=".json"); open(p, "w").write(json.dumps(cfg(bulbs, **kw)))
        b = bb.Bridge(bb.load_config(p), log=lambda m: None); os.unlink(p)
        await b.start(); self.b = b
        self.addAsyncCleanup(b.stop)
        return b

    def send(self, b, pkt):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(pkt, ("127.0.0.1", b.port)); s.close()

    async def test_color_reaches_bulb_and_black_turns_off(self):
        b = await self.start([self.wizbulb(10)])
        self.send(b, ddp(9, [255, 128, 0])); await asyncio.sleep(0.3)
        self.assertEqual(self.wiz.got[-1], {"method": "setPilot", "params":
                         {"state": True, "r": 255, "g": 128, "b": 0, "dimming": 100}})
        self.send(b, ddp(9, [0, 0, 0])); await asyncio.sleep(0.3)
        self.assertEqual(self.wiz.got[-1]["params"], {"state": False})

    async def test_offset_inside_larger_packet_and_two_bulbs(self):
        b = await self.start([self.wizbulb(1), self.wizbulb(4)])
        self.send(b, ddp(0, [1, 2, 3, 4, 5, 6])); await asyncio.sleep(0.3)
        colors = sorted((m["params"]["r"], m["params"]["g"], m["params"]["b"]) for m in self.wiz.got)
        self.assertEqual(colors, [(1, 2, 3), (4, 5, 6)])

    async def test_latest_value_wins_and_rate_limited(self):
        b = await self.start([self.wizbulb(1, min_interval=0.2)])
        for i in range(1, 101):                      # 100 updates in a burst
            self.send(b, ddp(0, [i, 0, 0]))
        await asyncio.sleep(0.7)
        self.assertLess(len(self.wiz.got), 8)        # far fewer than 100
        self.assertEqual(self.wiz.got[-1]["params"]["r"], 100)   # final color delivered

    async def test_unchanged_not_resent(self):
        b = await self.start([self.wizbulb(1)])
        for _ in range(5):
            self.send(b, ddp(0, [9, 9, 9])); await asyncio.sleep(0.15)
        self.assertEqual(len(self.wiz.got), 1)

    async def test_partial_and_malformed_packets_ignored(self):
        b = await self.start([self.wizbulb(1)])
        self.send(b, b"junk"); self.send(b, ddp(0, [1, 2, 3], flags=0x01))   # bad version
        self.send(b, ddp(1_000_000, [1, 2, 3]))      # far outside range
        self.send(b, struct.pack(">BBBBIH", 0x41, 0, 1, 1, 0, 50) + b"abc")  # truncated
        await asyncio.sleep(0.3)
        self.assertEqual(self.wiz.got, [])
        self.assertGreaterEqual(b.bad_packets, 3)

    async def test_failure_retries_with_latest_color(self):
        calls = []
        async def flaky(bulb, rgb):
            calls.append(rgb)
            if len(calls) < 3: raise ConnectionError("boom")
        b = await self.start([self.wizbulb(1)]); b.senders["wiz"] = flaky
        self.send(b, ddp(0, [1, 1, 1])); await asyncio.sleep(0.05)
        self.send(b, ddp(0, [2, 2, 2])); await asyncio.sleep(2.5)
        self.assertEqual(calls[-1], (2, 2, 2))
        self.assertEqual(b.slots[0].delivered, (2, 2, 2)); self.assertGreaterEqual(b.slots[0].fail, 2)

    async def test_slow_bulb_does_not_block_others(self):
        async def slow(bulb, rgb): await asyncio.sleep(5)
        b = await self.start([{"protocol": "cync", "cync_id": 1, "channel": 1}, self.wizbulb(4)])
        b.senders["cync"] = slow
        self.send(b, ddp(0, [1, 2, 3, 4, 5, 6])); await asyncio.sleep(0.3)
        self.assertEqual(len(self.wiz.got), 1)


    async def test_refresh_stops_when_fpp_goes_idle(self):
        b = await self.start([self.wizbulb(1)], refresh_seconds=0.2, idle_release_seconds=0.6)
        self.send(b, ddp(0, [9, 9, 9])); await asyncio.sleep(0.5)
        during = len(self.wiz.got)
        self.assertGreaterEqual(during, 2)            # refreshed while data is fresh
        await asyncio.sleep(1.0)                      # data now stale
        settled = len(self.wiz.got)
        await asyncio.sleep(1.0)
        self.assertEqual(len(self.wiz.got), settled)  # no more resends once idle
        self.assertLess(settled, 8)

    async def test_timecode_header(self):
        b = await self.start([self.wizbulb(1)])
        pkt = struct.pack(">BBBBIH", 0x51, 0, 1, 1, 0, 3) + b"\0\0\0\0" + bytes([5, 6, 7])
        self.send(b, pkt); await asyncio.sleep(0.3)
        self.assertEqual(self.wiz.got[0]["params"]["g"], 6)


class Config(unittest.TestCase):
    def load(self, bulbs):
        import tempfile
        p = tempfile.mktemp(); open(p, "w").write(json.dumps({"bulbs": bulbs})); return bb.load_config(p)

    def test_overlap_rejected(self):
        with self.assertRaises(ValueError):
            self.load([{"protocol": "wiz", "ip": "1.2.3.4", "channel": 1},
                       {"protocol": "wiz", "ip": "1.2.3.5", "channel": 3}])

    def test_missing_fields_rejected(self):
        for b in ({"protocol": "wiz", "channel": 1}, {"protocol": "matter", "channel": 1},
                  {"protocol": "cync", "channel": 1}, {"protocol": "zigbee", "channel": 1},
                  {"protocol": "wiz", "ip": "1.1.1.1", "channel": 0}):
            with self.assertRaises(ValueError): self.load([b])

    def test_disabled_ignored(self):
        self.assertEqual(self.load([{"protocol": "wiz", "channel": 0, "enabled": False}])["bulbs"], [])



class CyncSocket(unittest.IsolatedAsyncioTestCase):
    async def test_sender_talks_to_worker_socket(self):
        import tempfile, os
        d = tempfile.mkdtemp(); path = d + "/s.sock"; got = []
        async def handle(r, w):
            got.append(json.loads(await r.readline())); w.write(b'{"status":"queued"}\n'); await w.drain(); w.close()
        srv = await asyncio.start_unix_server(handle, path)
        await bb.CyncSender(path)({"cync_id": 42}, (1, 2, 3))
        srv.close(); await srv.wait_closed()
        self.assertEqual(got, [{"device_id": 42, "rgb": [1, 2, 3]}])

    async def test_rejection_and_missing_socket_raise(self):
        with self.assertRaises(RuntimeError): await bb.CyncSender("")({"cync_id": 1}, (1, 1, 1))
        with self.assertRaises(OSError): await bb.CyncSender("/nonexistent/x.sock")({"cync_id": 1}, (1, 1, 1))




class StraddleAndSync(unittest.IsolatedAsyncioTestCase):
    async def test_bulb_split_across_two_packets_still_updates(self):
        loop = asyncio.get_running_loop(); got = []
        class W(asyncio.DatagramProtocol):
            def datagram_received(s, d, a): got.append(json.loads(d))
        tr, _ = await loop.create_datagram_endpoint(W, local_addr=("127.0.0.1", 0)); wp = tr.get_extra_info("sockname")[1]
        import tempfile, os
        p = tempfile.mktemp(); open(p, "w").write(json.dumps({"listen_port": 0, "refresh_seconds": 0, "bulbs": [
            {"protocol": "wiz", "ip": "127.0.0.1", "port": wp, "channel": 1440}]}))   # indices 1439,1440,1441
        b = bb.Bridge(bb.load_config(p), log=lambda m: None); os.unlink(p); await b.start()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(ddp(0, [0] * 1439 + [200]), ("127.0.0.1", b.port))        # R in the first packet
        s.sendto(ddp(1440, [100, 50]), ("127.0.0.1", b.port))              # G,B in the second
        await asyncio.sleep(0.5); await b.stop(); tr.close(); s.close()
        self.assertEqual(got[-1]["params"]["r"], 200); self.assertEqual(got[-1]["params"]["g"], 100)
        self.assertEqual(got[-1]["params"]["b"], 50)


class SyncOutput(unittest.TestCase):
    def run_sync(self, bulbs, outputs):
        import tempfile, os
        sys.path.insert(0, __file__.rsplit("/tests", 1)[0] + "/tools")
        import importlib; so = importlib.import_module("sync_output")
        d = tempfile.mkdtemp(); c, o = d + "/c.json", d + "/o.json"
        open(c, "w").write(json.dumps({"bulbs": bulbs})); open(o, "w").write(json.dumps({"channelOutputs": outputs}))
        rc, msg = so.sync(c, o); return rc, msg, json.load(open(o))["channelOutputs"], d

    def test_adds_entry_and_keeps_other_outputs(self):
        other = {"type": "WizMatterBridge", "enabled": 1, "startChannel": 289667, "channelCount": 54}
        rc, msg, outs, d = self.run_sync([{"channel": 300001}, {"channel": 300010}], [other])
        self.assertEqual(rc, 0); self.assertEqual(outs[0], other)
        self.assertEqual(outs[1], {"type": "SmartBulbBridge", "enabled": 1, "startChannel": 300001, "channelCount": 12})
        import os; self.assertTrue(os.path.exists(d + "/o.json.smartbulb.bak"))

    def test_count_rounded_to_whole_triples_and_idempotent(self):
        rc, msg, outs, d = self.run_sync([{"channel": 10}, {"channel": 14}], [])      # span 10..16 = 7 -> 9
        self.assertEqual(outs[0]["channelCount"], 9)
        sys.path.insert(0, __file__.rsplit("/tests", 1)[0] + "/tools"); import sync_output as so
        rc2, msg2 = so.sync(d + "/c.json", d + "/o.json"); self.assertIn("already correct", msg2)

    def test_disabled_bulbs_ignored_and_no_bulbs_removes_entry(self):
        rc, msg, outs, d = self.run_sync([{"channel": 5, "enabled": False}],
                                         [{"type": "SmartBulbBridge", "enabled": 1, "startChannel": 5, "channelCount": 3}])
        self.assertEqual(outs, [])

    def test_too_wide_refused_without_changing_file(self):
        rc, msg, outs, d = self.run_sync([{"channel": 1}, {"channel": 70000}], [])
        self.assertEqual(rc, 2); self.assertEqual(outs, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
