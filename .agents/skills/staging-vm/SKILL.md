---
name: staging-vm
description: >-
  Start, stop, inspect, snapshot, restore, and deploy code to the `agy-staging`
  VirtualBox Ubuntu 24.04 staging VM. Use this skill whenever starting a work
  session on the home AI server, ensuring `agy-staging` is booted and reachable
  over SSH, deploying updates to `agy-telegram.service`, checking remote logs,
  or managing VM snapshots (`clean-base`).
---

# Staging VM Management (`agy-staging`)

Use [vm.ps1](./scripts/vm.ps1) as the single entrypoint for managing the `agy-staging` VirtualBox VM on this Windows host.

## Quick Procedures

### 1. Start a work session (ensure VM is up and ready)
Run:
```powershell
powershell -ExecutionPolicy Bypass -File .agents\skills\staging-vm\scripts\vm.ps1 start
```
This checks `VBoxManage`, boots `agy-staging` headless if it is powered off, waits until passwordless `ssh agy-staging` succeeds (~30s under Windows WHPX), and prints VM health and `agy-telegram.service` status.

### 2. Check status & service logs
```powershell
powershell -ExecutionPolicy Bypass -File .agents\skills\staging-vm\scripts\vm.ps1 status
powershell -ExecutionPolicy Bypass -File .agents\skills\staging-vm\scripts\vm.ps1 logs
```

### 3. Deploy local changes to `agy-staging`
```powershell
powershell -ExecutionPolicy Bypass -File .agents\skills\staging-vm\scripts\vm.ps1 deploy
```
This packs the repo (excluding `.git`, `vm/`, `.env`, `.venv`), uploads via `scp` (PowerShell 5.1 binary-safe), normalizes line endings, reloads `systemd`, restarts `agy-telegram.service`, and verifies it is `active`.

### 4. Cleanly stop the VM
```powershell
powershell -ExecutionPolicy Bypass -File .agents\skills\staging-vm\scripts\vm.ps1 stop
```
Always prefer clean shutdown (`sudo poweroff`) over `VBoxManage controlvm poweroff`.

### 5. Cold snapshot & rollback (`clean-base`)
Never use `--live` snapshots under Windows WHPX/NEM. Use:
```powershell
# Take a new cold snapshot (cleanly shuts down, snapshots, restarts)
powershell -ExecutionPolicy Bypass -File .agents\skills\staging-vm\scripts\vm.ps1 snapshot -Name pre-agy-cli

# Roll back to the pristine post-cloud-init baseline and restart
powershell -ExecutionPolicy Bypass -File .agents\skills\staging-vm\scripts\vm.ps1 restore -Name clean-base
```

## Connection Details
- **SSH alias:** `agy-staging` (defined in `~/.ssh/config`)
- **Endpoint:** `agy@127.0.0.1:2222` using `~/.ssh/id_ed25519_agy`
- **Remote app directory:** `~/agy-server`
- **Service unit:** `/etc/systemd/system/agy-telegram.service`
- **Baseline snapshot:** `clean-base` (Ubuntu 24.04.5 LTS after `cloud-init`, 1 vCPU, 3072 MB RAM, before app deploy)

## Validation
After `start` or `deploy`, confirm:
1. `ssh agy-staging "systemctl is-system-running"` returns `running` (or `degraded` only if an expected optional unit failed).
2. `ssh agy-staging "systemctl is-active agy-telegram.service"` returns `active`.
