# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""CLI entrypoint for `python3 -m server.watchers` (topic lifecycle IPC & runner)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ..config import WatcherRuntimeConfig, load_ipc_client_settings, post_bridge_ipc, resolve_env_file
from .collectors import probe_url
from .schema import MissionValidationError, load_mission


def _cmd_topic(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parent.parent.parent
    url_host, ipc_port, token_path, _timeout = load_ipc_client_settings(repo_root)
    payload: dict[str, object] = {"action": args.action}
    if args.action == "create":
        payload["title"] = args.title
        if args.request:
            payload["initial_request"] = args.request
        if args.questions:
            payload["opening_questions"] = args.questions
        if args.kind:
            payload["kind"] = args.kind
    else:
        payload["mission_id"] = args.id

    code, body = post_bridge_ipc(
        url_host,
        ipc_port,
        token_path,
        "/watcher/topic",
        payload,
        timeout=30.0,
    )
    print(json.dumps(body, indent=2))
    return 0 if code == 200 and body.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m server.watchers")
    sub = parser.add_subparsers(dest="cmd", required=True)

    topic_p = sub.add_parser("topic", help="Manage Mission Forum Topics via Bridge IPC")
    topic_sub = topic_p.add_subparsers(dest="action", required=True)

    c_p = topic_sub.add_parser("create", help="Create a Mission Forum Topic and draft mission")
    c_p.add_argument("--title", required=True)
    c_p.add_argument("--request", default="")
    c_p.add_argument("--questions", default="")
    c_p.add_argument("--kind", default="web_scout")

    a_p = topic_sub.add_parser("archive", help="Archive a mission and close its Forum Topic")
    a_p.add_argument("--id", required=True)

    r_p = topic_sub.add_parser("reopen", help="Reopen an archived/paused mission and its Forum Topic")
    r_p.add_argument("--id", required=True)

    run_p = sub.add_parser("run", help="Execute watcher runner tick")
    run_p.add_argument("--mission", default="")
    run_p.add_argument("--force", action="store_true")

    probe_p = sub.add_parser(
        "probe",
        help="CaMeL-safe structural pre-flight probe to check Tier-A viability for a URL",
    )
    probe_p.add_argument("url", help="Public http(s) URL to probe")

    val_p = sub.add_parser(
        "validate",
        help="Validate a mission JSON spec for runnability against the canonical schema",
    )
    val_p.add_argument("id", help="Mission slug ID (without .json)")
    val_p.add_argument("--missions-dir", default="", help="Optional missions directory override")

    ns = parser.parse_args(argv)
    if ns.cmd == "topic":
        return _cmd_topic(ns)
    if ns.cmd == "probe":
        allow_loopback = os.environ.get("WATCHER_ALLOW_LOOPBACK_FOR_TESTS") == "1"
        report = probe_url(ns.url, allow_loopback_for_tests=allow_loopback)
        print(json.dumps(report, indent=2))
        return 0
    if ns.cmd == "validate":
        repo_root = Path(__file__).resolve().parent.parent.parent
        if ns.missions_dir:
            missions_dir = Path(ns.missions_dir)
        else:
            env_file = resolve_env_file(repo_root)
            cfg = WatcherRuntimeConfig.from_env(env_file, repo_root=repo_root)
            missions_dir = cfg.missions_dir
        try:
            mission = load_mission(missions_dir, ns.id, require_runnable=True)
        except MissionValidationError as exc:
            print(json.dumps({"ok": False, "mission_id": ns.id, "error": str(exc)}, indent=2))
            return 1
        if mission is None:
            print(
                json.dumps(
                    {"ok": False, "mission_id": ns.id, "error": "mission_not_found"},
                    indent=2,
                )
            )
            return 1
        print(
            json.dumps(
                {
                    "ok": True,
                    "mission_id": mission["id"],
                    "status": mission["status"],
                    "kind": mission["kind"],
                    "interval_minutes": (mission.get("schedule") or {}).get("interval_minutes"),
                },
                indent=2,
            )
        )
        return 0
    if ns.cmd == "run":
        from .runner import main as runner_main

        r_args: list[str] = []
        if ns.mission:
            r_args.extend(["--mission", ns.mission])
        if ns.force:
            r_args.append("--force")
        return runner_main(r_args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
