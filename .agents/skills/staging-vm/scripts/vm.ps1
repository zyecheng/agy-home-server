# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("start", "stop", "status", "deploy", "logs", "snapshot", "restore")]
    [string]$Action,

    [string]$VmName = "agy-staging",
    [string]$SshHost = "agy-staging",
    [string]$Name = "clean-base",
    [int]$LogLines = 30
)

$ErrorActionPreference = "Stop"
$VBox = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..\..")).Path

if (-not (Test-Path $VBox)) {
    throw "VBoxManage.exe not found at $VBox. Install Oracle VirtualBox first."
}

function Get-VmState {
    $line = (& $VBox showvminfo $VmName --machinereadable 2>$null | Select-String '^VMState=').Line
    if (-not $line) { return "missing" }
    if ($line -match 'VMState="([^"]+)"') { return $Matches[1] }
    return "unknown"
}

function Wait-SshReady {
    param([int]$TimeoutSeconds = 90)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $out = ssh -o BatchMode=yes -o ConnectTimeout=5 $SshHost "echo SSH_OK" 2>&1
        if ($LASTEXITCODE -eq 0 -and ($out -match "SSH_OK")) {
            return $true
        }
        Start-Sleep -Seconds 3
    }
    return $false
}

function Stop-VmCleanly {
    $state = Get-VmState
    if ($state -eq "poweroff") {
        Write-Host "$VmName is already powered off."
        return
    }
    if ($state -eq "running") {
        Write-Host "Sending clean sudo poweroff to $SshHost..."
        ssh -o BatchMode=yes -o ConnectTimeout=5 $SshHost "sudo poweroff" 2>$null | Out-Null
        for ($i = 0; $i -lt 30; $i++) {
            if ((Get-VmState) -eq "poweroff") {
                Write-Host "$VmName powered off cleanly."
                return
            }
            Start-Sleep -Seconds 2
        }
        Write-Host "Clean shutdown timed out; sending ACPI power button..."
        & $VBox controlvm $VmName acpipowerbutton 2>$null | Out-Null
        Start-Sleep -Seconds 10
    }
    if ((Get-VmState) -ne "poweroff") {
        Write-Host "Forcing poweroff on $VmName..."
        & $VBox controlvm $VmName poweroff
        Start-Sleep -Seconds 2
    }
}

function Start-VmAndWait {
    $state = Get-VmState
    if ($state -eq "missing") {
        throw "VM '$VmName' does not exist. Run scripts\setup-vm.ps1 first."
    }
    if ($state -ne "running") {
        Write-Host "Starting $VmName headless (current state: $state)..."
        & $VBox startvm $VmName --type headless
        if ($LASTEXITCODE -ne 0) { throw "VBoxManage startvm failed" }
        Write-Host "Waiting for SSH on $SshHost (~25-35s under Windows WHPX)..."
        Start-Sleep -Seconds 15
    } else {
        Write-Host "$VmName is already running."
    }

    if (-not (Wait-SshReady -TimeoutSeconds 90)) {
        throw "Timed out waiting for SSH on $SshHost."
    }
    Write-Host "SSH is ready on $SshHost!"
    ssh -o BatchMode=yes $SshHost "echo Host: `$(hostname); echo Uptime: `$(uptime -p); echo System: `$(systemctl is-system-running || true); echo Service: `$(systemctl is-active agy-telegram.service || true)"
}

switch ($Action) {
    "start" {
        Start-VmAndWait
    }
    "stop" {
        Stop-VmCleanly
    }
    "status" {
        $state = Get-VmState
        Write-Host "VM '$VmName' state: $state"
        Write-Host "--- Snapshots ---"
        & $VBox snapshot $VmName list
        if ($state -eq "running") {
            Write-Host "--- Guest Health ---"
            ssh -o BatchMode=yes -o ConnectTimeout=5 $SshHost "hostnamectl --static; uptime -p; echo system=`$(systemctl is-system-running || true); echo agy-telegram=`$(systemctl is-active agy-telegram.service || true); free -h | head -n 2; df -h / | tail -n 1"
        }
    }
    "deploy" {
        Start-VmAndWait
        & powershell -ExecutionPolicy Bypass -File (Join-Path $RepoRoot "scripts\deploy.ps1") -TargetHost $SshHost
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    "logs" {
        ssh -o BatchMode=yes $SshHost "journalctl -u agy-telegram.service -n $LogLines --no-pager"
    }
    "snapshot" {
        Stop-VmCleanly
        Write-Host "Taking cold snapshot '$Name'..."
        & $VBox snapshot $VmName take $Name --description "Cold snapshot taken $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
        if ($LASTEXITCODE -ne 0) { throw "Snapshot failed" }
        & $VBox snapshot $VmName list
        Start-VmAndWait
    }
    "restore" {
        Stop-VmCleanly
        Write-Host "Restoring snapshot '$Name'..."
        & $VBox snapshot $VmName restore $Name
        if ($LASTEXITCODE -ne 0) { throw "Snapshot restore failed" }
        Start-VmAndWait
    }
}
