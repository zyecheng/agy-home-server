# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Lock-free, fail-closed Telegram inline-button approval manager for PreToolUse hooks."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field

from .telegram_client import TelegramClient

log = logging.getLogger("agy-bridge.approvals")

DEFAULT_APPROVAL_TIMEOUT_SECONDS = 120.0


def _summarize_tool_call(tool_name: str, tool_args: dict) -> str:
    """Format a concise, readable summary of a proposed tool call for Telegram."""
    if not isinstance(tool_args, dict):
        return f"Tool: {tool_name}"
    if tool_name == "run_command":
        cmd = str(tool_args.get("CommandLine") or "").strip()
        cwd = str(tool_args.get("Cwd") or "").strip()
        lines = [f"Tool: {tool_name}"]
        if cwd:
            lines.append(f"Cwd: {cwd}")
        if cmd:
            if len(cmd) > 900:
                cmd = cmd[:897] + "..."
            lines.append(f"Command:\n{cmd}")
        return "\n".join(lines)

    for key in ("TargetFile", "Path", "AbsolutePath", "Url"):
        val = tool_args.get(key)
        if val:
            s = str(val).strip()
            if len(s) > 400:
                s = s[:397] + "..."
            return f"Tool: {tool_name}\n{key}: {s}"

    compact = json.dumps(tool_args, ensure_ascii=False)
    if len(compact) > 600:
        compact = compact[:597] + "..."
    return f"Tool: {tool_name}\nArgs: {compact}"


def compute_tool_digest(tool_name: str, tool_args: dict) -> str:
    canonical = json.dumps(
        {"name": tool_name, "args": tool_args}, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass
class PendingApproval:
    nonce: str
    chat_id: str
    thread_id: int
    tool_name: str
    args_digest: str
    summary: str
    expires_at: float
    message_id: int | None = None
    decision: str = "deny"
    reason: str = "Approval timed out (fail-closed)."
    event: threading.Event = field(default_factory=threading.Event)


class ApprovalManager:
    """Coordinates PreToolUse approval waits and Telegram callback_query responses.

    Uses its own lock exclusively (never acquires BridgeRouter endpoint locks)
    so approval IPC cannot deadlock with an in-flight turn.
    """

    def __init__(
        self,
        telegram: TelegramClient,
        *,
        timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    ) -> None:
        self.telegram = telegram
        self.timeout_seconds = max(0.05, float(timeout_seconds))
        self._lock = threading.Lock()
        self._pending: dict[str, PendingApproval] = {}

    def request_approval(
        self,
        *,
        chat_id: str,
        thread_id: int,
        tool_name: str,
        tool_args: dict,
    ) -> dict:
        nonce = secrets.token_hex(12)  # 24 hex chars; callback_data stays well under 64B
        digest = compute_tool_digest(tool_name, tool_args)
        summary = _summarize_tool_call(tool_name, tool_args)
        timeout_label = (
            f"{int(round(self.timeout_seconds))}s"
            if self.timeout_seconds >= 1.0
            else f"{self.timeout_seconds:g}s"
        )
        prompt_text = (
            f"Approval required ({timeout_label} timeout, fail-closed):\n\n"
            f"{summary}\n\n"
            f"[id:{digest}]"
        )
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "Approve", "callback_data": f"ap:{nonce}"},
                    {"text": "Deny", "callback_data": f"dn:{nonce}"},
                ]
            ]
        }

        pending = PendingApproval(
            nonce=nonce,
            chat_id=str(chat_id),
            thread_id=int(thread_id),
            tool_name=tool_name,
            args_digest=digest,
            summary=summary,
            expires_at=time.time() + self.timeout_seconds,
        )

        with self._lock:
            self._pending[nonce] = pending

        try:
            res = self.telegram.send_message(
                chat_id,
                prompt_text,
                thread_id=thread_id,
                reply_markup=reply_markup,
            )
            msg_obj = res.get("result") if isinstance(res, dict) else None
            if isinstance(msg_obj, dict) and msg_obj.get("message_id") is not None:
                pending.message_id = int(msg_obj["message_id"])
        except Exception as exc:
            log.exception("Failed sending Telegram approval prompt; denying fail-closed")
            with self._lock:
                self._pending.pop(nonce, None)
            return {
                "decision": "deny",
                "reason": f"Could not deliver Telegram approval prompt: {exc}",
            }

        signaled = pending.event.wait(timeout=self.timeout_seconds)
        with self._lock:
            self._pending.pop(nonce, None)

        if not signaled:
            if pending.message_id is not None:
                try:
                    self.telegram.edit_message_text(
                        pending.chat_id,
                        pending.message_id,
                        f"Timed out — auto-denied ({timeout_label}):\n\n{summary}",
                    )
                except Exception:
                    pass
            return {
                "decision": "deny",
                "reason": f"User did not approve within {timeout_label} (fail-closed).",
            }

        out = {"decision": pending.decision}
        if pending.reason:
            out["reason"] = pending.reason
        return out

    def resolve_callback(
        self,
        *,
        chat_id: str,
        callback_query_id: str,
        callback_data: str,
    ) -> bool:
        """Consume a single-use approval callback and wake the waiting hook."""
        if ":" not in callback_data:
            return False
        action, nonce = callback_data.split(":", 1)
        if action not in ("ap", "dn") or not nonce:
            return False

        with self._lock:
            pending = self._pending.pop(nonce, None)

        if pending is None:
            self._safe_answer(callback_query_id, "Approval already resolved or expired.")
            return False

        if str(chat_id) != pending.chat_id:
            # Put it back so another chat cannot spoof-consume the owner's approval
            with self._lock:
                if time.time() < pending.expires_at and not pending.event.is_set():
                    self._pending[nonce] = pending
            self._safe_answer(callback_query_id, "Chat mismatch.")
            return False

        if time.time() >= pending.expires_at:
            self._safe_answer(callback_query_id, "Approval expired (denied).")
            pending.decision = "deny"
            pending.reason = "Approval expired (fail-closed)."
            pending.event.set()
            return False

        if action == "ap":
            pending.decision = "auto_approve"
            pending.reason = "Approved by user via Telegram."
            status_label = "Approved via Telegram"
            toast = "Approved"
        else:
            pending.decision = "deny"
            pending.reason = "Denied by user via Telegram."
            status_label = "Denied via Telegram"
            toast = "Denied"

        pending.event.set()
        self._safe_answer(callback_query_id, toast)

        if pending.message_id is not None:
            try:
                self.telegram.edit_message_text(
                    pending.chat_id,
                    pending.message_id,
                    f"{status_label}:\n\n{pending.summary}",
                )
            except Exception:
                log.debug("Could not edit approval message %s", pending.message_id)
        return True

    def cancel_all(self, reason: str = "Bridge shutting down (fail-closed).") -> None:
        """Wake and fail-close every pending approval on daemon shutdown."""
        with self._lock:
            items = list(self._pending.values())
            self._pending.clear()
        for pending in items:
            pending.decision = "deny"
            pending.reason = reason
            pending.event.set()

    def _safe_answer(self, callback_query_id: str, text: str) -> None:
        if not callback_query_id:
            return
        try:
            self.telegram.answer_callback_query(callback_query_id, text=text)
        except Exception:
            pass
