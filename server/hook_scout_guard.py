#!/usr/bin/env python3
# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""CaMeL Stage-1 PreToolUse hook for the quarantined web_scout workspace.

Allows ONLY:
- `search_web` (with a bounded query string <= 300 chars and deterministic per-run
  `AGY_SCOUT_MAX_SEARCHES` work budget so scouts wrap up gracefully via `finish`)
- `finish` (the synthetic structured-output tool used by `agy --json-schema`)

Hard-denies EVERY other tool (`read_url_content`, `run_command`, `view_file`,
`read_file`, `write_to_file`, `edit_file`, `list_dir`, `grep_search`,
`find_by_name`, `schedule`, `invoke_subagent`, MCP tools, etc.) and fails
closed on any malformed payload.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

MAX_QUERY_CHARS = 300
MAX_DOMAIN_CHARS = 120
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def _emit(decision: str, reason: str = "") -> int:
    out: dict[str, str] = {"decision": decision}
    if reason:
        out["reason"] = reason
    print(json.dumps(out))
    return 0


def _consume_search_budget(max_searches: int, counter_file: Path) -> tuple[bool, int]:
    """Increment per-run search_web counter safely under tempdir; returns (allowed, count)."""
    tmp_root = Path(tempfile.gettempdir()).resolve()
    if counter_file.is_symlink() or not counter_file.parent.resolve().is_relative_to(tmp_root):
        return False, max_searches

    fd = os.open(
        str(counter_file),
        os.O_RDWR | os.O_CREAT | _O_NOFOLLOW | _O_NONBLOCK,
        0o600,
    )
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return False, max_searches
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        raw = os.read(fd, 64).decode("utf-8", errors="replace").strip()
        used = int(raw) if raw.isdigit() else 0
        if used >= max_searches:
            return False, used
        next_used = used + 1
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, f"{next_used}\n".encode("utf-8"))
        return True, next_used
    except Exception:
        return False, max_searches
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
        os.close(fd)


def evaluate_tool_call(
    payload: object,
    *,
    max_searches: int | None = None,
    counter_file: Path | None = None,
) -> tuple[str, str]:
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

        if max_searches is not None and max_searches > 0 and counter_file is not None:
            allowed, used = _consume_search_budget(max_searches, counter_file)
            if not allowed:
                return (
                    "deny",
                    f"Search budget ({max_searches} search_web calls) reached (used={used}). "
                    "Do not search further; immediately call `finish` with the items you already gathered.",
                )
        return "auto_approve", ""

    return "deny", f"Tool '{name}' is blocked inside the quarantined web_scout sandbox."


def main() -> int:
    try:
        raw = sys.stdin.read().strip()
        payload = json.loads(raw) if raw else {}
    except Exception as exc:
        return _emit("deny", f"Scout guard parse error (fail-closed): {exc}")

    raw_max = os.environ.get("AGY_SCOUT_MAX_SEARCHES", "").strip()
    raw_counter = os.environ.get("AGY_SCOUT_COUNTER_FILE", "").strip()
    max_searches: int | None = None
    counter_file: Path | None = None
    if raw_max.isdigit() and int(raw_max) > 0 and raw_counter:
        max_searches = int(raw_max)
        counter_file = Path(raw_counter)

    decision, reason = evaluate_tool_call(
        payload, max_searches=max_searches, counter_file=counter_file
    )
    return _emit(decision, reason)


if __name__ == "__main__":
    sys.exit(main())
