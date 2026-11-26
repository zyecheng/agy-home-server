#!/usr/bin/env python3
# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Antigravity PreToolUse hook for interactive Telegram Approve / Deny buttons.

- For manual SSH / non-bridge sessions (`AGY_BRIDGE_ORIGIN != "bridge"`), returns
  `{"decision": "allow"}` immediately so normal interactive TUI prompts apply.
- For bridge-driven headless runs (`AGY_BRIDGE_ORIGIN == "bridge"`):
  - Vetted read-only local tools pass through with `allow` (still governed by agy policy).
  - All other tools (including unlisted or future MCP tools) forward to the
    authenticated localhost bridge (`POST /approve`) for Telegram button approval.
- STRICTLY FAIL-CLOSED: any error, missing token, non-loopback host, or timeout returns `deny`.
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

# Explicit allowlist of vetted read-only local inspection tools.
# Every other tool (shell, file writes, web/network, tasks, schedules, MCP, future tools)
# requires explicit Telegram approval.
SAFE_READ_ONLY_TOOLS = frozenset(
    {
        "view_file",
        "read_file",
        "list_dir",
        "find_by_name",
        "grep_search",
        "list_permissions",
    }
)


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
        if not tool_name:
            return _emit("deny", "Missing tool name in PreToolUse payload (fail-closed).")
        if tool_name in SAFE_READ_ONLY_TOOLS:
            return _emit("allow")

        tool_args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}

        from server.config import load_ipc_client_settings

        url_host, ipc_port, token_path, approval_timeout = load_ipc_client_settings(REPO_ROOT)
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
            f"http://{url_host}:{ipc_port}/approve",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Bridge-Token": ipc_token,
            },
            method="POST",
        )
        # Never route localhost IPC through ambient HTTP_PROXY variables
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=max(10.0, approval_timeout + 8.0)) as resp:
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
