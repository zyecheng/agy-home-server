# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Subprocess wrapper around headless Antigravity CLI (`agy -p`)."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import BridgeConfig

log = logging.getLogger("agy-bridge.runner")

_SENSITIVE_ENV_PREFIXES = ("TELEGRAM_", "BRIDGE_IPC_")
_HEADER_SANITIZE_RE = re.compile(r"[\r\n\[\]|]+")


@dataclass(frozen=True)
class AgyResult:
    conversation_id: str
    status: str
    response: str
    error: str = ""
    stderr: str = ""


def _sanitize_header_val(val: str) -> str:
    return _HEADER_SANITIZE_RE.sub(" ", val).strip()


def format_local_timestamp(timezone_name: str) -> str:
    """Format current timestamp using an IANA ZoneInfo name (handles DST automatically)."""
    try:
        tz = ZoneInfo(timezone_name)
        now = datetime.now(tz)
        return now.strftime("%Y-%m-%d %H:%M %Z")
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")


def format_channel_envelope(
    channel: str,
    text: str,
    *,
    topic_name: str = "",
    sender: str = "",
    attachments: list[str] | None = None,
    timezone_name: str = "America/Los_Angeles",
) -> str:
    """Build the compact bracketed envelope specified in Alpha's bridge_contract.md."""
    now_str = format_local_timestamp(timezone_name)
    parts = [f"channel: {_sanitize_header_val(channel)}"]
    if topic_name:
        parts.append(f"topic: {_sanitize_header_val(topic_name)}")
    if sender:
        parts.append(f"sender: {_sanitize_header_val(sender)}")
    parts.append(f"time: {now_str}")
    header = "[" + " | ".join(parts) + "]"
    lines = [header]
    if attachments:
        safe_att = [_sanitize_header_val(a) for a in attachments]
        lines.append("[attachments: " + ", ".join(safe_att) + "]")
        lines.append("[note: inspect the staged attachment path(s) above with view_file/read_file]")
    lines.append(text.strip())
    return "\n".join(lines)


def build_scrubbed_env(
    agy_bin: str,
    *,
    bridge_origin: bool = True,
    binding_id: int | None = None,
) -> dict[str, str]:
    """Build a child process environment with bridge/Telegram secrets removed."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(_SENSITIVE_ENV_PREFIXES)
    }
    bin_path = Path(agy_bin)
    if bin_path.parent != Path("."):
        bin_dir = str(bin_path.parent)
        path_parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
        if bin_dir not in path_parts:
            env["PATH"] = os.pathsep.join([bin_dir, *path_parts])
    if bridge_origin:
        env["AGY_BRIDGE_ORIGIN"] = "bridge"
        if binding_id is not None:
            env["AGY_BRIDGE_BINDING_ID"] = str(int(binding_id))
        else:
            env.pop("AGY_BRIDGE_BINDING_ID", None)
    else:
        env.pop("AGY_BRIDGE_ORIGIN", None)
        env.pop("AGY_BRIDGE_BINDING_ID", None)
    return env


def _parse_json_stdout(stdout: str) -> dict | None:
    """Parse agy --output-format json stdout (single-line or pretty-printed)."""
    try:
        obj = json.loads(stdout)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


class AgyRunner:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config

    def run_prompt(
        self,
        prompt: str,
        *,
        conversation_id: str | None = None,
        workspace_dir: Path | None = None,
        timeout_seconds: int | None = None,
        binding_id: int | None = None,
    ) -> AgyResult:
        cwd = workspace_dir or self.config.alpha_workspace
        cwd.mkdir(parents=True, exist_ok=True)

        eff_timeout = (
            self.config.agy_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        cmd = [
            self.config.agy_bin,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--disable-slash-commands",
        ]
        if conversation_id:
            cmd.extend(["--conversation", conversation_id])
        if self.config.agy_model:
            cmd.extend(["--model", self.config.agy_model])
        if eff_timeout > 0:
            cmd.extend(["--print-timeout", f"{eff_timeout}s"])

        env = build_scrubbed_env(
            self.config.agy_bin,
            bridge_origin=True,
            binding_id=binding_id,
        )
        sub_timeout = eff_timeout + 30 if eff_timeout > 0 else None

        log.info(
            "Running agy -p (conv=%s, cwd=%s, len=%d, timeout=%s)",
            conversation_id or "<new>",
            cwd,
            len(prompt),
            sub_timeout,
        )
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                env=env,
                capture_output=True,
                text=True,
                timeout=sub_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = (
                exc.stderr.decode("utf-8", errors="replace").strip()
                if isinstance(exc.stderr, bytes)
                else str(exc.stderr or "").strip()
            )
            return AgyResult(
                conversation_id=conversation_id or "",
                status="TIMEOUT",
                response="",
                error=f"agy timed out after {sub_timeout}s",
                stderr=stderr,
            )

        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        if not stdout:
            return AgyResult(
                conversation_id=conversation_id or "",
                status="ERROR",
                response="",
                error=stderr or f"agy exited with code {proc.returncode} and empty stdout",
                stderr=stderr,
            )

        payload = _parse_json_stdout(stdout)
        if payload is None:
            return AgyResult(
                conversation_id=conversation_id or "",
                status="ERROR",
                response="",
                error=f"Non-JSON agy output (rc={proc.returncode}): {stdout[:400]} | {stderr[:400]}",
                stderr=stderr,
            )

        return AgyResult(
            conversation_id=str(payload.get("conversation_id") or conversation_id or ""),
            status=str(payload.get("status") or ("SUCCESS" if proc.returncode == 0 else "ERROR")),
            response=str(payload.get("response") or "").strip(),
            error=str(payload.get("error") or (stderr if proc.returncode != 0 else "")).strip(),
            stderr=stderr,
        )

    def run_cli_slash(
        self,
        slash_cmd: str,
        *,
        conversation_id: str | None = None,
        workspace_dir: Path | None = None,
    ) -> tuple[int, str]:
        """Run a CLI-handled slash command like /compact or /usage."""
        cwd = workspace_dir or self.config.alpha_workspace
        cwd.mkdir(parents=True, exist_ok=True)
        cmd = [self.config.agy_bin, "-p", slash_cmd]
        if conversation_id:
            cmd.extend(["--conversation", conversation_id])
        env = build_scrubbed_env(self.config.agy_bin, bridge_origin=True)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            return 124, f"{slash_cmd} timed out after 180s"
        except OSError as exc:
            return 1, f"{slash_cmd} failed to start: {exc}"
        out = (proc.stdout or proc.stderr or "").strip()
        return proc.returncode, out
