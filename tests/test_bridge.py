# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Unit and integration tests for SQLite router, transcript parser, .md upload, and SSH mirroring."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from server.agy_runner import AgyResult
from server.config import BridgeConfig
from server.db import BridgeStore
from server.router import BridgeRouter, InboundMessage
from server.telegram_client import TelegramClient, build_two_line_preview
from server.transcript import (
    extract_user_request_text,
    read_steps_after,
    resolve_transcript_file,
)


class FakeTelegram(TelegramClient):
    def __init__(self, max_inline_chars: int = 3500) -> None:
        super().__init__(token="", max_inline_chars=max_inline_chars)
        self.sent_messages: list[dict] = []
        self.sent_documents: list[dict] = []

    def send_message(
        self,
        chat_id: str | int,
        text: str,
        *,
        thread_id: int = 0,
        disable_notification: bool = False,
    ) -> dict:
        item = {
            "chat_id": str(chat_id),
            "thread_id": thread_id,
            "text": text,
            "silent": disable_notification,
        }
        self.sent_messages.append(item)
        return {"ok": True}

    def send_document(
        self,
        chat_id: str | int,
        *,
        filename: str,
        content: bytes,
        caption: str = "",
        thread_id: int = 0,
        disable_notification: bool = False,
    ) -> dict:
        item = {
            "chat_id": str(chat_id),
            "thread_id": thread_id,
            "filename": filename,
            "content": content.decode("utf-8"),
            "caption": caption,
            "silent": disable_notification,
        }
        self.sent_documents.append(item)
        return {"ok": True}


class FakeRunner:
    def __init__(self, responses: list[AgyResult], on_call=None) -> None:
        self.responses = list(responses)
        self.on_call = on_call
        self.calls: list[dict] = []

    def run_prompt(self, prompt: str, *, conversation_id=None, workspace_dir=None, **kwargs) -> AgyResult:
        idx = len(self.calls)
        self.calls.append({"prompt": prompt, "conversation_id": conversation_id, "cwd": workspace_dir})
        if self.on_call is not None:
            self.on_call(idx)
        return self.responses.pop(0)

    def run_cli_slash(self, slash_cmd: str, *, conversation_id=None, workspace_dir=None):
        return 0, f"ran {slash_cmd}"


class BridgeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.brain = self.root / "brain"
        self.workspace = self.root / "alpha"
        self.workspace.mkdir()
        self.cfg = BridgeConfig(
            telegram_bot_token="",
            allowed_user_ids=frozenset({111}),
            allowed_chat_ids=frozenset(),
            poll_timeout_seconds=1,
            debounce_seconds=0.0,
            max_inline_chars=3500,
            ssh_prompt_preview_chars=300,
            default_mirror_mode="full",
            alpha_workspace=self.workspace,
            agy_bin="agy",
            agy_brain_dir=self.brain,
            agy_model="",
            agy_timeout_seconds=60,
            db_path=self.root / "bridge.db",
            ipc_host="127.0.0.1",
            ipc_port=0,
            timezone_label="PST",
        )
        self.store = BridgeStore(self.cfg.db_path)
        self.tg = FakeTelegram(max_inline_chars=3500)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _write_transcript(self, conv_id: str, steps: list[dict], full: bool = False) -> Path:
        logs = self.brain / conv_id / ".system_generated" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        fname = "transcript_full.jsonl" if full else "transcript.jsonl"
        p = logs / fname
        p.write_text("\n".join(json.dumps(s) for s in steps) + "\n", encoding="utf-8")
        return p

    def test_dm_and_supergroup_topics_map_to_distinct_conversations(self) -> None:
        runner = FakeRunner(
            [
                AgyResult(conversation_id="conv-dm-1", status="SUCCESS", response="DM reply"),
                AgyResult(conversation_id="conv-topic-42", status="SUCCESS", response="Topic 42 reply"),
                AgyResult(conversation_id="conv-topic-42", status="SUCCESS", response="Topic 42 follow-up"),
            ]
        )
        router = BridgeRouter(self.cfg, self.store, runner, self.tg)  # type: ignore[arg-type]

        router._process_batch([InboundMessage("telegram", "111", 0, "private", 111, "u", "1", "hi dm")])
        router._process_batch([InboundMessage("telegram", "-100999", 42, "supergroup", 111, "u", "2", "hi topic", topic_name="Networking")])
        router._process_batch([InboundMessage("telegram", "-100999", 42, "supergroup", 111, "u", "3", "follow up")])

        dm = self.store.get_active_binding("telegram", "111", 0)
        topic = self.store.get_active_binding("telegram", "-100999", 42)
        self.assertIsNotNone(dm)
        self.assertIsNotNone(topic)
        self.assertEqual(dm.agy_conversation_id, "conv-dm-1")
        self.assertEqual(topic.agy_conversation_id, "conv-topic-42")
        self.assertEqual(topic.topic_name, "Networking")
        # Third call resumed conv-topic-42
        self.assertEqual(runner.calls[2]["conversation_id"], "conv-topic-42")

    def test_long_reply_uploads_markdown_with_two_line_preview(self) -> None:
        long_reply = "First summary line.\nSecond detail line.\n\n" + ("x" * 3600)
        runner = FakeRunner([AgyResult(conversation_id="conv-long", status="SUCCESS", response=long_reply)])
        router = BridgeRouter(self.cfg, self.store, runner, self.tg)  # type: ignore[arg-type]

        router._process_batch([InboundMessage("telegram", "111", 0, "private", 111, "u", "10", "be specific")])

        self.assertEqual(len(self.tg.sent_messages), 0)
        self.assertEqual(len(self.tg.sent_documents), 1)
        doc = self.tg.sent_documents[0]
        self.assertTrue(doc["filename"].endswith(".md"))
        self.assertEqual(doc["content"], long_reply)
        self.assertIn("First summary line.\nSecond detail line.", doc["caption"])
        self.assertIn("Full reply attached as .md", doc["caption"])

    def test_ssh_stop_hook_mirrors_full_silently_and_dedups(self) -> None:
        conv_id = "conv-sync-1"
        b = self.store.get_or_create_binding("telegram", "111", 0, "private", str(self.workspace))
        self.store.set_conversation_id(b.id, conv_id)
        self.store.update_watermark(b.id, 1)

        tpath = self._write_transcript(
            conv_id,
            [
                {"step_index": 0, "type": "USER_INPUT", "status": "DONE", "content": "<USER_REQUEST>\nold msg\n</USER_REQUEST>"},
                {"step_index": 1, "type": "PLANNER_RESPONSE", "status": "DONE", "content": "old reply"},
                {"step_index": 2, "type": "USER_INPUT", "status": "DONE", "content": "<USER_REQUEST>\nCheck disk space over SSH\n</USER_REQUEST>"},
                {"step_index": 3, "type": "PLANNER_RESPONSE", "status": "DONE", "content": "Checking now..."},
                {"step_index": 4, "type": "PLANNER_RESPONSE", "status": "DONE", "content": "Root disk is at 26%."},
            ],
        )

        router = BridgeRouter(self.cfg, self.store, FakeRunner([]), self.tg)  # type: ignore[arg-type]
        res = router.handle_hook_event(
            {"event": "Stop", "origin": "ssh", "conversationId": conv_id, "transcriptPath": str(tpath)}
        )
        self.assertEqual(res["mirrored_messages"], 2)
        self.assertEqual(self.tg.sent_messages[0]["text"], "[SSH] Check disk space over SSH")
        self.assertTrue(self.tg.sent_messages[0]["silent"])
        self.assertEqual(self.tg.sent_messages[1]["text"], "Root disk is at 26%.")
        self.assertTrue(self.tg.sent_messages[1]["silent"])

        # Re-firing Stop hook should mirror 0 new messages (watermark advanced)
        res2 = router.handle_hook_event(
            {"event": "Stop", "origin": "ssh", "conversationId": conv_id, "transcriptPath": str(tpath)}
        )
        self.assertEqual(res2["mirrored_messages"], 0)

    def test_transcript_prefers_full_file_and_extracts_user_request(self) -> None:
        conv_id = "conv-full"
        t_compact = self._write_transcript(
            conv_id,
            [{"step_index": 0, "type": "PLANNER_RESPONSE", "status": "DONE", "content": "truncated"}],
            full=False,
        )
        self._write_transcript(
            conv_id,
            [{"step_index": 0, "type": "PLANNER_RESPONSE", "status": "DONE", "content": "complete text"}],
            full=True,
        )
        resolved = resolve_transcript_file(transcript_path=t_compact)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.name, "transcript_full.jsonl")
        steps = read_steps_after(resolved, -1)
        self.assertEqual(steps[0].text, "complete text")
        self.assertEqual(
            extract_user_request_text("<USER_REQUEST>\nhello world\n</USER_REQUEST>\n<ADDITIONAL_METADATA>x</ADDITIONAL_METADATA>"),
            "hello world",
        )
        preview = build_two_line_preview("Line 1\n\nLine 2\nLine 3\n" + "a" * 4000)
        self.assertTrue(preview.startswith("Line 1\nLine 2"))

    def test_empty_success_permission_denied_triggers_alpha_recovery(self) -> None:
        conv_id = "conv-perm-denied"
        b = self.store.get_or_create_binding("telegram", "111", 0, "private", str(self.workspace))
        self.store.set_conversation_id(b.id, conv_id)
        self.store.update_watermark(b.id, 1)

        base_steps = [
            {"step_index": 0, "type": "USER_INPUT", "status": "DONE", "content": "<USER_REQUEST>\nhi\n</USER_REQUEST>"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "status": "DONE", "content": "hi"},
        ]
        self._write_transcript(conv_id, base_steps)

        def append_failed_steps(call_idx: int) -> None:
            if call_idx == 0:
                self._write_transcript(
                    conv_id,
                    base_steps
                    + [
                        {
                            "step_index": 4,
                            "type": "PLANNER_RESPONSE",
                            "status": "DONE",
                            "tool_calls": [{"name": "run_command", "args": {"CommandLine": "uptime && free -h"}}],
                        },
                        {
                            "step_index": 5,
                            "type": "GENERIC",
                            "status": "ERROR",
                            "error": 'permission check failed for unsandboxed "uptime && free -h": user denied permission to run command',
                        },
                    ],
                )

        # First call mimics real agy bug (status=SUCCESS, empty response); second call is Alpha's no-tool recovery reply
        runner = FakeRunner(
            [
                AgyResult(conversation_id=conv_id, status="SUCCESS", response=""),
                AgyResult(
                    conversation_id=conv_id,
                    status="SUCCESS",
                    response="刚才查系统资源的命令在无头模式下被权限拦截了，你可以先发 /status 或在 settings.json 放开权限。",
                ),
            ],
            on_call=append_failed_steps,
        )
        router = BridgeRouter(self.cfg, self.store, runner, self.tg)  # type: ignore[arg-type]
        router._process_batch([InboundMessage("telegram", "111", 0, "private", 111, "u", "20", "你能查一下系统资源吗")])

        self.assertEqual(len(runner.calls), 2)
        self.assertIn("permission_denied", runner.calls[1]["prompt"])
        self.assertIn("uptime && free -h", runner.calls[1]["prompt"])
        self.assertEqual(len(self.tg.sent_messages), 1)
        sent_text = self.tg.sent_messages[0]["text"]
        self.assertNotIn("agy error (SUCCESS)", sent_text)
        self.assertIn("权限拦截", sent_text)
        self.assertIn("[permission_denied | step 5]", sent_text)

    def test_auth_error_skips_recovery_and_uses_fallback_report(self) -> None:
        runner = FakeRunner(
            [
                AgyResult(
                    conversation_id="",
                    status="ERROR",
                    response="",
                    error="authentication required: You are not logged into Antigravity.",
                )
            ]
        )
        router = BridgeRouter(self.cfg, self.store, runner, self.tg)  # type: ignore[arg-type]
        router._process_batch([InboundMessage("telegram", "111", 0, "private", 111, "u", "21", "hello")])

        # Must NOT call agy a second time on auth failure
        self.assertEqual(len(runner.calls), 1)
        sent_text = self.tg.sent_messages[0]["text"]
        self.assertIn("auth_required", sent_text)
        self.assertIn("ssh agy-staging", sent_text)

    def test_failed_recovery_falls_back_to_deterministic_report(self) -> None:
        conv_id = "conv-rec-fail"
        b = self.store.get_or_create_binding("telegram", "111", 0, "private", str(self.workspace))
        self.store.set_conversation_id(b.id, conv_id)
        self.store.update_watermark(b.id, 1)

        def append_err(call_idx: int) -> None:
            if call_idx == 0:
                self._write_transcript(
                    conv_id,
                    [
                        {
                            "step_index": 2,
                            "type": "GENERIC",
                            "status": "ERROR",
                            "error": 'permission check failed for unsandboxed "df -h": user denied permission',
                        }
                    ],
                )

        runner = FakeRunner(
            [
                AgyResult(conversation_id=conv_id, status="SUCCESS", response=""),
                AgyResult(conversation_id=conv_id, status="ERROR", response="", error="secondary crash"),
            ],
            on_call=append_err,
        )
        router = BridgeRouter(self.cfg, self.store, runner, self.tg)  # type: ignore[arg-type]
        router._process_batch([InboundMessage("telegram", "111", 0, "private", 111, "u", "22", "check disk")])

        self.assertEqual(len(runner.calls), 2)
        sent_text = self.tg.sent_messages[0]["text"]
        self.assertIn("permission_denied", sent_text)
        self.assertIn("df -h", sent_text)
        self.assertNotIn("agy error (SUCCESS)", sent_text)


if __name__ == "__main__":
    unittest.main()
