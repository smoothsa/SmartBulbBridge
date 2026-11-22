"""Offline tests for the web-driven Cync sign-in helper (stub pycync, no network)."""
import sys; sys.path.insert(0, __file__.rsplit("/tests", 1)[0] + "/bridge"); sys.path.insert(0, __file__.rsplit("/tests", 1)[0] + "/tests")
import asyncio, json, os, tempfile, unittest
from pathlib import Path
import test_cync_support as base      # reuses its stub pycync installer
import cync_web


class WebSignInTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.log = base._Log()
        self.old = base._install_stubs(self.log)
        self.d = Path(tempfile.mkdtemp()); os.chmod(self.d, 0o700)

    def tearDown(self):
        for k, v in self.old.items():
            if v is None: sys.modules.pop(k, None)
            else: sys.modules[k] = v

    def _request(self):
        p = self.d / 'login-request.json'
        p.write_text(json.dumps({'email': 'a@example.com', 'password': 'secret-pw'})); os.chmod(p, 0o600)

    def _status(self):
        return json.loads((self.d / 'onboard-status.json').read_text())

    async def test_no_code_needed(self):
        self._request()
        self.assertEqual(await cync_web.run(self.d, 2, .05), 0)
        st = self._status()
        self.assertEqual(st['state'], 'done'); self.assertEqual(st['devices'][0]['id'], 1)
        self.assertTrue((self.d / 'auth.json').exists()); self.assertTrue((self.d / 'cync-session.json').exists())
        self.assertFalse((self.d / 'login-request.json').exists(), 'password file must be removed')
        self.assertNotIn('secret-pw', (self.d / 'onboard-status.json').read_text())

    async def test_code_flow(self):
        self.log.stub_needs_2fa = True
        self._request()
        task = asyncio.create_task(cync_web.run(self.d, 5, .05))
        for _ in range(100):
            await asyncio.sleep(.05)
            if (self.d / 'onboard-status.json').exists() and self._status()['state'] == 'need_code':
                break
        self.assertEqual(self._status()['state'], 'need_code')
        (self.d / 'code-request.txt').write_text('123456')
        self.assertEqual(await task, 0)
        self.assertEqual(self._status()['state'], 'done')
        self.assertIn(('login', '123456'), self.log)

    async def test_code_timeout(self):
        self.log.stub_needs_2fa = True
        self._request()
        self.assertEqual(await cync_web.run(self.d, .3, .05), 1)
        self.assertEqual(self._status()['state'], 'error')
        self.assertFalse((self.d / 'auth.json').exists())

    async def test_missing_request(self):
        self.assertEqual(await cync_web.run(self.d, .1, .05), 1)
        self.assertEqual(self._status()['state'], 'error')


if __name__ == '__main__':
    unittest.main()
