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
_INLINE_CMD_LIMIT = 1200
_INLINE_CONTENT_LIMIT = 1000
_FILE_EDIT_TOOLS = frozenset(
    {
        "write_to_file",
        "write_file",
        "replace_file_content",
        "multi_replace_file_content",
        "edit_file",
    }
)


def _summarize_tool_call(tool_name: str, tool_args: dict) -> tuple[str, str | None]:
    """Format a transparent summary of a proposed tool call plus optional full payload attachment.

    Returns `(inline_summary, full_payload_if_truncated)`. Never silently hides tail commands
    or file-write contents: if inline display must be shortened, returns the full payload
    so the caller can attach it as a document before prompting.
    """
    if not isinstance(tool_args, dict):
        return f"Tool: {tool_name}", None

    full_json = json.dumps(tool_args, indent=2, ensure_ascii=False)

    if tool_name == "run_command":
        cmd = str(tool_args.get("CommandLine") or "").strip()
        cwd = str(tool_args.get("Cwd") or "").strip()
        if not cmd:
            if len(full_json) <= _INLINE_CMD_LIMIT:
                return f"Tool: {tool_name}\nArgs:\n{full_json}", None
            return (
                f"Tool: {tool_name}\nArgs (truncated — full payload attached):\n"
                f"{full_json[:_INLINE_CMD_LIMIT]}...",
                full_json,
            )
        lines = [f"Tool: {tool_name}"]
        if cwd:
            lines.append(f"Cwd: {cwd}")
        if len(cmd) <= _INLINE_CMD_LIMIT:
            lines.append(f"Command:\n{cmd}")
            return "\n".join(lines), None
        lines.append(
            f"Command (truncated {len(cmd):,} chars — full command attached):\n"
            f"{cmd[:_INLINE_CMD_LIMIT]}..."
        )
        return "\n".join(lines), f"Cwd: {cwd or '(default)'}\n\n{cmd}\n"

    if tool_name in _FILE_EDIT_TOOLS:
        target = str(
            tool_args.get("TargetFile")
            or tool_args.get("Path")
            or tool_args.get("AbsolutePath")
            or ""
        ).strip()
        lines = [f"Tool: {tool_name}"]
        if target:
            lines.append(f"File: {target}")
        content_parts: list[str] = []
        for label, key in (
            ("Content", "CodeContent"),
            ("Content", "Content"),
            ("Target", "TargetContent"),
            ("Target", "Target"),
            ("Replacement", "ReplacementContent"),
            ("Replacement", "Replacement"),
        ):
            val = tool_args.get(key)
            if val is not None and str(val) != "":
                content_parts.append(f"{label}:\n{val}")
        if "ReplacementChunks" in tool_args:
            content_parts.append(
                "Chunks:\n"
                + json.dumps(tool_args["ReplacementChunks"], indent=2, ensure_ascii=False)
            )
        body = "\n\n".join(content_parts) if content_parts else full_json
        if len(body) <= _INLINE_CONTENT_LIMIT:
            lines.append(body)
            return "\n".join(lines), None
        lines.append(
            f"Preview (truncated {len(body):,} chars — full payload attached):\n"
            f"{body[:_INLINE_CONTENT_LIMIT]}..."
        )
        return "\n".join(lines), f"Tool: {tool_name}\nFile: {target}\n\n{body}\n"

    for key in ("Url", "TargetFile", "Path", "AbsolutePath"):
        val = tool_args.get(key)
        if val:
            s = str(val).strip()
            if len(s) <= _INLINE_CONTENT_LIMIT and len(full_json) <= _INLINE_CMD_LIMIT:
                return f"Tool: {tool_name}\n{key}: {s}", None
            return (
                f"Tool: {tool_name}\n{key}: {s[:400]}\n(Full args attached)",
                full_json,
            )

    if len(full_json) <= _INLINE_CONTENT_LIMIT:
        return f"Tool: {tool_name}\nArgs: {full_json}", None
    return (
        f"Tool: {tool_name}\nArgs (truncated {len(full_json):,} chars — full payload attached):\n"
        f"{full_json[:_INLINE_CONTENT_LIMIT]}...",
        full_json,
    )


def compute_tool_digest(tool_name: str, tool_args: dict) -> str:
    canonical = json.dumps(
        {"name": tool_name, "args": tool_args}, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass
class PendingApproval:
    chat_id: str
    summary: str
    expires_at: float
    message_id: int | None = None
    decision: str = "deny"
    reason: str = "Approval timed out (fail-closed)."
    resolved: bool = False
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
        self._closed = False

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
        summary, full_payload = _summarize_tool_call(tool_name, tool_args)
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
            chat_id=str(chat_id),
            summary=summary,
            expires_at=time.monotonic() + self.timeout_seconds,
        )

        with self._lock:
            if self._closed:
                return {
                    "decision": "deny",
                    "reason": "Bridge is shutting down (fail-closed).",
                }
            self._pending[nonce] = pending

        try:
            # When the inline summary was shortened, attach the full payload BEFORE prompting
            if full_payload is not None:
                self.telegram.send_document(
                    chat_id,
                    filename=f"approval-{tool_name}-{digest}.txt",
                    content=full_payload.encode("utf-8"),
                    caption=f"Full payload for {tool_name} [id:{digest}]",
                    thread_id=thread_id,
                )

            res = self.telegram.send_message(
                chat_id,
                prompt_text,
                thread_id=thread_id,
                reply_markup=reply_markup,
            )
            msg_obj = res.get("result") if isinstance(res, dict) else None
            if isinstance(msg_obj, dict) and msg_obj.get("message_id") is not None:
                with self._lock:
                    pending.message_id = int(msg_obj["message_id"])
        except Exception as exc:
            log.exception("Failed sending Telegram approval prompt; denying fail-closed")
            with self._lock:
                self._pending.pop(nonce, None)
                pending.resolved = True
            return {
                "decision": "deny",
                "reason": f"Could not deliver Telegram approval prompt: {exc}",
            }

        pending.event.wait(timeout=self.timeout_seconds)

        timed_out_here = False
        msg_id: int | None = None
        with self._lock:
            if not pending.resolved:
                self._pending.pop(nonce, None)
                pending.resolved = True
                pending.decision = "deny"
                pending.reason = (
                    f"User did not approve within {timeout_label} (fail-closed)."
                )
                timed_out_here = True
            decision = pending.decision
            reason = pending.reason
            msg_id = pending.message_id

        if timed_out_here and msg_id is not None:
            try:
                self.telegram.edit_message_text(
                    pending.chat_id,
                    msg_id,
                    f"Timed out — auto-denied ({timeout_label}):\n\n{summary}",
                )
            except Exception:
                pass

        out = {"decision": decision}
        if reason:
            out["reason"] = reason
        return out

    def resolve_callback(
        self,
        *,
        chat_id: str,
        callback_query_id: str,
        callback_data: str,
        message_id: int | None = None,
    ) -> bool:
        """Consume a single-use approval callback and wake the waiting hook."""
        if ":" not in callback_data:
            return False
        action, nonce = callback_data.split(":", 1)
        if action not in ("ap", "dn") or not nonce:
            return False

        edit_chat_id = ""
        edit_msg_id: int | None = None
        edit_text = ""
        toast = ""
        resolved_ok = False

        with self._lock:
            pending = self._pending.get(nonce)
            if pending is None or pending.resolved:
                toast = "Approval already resolved or expired."
            elif str(chat_id) != pending.chat_id:
                # Validate chat_id BEFORE popping so another chat can never disturb the owner's entry
                toast = "Chat mismatch."
            elif time.monotonic() >= pending.expires_at:
                self._pending.pop(nonce, None)
                pending.resolved = True
                pending.decision = "deny"
                pending.reason = "Approval expired (fail-closed)."
                pending.event.set()
                toast = "Approval expired (denied)."
            else:
                self._pending.pop(nonce, None)
                pending.resolved = True
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
                edit_chat_id = pending.chat_id
                edit_msg_id = pending.message_id if pending.message_id is not None else message_id
                edit_text = f"{status_label}:\n\n{pending.summary}"
                resolved_ok = True
                pending.event.set()

        self._safe_answer(callback_query_id, toast)

        if resolved_ok and edit_msg_id is not None:
            try:
                self.telegram.edit_message_text(edit_chat_id, edit_msg_id, edit_text)
            except Exception:
                log.debug("Could not edit approval message %s", edit_msg_id)
        return resolved_ok

    def cancel_all(self, reason: str = "Bridge shutting down (fail-closed).") -> None:
        """Close the manager and fail-close every pending approval on daemon shutdown."""
        with self._lock:
            self._closed = True
            items = list(self._pending.values())
            self._pending.clear()
            for pending in items:
                if not pending.resolved:
                    pending.resolved = True
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
