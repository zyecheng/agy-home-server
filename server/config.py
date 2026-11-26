# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Configuration loader for the Antigravity home server bridge."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

VALID_MIRROR_MODES = ("full", "replies_only", "muted")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
DEFAULT_TELEGRAM_API_BASE_URL = "https://api.telegram.org"


def validate_telegram_api_base_url(raw: str) -> str:
    """Allow only official Telegram API HTTPS or loopback HTTP (for local E2E tests)."""
    url = (raw or DEFAULT_TELEGRAM_API_BASE_URL).strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname == "api.telegram.org" and not parsed.path:
        return url
    if parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS and not parsed.path:
        return url
    raise ValueError(
        f"Refusing TELEGRAM_API_BASE_URL '{url}'; must be https://api.telegram.org or loopback http://"
    )


def parse_dotenv(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE .env file into a dict without mutating os.environ."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        k = key.strip()
        if k:
            values[k] = value.strip().strip('"').strip("'")
    return values


def _parse_int_set(raw: str) -> frozenset[int]:
    return frozenset(int(x.strip()) for x in raw.split(",") if x.strip())


def _parse_int(raw: str | None, default: int) -> int:
    s = (raw or "").strip()
    return int(s) if s else default


def _parse_float(raw: str | None, default: float) -> float:
    s = (raw or "").strip()
    return float(s) if s else default


def ensure_ipc_token(token_path: Path) -> str:
    """Read or atomically create a 0600 shared secret for localhost IPC auth."""
    if token_path.exists():
        existing = token_path.read_text(encoding="utf-8").strip()
        if existing:
            try:
                os.chmod(token_path, 0o600)
            except OSError:
                pass
            return existing
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(32)
    tmp = token_path.with_name(f".{token_path.name}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except AttributeError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(token + "\n")
        os.replace(tmp, token_path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return token


@dataclass(frozen=True)
class BridgeConfig:
    telegram_bot_token: str
    allowed_user_ids: frozenset[int]
    allowed_chat_ids: frozenset[int]
    poll_timeout_seconds: int
    debounce_seconds: float
    max_inline_chars: int
    ssh_prompt_preview_chars: int
    default_mirror_mode: str
    alpha_workspace: Path
    agy_bin: str
    agy_brain_dir: Path
    agy_model: str
    agy_timeout_seconds: int
    db_path: Path
    ipc_host: str
    ipc_port: int
    ipc_token_path: Path
    timezone_name: str
    telegram_api_base_url: str = DEFAULT_TELEGRAM_API_BASE_URL
    approval_timeout_seconds: float = 120.0

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "BridgeConfig":
        # Merge process env (takes precedence) over .env without mutating os.environ
        file_vals = parse_dotenv(env_file) if env_file is not None else {}

        def get(key: str, default: str = "") -> str:
            if key in os.environ and os.environ[key] != "":
                return os.environ[key]
            if key in file_vals and file_vals[key] != "":
                return file_vals[key]
            return default

        home = Path.home()
        mirror_mode = get("DEFAULT_MIRROR_MODE", "full").strip()
        if mirror_mode not in VALID_MIRROR_MODES:
            raise ValueError(
                f"Invalid DEFAULT_MIRROR_MODE '{mirror_mode}'; expected one of {VALID_MIRROR_MODES}"
            )

        ipc_host = get("BRIDGE_IPC_HOST", "127.0.0.1").strip()
        if ipc_host not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"Refusing non-loopback BRIDGE_IPC_HOST '{ipc_host}'; must be loopback"
            )

        db_path = Path(get("BRIDGE_DB_PATH", str(home / "agy-server/data/bridge.db")))
        ipc_token_path = Path(
            get("BRIDGE_IPC_TOKEN_PATH", str(db_path.parent / "ipc.token"))
        )
        telegram_api_base_url = validate_telegram_api_base_url(
            get("TELEGRAM_API_BASE_URL", DEFAULT_TELEGRAM_API_BASE_URL)
        )
        approval_timeout_seconds = max(
            0.05, _parse_float(get("APPROVAL_TIMEOUT_SECONDS", "120"), 120.0)
        )

        return cls(
            telegram_bot_token=get("TELEGRAM_BOT_TOKEN", "").strip(),
            allowed_user_ids=_parse_int_set(get("TELEGRAM_ALLOWED_USER_IDS", "")),
            allowed_chat_ids=_parse_int_set(get("TELEGRAM_ALLOWED_CHAT_IDS", "")),
            poll_timeout_seconds=_parse_int(get("POLL_TIMEOUT_SECONDS", "30"), 30),
            debounce_seconds=_parse_float(get("DEBOUNCE_SECONDS", "1.5"), 1.5),
            max_inline_chars=max(500, _parse_int(get("MAX_INLINE_CHARS", "3500"), 3500)),
            ssh_prompt_preview_chars=max(
                20, _parse_int(get("SSH_PROMPT_PREVIEW_CHARS", "300"), 300)
            ),
            default_mirror_mode=mirror_mode,
            alpha_workspace=Path(
                get("ALPHA_WORKSPACE", str(home / "alpha-assistant"))
            ),
            agy_bin=get("AGY_BIN", str(home / ".local/bin/agy")),
            agy_brain_dir=Path(
                get("AGY_BRAIN_DIR", str(home / ".gemini/antigravity-cli/brain"))
            ),
            agy_model=get("AGY_MODEL", "").strip(),
            agy_timeout_seconds=_parse_int(get("AGY_TIMEOUT_SECONDS", "600"), 600),
            db_path=db_path,
            ipc_host=ipc_host,
            ipc_port=_parse_int(get("BRIDGE_IPC_PORT", "8765"), 8765),
            ipc_token_path=ipc_token_path,
            timezone_name=get("BRIDGE_TIMEZONE", "America/Los_Angeles").strip(),
            telegram_api_base_url=telegram_api_base_url,
            approval_timeout_seconds=approval_timeout_seconds,
        )
