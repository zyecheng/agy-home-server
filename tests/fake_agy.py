#!/usr/bin/env python3
# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Realistic stub `agy` CLI executable for Tier 1 full-process E2E, stress, and chaos tests.

- Verifies no TELEGRAM_* or BRIDGE_IPC_* secrets leaked into its environment.
- Writes realistic JSONL trajectory steps into `<AGY_BRAIN_DIR>/<conv_id>/.system_generated/logs/transcript_full.jsonl`.
- Spawns the real `server/hook_approve.py` (PreToolUse) and `server/hook_notify.py` (PreInvocation/Stop)
  as OS child processes so the entire IPC + hook contract is exercised end-to-end.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_APPROVE = REPO_ROOT / "server" / "hook_approve.py"
HOOK_NOTIFY = REPO_ROOT / "server" / "hook_notify.py"


def _append_steps(transcript_path: Path, steps: list[dict]) -> int:
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    next_idx = 0
    if transcript_path.exists():
        for raw in transcript_path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                next_idx = max(next_idx, int(obj.get("step_index", -1)) + 1)
            except Exception:
                pass
    with transcript_path.open("a", encoding="utf-8") as fh:
        for step in steps:
            entry = dict(step)
            entry["step_index"] = next_idx
            next_idx += 1
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return next_idx - 1


def _run_hook(script: Path, args: list[str], payload: dict, timeout: int = 135) -> dict:
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=dict(os.environ),
        timeout=timeout,
    )
    out = proc.stdout.strip()
    return json.loads(out) if out else {}


def _strip_envelope(prompt: str) -> str:
    lines = prompt.splitlines()
    if lines and lines[0].startswith("[channel:"):
        lines = lines[1:]
    if lines and lines[0].startswith("[attachments:"):
        lines = lines[1:]
    if lines and lines[0].startswith("[note:"):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _extract_attachments(prompt: str) -> list[Path]:
    for line in prompt.splitlines()[:4]:
        if line.startswith("[attachments:") and line.endswith("]"):
            inner = line[len("[attachments:") : -1].strip()
            return [Path(p.strip()) for p in inner.split(",") if p.strip()]
    return []


def main() -> int:
    # Security assertion: bridge must NEVER leak TELEGRAM_* or BRIDGE_IPC_* into agy child env
    if os.environ.get("AGY_BRIDGE_ORIGIN") == "bridge":
        leaked = [
            k for k in os.environ if k.startswith(("TELEGRAM_", "BRIDGE_IPC_"))
        ]
        if leaked:
            sys.stderr.write(f"SECRET_LEAK_DETECTED: {','.join(sorted(leaked))}\n")
            return 99

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-p", "--prompt", dest="prompt", default="")
    parser.add_argument("--conversation", dest="conversation", default="")
    parser.add_argument("--output-format", dest="output_format", default="text")
    parser.add_argument("--disable-slash-commands", action="store_true")
    parser.add_argument("--print-timeout", default="")
    parser.add_argument("--model", default="")
    ns, _unknown = parser.parse_known_args()

    prompt = ns.prompt
    conv_id = ns.conversation.strip() or f"fake-conv-{uuid.uuid4().hex[:12]}"

    # Handle CLI slash commands like /compact
    if prompt.strip().startswith("/"):
        print(f"Compacted {conv_id}")
        return 0

    brain_dir = Path(
        os.environ.get("FAKE_AGY_BRAIN_DIR")
        or os.environ.get("AGY_BRAIN_DIR")
        or (Path.home() / ".gemini/antigravity-cli/brain")
    )
    tpath = brain_dir / conv_id / ".system_generated" / "logs" / "transcript_full.jsonl"
    is_ssh = os.environ.get("AGY_BRIDGE_ORIGIN", "") != "bridge"
    user_text = _strip_envelope(prompt)

    if is_ssh:
        _run_hook(
            HOOK_NOTIFY,
            ["PreInvocation"],
            {"conversationId": conv_id, "transcriptPath": str(tpath)},
            timeout=10,
        )

    m_delay = re.search(r"\[delay=([0-9.]+)\]", user_text)
    if m_delay:
        time.sleep(min(float(m_delay.group(1)), 5.0))

    # Stage-2 failure recovery turn
    if "SYSTEM RECOVERY INSTRUCTION" in prompt or "[channel: bridge-recovery" in prompt:
        reply = "RECOVERY_OK: command was denied or failed safely."
        _append_steps(
            tpath,
            [
                {"type": "USER_INPUT", "status": "DONE", "content": f"<USER_REQUEST>\n{prompt}\n</USER_REQUEST>"},
                {"type": "PLANNER_RESPONSE", "status": "DONE", "content": reply},
            ],
        )
        print(json.dumps({"conversation_id": conv_id, "status": "SUCCESS", "response": reply}))
        return 0

    # Tool call directive -> invokes real server/hook_approve.py
    m_tool = re.search(r"\[tool=(\w+)\s+cmd=([^\]]+)\]", user_text)
    if m_tool:
        tool_name = m_tool.group(1)
        cmd_str = m_tool.group(2).strip()
        hook_res = _run_hook(
            HOOK_APPROVE,
            [],
            {
                "conversationId": conv_id,
                "toolCall": {
                    "name": tool_name,
                    "args": {"CommandLine": cmd_str, "Cwd": os.getcwd()},
                },
            },
            timeout=135,
        )
        decision = str(hook_res.get("decision") or "deny")
        reason = str(hook_res.get("reason") or "")
        if decision == "auto_approve":
            reply = f"TOOL_APPROVED[{conv_id}]: {cmd_str}"
            _append_steps(
                tpath,
                [
                    {"type": "USER_INPUT", "status": "DONE", "content": f"<USER_REQUEST>\n{prompt}\n</USER_REQUEST>"},
                    {
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "tool_calls": [{"name": tool_name, "args": {"CommandLine": cmd_str}}],
                        "content": reply,
                    },
                ],
            )
            print(json.dumps({"conversation_id": conv_id, "status": "SUCCESS", "response": reply}))
            return 0
        else:
            # Emulate real agy 1.5.18 permission-denied empty SUCCESS behavior
            _append_steps(
                tpath,
                [
                    {"type": "USER_INPUT", "status": "DONE", "content": f"<USER_REQUEST>\n{prompt}\n</USER_REQUEST>"},
                    {
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "tool_calls": [{"name": tool_name, "args": {"CommandLine": cmd_str}}],
                    },
                    {
                        "type": "GENERIC",
                        "status": "ERROR",
                        "error": f'permission check failed for unsandboxed "{cmd_str}": {reason or "user denied permission"}',
                    },
                ],
            )
            print(json.dumps({"conversation_id": conv_id, "status": "SUCCESS", "response": ""}))
            return 0

    if "[long]" in user_text:
        reply = (
            "Long Report Title — 中文测试 🚀\n"
            "Second summary line for two-line preview.\n\n"
            + ("这是一段用于测试超长 Markdown 附件上传的内容 — block.\n" * 120)
        )
    else:
        att_paths = _extract_attachments(prompt)
        if att_paths:
            total_bytes = sum(p.stat().st_size for p in att_paths if p.exists())
            names = ",".join(p.name for p in att_paths if p.exists())
            reply = f"ATTACHED_OK[{len(att_paths)}:{total_bytes}:{names}]: {user_text}"
        else:
            reply = f"ECHO[{conv_id}]: {user_text}"

    _append_steps(
        tpath,
        [
            {"type": "USER_INPUT", "status": "DONE", "content": f"<USER_REQUEST>\n{prompt}\n</USER_REQUEST>"},
            {"type": "PLANNER_RESPONSE", "status": "DONE", "content": reply},
        ],
    )

    if is_ssh:
        _run_hook(
            HOOK_NOTIFY,
            ["Stop"],
            {
                "conversationId": conv_id,
                "transcriptPath": str(tpath),
                "fullyIdle": True,
            },
            timeout=10,
        )

    if ns.output_format == "json":
        print(json.dumps({"conversation_id": conv_id, "status": "SUCCESS", "response": reply}))
    else:
        print(reply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
