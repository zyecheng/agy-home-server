#!/usr/bin/env python3
"""Antigravity PreInvocation / Stop hook bridge notifier.

Reads hook JSON payload from stdin, forwards it to the local agy-bridge IPC
server on 127.0.0.1, and ALWAYS prints a valid non-blocking hook response so
`agy` is never slowed down or broken if the bridge daemon is offline.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else "Stop"
    raw = sys.stdin.read().strip()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {}

    origin = os.environ.get("AGY_BRIDGE_ORIGIN", "ssh")
    if origin != "bridge":
        ipc_host = os.environ.get("BRIDGE_IPC_HOST", "127.0.0.1")
        ipc_port = int(os.environ.get("BRIDGE_IPC_PORT", "8765"))
        body = json.dumps(
            {
                "event": event,
                "origin": origin,
                "conversationId": payload.get("conversationId", ""),
                "transcriptPath": payload.get("transcriptPath", ""),
                "fullyIdle": payload.get("fullyIdle", True),
                "lastUserInput": payload.get("lastUserInput", ""),
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"http://{ipc_host}:{ipc_port}/hook",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=3):
                pass
        except Exception:
            pass

    if event == "Stop":
        print(json.dumps({"decision": "allow"}))
    else:
        print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
