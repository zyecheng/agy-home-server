# Home AI Server (`agy-home-server`) — Workspace Rules

## Overview
This repository holds the staging VM automation, server code, `systemd` units, and deployment scripts for a 24/7 home AI server:
- **Staging target:** VirtualBox headless Ubuntu 24.04.5 LTS VM named `agy-staging` (`ssh agy-staging`, `127.0.0.1:2222 -> 22`, user `agy`, key `~/.ssh/id_ed25519_agy`).
- **Production target (upcoming):** HP Elite 600 G9 Mini PC (i5-12500T, 24 GB RAM, 256 GB NVMe, Intel vPro/AMT, wired Ethernet).
- **Architecture:** Host-driven coding & deployment over SSH + Telegram bot front-end (`agy-telegram.service`) alongside built-in Antigravity Remote Control.

## Managing the Staging VM
Always use the **`staging-vm`** skill (`.agents/skills/staging-vm/SKILL.md`) and its helper script `.agents/skills/staging-vm/scripts/vm.ps1` to start, stop, inspect, snapshot, restore, or deploy to `agy-staging`.

## Critical Host & VM Guardrails (Windows + VirtualBox WHPX/NEM)
1. **Keep `agy-staging` at 1 vCPU under WHPX NEM:** Windows Hyper-V / Memory Integrity forces VirtualBox into WHPX NEM fallback mode, where 2+ vCPUs can trigger a Linux 6.8 SMP boot spinlock. Keep `--cpus 1` unless native VT-x is enabled.
2. **Cold snapshots only:** Never run `VBoxManage snapshot ... --live` under WHPX NEM (stalls and can freeze guest networking on resume). Always shut down cleanly (`vm.ps1 stop`) before taking or restoring snapshots.
3. **No binary pipes in PowerShell 5.1:** Do not use `tar -cf - . | ssh ...` on Windows PowerShell 5.1 (stdout text-encoding corrupts binary streams). Always deploy via `scripts/deploy.ps1` (which packs a `.tar.gz` and uploads via `scp`).
4. **Unix LF line endings:** `.gitattributes` enforces LF on `.sh`, `.py`, `.service`, `.timer`, `.yml`, `.yaml`, `.env`, and cloud-init seed files.
5. **Secrets & large binaries:** Never commit `.env`, SSH private keys, or `vm/*.ova`, `vm/*.iso`, `vm/*.exe`.
6. **256 GB disk hygiene (HP Elite 600 G9):** Keep `journald` capped (`SystemMaxUse=500M`), avoid large monorepo checkouts on the 256 GB drive, and keep services lightweight.
