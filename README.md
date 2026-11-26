<!-- Copyright (c) 2026 zyecheng | SPDX-License-Identifier: BSD-3-Clause -->

# Antigravity 24/7 Home AI Server (`agy-home-server`)

> [!NOTE]
> **Personal Hobby Project:** This repository is a personal hobby project built specifically for the author's own home-server setup, workflow, and bilingual assistant persona (**Alpha**). It is shared publicly as-is under the [BSD-3-Clause license](LICENSE) for reference and inspiration rather than as a turnkey general-purpose product. Expect opinionated paths, Windows-host staging scripts, and hardware assumptions tailored to the author's environment.

Staging (VirtualBox Ubuntu 24.04 LTS) and deployment scaffolding for a 24/7 personal home AI server powered by the **Antigravity CLI (`agy`)**, with a zero-dependency Python 3.12 stdlib + SQLite (WAL) Telegram bridge and hardened `systemd` services.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for full architecture diagrams, threat boundaries, and lifecycle details.

## Highlights

- **Telegram DM & Forum Topic Routing:** Maps a 1:1 DM to a persistent `agy` conversation and each Supergroup Forum Topic to its own isolated conversation in SQLite (WAL).
- **Interactive `PreToolUse` Approvals:** Gates non-read-only tools via fail-closed Telegram `[Approve]` / `[Deny]` inline buttons (single-use cryptographic nonces, 120s TTL, full payload attachments when truncated).
- **SSH ↔ Telegram Sync:** Mirrors interactive SSH `agy` sessions (`alpha-ssh`) silently to Telegram via `PreInvocation` and `Stop` hooks (`full` | `replies_only` | `muted`).
- **Mobile-Friendly Rendering:** Converts Markdown replies (`<= 3,500` chars) to Telegram HTML with automatic plain-text fallback, uploads longer replies as `.md` files with a 2-line preview, and registers native `/` slash commands on startup.
- **24/7 Resilience & Hardening:** `systemd` `Type=notify` watchdog (`WatchdogSec=120`), startup transcript reconciliation and unclean-restart alerts, scrubbed child environments, authenticated loopback IPC (`X-Bridge-Token`), and `ProtectSystem=strict` / `NoNewPrivileges=yes` isolation.
- **Automated Testing:** Unit tests plus Tier 1 full-process E2E / 10-topic stress / chaos (`429`/`502`/`SIGTERM`/`SIGKILL`) and opt-in Tier 2 real-`agy` automated E2E suites.

## Quick Commands (Windows PowerShell)

```powershell
# SSH into staging VM (configured in ~/.ssh/config)
ssh agy-staging

# Deploy latest repo to staging VM (or swap to target host)
powershell -ExecutionPolicy Bypass -File .\scripts\deploy.ps1 -TargetHost agy-staging

# Check service status & logs
ssh agy-staging "systemctl status agy-telegram.service --no-pager && journalctl -u agy-telegram.service -n 30 --no-pager"

# Run offline Unit + Tier 1 E2E / Stress / Chaos tests on the server
ssh agy-staging "cd ~/agy-server && python3 -m unittest discover -s tests -v"

# Snapshot / rollback staging VM
& "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe" snapshot agy-staging list
& "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe" controlvm agy-staging acpipowerbutton
& "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe" snapshot agy-staging restore clean-base
& "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe" startvm agy-staging --type headless
```

## Enabling the Telegram Bot

1. Create a bot with `@BotFather` on Telegram and copy the token.
2. Create `/home/agy/agy-server/.env` on the server (using `.env.example` as a template) or use `scripts/set-bot-token.ps1`:
   ```bash
   cp ~/agy-server/.env.example ~/agy-server/.env
   chmod 600 ~/agy-server/.env
   nano ~/agy-server/.env
   sudo systemctl restart agy-telegram.service
   ```

## Notes for Windows Host (VirtualBox + WHPX/Hyper-V)

- `scripts/setup-vm.ps1` uses `1 vCPU` by default when Windows Hyper-V / Memory Integrity forces VirtualBox into WHPX (NEM) fallback mode, avoiding a Linux 6.8 multi-vCPU SMP spinlock on boot.
- Always take **cold** snapshots (after `sudo poweroff`) rather than `--live` snapshots under WHPX NEM.

## License

Licensed under the [BSD 3-Clause License](LICENSE).
