# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Secure Telegram multimodal attachment extraction, staging, and TTL pruning."""

from __future__ import annotations

import logging
import mimetypes
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .telegram_client import FileTooLargeError, TelegramClient
from .transcript import is_safe_conversation_id

log = logging.getLogger("agy-bridge.attachments")

DEFAULT_MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024  # 15 MiB (Telegram Bot API getFile cap is 20 MiB)
DEFAULT_INBOX_RETENTION_DAYS = 7

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,12}$")
_KIND_DEFAULT_EXT = {
    "photo": ".jpg",
    "voice": ".ogg",
    "audio": ".mp3",
    "video": ".mp4",
    "video_note": ".mp4",
    "sticker": ".webp",
    "document": ".bin",
}


@dataclass(frozen=True)
class PendingMedia:
    kind: str
    file_id: str
    file_name: str = ""
    mime_type: str = ""
    file_size: int | None = None
    duration: int | None = None


@dataclass(frozen=True)
class StagedAttachment:
    kind: str
    path: Path
    size_bytes: int
    mime_type: str = ""
    original_name: str = ""

    def to_log_dict(self) -> dict:
        return {
            "kind": self.kind,
            "path": str(self.path),
            "size_bytes": self.size_bytes,
            "mime_type": self.mime_type,
            "original_name": self.original_name,
        }


def extract_telegram_media(msg: dict) -> tuple[list[PendingMedia], str]:
    """Extract downloadable media descriptors and optional location text from a Telegram message."""
    items: list[PendingMedia] = []
    notes: list[str] = []

    photos = msg.get("photo")
    if isinstance(photos, list) and photos:
        best = max(
            (p for p in photos if isinstance(p, dict) and p.get("file_id")),
            key=lambda p: (
                int(p.get("file_size") or 0),
                int(p.get("width") or 0) * int(p.get("height") or 0),
            ),
            default=None,
        )
        if best is not None:
            items.append(
                PendingMedia(
                    kind="photo",
                    file_id=str(best["file_id"]),
                    file_name="photo.jpg",
                    mime_type="image/jpeg",
                    file_size=int(best["file_size"]) if best.get("file_size") is not None else None,
                )
            )

    for key, kind in (
        ("document", "document"),
        ("voice", "voice"),
        ("audio", "audio"),
        ("video", "video"),
        ("video_note", "video_note"),
        ("sticker", "sticker"),
    ):
        obj = msg.get(key)
        if isinstance(obj, dict) and obj.get("file_id"):
            items.append(
                PendingMedia(
                    kind=kind,
                    file_id=str(obj["file_id"]),
                    file_name=str(obj.get("file_name") or ""),
                    mime_type=str(obj.get("mime_type") or ""),
                    file_size=int(obj["file_size"]) if obj.get("file_size") is not None else None,
                    duration=int(obj["duration"]) if obj.get("duration") is not None else None,
                )
            )

    loc = msg.get("location")
    if isinstance(loc, dict) and loc.get("latitude") is not None and loc.get("longitude") is not None:
        lat = float(loc["latitude"])
        lon = float(loc["longitude"])
        loc_parts = [f"lat={lat:.6f}", f"lon={lon:.6f}"]
        if loc.get("horizontal_accuracy") is not None:
            loc_parts.append(f"accuracy_m={float(loc['horizontal_accuracy']):.0f}")
        venue = msg.get("venue")
        if isinstance(venue, dict):
            title = str(venue.get("title") or "").strip()
            addr = str(venue.get("address") or "").strip()
            if title:
                loc_parts.append(f"venue={title}")
            if addr:
                loc_parts.append(f"address={addr}")
        notes.append("[location: " + ", ".join(loc_parts) + "]")

    return items, "\n".join(notes)


def sanitize_filename(
    original_name: str,
    *,
    kind: str,
    mime_type: str = "",
    remote_file_path: str = "",
) -> str:
    """Produce a safe basename (no traversal, no dotfiles, bounded length, valid extension)."""
    raw_base = (original_name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    stem = ""
    ext = ""
    if raw_base:
        p = Path(raw_base)
        stem = p.stem
        ext = p.suffix.lower()

    if not _SAFE_EXT_RE.match(ext):
        ext = ""
    if not ext and remote_file_path:
        r_ext = Path(remote_file_path.replace("\\", "/").rsplit("/", 1)[-1]).suffix.lower()
        if _SAFE_EXT_RE.match(r_ext):
            ext = r_ext
    if not ext and mime_type:
        guessed = mimetypes.guess_extension(mime_type.split(";", 1)[0].strip().lower()) or ""
        guessed = {".jpe": ".jpg", ".oga": ".ogg"}.get(guessed.lower(), guessed.lower())
        if _SAFE_EXT_RE.match(guessed):
            ext = guessed
    if not ext:
        ext = _KIND_DEFAULT_EXT.get(kind, ".bin")

    cleaned = _SAFE_NAME_RE.sub("_", stem).strip("._-")
    if not cleaned:
        cleaned = kind
    cleaned = cleaned[:64]
    return f"{cleaned}{ext}"


def _resolve_scope_dir(workspace_dir: Path, *, scope_name: str) -> Path:
    inbox_root = workspace_dir / "inbox"
    inbox_root.mkdir(parents=True, exist_ok=True)
    if inbox_root.is_symlink():
        raise ValueError("Refusing symlinked inbox directory")
    resolved_root = inbox_root.resolve()

    if not is_safe_conversation_id(scope_name):
        raise ValueError(f"Unsafe inbox scope name: {scope_name!r}")
    target = (resolved_root / scope_name).resolve()
    if not target.is_relative_to(resolved_root) or target == resolved_root:
        raise ValueError(f"Inbox scope escapes inbox root: {scope_name!r}")
    target.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError("Refusing symlinked inbox scope directory")
    return target


def stage_telegram_media(
    telegram: TelegramClient,
    media: PendingMedia,
    *,
    workspace_dir: Path,
    scope_name: str,
    message_id: str,
    max_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
) -> StagedAttachment:
    """Download one Telegram media item and atomically stage it under `<workspace>/inbox/<scope>/`."""
    if media.file_size is not None and media.file_size > max_bytes:
        raise FileTooLargeError(
            f"Attachment exceeds limit ({media.file_size:,} bytes > {max_bytes:,} bytes)"
        )

    file_info = telegram.get_file(media.file_id)
    remote_path = str(file_info.get("file_path") or "").strip()
    if not remote_path:
        raise RuntimeError("Telegram getFile response missing file_path")
    reported_size = file_info.get("file_size")
    if reported_size is not None and int(reported_size) > max_bytes:
        raise FileTooLargeError(
            f"Attachment exceeds limit ({int(reported_size):,} bytes > {max_bytes:,} bytes)"
        )

    data = telegram.download_file(remote_path, max_bytes=max_bytes)

    target_dir = _resolve_scope_dir(workspace_dir, scope_name=scope_name)
    safe_base = sanitize_filename(
        media.file_name,
        kind=media.kind,
        mime_type=media.mime_type,
        remote_file_path=remote_path,
    )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_mid = re.sub(r"[^A-Za-z0-9_-]+", "", str(message_id or "0"))[:16] or "0"
    nonce = secrets.token_hex(4)
    final_name = f"{stamp}-m{safe_mid}-{nonce}-{safe_base}"

    dest = (target_dir / final_name).resolve()
    if not dest.is_relative_to(target_dir):
        raise ValueError(f"Staged path escapes scope directory: {final_name!r}")

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(dest), flags, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except AttributeError:
            pass
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except Exception:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    return StagedAttachment(
        kind=media.kind,
        path=dest,
        size_bytes=len(data),
        mime_type=media.mime_type,
        original_name=media.file_name or safe_base,
    )


def prune_inbox(
    workspace_dir: Path,
    *,
    retention_days: int = DEFAULT_INBOX_RETENTION_DAYS,
    now_ts: float | None = None,
) -> int:
    """Delete staged inbox files older than retention_days and clean empty subdirectories."""
    if retention_days <= 0:
        return 0
    inbox_root = workspace_dir / "inbox"
    if not inbox_root.exists() or inbox_root.is_symlink():
        return 0

    cutoff = (time.time() if now_ts is None else now_ts) - (retention_days * 86400.0)
    removed = 0

    for root, dirs, files in os.walk(inbox_root, topdown=False, followlinks=False):
        root_path = Path(root)
        for fname in files:
            fpath = root_path / fname
            try:
                if fpath.is_symlink():
                    continue
                if fpath.stat().st_mtime < cutoff:
                    fpath.unlink()
                    removed += 1
            except OSError:
                continue
        if root_path != inbox_root:
            try:
                if not any(root_path.iterdir()):
                    root_path.rmdir()
            except OSError:
                pass

    if removed > 0:
        log.info("Pruned %d expired file(s) from %s (retention=%dd)", removed, inbox_root, retention_days)
    return removed
