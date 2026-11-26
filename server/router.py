# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Core router, debouncer, failure recovery, and SSH transcript mirror."""

from __future__ import annotations

import logging
import platform
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .agy_runner import AgyResult, AgyRunner, format_channel_envelope
from .approvals import ApprovalManager
from .attachments import (
    PendingMedia,
    StagedAttachment,
    extract_telegram_media,
    prune_inbox,
    stage_telegram_media,
)
from .config import VALID_MIRROR_MODES, BridgeConfig
from .db import Binding, BridgeStore
from .failure_analyzer import (
    analyze_turn_failure,
    build_recovery_prompt,
    format_fallback_report,
)
from .telegram_client import FileTooLargeError, TelegramClient
from .transcript import (
    get_latest_step_index,
    is_safe_conversation_id,
    read_steps_after,
    resolve_transcript_file,
    strip_bridge_header,
)

log = logging.getLogger("agy-bridge.router")

SSH_BUSY_TTL_SECONDS = 300.0
RECOVERY_TIMEOUT_SECONDS = 45
INBOX_PRUNE_INTERVAL_SECONDS = 6 * 3600.0


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
    pending_media: list[PendingMedia] = field(default_factory=list)


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
        self.approvals = ApprovalManager(
            telegram,
            timeout_seconds=config.approval_timeout_seconds,
        )

        # Locks keyed by normalized endpoint (channel, chat_id, thread_id) so
        # /reset cannot split two threads across different lock objects.
        self._endpoint_locks_guard = threading.Lock()
        self._endpoint_locks: dict[tuple[str, str, int], threading.Lock] = {}

        # Track active bridge runs and external SSH runs per agy conversation_id
        self._state_guard = threading.Lock()
        self._bridge_in_flight: set[str] = set()
        self._ssh_busy_until: dict[str, float] = {}

        # Debounce buffers keyed by (channel, chat_id, thread_id) with generation IDs
        self._debounce_guard = threading.Lock()
        self._pending_batches: dict[tuple[str, str, int], list[InboundMessage]] = {}
        self._debounce_timers: dict[tuple[str, str, int], tuple[int, threading.Timer]] = {}
        self._debounce_seq = 0

        # Track worker threads for graceful shutdown draining
        self._workers_guard = threading.Lock()
        self._workers: set[threading.Thread] = set()
        self._shutting_down = threading.Event()
        self._last_inbox_prune_mono = 0.0

    def _get_endpoint_lock(
        self, channel: str, chat_id: str, thread_id: int
    ) -> threading.Lock:
        key = (channel, str(chat_id), int(thread_id))
        with self._endpoint_locks_guard:
            lock = self._endpoint_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._endpoint_locks[key] = lock
            return lock

    def _spawn_worker(self, target, args: tuple = ()) -> threading.Thread:
        def _wrapped() -> None:
            try:
                target(*args)
            except Exception:
                log.exception("Unhandled error in bridge worker thread")
            finally:
                with self._workers_guard:
                    self._workers.discard(threading.current_thread())

        t = threading.Thread(target=_wrapped, daemon=False)
        with self._workers_guard:
            self._workers.add(t)
        t.start()
        return t

    def begin_shutdown(self) -> None:
        """Immediately mark shutting down and fail-close any waiting tool approvals."""
        self._shutting_down.set()
        self.approvals.cancel_all()

    def start_startup_recovery(self, *, unclean_exit: bool) -> threading.Thread:
        """Run startup transcript reconciliation asynchronously so READY=1 is never blocked."""
        return self._spawn_worker(self._run_startup_recovery, (unclean_exit,))

    def _run_startup_recovery(self, unclean_exit: bool) -> None:
        try:
            self.recover_after_startup(unclean_exit=unclean_exit)
        except Exception:
            log.exception("Startup recovery failed; continuing daemon operation")

    def shutdown(self, timeout_seconds: float = 35.0) -> None:
        """Fail-close pending approvals, cancel timers, flush debounce batches, and join workers."""
        self.begin_shutdown()
        pending_to_flush: list[list[InboundMessage]] = []
        with self._debounce_guard:
            for _gen, timer in self._debounce_timers.values():
                timer.cancel()
            self._debounce_timers.clear()
            for batch in self._pending_batches.values():
                if batch:
                    pending_to_flush.append(batch)
            self._pending_batches.clear()

        for batch in pending_to_flush:
            self._spawn_worker(self._process_batch, (batch,))

        deadline = time.time() + timeout_seconds
        while True:
            with self._workers_guard:
                alive = [t for t in self._workers if t.is_alive()]
            if not alive or time.time() >= deadline:
                break
            for t in alive:
                rem = max(0.05, deadline - time.time())
                t.join(timeout=min(rem, 1.0))

    def is_authorized_telegram(
        self,
        user_id: int | None,
        chat_id: int | None,
        *,
        is_bot: bool = False,
    ) -> bool:
        if is_bot:
            return False
        if not self.config.allowed_user_ids:
            # Always fail closed in live mode when TELEGRAM_ALLOWED_USER_IDS is unset
            return not self.telegram.configured
        if user_id is None or user_id not in self.config.allowed_user_ids:
            return False
        if self.config.allowed_chat_ids:
            if chat_id is None or chat_id not in self.config.allowed_chat_ids:
                return False
        return True

    def handle_telegram_update(self, update: dict) -> None:
        cb = update.get("callback_query")
        if isinstance(cb, dict):
            self._handle_callback_query(cb)
            return

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
            thread_id = int(raw_thread_id) if raw_thread_id else 1

        is_bot = bool(sender.get("is_bot", False))

        # Capture topic creation / rename metadata
        topic_event = msg.get("forum_topic_created") or msg.get("forum_topic_edited")
        if isinstance(topic_event, dict) and topic_event.get("name"):
            if self.is_authorized_telegram(user_id, chat_id, is_bot=is_bot):
                self.store.get_or_create_binding(
                    channel="telegram",
                    chat_id=str(chat_id),
                    thread_id=thread_id,
                    chat_type=chat_type,
                    workspace_dir=str(self.config.alpha_workspace),
                    topic_name=str(topic_event["name"]),
                    default_mirror_mode=self.config.default_mirror_mode,
                )
            return

        text = (msg.get("text") or msg.get("caption") or "").strip()
        pending_media, location_note = extract_telegram_media(msg)
        if location_note:
            text = f"{text}\n{location_note}".strip() if text else location_note
        if not text and not pending_media:
            return

        if not self.is_authorized_telegram(user_id, chat_id, is_bot=is_bot):
            log.warning(
                "Ignored unauthorized Telegram message (user_id=%s chat_id=%s thread_id=%s is_bot=%s)",
                user_id,
                chat_id,
                thread_id,
                is_bot,
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
            pending_media=pending_media,
        )

        if text.startswith("/") and not pending_media:
            self._spawn_worker(self._handle_command, (inbound,))
            return

        self.enqueue_inbound(inbound)

    def _handle_callback_query(self, cb: dict) -> None:
        sender = cb.get("from") or {}
        msg = cb.get("message") or {}
        chat = msg.get("chat") or {}
        user_id = sender.get("id")
        chat_id = chat.get("id")
        is_bot = bool(sender.get("is_bot", False))
        if chat_id is None or user_id is None:
            return
        if not self.is_authorized_telegram(user_id, chat_id, is_bot=is_bot):
            log.warning(
                "Ignored unauthorized Telegram callback_query (user_id=%s chat_id=%s is_bot=%s)",
                user_id,
                chat_id,
                is_bot,
            )
            return

        cb_id = str(cb.get("id") or "")
        cb_data = str(cb.get("data") or "")
        raw_msg_id = msg.get("message_id")
        cb_msg_id = int(raw_msg_id) if raw_msg_id is not None else None
        if not cb_data:
            return
        self._spawn_worker(
            self._resolve_approval_callback,
            (str(chat_id), cb_id, cb_data, cb_msg_id),
        )

    def _resolve_approval_callback(
        self,
        chat_id: str,
        callback_query_id: str,
        callback_data: str,
        message_id: int | None = None,
    ) -> None:
        self.approvals.resolve_callback(
            chat_id=chat_id,
            callback_query_id=callback_query_id,
            callback_data=callback_data,
            message_id=message_id,
        )

    def enqueue_inbound(self, msg: InboundMessage) -> None:
        key = (msg.channel, msg.chat_id, msg.thread_id)
        delay = max(0.0, self.config.debounce_seconds)
        if delay == 0.0:
            self._spawn_worker(self._process_batch, ([msg],))
            return

        with self._debounce_guard:
            self._pending_batches.setdefault(key, []).append(msg)
            prev = self._debounce_timers.pop(key, None)
            if prev is not None:
                prev[1].cancel()
            self._debounce_seq += 1
            gen = self._debounce_seq
            timer = threading.Timer(delay, self._flush_batch, args=(key, gen))
            timer.daemon = True
            self._debounce_timers[key] = (gen, timer)
            timer.start()

    def _cancel_pending_debounce(self, key: tuple[str, str, int]) -> None:
        with self._debounce_guard:
            prev = self._debounce_timers.pop(key, None)
            if prev is not None:
                prev[1].cancel()
            self._pending_batches.pop(key, None)

    def _flush_batch(self, key: tuple[str, str, int], expected_gen: int) -> None:
        with self._debounce_guard:
            entry = self._debounce_timers.get(key)
            if entry is None or entry[0] != expected_gen:
                return
            self._debounce_timers.pop(key, None)
            batch = self._pending_batches.pop(key, [])
        if batch:
            self._spawn_worker(self._process_batch, (batch,))

    def _wait_if_ssh_busy(
        self, conv_id: str | None, chat_id: str, thread_id: int, *, notify: bool = True
    ) -> None:
        """Wait OUTSIDE the endpoint lock so the Stop hook can mirror cleanly."""
        if not conv_id:
            return
        notified = False
        while not self._shutting_down.is_set():
            with self._state_guard:
                until = self._ssh_busy_until.get(conv_id, 0.0)
                if time.time() >= until:
                    self._ssh_busy_until.pop(conv_id, None)
                    return
            if notify and not notified:
                notified = True
                try:
                    self.telegram.send_message(
                        chat_id,
                        "Queued — active SSH session on this conversation. Will run as soon as it finishes.",
                        thread_id=thread_id,
                        disable_notification=True,
                    )
                except Exception:
                    log.exception("Failed sending queued notification")
            time.sleep(1.0)

    def _run_tracked_prompt(
        self,
        prompt: str,
        *,
        conv_id: str | None,
        workspace_dir: Path,
        timeout_seconds: int | None = None,
        binding_id: int | None = None,
    ) -> AgyResult:
        if conv_id:
            with self._state_guard:
                self._bridge_in_flight.add(conv_id)
        try:
            return self.runner.run_prompt(
                prompt,
                conversation_id=conv_id,
                workspace_dir=workspace_dir,
                timeout_seconds=timeout_seconds,
                binding_id=binding_id,
            )
        finally:
            if conv_id:
                with self._state_guard:
                    self._bridge_in_flight.discard(conv_id)

    def _reconcile_external_steps_locked(self, binding: Binding) -> None:
        """Mirror any un-mirrored SSH/offline turns before running a new bridge turn."""
        if binding.agy_conversation_id:
            self._mirror_binding_locked(binding, transcript_path=None)

    def _maybe_prune_inbox(self, workspace_dir: Path, *, force: bool = False) -> int:
        if self.config.inbox_retention_days <= 0:
            return 0
        now_m = time.monotonic()
        with self._state_guard:
            if not force and (now_m - self._last_inbox_prune_mono) < INBOX_PRUNE_INTERVAL_SECONDS:
                return 0
            self._last_inbox_prune_mono = now_m
        try:
            return prune_inbox(
                workspace_dir,
                retention_days=self.config.inbox_retention_days,
            )
        except Exception:
            log.exception("Failed pruning inbox under %s", workspace_dir)
            return 0

    def _stage_batch_media(
        self,
        batch: list[InboundMessage],
        *,
        binding: Binding,
    ) -> tuple[list[StagedAttachment], list[str]]:
        staged: list[StagedAttachment] = []
        errors: list[str] = []
        items = [(m.message_id, pm) for m in batch for pm in m.pending_media]
        if not items:
            return staged, errors

        workspace_dir = Path(binding.workspace_dir)
        self._maybe_prune_inbox(workspace_dir)
        scope_name = binding.agy_conversation_id or f"binding-{binding.id}"

        for mid, pm in items:
            label = pm.file_name or pm.kind
            try:
                rec = stage_telegram_media(
                    self.telegram,
                    pm,
                    workspace_dir=workspace_dir,
                    scope_name=scope_name,
                    message_id=mid,
                    max_bytes=self.config.max_attachment_bytes,
                )
                staged.append(rec)
            except FileTooLargeError as exc:
                log.warning("Rejected oversized Telegram attachment (%s): %s", label, exc)
                errors.append(f"{label}: {exc}")
            except Exception as exc:
                log.exception("Failed staging Telegram attachment (%s)", label)
                errors.append(f"{label}: download failed ({exc})")
        return staged, errors

    def _process_batch(self, batch: list[InboundMessage]) -> None:
        first = batch[0]
        topic_name = next((m.topic_name for m in batch if m.topic_name), "")
        combined_text = "\n\n".join(m.text for m in batch if m.text)
        pre_attachments = [a for m in batch for a in m.attachments]

        # Wait for active SSH session OUTSIDE endpoint lock so Stop hook is never blocked
        pre_binding = self.store.get_active_binding(
            first.channel, first.chat_id, first.thread_id
        )
        if pre_binding and pre_binding.agy_conversation_id:
            self._wait_if_ssh_busy(
                pre_binding.agy_conversation_id, first.chat_id, first.thread_id
            )

        lock = self._get_endpoint_lock(first.channel, first.chat_id, first.thread_id)
        with lock:
            binding = self.store.get_or_create_binding(
                channel=first.channel,
                chat_id=first.chat_id,
                thread_id=first.thread_id,
                chat_type=first.chat_type,
                workspace_dir=str(self.config.alpha_workspace),
                topic_name=topic_name,
                default_mirror_mode=self.config.default_mirror_mode,
            )
            # Reconcile any SSH turns that finished before we acquired the lock
            self._reconcile_external_steps_locked(binding)
            refreshed = self.store.get_active_binding(
                binding.channel, binding.chat_id, binding.thread_id
            )
            if refreshed is None:
                return
            binding = refreshed

            staged_records, stage_errors = self._stage_batch_media(batch, binding=binding)
            if stage_errors:
                notice = "Could not attach:\n" + "\n".join(f"- {e}" for e in stage_errors)
                try:
                    self.telegram.send_message(
                        binding.chat_id,
                        notice,
                        thread_id=binding.thread_id,
                        disable_notification=True,
                    )
                except Exception:
                    log.exception("Failed sending attachment skip notice")

            attachments = pre_attachments + [str(s.path) for s in staged_records]
            if not combined_text and not attachments:
                return

            if not combined_text and attachments:
                if staged_records and all(s.kind in ("voice", "audio") for s in staged_records):
                    combined_text = (
                        "The user sent a voice/audio message without text. "
                        "Inspect the attached audio file and respond directly in the same language."
                    )
                else:
                    combined_text = (
                        "The user sent attachment(s) without a caption. "
                        "Inspect the attached file(s) and respond concisely."
                    )

            self.store.log_message(
                binding.id,
                direction="in",
                origin=first.channel,
                external_message_id=first.message_id,
                content_preview=combined_text,
                attachments=[s.to_log_dict() for s in staged_records] or None,
            )

            envelope = format_channel_envelope(
                channel=first.channel,
                text=combined_text,
                topic_name=binding.topic_name,
                attachments=attachments or None,
                timezone_name=self.config.timezone_name,
            )
            self._execute_and_deliver_locked(
                binding=binding,
                envelope=envelope,
                raw_user_text=combined_text,
                origin="bridge",
                show_typing=True,
            )

    def _execute_and_deliver_locked(
        self,
        *,
        binding: Binding,
        envelope: str,
        raw_user_text: str,
        origin: str,
        show_typing: bool,
    ) -> None:
        stop_typing = threading.Event()
        if show_typing:
            threading.Thread(
                target=self._typing_loop,
                args=(binding.chat_id, binding.thread_id, stop_typing),
                daemon=True,
            ).start()

        try:
            conv_id = binding.agy_conversation_id
            pre_turn_step = binding.last_mirrored_step
            if conv_id:
                tpath_before = resolve_transcript_file(
                    brain_dir=self.config.agy_brain_dir,
                    conversation_id=conv_id,
                )
                if tpath_before is not None:
                    pre_turn_step = max(pre_turn_step, get_latest_step_index(tpath_before))

            try:
                result = self._run_tracked_prompt(
                    envelope,
                    conv_id=conv_id,
                    workspace_dir=Path(binding.workspace_dir),
                    binding_id=binding.id,
                )
            except Exception as exc:
                log.exception("agy runner crashed for binding %s", binding.id)
                result = AgyResult(
                    conversation_id=conv_id or "",
                    status="ERROR",
                    response="",
                    error=f"Runner exception: {exc}",
                )

            final_conv_id = result.conversation_id or conv_id
            if final_conv_id and final_conv_id != binding.agy_conversation_id:
                self.store.set_conversation_id(binding.id, final_conv_id)
                binding.agy_conversation_id = final_conv_id

            if result.status != "SUCCESS" or not result.response:
                reply_text = self._recover_failed_turn(
                    binding=binding,
                    user_message=raw_user_text,
                    result=result,
                    conv_id=final_conv_id,
                    pre_turn_step=pre_turn_step,
                )
                delivery_origin = f"{origin}_recovery"
            else:
                reply_text = result.response
                delivery_origin = origin

            # Deliver BEFORE advancing watermark so transient delivery failures are not silently lost
            self.telegram.send_text_or_markdown_file(
                binding.chat_id,
                reply_text,
                thread_id=binding.thread_id,
            )
            self._advance_watermark(binding.id, final_conv_id)
            self.store.log_message(
                binding.id,
                direction="out",
                origin=delivery_origin,
                content_preview=reply_text,
            )
        finally:
            stop_typing.set()

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
        result: AgyResult,
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

        # Skip Stage-2 agent recovery when shutting down or when no conversation_id exists yet
        if diag.recoverable_via_agent and conv_id and not self._shutting_down.is_set():
            recovery_prompt = build_recovery_prompt(
                user_message, diag, timezone_name=self.config.timezone_name
            )
            try:
                rec = self._run_tracked_prompt(
                    recovery_prompt,
                    conv_id=conv_id,
                    workspace_dir=Path(binding.workspace_dir),
                    timeout_seconds=RECOVERY_TIMEOUT_SECONDS,
                )
                if rec.status == "SUCCESS" and rec.response:
                    tag = f"[{diag.category}"
                    if diag.failed_step_index is not None:
                        tag += f" | step {diag.failed_step_index}"
                    tag += "]"
                    return f"{rec.response}\n\n{tag}"
            except Exception:
                log.exception("Recovery turn failed for binding=%s", binding.id)

        return format_fallback_report(diag, conversation_id=conv_id)

    def _typing_loop(self, chat_id: str, thread_id: int, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            self.telegram.send_typing(chat_id, thread_id)
            stop_event.wait(4.0)

    def _handle_command(self, msg: InboundMessage) -> None:
        parts = msg.text.split()
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1].lower() if len(parts) > 1 else ""

        if cmd == "/id":
            binding = self.store.get_active_binding(msg.channel, msg.chat_id, msg.thread_id)
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
            binding = self.store.get_active_binding(msg.channel, msg.chat_id, msg.thread_id)
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
            lock = self._get_endpoint_lock(msg.channel, msg.chat_id, msg.thread_id)
            with lock:
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
                    reply = (
                        f"Current SSH mirror mode: {binding.mirror_mode}\n"
                        "Usage: /mirror full | replies_only | muted"
                    )
                elif arg not in VALID_MIRROR_MODES:
                    reply = f"Invalid mode '{arg}'. Choose: {', '.join(VALID_MIRROR_MODES)}"
                else:
                    self.store.set_mirror_mode(binding.id, arg)
                    reply = f"SSH mirror mode set to: {arg}"
            self.telegram.send_message(msg.chat_id, reply, thread_id=msg.thread_id)
            return

        if cmd == "/reset":
            key = (msg.channel, msg.chat_id, msg.thread_id)
            self._cancel_pending_debounce(key)
            lock = self._get_endpoint_lock(msg.channel, msg.chat_id, msg.thread_id)
            with lock:
                binding = self.store.get_active_binding(msg.channel, msg.chat_id, msg.thread_id)
                if binding is not None:
                    old_id = binding.agy_conversation_id or "(none)"
                    self.store.archive_binding(binding.id)
                    reply = f"Archived conversation {old_id}. Your next message starts a fresh conversation."
                else:
                    reply = "No active conversation on this thread yet."
            self.telegram.send_message(msg.chat_id, reply, thread_id=msg.thread_id)
            return

        if cmd == "/compact":
            pre_binding = self.store.get_active_binding(msg.channel, msg.chat_id, msg.thread_id)
            if pre_binding and pre_binding.agy_conversation_id:
                self._wait_if_ssh_busy(
                    pre_binding.agy_conversation_id, msg.chat_id, msg.thread_id
                )
            lock = self._get_endpoint_lock(msg.channel, msg.chat_id, msg.thread_id)
            with lock:
                binding = self.store.get_active_binding(msg.channel, msg.chat_id, msg.thread_id)
                if binding is None or not binding.agy_conversation_id:
                    self.telegram.send_message(
                        msg.chat_id,
                        "No active conversation to compact yet.",
                        thread_id=msg.thread_id,
                    )
                    return
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

        self.enqueue_inbound(msg)

    # ------------------------------------------------------------------
    # Hook, Approval & Watcher IPC handlers (called by authenticated localhost IPC)
    # ------------------------------------------------------------------

    def handle_approval_request(self, payload: dict) -> dict:
        """Prompt the user on Telegram with Approve/Deny buttons (fail-closed, lock-free)."""
        if self._shutting_down.is_set():
            return {"decision": "deny", "reason": "Bridge is shutting down (fail-closed)."}

        tool_call = payload.get("toolCall")
        if not isinstance(tool_call, dict):
            return {"decision": "deny", "reason": "Invalid toolCall payload (fail-closed)."}

        tool_name = str(tool_call.get("name") or "").strip()
        if not tool_name:
            return {"decision": "deny", "reason": "Missing tool name (fail-closed)."}
        raw_args = tool_call.get("args")
        if raw_args is None:
            tool_args: dict = {}
        elif isinstance(raw_args, dict):
            tool_args = raw_args
        else:
            return {"decision": "deny", "reason": "Invalid tool args (fail-closed)."}

        conv_id = str(payload.get("conversationId") or "").strip()
        raw_binding_id = str(payload.get("bindingId") or "").strip()

        binding: Binding | None = None
        if raw_binding_id.isdigit():
            binding = self.store.get_binding_by_id(int(raw_binding_id))
            if (
                binding is not None
                and binding.agy_conversation_id
                and conv_id
                and binding.agy_conversation_id != conv_id
            ):
                log.warning(
                    "Approval bindingId=%s conversation mismatch (%s != %s); denying fail-closed",
                    binding.id,
                    binding.agy_conversation_id,
                    conv_id,
                )
                return {
                    "decision": "deny",
                    "reason": "Binding/conversation mismatch (fail-closed).",
                }

        if binding is None and conv_id and is_safe_conversation_id(conv_id):
            binding = self.store.get_binding_by_conversation(conv_id)

        if binding is None:
            log.warning(
                "Approval request for tool=%s had no active binding (bindingId=%s conv=%s); denying",
                tool_name,
                raw_binding_id or "-",
                conv_id or "-",
            )
            return {
                "decision": "deny",
                "reason": "No active Telegram binding for approval request (fail-closed).",
            }

        log.info(
            "Requesting Telegram approval (binding=%s chat=%s thread=%s tool=%s)",
            binding.id,
            binding.chat_id,
            binding.thread_id,
            tool_name,
        )
        res = self.approvals.request_approval(
            chat_id=binding.chat_id,
            thread_id=binding.thread_id,
            tool_name=tool_name,
            tool_args=tool_args,
        )
        log.info(
            "Telegram approval resolved (binding=%s tool=%s decision=%s)",
            binding.id,
            tool_name,
            res.get("decision"),
        )
        return res

    def handle_hook_event(self, payload: dict, *, sync_mirror: bool = False) -> dict:
        event = str(payload.get("event") or "")
        conv_id = str(payload.get("conversationId") or "").strip()
        origin = str(payload.get("origin") or "ssh")
        if not conv_id or not is_safe_conversation_id(conv_id):
            return {"status": "ignored", "reason": "invalid_conversation_id"}

        if origin == "bridge":
            return {"status": "ignored", "reason": "bridge_origin"}
        with self._state_guard:
            if conv_id in self._bridge_in_flight:
                return {"status": "ignored", "reason": "bridge_in_flight"}

        if event == "PreInvocation":
            # Only track busy state for conversations bound to an active channel
            if self.store.get_binding_by_conversation(conv_id) is None:
                return {"status": "ignored", "reason": "unbound_conversation"}
            with self._state_guard:
                self._ssh_busy_until[conv_id] = time.time() + SSH_BUSY_TTL_SECONDS
            return {"status": "marked_busy"}

        if event == "Stop":
            if not payload.get("fullyIdle", True):
                return {"status": "ignored", "reason": "not_fully_idle"}
            transcript_raw = str(payload.get("transcriptPath") or "").strip()
            tpath = Path(transcript_raw) if transcript_raw else None
            if sync_mirror:
                mirrored = self.mirror_external_turns(conv_id, transcript_path=tpath)
                with self._state_guard:
                    self._ssh_busy_until.pop(conv_id, None)
                return {"status": "synced", "mirrored_messages": mirrored}

            # Mirror asynchronously so Stop hook returns immediately (<50ms) without stalling SSH;
            # keep _ssh_busy_until set until mirroring completes so queued Telegram turns don't jump ahead.
            def _bg_mirror() -> None:
                try:
                    self.mirror_external_turns(conv_id, transcript_path=tpath)
                finally:
                    with self._state_guard:
                        self._ssh_busy_until.pop(conv_id, None)

            self._spawn_worker(_bg_mirror)
            return {"status": "queued_mirror"}

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

        lock = self._get_endpoint_lock(binding.channel, binding.chat_id, binding.thread_id)
        with lock:
            current = self.store.get_binding_by_conversation(conversation_id)
            if current is None:
                return 0
            return self._mirror_binding_locked(current, transcript_path=transcript_path)

    def _mirror_binding_locked(
        self,
        binding: Binding,
        *,
        transcript_path: Path | None,
    ) -> int:
        if not binding.agy_conversation_id:
            return 0
        resolved = resolve_transcript_file(
            transcript_path=transcript_path,
            brain_dir=self.config.agy_brain_dir,
            conversation_id=binding.agy_conversation_id,
        )
        if resolved is None:
            return 0

        new_steps = read_steps_after(resolved, binding.last_mirrored_step)
        if not new_steps:
            return 0

        sent = 0
        pending_user: str | None = None
        last_user_from_bridge = False
        for idx, step in enumerate(new_steps):
            if step.step_type == "USER_INPUT":
                last_user_from_bridge = step.text.lstrip().startswith("[channel:")
                pending_user = None if last_user_from_bridge else strip_bridge_header(step.text)
            elif step.step_type == "PLANNER_RESPONSE":
                next_is_planner = (
                    idx + 1 < len(new_steps)
                    and new_steps[idx + 1].step_type == "PLANNER_RESPONSE"
                )
                if next_is_planner:
                    continue

                # /mirror muted suppresses SSH mirroring only; stranded bridge/Telegram replies still recover
                if not last_user_from_bridge and binding.mirror_mode == "muted":
                    self.store.update_watermark(binding.id, step.step_index)
                    pending_user = None
                    last_user_from_bridge = False
                    continue

                if not last_user_from_bridge and binding.mirror_mode == "full" and pending_user:
                    snippet = pending_user.replace("\n", " ").strip()
                    cap = max(20, self.config.ssh_prompt_preview_chars)
                    if len(snippet) > cap:
                        snippet = snippet[: cap - 3].rstrip() + "..."
                    self.telegram.send_message(
                        binding.chat_id,
                        f"[SSH] {snippet}",
                        thread_id=binding.thread_id,
                        disable_notification=True,
                    )
                    sent += 1

                prefix = (
                    "[SSH] "
                    if (not last_user_from_bridge and binding.mirror_mode == "replies_only")
                    else ""
                )
                self.telegram.send_text_or_markdown_file(
                    binding.chat_id,
                    step.text,
                    thread_id=binding.thread_id,
                    disable_notification=True,
                    prefix=prefix,
                    filename_prefix="alpha-ssh-reply",
                )
                # Advance watermark after each delivered turn so partial failures don't duplicate earlier turns
                self.store.update_watermark(binding.id, step.step_index)
                self.store.log_message(
                    binding.id,
                    direction="mirror",
                    origin="startup_recovery" if last_user_from_bridge else "ssh",
                    agy_step_index=step.step_index,
                    content_preview=step.text,
                )
                sent += 1
                pending_user = None
                last_user_from_bridge = False

        return sent

    def recover_after_startup(self, *, unclean_exit: bool) -> int:
        """Reconcile finished-but-undelivered transcript steps, prune expired inbox files, and alert on unclean restart."""
        self._maybe_prune_inbox(self.config.alpha_workspace, force=True)
        reconciled = 0
        for binding in self.store.list_active_bindings():
            if not binding.agy_conversation_id:
                continue
            try:
                reconciled += self.mirror_external_turns(binding.agy_conversation_id)
            except Exception:
                log.exception(
                    "Startup transcript reconciliation failed for binding=%s", binding.id
                )

        if not unclean_exit:
            return reconciled
        log.warning(
            "Detected previous unclean shutdown (reconciled_messages=%d)", reconciled
        )
        if not self.telegram.configured:
            return reconciled
        primary = self.store.get_primary_dm_binding("telegram")
        if primary is None:
            return reconciled
        if reconciled > 0:
            notice = (
                f"[System] Bridge recovered from an unexpected restart and "
                f"delivered {reconciled} pending message(s)."
            )
        else:
            notice = (
                "[System] Bridge recovered from an unexpected restart. "
                "If a message was in flight when it exited, please re-send it."
            )
        try:
            self.telegram.send_message(
                primary.chat_id,
                notice,
                thread_id=primary.thread_id,
                disable_notification=True,
            )
        except Exception:
            log.exception("Failed sending unclean-restart notice to primary DM")
        return reconciled

    def handle_watcher_event(self, payload: dict) -> dict:
        """Route a proactive watcher event through Alpha in the primary DM binding."""
        source = str(payload.get("source") or "watcher").strip()
        message = str(payload.get("message") or "").strip()
        if not message:
            return {"status": "error", "error": "empty_message"}

        binding = self.store.get_primary_dm_binding("telegram")
        if binding is None:
            return {"status": "error", "error": "no_primary_dm_binding"}

        self._spawn_worker(
            self._process_watcher_for_binding,
            (binding, source, f"[source: {source}] {message}"),
        )
        return {"status": "queued", "binding_id": binding.id}

    def _process_watcher_for_binding(
        self, binding: Binding, source: str, text: str
    ) -> None:
        if binding.agy_conversation_id:
            self._wait_if_ssh_busy(
                binding.agy_conversation_id, binding.chat_id, binding.thread_id, notify=False
            )

        lock = self._get_endpoint_lock(binding.channel, binding.chat_id, binding.thread_id)
        with lock:
            current = self.store.get_active_binding(
                binding.channel, binding.chat_id, binding.thread_id
            )
            if current is None:
                return
            self._reconcile_external_steps_locked(current)
            envelope = format_channel_envelope(
                channel="watcher",
                text=text,
                sender=source,
                timezone_name=self.config.timezone_name,
            )
            self._execute_and_deliver_locked(
                binding=current,
                envelope=envelope,
                raw_user_text=text,
                origin="watcher",
                show_typing=False,
            )
