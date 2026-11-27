#!/usr/bin/env python3
# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Main daemon: Telegram long-polling bridge + authenticated localhost IPC server."""

from __future__ import annotations

import hmac
import json
import logging
import os
import signal
import socket
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .agy_runner import AgyRunner
from .config import BridgeConfig, ensure_ipc_token, resolve_env_file
from .db import BridgeStore
from .router import BridgeRouter
from .telegram_client import BOT_COMMANDS, TelegramClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("agy-bridge.main")

MAX_IPC_BODY_BYTES = 1_048_576  # 1 MiB


def sd_notify(state: str) -> bool:
    """Send a systemd sd_notify datagram (READY=1, WATCHDOG=1, STOPPING=1) using stdlib only."""
    addr = os.environ.get("NOTIFY_SOCKET", "").strip()
    if not addr or not hasattr(socket, "AF_UNIX"):
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1.0)
            sock.sendto(state.encode("utf-8"), addr)
        return True
    except OSError as exc:
        log.debug("sd_notify(%s) skipped: %s", state, exc)
        return False


def start_ipc_server(
    config: BridgeConfig,
    router: BridgeRouter,
    ipc_token: str,
    *,
    watcher_token: str | None = None,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            log.debug("IPC " + fmt, *args)

        def _send_json(self, code: int, body: dict) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _is_ipc_authenticated(self) -> bool:
            presented = self.headers.get("X-Bridge-Token", "")
            return bool(presented) and hmac.compare_digest(presented, ipc_token)

        def _is_watcher_authenticated(self) -> bool:
            presented = self.headers.get("X-Bridge-Token", "")
            if not presented:
                return False
            if watcher_token and hmac.compare_digest(presented, watcher_token):
                return True
            return hmac.compare_digest(presented, ipc_token)

        def do_GET(self) -> None:
            if self.path == "/health":
                bindings = router.store.list_active_bindings()
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "telegram_configured": router.telegram.configured,
                        "active_bindings": len(bindings),
                    },
                )
                return
            self._send_json(404, {"error": "not_found"})

        def do_POST(self) -> None:
            if self.path == "/watcher":
                if not self._is_watcher_authenticated():
                    self._send_json(401, {"error": "unauthorized"})
                    return
            else:
                if not self._is_ipc_authenticated():
                    self._send_json(401, {"error": "unauthorized"})
                    return

            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._send_json(415, {"error": "expected_application_json"})
                return

            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._send_json(400, {"error": "invalid_content_length"})
                return
            if length < 0 or length > MAX_IPC_BODY_BYTES:
                self._send_json(413, {"error": "payload_too_large"})
                return

            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(400, {"error": "invalid_json"})
                return
            if not isinstance(payload, dict):
                self._send_json(400, {"error": "expected_json_object"})
                return

            if self.path == "/hook":
                res = router.handle_hook_event(payload)
                self._send_json(200, res)
                return
            if self.path == "/approve":
                res = router.handle_approval_request(payload)
                self._send_json(200, res)
                return
            if self.path == "/watcher":
                res = router.handle_watcher_event(payload)
                st = res.get("status")
                if st == "shutting_down":
                    code = 503
                elif st == "error":
                    code = 400
                elif st == "failed":
                    code = 502
                else:
                    code = 200
                self._send_json(code, res)
                return
            if self.path == "/watcher/topic":
                res = router.handle_watcher_topic_request(payload)
                if res.get("ok"):
                    code = 200
                elif res.get("error") == "rate_limited":
                    code = 429
                else:
                    code = 400
                self._send_json(code, res)
                return
            self._send_json(404, {"error": "not_found"})

    server = ThreadingHTTPServer((config.ipc_host, config.ipc_port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("Local IPC server listening on http://%s:%d", config.ipc_host, config.ipc_port)
    return server


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    env_file = resolve_env_file(repo_root)
    config = BridgeConfig.from_env(env_file)
    ipc_token = ensure_ipc_token(config.ipc_token_path)
    watcher_token = ensure_ipc_token(config.watcher_token_path)
    config.export_watcher_env()
    store = BridgeStore(config.db_path)
    runner = AgyRunner(config)
    telegram = TelegramClient(
        config.telegram_bot_token,
        max_inline_chars=config.max_inline_chars,
        api_base_url=config.telegram_api_base_url,
    )
    router = BridgeRouter(config, store, runner, telegram)
    stop_event = threading.Event()
    clean_exit = False

    def _handle_sig(_signum, _frame) -> None:
        log.info("Shutting down agy-bridge...")
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    # Immediately fail-close approvals & terminate in-flight Stage-2 watcher turns on SIGTERM
    def _shutdown_watcher() -> None:
        stop_event.wait()
        router.begin_shutdown()

    threading.Thread(target=_shutdown_watcher, daemon=True).start()

    ipc = start_ipc_server(config, router, ipc_token, watcher_token=watcher_token)

    try:
        # Mark running state and notify systemd READY=1 BEFORE any network calls
        prev_clean = store.get_kv("clean_shutdown", "")
        unclean_exit = prev_clean == "0"
        store.set_kv("clean_shutdown", "0")
        sd_notify("READY=1")

        # Run startup reconciliation asynchronously so a boot WAN outage never blocks READY=1
        router.start_startup_recovery(unclean_exit=unclean_exit)

        if not telegram.configured:
            log.info(
                "TELEGRAM_BOT_TOKEN not configured yet; IPC server is active on %s:%d, Telegram polling in standby.",
                config.ipc_host,
                config.ipc_port,
            )
            while not stop_event.is_set():
                sd_notify("WATCHDOG=1")
                stop_event.wait(5.0)
            clean_exit = True
            return 0

        # Register slash commands in background so degraded WAN at boot never delays polling/watchdog
        def _register_commands() -> None:
            try:
                telegram.set_my_commands(BOT_COMMANDS)
                log.info("Registered %d Telegram bot slash commands", len(BOT_COMMANDS))
            except Exception as exc:
                log.warning("Could not register Telegram slash commands: %s", exc)

        threading.Thread(target=_register_commands, daemon=True).start()

        offset = int(store.get_kv("telegram_offset", "0") or "0")
        log.info(
            "Starting Telegram long-polling (offset=%d, poll_timeout=%ds, mirror_default=%s, max_inline=%d)",
            offset,
            config.poll_timeout_seconds,
            config.default_mirror_mode,
            config.max_inline_chars,
        )
        while not stop_event.is_set():
            sd_notify("WATCHDOG=1")
            try:
                updates = telegram.get_updates(
                    offset=offset, timeout=config.poll_timeout_seconds
                )
                for upd in updates:
                    upd_id = int(upd["update_id"])
                    if upd_id < offset:
                        log.debug("Skipping duplicate/stale update_id=%d (offset=%d)", upd_id, offset)
                        continue
                    offset = upd_id + 1
                    store.set_kv("telegram_offset", str(offset))
                    try:
                        router.handle_telegram_update(upd)
                    except Exception:
                        log.exception("Failed handling update %s", upd_id)
            except urllib.error.URLError as exc:
                log.warning("Telegram network error: %s", exc)
                stop_event.wait(5.0)
            except Exception:
                log.exception("Unexpected error in Telegram polling loop")
                stop_event.wait(5.0)
        clean_exit = True
    finally:
        sd_notify("STOPPING=1")
        router.begin_shutdown()
        ipc.shutdown()
        ipc.server_close()
        router.shutdown(timeout_seconds=25.0)
        if clean_exit:
            try:
                store.set_kv("clean_shutdown", "1")
            except Exception:
                log.debug("Could not persist clean_shutdown flag")
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
