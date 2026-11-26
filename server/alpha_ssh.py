#!/usr/bin/env python3
# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Helper CLI to list mapped Telegram bindings and attach `agy --conversation` over SSH."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Support running directly via symlink from ~/.local/bin/alpha-ssh
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.agy_runner import build_scrubbed_env
from server.config import BridgeConfig
from server.db import Binding, BridgeStore


def select_binding(bindings: list[Binding], query: str) -> Binding | None:
    """Resolve exact dm / numeric id / thread_id matches before substring matching."""
    q = query.strip().lower()
    if not q:
        return None
    if q == "dm":
        return next((b for b in bindings if b.chat_type == "private"), None)
    if q.isdigit():
        num = int(q)
        exact = next((b for b in bindings if b.id == num), None)
        if exact is not None:
            return exact
        by_thread = next((b for b in bindings if b.thread_id == num), None)
        if by_thread is not None:
            return by_thread
    by_topic = next(
        (b for b in bindings if b.topic_name and q in b.topic_name.lower()), None
    )
    if by_topic is not None:
        return by_topic
    return next(
        (b for b in bindings if b.agy_conversation_id and q in b.agy_conversation_id.lower()),
        None,
    )


def main() -> int:
    config = BridgeConfig.from_env(REPO_ROOT / ".env")
    if not config.db_path.exists():
        print("No active channel bindings yet.")
        return 0
    store = BridgeStore(config.db_path, read_only=True)
    bindings = store.list_active_bindings()
    store.close()

    if len(sys.argv) < 2 or sys.argv[1] in ("list", "-l", "--list"):
        if not bindings:
            print("No active channel bindings yet.")
            return 0
        print(f"{'ID':<4} {'TYPE':<11} {'THREAD':<8} {'MIRROR':<13} {'TOPIC / LABEL':<24} {'AGY CONVERSATION ID'}")
        for b in bindings:
            label = "DM" if b.chat_type == "private" else (b.topic_name or f"topic-{b.thread_id}")
            print(
                f"{b.id:<4} {b.chat_type:<11} {b.thread_id:<8} {b.mirror_mode:<13} {label[:24]:<24} {b.agy_conversation_id or '(uninitialized)'}"
            )
        print("\nUsage: alpha-ssh <binding_id | dm | topic_substring>")
        return 0

    query = " ".join(sys.argv[1:]).strip()
    target = select_binding(bindings, query)

    if target is None:
        print(f"No binding matched '{query}'. Run `alpha-ssh list` to see available bindings.", file=sys.stderr)
        return 1
    if not target.agy_conversation_id:
        print(f"Binding {target.id} has no agy conversation yet (send one message first).", file=sys.stderr)
        return 1

    os.chdir(target.workspace_dir)
    env = build_scrubbed_env(config.agy_bin, bridge_origin=False)
    args = [config.agy_bin, "--conversation", target.agy_conversation_id]
    print(f"Attaching to {target.agy_conversation_id} in {target.workspace_dir} (mirror={target.mirror_mode})...")
    os.execvpe(config.agy_bin, args, env)
    return 0


if __name__ == "__main__":
    sys.exit(main())
