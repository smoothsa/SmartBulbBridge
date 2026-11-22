#!/usr/bin/env python3
"""Matter helper for the Smart Bulbs web page. Talks to a local Matter server WebSocket.

  matter_web.py nodes --dir D   # quick: writes D/matter-nodes.json (server info + paired nodes)
  matter_web.py pair  --dir D   # reads D/matter-request.json {"code": ..., "network_only": true},
                                #   deletes it, pairs one bulb, writes D/matter-status.json

The setup code is never written to the status files or printed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path

DEFAULT_URL = "ws://127.0.0.1:5580/ws"
CODE_RE = re.compile(r"(?:\d{11}|\d{21}|MT:[A-Z0-9.\-]{1,509})")


def clean_code(code: str) -> str:
    code = re.sub(r"[\s\-]", "", str(code)) if not str(code).startswith("MT:") else str(code).strip()
    if CODE_RE.fullmatch(code):
        return code
    raise ValueError("The setup code should be 11 digits (or a Matter QR text starting MT:)")


def write_json(path: Path, obj) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".matter-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(obj, out)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def parse_nodes(raw) -> list[dict]:
    """Best-effort list of {node_id, name, endpoint_id} for nodes that look like color lights."""
    out = []
    for n in raw if isinstance(raw, list) else []:
        if not isinstance(n, dict) or type(n.get("node_id")) is not int:
            continue
        attrs = n.get("attributes") if isinstance(n.get("attributes"), dict) else {}
        endpoint = None
        for key in attrs:
            m = re.fullmatch(r"(\d+)/768/\d+", str(key))      # color control cluster
            if m:
                endpoint = int(m.group(1)); break
        if attrs and endpoint is None:
            continue                                            # has attributes but no color control
        name = attrs.get("0/40/5") or attrs.get("0/40/3") or f"Matter node {n['node_id']}"
        out.append({"node_id": n["node_id"], "name": str(name)[:40], "endpoint_id": endpoint or 1})
    return out


async def request(ws, command: str, args: dict | None, timeout: float):
    mid = uuid.uuid4().hex
    await ws.send(json.dumps({"message_id": mid, "command": command, "args": args or {}}))
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        left = deadline - asyncio.get_running_loop().time()
        if left <= 0:
            raise asyncio.TimeoutError()
        reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=left))
        if isinstance(reply, dict) and reply.get("message_id") == mid:
            return reply


async def nodes(d: Path, url: str = DEFAULT_URL) -> int:
    info = {"ok": False}
    try:
        import websockets
        async with websockets.connect(url, open_timeout=5, max_size=8 * 1024 * 1024) as ws:
            greeting = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            info["server"] = str(greeting.get("sdk_version", "Matter server"))[:80] if isinstance(greeting, dict) else "Matter server"
            info["bluetooth"] = bool(isinstance(greeting, dict) and greeting.get("bluetooth_enabled") is True
                                     and greeting.get("wifi_credentials_set") is True)
            reply = await request(ws, "get_nodes", None, 8)
            info["nodes"] = parse_nodes(reply.get("result"))
            info["ok"] = True
    except ImportError:
        info["error"] = "The Python websockets package is not installed (sudo apt install python3-websockets)."
    except Exception as exc:
        info["error"] = f"No Matter controller answered at {url} ({type(exc).__name__})."
    write_json(d / "matter-nodes.json", info)
    return 0


async def pair(d: Path, url: str = DEFAULT_URL, timeout: float = 360) -> int:
    req = d / "matter-request.json"
    try:
        data = json.loads(req.read_text("utf-8"))
        code, network_only = clean_code(data["code"]), bool(data.get("network_only", True))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        write_json(d / "matter-status.json", {"state": "error",
                   "message": str(exc) if isinstance(exc, ValueError) and "setup code" in str(exc) else "No pairing request was found. Please try again."})
        return 1
    finally:
        try:
            req.unlink()
        except OSError:
            pass
    write_json(d / "matter-status.json", {"state": "working", "message": "Pairing... this can take a minute or two."})
    try:
        import websockets
        async with websockets.connect(url, open_timeout=10, max_size=8 * 1024 * 1024) as ws:
            await asyncio.wait_for(ws.recv(), timeout=10)
            reply = await request(ws, "commission_with_code", {"code": code, "network_only": network_only}, timeout)
            del code
            if "error_code" in reply or "error" in reply:
                reason = reply.get("reason") if reply.get("reason") in {
                    "not-discovered", "passcode-mismatch", "invalid-payload", "attestation-rejected", "timeout"} else None
                hint = {"not-discovered": "The controller could not find the bulb on the network. Is it on the same network, and was the code just generated?",
                        "passcode-mismatch": "The code was not accepted. Codes from Alexa expire after a few minutes; generate a new one.",
                        "invalid-payload": "That is not a valid Matter code."}.get(reason, "Pairing failed.")
                write_json(d / "matter-status.json", {"state": "error", "message": hint})
                return 1
            result = reply.get("result")
            node_id = result.get("node_id") if isinstance(result, dict) else result
            if isinstance(node_id, (int, str)) and str(node_id).isdigit():
                write_json(d / "matter-status.json", {"state": "done", "node_id": int(node_id),
                           "message": f"Paired as Matter node {int(node_id)}."})
                return 0
            write_json(d / "matter-status.json", {"state": "done", "message": "Paired. The bulb is in the list below."})
            return 0
    except Exception as exc:
        write_json(d / "matter-status.json", {"state": "error",
                   "message": f"Pairing failed ({type(exc).__name__}). Is the Matter controller running?"})
        return 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["nodes", "pair"])
    p.add_argument("--dir", required=True, type=Path)
    p.add_argument("--url", default=DEFAULT_URL)
    a = p.parse_args(argv)
    return asyncio.run(nodes(a.dir, a.url) if a.mode == "nodes" else pair(a.dir, a.url))


if __name__ == "__main__":
    sys.exit(main())
