# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Tier 2: Automated E2E suite against the REAL `agy` CLI binary and real hooks.

Opt-in via `RUN_LIVE_AGY_E2E=1` so offline/CI unit runs consume zero LLM quota.
Runs a completely isolated `python3 -m server.bot` + temp SQLite DB + temp workspace
pointed at the local `MockTelegramServer` (never touches production DB or real Telegram).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.mock_telegram import MockTelegramServer
from tests.test_e2e_stress import BotProcessHarness

REPO_ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(
    os.environ.get("RUN_LIVE_AGY_E2E") == "1",
    "Set RUN_LIVE_AGY_E2E=1 on agy-staging to run live agy CLI automated E2E tests",
)
class Tier2LiveAgyE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self.agy_bin = Path(
            os.environ.get("AGY_BIN") or str(Path.home() / ".local/bin/agy")
        )
        if not self.agy_bin.exists():
            self.skipTest(f"Real agy binary not found at {self.agy_bin}")

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.tg = MockTelegramServer()

        self.bot = BotProcessHarness(
            root=self.root,
            mock_tg=self.tg,
            agy_bin=self.agy_bin,
            debounce_seconds=0.0,
            approval_timeout_seconds=60.0,
            extra_env={
                # Real agy writes transcripts to ~/.gemini/antigravity-cli/brain
                "AGY_BRAIN_DIR": str(Path.home() / ".gemini/antigravity-cli/brain"),
                "AGY_TIMEOUT_SECONDS": "120",
            },
        )
        # Copy real Alpha persona & real hooks.json into the isolated temp workspace
        ws_agents = self.bot.workspace / ".agents"
        ws_agents.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / "alpha-workspace" / "GEMINI.md", self.bot.workspace / "GEMINI.md")
        shutil.copy2(
            REPO_ROOT / "alpha-workspace" / ".agents" / "hooks.json",
            ws_agents / "hooks.json",
        )
        self.bot.start(timeout=15.0)

    def tearDown(self) -> None:
        self.bot.stop(timeout=15.0)
        self.tg.close()
        self.tmp.cleanup()

    def test_real_agy_normal_approve_deny_and_ssh_mirror_e2e(self) -> None:
        # 1. Basic DM turn with real agy CLI
        self.tg.push_message(
            chat_id=111,
            text="Reply with exactly the single token AGY_LIVE_PONG and nothing else. Do not run any tools.",
        )
        ok = self.tg.wait_for(
            lambda: any("AGY_LIVE_PONG" in m["text"] for m in self.tg.sent_messages),
            timeout=90.0,
        )
        self.assertTrue(
            ok,
            f"Real agy basic turn failed. msgs={self.tg.sent_messages}\nstderr={self.bot.read_stderr()}",
        )

        # Fetch real conversation UUID via /id
        self.tg.push_message(chat_id=111, text="/id")
        ok = self.tg.wait_for(
            lambda: any("agy_conversation_id:" in m["text"] for m in self.tg.sent_messages),
            timeout=10.0,
        )
        self.assertTrue(ok)
        id_msg = next(m["text"] for m in self.tg.sent_messages if "agy_conversation_id:" in m["text"])
        conv_id = [
            ln.split(": ", 1)[1].strip()
            for ln in id_msg.splitlines()
            if ln.startswith("agy_conversation_id:")
        ][0]
        self.assertNotEqual(conv_id, "(none yet)")
        print(f"\n[TIER2 LIVE] Bound real agy conversation: {conv_id}", flush=True)

        # 2. Real PreToolUse Approve flow (MockTelegram auto-clicks Approve!)
        self.tg.set_approval_policy(lambda _rec: ("approve", 0.2, 111))
        before_approve = len(self.tg.sent_messages)
        self.tg.push_message(
            chat_id=111,
            text="Please run the shell command `echo AGY_E2E_APPROVE_OK` using run_command and include its exact output in your reply.",
        )
        ok = self.tg.wait_for(
            lambda: any(
                "AGY_E2E_APPROVE_OK" in m["text"]
                and m.get("reply_markup") is None
                for m in self.tg.sent_messages[before_approve:]
            ),
            timeout=120.0,
        )
        self.assertTrue(
            ok,
            f"Real agy Approve E2E failed. msgs={self.tg.sent_messages[before_approve:]}\nedits={self.tg.edited_messages}",
        )
        self.assertTrue(
            any("Approved via Telegram" in e["text"] for e in self.tg.edited_messages),
            f"Expected Approved edit, got {self.tg.edited_messages}",
        )
        print("[TIER2 LIVE] PreToolUse [Approve] verified end-to-end!", flush=True)

        # 3. Real PreToolUse Deny flow (MockTelegram auto-clicks Deny!)
        self.tg.set_approval_policy(lambda _rec: ("deny", 0.2, 111))
        before_deny = len(self.tg.sent_messages)
        self.tg.push_message(
            chat_id=111,
            text="Please run the shell command `echo AGY_E2E_FORBIDDEN_TOKEN` using run_command and show the output.",
        )
        ok = self.tg.wait_for(
            lambda: any("Denied via Telegram" in e["text"] for e in self.tg.edited_messages)
            and any(
                m.get("reply_markup") is None
                for m in self.tg.sent_messages[before_deny:]
            ),
            timeout=120.0,
        )
        self.assertTrue(
            ok,
            f"Real agy Deny E2E failed. msgs={self.tg.sent_messages[before_deny:]}\nedits={self.tg.edited_messages}",
        )
        deny_replies = [
            m["text"] for m in self.tg.sent_messages[before_deny:] if m.get("reply_markup") is None
        ]
        self.assertTrue(deny_replies)
        self.assertFalse(any("agy error (SUCCESS)" in r for r in deny_replies))
        print(f"[TIER2 LIVE] PreToolUse [Deny] verified end-to-end! reply={deny_replies[-1][:140]!r}", flush=True)

        # 4. Real SSH Stop hook mirroring on the SAME live conversation
        before_ssh = len(self.tg.sent_messages)
        ssh_env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("TELEGRAM_", "BRIDGE_", "AGY_"))
        }
        ssh_env["BRIDGE_ENV_FILE"] = str(self.bot.env_file)
        ssh_proc = subprocess.run(
            [
                str(self.agy_bin),
                "-p",
                "Reply with exactly AGY_SSH_MIRROR_OK and nothing else. Do not run any tools.",
                "--conversation",
                conv_id,
                "--output-format",
                "json",
                "--disable-slash-commands",
            ],
            cwd=str(self.bot.workspace),
            env=ssh_env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(ssh_proc.returncode, 0, ssh_proc.stderr)
        ok = self.tg.wait_for(
            lambda: any(
                m["disable_notification"] and "AGY_SSH_MIRROR_OK" in m["text"]
                for m in self.tg.sent_messages[before_ssh:]
            ),
            timeout=20.0,
        )
        self.assertTrue(
            ok,
            f"Real SSH Stop hook mirror missing. msgs={self.tg.sent_messages[before_ssh:]}\nstderr={self.bot.read_stderr()}",
        )
        print("[TIER2 LIVE] Real SSH Stop hook mirror verified end-to-end!", flush=True)


if __name__ == "__main__":
    unittest.main()
