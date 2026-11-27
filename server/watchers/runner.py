# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Scheduler & two-phase delivery engine for agy-watcher.service (`python3 -m server.watchers.runner`)."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..agy_runner import AgyRunner
from ..config import WatcherRuntimeConfig, post_bridge_ipc, resolve_env_file
from .collectors import (
    CollectResult,
    collect_github_releases,
    collect_http_diff,
    collect_rss,
    collect_web_scout,
)
from .schema import (
    MAX_KNOWN_ENTITIES,
    MAX_PENDING_ITEMS,
    MAX_SEEN_IDS,
    MissionValidationError,
    atomic_write_json,
    is_valid_mission_id,
    list_missions,
    load_mission,
    load_safe_state,
    parse_iso_timestamp,
    pending_path,
    safe_read_json,
    state_lock_path,
    state_path,
    validate_mission_id,
)

try:
    import fcntl
except ImportError:  # Windows unit tests
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger("agy-bridge.watchers.runner")

MAX_WEB_SCOUTS_PER_TICK = 3
MAX_BACKOFF_SECONDS = 24 * 3600
WATCHER_IPC_TIMEOUT_SECONDS = 125.0
_SAFE_ERR_RE = re.compile(r"[^A-Za-z0-9_.:/\- ]+")


def _sanitize_error_code(exc: Exception | str) -> str:
    raw = str(exc or type(exc).__name__)
    cleaned = _SAFE_ERR_RE.sub(" ", raw).strip()
    return (cleaned or "collector_error")[:120]


def _append_seen_ids(existing: list[str], new_ids: list[str]) -> list[str]:
    return list(dict.fromkeys([*existing, *new_ids]))[-MAX_SEEN_IDS:]


def _append_entities(
    existing: list[dict[str, Any]], new_entities: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    out = [e for e in existing if isinstance(e, dict)]
    keys = {e.get("key") for e in out}
    for ent in new_entities:
        if isinstance(ent, dict) and ent.get("key") not in keys:
            out.append(ent)
            keys.add(ent.get("key"))
    return out[-MAX_KNOWN_ENTITIES:]


def _dedupe_items(
    existing: list[dict[str, Any]], new_items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    out = [it for it in existing if isinstance(it, dict)]
    seen = {
        str(it.get("id") or f"{it.get('key', '')}:{it.get('url', '')}:{it.get('title', '')}")
        for it in out
    }
    for it in new_items:
        if not isinstance(it, dict):
            continue
        k = str(it.get("id") or f"{it.get('key', '')}:{it.get('url', '')}:{it.get('title', '')}")
        if k not in seen:
            out.append(it)
            seen.add(k)
    return out[-MAX_PENDING_ITEMS:]


def _get_tz(tz_name: str):
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now().astimezone().tzinfo


def _parse_hhmm(hhmm: str) -> int:
    h, m = hhmm.split(":", 1)
    return int(h) * 60 + int(m)


def is_in_quiet_hours(
    mission: dict[str, Any],
    *,
    now_ts: float,
    timezone_name: str,
    default_start: str,
    default_end: str,
) -> bool:
    """Return True if non-urgent delivery should be held until quiet hours end."""
    notify = mission.get("notify") or {}
    if str(notify.get("priority") or "normal") == "urgent":
        return False

    qh = notify.get("quiet_hours")
    if isinstance(qh, dict):
        if qh.get("disabled"):
            return False
        start_str = str(qh.get("start") or default_start)
        end_str = str(qh.get("end") or default_end)
    else:
        start_str = default_start
        end_str = default_end

    if not start_str or not end_str or start_str == end_str:
        return False

    dt = datetime.fromtimestamp(now_ts, tz=_get_tz(timezone_name))
    cur_min = dt.hour * 60 + dt.minute
    start_min = _parse_hhmm(start_str)
    end_min = _parse_hhmm(end_str)
    if start_min < end_min:
        return start_min <= cur_min < end_min
    return cur_min >= start_min or cur_min < end_min


@contextlib.contextmanager
def _file_lock(lock_file: Path, *, blocking: bool = False) -> Iterator[bool]:
    """Acquire an advisory flock; yields True if acquired, False if busy."""
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    acquired = False
    try:
        if fcntl is not None:
            flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                fcntl.flock(fd, flags)
                acquired = True
            except BlockingIOError:
                acquired = False
        else:
            acquired = True
        yield acquired
    finally:
        if acquired and fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


class WatcherRunner:
    def __init__(
        self,
        config: WatcherRuntimeConfig,
        *,
        allow_loopback_for_tests: bool = False,
        agy_runner: AgyRunner | None = None,
    ) -> None:
        self.config = config
        self.allow_loopback_for_tests = allow_loopback_for_tests
        self.agy_runner = agy_runner or AgyRunner.from_cli_settings(
            agy_bin=config.agy_bin,
            agy_model=config.agy_model,
            alpha_workspace=config.scout_sandbox_dir,
            agy_timeout_seconds=150,
        )

    def _today_str(self, now_ts: float) -> str:
        return datetime.fromtimestamp(now_ts, tz=_get_tz(self.config.timezone_name)).strftime(
            "%Y-%m-%d"
        )

    def _consume_daily_web_scout_quota(self, now_ts: float) -> bool:
        """Atomically check & increment daily web_scout run counter in _runner_meta.json."""
        meta_file = self.config.state_dir / "_runner_meta.json"
        lock_file = self.config.state_dir / "_runner_meta.lock"
        today = self._today_str(now_ts)
        with _file_lock(lock_file, blocking=True) as ok:
            if not ok:
                return False
            try:
                meta = safe_read_json(meta_file, self.config.state_dir) or {}
            except MissionValidationError:
                meta = {}
            if meta.get("date") != today:
                meta = {"date": today, "web_scout_runs": 0}
            used = int(meta.get("web_scout_runs") or 0)
            if used >= self.config.max_daily_web_scouts:
                return False
            meta["web_scout_runs"] = used + 1
            atomic_write_json(meta_file, self.config.state_dir, meta)
            return True

    def drain_triggers(self) -> list[str]:
        """Unlink trigger files at intake while skipping in-flight atomic temp files."""
        tdir = self.config.triggers_dir
        if not tdir.exists() or tdir.is_symlink():
            return []
        triggered: list[str] = []
        seen: set[str] = set()
        for entry in sorted(tdir.iterdir()):
            name = entry.name
            if name.startswith(".") and name.endswith(".tmp"):
                continue
            try:
                entry.unlink(missing_ok=True)
            except OSError:
                continue
            if not name.endswith(".req"):
                continue
            mid = name[:-4]
            if is_valid_mission_id(mid) and mid not in seen:
                seen.add(mid)
                triggered.append(mid)
        return triggered

    def _load_pending(self, mission_id: str) -> dict[str, Any]:
        pfile = pending_path(self.config.state_dir, mission_id)
        try:
            raw = safe_read_json(pfile, self.config.state_dir)
        except MissionValidationError:
            return {"items": [], "new_seen_ids": [], "new_entities": []}
        if not raw:
            return {"items": [], "new_seen_ids": [], "new_entities": []}
        return {
            "items": list(raw.get("items") or [])[:MAX_PENDING_ITEMS],
            "new_seen_ids": list(raw.get("new_seen_ids") or []),
            "new_entities": list(raw.get("new_entities") or []),
        }

    def _save_pending(self, mission_id: str, pending: dict[str, Any]) -> None:
        pfile = pending_path(self.config.state_dir, mission_id)
        if not pending.get("items"):
            try:
                pfile.unlink(missing_ok=True)
            except OSError:
                pass
            return
        trimmed = {
            "items": list(pending.get("items") or [])[-MAX_PENDING_ITEMS:],
            "new_seen_ids": list(pending.get("new_seen_ids") or [])[-MAX_SEEN_IDS:],
            "new_entities": list(pending.get("new_entities") or [])[-MAX_KNOWN_ENTITIES:],
        }
        atomic_write_json(pfile, self.config.state_dir, trimmed)

    def _post_watcher(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return post_bridge_ipc(
            self.config.ipc_host,
            self.config.ipc_port,
            self.config.watcher_token_path,
            "/watcher",
            payload,
            timeout=WATCHER_IPC_TIMEOUT_SECONDS,
        )

    def _commit_delivered(
        self,
        mission_id: str,
        state: dict[str, Any],
        pending: dict[str, Any],
        *,
        status: str,
        now_ts: float,
    ) -> None:
        state["seen_ids"] = _append_seen_ids(
            list(state.get("seen_ids") or []),
            list(pending.get("new_seen_ids") or []),
        )
        state["known_entities"] = _append_entities(
            list(state.get("known_entities") or []),
            list(pending.get("new_entities") or []),
        )
        state["pending_count"] = 0
        state["next_flush_at"] = 0.0
        state["consecutive_failures"] = 0
        state["last_error"] = ""
        if status == "delivered":
            state["last_notified_at"] = now_ts
        # Persist state.json BEFORE unlinking pending.json so a crash never loses committed IDs
        atomic_write_json(state_path(self.config.state_dir, mission_id), self.config.state_dir, state)
        self._save_pending(mission_id, {"items": [], "new_seen_ids": [], "new_entities": []})

    def _attempt_flush(
        self,
        mission: dict[str, Any],
        state: dict[str, Any],
        *,
        now_ts: float,
        force: bool = False,
    ) -> str:
        """Attempt two-phase delivery of pending items. Returns outcome string."""
        mid = str(mission["id"])
        pending = self._load_pending(mid)
        items = pending.get("items") or []
        if not items:
            state["pending_count"] = 0
            state["next_flush_at"] = 0.0
            return "empty"

        if not force and is_in_quiet_hours(
            mission,
            now_ts=now_ts,
            timezone_name=self.config.timezone_name,
            default_start=self.config.quiet_start,
            default_end=self.config.quiet_end,
        ):
            state["pending_count"] = len(items)
            state["next_flush_at"] = 0.0  # Ready immediately when quiet hours end
            return "quiet_held"

        code, body = self._post_watcher(
            {
                "mission_id": mid,
                "event_type": "delta",
                "items": items,
            }
        )
        delivery_status = str(body.get("status") or "")
        if code == 200 and delivery_status in ("delivered", "suppressed"):
            self._commit_delivered(mid, state, pending, status=delivery_status, now_ts=now_ts)
            return delivery_status

        # Delivery failed: keep pending.json intact, do NOT advance seen_ids, and apply backoff
        failures = int(state.get("consecutive_failures") or 0) + 1
        state["pending_count"] = len(items)
        state["consecutive_failures"] = failures
        state["last_error"] = _sanitize_error_code(
            body.get("detail") or body.get("error") or f"ipc_http_{code}"
        )
        state["next_flush_at"] = self._compute_next_run(mission, now_ts=now_ts, failures=failures)
        return "delivery_failed"

    def _compute_next_run(
        self, mission: dict[str, Any], *, now_ts: float, failures: int
    ) -> float:
        base_sec = max(60, int((mission.get("schedule") or {}).get("interval_minutes") or 60) * 60)
        if failures <= 0:
            return now_ts + base_sec
        backoff = min(base_sec * (2 ** min(failures, 6)), MAX_BACKOFF_SECONDS)
        return now_ts + backoff

    def _run_collector(self, mission: dict[str, Any], state: dict[str, Any]) -> CollectResult:
        kind = str(mission["kind"])
        if kind == "rss":
            return collect_rss(
                mission, state, allow_loopback_for_tests=self.allow_loopback_for_tests
            )
        if kind == "github_releases":
            return collect_github_releases(
                mission, state, allow_loopback_for_tests=self.allow_loopback_for_tests
            )
        if kind == "http_diff":
            return collect_http_diff(
                mission, state, allow_loopback_for_tests=self.allow_loopback_for_tests
            )
        if kind == "web_scout":
            return collect_web_scout(
                mission,
                state,
                agy_runner=self.agy_runner,
                scout_sandbox_dir=self.config.scout_sandbox_dir,
                brain_dir=self.config.agy_brain_dir,
            )
        raise MissionValidationError(f"Unsupported mission kind: {kind}")

    def run_single_mission(
        self,
        mission_id: str,
        *,
        now_ts: float | None = None,
        force: bool = False,
        scout_budget_remaining: list[int] | None = None,
    ) -> dict[str, Any]:
        """Process flush, lifecycle checks, and collection for one mission under per-mission flock."""
        now = time.time() if now_ts is None else now_ts
        mid = validate_mission_id(mission_id)
        try:
            mission = load_mission(self.config.missions_dir, mid, require_runnable=True)
        except MissionValidationError as exc:
            log.warning("Skipping invalid mission %s: %s", mid, exc)
            return {"mission_id": mid, "outcome": "invalid", "detail": str(exc)}
        if mission is None or mission["status"] != "active":
            return {"mission_id": mid, "outcome": "inactive"}

        lpath = state_lock_path(self.config.state_dir, mid)
        with _file_lock(lpath, blocking=False) as acquired:
            if not acquired:
                return {"mission_id": mid, "outcome": "locked"}

            state = load_safe_state(self.config.state_dir, mission)
            sfile = state_path(self.config.state_dir, mid)

            # 1. Expiry check (honoring backoff after previous expiry_failed)
            exp_ts = parse_iso_timestamp((mission.get("lifecycle") or {}).get("expires_at"))
            if exp_ts is not None and now >= exp_ts:
                if (
                    not force
                    and int(state.get("consecutive_failures") or 0) > 0
                    and now < float(state.get("next_run_at") or 0.0)
                ):
                    return {"mission_id": mid, "outcome": "expiry_backoff"}
                code, body = self._post_watcher({"mission_id": mid, "event_type": "expired"})
                if code == 200 and body.get("status") in ("delivered", "archived"):
                    state["last_run_at"] = now
                    atomic_write_json(sfile, self.config.state_dir, state)
                    return {"mission_id": mid, "outcome": "expired"}
                state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
                state["last_error"] = _sanitize_error_code(
                    body.get("detail") or body.get("error") or f"expiry_http_{code}"
                )
                state["next_run_at"] = self._compute_next_run(
                    mission, now_ts=now, failures=state["consecutive_failures"]
                )
                atomic_write_json(sfile, self.config.state_dir, state)
                return {"mission_id": mid, "outcome": "expiry_failed"}

            # 2. Independent pending delivery flush (decoupled from next_run_at, honors next_flush_at backoff!)
            flush_outcome = "empty"
            has_pending = (
                int(state.get("pending_count") or 0) > 0
                or pending_path(self.config.state_dir, mid).exists()
            )
            if has_pending:
                next_flush_at = float(state.get("next_flush_at") or 0.0)
                if not force and next_flush_at > 0.0 and now < next_flush_at:
                    return {"mission_id": mid, "outcome": "flush_backoff"}
                flush_outcome = self._attempt_flush(mission, state, now_ts=now, force=force)
                if flush_outcome == "delivery_failed":
                    state["next_run_at"] = float(state["next_flush_at"])
                    atomic_write_json(sfile, self.config.state_dir, state)
                    return {"mission_id": mid, "outcome": "delivery_failed"}

            # 3. Collection due check
            next_run_at = float(state.get("next_run_at") or 0.0)
            is_due = force or now >= next_run_at
            if not is_due:
                if flush_outcome not in ("empty", "delivered", "suppressed"):
                    atomic_write_json(sfile, self.config.state_dir, state)
                return {
                    "mission_id": mid,
                    "outcome": f"flushed:{flush_outcome}" if flush_outcome != "empty" else "not_due",
                }

            if mission["kind"] == "web_scout":
                if scout_budget_remaining is not None and scout_budget_remaining[0] <= 0:
                    return {"mission_id": mid, "outcome": "tick_budget_deferred"}
                if not self._consume_daily_web_scout_quota(now):
                    state["last_error"] = "daily_web_scout_quota_reached"
                    state["next_run_at"] = now + 3600
                    atomic_write_json(sfile, self.config.state_dir, state)
                    return {"mission_id": mid, "outcome": "daily_quota_exceeded"}
                if scout_budget_remaining is not None:
                    scout_budget_remaining[0] -= 1

            # Merge pending seen_ids/known_entities into collector view so quiet-hours ticks never duplicate items
            existing_pending = self._load_pending(mid)
            collector_state = dict(state)
            if existing_pending["new_seen_ids"]:
                collector_state["seen_ids"] = _append_seen_ids(
                    list(state.get("seen_ids") or []), existing_pending["new_seen_ids"]
                )
            if existing_pending["new_entities"]:
                collector_state["known_entities"] = _append_entities(
                    list(state.get("known_entities") or []), existing_pending["new_entities"]
                )

            try:
                collected = self._run_collector(mission, collector_state)
            except Exception as exc:
                log.warning("Collector failed for %s: %s", mid, exc)
                failures = int(state.get("consecutive_failures") or 0) + 1
                state["last_run_at"] = now
                state["consecutive_failures"] = failures
                state["last_error"] = _sanitize_error_code(exc)
                state["next_run_at"] = self._compute_next_run(mission, now_ts=now, failures=failures)
                atomic_write_json(sfile, self.config.state_dir, state)
                return {"mission_id": mid, "outcome": "collector_failed", "detail": state["last_error"]}

            state["last_run_at"] = now
            state["next_run_at"] = self._compute_next_run(mission, now_ts=now, failures=0)
            if collected.http_etag:
                state["http_etag"] = collected.http_etag
            if collected.http_last_modified:
                state["http_last_modified"] = collected.http_last_modified
            if collected.last_hash:
                state["last_hash"] = collected.last_hash

            if collected.items:
                # Two-phase durability: persist deduplicated pending items BEFORE calling POST /watcher
                merged_items = _dedupe_items(existing_pending["items"], collected.items)
                merged_seen = _append_seen_ids(
                    existing_pending["new_seen_ids"], collected.new_seen_ids
                )
                merged_ents = _append_entities(
                    existing_pending["new_entities"], collected.new_entities
                )
                self._save_pending(
                    mid,
                    {
                        "items": merged_items,
                        "new_seen_ids": merged_seen,
                        "new_entities": merged_ents,
                    },
                )
                state["pending_count"] = len(merged_items)
                atomic_write_json(sfile, self.config.state_dir, state)

                outcome = self._attempt_flush(mission, state, now_ts=now, force=force)
                if outcome == "delivery_failed":
                    state["next_run_at"] = float(state["next_flush_at"])
                if outcome not in ("delivered", "suppressed"):
                    atomic_write_json(sfile, self.config.state_dir, state)
                return {"mission_id": mid, "outcome": outcome, "items": len(collected.items)}

            # No matching items to notify: commit any filtered-out seen_ids cleanly
            if collected.new_seen_ids:
                state["seen_ids"] = _append_seen_ids(
                    list(state.get("seen_ids") or []), collected.new_seen_ids
                )
            state["consecutive_failures"] = 0
            state["last_error"] = ""

            # 4. 30-day check-in (when silent for checkin_after_days and outside quiet hours)
            checkin_days = int((mission.get("lifecycle") or {}).get("checkin_after_days") or 0)
            last_checkin = float(state.get("last_checkin_at") or now)
            if (
                checkin_days > 0
                and (now - last_checkin) >= checkin_days * 86400
                and not is_in_quiet_hours(
                    mission,
                    now_ts=now,
                    timezone_name=self.config.timezone_name,
                    default_start=self.config.quiet_start,
                    default_end=self.config.quiet_end,
                )
            ):
                code, body = self._post_watcher({"mission_id": mid, "event_type": "checkin"})
                if code == 200 and body.get("status") == "delivered":
                    state["last_checkin_at"] = now
                    atomic_write_json(sfile, self.config.state_dir, state)
                    return {"mission_id": mid, "outcome": "checkin_sent"}

            atomic_write_json(sfile, self.config.state_dir, state)
            return {"mission_id": mid, "outcome": "no_change"}

    def run_tick(
        self,
        *,
        now_ts: float | None = None,
        only_mission_id: str | None = None,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        """Execute one scheduler tick across triggered and due active missions."""
        now = time.time() if now_ts is None else now_ts
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        scout_budget = [MAX_WEB_SCOUTS_PER_TICK]

        if only_mission_id:
            return [
                self.run_single_mission(
                    only_mission_id,
                    now_ts=now,
                    force=force,
                    scout_budget_remaining=scout_budget,
                )
            ]

        triggered_ids = self.drain_triggers()
        results: list[dict[str, Any]] = []
        processed: set[str] = set()

        for mid in triggered_ids:
            processed.add(mid)
            results.append(
                self.run_single_mission(
                    mid,
                    now_ts=now,
                    force=True,
                    scout_budget_remaining=scout_budget,
                )
            )

        missions = [m for m in list_missions(self.config.missions_dir) if m["status"] == "active"]

        def _sort_key(m: dict[str, Any]) -> tuple[int, int, float]:
            st = load_safe_state(self.config.state_dir, m)
            prio = 0 if (m.get("notify") or {}).get("priority") == "urgent" else 1
            tier = 1 if m.get("kind") == "web_scout" else 0
            return (prio, tier, float(st.get("next_run_at") or 0.0))

        for m in sorted(missions, key=_sort_key):
            mid = str(m["id"])
            if mid in processed:
                continue
            results.append(
                self.run_single_mission(
                    mid,
                    now_ts=now,
                    force=force,
                    scout_budget_remaining=scout_budget,
                )
            )
        return results


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    parser = argparse.ArgumentParser(description="Run proactive watcher missions.")
    parser.add_argument("--mission", default="", help="Run a single mission ID.")
    parser.add_argument("--force", action="store_true", help="Force run ignoring next_run_at & quiet hours.")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent.parent
    env_file = resolve_env_file(repo_root)
    config = WatcherRuntimeConfig.from_env(env_file, repo_root=repo_root)
    allow_loopback = os.environ.get("WATCHER_ALLOW_LOOPBACK_FOR_TESTS") == "1"
    runner = WatcherRunner(config, allow_loopback_for_tests=allow_loopback)
    results = runner.run_tick(
        only_mission_id=args.mission.strip() or None,
        force=bool(args.force),
    )
    print(json.dumps({"results": results}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
