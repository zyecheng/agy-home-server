#!/usr/bin/env python3
# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""CaMeL Stage-1 PreToolUse hook for the quarantined web_scout workspace.

Allows ONLY:
- `search_web` (with a bounded query string <= 300 chars)
- `finish` (the synthetic structured-output tool used by `agy --json-schema`)

Hard-denies EVERY other tool (`read_url_content`, `run_command`, `view_file`,
`read_file`, `write_to_file`, `edit_file`, `list_dir`, `grep_search`,
`find_by_name`, `schedule`, `invoke_subagent`, MCP tools, etc.) and fails
closed on any malformed payload.
"""

from __future__ import annotations

import json
import sys

MAX_QUERY_CHARS = 300
MAX_DOMAIN_CHARS = 120


def _emit(decision: str, reason: str = "") -> int:
    out: dict[str, str] = {"decision": decision}
    if reason:
        out["reason"] = reason
    print(json.dumps(out))
    return 0


def evaluate_tool_call(payload: object) -> tuple[str, str]:
    if not isinstance(payload, dict):
        return "deny", "Invalid PreToolUse payload (fail-closed)."

    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict):
        return "deny", "Missing toolCall in PreToolUse payload (fail-closed)."

    name = str(tool_call.get("name") or "").strip()
    if not name:
        return "deny", "Missing tool name in PreToolUse payload (fail-closed)."

    if name == "finish":
        return "auto_approve", ""

    if name == "search_web":
        args = tool_call.get("args")
        if not isinstance(args, dict):
            return "deny", "search_web requires object args."
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return "deny", "search_web requires a non-empty query string."
        if len(query) > MAX_QUERY_CHARS:
            return "deny", f"search_web query exceeds {MAX_QUERY_CHARS} chars."
        domain = args.get("domain")
        if domain is not None:
            if not isinstance(domain, str) or len(domain) > MAX_DOMAIN_CHARS:
                return "deny", "Invalid search_web domain parameter."
        return "auto_approve", ""

    return "deny", f"Tool '{name}' is blocked inside the quarantined web_scout sandbox."


def main() -> int:
    try:
        raw = sys.stdin.read().strip()
        payload = json.loads(raw) if raw else {}
    except Exception as exc:
        return _emit("deny", f"Scout guard parse error (fail-closed): {exc}")
    decision, reason = evaluate_tool_call(payload)
    return _emit(decision, reason)


if __name__ == "__main__":
    sys.exit(main())
