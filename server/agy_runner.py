# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Subprocess wrapper around headless Antigravity CLI (`agy -p`)."""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import BridgeConfig

log = logging.getLogger("agy-bridge.runner")

_SENSITIVE_ENV_PREFIXES = ("TELEGRAM_", "BRIDGE_IPC_", "WATCHER_", "AGY_SCOUT_")
_HEADER_SANITIZE_RE = re.compile(r"[\r\n\[\]|]+")


@dataclass(frozen=True)
class AgyResult:
    conversation_id: str
    status: str
    response: str
    error: str = ""
    stderr: str = ""
    structured_output: dict[str, Any] | None = None
    duration_seconds: float | None = None


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
    mission_tag: str = "",
    initial_context: str = "",
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
    if mission_tag:
        lines.append(f"[mission: {_sanitize_header_val(mission_tag)}]")
    if initial_context:
        lines.append(initial_context.strip())
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
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a child process environment with bridge/Telegram/watcher secrets removed."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(_SENSITIVE_ENV_PREFIXES) and k != "AGY_WATCHER_BRIEFING"
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
    if extra_env:
        for k, v in extra_env.items():
            env[str(k)] = str(v)
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


@dataclass(frozen=True)
class _CliSettings:
    agy_bin: str
    agy_model: str
    alpha_workspace: Path
    agy_timeout_seconds: int


class AgyRunner:
    def __init__(self, config: BridgeConfig | _CliSettings) -> None:
        self.config = config

    @classmethod
    def from_cli_settings(
        cls,
        *,
        agy_bin: str,
        agy_model: str = "",
        alpha_workspace: Path,
        agy_timeout_seconds: int = 150,
    ) -> "AgyRunner":
        return cls(
            _CliSettings(
                agy_bin=agy_bin,
                agy_model=agy_model,
                alpha_workspace=alpha_workspace,
                agy_timeout_seconds=agy_timeout_seconds,
            )
        )

    def _terminate_proc(self, proc: subprocess.Popen) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass

    def _run_stream_json(
        self,
        proc: subprocess.Popen,
        *,
        conversation_id: str | None,
        idle_timeout_seconds: float,
        hard_timeout_seconds: float | None,
    ) -> AgyResult:
        """Read stream-json NDJSON lines resetting idle timer on every progress event."""
        line_q: queue.Queue[str | None] = queue.Queue()
        stderr_chunks: list[str] = []

        def _read_stdout() -> None:
            assert proc.stdout is not None
            try:
                for raw in proc.stdout:
                    line_q.put(raw)
            finally:
                line_q.put(None)

        def _read_stderr() -> None:
            assert proc.stderr is not None
            try:
                stderr_chunks.append(proc.stderr.read())
            except Exception:
                pass

        t_out = threading.Thread(target=_read_stdout, daemon=True)
        t_err = threading.Thread(target=_read_stderr, daemon=True)
        t_out.start()
        t_err.start()

        def _cleanup_pipes() -> None:
            t_out.join(timeout=2)
            t_err.join(timeout=2)
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except Exception:
                        pass

        start_mono = time.monotonic()
        last_activity = start_mono
        hard_deadline = (
            start_mono + hard_timeout_seconds if hard_timeout_seconds is not None else None
        )
        seen_conv_id = conversation_id or ""
        result_payload: dict[str, Any] | None = None
        last_json_obj: dict[str, Any] | None = None
        eof = False

        while not eof:
            now = time.monotonic()
            idle_rem = idle_timeout_seconds - (now - last_activity)
            if idle_rem <= 0:
                self._terminate_proc(proc)
                _cleanup_pipes()
                return AgyResult(
                    conversation_id=seen_conv_id,
                    status="TIMEOUT",
                    response="",
                    error=f"agy idle timeout ({int(idle_timeout_seconds)}s without progress)",
                    stderr="".join(stderr_chunks).strip(),
                )
            wait_sec = min(idle_rem, 1.0)
            if hard_deadline is not None:
                hard_rem = hard_deadline - now
                if hard_rem <= 0:
                    self._terminate_proc(proc)
                    _cleanup_pipes()
                    return AgyResult(
                        conversation_id=seen_conv_id,
                        status="TIMEOUT",
                        response="",
                        error=f"agy exceeded hard ceiling ({int(hard_timeout_seconds or 0)}s)",
                        stderr="".join(stderr_chunks).strip(),
                    )
                wait_sec = min(wait_sec, hard_rem)

            try:
                item = line_q.get(timeout=max(0.05, wait_sec))
            except queue.Empty:
                continue
            if item is None:
                eof = True
                break
            line = item.strip()
            if not line:
                continue
            last_activity = time.monotonic()
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            last_json_obj = obj
            if obj.get("conversation_id"):
                seen_conv_id = str(obj["conversation_id"])
            if obj.get("event") == "result" and isinstance(obj.get("result"), dict):
                result_payload = obj["result"]
                if result_payload.get("conversation_id"):
                    seen_conv_id = str(result_payload["conversation_id"])

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._terminate_proc(proc)
        _cleanup_pipes()
        stderr = "".join(stderr_chunks).strip()

        payload = result_payload or (
            last_json_obj if isinstance(last_json_obj, dict) and "status" in last_json_obj else None
        )
        if payload is None:
            return AgyResult(
                conversation_id=seen_conv_id,
                status="ERROR",
                response="",
                error=stderr or f"agy stream-json ended without result (rc={proc.returncode})",
                stderr=stderr,
            )

        s_out = payload.get("structured_output")
        raw_dur = payload.get("duration_seconds")
        dur = float(raw_dur) if isinstance(raw_dur, (int, float)) else None
        return AgyResult(
            conversation_id=str(payload.get("conversation_id") or seen_conv_id),
            status=str(payload.get("status") or ("SUCCESS" if proc.returncode == 0 else "ERROR")),
            response=str(payload.get("response") or "").strip(),
            error=str(payload.get("error") or (stderr if proc.returncode != 0 else "")).strip(),
            stderr=stderr,
            structured_output=s_out if isinstance(s_out, dict) else None,
            duration_seconds=dur,
        )

    def run_prompt(
        self,
        prompt: str,
        *,
        conversation_id: str | None = None,
        workspace_dir: Path | None = None,
        timeout_seconds: int | None = None,
        idle_timeout_seconds: float | None = None,
        binding_id: int | None = None,
        bridge_origin: bool = True,
        extra_env: dict[str, str] | None = None,
        json_schema: str | None = None,
        settings_file: Path | None = None,
        effort: str = "",
        create_workspace: bool = True,
        on_process_start: Callable[[subprocess.Popen], None] | None = None,
        on_process_end: Callable[[subprocess.Popen], None] | None = None,
    ) -> AgyResult:
        cwd = workspace_dir or self.config.alpha_workspace
        if create_workspace:
            cwd.mkdir(parents=True, exist_ok=True)

        eff_timeout = (
            self.config.agy_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        use_stream = idle_timeout_seconds is not None and idle_timeout_seconds > 0
        cmd = [
            self.config.agy_bin,
            "--output-format",
            "stream-json" if use_stream else "json",
            "--disable-slash-commands",
        ]
        if conversation_id:
            cmd.extend(["--conversation", conversation_id])
        if self.config.agy_model:
            cmd.extend(["--model", self.config.agy_model])
        if effort:
            cmd.extend(["--effort", effort])
        if eff_timeout > 0:
            cmd.extend(["--print-timeout", f"{eff_timeout}s"])
        if json_schema:
            cmd.extend(["--json-schema", json_schema])
        if settings_file is not None:
            cmd.extend(["--settings", str(settings_file)])
        cmd.extend(["-p", prompt])

        env = build_scrubbed_env(
            self.config.agy_bin,
            bridge_origin=bridge_origin,
            binding_id=binding_id,
            extra_env=extra_env,
        )
        sub_timeout = eff_timeout + 15 if eff_timeout > 0 else None

        log.info(
            "Running agy -p (conv=%s, cwd=%s, len=%d, effort=%s, timeout=%s, idle=%s)",
            conversation_id or "<new>",
            cwd,
            len(prompt),
            effort or "default",
            sub_timeout,
            idle_timeout_seconds,
        )
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1 if use_stream else -1,
        )
        if on_process_start is not None:
            on_process_start(proc)
        try:
            if use_stream:
                assert idle_timeout_seconds is not None
                return self._run_stream_json(
                    proc,
                    conversation_id=conversation_id,
                    idle_timeout_seconds=idle_timeout_seconds,
                    hard_timeout_seconds=float(sub_timeout) if sub_timeout is not None else None,
                )
            try:
                stdout_raw, stderr_raw = proc.communicate(timeout=sub_timeout)
            except subprocess.TimeoutExpired:
                self._terminate_proc(proc)
                try:
                    stdout_raw, stderr_raw = proc.communicate(timeout=2)
                except Exception:
                    stdout_raw, stderr_raw = "", ""
                return AgyResult(
                    conversation_id=conversation_id or "",
                    status="TIMEOUT",
                    response="",
                    error=f"agy timed out after {sub_timeout}s",
                    stderr=(stderr_raw or "").strip(),
                )
        finally:
            if on_process_end is not None:
                on_process_end(proc)

        stdout = (stdout_raw or "").strip()
        stderr = (stderr_raw or "").strip()
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

        s_out = payload.get("structured_output")
        raw_dur = payload.get("duration_seconds")
        dur = float(raw_dur) if isinstance(raw_dur, (int, float)) else None
        return AgyResult(
            conversation_id=str(payload.get("conversation_id") or conversation_id or ""),
            status=str(payload.get("status") or ("SUCCESS" if proc.returncode == 0 else "ERROR")),
            response=str(payload.get("response") or "").strip(),
            error=str(payload.get("error") or (stderr if proc.returncode != 0 else "")).strip(),
            stderr=stderr,
            structured_output=s_out if isinstance(s_out, dict) else None,
            duration_seconds=dur,
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
