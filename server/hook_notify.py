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

    from server.config import parse_dotenv

    env_override = os.environ.get("BRIDGE_ENV_FILE")
    env_path = Path(env_override) if env_override else (REPO_ROOT / ".env")
    env_vals = parse_dotenv(env_path)
    ipc_host = (
        os.environ.get("BRIDGE_IPC_HOST") or env_vals.get("BRIDGE_IPC_HOST") or "127.0.0.1"
    ).strip()
    ipc_port = int(
        (os.environ.get("BRIDGE_IPC_PORT") or env_vals.get("BRIDGE_IPC_PORT") or "8765").strip()
    )
    db_path = Path(
        os.environ.get("BRIDGE_DB_PATH")
        or env_vals.get("BRIDGE_DB_PATH")
        or str(REPO_ROOT / "data" / "bridge.db")
    )
    token_path = Path(
        os.environ.get("BRIDGE_IPC_TOKEN_PATH")
        or env_vals.get("BRIDGE_IPC_TOKEN_PATH")
        or str(db_path.parent / "ipc.token")
    )
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
        f"http://{ipc_host}:{ipc_port}/hook",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Bridge-Token": ipc_token,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=3):
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
