#!/usr/bin/env python3
"""Explicit, isolated Cync dependency preparation for a fresh FPP installation.

Never invoked by install/upgrade hooks.  Requires an operator to opt in and
run it as the fpp user with Python >=3.13.  Does not touch FPP's system Python,
restart FPP, enable systemd services, open network listeners or store secrets.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

PYCYNC = 'pycync==0.6.1'
AIOHTTP = 'aiohttp==3.14.5'


def check_private_directory(directory: Path) -> None:
    if not directory.is_absolute() or directory.is_symlink():
        raise ValueError('Cync data directory must be an absolute, non-symlink path')
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or
            stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError('Cync data directory must belong to the operator and be 0700')


def check_runtime(directory: Path) -> dict:
    check_private_directory(directory)
    interpreter = directory / 'venv/bin/python3'
    if not interpreter.exists():
        return {'ready': False, 'reason': 'isolated Cync runtime not installed'}
    if not (directory / 'venv/pyvenv.cfg').is_file():
        return {'ready': False, 'reason': 'Python virtual environment metadata missing'}
    if not interpreter.is_file():
        return {'ready': False, 'reason': 'invalid isolated runtime'}
    code = ('import sys, importlib.metadata as m; '
            'assert sys.prefix != sys.base_prefix; '
            'assert sys.version_info >= (3,13); '
            f'assert m.version("pycync") == "{PYCYNC.split("==")[1]}"; '
            f'assert m.version("aiohttp") == "{AIOHTTP.split("==")[1]}"')
    try:
        result = subprocess.run([str(interpreter), '-I', '-B', '-c', code],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {'ready': False, 'reason': 'Cync runtime is unavailable'}
    return {'ready': result.returncode == 0,
            'reason': 'ready' if result.returncode == 0 else 'runtime dependencies or Python version do not match'}


def prepare(directory: Path) -> dict:
    check_private_directory(directory)
    if sys.version_info < (3, 13):
        raise ValueError('Cync provider requires Python 3.13 or newer; select a compatible interpreter')
    if os.geteuid() == 0:
        raise ValueError('Run as fpp, never root')
    venv = directory / 'venv'
    if venv.is_symlink() or venv.exists():
        existing = check_runtime(directory)
        if existing['ready']:
            return existing
        raise ValueError('Existing unverified Cync venv needs operator review; refusing overwrite')
    try:
        subprocess.run([sys.executable, '-I', '-m', 'venv', str(venv)], check=True, timeout=120)
        subprocess.run([str(venv / 'bin/python3'), '-I', '-m', 'pip',
                        '--disable-pip-version-check', 'install', '--only-binary=:all:',
                        PYCYNC, AIOHTTP], check=True, timeout=180)
        result = check_runtime(directory)
        if not result['ready']:
            raise ValueError(result['reason'])
        return result
    except BaseException:
        # This invocation created the directory. Remove only a validated
        # owner-private, not-symlinked directory to avoid partial dependencies.
        if venv.is_dir() and not venv.is_symlink() and venv.stat().st_uid == os.geteuid():
            shutil.rmtree(venv)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description='Optional isolated Cync provider runtime')
    parser.add_argument('--private-dir', type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--check', action='store_true')
    group.add_argument('--prepare-with-network-downloads', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.check:
            print(json.dumps(check_runtime(args.private_dir), sort_keys=True))
            return 0
        print('Preparing a private Python Cync environment (downloads pycync and aiohttp).',
              file=sys.stderr)
        print(json.dumps(prepare(args.private_dir), sort_keys=True))
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f'Cync preparation aborted: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
