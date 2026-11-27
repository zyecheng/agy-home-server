---
name: watch-missions
description: Scope, create, refine, and archive asynchronous proactive watch missions bound 1:1 to dedicated Telegram Mission Forum Topics.
---

<!-- Copyright (c) 2026 zyecheng | SPDX-License-Identifier: BSD-3-Clause -->

# Proactive Watch Missions Skill

**Authoritative mission path:** Every mission spec lives at `watchers/missions/<mission_id>.json` (relative to `~/alpha-assistant/`). Do NOT look under `missions/` or run shell `ls` commands — use `view_file` / `edit_file` directly on `watchers/missions/<mission_id>.json`.

Follow this 4-step workflow whenever the user asks you to monitor, scout, track, or watch something over time:

## 1. Initiate a Dedicated Mission Topic (from 1:1 DM)
- If the user asks in **1:1 DM** (not already inside a Mission Topic) to start a long-term watch task:
  - Create a dedicated Mission Topic via:
    ```bash
    PYTHONPATH=/home/agy/agy-server python3 -m server.watchers topic create \
      --title "<concise mission title>" \
      --request "<user's original request>" \
      --questions "<2-3 clarifying questions if ambiguous, else empty>"
    ```
  - Share the returned `deep_link` in the 1:1 DM and invite the user to continue scoping there (keeping 1:1 DM clean).
  - If `deep_link` is empty (`WATCHER_FORUM_CHAT_ID` not configured), scope inline in DM using the returned `mission_id`.

## 2. Clarify Ambiguity Before Activating
- For **vague or open-ended requests** (e.g., *"watch for piano lessons for a 6-year-old around Mountain View"*), ask **at most 2–3 high-leverage questions**:
  1. Scope/format preferences (e.g., 1-on-1 vs. group, age/skill, distance/budget).
  2. Exclusions (what to ignore so alerts stay high-signal).
  3. Preferred cadence or expiry window (if seasonal).
- For **crystal-clear requests** (e.g., *"watch GitHub repo owner/name releases for hooks changes"*), skip unnecessary interrogation and propose the concrete mission spec immediately.

## 3. Pick the Lightest Viable Collector Tier & Activate
Always prefer deterministic Tier-A collectors (0 LLM tokens when unchanged) over `web_scout` whenever a structured source exists:
- `"rss"`: RSS/Atom feed (`config.url`, optional `match_keywords`, `max_items`; interval `>= 15` min).
- `"github_releases"`: GitHub repo releases (`config.repo: "owner/name"`, optional `include_prereleases`, `match_keywords`; interval `>= 15` min).
- `"http_diff"`: Public webpage/portal change tracker (`config.url`, optional `regex`, `match_keywords`; interval `>= 15` min).
- `"web_scout"`: Open-ended web discovery (`config.queries`: 1–4 search strings, `max_items`; interval `>= 60` min).

Write/update `watchers/missions/<mission_id>.json` with `"status": "active"`, clear `relevance_criteria`, explicit `exclusions`, and valid `schedule.interval_minutes` (leaving `notify.target` intact!). Never edit or read raw scraped caches under `watchers/state/*.pending.json`.

## 4. In-Thread Refinement & Archival
- When the user replies inside an existing Mission Topic (`[mission: <id> | ...]`):
  - Read `watchers/missions/<id>.json` via `view_file` to inspect current rules.
  - Update `relevance_criteria`, `exclusions`, `schedule`, `config`, or `lifecycle.expires_at` cleanly.
- When the user says they are done (*"we enrolled"*, *"stop watching"*, *"archive this"*):
  - Archive the mission and close its Forum Topic via:
    ```bash
    PYTHONPATH=/home/agy/agy-server python3 -m server.watchers topic archive --id <mission_id>
    ```
