# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

"""Tier 1: Full-process E2E, high-concurrency stress, and chaos fault-injection suite.

Spawns the REAL `python3 -m server.bot` daemon against `MockTelegramServer` and
`tests/fake_agy.py` (which invokes the real `server/hook_approve.py` and
`server/hook_notify.py` subprocesses). Uses zero LLM quota and touches zero
production files or ports.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from server.config import validate_telegram_api_base_url
from tests.mock_telegram import MockTelegramServer

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_AGY = REPO_ROOT / "tests" / "fake_agy.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _proc_metrics(pid: int) -> dict[str, int]:
    """Read Linux RSS (KB) and open FD count when available."""
    out: dict[str, int] = {}
    status_p = Path(f"/proc/{pid}/status")
    if status_p.exists():
        for line in status_p.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("VmRSS:"):
                out["rss_kb"] = int(line.split()[1])
            elif line.startswith("Threads:"):
                out["threads"] = int(line.split()[1])
    fd_dir = Path(f"/proc/{pid}/fd")
    if fd_dir.exists():
        try:
            out["open_fds"] = len(list(fd_dir.iterdir()))
        except OSError:
            pass
    return out


class BotProcessHarness:
    """Manages an isolated real `python3 -m server.bot` daemon subprocess."""

    def __init__(
        self,
        *,
        root: Path,
        mock_tg: MockTelegramServer,
        agy_bin: Path,
        debounce_seconds: float = 0.1,
        approval_timeout_seconds: float = 10.0,
        poll_timeout_seconds: int = 1,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self.root = root
        self.mock_tg = mock_tg
        self.ipc_port = _free_port()
        self.db_path = root / "data" / "bridge.db"
        self.token_path = root / "data" / "ipc.token"
        self.brain_dir = root / "brain"
        self.workspace = root / "alpha"
        self.env_file = root / "test.env"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.brain_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Non-secret test.env so real hook_approve.py / hook_notify.py discover this isolated IPC
        self.env_file.write_text(
            "\n".join(
                [
                    "BRIDGE_IPC_HOST=127.0.0.1",
                    f"BRIDGE_IPC_PORT={self.ipc_port}",
                    f"BRIDGE_DB_PATH={self.db_path}",
                    f"BRIDGE_IPC_TOKEN_PATH={self.token_path}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        self.env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("TELEGRAM_", "BRIDGE_", "AGY_"))
        }
        self.env.update(
            {
                "BRIDGE_ENV_FILE": str(self.env_file),
                "TELEGRAM_BOT_TOKEN": mock_tg.expected_token,
                "TELEGRAM_API_BASE_URL": mock_tg.base_url,
                "TELEGRAM_ALLOWED_USER_IDS": "111",
                "POLL_TIMEOUT_SECONDS": str(poll_timeout_seconds),
                "DEBOUNCE_SECONDS": str(debounce_seconds),
                "APPROVAL_TIMEOUT_SECONDS": str(approval_timeout_seconds),
                "ALPHA_WORKSPACE": str(self.workspace),
                "AGY_BIN": str(agy_bin),
                "AGY_BRAIN_DIR": str(self.brain_dir),
                "FAKE_AGY_BRAIN_DIR": str(self.brain_dir),
                "AGY_TIMEOUT_SECONDS": "60",
                "PYTHONUNBUFFERED": "1",
            }
        )
        if extra_env:
            self.env.update(extra_env)

        self.proc: subprocess.Popen | None = None
        self.stderr_log = root / "bot.stderr.log"

    def start(self, timeout: float = 10.0) -> None:
        self._stderr_fh = self.stderr_log.open("ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "server.bot"],
            cwd=str(REPO_ROOT),
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=self._stderr_fh,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"bot.py exited prematurely rc={self.proc.returncode}:\n{self.read_stderr()}"
                )
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.ipc_port}/health", timeout=1
                ) as resp:
                    if resp.status == 200:
                        return
            except Exception:
                time.sleep(0.1)
        raise TimeoutError(f"bot.py /health not ready within {timeout}s:\n{self.read_stderr()}")

    def stop(self, timeout: float = 12.0) -> int:
        if self.proc is None:
            return 0
        if self.proc.poll() is None:
            if os.name == "nt":
                self.proc.terminate()
            else:
                self.proc.send_signal(signal.SIGTERM)
            try:
                rc = self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                rc = self.proc.wait(timeout=5)
        else:
            rc = self.proc.returncode
        try:
            self._stderr_fh.close()
        except Exception:
            pass
        self.proc = None
        return rc

    def read_stderr(self) -> str:
        if not self.stderr_log.exists():
            return ""
        return self.stderr_log.read_text(encoding="utf-8", errors="replace")

    def run_ssh_turn(self, conv_id: str, prompt: str) -> subprocess.CompletedProcess:
        ssh_env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("TELEGRAM_", "BRIDGE_", "AGY_"))
        }
        ssh_env["BRIDGE_ENV_FILE"] = str(self.env_file)
        ssh_env["FAKE_AGY_BRAIN_DIR"] = str(self.brain_dir)
        return subprocess.run(
            [
                sys.executable,
                str(FAKE_AGY),
                "-p",
                prompt,
                "--conversation",
                conv_id,
            ],
            cwd=str(self.workspace),
            env=ssh_env,
            capture_output=True,
            text=True,
            timeout=15,
        )


@unittest.skipIf(os.name == "nt", "Full-process POSIX executable E2E suite runs on Linux (agy-staging)")
class Tier1E2EStressChaosTest(unittest.TestCase):
    def setUp(self) -> None:
        os.chmod(FAKE_AGY, 0o755)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.tg = MockTelegramServer()
        self.bot: BotProcessHarness | None = None

    def tearDown(self) -> None:
        if self.bot is not None:
            self.bot.stop()
        self.tg.close()
        self.tmp.cleanup()

    def _check_db_integrity(self, db_path: Path) -> None:
        conn = sqlite3.connect(str(db_path))
        try:
            res = conn.execute("PRAGMA integrity_check;").fetchone()
            self.assertEqual(res[0], "ok")
        finally:
            conn.close()

    def test_01_full_lifecycle_e2e_including_hooks_approvals_long_md_and_ssh_mirror(self) -> None:
        # Security guard check: non-loopback non-Telegram base URLs must be rejected
        with self.assertRaises(ValueError):
            validate_telegram_api_base_url("https://evil.example.com")
        self.assertEqual(
            validate_telegram_api_base_url("https://api.telegram.org/"),
            "https://api.telegram.org",
        )

        self.bot = BotProcessHarness(
            root=self.root,
            mock_tg=self.tg,
            agy_bin=FAKE_AGY,
            debounce_seconds=0.1,
            approval_timeout_seconds=10.0,
        )
        self.bot.start()

        # 0. Verify bot registered Telegram slash command menu via setMyCommands
        ok = self.tg.wait_for(lambda: len(self.tg.registered_commands) >= 6, timeout=5.0)
        self.assertTrue(ok, f"setMyCommands not called: {self.tg.registered_commands}")
        cmd_names = {c["command"] for c in self.tg.registered_commands}
        self.assertTrue({"status", "mirror", "compact", "reset", "id", "help"}.issubset(cmd_names))

        # 1. Unauthorized & bot messages must be ignored
        self.tg.push_message(chat_id=111, user_id=999, text="intruder")
        self.tg.push_message(chat_id=111, user_id=111, is_bot=True, text="bot loop")

        # 2. Turn 1 tool call (BEFORE agy_conversation_id exists!) -> Approve
        def _policy(rec: dict) -> tuple[str, float, int]:
            if "rm -rf" in rec["text"]:
                return ("deny", 0.05, 111)
            return ("approve", 0.05, 111)

        self.tg.set_approval_policy(_policy)
        self.tg.push_message(chat_id=111, text="[tool=run_command cmd=uname -a]")

        ok = self.tg.wait_for(
            lambda: any("TOOL_APPROVED[" in m["text"] for m in self.tg.sent_messages),
            timeout=15.0,
        )
        self.assertTrue(ok, f"Approval flow timed out. msgs={self.tg.sent_messages}\nstderr={self.bot.read_stderr()}")
        self.assertTrue(any("Approved via Telegram" in e["text"] for e in self.tg.edited_messages))

        # Verify zero intruder echoes
        self.assertFalse(any("intruder" in m["text"] or "bot loop" in m["text"] for m in self.tg.sent_messages))

        # Extract assigned conversation_id via /id
        self.tg.push_message(chat_id=111, text="/id")
        ok = self.tg.wait_for(
            lambda: any("agy_conversation_id: fake-conv-" in m["text"] for m in self.tg.sent_messages),
            timeout=10.0,
        )
        self.assertTrue(ok)
        id_msg = next(m["text"] for m in self.tg.sent_messages if "agy_conversation_id: fake-conv-" in m["text"])
        conv_id = [ln.split(": ", 1)[1].strip() for ln in id_msg.splitlines() if ln.startswith("agy_conversation_id:")][0]

        # 3. Turn 2 tool call -> Deny -> triggers Stage-2 recovery cleanly
        self.tg.push_message(chat_id=111, text="[tool=run_command cmd=rm -rf /tmp/x]")
        ok = self.tg.wait_for(
            lambda: any("RECOVERY_OK" in m["text"] and "[permission_denied" in m["text"] for m in self.tg.sent_messages),
            timeout=15.0,
        )
        self.assertTrue(ok, f"Deny+recovery failed. msgs={self.tg.sent_messages}")
        self.assertTrue(any("Denied via Telegram" in e["text"] for e in self.tg.edited_messages))

        # 4. Markdown reply renders as Telegram HTML + long reply (>3500 chars, UTF-8) uploads .md
        self.tg.push_message(chat_id=111, text="check **bold** and `inline`")
        ok = self.tg.wait_for(
            lambda: any(
                m.get("parse_mode") == "HTML"
                and "<b>bold</b> and <code>inline</code>" in m["text"]
                for m in self.tg.sent_messages
            ),
            timeout=10.0,
        )
        self.assertTrue(ok, f"Markdown->HTML delivery failed. msgs={self.tg.sent_messages}")

        self.tg.push_message(chat_id=111, text="[long]")
        ok = self.tg.wait_for(lambda: len(self.tg.sent_documents) >= 1, timeout=15.0)
        self.assertTrue(ok, f"Long .md upload failed. stderr={self.bot.read_stderr()}")
        doc = self.tg.sent_documents[0]
        self.assertTrue(doc["filename"].endswith(".md"))
        self.assertGreater(len(doc["content"]), 3500)
        self.assertIn("Long Report Title — 中文测试 🚀", doc["caption"])
        self.assertIn("Full reply attached as .md", doc["caption"])

        # 5. Real SSH session invoking real hook_notify.py PreInvocation + Stop -> silent mirror
        ssh_proc = self.bot.run_ssh_turn(conv_id, "ssh live check")
        self.assertEqual(ssh_proc.returncode, 0, ssh_proc.stderr)
        ok = self.tg.wait_for(
            lambda: any(
                m["disable_notification"] and m["text"] == "[SSH] ssh live check"
                for m in self.tg.sent_messages
            )
            and any(
                m["disable_notification"] and f"ECHO[{conv_id}]: ssh live check" in m["text"]
                for m in self.tg.sent_messages
            ),
            timeout=10.0,
        )
        self.assertTrue(ok, f"SSH mirror missing. msgs={self.tg.sent_messages}")

        # 6. Local control commands (/status, /mirror, /compact, /reset)
        self.tg.push_message(chat_id=111, text="/status")
        self.tg.push_message(chat_id=111, text="/mirror replies_only")
        self.tg.push_message(chat_id=111, text="/compact")
        self.tg.push_message(chat_id=111, text="/reset")
        ok = self.tg.wait_for(
            lambda: any("SSH mirror mode set to: replies_only" in m["text"] for m in self.tg.sent_messages)
            and any("/compact finished (rc=0)" in m["text"] for m in self.tg.sent_messages)
            and any("Archived conversation" in m["text"] for m in self.tg.sent_messages),
            timeout=15.0,
        )
        self.assertTrue(ok, f"Commands failed. msgs={self.tg.sent_messages}")
        self.assertNotIn("SECRET_LEAK_DETECTED", self.bot.read_stderr())
        self._check_db_integrity(self.bot.db_path)

    def test_02_parallel_multi_topic_stress_and_resource_bounds(self) -> None:
        self.bot = BotProcessHarness(
            root=self.root,
            mock_tg=self.tg,
            agy_bin=FAKE_AGY,
            debounce_seconds=0.08,
            approval_timeout_seconds=15.0,
        )
        self.bot.start()
        assert self.bot.proc is not None
        pid = self.bot.proc.pid
        m_start = _proc_metrics(pid)

        # Auto-approve even topics, deny odd-cmd topics
        def _policy(rec: dict) -> tuple[str, float, int]:
            if "deny_me" in rec["text"]:
                return ("deny", 0.02, 111)
            return ("approve", 0.02, 111)

        self.tg.set_approval_policy(_policy)

        num_topics = 10
        supergroup_id = -100555
        t0 = time.time()

        # Wave 1: 10 topics each send a 3-message rapid burst (tests debounce coalescing under load)
        for idx in range(num_topics):
            tid = 100 + idx
            for part in range(3):
                self.tg.push_message(
                    chat_id=supergroup_id,
                    chat_type="supergroup",
                    thread_id=tid,
                    topic_name=f"Topic-{idx}",
                    text=f"topic{idx}-part{part}",
                )

        # Wait for all 10 coalesced Wave-1 replies
        ok = self.tg.wait_for(
            lambda: all(
                any(
                    m["thread_id"] == 100 + idx
                    and f"topic{idx}-part0" in m["text"]
                    and f"topic{idx}-part2" in m["text"]
                    for m in self.tg.sent_messages
                )
                for idx in range(num_topics)
            ),
            timeout=30.0,
        )
        self.assertTrue(ok, f"Wave 1 multi-topic burst incomplete. stderr={self.bot.read_stderr()}")

        # Wave 2: Simultaneous tool approvals (5 approve + 5 deny) across all 10 topics in parallel
        for idx in range(num_topics):
            tid = 100 + idx
            cmd = f"ok_cmd_{idx}" if idx % 2 == 0 else f"deny_me_{idx}"
            self.tg.push_message(
                chat_id=supergroup_id,
                chat_type="supergroup",
                thread_id=tid,
                text=f"[tool=run_command cmd={cmd}]",
            )

        ok = self.tg.wait_for(
            lambda: all(
                any(
                    m["thread_id"] == 100 + idx
                    and (
                        f"TOOL_APPROVED[" in m["text"] and f"ok_cmd_{idx}" in m["text"]
                        if idx % 2 == 0
                        else "RECOVERY_OK" in m["text"]
                    )
                    for m in self.tg.sent_messages
                )
                for idx in range(num_topics)
            ),
            timeout=40.0,
        )
        self.assertTrue(ok, f"Wave 2 parallel approvals incomplete. stderr={self.bot.read_stderr()}")

        # Wave 3: 50 additional sequential/parallel turns across topics to check resource stability
        for round_i in range(5):
            for idx in range(num_topics):
                tid = 100 + idx
                self.tg.push_message(
                    chat_id=supergroup_id,
                    chat_type="supergroup",
                    thread_id=tid,
                    text=f"soak-r{round_i}-t{idx}",
                )
            ok = self.tg.wait_for(
                lambda r=round_i: all(
                    any(
                        m["thread_id"] == 100 + idx and f"soak-r{r}-t{idx}" in m["text"]
                        for m in self.tg.sent_messages
                    )
                    for idx in range(num_topics)
                ),
                timeout=30.0,
            )
            self.assertTrue(ok, f"Wave 3 round {round_i} incomplete")

        elapsed = time.time() - t0
        m_end = _proc_metrics(pid)

        # Verify distinct conversation_ids for all 10 topics (no cross-topic bleed!)
        conn = sqlite3.connect(str(self.bot.db_path))
        try:
            rows = conn.execute(
                "SELECT thread_id, agy_conversation_id FROM bindings WHERE active = 1"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), num_topics)
        conv_ids = [r[1] for r in rows]
        self.assertEqual(len(set(conv_ids)), num_topics)

        # Resource hygiene assertions
        if "rss_kb" in m_end:
            self.assertLess(m_end["rss_kb"], 120_000, f"Unexpected RSS growth: {m_end}")
        if "open_fds" in m_end and "open_fds" in m_start:
            self.assertLess(m_end["open_fds"] - m_start["open_fds"], 40, f"FD leak detected: start={m_start} end={m_end}")

        print(
            f"\n[TIER1 STRESS METRICS] topics={num_topics} turns=70+ elapsed={elapsed:.2f}s "
            f"start={m_start} end={m_end}",
            flush=True,
        )
        self.assertNotIn("Traceback (most recent call last)", self.bot.read_stderr())
        self._check_db_integrity(self.bot.db_path)

    def test_03_chaos_fault_injection_timeout_and_sigterm_restart(self) -> None:
        self.bot = BotProcessHarness(
            root=self.root,
            mock_tg=self.tg,
            agy_bin=FAKE_AGY,
            debounce_seconds=0.0,
            approval_timeout_seconds=0.8,
        )
        self.bot.start()

        # 1. Inject Telegram 429 + 502 faults on sendMessage -> must retry & deliver cleanly
        self.tg.inject_faults(
            "sendMessage",
            [{"status": 429, "retry_after": 1}, {"status": 502}],
        )
        # Also inject a duplicate update_id -> must process only once!
        self.tg.push_message(chat_id=111, text="chaos-retry-msg", duplicate_count=2)

        ok = self.tg.wait_for(
            lambda: any("chaos-retry-msg" in m["text"] for m in self.tg.sent_messages),
            timeout=15.0,
        )
        self.assertTrue(ok, f"Retry delivery failed. stderr={self.bot.read_stderr()}")
        time.sleep(0.4)
        matches = [m for m in self.tg.sent_messages if "chaos-retry-msg" in m["text"]]
        self.assertEqual(len(matches), 1, f"Duplicate delivery detected: {matches}")
        self.assertGreaterEqual(len(self.tg.faults_triggered), 2)

        # 2. Inject getUpdates 502 + socket drop -> polling loop must recover automatically
        self.tg.inject_faults("getUpdates", [{"status": 502}, {"drop": True}])
        self.tg.push_message(chat_id=111, text="after-poll-fault")
        ok = self.tg.wait_for(
            lambda: any("after-poll-fault" in m["text"] for m in self.tg.sent_messages),
            timeout=20.0,
        )
        self.assertTrue(ok, f"Polling did not recover after faults. stderr={self.bot.read_stderr()}")

        # 3. Real PreToolUse approval timeout (0.8s) -> fail-closed deny + message edit
        self.tg.set_approval_policy(lambda _rec: ("ignore", 0.0, 111))
        self.tg.push_message(chat_id=111, text="[tool=run_command cmd=timeout_test]")
        ok = self.tg.wait_for(
            lambda: any("Timed out — auto-denied" in e["text"] for e in self.tg.edited_messages)
            and any("RECOVERY_OK" in m["text"] for m in self.tg.sent_messages),
            timeout=15.0,
        )
        self.assertTrue(ok, f"Approval timeout did not fail closed. edits={self.tg.edited_messages}")

        # 4. Mid-flight SIGTERM while a 30s approval is actively waiting -> fast fail-closed shutdown
        self.bot.stop(timeout=10.0)
        self.bot = BotProcessHarness(
            root=self.root,
            mock_tg=self.tg,
            agy_bin=FAKE_AGY,
            debounce_seconds=0.0,
            approval_timeout_seconds=30.0,
        )
        self.bot.start()
        before_prompts = len(self.tg.sent_messages)
        self.tg.push_message(chat_id=111, text="[tool=run_command cmd=sigterm_wait_cmd]")
        ok = self.tg.wait_for(
            lambda: any("sigterm_wait_cmd" in m["text"] for m in self.tg.sent_messages[before_prompts:]),
            timeout=10.0,
        )
        self.assertTrue(ok, "Approval prompt never arrived before SIGTERM")

        t_stop = time.time()
        rc = self.bot.stop(timeout=10.0)
        stop_elapsed = time.time() - t_stop
        self.assertEqual(rc, 0, f"Expected clean rc=0 on SIGTERM, got {rc}")
        self.assertLess(stop_elapsed, 8.0, f"SIGTERM hung waiting on approval ({stop_elapsed:.2f}s)")

        # 5. Restart against SAME SQLite DB -> offset persists (no duplicate replay) & accepts new turns
        msg_count_before_restart = len(self.tg.sent_messages)
        self.bot.start()
        self.tg.push_message(chat_id=111, text="post-restart-hello")
        ok = self.tg.wait_for(
            lambda: any("post-restart-hello" in m["text"] for m in self.tg.sent_messages[msg_count_before_restart:]),
            timeout=10.0,
        )
        self.assertTrue(ok, f"Post-restart turn failed. stderr={self.bot.read_stderr()}")
        # Pre-restart message must NOT be replayed
        replayed = [
            m
            for m in self.tg.sent_messages[msg_count_before_restart:]
            if "after-poll-fault" in m["text"] or "chaos-retry-msg" in m["text"]
        ]
        self.assertEqual(replayed, [], f"Offset did not persist across restart: {replayed}")
        self._check_db_integrity(self.bot.db_path)

    def test_04_sigkill_crash_recovery_reconciles_reply_and_emits_watchdog(self) -> None:
        notify_sock_path = self.root / "sd_notify.sock"
        notify_msgs: list[str] = []
        notify_stop = threading.Event()
        notify_srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        notify_srv.bind(str(notify_sock_path))
        notify_srv.settimeout(0.2)

        def _drain_notify() -> None:
            while not notify_stop.is_set():
                try:
                    data = notify_srv.recv(256)
                    notify_msgs.append(data.decode("utf-8", errors="replace"))
                except socket.timeout:
                    continue
                except OSError:
                    break

        drain_thread = threading.Thread(target=_drain_notify, daemon=True)
        drain_thread.start()

        try:
            self.bot = BotProcessHarness(
                root=self.root,
                mock_tg=self.tg,
                agy_bin=FAKE_AGY,
                debounce_seconds=0.0,
                approval_timeout_seconds=30.0,
                extra_env={"NOTIFY_SOCKET": str(notify_sock_path)},
            )
            self.bot.start()

            # 1. Establish DM binding & verify READY=1 + WATCHDOG=1 notifications
            self.tg.push_message(chat_id=111, text="before-crash")
            ok = self.tg.wait_for(
                lambda: any("before-crash" in m["text"] for m in self.tg.sent_messages),
                timeout=10.0,
            )
            self.assertTrue(ok)
            self.assertIn("READY=1", notify_msgs)
            self.assertIn("WATCHDOG=1", notify_msgs)

            conn = sqlite3.connect(str(self.bot.db_path))
            try:
                conv_id = conn.execute(
                    "SELECT agy_conversation_id FROM bindings WHERE active = 1"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertTrue(conv_id)

            # 2. Start a pending approval turn, then hard-kill bot.py with SIGKILL (kill -9)!
            self.tg.set_approval_policy(lambda _rec: ("ignore", 0.0, 111))
            before_prompt = len(self.tg.sent_messages)
            self.tg.push_message(chat_id=111, text="[tool=run_command cmd=kill9_cmd]")
            ok = self.tg.wait_for(
                lambda: any("kill9_cmd" in m["text"] for m in self.tg.sent_messages[before_prompt:]),
                timeout=10.0,
            )
            self.assertTrue(ok)

            assert self.bot.proc is not None
            self.bot.proc.send_signal(signal.SIGKILL)
            kill_rc = self.bot.proc.wait(timeout=5)
            self.assertEqual(kill_rc, -signal.SIGKILL)
            self.bot.proc = None
            try:
                self.bot._stderr_fh.close()
            except Exception:
                pass

            # Wait briefly so the orphaned fake_agy/hook_approve finishes failing closed
            time.sleep(0.4)

            # Simulate an agy reply that finished writing to disk right as bot.py was killed
            tpath = (
                self.bot.brain_dir
                / conv_id
                / ".system_generated"
                / "logs"
                / "transcript_full.jsonl"
            )
            existing_lines = [ln for ln in tpath.read_text(encoding="utf-8").splitlines() if ln.strip()]
            max_step = max(int(json.loads(ln)["step_index"]) for ln in existing_lines)
            with tpath.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "step_index": max_step + 1,
                            "type": "USER_INPUT",
                            "status": "DONE",
                            "content": "<USER_REQUEST>\n[channel: telegram | time: 2026-11-26]\nwhat happened?\n</USER_REQUEST>",
                        }
                    )
                    + "\n"
                )
                fh.write(
                    json.dumps(
                        {
                            "step_index": max_step + 2,
                            "type": "PLANNER_RESPONSE",
                            "status": "DONE",
                            "content": "STRANDED_REPLY_RECOVERED",
                        }
                    )
                    + "\n"
                )

            # 3. Restart bot.py -> must detect unclean exit, deliver stranded reply (no [SSH] prefix),
            #    and send silent recovery notice to primary DM!
            before_restart = len(self.tg.sent_messages)
            self.bot.start()
            ok = self.tg.wait_for(
                lambda: any(
                    m["text"] == "STRANDED_REPLY_RECOVERED"
                    for m in self.tg.sent_messages[before_restart:]
                )
                and any(
                    m["disable_notification"]
                    and "recovered from an unexpected restart" in m["text"]
                    and "delivered 1 pending message(s)" in m["text"]
                    for m in self.tg.sent_messages[before_restart:]
                ),
                timeout=10.0,
            )
            self.assertTrue(
                ok,
                f"SIGKILL recovery failed. post_restart={self.tg.sent_messages[before_restart:]}\nstderr={self.bot.read_stderr()}",
            )
            # Ensure no bogus [SSH] prompt echo for Telegram-originated stranded turn
            self.assertFalse(
                any(
                    m["text"].startswith("[SSH]")
                    for m in self.tg.sent_messages[before_restart:]
                )
            )

            # 4. Graceful SIGTERM sets clean_shutdown=1 and emits STOPPING=1; next start emits NO alert
            rc_clean = self.bot.stop(timeout=10.0)
            self.assertEqual(rc_clean, 0)
            time.sleep(0.1)
            self.assertIn("STOPPING=1", notify_msgs)

            before_clean_restart = len(self.tg.sent_messages)
            self.bot.start()
            self.tg.push_message(chat_id=111, text="after-clean-restart")
            ok = self.tg.wait_for(
                lambda: any(
                    "after-clean-restart" in m["text"]
                    for m in self.tg.sent_messages[before_clean_restart:]
                ),
                timeout=10.0,
            )
            self.assertTrue(ok)
            self.assertFalse(
                any(
                    "unexpected restart" in m["text"]
                    for m in self.tg.sent_messages[before_clean_restart:]
                ),
                f"Clean restart should not emit crash alert: {self.tg.sent_messages[before_clean_restart:]}",
            )
            self._check_db_integrity(self.bot.db_path)
        finally:
            notify_stop.set()
            notify_srv.close()

    def test_05_multimodal_attachments_album_voice_location_and_size_limit(self) -> None:
        self.bot = BotProcessHarness(
            root=self.root,
            mock_tg=self.tg,
            agy_bin=FAKE_AGY,
            debounce_seconds=0.15,
            extra_env={"MAX_ATTACHMENT_BYTES": "2048", "INBOX_RETENTION_DAYS": "7"},
        )
        self.bot.start()

        self.tg.register_file("pic-1", "photos/a.jpg", b"JPEGDATA1")
        self.tg.register_file("pic-2", "photos/b.jpg", b"JPEGDATA22")
        self.tg.register_file("voice-1", "voice/note.ogg", b"OGGAUDIO12345")
        self.tg.register_file("big-doc", "docs/too_big.pdf", b"X" * 4096)

        # 1. Unauthorized photo must NOT be downloaded
        self.tg.push_raw_message(
            {
                "from": {"id": 999, "is_bot": False},
                "chat": {"id": 111, "type": "private"},
                "photo": [{"file_id": "pic-1", "width": 100, "height": 100}],
            }
        )

        # 2. Two-photo album with caption on first photo -> coalesced into ONE agy turn with 2 staged files!
        self.tg.push_raw_message(
            {
                "from": {"id": 111, "is_bot": False},
                "chat": {"id": 111, "type": "private"},
                "caption": "compare these two",
                "photo": [{"file_id": "pic-1", "width": 640, "height": 480, "file_size": 9}],
            }
        )
        self.tg.push_raw_message(
            {
                "from": {"id": 111, "is_bot": False},
                "chat": {"id": 111, "type": "private"},
                "photo": [{"file_id": "pic-2", "width": 640, "height": 480, "file_size": 10}],
            }
        )
        ok = self.tg.wait_for(
            lambda: any("ATTACHED_OK[2:19:" in m["text"] and "compare these two" in m["text"] for m in self.tg.sent_messages),
            timeout=15.0,
        )
        self.assertTrue(ok, f"Album staging failed. msgs={self.tg.sent_messages}\nstderr={self.bot.read_stderr()}")

        # 3. Captionless voice note -> stages .ogg and supplies voice default prompt
        before_voice = len(self.tg.sent_messages)
        self.tg.push_raw_message(
            {
                "from": {"id": 111, "is_bot": False},
                "chat": {"id": 111, "type": "private"},
                "voice": {"file_id": "voice-1", "mime_type": "audio/ogg", "duration": 3, "file_size": 13},
            }
        )
        ok = self.tg.wait_for(
            lambda: any(
                "ATTACHED_OK[1:13:" in m["text"] and ".ogg" in m["text"]
                for m in self.tg.sent_messages[before_voice:]
            ),
            timeout=15.0,
        )
        self.assertTrue(ok, f"Voice staging failed. msgs={self.tg.sent_messages[before_voice:]}")

        # 4. Location-only message -> routed cleanly with coordinates
        before_loc = len(self.tg.sent_messages)
        self.tg.push_raw_message(
            {
                "from": {"id": 111, "is_bot": False},
                "chat": {"id": 111, "type": "private"},
                "location": {"latitude": 37.3346, "longitude": -122.009},
            }
        )
        ok = self.tg.wait_for(
            lambda: any("lat=37.334600" in m["text"] for m in self.tg.sent_messages[before_loc:]),
            timeout=15.0,
        )
        self.assertTrue(ok, f"Location routing failed. msgs={self.tg.sent_messages[before_loc:]}")

        # 5. Oversized document (>2048B) without caption -> rejected cleanly with skip notice, no crash
        before_big = len(self.tg.sent_messages)
        self.tg.push_raw_message(
            {
                "from": {"id": 111, "is_bot": False},
                "chat": {"id": 111, "type": "private"},
                "document": {
                    "file_id": "big-doc",
                    "file_name": "too_big.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 4096,
                },
            }
        )
        ok = self.tg.wait_for(
            lambda: any(
                "Could not attach:" in m["text"] and "too_big.pdf" in m["text"]
                for m in self.tg.sent_messages[before_big:]
            ),
            timeout=15.0,
        )
        self.assertTrue(ok, f"Oversized notice missing. msgs={self.tg.sent_messages[before_big:]}")
        self.assertNotIn("Traceback (most recent call last)", self.bot.read_stderr())
        self._check_db_integrity(self.bot.db_path)

    def test_06_watcher_missions_e2e_and_stress(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from server.watchers.schema import atomic_write_json, mission_path, validate_mission

        forum_chat_id = -100777888
        scout_sandbox = self.root / "scout-sandbox"
        (scout_sandbox / ".agents").mkdir(parents=True, exist_ok=True)
        (scout_sandbox / ".agents" / "hooks.json").write_text(
            json.dumps({"hooks": {"PreToolUse": []}}), encoding="utf-8"
        )
        (scout_sandbox / ".agents" / "settings.json").write_text(
            json.dumps({"trustedWorkspaces": [str(scout_sandbox)]}), encoding="utf-8"
        )

        # Local mock HTTP feed server for 25 concurrent missions (RSS, GitHub Releases, HTML)
        feed_routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
        for i in range(8):
            marker = "[NO_ALERT_MARKER]" if i == 7 else ("[EXFIL_LINK]" if i == 6 else "")
            feed_routes[f"/rss/{i}.xml"] = (
                200,
                {"ETag": f'"rss-{i}-v1"'},
                (
                    f'<?xml version="1.0"?><rss><channel><item>'
                    f"<guid>g-{i}-1</guid><title>RSS Item {i}</title>"
                    f"<link>https://feeds.example/rss/{i}</link>"
                    f"<description>Fresh update {i} {marker}</description>"
                    f"</item></channel></rss>"
                ).encode("utf-8"),
            )
        for i in range(6):
            feed_routes[f"/repos/acme/repo{i}/releases?per_page=20"] = (
                200,
                {"ETag": f'"gh-{i}-v1"'},
                json.dumps(
                    [
                        {
                            "tag_name": f"v1.{i}.0",
                            "name": f"Release v1.{i}.0",
                            "prerelease": False,
                            "html_url": f"https://github.example/acme/repo{i}/releases/v1.{i}.0",
                            "body": f"Changelog for repo{i}",
                        }
                    ]
                ).encode("utf-8"),
            )
        for i in range(6):
            feed_routes[f"/page/{i}"] = (
                200,
                {"Content-Type": "text/html"},
                f"<html><script>var nonce='{i}xyz';</script><body><h1>Portal {i}</h1><p>Open Spots Available</p></body></html>".encode(
                    "utf-8"
                ),
            )

        class FeedHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args) -> None:
                return

            def do_GET(self) -> None:
                entry = feed_routes.get(self.path)
                if entry is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                code, hdrs, body = entry
                if hdrs.get("ETag") and self.headers.get("If-None-Match") == hdrs["ETag"]:
                    self.send_response(304)
                    self.send_header("ETag", hdrs["ETag"])
                    self.end_headers()
                    return
                self.send_response(code)
                for k, v in hdrs.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        feed_srv = ThreadingHTTPServer(("127.0.0.1", 0), FeedHandler)
        feed_base = f"http://127.0.0.1:{feed_srv.server_address[1]}"
        threading.Thread(target=feed_srv.serve_forever, daemon=True).start()

        try:
            self.bot = BotProcessHarness(
                root=self.root,
                mock_tg=self.tg,
                agy_bin=FAKE_AGY,
                debounce_seconds=0.05,
                extra_env={
                    "WATCHER_FORUM_CHAT_ID": str(forum_chat_id),
                    "SCOUT_SANDBOX_DIR": str(scout_sandbox),
                    "WATCHER_QUIET_START": "00:00",
                    "WATCHER_QUIET_END": "00:00",  # Quiet hours off for stress run
                    "MAX_DAILY_WEB_SCOUTS": "20",
                    "WATCHER_STAGE2_TIMEOUT_SECONDS": "25",
                },
            )
            self.bot.start()
            assert self.bot.proc is not None
            pid = self.bot.proc.pid
            m_start = _proc_metrics(pid)

            # 1. Establish DM binding & test /newwatch (0-LLM Forum Topic creation + deep link)
            self.tg.push_message(chat_id=111, text="hello dm")
            self.assertTrue(
                self.tg.wait_for(lambda: any("hello dm" in m["text"] for m in self.tg.sent_messages), timeout=10.0)
            )

            before_nw = len(self.tg.sent_messages)
            self.tg.push_message(chat_id=111, text="/newwatch MV Piano Lessons")
            ok = self.tg.wait_for(
                lambda: len(self.tg.created_topics) >= 1
                and any("https://t.me/c/777888/" in m["text"] for m in self.tg.sent_messages[before_nw:]),
                timeout=10.0,
            )
            self.assertTrue(ok, f"/newwatch failed: {self.tg.sent_messages[before_nw:]}")
            created_tid = self.tg.created_topics[0]["message_thread_id"]

            # Turn 1 inside new Mission Topic must receive [mission: ...] and [initial_request: ...]
            before_t1 = len(self.tg.sent_messages)
            self.tg.push_message(
                chat_id=forum_chat_id,
                chat_type="supergroup",
                thread_id=created_tid,
                text="weekends preferred, under 5 miles",
            )
            ok = self.tg.wait_for(
                lambda: any(
                    m["thread_id"] == created_tid and "weekends preferred" in m["text"]
                    for m in self.tg.sent_messages[before_t1:]
                ),
                timeout=10.0,
            )
            self.assertTrue(ok, f"Mission Topic Turn 1 failed: {self.tg.sent_messages[before_t1:]}")

            # /watch status, /watch pause, /watch archive inside the topic (no [id] needed!)
            self.tg.push_message(chat_id=forum_chat_id, chat_type="supergroup", thread_id=created_tid, text="/watch status")
            self.tg.push_message(chat_id=forum_chat_id, chat_type="supergroup", thread_id=created_tid, text="/watch archive")
            ok = self.tg.wait_for(
                lambda: any("Mission: MV Piano Lessons" in m["text"] for m in self.tg.sent_messages)
                and len(self.tg.closed_topics) >= 1,
                timeout=10.0,
            )
            self.assertTrue(ok, f"/watch status/archive failed: {self.tg.sent_messages}")

            # 2. Seed 25 active missions (8 RSS + 6 GitHub + 6 HTML + 5 web_scout) across Mission Topics
            missions_dir = self.bot.workspace / "watchers" / "missions"
            missions_dir.mkdir(parents=True, exist_ok=True)
            total_missions = 25
            for i in range(8):
                tid = 600 + i
                m = validate_mission(
                    {
                        "id": f"stress-rss-{i}",
                        "title": f"RSS Stress {i}",
                        "status": "active",
                        "kind": "rss",
                        "schedule": {"interval_minutes": 15},
                        "config": {"url": f"{feed_base}/rss/{i}.xml"},
                        "notify": {"priority": "normal", "quiet_hours": {"disabled": True}, "target": {"chat_id": str(forum_chat_id), "thread_id": tid}},
                    }
                )
                atomic_write_json(mission_path(missions_dir, m["id"]), missions_dir, m)
            for i in range(6):
                tid = 610 + i
                m = validate_mission(
                    {
                        "id": f"stress-gh-{i}",
                        "title": f"GH Stress {i}",
                        "status": "active",
                        "kind": "github_releases",
                        "schedule": {"interval_minutes": 15},
                        "config": {"repo": f"acme/repo{i}", "api_base_url": feed_base},
                        "notify": {"priority": "normal", "quiet_hours": {"disabled": True}, "target": {"chat_id": str(forum_chat_id), "thread_id": tid}},
                    }
                )
                atomic_write_json(mission_path(missions_dir, m["id"]), missions_dir, m)
            for i in range(6):
                tid = 620 + i
                m = validate_mission(
                    {
                        "id": f"stress-html-{i}",
                        "title": f"HTML Stress {i}",
                        "status": "active",
                        "kind": "http_diff",
                        "schedule": {"interval_minutes": 15},
                        "config": {"url": f"{feed_base}/page/{i}"},
                        "notify": {"priority": "normal", "quiet_hours": {"disabled": True}, "target": {"chat_id": str(forum_chat_id), "thread_id": tid}},
                    }
                )
                atomic_write_json(mission_path(missions_dir, m["id"]), missions_dir, m)
            for i in range(5):
                tid = 630 + i
                tag = "NO_ALERT_SCOUT" if i == 4 else ("EXFIL_SCOUT" if i == 3 else f"piano {i}")
                m = validate_mission(
                    {
                        "id": f"stress-scout-{i}",
                        "title": f"Scout Stress {i}",
                        "status": "active",
                        "kind": "web_scout",
                        "schedule": {"interval_minutes": 60},
                        "config": {"queries": [f"Mountain View {tag}"]},
                        "notify": {"priority": "normal", "quiet_hours": {"disabled": True}, "target": {"chat_id": str(forum_chat_id), "thread_id": tid}},
                    }
                )
                atomic_write_json(mission_path(missions_dir, m["id"]), missions_dir, m)

            # 3. Run 2 CONCURRENT watcher_runner.py processes (flock contention!) + 8 concurrent interactive DMs!
            watcher_env_file = self.root / "watcher-ipc" / "watcher.env"
            self.assertTrue(watcher_env_file.exists(), "bot.py did not export watcher-ipc/watcher.env")
            runner_env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("TELEGRAM_", "BRIDGE_", "AGY_", "WATCHER_"))
            }
            runner_env["BRIDGE_ENV_FILE"] = str(watcher_env_file)
            runner_env["WATCHER_ALLOW_LOOPBACK_FOR_TESTS"] = "1"
            runner_env["FAKE_AGY_BRAIN_DIR"] = str(self.bot.brain_dir)

            t0 = time.time()
            before_stress = len(self.tg.sent_messages)

            p_run1 = subprocess.Popen(
                [sys.executable, "-m", "server.watchers.runner"],
                cwd=str(REPO_ROOT),
                env=runner_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            p_run2 = subprocess.Popen(
                [sys.executable, "-m", "server.watchers.runner"],
                cwd=str(REPO_ROOT),
                env=runner_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            # Send 8 interactive DM messages WHILE the 25 watcher missions are executing!
            for dm_i in range(8):
                self.tg.push_message(chat_id=111, text=f"concurrent-dm-{dm_i}")

            out1, err1 = p_run1.communicate(timeout=90)
            out2, err2 = p_run2.communicate(timeout=90)
            self.assertEqual(p_run1.returncode, 0, f"runner1 failed: {err1}")
            self.assertEqual(p_run2.returncode, 0, f"runner2 failed: {err2}")

            # Run one more tick so remaining 2 web_scouts (per-tick budget=3) complete too
            p_run3 = subprocess.run(
                [sys.executable, "-m", "server.watchers.runner"],
                cwd=str(REPO_ROOT),
                env=runner_env,
                capture_output=True,
                text=True,
                timeout=90,
            )
            self.assertEqual(p_run3.returncode, 0, f"runner3 failed: {p_run3.stderr}")

            # Verify all 8 interactive DMs completed cleanly
            ok = self.tg.wait_for(
                lambda: all(
                    any(f"concurrent-dm-{dm_i}" in m["text"] for m in self.tg.sent_messages[before_stress:])
                    for dm_i in range(8)
                ),
                timeout=20.0,
            )
            self.assertTrue(ok, "Interactive DMs did not finish during watcher stress")

            # Verify delivered watcher briefs:
            # 25 missions total minus 2 NO_ALERT missions (stress-rss-7 and stress-scout-4) = 23 briefs!
            watcher_msgs = [
                m for m in self.tg.sent_messages[before_stress:] if m["chat_id"] == forum_chat_id
            ]
            self.assertEqual(len(watcher_msgs), 23, f"Expected 23 briefs (25 - 2 NO_ALERT), got {len(watcher_msgs)}")
            # Every watcher brief MUST disable link previews & strip injected evil/tg:// links!
            for wm in watcher_msgs:
                self.assertEqual(wm["link_preview_options"], {"is_disabled": True})
                self.assertNotIn("evil.example", wm["text"])
                self.assertNotIn("evil2.example", wm["text"])
                self.assertNotIn("tg://", wm["text"])
                self.assertNotIn("NO_ALERT", wm["text"])

            # 4. Unchanged second tick MUST produce 0 new watcher messages (ETag 304 + dedupe!)
            before_tick2 = len(self.tg.sent_messages)
            p_run4 = subprocess.run(
                [sys.executable, "-m", "server.watchers.runner", "--force"],
                cwd=str(REPO_ROOT),
                env=runner_env,
                capture_output=True,
                text=True,
                timeout=90,
            )
            self.assertEqual(p_run4.returncode, 0, p_run4.stderr)
            after_tick2 = [
                m for m in self.tg.sent_messages[before_tick2:] if m["chat_id"] == forum_chat_id
            ]
            self.assertEqual(after_tick2, [], f"Unchanged tick emitted duplicate alerts: {after_tick2}")

            # 5. Fast SIGTERM shutdown during an active slow Stage-2 briefing (<6s, clean rc=0!)
            slow_m = validate_mission(
                {
                    "id": "slow-stage2",
                    "title": "Slow Stage2",
                    "status": "active",
                    "kind": "rss",
                    "schedule": {"interval_minutes": 15},
                    "config": {"url": f"{feed_base}/rss/0.xml"},
                    "notify": {"priority": "normal", "quiet_hours": {"disabled": True}, "target": None},
                }
            )
            atomic_write_json(mission_path(missions_dir, "slow-stage2"), missions_dir, slow_m)

            from server.config import post_bridge_ipc

            slow_res: list[tuple[int, dict]] = []

            def _call_slow() -> None:
                slow_res.append(
                    post_bridge_ipc(
                        "127.0.0.1",
                        self.bot.ipc_port,
                        self.root / "watcher-ipc" / "watcher.token",
                        "/watcher",
                        {
                            "mission_id": "slow-stage2",
                            "event_type": "delta",
                            "items": [{"title": "[STAGE2_SLOW]", "url": "https://feeds.example/slow", "summary": "[STAGE2_SLOW]"}],
                        },
                        timeout=30.0,
                    )
                )

            t_slow = threading.Thread(target=_call_slow, daemon=True)
            t_slow.start()
            time.sleep(0.5)
            t_stop = time.time()
            rc_stop = self.bot.stop(timeout=10.0)
            stop_dt = time.time() - t_stop
            t_slow.join(timeout=5.0)

            self.assertEqual(rc_stop, 0, f"Expected clean rc=0 on SIGTERM during Stage-2, got {rc_stop}")
            self.assertLess(stop_dt, 6.0, f"SIGTERM stalled during Stage-2 ({stop_dt:.2f}s)")
            self.assertEqual(len(slow_res), 1)
            self.assertIn(slow_res[0][0], (0, 503), f"Unexpected Stage-2 SIGTERM status: {slow_res}")

            elapsed = time.time() - t0
            print(
                f"\n[WATCHER 25-MISSION STRESS] missions={total_missions} briefs=23 "
                f"elapsed={elapsed:.2f}s sigterm_stop={stop_dt:.2f}s start={m_start}",
                flush=True,
            )
            self.assertNotIn("SECRET_LEAK_DETECTED", self.bot.read_stderr())
            self.assertNotIn("SCOUT_GUARD_FAILED", self.bot.read_stderr())
            self.assertNotIn("STAGE2_LOCKOUT_FAILED", self.bot.read_stderr())
            self.assertNotIn("Traceback (most recent call last)", self.bot.read_stderr())
            self._check_db_integrity(self.bot.db_path)
        finally:
            feed_srv.shutdown()
            feed_srv.server_close()


if __name__ == "__main__":
    unittest.main()
