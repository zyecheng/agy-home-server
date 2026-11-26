# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""SQLite (WAL) store for channel <-> Antigravity conversation bindings."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

VALID_MIRROR_MODES = ("full", "replies_only", "muted")


@dataclass
class Binding:
    id: int
    channel: str
    chat_id: str
    thread_id: int
    chat_type: str
    topic_name: str
    agy_conversation_id: str | None
    workspace_dir: str
    mirror_mode: str
    last_mirrored_step: int
    active: bool
    created_at: float
    updated_at: float


class BridgeStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    thread_id INTEGER NOT NULL DEFAULT 0,
                    chat_type TEXT NOT NULL DEFAULT 'private',
                    topic_name TEXT NOT NULL DEFAULT '',
                    agy_conversation_id TEXT,
                    workspace_dir TEXT NOT NULL,
                    mirror_mode TEXT NOT NULL DEFAULT 'full',
                    last_mirrored_step INTEGER NOT NULL DEFAULT -1,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_bindings_active_endpoint
                    ON bindings(channel, chat_id, thread_id)
                    WHERE active = 1;

                CREATE INDEX IF NOT EXISTS idx_bindings_conv_active
                    ON bindings(agy_conversation_id)
                    WHERE active = 1 AND agy_conversation_id IS NOT NULL;

                CREATE TABLE IF NOT EXISTS message_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    binding_id INTEGER NOT NULL REFERENCES bindings(id),
                    direction TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    external_message_id TEXT,
                    agy_step_index INTEGER,
                    content_preview TEXT NOT NULL DEFAULT '',
                    attachments_json TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_message_log_binding_step
                    ON message_log(binding_id, agy_step_index);

                CREATE TABLE IF NOT EXISTS kv_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _row_to_binding(row: sqlite3.Row | None) -> Binding | None:
        if row is None:
            return None
        return Binding(
            id=int(row["id"]),
            channel=row["channel"],
            chat_id=row["chat_id"],
            thread_id=int(row["thread_id"]),
            chat_type=row["chat_type"],
            topic_name=row["topic_name"],
            agy_conversation_id=row["agy_conversation_id"],
            workspace_dir=row["workspace_dir"],
            mirror_mode=row["mirror_mode"],
            last_mirrored_step=int(row["last_mirrored_step"]),
            active=bool(row["active"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def get_kv(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM kv_state WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO kv_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_active_binding(
        self, channel: str, chat_id: str | int, thread_id: int = 0
    ) -> Binding | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM bindings "
                "WHERE channel = ? AND chat_id = ? AND thread_id = ? AND active = 1",
                (channel, str(chat_id), int(thread_id)),
            ).fetchone()
            return self._row_to_binding(row)

    def get_or_create_binding(
        self,
        channel: str,
        chat_id: str | int,
        thread_id: int,
        chat_type: str,
        workspace_dir: str,
        topic_name: str = "",
        default_mirror_mode: str = "full",
    ) -> Binding:
        with self._lock, self._conn:
            existing = self.get_active_binding(channel, chat_id, thread_id)
            now = time.time()
            if existing is not None:
                if topic_name and topic_name != existing.topic_name:
                    self._conn.execute(
                        "UPDATE bindings SET topic_name = ?, updated_at = ? WHERE id = ?",
                        (topic_name, now, existing.id),
                    )
                    existing.topic_name = topic_name
                    existing.updated_at = now
                return existing

            cur = self._conn.execute(
                "INSERT INTO bindings "
                "(channel, chat_id, thread_id, chat_type, topic_name, workspace_dir, mirror_mode, active, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    channel,
                    str(chat_id),
                    int(thread_id),
                    chat_type,
                    topic_name,
                    workspace_dir,
                    default_mirror_mode,
                    now,
                    now,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM bindings WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            binding = self._row_to_binding(row)
            assert binding is not None
            return binding

    def get_binding_by_conversation(self, agy_conversation_id: str) -> Binding | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM bindings "
                "WHERE agy_conversation_id = ? AND active = 1 "
                "ORDER BY updated_at DESC LIMIT 1",
                (agy_conversation_id,),
            ).fetchone()
            return self._row_to_binding(row)

    def get_primary_dm_binding(self, channel: str = "telegram") -> Binding | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM bindings "
                "WHERE channel = ? AND chat_type = 'private' AND active = 1 "
                "ORDER BY updated_at DESC LIMIT 1",
                (channel,),
            ).fetchone()
            return self._row_to_binding(row)

    def list_active_bindings(self) -> list[Binding]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM bindings WHERE active = 1 ORDER BY updated_at DESC"
            ).fetchall()
            return [b for r in rows if (b := self._row_to_binding(r)) is not None]

    def set_conversation_id(self, binding_id: int, agy_conversation_id: str) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE bindings SET agy_conversation_id = ?, updated_at = ? WHERE id = ?",
                (agy_conversation_id, now, binding_id),
            )

    def update_watermark(self, binding_id: int, step_index: int) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE bindings "
                "SET last_mirrored_step = MAX(last_mirrored_step, ?), updated_at = ? "
                "WHERE id = ?",
                (int(step_index), now, binding_id),
            )

    def set_mirror_mode(self, binding_id: int, mode: str) -> None:
        if mode not in VALID_MIRROR_MODES:
            raise ValueError(f"Invalid mirror mode: {mode}")
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE bindings SET mirror_mode = ?, updated_at = ? WHERE id = ?",
                (mode, now, binding_id),
            )

    def archive_binding(self, binding_id: int) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE bindings SET active = 0, updated_at = ? WHERE id = ?",
                (now, binding_id),
            )

    def log_message(
        self,
        binding_id: int,
        direction: str,
        origin: str,
        external_message_id: str | None = None,
        agy_step_index: int | None = None,
        content_preview: str = "",
        attachments: list[dict] | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO message_log "
                "(binding_id, direction, origin, external_message_id, agy_step_index, content_preview, attachments_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    binding_id,
                    direction,
                    origin,
                    external_message_id,
                    agy_step_index,
                    content_preview[:500],
                    json.dumps(attachments or []),
                    time.time(),
                ),
            )
