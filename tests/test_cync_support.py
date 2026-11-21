"""Offline-only Cync session recovery, reconnection and color refresh tests."""
import sys; sys.path.insert(0, __file__.rsplit("/tests", 1)[0] + "/bridge")
import asyncio
from collections import Counter
import os
from pathlib import Path
import tempfile
import unittest

from cyncsupport.cync_pycync import CyncTwoFactorRequired
from cyncsupport.cync_session import (RecoveringCyncSender, CyncSessionUnavailable,
                                      CyncReauthenticationRequired,
                                      CyncDeviceUnavailable)
from cyncsupport.cync_worker import CyncWorker


class FakeBackend:
    def __init__(self, behavior='okay', accepted=None, devices=(1, 2)):
        self.behavior = behavior
        self.accepted = accepted if accepted is not None else []
        self.devices = devices
        self.closed = False
        self.connects = 0

    async def connect(self):
        self.connects += 1
        if self.behavior == '2fa':
            raise CyncTwoFactorRequired('email code')
        if self.behavior == 'login_bad':
            raise ConnectionError('offline')
        return [{'id': i, 'name': f'Cync {i}'} for i in self.devices]

    async def apply_rgb(self, i, rgb):
        if self.behavior == 'fail_command':
            raise ConnectionError('broken vendor session')
        self.accepted.append((i, rgb))
        return 'accepted'

    async def close(self):
        self.closed = True


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_command_reconnects_then_sends_latest(self):
        instances = []
        accepted = []
        def factory():
            b = FakeBackend('fail_command' if not instances else 'okay', accepted)
            instances.append(b)
            return b
        sender = RecoveringCyncSender(factory, {1}, retry_base=.1, retry_cap=.1)
        await sender.start()
        with self.assertRaises(CyncSessionUnavailable):
            await sender.apply_rgb(1, (5, 6, 7))
        self.assertTrue(instances[0].closed)
        with self.assertRaises(CyncSessionUnavailable):
            await sender.apply_rgb(1, (8, 9, 10))
        self.assertEqual(len(instances), 1, 'must not storm logins before cooldown')
        await asyncio.sleep(.12)
        self.assertEqual(await sender.apply_rgb(1, (8, 9, 10)), 'accepted')
        self.assertEqual(accepted, [(1, (8, 9, 10))])
        self.assertEqual(sender.counters['reconnected'], 1)
        await sender.close()
        self.assertTrue(instances[1].closed)

    async def test_reconnect_replays_only_cached_nonpending_colors(self):
        tmp = tempfile.TemporaryDirectory(prefix='wmb-v024-')
        self.addCleanup(tmp.cleanup)
        private = Path(tmp.name)/'private'
        private.mkdir(mode=0o700)
        accepted = []
        instances = []
        def factory():
            b = FakeBackend('okay', accepted)
            instances.append(b)
            return b
        sender = RecoveringCyncSender(factory, {1, 2}, retry_base=.1, retry_cap=.1)
        await sender.start()
        worker = CyncWorker(private/'cync.sock', {1, 2}, sender.apply_rgb,
                            global_interval=0.0)
        sender.set_reconnect_callback(worker.replay_applied)
        await worker.start()
        try:
            worker.enqueue(1, (10, 20, 30))
            worker.enqueue(2, (20, 30, 40))
            for _ in range(100):
                if len(worker.applied) == 2:
                    break
                await asyncio.sleep(.005)
            self.assertEqual(len(worker.applied), 2)
            # Simulate loss of vendor connection; next command triggers recovery.
            instances[0].behavior = 'fail_command'
            worker.enqueue(1, (50, 60, 70))
            for _ in range(100):
                if instances[0].closed:
                    break
                await asyncio.sleep(.005)
            self.assertTrue(instances[0].closed)
            await asyncio.sleep(.25)
            for _ in range(200):
                if worker.applied.get(1) == (50,60,70) and (
                    accepted.count((2, (20,30,40))) >= 2):
                    break
                await asyncio.sleep(.005)
            self.assertEqual(worker.applied[1], (50,60,70))
            self.assertGreaterEqual(accepted.count((2,(20,30,40))), 2)
            self.assertEqual(sender.counters['reconnected'], 1)
            self.assertGreaterEqual(worker.stats['replay_scheduled'], 1)
        finally:
            await worker.close()
            await sender.close()

    async def test_email_challenge_latches_no_flood(self):
        instances = []
        def factory():
            b = FakeBackend('2fa')
            instances.append(b)
            return b
        sender = RecoveringCyncSender(factory, {1})
        with self.assertRaises(CyncReauthenticationRequired):
            await sender.start()
        with self.assertRaises(CyncReauthenticationRequired):
            await sender.apply_rgb(1, (7, 8, 9))
        self.assertEqual(len(instances), 1)
        self.assertTrue(instances[0].closed)
        await sender.close()

    async def test_reauth_after_midrun_failure_latches_once(self):
        modes = iter(['okay', '2fa'])
        instances=[]
        def factory():
            b = FakeBackend(next(modes, '2fa'))
            instances.append(b)
            return b
        sender = RecoveringCyncSender(factory, {1}, retry_base=.1, retry_cap=.1)
        await sender.start()
        instances[0].behavior='fail_command'
        with self.assertRaises(CyncSessionUnavailable):
            await sender.apply_rgb(1, (7,8,9))
        await asyncio.sleep(.11)
        with self.assertRaises(CyncReauthenticationRequired):
            await sender.apply_rgb(1, (7,8,9))
        for _ in range(5):
            with self.assertRaises(CyncReauthenticationRequired):
                await sender.apply_rgb(1, (7,8,9))
        self.assertEqual(len(instances),2)
        await sender.close()

    async def test_missing_configured_device_not_accepted(self):
        instances=[]
        def factory():
            b=FakeBackend(devices=(3,))
            instances.append(b)
            return b
        sender=RecoveringCyncSender(factory, {1,2},retry_base=.1)
        with self.assertRaises(CyncSessionUnavailable):
            await sender.start()
        self.assertTrue(instances[0].closed)
        self.assertEqual(sender.counters['connection_errors'], 1)
        await sender.close()

    async def test_wrong_device_rejected_without_new_login(self):
        instances=[]
        def factory():
            b=FakeBackend()
            instances.append(b)
            return b
        sender=RecoveringCyncSender(factory, {1})
        await sender.start()
        with self.assertRaises(ValueError):
            await sender.apply_rgb(999,(0,0,0))
        self.assertEqual(len(instances),1)
        await sender.close()

    async def test_cached_replay_does_not_replace_newer_pending_or_inflight(self):
        tmp=tempfile.TemporaryDirectory(prefix='wmb-v024-')
        self.addCleanup(tmp.cleanup)
        private=Path(tmp.name)/'private'; private.mkdir(mode=0o700)
        async def apply(*args): pass
        worker=CyncWorker(private/'cync.sock', {1,2,3},apply)
        worker.applied={1:(1,1,1),2:(2,2,2),3:(3,3,3)}
        worker.pending={2:(9,9,9)}
        worker._queue.append(2)
        worker._queued.add(2)
        worker.inflight={3:(7,7,7)}
        self.assertEqual(worker.replay_applied(),1)
        self.assertEqual(worker.pending[1],(1,1,1))
        self.assertEqual(worker.pending[2],(9,9,9))
        self.assertEqual(worker.inflight[3],(7,7,7))
        self.assertEqual(len(worker._queue),2)
        self.assertNotIn(1,worker.applied)



# ---- saved-session tests (stub pycync/aiohttp, no network) ----
import sys, types, json, time, stat as _stat


def _install_stubs(log):
    class TwoFactorRequiredError(Exception): pass
    class User:
        def __init__(self, access_token, refresh_token, authorize, user_id, expire_in=3600):
            self.access_token, self.refresh_token = access_token, refresh_token
            self.authorize, self.user_id = authorize, user_id
            self.expires_at = time.time() + expire_in
    class Auth:
        def __init__(self, session, user=None, username=None, password=None):
            self.user, self.username, self.password = user, username, password
            log.append(('auth', user is not None))
        async def login(self, code=None):
            log.append(('login', code))
            if code is None and log.stub_needs_2fa:
                raise TwoFactorRequiredError()
            self.user = User('A1', 'R1', 'auth', 7, expire_in=7200)
            return self.user
    class Dev:
        device_id = 1; name = 'Porch'
        def supports_capability(self, c): return True
        async def turn_on(self): pass
        async def turn_off(self): pass
        async def set_rgb(self, rgb): pass
    class Api:
        def __init__(self, auth): self.auth = auth
        def get_devices(self): return [Dev()]
        async def shut_down(self): pass
    class Cync:
        @staticmethod
        async def create(auth):
            if log.stub_reject_session and auth.user and auth.user.access_token == 'OLD':
                raise RuntimeError('expired')
            return Api(auth)
    class ClientSession:
        async def close(self): pass
    mods = {
        'aiohttp': types.ModuleType('aiohttp'), 'pycync': types.ModuleType('pycync'),
        'pycync.devices': types.ModuleType('pycync.devices'),
        'pycync.devices.capabilities': types.ModuleType('pycync.devices.capabilities'),
        'pycync.exceptions': types.ModuleType('pycync.exceptions')}
    mods['aiohttp'].ClientSession = ClientSession
    mods['pycync'].Auth, mods['pycync'].Cync, mods['pycync'].User = Auth, Cync, User
    mods['pycync.devices.capabilities'].CyncCapability = types.SimpleNamespace(RGB_COLOR=1, ON_OFF=2)
    mods['pycync.exceptions'].TwoFactorRequiredError = TwoFactorRequiredError
    old = {k: sys.modules.get(k) for k in mods}
    sys.modules.update(mods)
    return old


class _Log(list):
    stub_needs_2fa = False
    stub_reject_session = False


class SavedSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.log = _Log()
        self.old = _install_stubs(self.log)
        self.tmp = tempfile.mkdtemp()
        os.chmod(self.tmp, 0o700)
        self.sp = Path(self.tmp) / 'cync-session.json'

    def tearDown(self):
        for k, v in self.old.items():
            if v is None: sys.modules.pop(k, None)
            else: sys.modules[k] = v

    def _backend(self, **kw):
        from cyncsupport.cync_pycync import PyCyncBackend
        return PyCyncBackend('u@example.com', 'pw', session_path=self.sp, **kw)

    async def test_login_saves_private_session_then_restart_skips_login(self):
        b = self._backend()
        await b.connect()
        self.assertEqual(_stat.S_IMODE(self.sp.stat().st_mode), 0o600)
        await b.close()
        self.log.clear()
        b2 = self._backend()
        devs = await b2.connect()
        self.assertEqual(devs[0]['id'], 1)
        self.assertNotIn(('login', None), self.log, 'restart must not log in again')
        self.assertIn(('auth', True), self.log)
        await b2.close()

    async def test_rotated_tokens_are_saved(self):
        b = self._backend()
        await b.connect()
        b._auth.user.access_token, b._auth.user.refresh_token = 'A2', 'R2'
        await b.apply_rgb(1, (1, 2, 3))
        self.assertEqual(json.loads(self.sp.read_text())['refresh_token'], 'R2')
        await b.close()

    async def test_stale_session_falls_back_to_password_login(self):
        from cyncsupport.cync_pycync import save_session
        save_session(self.sp, {'access_token': 'OLD', 'refresh_token': 'OLDR', 'authorize': 'x',
                               'user_id': 7, 'expires_at': time.time() + 10})
        self.log.stub_reject_session = True
        b = self._backend()
        await b.connect()
        self.assertIn(('login', None), self.log)
        self.assertEqual(json.loads(self.sp.read_text())['access_token'], 'A1')
        await b.close()

    async def test_stale_session_and_2fa_needed_asks_for_enrollment(self):
        from cyncsupport.cync_pycync import save_session
        save_session(self.sp, {'access_token': 'OLD', 'refresh_token': 'OLDR', 'authorize': 'x',
                               'user_id': 7, 'expires_at': time.time() + 10})
        self.log.stub_reject_session = True
        self.log.stub_needs_2fa = True
        with self.assertRaises(CyncTwoFactorRequired):
            await self._backend().connect()

    def test_unsafe_or_corrupt_session_is_ignored(self):
        from cyncsupport.cync_pycync import load_session
        self.assertIsNone(load_session(self.sp))
        self.sp.write_text('{bad'); os.chmod(self.sp, 0o600)
        self.assertIsNone(load_session(self.sp))
        self.sp.write_text(json.dumps({'access_token': 'a', 'refresh_token': 'b', 'authorize': 'c',
                                       'user_id': 1, 'expires_at': 1}))
        os.chmod(self.sp, 0o644)
        self.assertIsNone(load_session(self.sp))


if __name__ == '__main__':
    unittest.main()

class ExitPolicyTests(unittest.TestCase):
    def test_cync_unit_disables_restart_after_email_challenge(self):
        # The FPP installer installs a disabled Cync unit with a controlled
        # no-restart exit status for interactive email-code challenges.
        src = Path(__file__).resolve().parents[1]
        install = (src/'scripts/fpp_install.sh').read_text()
        self.assertIn('RestartPreventExitStatus=78', install)
        entry=(src/'deployment/cync_worker_entry.py').read_text()
        self.assertIn('return 78', entry)

    def test_provider_sets_reauthentication_event(self):
        async def check():
            sender=RecoveringCyncSender(lambda: FakeBackend('2fa'), {1})
            with self.assertRaises(CyncReauthenticationRequired):
                await sender.start()
            self.assertTrue(sender.reauthentication_required.is_set())
            await sender.close()
        asyncio.run(check())


class PartialDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_one_bulb_does_not_disable_rest(self):
        accepted=[]
        instances=[]
        def factory():
            backend=FakeBackend(devices=(1,),accepted=accepted)
            instances.append(backend)
            return backend
        sender=RecoveringCyncSender(factory,{1,2})
        try:
            await sender.start()
            self.assertEqual(sender.counters['missing_at_discovery'],1)
            with self.assertRaises(CyncDeviceUnavailable):
                await sender.apply_rgb(2,(5,6,7))
            self.assertEqual(await sender.apply_rgb(1,(3,4,5)),'accepted')
            self.assertEqual(accepted,[(1,(3,4,5))])
            self.assertEqual(len(instances),1)
            self.assertFalse(instances[0].closed)
        finally:
            await sender.close()

    async def test_one_thousand_configured_bulbs_partial_discovery(self):
        allowed=set(range(1,1001))
        discovered=set(range(1,801))
        accepted=[]
        def factory():
            return FakeBackend(devices=discovered,accepted=accepted)
        sender=RecoveringCyncSender(factory,allowed)
        try:
            await sender.start()
            self.assertEqual(sender.counters['missing_at_discovery'],200)
            await sender.apply_rgb(800,(100,0,55))
            with self.assertRaises(CyncDeviceUnavailable):
                await sender.apply_rgb(999,(9,9,9))
            self.assertEqual(accepted,[(800,(100,0,55))])
        finally:
            await sender.close()

    async def test_missing_backend_value_error_is_device_local(self):
        class FakeAbsent(FakeBackend):
            async def apply_rgb(self, i, rgb):
                if i==2:raise ValueError('Cync ID missing')
                return await super().apply_rgb(i,rgb)
        instances=[]
        def factory():
            obj=FakeAbsent(devices=(1,2));instances.append(obj);return obj
        sender=RecoveringCyncSender(factory,{1,2})
        try:
            await sender.start()
            with self.assertRaises(CyncDeviceUnavailable):
                await sender.apply_rgb(2,(3,4,5))
            await sender.apply_rgb(1,(6,7,8))
            self.assertEqual(len(instances),1)
            self.assertFalse(instances[0].closed)
        finally:
            await sender.close()
