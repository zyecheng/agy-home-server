"""Configuration loader for the Antigravity home server bridge."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _parse_int_set(raw: str) -> frozenset[int]:
    return frozenset(int(x.strip()) for x in raw.split(",") if x.strip())


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
    timezone_label: str

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "BridgeConfig":
        if env_file is not None:
            load_dotenv(env_file)
        home = Path.home()
        return cls(
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            allowed_user_ids=_parse_int_set(
                os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")
            ),
            allowed_chat_ids=_parse_int_set(
                os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
            ),
            poll_timeout_seconds=int(os.environ.get("POLL_TIMEOUT_SECONDS", "30")),
            debounce_seconds=float(os.environ.get("DEBOUNCE_SECONDS", "1.5")),
            max_inline_chars=int(os.environ.get("MAX_INLINE_CHARS", "3500")),
            ssh_prompt_preview_chars=int(
                os.environ.get("SSH_PROMPT_PREVIEW_CHARS", "300")
            ),
            default_mirror_mode=os.environ.get("DEFAULT_MIRROR_MODE", "full").strip(),
            alpha_workspace=Path(
                os.environ.get("ALPHA_WORKSPACE", str(home / "alpha-assistant"))
            ),
            agy_bin=os.environ.get("AGY_BIN", str(home / ".local/bin/agy")),
            agy_brain_dir=Path(
                os.environ.get(
                    "AGY_BRAIN_DIR", str(home / ".gemini/antigravity-cli/brain")
                )
            ),
            agy_model=os.environ.get("AGY_MODEL", "").strip(),
            agy_timeout_seconds=int(os.environ.get("AGY_TIMEOUT_SECONDS", "600")),
            db_path=Path(
                os.environ.get(
                    "BRIDGE_DB_PATH", str(home / "agy-server/data/bridge.db")
                )
            ),
            ipc_host=os.environ.get("BRIDGE_IPC_HOST", "127.0.0.1"),
            ipc_port=int(os.environ.get("BRIDGE_IPC_PORT", "8765")),
            timezone_label=os.environ.get("BRIDGE_TZ_LABEL", "UTC"),
        )
