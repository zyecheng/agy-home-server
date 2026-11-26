#!/usr/bin/env python3
# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Antigravity PreInvocation / Stop hook bridge notifier.

Reads hook JSON payload from stdin, forwards it to the authenticated local
agy-bridge IPC server on 127.0.0.1, and ALWAYS prints a valid non-blocking hook
response so `agy` is never slowed down or broken if the bridge daemon is offline.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _notify_bridge(event: str) -> None:
    origin = os.environ.get("AGY_BRIDGE_ORIGIN", "ssh")
    if origin == "bridge":
        return

    raw = sys.stdin.read().strip()
    payload = json.loads(raw) if raw else {}
    if not isinstance(payload, dict):
        return

    from server.config import load_ipc_client_settings

    url_host, ipc_port, token_path, _approval_timeout = load_ipc_client_settings(REPO_ROOT)
    if not token_path.exists():
        return
    ipc_token = token_path.read_text(encoding="utf-8").strip()
    if not ipc_token:
        return

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
        f"http://{url_host}:{ipc_port}/hook",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Bridge-Token": ipc_token,
        },
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=3):
        pass


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else "Stop"
    try:
        _notify_bridge(event)
    except Exception:
        pass

    if event == "Stop":
        print(json.dumps({"decision": "allow"}))
    else:
        print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
