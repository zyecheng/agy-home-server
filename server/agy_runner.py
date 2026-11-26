# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Subprocess wrapper around headless Antigravity CLI (`agy -p`)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import BridgeConfig

log = logging.getLogger("agy-bridge.runner")


@dataclass(frozen=True)
class AgyResult:
    conversation_id: str
    status: str
    response: str
    error: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    num_turns: int = 0


def format_channel_envelope(
    channel: str,
    text: str,
    *,
    topic_name: str = "",
    sender: str = "",
    attachments: list[str] | None = None,
    tz_label: str = "UTC",
) -> str:
    """Build the compact bracketed envelope specified in Alpha's bridge_contract.md."""
    now_str = datetime.now().strftime(f"%Y-%m-%d %H:%M {tz_label}")
    parts = [f"channel: {channel}"]
    if topic_name:
        parts.append(f"topic: {topic_name}")
    if sender:
        parts.append(f"sender: {sender}")
    parts.append(f"time: {now_str}")
    header = "[" + " | ".join(parts) + "]"
    lines = [header]
    if attachments:
        lines.append("[attachments: " + ", ".join(attachments) + "]")
    lines.append(text.strip())
    return "\n".join(lines)


class AgyRunner:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config

    def run_prompt(
        self,
        prompt: str,
        *,
        conversation_id: str | None = None,
        workspace_dir: Path | None = None,
        model: str | None = None,
        disable_slash_commands: bool = True,
    ) -> AgyResult:
        cwd = workspace_dir or self.config.alpha_workspace
        cwd.mkdir(parents=True, exist_ok=True)

        cmd = [self.config.agy_bin, "-p", prompt, "--output-format", "json"]
        if conversation_id:
            cmd.extend(["--conversation", conversation_id])
        selected_model = model or self.config.agy_model
        if selected_model:
            cmd.extend(["--model", selected_model])
        if disable_slash_commands:
            cmd.append("--disable-slash-commands")
        if self.config.agy_timeout_seconds > 0:
            cmd.extend(["--print-timeout", f"{self.config.agy_timeout_seconds}s"])

        env = os.environ.copy()
        local_bin = str(Path(self.config.agy_bin).parent)
        if local_bin not in env.get("PATH", ""):
            env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"
        # Tag child process so hooks know this run originated from the bridge daemon
        env["AGY_BRIDGE_ORIGIN"] = "bridge"

        log.info(
            "Running agy -p (conv=%s, cwd=%s, len=%d)",
            conversation_id or "<new>",
            cwd,
            len(prompt),
        )
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=self.config.agy_timeout_seconds + 30,
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

        # Parse the last JSON line in case any diagnostic line preceded it
        payload = None
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

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
            duration_seconds=float(payload.get("duration_seconds") or 0.0),
            num_turns=int(payload.get("num_turns") or 0),
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
        env = os.environ.copy()
        env["AGY_BRIDGE_ORIGIN"] = "bridge"
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        out = (proc.stdout or proc.stderr or "").strip()
        return proc.returncode, out
