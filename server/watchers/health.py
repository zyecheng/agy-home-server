# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Dedicated zero-LLM host health probe & daily online SQLite backup (`agy-health.service`).

Alerts directly via TelegramClient (zero bridge-daemon or LLM dependency), with
state-transition alerting, 6-hour repeat cooldown, recovery notices, and
alert-before-write on disk-full conditions.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import BridgeConfig, resolve_env_file
from ..db import BridgeStore
from ..telegram_client import TelegramClient
from .schema import atomic_write_json, safe_read_json

log = logging.getLogger("agy-bridge.health")

DISK_WARN_PERCENT = 85.0
OAUTH_MAX_AGE_DAYS = 14
ALERT_COOLDOWN_SECONDS = 6 * 3600.0
BACKUP_RETENTION_DAYS = 7


def check_disk_usage(path: str = "/", *, threshold_pct: float = DISK_WARN_PERCENT) -> str | None:
    usage = shutil.disk_usage(path)
    pct = (usage.used / usage.total) * 100.0 if usage.total > 0 else 0.0
    if pct >= threshold_pct:
        free_gb = usage.free / (1024**3)
        return f"Disk {path} usage is {pct:.1f}% ({free_gb:.1f} GiB free; threshold {threshold_pct:.0f}%)"
    return None


def check_systemd_service(unit: str = "agy-telegram.service") -> str | None:
    if os.name == "nt":
        return None
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=10,
        )
        state = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode != 0 or state != "active":
            return f"{unit} is not active (state={state or 'unknown'})"
        return None
    except FileNotFoundError:
        return None
    except Exception as exc:
        return f"Failed checking {unit}: {exc}"


def check_oauth_token(
    oauth_path: Path,
    *,
    now_ts: float,
    max_age_days: int = OAUTH_MAX_AGE_DAYS,
) -> str | None:
    if not oauth_path.exists():
        return f"Antigravity OAuth token missing at {oauth_path}"
    try:
        st = oauth_path.stat()
        if st.st_size <= 0:
            return f"Antigravity OAuth token file is empty at {oauth_path}"
        age_days = (now_ts - st.st_mtime) / 86400.0
        if max_age_days > 0 and age_days > max_age_days:
            return (
                f"Antigravity OAuth token has not refreshed in {age_days:.1f} days "
                f"(>{max_age_days}d); verify `agy` auth."
            )
    except OSError as exc:
        return f"Cannot inspect OAuth token: {exc}"
    return None


def run_online_sqlite_backup(
    db_path: Path,
    backups_dir: Path,
    *,
    now_ts: float,
    retention_days: int = BACKUP_RETENTION_DAYS,
) -> tuple[Path | None, str | None]:
    """Perform a daily online SQLite backup (mode 0600) and prune old backups."""
    if not db_path.exists():
        return None, None
    backups_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(backups_dir, 0o700)
    except OSError:
        pass

    day = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    target = backups_dir / f"bridge-{day}.db"
    if not target.exists():
        tmp = backups_dir / f".bridge-{day}.tmp.db"
        try:
            tmp.unlink(missing_ok=True)
            src_uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
            src = sqlite3.connect(src_uri, uri=True, timeout=10.0)
            try:
                dst = sqlite3.connect(str(tmp), timeout=10.0)
                try:
                    src.backup(dst)
                    res = dst.execute("PRAGMA integrity_check;").fetchone()
                    if not res or str(res[0]).lower() != "ok":
                        raise RuntimeError(f"backup integrity check failed: {res}")
                finally:
                    dst.close()
            finally:
                src.close()
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, target)
        except Exception as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return None, f"SQLite online backup failed: {exc}"

    # Rotate backups older than retention_days
    cutoff = now_ts - max(1, retention_days) * 86400.0
    for old in sorted(backups_dir.glob("bridge-*.db")):
        if old == target:
            continue
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
        except OSError:
            pass
    return target, None


def resolve_alert_chat_id(config: BridgeConfig) -> str | None:
    """Resolve DM alert target: read-only bridge.db -> ALLOWED_CHAT_IDS -> ALLOWED_USER_IDS."""
    if config.db_path.exists():
        try:
            store = BridgeStore(config.db_path, read_only=True)
            try:
                primary = store.get_primary_dm_binding("telegram")
                if primary is not None:
                    return str(primary.chat_id)
            finally:
                store.close()
        except Exception:
            pass
    if config.allowed_chat_ids:
        return str(sorted(config.allowed_chat_ids)[0])
    if config.allowed_user_ids:
        return str(sorted(config.allowed_user_ids)[0])
    return None


def run_health_check(
    config: BridgeConfig,
    *,
    telegram: TelegramClient | None = None,
    oauth_path: Path | None = None,
    disk_path: str = "/",
    service_unit: str | None = "agy-telegram.service",
    disk_threshold_pct: float = DISK_WARN_PERCENT,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Run all host health probes + daily backup and alert directly via TelegramClient."""
    now = time.time() if now_ts is None else now_ts
    tg = telegram or TelegramClient(
        config.telegram_bot_token,
        max_inline_chars=config.max_inline_chars,
        api_base_url=config.telegram_api_base_url,
    )

    state_dir = config.db_path.parent
    state_file = state_dir / "health_state.json"
    backups_dir = state_dir / "backups"
    oauth = oauth_path or (config.agy_brain_dir.parent / "antigravity-oauth-token")

    faults: dict[str, str] = {}
    disk_err = check_disk_usage(disk_path, threshold_pct=disk_threshold_pct)
    if disk_err:
        faults["disk"] = disk_err
    if service_unit:
        svc_err = check_systemd_service(service_unit)
        if svc_err:
            faults["service"] = svc_err
    oauth_err = check_oauth_token(oauth, now_ts=now)
    if oauth_err:
        faults["oauth"] = oauth_err

    # Skip writing backup if disk is already critically full
    backup_path: Path | None = None
    if "disk" not in faults:
        backup_path, b_err = run_online_sqlite_backup(
            config.db_path, backups_dir, now_ts=now
        )
        if b_err:
            faults["backup"] = b_err

    prev_state: dict[str, Any] = {}
    if state_dir.exists():
        try:
            prev_state = safe_read_json(state_file, state_dir) or {}
        except Exception:
            prev_state = {}

    prev_active: dict[str, float] = dict(prev_state.get("active_faults") or {})
    to_alert: list[str] = []
    alert_keys: list[str] = []
    recovered: list[str] = []
    next_active: dict[str, float] = {}

    for key, detail in faults.items():
        last_sent = float(prev_active.get(key) or 0.0)
        if last_sent == 0.0 or (now - last_sent) >= ALERT_COOLDOWN_SECONDS:
            to_alert.append(f"- [{key}] {detail}")
            alert_keys.append(key)
        # Preserve previous timestamp until delivery succeeds (never stamp cooldown unconfigured!)
        if last_sent > 0.0:
            next_active[key] = last_sent

    for old_key in prev_active:
        if old_key not in faults:
            recovered.append(old_key)

    chat_id = resolve_alert_chat_id(config)
    sent_messages = 0
    if chat_id and tg.configured:
        if to_alert:
            msg = "[Host Health Alert]\n" + "\n".join(to_alert)
            try:
                tg.send_message(chat_id, msg, disable_web_page_preview=True)
                sent_messages += 1
                for k in alert_keys:
                    next_active[k] = now
            except Exception as exc:
                log.warning("Failed sending host health alert: %s", exc)
        if recovered:
            msg = "[Host Health Recovered]\nResolved: " + ", ".join(sorted(recovered))
            try:
                tg.send_message(
                    chat_id,
                    msg,
                    disable_notification=True,
                    disable_web_page_preview=True,
                )
                sent_messages += 1
            except Exception as exc:
                log.warning("Failed sending host health recovery notice: %s", exc)
                # Keep recovered keys in active_faults so recovery retries on next tick
                for rk in recovered:
                    next_active[rk] = float(prev_active.get(rk) or now)
    else:
        # Preserve previously alerted faults if Telegram isn't ready yet so recovery isn't silently lost
        for rk in recovered:
            next_active[rk] = float(prev_active.get(rk) or now)

    # Persist state AFTER alerting so a full disk never suppresses the disk-full alert
    try:
        atomic_write_json(
            state_file,
            state_dir,
            {"last_check_at": now, "active_faults": next_active},
        )
    except OSError as exc:
        log.warning("Could not persist health_state.json: %s", exc)

    return {
        "ok": not faults,
        "faults": faults,
        "recovered": recovered,
        "sent_messages": sent_messages,
        "backup_path": str(backup_path) if backup_path else "",
    }


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    repo_root = Path(__file__).resolve().parent.parent.parent
    env_file = resolve_env_file(repo_root)
    config = BridgeConfig.from_env(env_file)
    result = run_health_check(config)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
