#!/usr/bin/env python3
"""Cync sign-in driven by the Smart Bulbs web page (runs inside the private Cync venv).

The page writes the email/password to <dir>/login-request.json (mode 0600), starts this
program in the background, and polls <dir>/onboard-status.json. If Cync emails a code,
the page writes it to <dir>/code-request.txt. On success the account is saved the same
way cync_onboard.py saves it (auth.json + cync-session.json). Secrets are never put in
the status file or the log.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cyncsupport.cync_pycync import CyncTwoFactorRequired, PyCyncBackend, save_credentials


def write_status(d: Path, **fields) -> None:
    fd, tmp = tempfile.mkstemp(prefix='.onboard-', dir=d)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(fields, out)
        os.replace(tmp, d / 'onboard-status.json')
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


async def wait_for_code(d: Path, timeout: float, poll: float):
    path = d / 'code-request.txt'
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            code = path.read_text('utf-8').strip()
            path.unlink()
            if re.fullmatch(r'\d{4,8}', code):
                return code
        await asyncio.sleep(poll)
    return None


async def run(d: Path, code_timeout: float = 600, poll: float = 0.5) -> int:
    request = d / 'login-request.json'
    try:
        data = json.loads(request.read_text('utf-8'))
        email, password = data['email'], data['password']
    except (OSError, ValueError, KeyError, TypeError):
        write_status(d, state='error', message='No sign-in request was found. Please try again.')
        return 1
    finally:
        try:
            request.unlink()          # the password does not stay on disk
        except OSError:
            pass
    write_status(d, state='working', message='Signing in to Cync...')
    backend = PyCyncBackend(email, password, session_path=d / 'cync-session.json',
                            use_saved_session=False)
    try:
        try:
            devices = await backend.connect()
        except CyncTwoFactorRequired:
            write_status(d, state='need_code',
                         message='Cync emailed you a verification code. Enter it below.')
            code = await wait_for_code(d, code_timeout, poll)
            if code is None:
                write_status(d, state='error', message='No code was entered in time. Please start again.')
                return 1
            write_status(d, state='working', message='Checking the code...')
            devices = await backend.connect(code)
        save_credentials(d / 'auth.json', email, password, overwrite=True)
        write_status(d, state='done', devices=devices,
                     message=f'Connected. Found {len(devices)} color-capable bulb(s).')
        return 0
    except Exception as exc:     # library errors may contain private data: show the type only
        write_status(d, state='error',
                     message=f'Sign-in failed ({type(exc).__name__}). Check your email, password and code, then try again.')
        return 1
    finally:
        await backend.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--dir', required=True, type=Path)
    a = p.parse_args(argv)
    return asyncio.run(run(a.dir))


if __name__ == '__main__':
    sys.exit(main())
