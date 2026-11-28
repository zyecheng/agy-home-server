# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Mission schemas, safe state persistence, path containment, and validation."""

from __future__ import annotations

import json
import os
import re
import stat
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ssrf import validate_url_syntax

MISSION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SAFE_ERR_RE = re.compile(r"[^A-Za-z0-9_.:/\- ]+")

VALID_STATUSES = ("scoping", "active", "paused", "archived")
VALID_KINDS = ("rss", "github_releases", "http_diff", "web_scout")
VALID_PRIORITIES = ("normal", "urgent")
VALID_EFFORTS = ("low", "medium", "high")
VALID_ENTITY_OUTCOMES = ("delivered", "suppressed")
EFFORT_IDLE_TIMEOUTS: dict[str, int] = {"low": 120, "medium": 180, "high": 240}
DEFAULT_MAX_SEARCHES = 5
MAX_SEARCHES_CAP = 15

MIN_INTERVAL_MINUTES: dict[str, int] = {
    "rss": 15,
    "github_releases": 15,
    "http_diff": 15,
    "web_scout": 60,
}

MAX_STATE_READ_BYTES = 256 * 1024  # 256 KiB
MAX_SEEN_IDS = 500
MAX_KNOWN_ENTITIES = 100
MAX_PENDING_ITEMS = 50
MAX_ACTIVE_MISSIONS = 50
MAX_TOPIC_TITLE_CHARS = 128

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class MissionValidationError(ValueError):
    """Raised when a mission spec, slug, or file path fails validation."""


def is_valid_mission_id(mission_id: str) -> bool:
    return bool(MISSION_ID_RE.match(mission_id or ""))


def validate_mission_id(mission_id: str) -> str:
    clean = (mission_id or "").strip()
    if not is_valid_mission_id(clean):
        raise MissionValidationError(
            f"Invalid mission id {mission_id!r}; must match {MISSION_ID_RE.pattern}"
        )
    return clean


def slugify_title(title: str, existing_ids: set[str] | None = None) -> str:
    """Create a valid, unique mission slug from a human title."""
    raw = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-_")
    base = raw[:48].rstrip("-_") or "mission"
    if not base[0].isalnum():
        base = "m-" + base
    base = base[:56]
    taken = existing_ids or set()
    if base not in taken and is_valid_mission_id(base):
        return base
    for n in range(2, 500):
        cand = f"{base[:58]}-{n}"
        if cand not in taken and is_valid_mission_id(cand):
            return cand
    return f"mission-{uuid.uuid4().hex[:8]}"


def _ensure_contained_path(base_dir: Path, filename: str) -> Path:
    """Resolve filename strictly inside base_dir without allowing symlinks or traversal."""
    if not filename or "/" in filename or "\\" in filename or filename in (".", ".."):
        raise MissionValidationError(f"Unsafe filename: {filename!r}")
    if base_dir.is_symlink():
        raise MissionValidationError(f"Refusing symlink directory: {base_dir}")
    resolved_base = base_dir.resolve()
    candidate = base_dir / filename
    if candidate.is_symlink():
        raise MissionValidationError(f"Refusing symlink file: {candidate}")
    resolved_target = candidate.resolve()
    if not resolved_target.is_relative_to(resolved_base):
        raise MissionValidationError(f"Path escapes base directory: {candidate}")
    return candidate


def _verify_direct_child(path: Path, base_dir: Path) -> Path:
    """Verify caller's path is a direct non-symlink child of base_dir and return the validated Path."""
    if base_dir.is_symlink() or path.parent.is_symlink():
        raise MissionValidationError(f"Refusing symlink directory: {path.parent}")
    if path.parent.resolve() != base_dir.resolve():
        raise MissionValidationError(f"Path escapes base directory: {path}")
    return _ensure_contained_path(base_dir, path.name)


def mission_path(missions_dir: Path, mission_id: str) -> Path:
    mid = validate_mission_id(mission_id)
    return _ensure_contained_path(missions_dir, f"{mid}.json")


def state_path(state_dir: Path, mission_id: str) -> Path:
    mid = validate_mission_id(mission_id)
    return _ensure_contained_path(state_dir, f"{mid}.state.json")


def pending_path(state_dir: Path, mission_id: str) -> Path:
    mid = validate_mission_id(mission_id)
    return _ensure_contained_path(state_dir, f"{mid}.pending.json")


def state_lock_path(state_dir: Path, mission_id: str) -> Path:
    mid = validate_mission_id(mission_id)
    return _ensure_contained_path(state_dir, f"{mid}.lock")


def trigger_path(triggers_dir: Path, mission_id: str) -> Path:
    mid = validate_mission_id(mission_id)
    return _ensure_contained_path(triggers_dir, f"{mid}.req")


def safe_read_json(
    path: Path,
    base_dir: Path,
    *,
    max_bytes: int = MAX_STATE_READ_BYTES,
) -> dict[str, Any] | None:
    """Read a JSON object safely with O_NOFOLLOW, O_NONBLOCK, S_ISREG, containment, and byte cap."""
    target = _verify_direct_child(path, base_dir)
    flags = os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK
    try:
        fd = os.open(str(target), flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise MissionValidationError(f"Cannot safely open {target.name}: {exc}") from exc

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise MissionValidationError(f"Refusing non-regular file: {target.name}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise MissionValidationError(
                    f"File {target.name} exceeds {max_bytes:,}-byte limit"
                )
            chunks.append(chunk)
    finally:
        os.close(fd)

    try:
        obj = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MissionValidationError(f"Corrupt JSON in {target.name}: {exc}") from exc
    if not isinstance(obj, dict):
        raise MissionValidationError(f"Expected JSON object in {target.name}")
    return obj


def _atomic_write_bytes(
    path: Path,
    base_dir: Path,
    data: bytes,
    *,
    exclusive: bool = False,
) -> Path:
    """Atomically write bytes with mode 0600, O_NOFOLLOW, direct-child containment, and fsync."""
    if base_dir.is_symlink():
        raise MissionValidationError(f"Refusing symlink directory: {base_dir}")
    base_dir.mkdir(parents=True, exist_ok=True)
    target = _verify_direct_child(path, base_dir)
    write_target = (
        target if exclusive else (base_dir / f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW
    fd = os.open(str(write_target), flags, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except AttributeError:
            pass
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if not exclusive:
            os.replace(write_target, target)
    except Exception:
        try:
            write_target.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target


def atomic_write_json(
    path: Path,
    base_dir: Path,
    payload: dict[str, Any],
    *,
    exclusive: bool = False,
) -> None:
    """Atomically persist a JSON dict with mode 0600, O_NOFOLLOW, containment, and fsync."""
    encoded = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    _atomic_write_bytes(path, base_dir, encoded, exclusive=exclusive)


def write_trigger_file(triggers_dir: Path, mission_id: str) -> Path:
    """Write a trigger file via exclusive temp + os.replace so pre-existing FIFOs are never opened."""
    target = trigger_path(triggers_dir, mission_id)
    return _atomic_write_bytes(target, triggers_dir, f"{mission_id}\n".encode("utf-8"), exclusive=False)


def iso_now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_timestamp(raw: str | None) -> float | None:
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def validate_mission(
    data: dict[str, Any],
    *,
    expected_id: str | None = None,
    require_runnable: bool = False,
) -> dict[str, Any]:
    """Validate and normalize a mission spec dict. Raises MissionValidationError on invalid specs."""
    if not isinstance(data, dict):
        raise MissionValidationError("Mission spec must be a JSON object")

    mid = validate_mission_id(str(data.get("id") or ""))
    if expected_id is not None and mid != expected_id:
        raise MissionValidationError(f"Mission id {mid!r} does not match filename {expected_id!r}")

    title = str(data.get("title") or "").strip()
    if not title:
        raise MissionValidationError("Mission title is required")

    status = str(data.get("status") or "scoping").strip()
    if status not in VALID_STATUSES:
        raise MissionValidationError(f"Invalid mission status {status!r}; expected {VALID_STATUSES}")

    kind = str(data.get("kind") or "web_scout").strip()
    if kind not in VALID_KINDS:
        raise MissionValidationError(f"Invalid mission kind {kind!r}; expected {VALID_KINDS}")

    sched = data.get("schedule") if isinstance(data.get("schedule"), dict) else {}
    min_iv = MIN_INTERVAL_MINUTES[kind]
    raw_iv = sched.get("interval_minutes")
    try:
        interval_minutes = int(raw_iv) if raw_iv is not None else max(min_iv, 60)
    except (TypeError, ValueError) as exc:
        raise MissionValidationError("schedule.interval_minutes must be an integer") from exc

    cfg = data.get("config") if isinstance(data.get("config"), dict) else {}
    must_validate_runnable = require_runnable or status == "active"

    if must_validate_runnable:
        if interval_minutes < min_iv:
            raise MissionValidationError(
                f"{kind} interval_minutes ({interval_minutes}) must be >= {min_iv}"
            )
        if kind in ("rss", "http_diff"):
            url = str(cfg.get("url") or "").strip()
            if not url:
                raise MissionValidationError(f"{kind} mission requires config.url")
            validate_url_syntax(url)
            if kind == "http_diff" and cfg.get("regex"):
                try:
                    re.compile(str(cfg["regex"]))
                except re.error as exc:
                    raise MissionValidationError(f"Invalid http_diff regex: {exc}") from exc
        elif kind == "github_releases":
            repo = str(cfg.get("repo") or "").strip()
            if not GITHUB_REPO_RE.match(repo):
                raise MissionValidationError(
                    f"github_releases mission requires config.repo like 'owner/name' (got {repo!r})"
                )
        elif kind == "web_scout":
            queries = cfg.get("queries")
            if (
                not isinstance(queries, list)
                or not (1 <= len(queries) <= 4)
                or not all(isinstance(q, str) and 1 <= len(q.strip()) <= 250 for q in queries)
            ):
                raise MissionValidationError(
                    "web_scout mission requires config.queries with 1 to 4 non-empty strings (<=250 chars)"
                )
            raw_eff = str(cfg.get("effort") or "low").strip().lower()
            if raw_eff not in VALID_EFFORTS:
                raise MissionValidationError(
                    f"web_scout config.effort {raw_eff!r} must be one of {VALID_EFFORTS}"
                )
            cfg = dict(cfg)
            cfg["effort"] = raw_eff
            if "max_searches" in cfg and cfg["max_searches"] is not None:
                if isinstance(cfg["max_searches"], bool):
                    raise MissionValidationError("web_scout config.max_searches must be an integer")
                try:
                    ms = int(cfg["max_searches"])
                except (TypeError, ValueError) as exc:
                    raise MissionValidationError("web_scout config.max_searches must be an integer") from exc
                if ms < 1 or ms > MAX_SEARCHES_CAP:
                    raise MissionValidationError(
                        f"web_scout config.max_searches ({ms}) must be between 1 and {MAX_SEARCHES_CAP}"
                    )
                cfg["max_searches"] = ms

    notify = data.get("notify") if isinstance(data.get("notify"), dict) else {}
    priority = str(notify.get("priority") or "normal").strip()
    if priority not in VALID_PRIORITIES:
        raise MissionValidationError(
            f"Invalid notify.priority {priority!r}; expected {VALID_PRIORITIES}"
        )

    qh = notify.get("quiet_hours")
    if qh is not None:
        if not isinstance(qh, dict):
            raise MissionValidationError("notify.quiet_hours must be null or an object")
        if not qh.get("disabled"):
            start = str(qh.get("start") or "")
            end = str(qh.get("end") or "")
            if not HHMM_RE.match(start) or not HHMM_RE.match(end):
                raise MissionValidationError(
                    "notify.quiet_hours must specify start/end as HH:MM or disabled: true"
                )

    target = notify.get("target")
    norm_target: dict[str, Any] | None = None
    if target is not None:
        if not isinstance(target, dict):
            raise MissionValidationError(
                "notify.target must be null or {'chat_id': str, 'thread_id': int}"
            )
        t_chat = str(target.get("chat_id") or "").strip()
        try:
            t_thread = int(target.get("thread_id") or 0)
        except (TypeError, ValueError) as exc:
            raise MissionValidationError("notify.target.thread_id must be an integer") from exc
        if not t_chat:
            raise MissionValidationError("notify.target.chat_id is required")
        norm_target = {"chat_id": t_chat, "thread_id": t_thread}

    lifecycle = data.get("lifecycle") if isinstance(data.get("lifecycle"), dict) else {}
    expires_at = lifecycle.get("expires_at")
    if expires_at is not None and parse_iso_timestamp(str(expires_at)) is None:
        raise MissionValidationError(f"Invalid lifecycle.expires_at timestamp: {expires_at!r}")
    try:
        checkin_days = int(lifecycle.get("checkin_after_days", 30))
    except (TypeError, ValueError) as exc:
        raise MissionValidationError("lifecycle.checkin_after_days must be an integer") from exc
    if checkin_days < 0:
        raise MissionValidationError("lifecycle.checkin_after_days must be >= 0")

    exclusions_raw = data.get("exclusions")
    exclusions = (
        [str(x).strip() for x in exclusions_raw if str(x).strip()]
        if isinstance(exclusions_raw, list)
        else []
    )

    out = dict(data)
    out["schema_version"] = 1
    out["id"] = mid
    out["title"] = title[:MAX_TOPIC_TITLE_CHARS]
    out["status"] = status
    out["kind"] = kind
    out["created_at"] = str(data.get("created_at") or iso_now_utc())
    out["schedule"] = {"interval_minutes": interval_minutes}
    out["config"] = cfg
    out["relevance_criteria"] = str(data.get("relevance_criteria") or "").strip()
    out["exclusions"] = exclusions[:25]
    out["notify"] = {
        "priority": priority,
        "quiet_hours": qh,
        "target": norm_target,
    }
    out["lifecycle"] = {
        "expires_at": str(expires_at) if expires_at is not None else None,
        "checkin_after_days": checkin_days,
    }
    return out


def default_state(mission: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    ts = time.time() if now is None else now
    created_ts = parse_iso_timestamp(mission.get("created_at")) or ts
    return {
        "last_run_at": 0.0,
        "next_run_at": 0.0,
        "next_flush_at": 0.0,
        "last_notified_at": 0.0,
        "last_checkin_at": created_ts,
        "consecutive_failures": 0,
        "failure_alerted": False,
        "last_error": "",
        "http_etag": "",
        "http_last_modified": "",
        "last_hash": "",
        "seen_ids": [],
        "known_entities": [],
        "pending_count": 0,
    }


def load_mission(
    missions_dir: Path,
    mission_id: str,
    *,
    require_runnable: bool = False,
) -> dict[str, Any] | None:
    mid = validate_mission_id(mission_id)
    path = mission_path(missions_dir, mid)
    raw = safe_read_json(path, missions_dir)
    if raw is None:
        return None
    return validate_mission(raw, expected_id=mid, require_runnable=require_runnable)


def list_missions(missions_dir: Path) -> list[dict[str, Any]]:
    """List all valid mission specs in missions_dir sorted by id."""
    if not missions_dir.exists() or missions_dir.is_symlink():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(missions_dir.iterdir()):
        if not p.name.endswith(".json") or p.name.startswith("."):
            continue
        mid = p.name[:-5]
        if not is_valid_mission_id(mid):
            continue
        try:
            m = load_mission(missions_dir, mid)
            if m is not None:
                out.append(m)
        except MissionValidationError:
            continue
    return out


def _safe_float(val: Any, default: float = 0.0) -> float:
    if isinstance(val, bool):
        return default
    try:
        out = float(val)
        return out if out >= 0.0 else default
    except (TypeError, ValueError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    if isinstance(val, bool):
        return default
    try:
        out = int(val)
        return out if out >= 0 else default
    except (TypeError, ValueError):
        return default


def load_safe_state(state_dir: Path, mission: dict[str, Any]) -> dict[str, Any]:
    """Safely read & strictly type-validate watchers/state/<id>.state.json across trust boundary."""
    mid = validate_mission_id(str(mission["id"]))
    base = default_state(mission)
    if not state_dir.exists() or state_dir.is_symlink():
        return base
    try:
        raw = safe_read_json(state_path(state_dir, mid), state_dir)
    except MissionValidationError:
        return base
    if not raw:
        return base

    merged = dict(base)
    for fk in ("last_run_at", "next_run_at", "next_flush_at", "last_notified_at"):
        merged[fk] = _safe_float(raw.get(fk), 0.0)
    merged["last_checkin_at"] = _safe_float(raw.get("last_checkin_at"), base["last_checkin_at"]) or base["last_checkin_at"]
    merged["consecutive_failures"] = _safe_int(raw.get("consecutive_failures"), 0)
    merged["failure_alerted"] = bool(raw.get("failure_alerted") is True)
    merged["pending_count"] = min(MAX_PENDING_ITEMS, _safe_int(raw.get("pending_count"), 0))

    raw_err = raw.get("last_error")
    merged["last_error"] = (
        _SAFE_ERR_RE.sub(" ", raw_err).strip()[:120] if isinstance(raw_err, str) else ""
    )
    for sk, cap in (("http_etag", 200), ("http_last_modified", 120), ("last_hash", 128)):
        sv = raw.get(sk)
        merged[sk] = sv.strip()[:cap] if isinstance(sv, str) else ""

    raw_seen = raw.get("seen_ids")
    if isinstance(raw_seen, list):
        merged["seen_ids"] = [
            s[:128] for s in raw_seen if isinstance(s, str) and s
        ][-MAX_SEEN_IDS:]

    raw_ents = raw.get("known_entities")
    if isinstance(raw_ents, list):
        clean_ents: list[dict[str, str]] = []
        for ent in raw_ents:
            if not isinstance(ent, dict):
                continue
            k = str(ent.get("key") or "").strip()
            if not is_valid_mission_id(k):
                continue
            raw_date = str(ent.get("first_seen") or "").strip()[:10]
            first_seen = raw_date if ISO_DATE_RE.match(raw_date) else ""
            raw_out = str(ent.get("outcome") or "").strip().lower()
            outcome = raw_out if raw_out in VALID_ENTITY_OUTCOMES else ""
            rec: dict[str, str] = {
                "key": k,
                "url_hash": str(ent.get("url_hash") or "")[:128],
                "first_seen": first_seen,
            }
            if outcome:
                rec["outcome"] = outcome
            clean_ents.append(rec)
        merged["known_entities"] = clean_ents[-MAX_KNOWN_ENTITIES:]

    return merged
