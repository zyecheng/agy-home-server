<!-- Copyright (c) 2026 zyecheng | SPDX-License-Identifier: BSD-3-Clause -->

# Antigravity 24/7 Home AI Server (`agy-home-server`)

Staging (VirtualBox Ubuntu 24.04 LTS) and deployment scaffolding for a 24/7 home AI server (HP Elite 600 G9 Mini PC) with a Telegram bridge + `systemd` services.

## Quick Commands (Windows PowerShell)

```powershell
# SSH into staging VM (configured in ~/.ssh/config)
ssh agy-staging

# Deploy latest repo to staging VM (or swap to hp-agy once hardware arrives)
.\scripts\deploy.ps1 -TargetHost agy-staging

# Check service status & logs
ssh agy-staging "systemctl status agy-telegram.service --no-pager && journalctl -u agy-telegram.service -n 30 --no-pager"

# Snapshot / rollback staging VM
& "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe" snapshot agy-staging list
& "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe" controlvm agy-staging acpipowerbutton
& "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe" snapshot agy-staging restore clean-base
& "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe" startvm agy-staging --type headless
```

## Enabling the Telegram Bot

1. Create a bot with `@BotFather` on Telegram and copy the token.
2. Create `/home/agy/agy-server/.env` on the server (using `.env.example` as a template):
   ```bash
   cp ~/agy-server/.env.example ~/agy-server/.env
   chmod 600 ~/agy-server/.env
   nano ~/agy-server/.env
   sudo systemctl restart agy-telegram.service
   ```

## Notes for Windows Host (VirtualBox + WHPX/Hyper-V)

- `scripts/setup-vm.ps1` uses `1 vCPU` by default when Windows Hyper-V / Memory Integrity forces VirtualBox into WHPX (NEM) fallback mode, avoiding a Linux 6.8 multi-vCPU SMP spinlock on boot.
- Always take **cold** snapshots (after `sudo poweroff`) rather than `--live` snapshots under WHPX NEM.
