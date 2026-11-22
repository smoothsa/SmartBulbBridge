"""Offline tests for the Matter page helper, using a fake websockets module."""
import sys; sys.path.insert(0, __file__.rsplit("/tests", 1)[0] + "/bridge")
import asyncio, json, os, tempfile, types, unittest
from pathlib import Path
import matter_web


class FakeWS:
    def __init__(self, script):
        self.script, self.sent, self.queue = script, [], [json.dumps(script["greeting"])]
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def send(self, raw):
        m = json.loads(raw); self.sent.append(m)
        handler = self.script["replies"].get(m["command"])
        if handler is not None:
            self.queue.append(json.dumps({"message_id": m["message_id"], **handler}))
    async def recv(self):
        while not self.queue: await asyncio.sleep(.01)
        return self.queue.pop(0)


def install(script, fail=False):
    mod = types.ModuleType("websockets")
    holder = {}
    def connect(url, **kw):
        if fail: raise ConnectionRefusedError()
        holder["ws"] = FakeWS(script); return holder["ws"]
    mod.connect = connect
    old = sys.modules.get("websockets"); sys.modules["websockets"] = mod
    return holder, old


class MatterWebTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp()); os.chmod(self.d, 0o700)
    def tearDown(self):
        sys.modules.pop("websockets", None)
    def status(self, name="matter-status.json"):
        return json.loads((self.d / name).read_text())

    def test_clean_code(self):
        self.assertEqual(matter_web.clean_code("123-4567-8901"), "12345678901")
        self.assertEqual(matter_web.clean_code(" 1234 5678 901 "), "12345678901")
        with self.assertRaises(ValueError): matter_web.clean_code("1234")

    async def test_nodes_lists_color_lights_only(self):
        script = {"greeting": {"sdk_version": "matter-server/1.5.5", "bluetooth_enabled": True, "wifi_credentials_set": True},
                  "replies": {"get_nodes": {"result": [
                      {"node_id": 5, "attributes": {"1/768/0": 1, "0/40/3": "A19 RGBTW"}},
                      {"node_id": 6, "attributes": {"2/768/0": 1, "0/40/5": "Porch"}},
                      {"node_id": 7, "attributes": {"1/6/0": True}},            # plug: no color control
                      {"node_id": "x"}]}}}
        install(script)
        await matter_web.nodes(self.d)
        st = self.status("matter-nodes.json")
        self.assertTrue(st["ok"]); self.assertTrue(st["bluetooth"])
        self.assertEqual([(n["node_id"], n["name"], n["endpoint_id"]) for n in st["nodes"]],
                         [(5, "A19 RGBTW", 1), (6, "Porch", 2)])

    async def test_nodes_server_down(self):
        install({"greeting": {}, "replies": {}}, fail=True)
        await matter_web.nodes(self.d)
        st = self.status("matter-nodes.json")
        self.assertFalse(st["ok"]); self.assertIn("No Matter controller", st["error"])

    async def _pair(self, reply, network_only=True, code="123-4567-8901"):
        holder, _ = install({"greeting": {}, "replies": {"commission_with_code": reply}})
        (self.d / "matter-request.json").write_text(json.dumps({"code": code, "network_only": network_only}))
        rc = await matter_web.pair(self.d, timeout=3)
        return rc, holder

    async def test_pair_ok_sends_clean_code_network_only(self):
        rc, holder = await self._pair({"result": {"node_id": 7}})
        self.assertEqual(rc, 0)
        st = self.status(); self.assertEqual((st["state"], st["node_id"]), ("done", 7))
        sent = holder["ws"].sent[0]
        self.assertEqual(sent["args"], {"code": "12345678901", "network_only": True})
        self.assertNotIn("12345678901", json.dumps(st))
        self.assertFalse((self.d / "matter-request.json").exists())

    async def test_pair_bluetooth_flag(self):
        rc, holder = await self._pair({"result": {"node_id": 8}}, network_only=False)
        self.assertEqual(holder["ws"].sent[0]["args"]["network_only"], False)

    async def test_pair_error_gives_hint(self):
        rc, _ = await self._pair({"error_code": 1, "reason": "not-discovered"})
        self.assertEqual(rc, 1); self.assertIn("could not find the bulb", self.status()["message"])

    async def test_pair_bad_code(self):
        install({"greeting": {}, "replies": {}})
        (self.d / "matter-request.json").write_text(json.dumps({"code": "12"}))
        self.assertEqual(await matter_web.pair(self.d), 1)
        self.assertIn("11 digits", self.status()["message"])


if __name__ == "__main__":
    unittest.main()
