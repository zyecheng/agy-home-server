#!/usr/bin/env python3
# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Antigravity PreToolUse hook for interactive Telegram Approve / Deny buttons.

- For manual SSH / non-bridge sessions (`AGY_BRIDGE_ORIGIN != "bridge"`), returns
  `{"decision": "allow"}` immediately so normal interactive TUI prompts apply.
- For bridge-driven headless runs (`AGY_BRIDGE_ORIGIN == "bridge"`), forwards the
  proposed tool call to the authenticated local bridge (`POST /approve`) and waits
  for the user's Telegram button tap.
- STRICTLY FAIL-CLOSED: any error, missing token, or timeout returns `deny`.
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


def _emit(decision: str, reason: str = "") -> int:
    out: dict[str, str] = {"decision": decision}
    if reason:
        out["reason"] = reason
    print(json.dumps(out))
    return 0


def main() -> int:
    # Only gate bridge-driven headless turns; leave interactive SSH TUI alone
    if os.environ.get("AGY_BRIDGE_ORIGIN", "") != "bridge":
        return _emit("allow")

    try:
        raw = sys.stdin.read().strip()
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            return _emit("deny", "Invalid PreToolUse hook payload (fail-closed).")

        tool_call = payload.get("toolCall")
        if not isinstance(tool_call, dict):
            return _emit("deny", "Missing toolCall in PreToolUse payload (fail-closed).")

        tool_name = str(tool_call.get("name") or "").strip()
        tool_args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}

        from server.config import parse_dotenv

        env_override = os.environ.get("BRIDGE_ENV_FILE")
        env_path = Path(env_override) if env_override else (REPO_ROOT / ".env")
        env_vals = parse_dotenv(env_path)
        ipc_host = (
            os.environ.get("BRIDGE_IPC_HOST")
            or env_vals.get("BRIDGE_IPC_HOST")
            or "127.0.0.1"
        ).strip()
        ipc_port = int(
            (
                os.environ.get("BRIDGE_IPC_PORT")
                or env_vals.get("BRIDGE_IPC_PORT")
                or "8765"
            ).strip()
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
            return _emit("deny", "Bridge IPC token unavailable (fail-closed).")
        ipc_token = token_path.read_text(encoding="utf-8").strip()
        if not ipc_token:
            return _emit("deny", "Empty bridge IPC token (fail-closed).")

        body = json.dumps(
            {
                "conversationId": str(payload.get("conversationId") or ""),
                "bindingId": os.environ.get("AGY_BRIDGE_BINDING_ID", ""),
                "toolCall": {"name": tool_name, "args": tool_args},
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            f"http://{ipc_host}:{ipc_port}/approve",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Bridge-Token": ipc_token,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=128) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        decision = str(result.get("decision") or "deny")
        reason = str(result.get("reason") or "")
        if decision != "auto_approve":
            return _emit("deny", reason or "Denied by Telegram approval gate.")
        return _emit("auto_approve", reason)
    except Exception as exc:
        return _emit("deny", f"Telegram approval bridge error (fail-closed): {exc}")


if __name__ == "__main__":
    sys.exit(main())
