#!/usr/bin/env python3
"""Cync worker: runs inside an isolated Python>=3.13 venv (see cync_setup.py).

Serves a private Unix socket; the main bridge daemon sends it {device_id, rgb}.
Exits with status 78 when Cync needs interactive email verification
(run cync_onboard.py), so scripts do not retry and spam login emails.
"""
import argparse, asyncio, json, logging, signal, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cyncsupport.cync_pycync import PyCyncBackend, load_credentials
from cyncsupport.cync_worker import CyncWorker
from cyncsupport.cync_session import RecoveringCyncSender, CyncReauthenticationRequired

LOG = logging.getLogger("smart-bulbs-cync")


def cync_ids(config):
    cfg = json.loads(Path(config).read_text("utf-8"))
    return {b["cync_id"] for b in cfg.get("bulbs", [])
            if b.get("protocol") == "cync" and b.get("enabled", True) and type(b.get("cync_id")) is int}


async def run(a):
    creds = load_credentials(a.auth_file)
    allowed = cync_ids(a.config)
    if not allowed:
        raise RuntimeError("no enabled Cync bulbs configured")
    provider = RecoveringCyncSender(lambda: PyCyncBackend(creds["username"], creds["password"],
                                          session_path=a.auth_file.parent / "cync-session.json"), allowed)
    try:
        await provider.start()
        worker = CyncWorker(a.socket, allowed, provider.apply_rgb, global_interval=0.25)
        provider.set_reconnect_callback(worker.replay_applied)
        try:
            await worker.start()
            stop = asyncio.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)
            t1 = asyncio.create_task(stop.wait())
            t2 = asyncio.create_task(provider.reauthentication_required.wait())
            try:
                done, _ = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
                if t2 in done and provider.reauthentication_required.is_set():
                    raise CyncReauthenticationRequired("email verification required")
            finally:
                for t in (t1, t2): t.cancel()
                await asyncio.gather(t1, t2, return_exceptions=True)
        finally:
            await worker.close()
    finally:
        await provider.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--auth-file", type=Path, required=True)
    p.add_argument("--socket", type=Path, required=True)
    p.add_argument("--pidfile", type=Path)
    a = p.parse_args()
    if a.pidfile:
        a.pidfile.write_text(str(__import__("os").getpid()))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s cync: %(message)s")
    try:
        asyncio.run(run(a))
    except CyncReauthenticationRequired:
        LOG.error("Cync needs email verification: run cync_onboard.py, then restart")
        return 78
    except Exception as e:           # library errors may contain private data: type only
        LOG.error("Cync worker stopped: %s", type(e).__name__)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
