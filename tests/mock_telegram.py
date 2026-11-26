# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Local stdlib HTTP mock of the Telegram Bot API with fault injection and auto-click."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from email.parser import BytesParser
from email.policy import default as email_default_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable


def _parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """Parse multipart/form-data using stdlib email.parser."""
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    msg = BytesParser(policy=email_default_policy).parsebytes(header + body)
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    if not msg.is_multipart():
        return fields, files
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename is not None:
            files[name] = (filename, payload)
        else:
            fields[name] = payload.decode("utf-8", errors="replace")
    return fields, files


class _ThreadedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class MockTelegramServer:
    """In-process mock Telegram Bot API server for full-daemon E2E, chaos, and stress tests."""

    def __init__(self, expected_token: str = "99999:mock-token") -> None:
        self.expected_token = expected_token
        self._lock = threading.Lock()
        self._update_cond = threading.Condition(self._lock)
        self._record_cond = threading.Condition(self._lock)
        self._closed = False

        self._next_update_id = 1000
        self._next_message_id = 1
        self._updates: list[dict] = []
        self._faults: dict[str, deque[dict]] = {}

        self.sent_messages: list[dict] = []
        self.sent_documents: list[dict] = []
        self.edited_messages: list[dict] = []
        self.answered_callbacks: list[dict] = []
        self.chat_actions: list[dict] = []
        self.registered_commands: list[dict] = []
        self.get_updates_calls: list[dict] = []
        self.faults_triggered: list[dict] = []

        # Policy function: (msg_record) -> ("approve" | "deny" | "ignore", delay_seconds, user_id)
        self._approval_policy: Callable[[dict], tuple[str, float, int]] | None = None

        mock_self = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args) -> None:
                return

            def _send_json(self, code: int, obj: dict) -> None:
                raw = json.dumps(obj).encode("utf-8")
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            def do_POST(self) -> None:
                prefix = f"/bot{mock_self.expected_token}/"
                if not self.path.startswith(prefix):
                    self._send_json(401, {"ok": False, "description": "Unauthorized"})
                    return
                method = self.path[len(prefix) :].split("?", 1)[0]
                length = int(self.headers.get("Content-Length") or "0")
                raw_body = self.rfile.read(length) if length > 0 else b""
                ctype = self.headers.get("Content-Type") or ""

                # Check programmable fault injection queue for this method
                fault = mock_self._pop_fault(method)
                if fault is not None:
                    with mock_self._lock:
                        mock_self.faults_triggered.append({"method": method, "fault": fault})
                    if fault.get("drop"):
                        self.close_connection = True
                        try:
                            self.connection.close()
                        except OSError:
                            pass
                        return
                    status = int(fault.get("status", 500))
                    body: dict = {"ok": False, "error_code": status, "description": f"Injected {status}"}
                    if status == 429:
                        body["parameters"] = {"retry_after": int(fault.get("retry_after", 1))}
                    self._send_json(status, body)
                    return

                if method == "getUpdates":
                    payload = json.loads(raw_body.decode("utf-8") or "{}")
                    offset = int(payload.get("offset") or 0)
                    poll_timeout = min(float(payload.get("timeout") or 1), 1.0)
                    with mock_self._lock:
                        mock_self.get_updates_calls.append({"offset": offset, "ts": time.time()})
                    results = mock_self._poll_updates(offset, poll_timeout)
                    self._send_json(200, {"ok": True, "result": results})
                    return

                if method == "sendMessage":
                    payload = json.loads(raw_body.decode("utf-8") or "{}")
                    rec = mock_self._record_send_message(payload)
                    self._send_json(200, {"ok": True, "result": {"message_id": rec["message_id"]}})
                    return

                if method == "sendDocument":
                    fields, files = _parse_multipart(ctype, raw_body)
                    rec = mock_self._record_send_document(fields, files)
                    self._send_json(200, {"ok": True, "result": {"message_id": rec["message_id"]}})
                    return

                if method == "editMessageText":
                    payload = json.loads(raw_body.decode("utf-8") or "{}")
                    with mock_self._record_cond:
                        mock_self.edited_messages.append(payload)
                        mock_self._record_cond.notify_all()
                    self._send_json(200, {"ok": True, "result": {"message_id": payload.get("message_id")}})
                    return

                if method == "answerCallbackQuery":
                    payload = json.loads(raw_body.decode("utf-8") or "{}")
                    with mock_self._record_cond:
                        mock_self.answered_callbacks.append(payload)
                        mock_self._record_cond.notify_all()
                    self._send_json(200, {"ok": True, "result": True})
                    return

                if method == "sendChatAction":
                    payload = json.loads(raw_body.decode("utf-8") or "{}")
                    with mock_self._record_cond:
                        mock_self.chat_actions.append(payload)
                        mock_self._record_cond.notify_all()
                    self._send_json(200, {"ok": True, "result": True})
                    return

                if method == "setMyCommands":
                    payload = json.loads(raw_body.decode("utf-8") or "{}")
                    with mock_self._record_cond:
                        mock_self.registered_commands = list(payload.get("commands") or [])
                        mock_self._record_cond.notify_all()
                    self._send_json(200, {"ok": True, "result": True})
                    return

                self._send_json(404, {"ok": False, "description": f"Unknown method {method}"})

        self._server = _ThreadedHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        with self._update_cond:
            self._closed = True
            self._update_cond.notify_all()
        self._server.shutdown()
        self._server.server_close()

    def set_approval_policy(
        self, policy: Callable[[dict], tuple[str, float, int]] | None
    ) -> None:
        with self._lock:
            self._approval_policy = policy

    def inject_faults(self, method: str, faults: list[dict]) -> None:
        with self._lock:
            q = self._faults.setdefault(method, deque())
            for f in faults:
                q.append(dict(f))

    def _pop_fault(self, method: str) -> dict | None:
        with self._lock:
            q = self._faults.get(method)
            if q:
                return q.popleft()
            return None

    def _poll_updates(self, offset: int, timeout: float) -> list[dict]:
        deadline = time.time() + max(0.05, timeout)
        with self._update_cond:
            while not self._closed:
                ready = [u for u in self._updates if int(u["update_id"]) >= offset]
                if ready:
                    return list(ready)
                rem = deadline - time.time()
                if rem <= 0:
                    return []
                self._update_cond.wait(timeout=min(rem, 0.2))
            return []

    def push_message(
        self,
        *,
        chat_id: int,
        text: str,
        user_id: int = 111,
        chat_type: str = "private",
        thread_id: int = 0,
        topic_name: str = "",
        is_bot: bool = False,
        duplicate_count: int = 1,
    ) -> int:
        with self._update_cond:
            upd_id = self._next_update_id
            self._next_update_id += 1
            msg_id = self._next_message_id
            self._next_message_id += 1
            msg: dict = {
                "message_id": msg_id,
                "from": {"id": user_id, "is_bot": is_bot, "first_name": f"user{user_id}"},
                "chat": {"id": chat_id, "type": chat_type},
                "text": text,
            }
            if thread_id > 0:
                msg["message_thread_id"] = thread_id
            if topic_name:
                msg["reply_to_message"] = {"forum_topic_created": {"name": topic_name}}
            upd = {"update_id": upd_id, "message": msg}
            for _ in range(max(1, duplicate_count)):
                self._updates.append(upd)
            self._update_cond.notify_all()
            return upd_id

    def push_callback_query(
        self,
        *,
        chat_id: int,
        data: str,
        user_id: int = 111,
        message_id: int = 1,
        is_bot: bool = False,
    ) -> int:
        with self._update_cond:
            upd_id = self._next_update_id
            self._next_update_id += 1
            upd = {
                "update_id": upd_id,
                "callback_query": {
                    "id": f"cbq-{upd_id}",
                    "from": {"id": user_id, "is_bot": is_bot, "first_name": f"user{user_id}"},
                    "message": {"chat": {"id": chat_id}, "message_id": message_id},
                    "data": data,
                },
            }
            self._updates.append(upd)
            self._update_cond.notify_all()
            return upd_id

    def _record_send_message(self, payload: dict) -> dict:
        with self._record_cond:
            msg_id = self._next_message_id
            self._next_message_id += 1
            rec = {
                "message_id": msg_id,
                "chat_id": int(payload["chat_id"]),
                "thread_id": int(payload.get("message_thread_id") or 0),
                "text": str(payload.get("text") or ""),
                "parse_mode": payload.get("parse_mode"),
                "disable_notification": bool(payload.get("disable_notification", False)),
                "reply_markup": payload.get("reply_markup"),
                "ts": time.time(),
            }
            self.sent_messages.append(rec)
            policy = self._approval_policy
            self._record_cond.notify_all()

        # Auto-click inline Approve/Deny buttons if a policy is registered
        if policy is not None and isinstance(rec.get("reply_markup"), dict):
            kb = rec["reply_markup"].get("inline_keyboard") or []
            if kb and isinstance(kb[0], list) and len(kb[0]) >= 2:
                ap_data = str(kb[0][0].get("callback_data") or "")
                dn_data = str(kb[0][1].get("callback_data") or "")
                if ap_data.startswith("ap:") and dn_data.startswith("dn:"):
                    action, delay, uid = policy(rec)
                    if action in ("approve", "deny"):
                        chosen = ap_data if action == "approve" else dn_data

                        def _click() -> None:
                            if delay > 0:
                                time.sleep(delay)
                            self.push_callback_query(
                                chat_id=rec["chat_id"],
                                data=chosen,
                                user_id=uid,
                                message_id=rec["message_id"],
                            )

                        threading.Thread(target=_click, daemon=True).start()
        return rec

    def _record_send_document(
        self, fields: dict[str, str], files: dict[str, tuple[str, bytes]]
    ) -> dict:
        with self._record_cond:
            msg_id = self._next_message_id
            self._next_message_id += 1
            filename, content = files.get("document", ("", b""))
            rec = {
                "message_id": msg_id,
                "chat_id": int(fields.get("chat_id") or 0),
                "thread_id": int(fields.get("message_thread_id") or 0),
                "caption": fields.get("caption", ""),
                "disable_notification": fields.get("disable_notification") == "true",
                "filename": filename,
                "content": content.decode("utf-8", errors="replace"),
                "ts": time.time(),
            }
            self.sent_documents.append(rec)
            self._record_cond.notify_all()
            return rec

    def wait_for(self, predicate: Callable[[], bool], timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        with self._record_cond:
            while not predicate():
                rem = deadline - time.time()
                if rem <= 0:
                    return False
                self._record_cond.wait(timeout=min(rem, 0.2))
            return True
