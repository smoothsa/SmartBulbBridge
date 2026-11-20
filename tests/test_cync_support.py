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
