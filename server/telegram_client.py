# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Minimal stdlib Telegram Bot API client with >3500-char .md attachment support."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
import uuid
from datetime import datetime

log = logging.getLogger("agy-bridge.telegram")


def build_two_line_preview(text: str, *, max_caption_chars: int = 900) -> str:
    """Build a concise 2-line preview caption for attached .md replies."""
    non_empty = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("```")]
    preview_lines = non_empty[:2]
    preview = "\n".join(preview_lines)
    if len(preview) > 280:
        preview = preview[:277].rstrip() + "..."
    footer = f"\n\n(Full reply attached as .md — {len(text):,} chars)"
    combined = (preview or "(Long reply)") + footer
    return combined[:max_caption_chars]


class TelegramClient:
    def __init__(self, token: str, *, max_inline_chars: int = 3500) -> None:
        self.token = token
        self.max_inline_chars = max_inline_chars

    @property
    def configured(self) -> bool:
        return bool(self.token and self.token != "replace-me")

    def _api_json(self, method: str, payload: dict | None = None, timeout: int = 40) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _api_multipart(
        self,
        method: str,
        fields: dict[str, str],
        file_field: str,
        filename: str,
        file_bytes: bytes,
        content_type: str = "text/markdown; charset=utf-8",
        timeout: int = 40,
    ) -> dict:
        boundary = f"----AgyBridge{uuid.uuid4().hex}"
        chunks: list[bytes] = []
        for k, v in fields.items():
            chunks.append(f"--{boundary}\r\n".encode("utf-8"))
            chunks.append(
                f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode("utf-8")
            )
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(file_bytes)
        chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(chunks)

        url = f"https://api.telegram.org/bot{self.token}/{method}"
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_updates(self, offset: int, timeout: int = 30) -> list[dict]:
        res = self._api_json(
            "getUpdates",
            {
                "timeout": timeout,
                "offset": offset,
                "allowed_updates": ["message"],
            },
            timeout=timeout + 15,
        )
        return list(res.get("result") or [])

    def send_typing(self, chat_id: str | int, thread_id: int = 0) -> None:
        if not self.configured:
            return
        payload: dict = {"chat_id": int(chat_id), "action": "typing"}
        if thread_id > 1:
            payload["message_thread_id"] = int(thread_id)
        try:
            self._api_json("sendChatAction", payload, timeout=10)
        except Exception:
            pass

    def send_text_or_markdown_file(
        self,
        chat_id: str | int,
        text: str,
        *,
        thread_id: int = 0,
        disable_notification: bool = False,
        prefix: str = "",
        filename_prefix: str = "alpha-reply",
    ) -> dict:
        """Send inline if <= max_inline_chars, else upload as .md with a 2-line preview."""
        full_text = f"{prefix}{text}" if prefix else text
        if len(full_text) <= self.max_inline_chars:
            return self.send_message(
                chat_id,
                full_text,
                thread_id=thread_id,
                disable_notification=disable_notification,
            )

        caption = build_two_line_preview(full_text)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"{filename_prefix}-{stamp}.md"
        return self.send_document(
            chat_id,
            filename=filename,
            content=full_text.encode("utf-8"),
            caption=caption,
            thread_id=thread_id,
            disable_notification=disable_notification,
        )

    def send_message(
        self,
        chat_id: str | int,
        text: str,
        *,
        thread_id: int = 0,
        disable_notification: bool = False,
    ) -> dict:
        if not self.configured:
            log.info(
                "[DRY-RUN Telegram sendMessage] chat=%s thread=%s silent=%s text=%s",
                chat_id,
                thread_id,
                disable_notification,
                text[:160],
            )
            return {"ok": True, "dry_run": True}

        # Telegram hard limit is 4096 chars
        safe_text = text if len(text) <= 4090 else text[:4087] + "..."
        payload: dict = {
            "chat_id": int(chat_id),
            "text": safe_text,
            "disable_notification": bool(disable_notification),
        }
        if thread_id > 1:
            payload["message_thread_id"] = int(thread_id)
        return self._api_json("sendMessage", payload)

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
        if not self.configured:
            log.info(
                "[DRY-RUN Telegram sendDocument] chat=%s thread=%s file=%s bytes=%d caption=%s",
                chat_id,
                thread_id,
                filename,
                len(content),
                caption[:120],
            )
            return {"ok": True, "dry_run": True}

        fields: dict[str, str] = {
            "chat_id": str(chat_id),
            "disable_notification": "true" if disable_notification else "false",
        }
        if caption:
            fields["caption"] = caption[:1024]
        if thread_id > 1:
            fields["message_thread_id"] = str(thread_id)
        return self._api_multipart(
            "sendDocument",
            fields=fields,
            file_field="document",
            filename=filename,
            file_bytes=content,
        )
