"""Parser for Antigravity CLI transcript.jsonl / transcript_full.jsonl logs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_USER_REQUEST_RE = re.compile(
    r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL
)
_BRIDGE_HEADER_RE = re.compile(r"^\[channel:\s*[^\]]+\]\s*\n?", re.IGNORECASE)


@dataclass(frozen=True)
class TranscriptStep:
    step_index: int
    step_type: str  # "USER_INPUT" or "PLANNER_RESPONSE"
    text: str
    raw_content: str
    created_at: str


def extract_user_request_text(raw_content: str) -> str:
    """Extract clean user text from Antigravity's <USER_REQUEST> wrapper."""
    match = _USER_REQUEST_RE.search(raw_content)
    text = match.group(1) if match else raw_content
    return text.strip()


def strip_bridge_header(text: str) -> str:
    return _BRIDGE_HEADER_RE.sub("", text, count=1).strip()


def resolve_transcript_file(
    transcript_path: Path | None = None,
    brain_dir: Path | None = None,
    conversation_id: str | None = None,
) -> Path | None:
    """Prefer transcript_full.jsonl over transcript.jsonl to avoid truncated responses."""
    candidates: list[Path] = []
    if transcript_path is not None:
        if transcript_path.name == "transcript.jsonl":
            candidates.append(transcript_path.with_name("transcript_full.jsonl"))
        candidates.append(transcript_path)
    if brain_dir is not None and conversation_id:
        logs_dir = brain_dir / conversation_id / ".system_generated" / "logs"
        candidates.append(logs_dir / "transcript_full.jsonl")
        candidates.append(logs_dir / "transcript.jsonl")

    for path in candidates:
        if path.exists():
            return path
    return None


def read_steps_after(path: Path, after_step_index: int = -1) -> list[TranscriptStep]:
    """Return DONE USER_INPUT and non-empty PLANNER_RESPONSE steps after watermark."""
    steps: list[TranscriptStep] = []
    if not path.exists():
        return steps

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            step_idx = obj.get("step_index")
            if not isinstance(step_idx, int) or step_idx <= after_step_index:
                continue
            if obj.get("status") != "DONE":
                continue

            step_type = obj.get("type", "")
            raw = obj.get("content") or obj.get("response") or ""
            if not isinstance(raw, str):
                continue

            if step_type == "USER_INPUT":
                clean = extract_user_request_text(raw)
                if clean:
                    steps.append(
                        TranscriptStep(
                            step_index=step_idx,
                            step_type="USER_INPUT",
                            text=clean,
                            raw_content=raw,
                            created_at=str(obj.get("created_at", "")),
                        )
                    )
            elif step_type == "PLANNER_RESPONSE":
                clean = raw.strip()
                if clean:
                    steps.append(
                        TranscriptStep(
                            step_index=step_idx,
                            step_type="PLANNER_RESPONSE",
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
    if not path.exists():
        return latest
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            idx = obj.get("step_index")
            if isinstance(idx, int) and idx > latest:
                latest = idx
    return latest
