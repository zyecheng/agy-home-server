# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Configuration loader and shared localhost IPC client for the Antigravity bridge."""

from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

VALID_MIRROR_MODES = ("full", "replies_only", "muted")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
DEFAULT_TELEGRAM_API_BASE_URL = "https://api.telegram.org"


def validate_telegram_api_base_url(raw: str) -> str:
    """Allow only official Telegram API HTTPS or loopback HTTP (for local E2E tests)."""
    url = (raw or DEFAULT_TELEGRAM_API_BASE_URL).strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.params or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError(
            f"Refusing TELEGRAM_API_BASE_URL '{url}'; must not include userinfo, query, or fragment"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"Invalid port in TELEGRAM_API_BASE_URL '{url}'") from exc
    if (
        parsed.scheme == "https"
        and parsed.hostname == "api.telegram.org"
        and not parsed.path
        and port in (None, 443)
    ):
        return url
    if parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS and not parsed.path:
        return url
    raise ValueError(
        f"Refusing TELEGRAM_API_BASE_URL '{url}'; must be https://api.telegram.org or loopback http://"
    )


def resolve_env_file(repo_root: Path) -> Path | None:
    """Resolve optional BRIDGE_ENV_FILE consistently across daemon, hooks, and watcher runner."""
    env_override = os.environ.get("BRIDGE_ENV_FILE")
    if env_override is not None:
        return Path(env_override) if env_override.strip() else None
    return repo_root / ".env"


def parse_dotenv(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE .env file into a dict without mutating os.environ."""
    values: dict[str, str] = {}
    try:
        if not path.exists():
            return values
        raw_text = path.read_text(encoding="utf-8")
    except (PermissionError, OSError):
        # Under systemd InaccessiblePaths, exists() is True on a 0000 placeholder; treat as unreadable/absent
        return values
    for raw_line in raw_text.splitlines():
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


def _parse_optional_int(raw: str | None) -> int | None:
    s = (raw or "").strip()
    return int(s) if s else None


def _parse_int(raw: str | None, default: int) -> int:
    s = (raw or "").strip()
    return int(s) if s else default


def _parse_float(raw: str | None, default: float) -> float:
    s = (raw or "").strip()
    return float(s) if s else default


def format_loopback_url_host(host: str) -> str:
    clean = (host or "127.0.0.1").strip()
    if clean not in _LOOPBACK_HOSTS:
        raise ValueError(f"Refusing non-loopback BRIDGE_IPC_HOST '{clean}'; must be loopback")
    return f"[{clean}]" if ":" in clean else clean


def load_ipc_client_settings(repo_root: Path) -> tuple[str, int, Path, float]:
    """Load validated loopback IPC settings for hook/CLI scripts (host_for_url, port, token_path, approval_timeout)."""
    env_file = resolve_env_file(repo_root)
    env_vals = parse_dotenv(env_file) if env_file is not None else {}

    def _get(key: str, default: str) -> str:
        val = os.environ.get(key)
        if val is not None and val != "":
            return val.strip()
        fval = env_vals.get(key)
        if fval is not None and fval != "":
            return fval.strip()
        return default

    url_host = format_loopback_url_host(_get("BRIDGE_IPC_HOST", "127.0.0.1"))
    ipc_port = int(_get("BRIDGE_IPC_PORT", "8765"))
    db_path = Path(_get("BRIDGE_DB_PATH", str(repo_root / "data" / "bridge.db")))
    token_path = Path(_get("BRIDGE_IPC_TOKEN_PATH", str(db_path.parent / "ipc.token")))
    approval_timeout = max(0.05, _parse_float(_get("APPROVAL_TIMEOUT_SECONDS", "120"), 120.0))
    return url_host, ipc_port, token_path, approval_timeout


def post_bridge_ipc(
    ipc_host: str,
    ipc_port: int,
    token_path: Path,
    endpoint: str,
    payload: dict[str, Any],
    *,
    timeout: float = 30.0,
) -> tuple[int, dict[str, Any]]:
    """POST authenticated JSON to localhost bridge IPC (bypassing ambient HTTP_PROXY)."""
    raw_host = ipc_host.strip("[]")
    url_host = format_loopback_url_host(raw_host)
    if not token_path.exists():
        return 0, {"status": "error", "error": "ipc_token_missing"}
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return 0, {"status": "error", "error": f"ipc_token_unreadable: {exc}"}
    if not token:
        return 0, {"status": "error", "error": "ipc_token_empty"}

    path = endpoint if endpoint.startswith("/") else f"/{endpoint}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://{url_host}:{int(ipc_port)}{path}",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Bridge-Token": token,
        },
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw) if raw.strip() else {}
            return int(resp.status), parsed if isinstance(parsed, dict) else {"raw": parsed}
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw) if raw.strip() else {}
        except Exception:
            parsed = {"status": "error", "error": f"http_{exc.code}"}
        return int(exc.code), parsed if isinstance(parsed, dict) else {"status": "error"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, {"status": "error", "error": f"ipc_network_error: {exc}"}


def _write_0600_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except AttributeError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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
    token = secrets.token_hex(32)
    _write_0600_atomic(token_path, token + "\n")
    return token


@dataclass(frozen=True)
class WatcherRuntimeConfig:
    """Non-secret runtime settings for agy-watcher.service (no .env / TELEGRAM_BOT_TOKEN access)."""

    ipc_host: str
    ipc_port: int
    watcher_token_path: Path
    timezone_name: str
    quiet_start: str
    quiet_end: str
    max_daily_web_scouts: int
    agy_bin: str
    agy_model: str
    agy_brain_dir: Path
    scout_sandbox_dir: Path
    missions_dir: Path
    state_dir: Path
    triggers_dir: Path
    max_concurrent_fast: int = 4
    max_concurrent_scouts: int = 2

    @classmethod
    def from_env(
        cls,
        env_file: Path | None = None,
        *,
        repo_root: Path | None = None,
    ) -> "WatcherRuntimeConfig":
        file_vals = parse_dotenv(env_file) if env_file is not None else {}

        def get(key: str, default: str = "") -> str:
            if key in os.environ and os.environ[key] != "":
                return os.environ[key].strip()
            if key in file_vals and file_vals[key] != "":
                return file_vals[key].strip()
            return default

        home = Path.home()
        root = repo_root or Path(__file__).resolve().parent.parent
        ipc_host = get("BRIDGE_IPC_HOST", "127.0.0.1")
        format_loopback_url_host(ipc_host)
        ipc_port = _parse_int(get("BRIDGE_IPC_PORT", "8765"), 8765)

        alpha_ws = Path(get("ALPHA_WORKSPACE", str(home / "alpha-assistant")))
        watcher_ipc_dir = Path(get("WATCHER_IPC_DIR", str(root / "watcher-ipc")))
        watcher_token_path = Path(
            get("WATCHER_TOKEN_PATH", str(watcher_ipc_dir / "watcher.token"))
        )
        missions_dir = Path(get("WATCHER_MISSIONS_DIR", str(alpha_ws / "watchers" / "missions")))
        state_dir = Path(get("WATCHER_STATE_DIR", str(alpha_ws / "watchers" / "state")))
        triggers_dir = Path(get("WATCHER_TRIGGERS_DIR", str(alpha_ws / "watchers" / "triggers")))
        max_concurrent_fast = max(
            1, min(16, _parse_int(get("WATCHER_MAX_CONCURRENT_FAST", "4"), 4))
        )
        max_concurrent_scouts = max(
            1, min(4, _parse_int(get("WATCHER_MAX_CONCURRENT_SCOUTS", "2"), 2))
        )

        return cls(
            ipc_host=ipc_host,
            ipc_port=ipc_port,
            watcher_token_path=watcher_token_path,
            timezone_name=get("BRIDGE_TIMEZONE", "America/Los_Angeles"),
            quiet_start=get("WATCHER_QUIET_START", "23:00"),
            quiet_end=get("WATCHER_QUIET_END", "08:00"),
            max_daily_web_scouts=max(0, _parse_int(get("MAX_DAILY_WEB_SCOUTS", "12"), 12)),
            agy_bin=get("AGY_BIN", str(home / ".local/bin/agy")),
            agy_model=get("AGY_MODEL", ""),
            agy_brain_dir=Path(
                get("AGY_BRAIN_DIR", str(home / ".gemini/antigravity-cli/brain"))
            ),
            scout_sandbox_dir=Path(
                get("SCOUT_SANDBOX_DIR", str(home / "agy-scout-sandbox"))
            ),
            missions_dir=missions_dir,
            state_dir=state_dir,
            triggers_dir=triggers_dir,
            max_concurrent_fast=max_concurrent_fast,
            max_concurrent_scouts=max_concurrent_scouts,
        )


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
    max_attachment_bytes: int = 15 * 1024 * 1024
    inbox_retention_days: int = 7
    watcher_forum_chat_id: int | None = None
    watcher_ipc_dir: Path | None = None
    scout_sandbox_dir: Path | None = None
    watcher_quiet_start: str = "23:00"
    watcher_quiet_end: str = "08:00"
    max_daily_web_scouts: int = 12
    watcher_max_concurrent_fast: int = 4
    watcher_max_concurrent_scouts: int = 2
    watcher_stage2_timeout_seconds: int = 90

    @property
    def effective_allowed_chat_ids(self) -> frozenset[int]:
        """Include WATCHER_FORUM_CHAT_ID ONLY when TELEGRAM_ALLOWED_CHAT_IDS is non-empty."""
        if not self.allowed_chat_ids:
            return frozenset()
        if self.watcher_forum_chat_id is not None:
            return self.allowed_chat_ids | {self.watcher_forum_chat_id}
        return self.allowed_chat_ids

    @property
    def resolved_watcher_ipc_dir(self) -> Path:
        if self.watcher_ipc_dir is not None:
            return self.watcher_ipc_dir
        return self.db_path.parent.parent / "watcher-ipc"

    @property
    def watcher_token_path(self) -> Path:
        return self.resolved_watcher_ipc_dir / "watcher.token"

    @property
    def watcher_env_path(self) -> Path:
        return self.resolved_watcher_ipc_dir / "watcher.env"

    @property
    def resolved_scout_sandbox_dir(self) -> Path:
        if self.scout_sandbox_dir is not None:
            return self.scout_sandbox_dir
        return Path.home() / "agy-scout-sandbox"

    @property
    def watcher_missions_dir(self) -> Path:
        return self.alpha_workspace / "watchers" / "missions"

    @property
    def watcher_state_dir(self) -> Path:
        return self.alpha_workspace / "watchers" / "state"

    @property
    def watcher_triggers_dir(self) -> Path:
        return self.alpha_workspace / "watchers" / "triggers"

    def export_watcher_env(self) -> Path:
        """Atomically export non-secret watcher runtime settings into watcher-ipc/watcher.env (0600)."""
        ipc_dir = self.resolved_watcher_ipc_dir
        ipc_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(ipc_dir, 0o700)
        except OSError:
            pass
        lines = [
            "# Auto-generated by agy-telegram.service (non-secret watcher runtime config)",
            f"BRIDGE_IPC_HOST={self.ipc_host}",
            f"BRIDGE_IPC_PORT={self.ipc_port}",
            f"WATCHER_IPC_DIR={ipc_dir}",
            f"WATCHER_TOKEN_PATH={self.watcher_token_path}",
            f"BRIDGE_TIMEZONE={self.timezone_name}",
            f"WATCHER_QUIET_START={self.watcher_quiet_start}",
            f"WATCHER_QUIET_END={self.watcher_quiet_end}",
            f"MAX_DAILY_WEB_SCOUTS={self.max_daily_web_scouts}",
            f"WATCHER_MAX_CONCURRENT_FAST={self.watcher_max_concurrent_fast}",
            f"WATCHER_MAX_CONCURRENT_SCOUTS={self.watcher_max_concurrent_scouts}",
            f"AGY_BIN={self.agy_bin}",
            f"AGY_MODEL={self.agy_model}",
            f"AGY_BRAIN_DIR={self.agy_brain_dir}",
            f"SCOUT_SANDBOX_DIR={self.resolved_scout_sandbox_dir}",
            f"ALPHA_WORKSPACE={self.alpha_workspace}",
            f"WATCHER_MISSIONS_DIR={self.watcher_missions_dir}",
            f"WATCHER_STATE_DIR={self.watcher_state_dir}",
            f"WATCHER_TRIGGERS_DIR={self.watcher_triggers_dir}",
        ]
        _write_0600_atomic(self.watcher_env_path, "\n".join(lines) + "\n")
        return self.watcher_env_path

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
        watcher_ipc_raw = get("WATCHER_IPC_DIR", "").strip()
        watcher_ipc_dir = (
            Path(watcher_ipc_raw) if watcher_ipc_raw else (db_path.parent.parent / "watcher-ipc")
        )
        scout_sandbox_raw = get("SCOUT_SANDBOX_DIR", "").strip()
        scout_sandbox_dir = (
            Path(scout_sandbox_raw) if scout_sandbox_raw else (home / "agy-scout-sandbox")
        )
        telegram_api_base_url = validate_telegram_api_base_url(
            get("TELEGRAM_API_BASE_URL", DEFAULT_TELEGRAM_API_BASE_URL)
        )
        approval_timeout_seconds = max(
            0.05, _parse_float(get("APPROVAL_TIMEOUT_SECONDS", "120"), 120.0)
        )
        max_attachment_bytes = max(
            1024,
            min(
                20 * 1024 * 1024,
                _parse_int(get("MAX_ATTACHMENT_BYTES", str(15 * 1024 * 1024)), 15 * 1024 * 1024),
            ),
        )
        inbox_retention_days = max(0, _parse_int(get("INBOX_RETENTION_DAYS", "7"), 7))
        stage2_timeout = max(
            10, min(180, _parse_int(get("WATCHER_STAGE2_TIMEOUT_SECONDS", "90"), 90))
        )
        max_concurrent_fast = max(
            1, min(16, _parse_int(get("WATCHER_MAX_CONCURRENT_FAST", "4"), 4))
        )
        max_concurrent_scouts = max(
            1, min(4, _parse_int(get("WATCHER_MAX_CONCURRENT_SCOUTS", "2"), 2))
        )

        return cls(
            telegram_bot_token=get("TELEGRAM_BOT_TOKEN", "").strip(),
            allowed_user_ids=_parse_int_set(get("TELEGRAM_ALLOWED_USER_IDS", "")),
            allowed_chat_ids=_parse_int_set(get("TELEGRAM_ALLOWED_CHAT_IDS", "")),
            poll_timeout_seconds=_parse_int(get("POLL_TIMEOUT_SECONDS", "15"), 15),
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
            max_attachment_bytes=max_attachment_bytes,
            inbox_retention_days=inbox_retention_days,
            watcher_forum_chat_id=_parse_optional_int(get("WATCHER_FORUM_CHAT_ID", "")),
            watcher_ipc_dir=watcher_ipc_dir,
            scout_sandbox_dir=scout_sandbox_dir,
            watcher_quiet_start=get("WATCHER_QUIET_START", "23:00").strip(),
            watcher_quiet_end=get("WATCHER_QUIET_END", "08:00").strip(),
            max_daily_web_scouts=max(0, _parse_int(get("MAX_DAILY_WEB_SCOUTS", "12"), 12)),
            watcher_max_concurrent_fast=max_concurrent_fast,
            watcher_max_concurrent_scouts=max_concurrent_scouts,
            watcher_stage2_timeout_seconds=stage2_timeout,
        )
