---
name: watch-missions
description: Scope, create, refine, and archive asynchronous proactive watch missions bound 1:1 to dedicated Telegram Mission Forum Topics.
---

<!-- Copyright (c) 2026 zyecheng | SPDX-License-Identifier: BSD-3-Clause -->

# Proactive Watch Missions Skill

**Authoritative mission path & strict rules:**
- Every mission spec lives at `watchers/missions/<mission_id>.json` (relative to `~/alpha-assistant/`). Use `view_file` / `edit_file` directly — do **NOT** run `ls`, `find`, or read source files under `/home/agy/agy-server/`.
- **CaMeL hygiene:** NEVER run `curl`, `wget`, or raw HTTP fetches against external websites (which would pull untrusted web content into your interactive session). Always use `python3 -m server.watchers probe "<url>"` to inspect candidate URLs safely.

Follow this 4-step workflow whenever the user asks you to monitor, scout, track, or watch something over time:

## 1. Initiate a Dedicated Mission Topic (from 1:1 DM — Handoff ONLY)
- If the user asks in **1:1 DM** (not already inside a Mission Topic) to start a long-term watch task:
  - Run **ONLY** `topic create` and stop:
    ```bash
    PYTHONPATH=/home/agy/agy-server python3 -m server.watchers topic create \
      --title "<concise mission title>" \
      --request "<user's original request>" \
      --questions "<2-3 clarifying questions if ambiguous, else empty>" \
      --kind "<web_scout|rss|github_releases|http_diff>"
    ```
    *(Pass `--kind` as an early hint if a structured Tier-A source is already obvious; otherwise omit it and finalize the tier inside the Mission Topic.)*
  - Share the returned `deep_link` in the 1:1 DM and invite the user to continue inside that Mission Topic.
  - **Do NOT** run `probe`, `curl`, `edit_file` on `watchers/missions/*.json`, or activate the mission from 1:1 DM (the only exception is if `deep_link` is empty because `WATCHER_FORUM_CHAT_ID` is not configured, in which case scope inline in DM).

## 2. Clarify Ambiguity Before Activating
- For **vague or open-ended requests** (e.g., *"watch for piano lessons for a 6-year-old around Mountain View"*), ask **at most 2–3 high-leverage questions**:
  1. Scope/format preferences (e.g., 1-on-1 vs. group, age/skill, distance/budget).
  2. Exclusions (what to ignore so alerts stay high-signal) & cadence/expiry window.
  3. **Scouting depth (`effort`):** Only ask if it is not obvious from the request — e.g., quick surface scan (`low`, ~30–50s, best for routine listings/deals/openings) vs. deeper multi-source research (`medium` or `high`, best for policy comparisons or nuanced analysis).
- For **crystal-clear requests** (e.g., *"watch GitHub repo owner/name releases for hooks changes"*), skip unnecessary interrogation and propose the concrete mission spec immediately.

## 3. Auto-Discover & Verify the Lightest Viable Collector Tier, Then Activate
Always prefer deterministic Tier-A collectors (0 LLM tokens when unchanged) over `web_scout` whenever a structured source exists:
- `"github_releases"`: GitHub repo releases (`config.repo: "owner/name"`, optional `include_prereleases`, `match_keywords`; interval `>= 15` min).
- `"rss"`: RSS/Atom feed (`config.url`, optional `match_keywords`, `max_items`; interval `>= 15` min).
- `"http_diff"`: Public webpage/JSON portal change tracker (`config.url`, optional `regex`, `match_keywords`; interval `>= 15` min).
- `"web_scout"`: Open-ended web discovery (`config.queries`: 1–4 search strings, `max_items`, optional `effort`: `"low"` | `"medium"` | `"high"` [default `"low"`], optional `max_searches`: `1..15` [default `5`]; interval `>= 60` min).

**Tier-A Auto-Discovery & Pre-Flight Probe (inside the Mission Topic before activating):**
1. When the user names a specific software project, blog, subreddit, newsletter, or single portal/website **without** pasting a URL or repo, do a quick `search_web` lookup to find the official GitHub `owner/name`, RSS/Atom feed URL, or direct portal URL — do not lazily leave it as `web_scout` if a clean Tier-A source exists.
2. Before activating an `rss` or `http_diff` mission on a discovered or user-provided URL, verify it with the CaMeL-safe structural probe (returns metadata only, zero raw page text):
   ```bash
   PYTHONPATH=/home/agy/agy-server python3 -m server.watchers probe "<url>"
   ```
3. Follow the probe result:
   - `github_repo` present → use `github_releases` with that `owner/name`.
   - `detected_kind == "rss"` → use `rss`.
   - `alternate_feeds` non-empty on a blog/news/changelog site → probe and prefer the discovered feed (`rss`); for a specific non-feed status/registration page, `http_diff` is fine when `detected_kind == "html_static"`.
   - `detected_kind` in `("html_static", "json", "text_plain")` → use `http_diff`.
   - `detected_kind` in `("js_shell", "blocked", "unreachable", "too_large", "http_error")` → briefly explain to the user that the site is JS-rendered or blocks direct HTTP polling, and fall back to `web_scout` (unless an alternate feed works).

Write/update `watchers/missions/<mission_id>.json` with `"status": "active"`, clear `relevance_criteria`, explicit `exclusions`, and valid `schedule.interval_minutes` (leaving `notify.target` intact!). Validate in one command (do NOT run ad-hoc `python3 -c` snippets):
```bash
PYTHONPATH=/home/agy/agy-server python3 -m server.watchers validate <mission_id>
```
Never edit or read raw scraped caches under `watchers/state/*.pending.json`.

## 4. In-Thread Refinement & Archival
- When the user replies inside an existing Mission Topic (`[mission: <id> | ...]`):
  - Read `watchers/missions/<id>.json` via `view_file` to inspect current rules.
  - Update `relevance_criteria`, `exclusions`, `schedule`, `config`, or `lifecycle.expires_at` cleanly.
- When the user says they are done (*"we enrolled"*, *"stop watching"*, *"archive this"*):
  - Archive the mission and close its Forum Topic via:
    ```bash
    PYTHONPATH=/home/agy/agy-server python3 -m server.watchers topic archive --id <mission_id>
    ```
