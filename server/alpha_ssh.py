#!/usr/bin/env python3
"""Helper CLI to list mapped Telegram bindings and attach `agy --conversation` over SSH."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Support running directly via symlink from ~/.local/bin/alpha-ssh
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.config import BridgeConfig
from server.db import BridgeStore


def main() -> int:
    config = BridgeConfig.from_env(REPO_ROOT / ".env")
    store = BridgeStore(config.db_path)
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

    query = " ".join(sys.argv[1:]).strip().lower()
    target = None
    for b in bindings:
        if query == "dm" and b.chat_type == "private":
            target = b
            break
        if query.isdigit() and (int(query) == b.id or int(query) == b.thread_id):
            target = b
            break
        if b.topic_name and query in b.topic_name.lower():
            target = b
            break
        if b.agy_conversation_id and query in b.agy_conversation_id.lower():
            target = b
            break

    if target is None:
        print(f"No binding matched '{query}'. Run `alpha-ssh list` to see available bindings.", file=sys.stderr)
        return 1
    if not target.agy_conversation_id:
        print(f"Binding {target.id} has no agy conversation yet (send one message first).", file=sys.stderr)
        return 1

    os.chdir(target.workspace_dir)
    env = os.environ.copy()
    env.pop("AGY_BRIDGE_ORIGIN", None)
    args = [config.agy_bin, "--conversation", target.agy_conversation_id]
    print(f"Attaching to {target.agy_conversation_id} in {target.workspace_dir} (mirror={target.mirror_mode})...")
    os.execvpe(config.agy_bin, args, env)
    return 0


if __name__ == "__main__":
    sys.exit(main())
