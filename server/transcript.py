# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Parser for Antigravity CLI transcript.jsonl / transcript_full.jsonl logs."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

_USER_REQUEST_RE = re.compile(
    r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL
)
_BRIDGE_HEADER_RE = re.compile(r"^\[channel:\s*[^\]]+\]\s*\n?", re.IGNORECASE)
_SAFE_CONV_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class TranscriptStep:
    step_index: int
    step_type: str  # "USER_INPUT" or "PLANNER_RESPONSE"
    text: str
    raw_content: str
    created_at: str


def is_safe_conversation_id(conversation_id: str | None) -> bool:
    return bool(
        conversation_id
        and ".." not in conversation_id
        and _SAFE_CONV_ID_RE.match(conversation_id)
    )


def extract_user_request_text(raw_content: str) -> str:
    """Extract clean user text from Antigravity's <USER_REQUEST> wrapper."""
    match = _USER_REQUEST_RE.search(raw_content)
    text = match.group(1) if match else raw_content
    return text.strip()


def strip_bridge_header(text: str) -> str:
    return _BRIDGE_HEADER_RE.sub("", text, count=1).strip()


def iter_transcript_entries(path: Path | None) -> Iterator[dict]:
    """Yield parsed JSON dict entries from a transcript JSONL file, skipping malformed lines."""
    if path is None or not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def resolve_transcript_file(
    transcript_path: Path | None = None,
    brain_dir: Path | None = None,
    conversation_id: str | None = None,
) -> Path | None:
    """Resolve transcript_full.jsonl / transcript.jsonl strictly contained in brain_dir."""
    candidates: list[Path] = []
    allowed_root: Path | None = None

    if brain_dir is not None and conversation_id:
        if not is_safe_conversation_id(conversation_id):
            return None
        logs_dir = brain_dir / conversation_id / ".system_generated" / "logs"
        allowed_root = logs_dir.resolve()
        if transcript_path is not None:
            try:
                resolved_tp = transcript_path.resolve()
                if resolved_tp.is_relative_to(allowed_root):
                    if resolved_tp.name == "transcript.jsonl":
                        candidates.append(resolved_tp.with_name("transcript_full.jsonl"))
                    candidates.append(resolved_tp)
            except OSError:
                pass
        candidates.append(logs_dir / "transcript_full.jsonl")
        candidates.append(logs_dir / "transcript.jsonl")
    elif transcript_path is not None and brain_dir is None:
        # Standalone helper usage (e.g., direct path in tests)
        if transcript_path.name == "transcript.jsonl":
            candidates.append(transcript_path.with_name("transcript_full.jsonl"))
        candidates.append(transcript_path)

    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if allowed_root is not None and not resolved.is_relative_to(allowed_root):
            continue
        if resolved.is_file():
            return resolved
    return None


def read_steps_after(path: Path, after_step_index: int = -1) -> list[TranscriptStep]:
    """Return DONE USER_INPUT and non-empty PLANNER_RESPONSE steps after watermark."""
    steps: list[TranscriptStep] = []
    for obj in iter_transcript_entries(path):
        step_idx = obj.get("step_index")
        if not isinstance(step_idx, int) or step_idx <= after_step_index:
            continue
        if obj.get("status") != "DONE":
            continue

        step_type = obj.get("type", "")
        if step_type not in ("USER_INPUT", "PLANNER_RESPONSE"):
            continue
        raw = obj.get("content") or obj.get("response") or ""
        if not isinstance(raw, str):
            continue

        clean = (
            extract_user_request_text(raw)
            if step_type == "USER_INPUT"
            else raw.strip()
        )
        if clean:
            steps.append(
                TranscriptStep(
                    step_index=step_idx,
                    step_type=step_type,
                    text=clean,
                    raw_content=raw,
                    created_at=str(obj.get("created_at", "")),
                )
            )

    steps.sort(key=lambda s: s.step_index)
    return steps


def get_latest_step_index(path: Path) -> int:
    """Return the highest step_index present in a transcript file, or -1."""
    latest = -1
    for obj in iter_transcript_entries(path):
        idx = obj.get("step_index")
        if isinstance(idx, int) and idx > latest:
            latest = idx
    return latest
