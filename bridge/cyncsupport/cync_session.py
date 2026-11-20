"""Bounded Cync authentication-session recovery for a long-running FPP show.

This provider owns *one* authenticated PyCync backend at a time. Commands are
not blindly resent after errors: CyncWorker retains the last wanted color and
retries at its own device-specific pace. A completed reconnect replays cached
last-accepted states for other configured bulbs, without overwriting fresher
pending commands. A renewed email-code challenge requires operator enrollment.

No network action or login occurs at import or construction time.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from typing import Callable

from .cync_pycync import CyncTwoFactorRequired


class CyncReauthenticationRequired(RuntimeError):
    """Two-factor challenge needs explicit operator action; do not loop logins."""


class CyncSessionUnavailable(RuntimeError):
    """Session temporarily offline. CyncWorker handles paced retry."""


class CyncDeviceUnavailable(CyncSessionUnavailable):
    """One configured device is absent; leave other devices connected."""


class RecoveringCyncSender:
    def __init__(self, factory: Callable, allowed_ids: set[int], *,
                 retry_base: float = 2.0, retry_cap: float = 120.0):
        if not callable(factory):
            raise ValueError('Cync backend factory required')
        if (type(allowed_ids) is not set or
                any(type(i) is not int or not 1 <= i <= 2**64 - 1 for i in allowed_ids)):
            raise ValueError('Invalid configured Cync IDs')
        if (not 0.1 <= retry_base <= retry_cap <= 3600):
            raise ValueError('Unsafe Cync recovery retry settings')
        self._factory = factory
        self.allowed = frozenset(allowed_ids)
        self.retry_base = retry_base
        self.retry_cap = retry_cap
        self._backend = None
        self._present: frozenset[int] = frozenset()
        self._next_attempt = -1e12
        self._failures = 0
        self._connected_once = False
        self._requires_2fa = False
        self.reauthentication_required = asyncio.Event()
        self._closed = False
        self._lock = asyncio.Lock()
        self._replay = None
        self.counters = Counter()

    def set_reconnect_callback(self, callback):
        if callback is not None and not callable(callback):
            raise ValueError('Reconnect callback must be callable')
        self._replay = callback

    def _cooldown(self):
        self._failures = min(self._failures + 1, 20)
        seconds = min(self.retry_cap,
                      self.retry_base * 2 ** min(self._failures - 1, 16))
        self._next_attempt = asyncio.get_running_loop().time() + seconds
        self.counters['backoffs'] += 1

    async def _disconnect(self):
        backend, self._backend = self._backend, None
        self._present = frozenset()
        if backend is not None:
            try:
                await backend.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.counters['close_errors'] += 1

    async def _connect(self):
        if self._closed:
            raise CyncSessionUnavailable('Cync session is closed')
        if self._requires_2fa:
            raise CyncReauthenticationRequired('Cync needs interactive email verification')
        if asyncio.get_running_loop().time() < self._next_attempt:
            raise CyncSessionUnavailable('Cync authentication retry is cooling down')
        candidate = self._factory()
        self.counters['connection_attempts'] += 1
        try:
            devices = await candidate.connect()
            found = {d['id'] for d in devices}
            # A large installation must not take every connected bulb offline
            # because one configured device was unplugged or factory reset.
            # Fail only when none of this account's enabled devices can be
            # controlled. Missing IDs remain queued with device-level backoff.
            if self.allowed and not self.allowed.intersection(found):
                raise CyncSessionUnavailable('No configured Cync RGB devices found')
        except asyncio.CancelledError:
            try:
                await candidate.close()
            finally:
                raise
        except CyncTwoFactorRequired as exc:
            self._requires_2fa = True
            self.reauthentication_required.set()
            self.counters['interactive_auth_required'] += 1
            try:
                await candidate.close()
            except Exception:
                self.counters['close_errors'] += 1
            raise CyncReauthenticationRequired(
                'Cync needs interactive email verification') from exc
        except Exception as exc:
            try:
                await candidate.close()
            except Exception:
                self.counters['close_errors'] += 1
            self._cooldown()
            self.counters['connection_errors'] += 1
            raise CyncSessionUnavailable('Cync session connection failed') from exc
        self._backend = candidate
        self._present = frozenset(self.allowed.intersection(found))
        self.counters['missing_at_discovery'] = len(self.allowed - self._present)
        self._failures = 0
        self._next_attempt = -1e12
        self.counters['connected'] += 1
        if self._connected_once:
            self.counters['reconnected'] += 1
            if self._replay:
                self._replay()
        self._connected_once = True

    async def start(self):
        """Initial login/discovery, before starting the socket listener."""
        async with self._lock:
            if self._backend is not None:
                raise RuntimeError('Cync session already started')
            await self._connect()

    async def apply_rgb(self, device_id: int, rgb: tuple[int, int, int]):
        if device_id not in self.allowed:
            raise ValueError('Cync device is not enabled in the bridge')
        async with self._lock:
            if self._backend is None:
                await self._connect()
            if device_id not in self._present:
                self.counters['missing_device_retries'] += 1
                raise CyncDeviceUnavailable('Configured Cync device missing from discovery')
            try:
                return await self._backend.apply_rgb(device_id, rgb)
            except asyncio.CancelledError:
                raise
            except ValueError as exc:
                # The backend uses ValueError for a missing ID. Do not tear
                # down the entire account session for a per-device condition.
                self.counters['device_errors'] += 1
                raise CyncDeviceUnavailable('Cync device command unavailable') from exc
            except Exception as exc:
                self.counters['command_errors'] += 1
                await self._disconnect()
                self._cooldown()
                # Return an error to CyncWorker, so the latest color stays
                # pending and its device-specific backoff remains in force.
                raise CyncSessionUnavailable('Cync provider command failed') from exc

    async def close(self):
        async with self._lock:
            self._closed = True
            await self._disconnect()
