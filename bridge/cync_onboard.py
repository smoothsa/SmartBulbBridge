#!/usr/bin/env python3
"""Explicit local-console Cync account enrollment; never called on install.

This utility saves username/password to an owner-only JSON file only AFTER
successful remote authorization; it never saves the one-time email 2FA code.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cyncsupport.cync_pycync import (CyncTwoFactorRequired, PyCyncBackend,
                                    save_credentials)


async def enroll(path: Path):
    print('Cync enrollment uses its vendor service and may disconnect the Cync mobile app.')
    username = input('Cync email: ').strip()
    password = getpass.getpass('Cync password (hidden): ')
    backend = PyCyncBackend(username, password)
    try:
        try:
            devices = await backend.connect()
        except CyncTwoFactorRequired:
            code = getpass.getpass('Email two-factor code (hidden): ')
            # Reuse the pending PyCync Auth/session: the emailed code is
            # associated with the original authentication challenge.
            devices = await backend.connect(code)
        save_credentials(path, username, password)
        print('Enrollment succeeded. RGB-capable devices discovered:', len(devices))
        for d in devices:
            print(f'  {d["name"]}: {d["id"]}')
        print('Credentials were saved locally with owner-only permissions.')
    finally:
        await backend.close()


def main(argv=None):
    p = argparse.ArgumentParser(description='Interactive Cync enrollment (opt in)')
    p.add_argument('--auth-file', required=True, type=Path)
    args = p.parse_args(argv)
    asyncio.run(enroll(args.auth_file))


if __name__ == '__main__':
    main()
