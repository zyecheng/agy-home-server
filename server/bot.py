#!/usr/bin/env python3
"""Minimal 24/7 home AI server Telegram bridge (stdlib-only starter)."""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("agy-telegram")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def tg_api(token: str, method: str, payload: dict | None = None, timeout: int = 40) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_host_status() -> str:
    uptime = subprocess.check_output(["uptime", "-p"], text=True).strip()
    disk = subprocess.check_output(["df", "-h", "/"], text=True).splitlines()[-1]
    return f"Host: {platform.node()}\nUptime: {uptime}\nDisk (/): {disk}"


def handle_message(token: str, chat_id: int, text: str) -> None:
    cmd = text.strip().split()[0] if text.strip() else ""
    if cmd == "/status":
        reply = get_host_status()
    elif cmd == "/ping":
        reply = f"pong from {platform.node()}"
    else:
        reply = (
            f"Received on {platform.node()}.\n"
            "Available starter commands: /ping, /status\n"
            "Ready to wire up `agy` CLI headless prompts."
        )
    tg_api(token, "sendMessage", {"chat_id": chat_id, "text": reply})


def main() -> int:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    allowed_raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    allowed_ids = {int(x.strip()) for x in allowed_raw.split(",") if x.strip()}
    poll_timeout = int(os.environ.get("POLL_TIMEOUT_SECONDS", "30"))

    if not token or token == "replace-me":
        log.info(
            "TELEGRAM_BOT_TOKEN not configured yet; running in standby heartbeat mode."
        )
        while True:
            time.sleep(60)

    log.info("Starting Telegram long-polling bridge on %s", platform.node())
    offset = 0
    while True:
        try:
            res = tg_api(
                token,
                "getUpdates",
                {"timeout": poll_timeout, "offset": offset},
                timeout=poll_timeout + 15,
            )
            for update in res.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = chat.get("id")
                text = msg.get("text")
                if not chat_id or not text:
                    continue
                if allowed_ids and chat_id not in allowed_ids:
                    log.warning("Ignored unauthorized chat_id=%s", chat_id)
                    continue
                handle_message(token, chat_id, text)
        except urllib.error.URLError as exc:
            log.warning("Network error polling Telegram: %s", exc)
            time.sleep(5)
        except Exception:
            log.exception("Unexpected error in polling loop")
            time.sleep(5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
