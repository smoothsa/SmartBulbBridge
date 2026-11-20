"""Private, bounded Cync command worker for the FPP Smart Bulb Bridge.

This service is a separately activated component; importing it has no effect.
Only explicitly configured Cync IDs are accepted, commands are coalesced to
latest values, and the Unix socket lives in a private operator-owned directory.
A reply means queued, NOT that the physical bulb has changed.
"""
from __future__ import annotations

import asyncio
import errno
import fcntl
from collections import Counter, deque
import json
import os
from pathlib import Path
import socket
import stat
from typing import Awaitable, Callable

RGB = tuple[int, int, int]
Apply = Callable[[int, RGB], Awaitable[object]]


class CyncWorkerError(RuntimeError):
    pass


class CyncWorker:
    def __init__(self, path: str | Path, allowed_ids: set[int], apply: Apply, *,
                 global_interval: float = 0.25, timeout: float = 4.0,
                 retry_base: float = 0.5, retry_cap: float = 30.0):
        self.path = Path(path)
        if (not self.path.is_absolute() or len(os.fsencode(self.path)) > 100 or
                self.path.name in ('.', '..')):
            raise ValueError('Invalid private Cync Unix socket path')
        if (not isinstance(allowed_ids, set) or
                any(type(v) is not int or not 1 <= v <= 2**64 - 1 for v in allowed_ids)):
            raise ValueError('Cync device IDs must be a set of positive integers')
        if not callable(apply):
            raise ValueError('Cync provider callback is missing')
        if (not 0 <= global_interval <= 60 or not 0.1 <= timeout <= 120 or
                not 0.01 <= retry_base <= retry_cap <= 120):
            raise ValueError('Unsafe pacing or retry policy')
        self.allowed = frozenset(allowed_ids)
        self.apply = apply
        self.interval = global_interval
        self.timeout = timeout
        self.retry_base = retry_base
        self.retry_cap = retry_cap
        self.pending: dict[int, RGB] = {}
        self.applied: dict[int, RGB] = {}
        self.inflight: dict[int, RGB] = {}
        self._queue = deque()
        self._queued: set[int] = set()
        self._failure: dict[int, int] = {}
        self._next_due: dict[int, float] = {}
        self._wake = asyncio.Event()
        self._server: asyncio.AbstractServer | None = None
        self._task: asyncio.Task | None = None
        self._inode: tuple[int, int] | None = None
        self._last_send = -1e12
        self._lock_fd: int | None = None
        self.stats = Counter()

    def enqueue(self, device_id: int, rgb: RGB) -> str:
        if type(device_id) is not int or device_id not in self.allowed:
            raise ValueError('Cync device is not configured or enabled')
        if (not isinstance(rgb, (tuple, list)) or len(rgb) != 3 or
                any(type(c) is not int or not 0 <= c <= 255 for c in rgb)):
            raise ValueError('Invalid Cync RGB color')
        rgb = tuple(rgb)
        if (self.pending.get(device_id) == rgb or
                device_id not in self.pending and device_id not in self.inflight
                and self.applied.get(device_id) == rgb):
            self.stats['duplicates'] += 1
            return 'duplicate'
        if device_id in self.pending:
            self.stats['coalesced'] += 1
        self.pending[device_id] = rgb
        if device_id not in self._queued:
            self._queue.append(device_id)
            self._queued.add(device_id)
        self._wake.set()
        self.stats['queued'] += 1
        return 'queued'

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter):
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=2.0)
            if not raw or len(raw) > 512 or not raw.endswith(b'\n'):
                raise ValueError('Bad request framing')
            msg = json.loads(raw)
            if not isinstance(msg, dict) or set(msg) != {'device_id', 'rgb'}:
                raise ValueError('Invalid command')
            result = self.enqueue(msg['device_id'], msg['rgb'])
            reply = {'status': result}
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception:
            self.stats['rejected'] += 1
            reply = {'status': 'rejected'}
        try:
            writer.write((json.dumps(reply, separators=(',', ':')) + '\n').encode('ascii'))
            await asyncio.wait_for(writer.drain(), timeout=1.0)
        except (ConnectionError, OSError, TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except (ConnectionError, OSError, TimeoutError):
                pass

    def _check_socket_directory(self):
        parent = self.path.parent
        st = parent.lstat()
        if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or
                (stat.S_IMODE(st.st_mode) & 0o077)):
            raise CyncWorkerError('Socket directory must be private (0700) and owned by service user')

    def _take_lock(self):
        """Single owner across restarts; never steal a living worker's socket."""
        lock = self.path.with_name(self.path.name + '.lock')
        if lock.is_symlink():
            raise CyncWorkerError('Unsafe Cync socket lock symlink')
        fd = os.open(lock, os.O_RDWR | os.O_CREAT |
                     getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0), 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
                    stat.S_IMODE(info.st_mode) != 0o600):
                raise CyncWorkerError('Unsafe Cync socket lock permissions')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CyncWorkerError('Another Cync worker owns this socket') from exc
        except BaseException:
            os.close(fd)
            raise
        self._lock_fd = fd

    def _recover_or_refuse_socket(self):
        """Reclaim only our own unresponsive stale socket under the owner lock."""
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
            raise CyncWorkerError('Socket path contains an unexpected object')
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            status = peer.connect_ex(str(self.path))
        finally:
            peer.close()
        if status != errno.ECONNREFUSED:
            raise CyncWorkerError('Cync socket is active or its state is ambiguous')
        # The directory is private and the process holds the singleton lock.
        # Still compare inode before removing the stale socket.
        check = self.path.lstat()
        if (not stat.S_ISSOCK(check.st_mode) or
                (check.st_dev, check.st_ino) != (info.st_dev, info.st_ino)):
            raise CyncWorkerError('Socket was changed during recovery')
        self.path.unlink()
        self.stats['stale_socket_recovered'] += 1

    def _release_lock(self):
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    async def start(self):
        if self._server is not None:
            raise CyncWorkerError('Worker already started')
        self._check_socket_directory()
        self._take_lock()
        try:
            self._recover_or_refuse_socket()
        except BaseException:
            self._release_lock()
            raise
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            old_umask = os.umask(0o077)
            try:
                sock.bind(str(self.path))
            finally:
                os.umask(old_umask)
            self.path.chmod(0o600)
            st = self.path.lstat()
            self._inode = (st.st_dev, st.st_ino)
            sock.listen(64)
            sock.setblocking(False)
            self._server = await asyncio.start_unix_server(self._handle, sock=sock, limit=1024)
            self._task = asyncio.create_task(self._drain(), name='cync-command-drain')
        except BaseException:
            sock.close()
            self._unlink_owned()
            self._release_lock()
            raise

    async def _drain(self):
        loop = asyncio.get_running_loop()
        while True:
            if not self._queue:
                self._wake.clear()
                await self._wake.wait()
                continue
            earliest = None
            device_id = None
            # Rotate past failed bulbs awaiting backoff so they do not block
            # healthy devices. One queue entry per known device, O(N) memory.
            for _ in range(len(self._queue)):
                candidate = self._queue.popleft()
                # A newer RGB value may have been absorbed before this stale
                # queue entry was consumed. Never pop a missing pending value.
                if candidate not in self.pending:
                    self._queued.discard(candidate)
                    continue
                due = self._next_due.get(candidate, -1e12)
                if due <= loop.time():
                    device_id = candidate
                    self._queued.remove(candidate)
                    break
                self._queue.append(candidate)
                earliest = due if earliest is None else min(earliest, due)
            if device_id is None:
                if earliest is None:
                    # Stale queue entries have been discarded; wait for work.
                    continue
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(),
                                           timeout=max(0.001, earliest - loop.time()))
                except asyncio.TimeoutError:
                    pass
                continue
            color = self.pending.pop(device_id)
            gap = self.interval - (loop.time() - self._last_send)
            if gap > 0:
                await asyncio.sleep(gap)
            # A fast show may update a device while the global provider pace
            # timer is running. Use its latest RGB rather than sending stale
            # colors and creating avoidable cloud/network traffic.
            if device_id in self.pending:
                color = self.pending.pop(device_id)
                self.stats['coalesced_before_send'] += 1
            self._last_send = loop.time()
            self.inflight[device_id] = color
            self.stats['attempts'] += 1
            try:
                await asyncio.wait_for(self.apply(device_id, color), timeout=self.timeout)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.stats['provider_errors'] += 1
                failures = self._failure.get(device_id, 0) + 1
                self._failure[device_id] = failures
                self._next_due[device_id] = loop.time() + min(
                    self.retry_cap, self.retry_base * 2 ** min(failures-1, 12))
                # A newer pending color always wins over the failed one.
                if device_id not in self.pending:
                    self.pending[device_id] = color
                if device_id not in self._queued:
                    self._queue.append(device_id)
                    self._queued.add(device_id)
            else:
                self.applied[device_id] = color
                self._failure.pop(device_id, None)
                self._next_due.pop(device_id, None)
                self.stats['provider_accepted'] += 1
            finally:
                self.inflight.pop(device_id, None)
            # Revisions arriving while this send was in flight were separately
            # queued and must not be dropped.

    def replay_applied(self) -> int:
        """Schedule last accepted colors after vendor reconnect, at normal pace.

        Preserve newer queued or in-flight colors. A replay is a refresh, not
        confirmation that an offline bulb physically kept its last state.
        """
        replayed = 0
        for device_id, color in list(self.applied.items()):
            if (device_id not in self.allowed or device_id in self.pending or
                    device_id in self.inflight):
                continue
            # Clearing the cache is essential: otherwise an unchanged color
            # would be suppressed as a duplicate and never get refreshed.
            del self.applied[device_id]
            self.pending[device_id] = color
            if device_id not in self._queued:
                self._queued.add(device_id)
                self._queue.append(device_id)
            replayed += 1
        if replayed:
            self._wake.set()
            self.stats['replay_scheduled'] += replayed
        return replayed

    def status(self):
        return {'configured': len(self.allowed), 'queued_devices': len(self.pending),
                'inflight_devices': len(self.inflight),
                'cync_global_interval_seconds': self.interval,
                # Scheduling arithmetic only; NOT a network latency guarantee.
                'minimum_full_sweep_seconds': round(max(0, len(self.allowed)-1) * self.interval, 3),
                'counters': dict(self.stats)}

    def _unlink_owned(self):
        if self._inode is None:
            return
        try:
            st = self.path.lstat()
            if stat.S_ISSOCK(st.st_mode) and (st.st_dev, st.st_ino) == self._inode:
                self.path.unlink()
        except FileNotFoundError:
            pass
        self._inode = None

    async def close(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self._unlink_owned()
        self._release_lock()
