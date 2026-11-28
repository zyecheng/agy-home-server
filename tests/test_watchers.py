# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Unit, security, CaMeL, SSRF, and lifecycle tests for Proactive Watchers & Intel Scout."""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from server.agy_runner import AgyRunner
from server.bot import start_ipc_server
from server.config import (
    BridgeConfig,
    WatcherRuntimeConfig,
    ensure_ipc_token,
    post_bridge_ipc,
)
from server.db import BridgeStore
from server.hook_scout_guard import evaluate_tool_call
from server.router import BridgeRouter, InboundMessage, sanitize_stage2_links
from server.telegram_client import TelegramClient
from server.watchers.collectors import (
    collect_github_releases,
    collect_http_diff,
    collect_rss,
    collect_web_scout,
    normalize_html_text,
)
from server.watchers.health import run_health_check
from server.watchers.runner import WatcherRunner, is_in_quiet_hours
from server.watchers.schema import (
    MAX_STATE_READ_BYTES,
    MissionValidationError,
    atomic_write_json,
    load_mission,
    load_safe_state,
    mission_path,
    pending_path,
    safe_read_json,
    slugify_title,
    state_path,
    validate_mission,
    write_trigger_file,
)
from server.watchers.ssrf import (
    ResponseTooLargeError,
    SSRFError,
    safe_fetch,
    validate_ip_address,
)
from tests.mock_telegram import MockTelegramServer

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_APPROVE = REPO_ROOT / "server" / "hook_approve.py"


class _MockFeedServer:
    """Local HTTP server for RSS, Atom, GitHub Releases, HTML diff, redirect, and gzip-bomb tests."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args) -> None:
                return

            def do_GET(self) -> None:
                hdrs = {k.lower(): v for k, v in self.headers.items()}
                outer.requests.append((self.path, hdrs))
                entry = outer.routes.get(self.path)
                if entry is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                code, resp_hdrs, body = entry
                if resp_hdrs.get("ETag") and hdrs.get("if-none-match") == resp_hdrs["ETag"]:
                    self.send_response(304)
                    self.send_header("ETag", resp_hdrs["ETag"])
                    self.end_headers()
                    return
                self.send_response(code)
                for k, v in resp_hdrs.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._srv.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()


class SSRFTests(unittest.TestCase):
    def test_blocks_all_non_global_ipv4_ipv6_multicast_and_nat64(self) -> None:
        blocked = [
            "127.0.0.1",
            "0.0.0.0",
            "10.0.0.1",
            "172.16.5.1",
            "192.168.1.1",
            "100.64.0.1",
            "169.254.169.254",
            "224.0.0.1",
            "255.255.255.255",
            "::",
            "::1",
            "fc00::1",
            "fd12::1",
            "fe80::1",
            "ff02::1",
            "::ffff:127.0.0.1",
            "::ffff:169.254.169.254",
            "64:ff9b::127.0.0.1",
            "64:ff9b::10.0.0.1",
            "64:ff9b:1::1",
        ]
        for addr in blocked:
            with self.assertRaises(SSRFError, msg=f"Should block {addr}"):
                validate_ip_address(addr)

        self.assertEqual(str(validate_ip_address("93.184.216.34")), "93.184.216.34")
        self.assertEqual(
            str(validate_ip_address("2606:2800:220:1:248:1893:25c8:1946")),
            "2606:2800:220:1:248:1893:25c8:1946",
        )

    def test_scheme_userinfo_redirect_and_size_caps(self) -> None:
        for bad_url in [
            "file:///etc/passwd",
            "ftp://example.com/a",
            "http://user:pass@93.184.216.34/",
            "http://127.0.0.1:8765/health",
            "http://0.0.0.0:8765/health",
            "http://[::1]:8765/health",
        ]:
            with self.assertRaises(SSRFError, msg=f"Should reject {bad_url}"):
                safe_fetch(bad_url)

        srv = _MockFeedServer()
        try:
            srv.routes["/redir"] = (302, {"Location": f"{srv.base_url}/ok"}, b"")
            srv.routes["/ok"] = (200, {"Content-Type": "text/plain"}, b"hello")
            srv.routes["/big"] = (200, {"Content-Type": "text/plain"}, b"A" * 4096)
            srv.routes["/bomb"] = (
                200,
                {"Content-Type": "text/plain", "Content-Encoding": "gzip"},
                gzip.compress(b"B" * 20_000),
            )

            res = safe_fetch(f"{srv.base_url}/redir", allow_loopback_for_tests=True)
            self.assertEqual(res.body, b"hello")

            with self.assertRaises(ResponseTooLargeError):
                safe_fetch(f"{srv.base_url}/big", max_bytes=1024, allow_loopback_for_tests=True)

            with self.assertRaises(ResponseTooLargeError):
                safe_fetch(f"{srv.base_url}/bomb", max_bytes=2048, allow_loopback_for_tests=True)
        finally:
            srv.close()


class SchemaAndContainmentTests(unittest.TestCase):
    def test_slug_validation_exclusive_write_fifo_and_bounded_reads(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            missions = base / "missions"
            triggers = base / "triggers"
            missions.mkdir()
            triggers.mkdir()

            with self.assertRaises(MissionValidationError):
                mission_path(missions, "../etc/passwd")
            with self.assertRaises(MissionValidationError):
                mission_path(missions, "UPPER_BAD")

            # Direct-child containment rejects paths outside base_dir even if filename looks safe
            outside = base / "outside.json"
            outside.write_text('{"x": 1}', encoding="utf-8")
            with self.assertRaises(MissionValidationError):
                safe_read_json(outside, missions)
            with self.assertRaises(MissionValidationError):
                atomic_write_json(outside, missions, {"x": 2})

            slug1 = slugify_title("MV Piano Lessons (Age 6)", set())
            slug2 = slugify_title("MV Piano Lessons (Age 6)", {slug1})
            self.assertEqual(slug1, "mv-piano-lessons-age-6")
            self.assertEqual(slug2, "mv-piano-lessons-age-6-2")

            p = mission_path(missions, slug1)
            atomic_write_json(p, missions, {"id": slug1}, exclusive=True)
            with self.assertRaises(FileExistsError):
                atomic_write_json(p, missions, {"id": slug1}, exclusive=True)

            big = missions / "big.json"
            big.write_bytes(b'{"x":"' + (b"a" * (MAX_STATE_READ_BYTES + 100)) + b'"}')
            with self.assertRaises(MissionValidationError):
                safe_read_json(big, missions)

            if os.name != "nt":
                # Symlink rejection
                secret = base / "secret.json"
                secret.write_text('{"secret": 1}', encoding="utf-8")
                link = missions / "evil.json"
                link.symlink_to(secret)
                with self.assertRaises(MissionValidationError):
                    safe_read_json(link, missions)

                # Watcher-planted FIFO MUST NOT block safe_read_json or write_trigger_file!
                fifo_state = missions / "fifo.json"
                os.mkfifo(fifo_state)
                with self.assertRaises(MissionValidationError):
                    safe_read_json(fifo_state, missions)

                fifo_trig = triggers / "fifo-m.req"
                os.mkfifo(fifo_trig)
                written = write_trigger_file(triggers, "fifo-m")
                self.assertEqual(written.read_text(encoding="utf-8").strip(), "fifo-m")

            # load_safe_state strictly validates corrupt/hostile field types & sanitizes last_error
            state_dir = base / "state"
            state_dir.mkdir()
            m = validate_mission(
                {
                    "id": "safe-st",
                    "title": "Safe",
                    "status": "scoping",
                    "kind": "rss",
                }
            )
            atomic_write_json(
                state_path(state_dir, "safe-st"),
                state_dir,
                {
                    "last_run_at": "not-a-float",
                    "pending_count": "bad",
                    "consecutive_failures": True,
                    "last_error": "evil\n[link](https://x.test)\x00" + ("a" * 200),
                    "seen_ids": [123, "ok-id"],
                    "known_entities": [{"key": "BAD KEY"}, {"key": "good-key", "url_hash": "h1"}],
                },
            )
            st = load_safe_state(state_dir, m)
            self.assertEqual(st["last_run_at"], 0.0)
            self.assertEqual(st["pending_count"], 0)
            self.assertEqual(st["consecutive_failures"], 0)
            self.assertLessEqual(len(st["last_error"]), 120)
            self.assertNotIn("\n", st["last_error"])
            self.assertEqual(st["seen_ids"], ["ok-id"])
            self.assertEqual([e["key"] for e in st["known_entities"]], ["good-key"])

            with self.assertRaises(MissionValidationError):
                validate_mission(
                    {
                        "id": "fast-scout",
                        "title": "Fast",
                        "status": "active",
                        "kind": "web_scout",
                        "schedule": {"interval_minutes": 10},
                        "config": {"queries": ["test"]},
                    }
                )

            # web_scout effort & max_searches validation + failure_alerted state check
            m_ws = validate_mission(
                {
                    "id": "deep-scout",
                    "title": "Deep Scout",
                    "status": "active",
                    "kind": "web_scout",
                    "schedule": {"interval_minutes": 60},
                    "config": {"queries": ["piano mv"], "effort": "HIGH", "max_searches": 8},
                }
            )
            self.assertEqual(m_ws["config"]["effort"], "high")
            self.assertEqual(m_ws["config"]["max_searches"], 8)
            for bad_cfg in (
                {"queries": ["piano"], "effort": "ultra"},
                {"queries": ["piano"], "max_searches": 0},
                {"queries": ["piano"], "max_searches": 99},
                {"queries": ["piano"], "max_searches": True},
            ):
                with self.assertRaises(MissionValidationError, msg=f"Should reject {bad_cfg}"):
                    validate_mission(
                        {
                            "id": "bad-scout",
                            "title": "Bad",
                            "status": "active",
                            "kind": "web_scout",
                            "schedule": {"interval_minutes": 60},
                            "config": bad_cfg,
                        }
                    )

            atomic_write_json(
                state_path(state_dir, "safe-st"),
                state_dir,
                {"failure_alerted": "yes"},
            )
            self.assertFalse(load_safe_state(state_dir, m)["failure_alerted"])
            atomic_write_json(
                state_path(state_dir, "safe-st"),
                state_dir,
                {"failure_alerted": True},
            )
            self.assertTrue(load_safe_state(state_dir, m)["failure_alerted"])


class CaMeLHookLockoutTests(unittest.TestCase):
    def test_hook_scout_guard_allows_only_search_web_and_finish(self) -> None:
        self.assertEqual(
            evaluate_tool_call({"toolCall": {"name": "search_web", "args": {"query": "piano mv"}}})[0],
            "auto_approve",
        )
        self.assertEqual(
            evaluate_tool_call({"toolCall": {"name": "finish", "args": {"items": []}}})[0],
            "auto_approve",
        )
        for blocked in (
            "read_url_content",
            "run_command",
            "view_file",
            "read_file",
            "write_to_file",
            "edit_file",
            "grep_search",
            "list_dir",
            "mcp_github_issue",
        ):
            dec, _reason = evaluate_tool_call({"toolCall": {"name": blocked, "args": {}}})
            self.assertEqual(dec, "deny", f"Expected {blocked} to be denied")

        dec, _ = evaluate_tool_call(
            {"toolCall": {"name": "search_web", "args": {"query": "x" * 350}}}
        )
        self.assertEqual(dec, "deny")

    def test_hook_scout_guard_enforces_max_searches_budget_and_allows_finish(self) -> None:
        fd, raw_path = tempfile.mkstemp(prefix="test-scout-budget-", suffix=".cnt")
        os.close(fd)
        counter = Path(raw_path)
        try:
            call = {"toolCall": {"name": "search_web", "args": {"query": "piano lessons mv"}}}
            d1, _ = evaluate_tool_call(call, max_searches=2, counter_file=counter)
            d2, _ = evaluate_tool_call(call, max_searches=2, counter_file=counter)
            d3, reason3 = evaluate_tool_call(call, max_searches=2, counter_file=counter)
            d_fin, _ = evaluate_tool_call(
                {"toolCall": {"name": "finish", "args": {"items": []}}},
                max_searches=2,
                counter_file=counter,
            )
            self.assertEqual(d1, "auto_approve")
            self.assertEqual(d2, "auto_approve")
            self.assertEqual(d3, "deny")
            self.assertIn("Search budget (2 search_web calls) reached", reason3)
            self.assertIn("finish", reason3)
            self.assertEqual(d_fin, "auto_approve")
        finally:
            counter.unlink(missing_ok=True)

    def test_stream_json_idle_timeout_resets_on_progress_and_kills_wedged_proc(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            runner = AgyRunner.from_cli_settings(agy_bin="agy", alpha_workspace=Path(td))

            # 1. Active stream emitting step_update every 0.15s for 0.6s with 0.35s idle timeout MUST succeed!
            active_code = (
                "import json, sys, time\n"
                "print(json.dumps({'event':'init','conversation_id':'conv-stream-1'}), flush=True)\n"
                "for i in range(4):\n"
                "    time.sleep(0.15)\n"
                "    print(json.dumps({'event':'step_update','step':{'id':str(i),'state':'DONE'}}), flush=True)\n"
                "print(json.dumps({'event':'result','result':{'conversation_id':'conv-stream-1','status':'SUCCESS',"
                "'response':'ok','structured_output':{'items':[{'title':'Found'}]},'duration_seconds':0.6}}), flush=True)\n"
            )
            proc_ok = subprocess.Popen(
                [sys.executable, "-u", "-c", active_code],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
            res_ok = runner._run_stream_json(
                proc_ok,
                conversation_id=None,
                idle_timeout_seconds=0.45,
                hard_timeout_seconds=5.0,
            )
            self.assertEqual(res_ok.status, "SUCCESS", res_ok.error)
            self.assertEqual(res_ok.conversation_id, "conv-stream-1")
            self.assertEqual(res_ok.structured_output, {"items": [{"title": "Found"}]})

            # 2. Wedged stream emitting init then hanging MUST time out with idle timeout!
            wedged_code = (
                "import json, time\n"
                "print(json.dumps({'event':'init','conversation_id':'conv-stuck'}), flush=True)\n"
                "time.sleep(5.0)\n"
            )
            proc_stuck = subprocess.Popen(
                [sys.executable, "-u", "-c", wedged_code],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
            res_stuck = runner._run_stream_json(
                proc_stuck,
                conversation_id=None,
                idle_timeout_seconds=0.3,
                hard_timeout_seconds=5.0,
            )
            self.assertEqual(res_stuck.status, "TIMEOUT")
            self.assertEqual(res_stuck.conversation_id, "conv-stuck")
            self.assertIn("idle timeout", res_stuck.error)

            # 3. Chatty stream exceeding hard ceiling MUST hit hard ceiling even while emitting events!
            chatty_code = (
                "import json, time\n"
                "for i in range(40):\n"
                "    print(json.dumps({'event':'step_update','i':i}), flush=True)\n"
                "    time.sleep(0.05)\n"
            )
            proc_hard = subprocess.Popen(
                [sys.executable, "-u", "-c", chatty_code],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
            res_hard = runner._run_stream_json(
                proc_hard,
                conversation_id="conv-hard",
                idle_timeout_seconds=2.0,
                hard_timeout_seconds=0.35,
            )
            self.assertEqual(res_hard.status, "TIMEOUT")
            self.assertIn("hard ceiling", res_hard.error)

    def test_hook_approve_watcher_briefing_denies_all_tools_first(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "AGY_BRIDGE_ORIGIN"}
        env["AGY_WATCHER_BRIEFING"] = "1"
        for tool in ("view_file", "read_file", "grep_search", "list_dir", "run_command", "write_to_file"):
            proc = subprocess.run(
                [sys.executable, str(HOOK_APPROVE)],
                input=json.dumps({"toolCall": {"name": tool, "args": {"AbsolutePath": "/tmp/x"}}}),
                text=True,
                capture_output=True,
                env=env,
                timeout=10,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout.strip())
            self.assertEqual(out["decision"], "deny", f"Expected {tool} denied under AGY_WATCHER_BRIEFING=1")


class CollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.srv = _MockFeedServer()

    def tearDown(self) -> None:
        self.srv.close()

    def test_rss_atom_etag_expat_doctype_and_baseline_cap(self) -> None:
        rss_xml = b"""<?xml version="1.0"?>
        <rss version="2.0"><channel><title>Deals</title>
          <item><guid>g1</guid><title>HP Mini 600 G9 $420</title><link>https://deals.example/1</link><description>Great deal</description></item>
          <item><guid>g2</guid><title>Random Toaster</title><link>https://deals.example/2</link><description>Toast</description></item>
          <item><guid>g3</guid><title>HP Mini 600 G9 $430</title><link>https://deals.example/3</link><description>Another</description></item>
        </channel></rss>"""
        self.srv.routes["/feed.xml"] = (200, {"ETag": '"v1"'}, rss_xml)

        mission = {
            "id": "deals",
            "title": "Deals",
            "kind": "rss",
            "config": {"url": f"{self.srv.base_url}/feed.xml", "match_keywords": ["HP Mini"], "max_items": 1},
        }
        res = collect_rss(mission, {}, allow_loopback_for_tests=True)
        self.assertEqual(len(res.items), 1)
        self.assertEqual(res.items[0]["title"], "HP Mini 600 G9 $420")
        self.assertEqual(len(res.new_seen_ids), 3)
        self.assertEqual(res.http_etag, '"v1"')

        res2 = collect_rss(mission, {"http_etag": '"v1"', "seen_ids": res.new_seen_ids}, allow_loopback_for_tests=True)
        self.assertTrue(res2.not_modified)
        self.assertEqual(res2.items, [])

        # Expat DTD handler rejects DOCTYPE even after >4 KiB comment padding or in UTF-16!
        padded_xxe = b"<?xml version='1.0'?><!-- " + (b"x" * 5000) + b" --><!DOCTYPE foo [<!ENTITY a 'b'>]><rss/>"
        self.srv.routes["/xxe.xml"] = (200, {}, padded_xxe)
        bad_m = dict(mission, config={"url": f"{self.srv.base_url}/xxe.xml"})
        with self.assertRaises(ValueError):
            collect_rss(bad_m, {}, allow_loopback_for_tests=True)

        # web_scout fails closed if hooks.json or settings.json is missing
        with tempfile.TemporaryDirectory() as td:
            empty_sb = Path(td)
            runner = AgyRunner.from_cli_settings(agy_bin="agy", alpha_workspace=empty_sb)
            with self.assertRaises(RuntimeError):
                collect_web_scout(
                    {"id": "ws", "title": "WS", "config": {"queries": ["test"]}},
                    {},
                    agy_runner=runner,
                    scout_sandbox_dir=empty_sb,
                    brain_dir=empty_sb,
                )

    def test_github_releases_and_http_diff_html_normalization(self) -> None:
        rels = json.dumps(
            [
                {"tag_name": "v2.0.0-rc1", "name": "RC1", "prerelease": True, "html_url": "https://gh.example/rc1", "body": "rc"},
                {"tag_name": "v1.6.0", "name": "v1.6.0", "prerelease": False, "html_url": "https://gh.example/160", "body": "Hooks update"},
            ]
        ).encode("utf-8")
        self.srv.routes["/repos/acme/cli/releases?per_page=20"] = (200, {"ETag": '"gh1"'}, rels)
        gm = {
            "id": "gh",
            "title": "GH",
            "kind": "github_releases",
            "config": {"repo": "acme/cli", "api_base_url": self.srv.base_url},
        }
        gres = collect_github_releases(gm, {}, allow_loopback_for_tests=True)
        self.assertEqual(len(gres.items), 1)
        self.assertIn("v1.6.0", gres.items[0]["title"])

        html_v1 = b"<html><head><script>var csrf='AAA111';</script></head><body><h1>Swim Classes</h1><p>Closed</p></body></html>"
        html_v2_same = b"<html><head><script>var csrf='BBB222';</script></head><body><h1>Swim Classes</h1><p>Closed</p></body></html>"
        html_v3_open = b"<html><head><script>var csrf='CCC333';</script></head><body><h1>Swim Classes</h1><p>Summer Registration OPEN</p></body></html>"

        self.srv.routes["/swim"] = (200, {"Content-Type": "text/html"}, html_v1)
        hm = {"id": "swim", "title": "Swim", "kind": "http_diff", "config": {"url": f"{self.srv.base_url}/swim"}}
        h1 = collect_http_diff(hm, {}, allow_loopback_for_tests=True)
        self.assertEqual(len(h1.items), 1)

        self.srv.routes["/swim"] = (200, {"Content-Type": "text/html"}, html_v2_same)
        h2 = collect_http_diff(hm, {"last_hash": h1.last_hash}, allow_loopback_for_tests=True)
        self.assertTrue(h2.not_modified)
        self.assertEqual(h2.items, [])

        self.srv.routes["/swim"] = (200, {"Content-Type": "text/html"}, html_v3_open)
        h3 = collect_http_diff(hm, {"last_hash": h1.last_hash}, allow_loopback_for_tests=True)
        self.assertEqual(len(h3.items), 1)
        self.assertIn("Summer Registration OPEN", h3.items[0]["summary"])
        self.assertEqual(normalize_html_text("<style>a{}</style>Hello   <b>world</b>"), "Hello world")


class HealthAndRunnerLifecycleTests(unittest.TestCase):
    @staticmethod
    def _make_config(
        root: Path,
        *,
        token: str = "t",
        api_base_url: str = "https://api.telegram.org",
        allowed_chat_ids: frozenset[int] = frozenset(),
        watcher_forum_chat_id: int | None = None,
        ipc_port: int = 8765,
    ) -> BridgeConfig:
        return BridgeConfig(
            telegram_bot_token=token,
            allowed_user_ids=frozenset({111}),
            allowed_chat_ids=allowed_chat_ids,
            poll_timeout_seconds=1,
            debounce_seconds=0.0,
            max_inline_chars=3500,
            ssh_prompt_preview_chars=300,
            default_mirror_mode="full",
            alpha_workspace=root / "alpha",
            agy_bin="agy",
            agy_brain_dir=root / "brain",
            agy_model="",
            agy_timeout_seconds=60,
            db_path=root / "data" / "bridge.db",
            ipc_host="127.0.0.1",
            ipc_port=ipc_port,
            ipc_token_path=root / "data" / "ipc.token",
            timezone_name="America/Los_Angeles",
            telegram_api_base_url=api_base_url,
            watcher_forum_chat_id=watcher_forum_chat_id,
            watcher_ipc_dir=root / "watcher-ipc",
        )

    def test_online_sqlite_backup_health_cooldown_simultaneous_recovery_and_unconfigured(self) -> None:
        tg = MockTelegramServer()
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                cfg = self._make_config(root, token=tg.expected_token, api_base_url=tg.base_url)
                store = BridgeStore(cfg.db_path)
                store.get_or_create_binding("telegram", "111", 0, "private", str(cfg.alpha_workspace))
                store.close()

                oauth_file = root / "antigravity-oauth-token"
                oauth_file.write_text("tok", encoding="utf-8")

                # Unconfigured Telegram MUST NOT stamp 6h cooldown
                cfg_unconf = self._make_config(root, token="replace-me", api_base_url=tg.base_url)
                r0 = run_health_check(
                    cfg_unconf,
                    oauth_path=oauth_file,
                    service_unit=None,
                    disk_threshold_pct=0.0,
                    now_ts=1_700_000_000.0,
                )
                self.assertEqual(r0["sent_messages"], 0)

                # Now configured: immediately alerts on disk fault (was NOT suppressed by unconfigured run!)
                r1 = run_health_check(
                    cfg,
                    oauth_path=oauth_file,
                    service_unit=None,
                    disk_threshold_pct=0.0,
                    now_ts=1_700_000_010.0,
                )
                self.assertFalse(r1["ok"])
                self.assertEqual(r1["sent_messages"], 1)
                self.assertEqual(tg.sent_messages[-1]["link_preview_options"], {"is_disabled": True})

                # Repeat within 6h cooldown -> suppressed
                r2 = run_health_check(
                    cfg,
                    oauth_path=oauth_file,
                    service_unit=None,
                    disk_threshold_pct=0.0,
                    now_ts=1_700_000_100.0,
                )
                self.assertEqual(r2["sent_messages"], 0)

                # Simultaneous disk recovery + new OAuth fault -> sends BOTH alert AND recovery!
                oauth_file.unlink()
                before_both = len(tg.sent_messages)
                r3 = run_health_check(
                    cfg,
                    oauth_path=oauth_file,
                    service_unit=None,
                    disk_threshold_pct=100.0,
                    now_ts=1_700_000_200.0,
                )
                self.assertEqual(r3["sent_messages"], 2)
                self.assertTrue(Path(r3["backup_path"]).exists())
                texts = [m["text"] for m in tg.sent_messages[before_both:]]
                self.assertTrue(any("[Host Health Alert]" in t and "[oauth]" in t for t in texts))
                self.assertTrue(any("[Host Health Recovered]" in t and "disk" in t for t in texts))
        finally:
            tg.close()

    def test_sanitize_stage2_links_defeats_nested_brackets_newlines_and_preserves_formatted_urls(self) -> None:
        allowed = {"https://arts4all.example/piano"}
        deep_nested = ("[x" * 14) + "[Click](https://a.invalid)" + ("](https://evil.example/deep)" * 14)
        raw = (
            "Check [CSMA](https://arts4all.example/piano) and `https://arts4all.example/piano` "
            "可直接预约（https://arts4all.example/piano）。 "
            "and [x[Click](https://a.invalid)](https://evil.example/nested) "
            "and [Click[](https://[2001:db8::1]/exfil?d=secret) "
            "and [Click[](https://arts4all.example/piano_?leak=secret) "
            "and tg://_x "
            f"and {deep_nested} "
            "and [x[TG](https://a.invalid)](tg://resolve?domain=evil) "
            "and [line1\nhttps://evil.example/nl](https://arts4all.example/piano) "
            "and [https://evil.example/inlabel](https://arts4all.example/piano) "
            "or https://evil.example/raw."
        )
        cleaned = sanitize_stage2_links(raw, allowed)
        self.assertIn("[CSMA](https://arts4all.example/piano)", cleaned)
        self.assertIn("`https://arts4all.example/piano`", cleaned)
        self.assertIn("可直接预约（https://arts4all.example/piano）。", cleaned)
        self.assertNotIn("evil.example", cleaned)
        self.assertNotIn("2001:db8", cleaned)
        self.assertNotIn("leak=secret", cleaned)
        self.assertNotIn("a.invalid", cleaned)
        self.assertNotIn("tg://", cleaned)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg_empty = self._make_config(root, watcher_forum_chat_id=-100999)
            self.assertEqual(cfg_empty.effective_allowed_chat_ids, frozenset())

            cfg_restricted = self._make_config(
                root, allowed_chat_ids=frozenset({111}), watcher_forum_chat_id=-100999
            )
            self.assertEqual(cfg_restricted.effective_allowed_chat_ids, frozenset({111, -100999}))

    def test_quiet_hours_multi_tick_dedupe_flush_backoff_and_trigger_drain(self) -> None:
        srv = _MockFeedServer()
        try:
            srv.routes["/rss"] = (
                200,
                {},
                b'<rss><channel><item><guid>id1</guid><title>New Piano Opening</title><link>https://p.example/1</link><description>Sat 10am</description></item></channel></rss>',
            )
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                ws = root / "alpha"
                missions_dir = ws / "watchers" / "missions"
                state_dir = ws / "watchers" / "state"
                triggers_dir = ws / "watchers" / "triggers"
                missions_dir.mkdir(parents=True)
                state_dir.mkdir(parents=True)
                triggers_dir.mkdir(parents=True)

                mission = validate_mission(
                    {
                        "id": "weekly-piano",
                        "title": "Weekly Piano",
                        "status": "active",
                        "kind": "rss",
                        "created_at": "2026-11-01T08:00:00Z",
                        "schedule": {"interval_minutes": 15},
                        "config": {"url": f"{srv.base_url}/rss"},
                        "notify": {"priority": "normal", "quiet_hours": None, "target": None},
                    }
                )
                atomic_write_json(mission_path(missions_dir, "weekly-piano"), missions_dir, mission)

                write_trigger_file(triggers_dir, "weekly-piano")
                (triggers_dir / "BAD..NAME.req").write_text("bad", encoding="utf-8")
                in_flight_tmp = triggers_dir / ".other.req.12345678.tmp"
                in_flight_tmp.write_text("tmp", encoding="utf-8")

                w_tok = root / "watcher.token"
                ensure_ipc_token(w_tok)
                wcfg = WatcherRuntimeConfig(
                    ipc_host="127.0.0.1",
                    ipc_port=1,  # unreachable
                    watcher_token_path=w_tok,
                    timezone_name="America/Los_Angeles",
                    quiet_start="23:00",
                    quiet_end="08:00",
                    max_daily_web_scouts=2,
                    agy_bin="agy",
                    agy_model="",
                    agy_brain_dir=root / "brain",
                    scout_sandbox_dir=root / "scout",
                    missions_dir=missions_dir,
                    state_dir=state_dir,
                    triggers_dir=triggers_dir,
                )
                runner = WatcherRunner(wcfg, allow_loopback_for_tests=True)

                drained = runner.drain_triggers()
                self.assertEqual(drained, ["weekly-piano"])
                self.assertEqual([p.name for p in triggers_dir.iterdir()], [in_flight_tmp.name])
                in_flight_tmp.unlink()

                # 1. Tick 1 during quiet hours (23:30 PST) -> queues 1 item
                quiet_ts = 1795764600.0
                self.assertTrue(
                    is_in_quiet_hours(
                        mission,
                        now_ts=quiet_ts,
                        timezone_name="America/Los_Angeles",
                        default_start="23:00",
                        default_end="08:00",
                    )
                )
                res_q1 = runner.run_single_mission("weekly-piano", now_ts=quiet_ts)
                self.assertEqual(res_q1["outcome"], "quiet_held")

                # 2. Tick 2 during quiet hours (+20m, due again!) -> MUST NOT duplicate pending item!
                res_q2 = runner.run_single_mission("weekly-piano", now_ts=quiet_ts + 1200)
                self.assertEqual(res_q2["outcome"], "no_change")
                pend = safe_read_json(pending_path(state_dir, "weekly-piano"), state_dir)
                assert pend is not None
                self.assertEqual(len(pend["items"]), 1)

                # 3. Morning 08:30 PST -> decoupled flush fails -> sets next_flush_at backoff!
                morning_ts = quiet_ts + 9 * 3600
                res_m1 = runner.run_single_mission("weekly-piano", now_ts=morning_ts)
                self.assertEqual(res_m1["outcome"], "delivery_failed")

                # 4. 5 minutes later -> MUST honor next_flush_at backoff instead of hammering IPC!
                res_m2 = runner.run_single_mission("weekly-piano", now_ts=morning_ts + 300)
                self.assertEqual(res_m2["outcome"], "flush_backoff")
        finally:
            srv.close()

    def test_topic_ipc_token_isolation_reset_expired_resume_and_external_cwd_cli(self) -> None:
        tg = MockTelegramServer()
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                cfg = self._make_config(
                    root,
                    token=tg.expected_token,
                    api_base_url=tg.base_url,
                    allowed_chat_ids=frozenset({111}),
                    watcher_forum_chat_id=-100888,
                    ipc_port=0,
                )
                ipc_tok = ensure_ipc_token(cfg.ipc_token_path)
                w_tok = ensure_ipc_token(cfg.watcher_token_path)
                cfg.export_watcher_env()
                self.assertTrue(cfg.watcher_env_path.exists())

                store = BridgeStore(cfg.db_path)
                store.get_or_create_binding("telegram", "111", 0, "private", str(cfg.alpha_workspace))
                router = BridgeRouter(
                    cfg,
                    store,
                    AgyRunner(cfg),
                    TelegramClient(tg.expected_token, api_base_url=tg.base_url),
                )
                srv = start_ipc_server(cfg, router, ipc_tok, watcher_token=w_tok)
                port = srv.server_address[1]
                try:
                    # watcher.token MUST be rejected (401) on /watcher/topic and /approve!
                    c1, _ = post_bridge_ipc("127.0.0.1", port, cfg.watcher_token_path, "/watcher/topic", {"action": "create", "title": "x"})
                    c2, _ = post_bridge_ipc("127.0.0.1", port, cfg.watcher_token_path, "/approve", {"toolCall": {"name": "run_command"}})
                    self.assertEqual(c1, 401)
                    self.assertEqual(c2, 401)

                    # Verify external-cwd PYTHONPATH invocation (exact pattern from watch-missions/SKILL.md)
                    cli_env = dict(os.environ)
                    cli_env["PYTHONPATH"] = str(REPO_ROOT)
                    cli_proc = subprocess.run(
                        [sys.executable, "-m", "server.watchers", "--help"],
                        cwd=str(root),
                        env=cli_env,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertEqual(cli_proc.returncode, 0, cli_proc.stderr)

                    # ipc.token creates Mission Topic cleanly
                    c3, body = post_bridge_ipc(
                        "127.0.0.1",
                        port,
                        cfg.ipc_token_path,
                        "/watcher/topic",
                        {
                            "action": "create",
                            "title": "MV Piano 6yo",
                            "initial_request": "Piano lessons within 10 miles of Mountain View",
                            "opening_questions": "1. Private or group?",
                        },
                    )
                    self.assertEqual(c3, 200, body)
                    self.assertTrue(body["ok"])
                    mid = body["mission_id"]
                    tid = body["thread_id"]
                    self.assertIn(f"https://t.me/c/888/{tid}", body["deep_link"])

                    # Turn-1 envelope injects initial_request & mission tag
                    b = store.get_active_binding("telegram", "-100888", tid)
                    assert b is not None
                    self.assertEqual(b.mission_id, mid)
                    tag, init_ctx = router._build_mission_envelope_extras(b)
                    self.assertIn(mid, tag)
                    self.assertIn("Piano lessons within 10 miles", init_ctx)
                    self.assertIn("Private or group", init_ctx)

                    # Closing/reopening a SCOPING topic MUST NOT auto-activate it!
                    for ev in ("forum_topic_closed", "forum_topic_reopened"):
                        router.handle_telegram_update(
                            {
                                "message": {
                                    "chat": {"id": -100888, "type": "supergroup"},
                                    "from": {"id": 111, "is_bot": False},
                                    "message_thread_id": tid,
                                    ev: {},
                                }
                            }
                        )
                    m_after = load_mission(cfg.watcher_missions_dir, mid)
                    assert m_after is not None
                    self.assertEqual(m_after["status"], "scoping")

                    # /reset inside Mission Topic preserves mission_id on replacement binding
                    router._handle_command(
                        InboundMessage(
                            channel="telegram",
                            chat_id="-100888",
                            thread_id=tid,
                            chat_type="supergroup",
                            user_id=111,
                            sender_name="u",
                            message_id="1",
                            text="/reset",
                        )
                    )
                    b2 = store.get_active_binding("telegram", "-100888", tid)
                    assert b2 is not None
                    self.assertEqual(b2.mission_id, mid)
                    self.assertIsNone(b2.agy_conversation_id)

                    # Server-side expiry check rejects premature expired event
                    m_after["status"] = "active"
                    m_after["config"] = {"queries": ["piano mv"]}
                    atomic_write_json(mission_path(cfg.watcher_missions_dir, mid), cfg.watcher_missions_dir, m_after)
                    c4, b4 = post_bridge_ipc(
                        "127.0.0.1",
                        port,
                        cfg.watcher_token_path,
                        "/watcher",
                        {"mission_id": mid, "event_type": "expired"},
                    )
                    self.assertEqual(c4, 400)
                    self.assertEqual(b4.get("error"), "mission_not_expired")

                    # Resuming an expired archived mission MUST clear past expires_at!
                    m_after["status"] = "archived"
                    m_after["lifecycle"]["expires_at"] = "2020-01-01T00:00:00Z"
                    atomic_write_json(mission_path(cfg.watcher_missions_dir, mid), cfg.watcher_missions_dir, m_after)
                    ok_res, _msg = router._set_mission_status(
                        mid, "active", require_runnable=True, reopen_topic=True
                    )
                    self.assertTrue(ok_res)
                    m_resumed = load_mission(cfg.watcher_missions_dir, mid)
                    assert m_resumed is not None
                    self.assertEqual(m_resumed["status"], "active")
                    self.assertIsNone(m_resumed["lifecycle"]["expires_at"])

                    # Consecutive failure escalation (silent warning at >=2, no duplicate at 3, silent recovery!)
                    feed_srv = _MockFeedServer()
                    try:
                        # Start with broken RSS endpoint (404)
                        m_fail = validate_mission(
                            {
                                "id": mid,
                                "title": "MV Piano 6yo",
                                "status": "active",
                                "kind": "rss",
                                "created_at": "2026-11-01T08:00:00Z",
                                "schedule": {"interval_minutes": 15},
                                "config": {"url": f"{feed_srv.base_url}/missing"},
                                "notify": {
                                    "priority": "normal",
                                    "quiet_hours": None,
                                    "target": m_resumed["notify"]["target"],
                                },
                            }
                        )
                        atomic_write_json(mission_path(cfg.watcher_missions_dir, mid), cfg.watcher_missions_dir, m_fail)
                        wcfg_live = WatcherRuntimeConfig(
                            ipc_host="127.0.0.1",
                            ipc_port=port,
                            watcher_token_path=cfg.watcher_token_path,
                            timezone_name="America/Los_Angeles",
                            quiet_start="23:00",
                            quiet_end="08:00",
                            max_daily_web_scouts=2,
                            agy_bin="agy",
                            agy_model="",
                            agy_brain_dir=root / "brain",
                            scout_sandbox_dir=root / "scout",
                            missions_dir=cfg.watcher_missions_dir,
                            state_dir=cfg.watcher_state_dir,
                            triggers_dir=cfg.watcher_triggers_dir,
                        )
                        wrunner = WatcherRunner(wcfg_live, allow_loopback_for_tests=True)

                        # Server-side rejects failure_alert with failures < 2
                        c_low, b_low = post_bridge_ipc(
                            "127.0.0.1",
                            port,
                            cfg.watcher_token_path,
                            "/watcher",
                            {"mission_id": mid, "event_type": "failure_alert", "consecutive_failures": 1},
                        )
                        self.assertEqual(c_low, 400)
                        self.assertEqual(b_low.get("error"), "failure_threshold_not_met")

                        before_f1 = len(tg.sent_messages)
                        base_ts = 1795800000.0
                        # Failure #1: silent (0 alerts)
                        f1 = wrunner.run_single_mission(mid, now_ts=base_ts, force=True)
                        self.assertEqual(f1["outcome"], "collector_failed")
                        self.assertEqual(len(tg.sent_messages), before_f1)

                        # Failure #2: sends 1 silent [Watch Warning] into Mission Topic
                        f2 = wrunner.run_single_mission(mid, now_ts=base_ts + 4000, force=True)
                        self.assertEqual(f2["outcome"], "collector_failed")
                        self.assertEqual(len(tg.sent_messages), before_f1 + 1)
                        warn_msg = tg.sent_messages[-1]
                        self.assertIn("[Watch Warning]", warn_msg["text"])
                        self.assertEqual(warn_msg["thread_id"], tid)
                        self.assertTrue(warn_msg["disable_notification"])
                        self.assertEqual(warn_msg["link_preview_options"], {"is_disabled": True})

                        # Failure #3: does NOT spam a duplicate warning
                        f3 = wrunner.run_single_mission(mid, now_ts=base_ts + 12000, force=True)
                        self.assertEqual(f3["outcome"], "collector_failed")
                        self.assertEqual(len(tg.sent_messages), before_f1 + 1)

                        # Feed recovers (empty valid RSS -> no_change): sends 1 silent [Watch Recovered]
                        feed_srv.routes["/missing"] = (200, {}, b"<rss><channel></channel></rss>")
                        rec = wrunner.run_single_mission(mid, now_ts=base_ts + 25000, force=True)
                        self.assertEqual(rec["outcome"], "no_change")
                        self.assertEqual(len(tg.sent_messages), before_f1 + 2)
                        rec_msg = tg.sent_messages[-1]
                        self.assertIn("[Watch Recovered]", rec_msg["text"])
                        self.assertEqual(rec_msg["thread_id"], tid)
                        self.assertTrue(rec_msg["disable_notification"])
                        self.assertFalse(load_safe_state(cfg.watcher_state_dir, m_fail)["failure_alerted"])
                    finally:
                        feed_srv.close()
                finally:
                    srv.shutdown()
                    srv.server_close()
                    store.close()
        finally:
            tg.close()


if __name__ == "__main__":
    unittest.main()
