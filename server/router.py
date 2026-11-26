"""Core channel-agnostic router, debouncer, and SSH transcript mirror."""

from __future__ import annotations

import logging
import platform
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .agy_runner import AgyRunner, format_channel_envelope
from .config import BridgeConfig
from .db import VALID_MIRROR_MODES, Binding, BridgeStore
from .failure_analyzer import (
    analyze_turn_failure,
    build_recovery_prompt,
    format_fallback_report,
)
from .telegram_client import TelegramClient
from .transcript import (
    get_latest_step_index,
    read_steps_after,
    resolve_transcript_file,
    strip_bridge_header,
)

log = logging.getLogger("agy-bridge.router")

SSH_BUSY_TTL_SECONDS = 300.0


@dataclass
class InboundMessage:
    channel: str
    chat_id: str
    thread_id: int
    chat_type: str
    user_id: int
    sender_name: str
    message_id: str
    text: str
    topic_name: str = ""
    attachments: list[str] = field(default_factory=list)


class BridgeRouter:
    def __init__(
        self,
        config: BridgeConfig,
        store: BridgeStore,
        runner: AgyRunner,
        telegram: TelegramClient,
    ) -> None:
        self.config = config
        self.store = store
        self.runner = runner
        self.telegram = telegram

        # Per-binding lock serializes turns on the same conversation
        self._binding_locks_guard = threading.Lock()
        self._binding_locks: dict[int, threading.Lock] = {}

        # Track active bridge runs and external SSH runs per agy conversation_id
        self._state_guard = threading.Lock()
        self._bridge_in_flight: set[str] = set()
        self._ssh_busy_until: dict[str, float] = {}

        # Debounce buffers keyed by (channel, chat_id, thread_id)
        self._debounce_guard = threading.Lock()
        self._pending_batches: dict[tuple[str, str, int], list[InboundMessage]] = {}
        self._debounce_timers: dict[tuple[str, str, int], threading.Timer] = {}

    def _get_binding_lock(self, binding_id: int) -> threading.Lock:
        with self._binding_locks_guard:
            lock = self._binding_locks.get(binding_id)
            if lock is None:
                lock = threading.Lock()
                self._binding_locks[binding_id] = lock
            return lock

    def is_authorized_telegram(self, user_id: int | None, chat_id: int | None) -> bool:
        if self.config.allowed_user_ids:
            if user_id is None or user_id not in self.config.allowed_user_ids:
                return False
        if self.config.allowed_chat_ids:
            if chat_id is None or chat_id not in self.config.allowed_chat_ids:
                return False
        # Fail closed if neither allowlist is set when running against live Telegram
        if not self.config.allowed_user_ids and not self.config.allowed_chat_ids:
            return not self.telegram.configured
        return True

    def handle_telegram_update(self, update: dict) -> None:
        msg = update.get("message")
        if not isinstance(msg, dict):
            return

        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        chat_id = chat.get("id")
        user_id = sender.get("id")
        if chat_id is None or user_id is None:
            return

        chat_type = str(chat.get("type") or "private")
        raw_thread_id = msg.get("message_thread_id")
        if chat_type == "private":
            thread_id = 0
        else:
            # General topic may omit message_thread_id or send 1
            thread_id = int(raw_thread_id) if raw_thread_id else 1

        # Capture topic creation metadata if present
        topic_created = msg.get("forum_topic_created")
        if isinstance(topic_created, dict) and topic_created.get("name"):
            if self.is_authorized_telegram(user_id, chat_id):
                self.store.get_or_create_binding(
                    channel="telegram",
                    chat_id=str(chat_id),
                    thread_id=thread_id,
                    chat_type=chat_type,
                    workspace_dir=str(self.config.alpha_workspace),
                    topic_name=str(topic_created["name"]),
                    default_mirror_mode=self.config.default_mirror_mode,
                )
            return

        text = (msg.get("text") or msg.get("caption") or "").strip()
        if not text:
            return

        # Allow /id even before allowlists are filled? No — log IDs on server instead for security.
        if not self.is_authorized_telegram(user_id, chat_id):
            log.warning(
                "Ignored unauthorized Telegram message (user_id=%s chat_id=%s thread_id=%s)",
                user_id,
                chat_id,
                thread_id,
            )
            return

        topic_name = ""
        reply_to = msg.get("reply_to_message")
        if isinstance(reply_to, dict):
            ftc = reply_to.get("forum_topic_created")
            if isinstance(ftc, dict) and ftc.get("name"):
                topic_name = str(ftc["name"])

        inbound = InboundMessage(
            channel="telegram",
            chat_id=str(chat_id),
            thread_id=thread_id,
            chat_type=chat_type,
            user_id=int(user_id),
            sender_name=str(sender.get("first_name") or sender.get("username") or "user"),
            message_id=str(msg.get("message_id") or ""),
            text=text,
            topic_name=topic_name,
        )

        # Commands execute immediately without debounce
        if text.startswith("/"):
            threading.Thread(
                target=self._handle_command, args=(inbound,), daemon=True
            ).start()
            return

        self.enqueue_inbound(inbound)

    def enqueue_inbound(self, msg: InboundMessage) -> None:
        key = (msg.channel, msg.chat_id, msg.thread_id)
        delay = max(0.0, self.config.debounce_seconds)
        if delay == 0.0:
            threading.Thread(
                target=self._process_batch, args=([msg],), daemon=True
            ).start()
            return

        with self._debounce_guard:
            self._pending_batches.setdefault(key, []).append(msg)
            old_timer = self._debounce_timers.pop(key, None)
            if old_timer is not None:
                old_timer.cancel()
            timer = threading.Timer(delay, self._flush_batch, args=(key,))
            timer.daemon = True
            self._debounce_timers[key] = timer
            timer.start()

    def _flush_batch(self, key: tuple[str, str, int]) -> None:
        with self._debounce_guard:
            batch = self._pending_batches.pop(key, [])
            self._debounce_timers.pop(key, None)
        if batch:
            self._process_batch(batch)

    def _wait_if_ssh_busy(self, conv_id: str | None, chat_id: str, thread_id: int) -> None:
        if not conv_id:
            return
        notified = False
        while True:
            with self._state_guard:
                until = self._ssh_busy_until.get(conv_id, 0.0)
                if time.time() >= until:
                    self._ssh_busy_until.pop(conv_id, None)
                    return
            if not notified:
                notified = True
                self.telegram.send_message(
                    chat_id,
                    "Queued — active SSH session on this conversation. Will run as soon as it finishes.",
                    thread_id=thread_id,
                    disable_notification=True,
                )
            time.sleep(1.5)

    def _process_batch(self, batch: list[InboundMessage]) -> None:
        first = batch[0]
        topic_name = next((m.topic_name for m in batch if m.topic_name), "")
        binding = self.store.get_or_create_binding(
            channel=first.channel,
            chat_id=first.chat_id,
            thread_id=first.thread_id,
            chat_type=first.chat_type,
            workspace_dir=str(self.config.alpha_workspace),
            topic_name=topic_name,
            default_mirror_mode=self.config.default_mirror_mode,
        )
        combined_text = "\n\n".join(m.text for m in batch)
        attachments = [a for m in batch for a in m.attachments]

        lock = self._get_binding_lock(binding.id)
        with lock:
            # Refresh binding state inside lock
            current = self.store.get_active_binding(
                binding.channel, binding.chat_id, binding.thread_id
            )
            if current is None:
                return
            binding = current
            self._wait_if_ssh_busy(
                binding.agy_conversation_id, binding.chat_id, binding.thread_id
            )

            self.store.log_message(
                binding.id,
                direction="in",
                origin=first.channel,
                external_message_id=first.message_id,
                content_preview=combined_text,
            )

            envelope = format_channel_envelope(
                channel=first.channel,
                text=combined_text,
                topic_name=binding.topic_name,
                attachments=attachments or None,
                tz_label=self.config.timezone_label,
            )

            stop_typing = threading.Event()
            typing_thread = threading.Thread(
                target=self._typing_loop,
                args=(binding.chat_id, binding.thread_id, stop_typing),
                daemon=True,
            )
            typing_thread.start()

            conv_id = binding.agy_conversation_id
            pre_turn_step = binding.last_mirrored_step
            if conv_id:
                tpath_before = resolve_transcript_file(
                    brain_dir=self.config.agy_brain_dir,
                    conversation_id=conv_id,
                )
                if tpath_before is not None:
                    pre_turn_step = max(pre_turn_step, get_latest_step_index(tpath_before))
                with self._state_guard:
                    self._bridge_in_flight.add(conv_id)

            try:
                result = self.runner.run_prompt(
                    envelope,
                    conversation_id=conv_id,
                    workspace_dir=Path(binding.workspace_dir),
                )
            except Exception as exc:
                log.exception("agy runner crashed for binding %s", binding.id)
                self.telegram.send_message(
                    binding.chat_id,
                    f"Agent execution crashed before returning: {exc}",
                    thread_id=binding.thread_id,
                )
                return
            finally:
                stop_typing.set()
                if conv_id:
                    with self._state_guard:
                        self._bridge_in_flight.discard(conv_id)

            final_conv_id = result.conversation_id or conv_id
            if final_conv_id and final_conv_id != binding.agy_conversation_id:
                self.store.set_conversation_id(binding.id, final_conv_id)
                binding.agy_conversation_id = final_conv_id

            if result.status != "SUCCESS" or not result.response:
                reply_text = self._recover_failed_turn(
                    binding=binding,
                    user_message=combined_text,
                    result=result,
                    conv_id=final_conv_id,
                    pre_turn_step=pre_turn_step,
                )
                self._advance_watermark(binding.id, final_conv_id)
                self.telegram.send_text_or_markdown_file(
                    binding.chat_id,
                    reply_text,
                    thread_id=binding.thread_id,
                )
                self.store.log_message(
                    binding.id,
                    direction="out",
                    origin="bridge_recovery",
                    content_preview=reply_text,
                )
                return

            self._advance_watermark(binding.id, final_conv_id)
            self.telegram.send_text_or_markdown_file(
                binding.chat_id,
                result.response,
                thread_id=binding.thread_id,
            )
            self.store.log_message(
                binding.id,
                direction="out",
                origin="bridge",
                content_preview=result.response,
            )

    def _advance_watermark(self, binding_id: int, conv_id: str | None) -> None:
        if not conv_id:
            return
        tpath = resolve_transcript_file(
            brain_dir=self.config.agy_brain_dir,
            conversation_id=conv_id,
        )
        if tpath is not None:
            latest_step = get_latest_step_index(tpath)
            if latest_step >= 0:
                self.store.update_watermark(binding_id, latest_step)

    def _recover_failed_turn(
        self,
        *,
        binding: Binding,
        user_message: str,
        result,
        conv_id: str | None,
        pre_turn_step: int,
    ) -> str:
        """Analyze an abrupt/empty turn and let Alpha explain it once (with deterministic fallback)."""
        diag = analyze_turn_failure(
            result,
            brain_dir=self.config.agy_brain_dir,
            conversation_id=conv_id,
            pre_turn_step_index=pre_turn_step,
        )
        log.warning(
            "Turn failed/empty on binding=%s conv=%s category=%s tools=%s",
            binding.id,
            conv_id,
            diag.category,
            diag.attempted_tools,
        )

        if diag.recoverable_via_agent:
            recovery_prompt = build_recovery_prompt(
                user_message, diag, tz_label=self.config.timezone_label
            )
            if conv_id:
                with self._state_guard:
                    self._bridge_in_flight.add(conv_id)
            try:
                rec = self.runner.run_prompt(
                    recovery_prompt,
                    conversation_id=conv_id,
                    workspace_dir=Path(binding.workspace_dir),
                )
                if rec.status == "SUCCESS" and rec.response:
                    tag = f"[{diag.category}"
                    if diag.failed_step_index is not None:
                        tag += f" | step {diag.failed_step_index}"
                    tag += "]"
                    return f"{rec.response}\n\n{tag}"
            except Exception:
                log.exception("Recovery turn failed for binding=%s", binding.id)
            finally:
                if conv_id:
                    with self._state_guard:
                        self._bridge_in_flight.discard(conv_id)

        return format_fallback_report(diag, conversation_id=conv_id)

    def _typing_loop(self, chat_id: str, thread_id: int, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            self.telegram.send_typing(chat_id, thread_id)
            stop_event.wait(4.0)

    def _handle_command(self, msg: InboundMessage) -> None:
        parts = msg.text.split()
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1].lower() if len(parts) > 1 else ""

        binding = self.store.get_active_binding(msg.channel, msg.chat_id, msg.thread_id)

        if cmd == "/id":
            reply = (
                f"user_id: {msg.user_id}\n"
                f"chat_id: {msg.chat_id}\n"
                f"thread_id: {msg.thread_id} ({msg.chat_type})\n"
                f"agy_conversation_id: {binding.agy_conversation_id if binding else '(none yet)'}"
            )
            self.telegram.send_message(msg.chat_id, reply, thread_id=msg.thread_id)
            return

        if cmd == "/help":
            reply = (
                "Commands:\n"
                "/status — host & conversation status\n"
                "/mirror [full|replies_only|muted] — set SSH mirror mode for this thread\n"
                "/compact — compact this conversation's context\n"
                "/reset — archive this mapping and start a fresh conversation\n"
                "/id — show chat/thread/conversation IDs"
            )
            self.telegram.send_message(msg.chat_id, reply, thread_id=msg.thread_id)
            return

        if cmd == "/status":
            try:
                uptime = subprocess.check_output(["uptime", "-p"], text=True).strip()
                disk = subprocess.check_output(["df", "-h", "/"], text=True).splitlines()[-1]
            except Exception:
                uptime, disk = "unknown", "unknown"
            conv = binding.agy_conversation_id if binding and binding.agy_conversation_id else "(none yet)"
            mode = binding.mirror_mode if binding else self.config.default_mirror_mode
            reply = (
                f"Host: {platform.node()}\n"
                f"Uptime: {uptime}\n"
                f"Disk (/): {disk}\n"
                f"Conversation: {conv}\n"
                f"SSH mirror: {mode}"
            )
            self.telegram.send_message(msg.chat_id, reply, thread_id=msg.thread_id)
            return

        if cmd == "/mirror":
            if binding is None:
                binding = self.store.get_or_create_binding(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    thread_id=msg.thread_id,
                    chat_type=msg.chat_type,
                    workspace_dir=str(self.config.alpha_workspace),
                    topic_name=msg.topic_name,
                    default_mirror_mode=self.config.default_mirror_mode,
                )
            if not arg:
                self.telegram.send_message(
                    msg.chat_id,
                    f"Current SSH mirror mode: {binding.mirror_mode}\nUsage: /mirror full | replies_only | muted",
                    thread_id=msg.thread_id,
                )
                return
            if arg not in VALID_MIRROR_MODES:
                self.telegram.send_message(
                    msg.chat_id,
                    f"Invalid mode '{arg}'. Choose: {', '.join(VALID_MIRROR_MODES)}",
                    thread_id=msg.thread_id,
                )
                return
            self.store.set_mirror_mode(binding.id, arg)
            self.telegram.send_message(
                msg.chat_id,
                f"SSH mirror mode set to: {arg}",
                thread_id=msg.thread_id,
            )
            return

        if cmd == "/reset":
            if binding is not None:
                old_id = binding.agy_conversation_id or "(none)"
                self.store.archive_binding(binding.id)
                self.telegram.send_message(
                    msg.chat_id,
                    f"Archived conversation {old_id}. Your next message starts a fresh conversation.",
                    thread_id=msg.thread_id,
                )
            else:
                self.telegram.send_message(
                    msg.chat_id,
                    "No active conversation on this thread yet.",
                    thread_id=msg.thread_id,
                )
            return

        if cmd == "/compact":
            if binding is None or not binding.agy_conversation_id:
                self.telegram.send_message(
                    msg.chat_id,
                    "No active conversation to compact yet.",
                    thread_id=msg.thread_id,
                )
                return
            lock = self._get_binding_lock(binding.id)
            with lock:
                rc, out = self.runner.run_cli_slash(
                    "/compact",
                    conversation_id=binding.agy_conversation_id,
                    workspace_dir=Path(binding.workspace_dir),
                )
            self.telegram.send_message(
                msg.chat_id,
                f"/compact finished (rc={rc}): {out[:1000] or 'ok'}",
                thread_id=msg.thread_id,
            )
            return

        # Unknown slash command: forward to Alpha as normal text
        self.enqueue_inbound(msg)

    # ------------------------------------------------------------------
    # Hook & Watcher IPC handlers (called by localhost HTTP IPC server)
    # ------------------------------------------------------------------

    def handle_hook_event(self, payload: dict) -> dict:
        event = str(payload.get("event") or "")
        conv_id = str(payload.get("conversationId") or "").strip()
        origin = str(payload.get("origin") or "ssh")
        if not conv_id:
            return {"status": "ignored", "reason": "no_conversation_id"}

        # Ignore hooks fired by our own bridge subprocess
        if origin == "bridge":
            return {"status": "ignored", "reason": "bridge_origin"}
        with self._state_guard:
            if conv_id in self._bridge_in_flight:
                return {"status": "ignored", "reason": "bridge_in_flight"}

        if event == "PreInvocation":
            with self._state_guard:
                self._ssh_busy_until[conv_id] = time.time() + SSH_BUSY_TTL_SECONDS
            return {"status": "marked_busy"}

        if event == "Stop":
            with self._state_guard:
                self._ssh_busy_until.pop(conv_id, None)
            transcript_raw = str(payload.get("transcriptPath") or "").strip()
            tpath = Path(transcript_raw) if transcript_raw else None
            mirrored = self.mirror_external_turns(conv_id, transcript_path=tpath)
            return {"status": "synced", "mirrored_messages": mirrored}

        return {"status": "ignored", "reason": f"unknown_event:{event}"}

    def mirror_external_turns(
        self,
        conversation_id: str,
        *,
        transcript_path: Path | None = None,
    ) -> int:
        """Read new DONE steps from transcript.jsonl and mirror to Telegram."""
        binding = self.store.get_binding_by_conversation(conversation_id)
        if binding is None:
            return 0

        lock = self._get_binding_lock(binding.id)
        with lock:
            # Re-fetch watermark inside lock
            binding = self.store.get_binding_by_conversation(conversation_id)
            if binding is None:
                return 0

            resolved = resolve_transcript_file(
                transcript_path=transcript_path,
                brain_dir=self.config.agy_brain_dir,
                conversation_id=conversation_id,
            )
            if resolved is None:
                return 0

            new_steps = read_steps_after(resolved, binding.last_mirrored_step)
            if not new_steps:
                return 0

            max_step = max(s.step_index for s in new_steps)
            if binding.mirror_mode == "muted":
                self.store.update_watermark(binding.id, max_step)
                return 0

            sent = 0
            # Group steps into turns: each USER_INPUT followed by its final PLANNER_RESPONSE
            pending_user: str | None = None
            for idx, step in enumerate(new_steps):
                if step.step_type == "USER_INPUT":
                    pending_user = strip_bridge_header(step.text)
                elif step.step_type == "PLANNER_RESPONSE":
                    # Only emit the last PLANNER_RESPONSE before the next USER_INPUT or end
                    next_is_planner = (
                        idx + 1 < len(new_steps)
                        and new_steps[idx + 1].step_type == "PLANNER_RESPONSE"
                    )
                    if next_is_planner:
                        continue

                    if binding.mirror_mode == "full" and pending_user:
                        snippet = pending_user.replace("\n", " ").strip()
                        cap = self.config.ssh_prompt_preview_chars
                        if len(snippet) > cap:
                            snippet = snippet[: cap - 3].rstrip() + "..."
                        self.telegram.send_message(
                            binding.chat_id,
                            f"[SSH] {snippet}",
                            thread_id=binding.thread_id,
                            disable_notification=True,
                        )
                        sent += 1

                    prefix = "[SSH] " if binding.mirror_mode == "replies_only" else ""
                    self.telegram.send_text_or_markdown_file(
                        binding.chat_id,
                        step.text,
                        thread_id=binding.thread_id,
                        disable_notification=True,
                        prefix=prefix,
                        filename_prefix="alpha-ssh-reply",
                    )
                    self.store.log_message(
                        binding.id,
                        direction="mirror",
                        origin="ssh",
                        agy_step_index=step.step_index,
                        content_preview=step.text,
                    )
                    sent += 1
                    pending_user = None

            self.store.update_watermark(binding.id, max_step)
            return sent

    def handle_watcher_event(self, payload: dict) -> dict:
        """Route a proactive watcher event through Alpha in the primary DM binding."""
        source = str(payload.get("source") or "watcher").strip()
        message = str(payload.get("message") or "").strip()
        if not message:
            return {"status": "error", "error": "empty_message"}

        binding = self.store.get_primary_dm_binding("telegram")
        if binding is None:
            return {"status": "error", "error": "no_primary_dm_binding"}

        inbound = InboundMessage(
            channel="watcher",
            chat_id=binding.chat_id,
            thread_id=binding.thread_id,
            chat_type=binding.chat_type,
            user_id=0,
            sender_name=source,
            message_id="",
            text=f"[source: {source}] {message}",
        )
        threading.Thread(
            target=self._process_watcher_for_binding,
            args=(binding, inbound),
            daemon=True,
        ).start()
        return {"status": "queued", "binding_id": binding.id}

    def _process_watcher_for_binding(self, binding: Binding, msg: InboundMessage) -> None:
        lock = self._get_binding_lock(binding.id)
        with lock:
            current = self.store.get_active_binding(
                binding.channel, binding.chat_id, binding.thread_id
            )
            if current is None:
                return
            binding = current
            envelope = format_channel_envelope(
                channel="watcher",
                text=msg.text,
                sender=msg.sender_name,
                tz_label=self.config.timezone_label,
            )
            conv_id = binding.agy_conversation_id
            if conv_id:
                with self._state_guard:
                    self._bridge_in_flight.add(conv_id)
            try:
                result = self.runner.run_prompt(
                    envelope,
                    conversation_id=conv_id,
                    workspace_dir=Path(binding.workspace_dir),
                )
            finally:
                if conv_id:
                    with self._state_guard:
                        self._bridge_in_flight.discard(conv_id)

            final_conv_id = result.conversation_id or conv_id
            if final_conv_id:
                if final_conv_id != binding.agy_conversation_id:
                    self.store.set_conversation_id(binding.id, final_conv_id)
                tpath = resolve_transcript_file(
                    brain_dir=self.config.agy_brain_dir,
                    conversation_id=final_conv_id,
                )
                if tpath is not None:
                    latest = get_latest_step_index(tpath)
                    if latest >= 0:
                        self.store.update_watermark(binding.id, latest)

            if result.status == "SUCCESS" and result.response:
                self.telegram.send_text_or_markdown_file(
                    binding.chat_id,
                    result.response,
                    thread_id=binding.thread_id,
                )
                self.store.log_message(
                    binding.id,
                    direction="out",
                    origin="watcher",
                    content_preview=result.response,
                )
