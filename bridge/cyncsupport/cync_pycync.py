"""Optional PyCync provider adapter; no I/O or imports of pycync until connect().

The upstream library is reverse engineered and may change. This backend is
explicitly opt-in; device discovery/control requires account credentials and
can be disrupted when the official Cync app opens the same account session.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import Any


class CyncConfigurationError(ValueError):
    pass


class CyncTwoFactorRequired(RuntimeError):
    pass


def _validate_private_parent(path: Path) -> None:
    if not path.is_absolute() or len(os.fsencode(path)) > 300:
        raise CyncConfigurationError('Credentials file must have an absolute path')
    parent = path.parent
    try:
        s = parent.lstat()
    except OSError as exc:
        raise CyncConfigurationError('Private credentials directory does not exist') from exc
    if (not stat.S_ISDIR(s.st_mode) or s.st_uid != os.geteuid() or
            stat.S_IMODE(s.st_mode) & 0o077):
        raise CyncConfigurationError('Credentials directory must be owned by worker user and mode 0700')


def load_credentials(path: Path) -> dict[str, str]:
    """Refuse symlinks, unexpected owner, world-readable credentials, bad JSON."""
    path = Path(path)
    _validate_private_parent(path)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CyncConfigurationError('Credentials file cannot be opened safely') from exc
    try:
        s = os.fstat(fd)
        if (not stat.S_ISREG(s.st_mode) or s.st_uid != os.geteuid() or
                stat.S_IMODE(s.st_mode) & 0o077 or s.st_size > 8192):
            raise CyncConfigurationError('Credentials must be private regular file owned by worker (0600)')
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            fd = -1
            obj = json.load(stream)
    except (ValueError, UnicodeError) as exc:
        raise CyncConfigurationError('Credentials JSON is invalid') from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if (not isinstance(obj, dict) or set(obj) != {'username', 'password'} or
            not isinstance(obj['username'], str) or not 1 <= len(obj['username']) <= 254 or
            not isinstance(obj['password'], str) or not 1 <= len(obj['password']) <= 1024):
        raise CyncConfigurationError('Credentials JSON needs username and password')
    return obj


def save_credentials(path: Path, username: str, password: str, overwrite: bool = False) -> None:
    """Atomic 0600 create. Refuses symlinks; refuses to replace an existing file
    unless overwrite=True (used only by interactive re-enrollment)."""
    path = Path(path)
    _validate_private_parent(path)
    if not isinstance(username, str) or not 1 <= len(username) <= 254:
        raise CyncConfigurationError('Invalid account username')
    if not isinstance(password, str) or not 1 <= len(password) <= 1024:
        raise CyncConfigurationError('Invalid password')
    if path.is_symlink() or (path.exists() and not overwrite):
        raise CyncConfigurationError('Credentials already exist; refusing overwrite')
    fd, tmp = tempfile.mkstemp(prefix='.cync-credentials-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(json.dumps({'username': username, 'password': password}, separators=(',', ':')))
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        # Exclusive create, rather than os.replace(), prevents races with other
        # instances and keeps an existing secret from being overwritten.
        if overwrite:
            os.replace(tmp, path)
        else:
            os.link(tmp, path, follow_symlinks=False)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


SESSION_KEYS = ('access_token', 'refresh_token', 'authorize', 'user_id', 'expires_at')


def load_session(path: Path) -> dict | None:
    """Return the saved login session, or None if absent/unusable.

    A bad or unsafe file is treated as "no session" so the worker falls back to
    a password login (which, at worst, asks for re-enrollment).
    """
    path = Path(path)
    try:
        _validate_private_parent(path)
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0))
    except (OSError, CyncConfigurationError):
        return None
    try:
        s = os.fstat(fd)
        if (not stat.S_ISREG(s.st_mode) or s.st_uid != os.geteuid() or
                stat.S_IMODE(s.st_mode) & 0o077 or s.st_size > 16384):
            return None
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            fd = -1
            obj = json.load(stream)
    except (OSError, ValueError, UnicodeError):
        return None
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(obj, dict) or any(k not in obj for k in SESSION_KEYS):
        return None
    if (not isinstance(obj['access_token'], str) or not isinstance(obj['refresh_token'], str) or
            not obj['access_token'] or not obj['refresh_token'] or
            not isinstance(obj['expires_at'], (int, float))):
        return None
    return {k: obj[k] for k in SESSION_KEYS}


def save_session(path: Path, data: dict) -> None:
    """Atomically replace the session file (0600). Tokens rotate, so this runs often."""
    path = Path(path)
    _validate_private_parent(path)
    if path.is_symlink():
        raise CyncConfigurationError('Session file must not be a symlink')
    fd, tmp = tempfile.mkstemp(prefix='.cync-session-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(json.dumps({k: data[k] for k in SESSION_KEYS}, separators=(',', ':')))
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _session_from_user(user) -> dict | None:
    try:
        return {'access_token': user.access_token, 'refresh_token': user.refresh_token,
                'authorize': user.authorize, 'user_id': user.user_id,
                'expires_at': float(user.expires_at)}
    except (AttributeError, TypeError, ValueError):
        return None


class PyCyncBackend:
    def __init__(self, username: str, password: str, session_path: Path | None = None,
                 use_saved_session: bool = True):
        if not username or not password:
            raise CyncConfigurationError('Account credentials are required')
        self.username, self.password = username, password
        # Where the login session (tokens) is kept so a restart does not need a
        # new emailed code. Tokens rotate, so they are re-saved when they change.
        self.session_path = Path(session_path) if session_path else None
        self.use_saved_session = use_saved_session
        self._saved = None
        self._session = None
        self._auth = None
        self._api = None
        self._devices: dict[int, Any] = {}
        self._on: set[int] = set()   # bulbs we already turned on; skips a cloud call per color change

    async def connect(self, code: str | None = None) -> list[dict[str, object]]:
        # PyCync binds the emailed 2FA challenge to the Auth/session instance.
        # Do not close that instance and trigger a new login when the operator
        # enters the emailed code: the original challenge may become invalid.
        if self._api is not None:
            raise RuntimeError('Provider already connected')
        if code is not None and self._auth is None:
            raise CyncConfigurationError('Two-factor code without a pending login')
        try:
            from aiohttp import ClientSession
            from pycync import Auth, Cync
            from pycync.devices.capabilities import CyncCapability
            from pycync.exceptions import TwoFactorRequiredError
        except ImportError as exc:
            raise RuntimeError('Optional pycync/aiohttp is not installed in the selected Python runtime') from exc
        restored = False
        if self._session is None:
            self._session = ClientSession()
            try:
                saved = (load_session(self.session_path)
                         if (self.session_path and self.use_saved_session and code is None) else None)
                user = None
                if saved:
                    from pycync import User
                    user = User(saved['access_token'], saved['refresh_token'], saved['authorize'],
                                saved['user_id'],
                                expire_in=max(1, int(saved['expires_at'] - time.time())))
                    restored = True
                self._auth = Auth(self._session, user=user, username=self.username,
                                  password=self.password)
            except Exception:
                await self.close()
                raise
        try:
            try:
                if not restored:
                    await self._auth.login(code)
            except TwoFactorRequiredError as exc:
                # Keep auth and session alive until connect(code) or close().
                raise CyncTwoFactorRequired('Cync emailed a two-factor code; operator enrollment is required') from exc
            try:
                self._api = await Cync.create(self._auth)
            except Exception:
                if not restored:
                    raise
                # The saved session was rejected (expired or revoked). Fall back
                # to a normal login once; if Cync wants a new emailed code the
                # caller reports "re-enrollment needed" instead of looping.
                await self.close()
                self.use_saved_session = False
                return await self.connect(code)
            self._persist()
            devices: dict[int, Any] = {}
            discovery: list[dict[str, object]] = []
            for d in self._api.get_devices():
                ident = getattr(d, 'device_id', None)
                # A white-only CyncLight ALSO has a callable set_rgb method,
                # but raises UnsupportedCapabilityError when it is used.
                # Filter by the actual upstream capability map, not method
                # existence, and require ON_OFF for a black=off command.
                supports = getattr(d, 'supports_capability', None)
                if (type(ident) is not int or ident <= 0 or
                        not callable(getattr(d, 'set_rgb', None)) or
                        not callable(getattr(d, 'turn_off', None)) or
                        not callable(supports) or
                        not supports(CyncCapability.RGB_COLOR) or
                        not supports(CyncCapability.ON_OFF)):
                    continue
                if ident in devices:
                    raise RuntimeError('Provider returned duplicate device IDs')
                devices[ident] = d
                discovery.append({'id': ident, 'name': str(getattr(d, 'name', 'Cync Light'))[:128]})
            self._devices = devices
            return discovery
        except CyncTwoFactorRequired:
            # The pending emailed code must be used on this very auth session.
            raise
        except Exception:
            await self.close()
            raise

    def _persist(self) -> None:
        """Save the current tokens if they changed (refresh rotates them)."""
        if not self.session_path or self._auth is None:
            return
        data = _session_from_user(getattr(self._auth, 'user', None))
        if data is None:
            return
        key = (data['access_token'], data['refresh_token'])
        if key == self._saved:
            return
        try:
            save_session(self.session_path, data)
            self._saved = key
        except (OSError, CyncConfigurationError):
            pass  # keep running; worst case is one re-enrollment later

    async def apply_rgb(self, device_id: int, rgb: tuple[int, int, int]):
        device = self._devices.get(device_id)
        if device is None:
            raise ValueError('Cync device not present in authenticated discovery')
        if rgb == (0, 0, 0):
            await device.turn_off()
            self._on.discard(device_id)
        else:
            if device_id not in self._on:
                await device.turn_on()
                self._on.add(device_id)
            await device.set_rgb(rgb)
        self._persist()
        # This is provider-command acceptance, not physical confirmation.
        return 'provider_command_accepted'

    async def close(self):
        # Always close the aiohttp session even if shutdown of a broken
        # provider connection raises. Teardown is idempotent.
        api, self._api = self._api, None
        self._persist()
        self._auth = None
        session, self._session = self._session, None
        self._devices.clear()
        self._on.clear()
        try:
            if api is not None:
                await api.shut_down()
        finally:
            if session is not None:
                await session.close()
