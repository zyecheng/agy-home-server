#!/usr/bin/env python3
# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Antigravity PreToolUse hook for interactive Telegram Approve / Deny buttons
and Stage-2 watcher briefing zero-tool lockout.

Evaluation order (STRICTLY FAIL-CLOSED):
1. `AGY_WATCHER_BRIEFING == "1"` -> hard-deny 100% of tools immediately (before
   checking `AGY_BRIDGE_ORIGIN` or `SAFE_READ_ONLY_TOOLS` — zero local file reads,
   zero shell, zero writes, zero network, zero MCP!).
2. Non-bridge sessions (`AGY_BRIDGE_ORIGIN != "bridge"`) -> `allow` so normal
   interactive SSH TUI prompts apply.
3. Bridge-driven interactive runs (`AGY_BRIDGE_ORIGIN == "bridge"`):
   - Vetted read-only local tools pass through with `allow`.
   - All other tools forward to authenticated localhost `POST /approve` for
     Telegram `[Approve]` / `[Deny]` confirmation.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Explicit allowlist of vetted read-only local inspection tools for interactive bridge turns.
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
    # 1. CaMeL Stage-2 watcher briefing lockout: evaluated FIRST, denies 100% of tools
    if os.environ.get("AGY_WATCHER_BRIEFING", "") == "1":
        return _emit(
            "deny",
            "All tools are disabled during watcher briefing (AGY_WATCHER_BRIEFING=1).",
        )

    # 2. Only gate bridge-driven headless turns; leave interactive SSH TUI alone
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

        from server.config import load_ipc_client_settings, post_bridge_ipc

        url_host, ipc_port, token_path, approval_timeout = load_ipc_client_settings(REPO_ROOT)
        code, result = post_bridge_ipc(
            url_host,
            ipc_port,
            token_path,
            "/approve",
            {
                "conversationId": str(payload.get("conversationId") or ""),
                "bindingId": os.environ.get("AGY_BRIDGE_BINDING_ID", ""),
                "toolCall": {"name": tool_name, "args": tool_args},
            },
            timeout=max(10.0, approval_timeout + 8.0),
        )
        if code != 200:
            err = str(result.get("error") or f"HTTP {code}")
            return _emit("deny", f"Telegram approval bridge rejected ({err}, fail-closed).")

        decision = str(result.get("decision") or "deny")
        reason = str(result.get("reason") or "")
        if decision != "auto_approve":
            return _emit("deny", reason or "Denied by Telegram approval gate.")
        return _emit("auto_approve", reason)
    except Exception as exc:
        return _emit("deny", f"Telegram approval bridge error (fail-closed): {exc}")


if __name__ == "__main__":
    sys.exit(main())
