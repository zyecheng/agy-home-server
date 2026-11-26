#!/usr/bin/env python3
"""Main daemon: Telegram long-polling bridge + localhost Hook/Watcher IPC server."""

from __future__ import annotations

import json
import logging
import signal
import sys
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .agy_runner import AgyRunner
from .config import BridgeConfig
from .db import BridgeStore
from .router import BridgeRouter
from .telegram_client import TelegramClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("agy-bridge.main")


def start_ipc_server(config: BridgeConfig, router: BridgeRouter) -> ThreadingHTTPServer:
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
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid_json"})
                return

            if self.path == "/hook":
                res = router.handle_hook_event(payload)
                self._send_json(200, res)
                return
            if self.path == "/watcher":
                res = router.handle_watcher_event(payload)
                code = 200 if res.get("status") != "error" else 400
                self._send_json(code, res)
                return
            self._send_json(404, {"error": "not_found"})

    server = ThreadingHTTPServer((config.ipc_host, config.ipc_port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("Local IPC server listening on http://%s:%d", config.ipc_host, config.ipc_port)
    return server


def main() -> int:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    config = BridgeConfig.from_env(env_file)
    store = BridgeStore(config.db_path)
    runner = AgyRunner(config)
    telegram = TelegramClient(
        config.telegram_bot_token,
        max_inline_chars=config.max_inline_chars,
    )
    router = BridgeRouter(config, store, runner, telegram)
    ipc = start_ipc_server(config, router)

    stop_event = threading.Event()

    def _handle_sig(_signum, _frame) -> None:
        log.info("Shutting down agy-bridge...")
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    try:
        if not telegram.configured:
            log.info(
                "TELEGRAM_BOT_TOKEN not configured yet; IPC server is active on %s:%d, Telegram polling in standby.",
                config.ipc_host,
                config.ipc_port,
            )
            while not stop_event.is_set():
                stop_event.wait(5.0)
            return 0

        offset = int(store.get_kv("telegram_offset", "0") or "0")
        log.info(
            "Starting Telegram long-polling (offset=%d, mirror_default=%s, max_inline=%d)",
            offset,
            config.default_mirror_mode,
            config.max_inline_chars,
        )
        while not stop_event.is_set():
            try:
                updates = telegram.get_updates(
                    offset=offset, timeout=config.poll_timeout_seconds
                )
                for upd in updates:
                    upd_id = int(upd["update_id"])
                    offset = max(offset, upd_id + 1)
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
    finally:
        ipc.shutdown()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
