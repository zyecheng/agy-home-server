# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

param(
    [string]$VmName = "agy-staging",
    [string]$OvaPath = "C:\Users\zxzha\.gemini\antigravity\scratch\agy-home-server\vm\noble-server-cloudimg-amd64.ova",
    [string]$SeedIsoPath = "C:\Users\zxzha\.gemini\antigravity\scratch\agy-home-server\vm\cidata.iso",
    # Use 1 vCPU by default when Windows Hyper-V/WHPX NEM fallback is active (avoids Linux 6.8 SMP spinlock)
    [int]$Cpus = 1,
    [int]$MemoryMb = 3072,
    [int]$HostSshPort = 2222
)

$ErrorActionPreference = "Stop"
$vbox = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
if (-not (Test-Path $vbox)) {
    throw "VBoxManage.exe not found at $vbox"
}

$existing = & $vbox list vms
if ($existing -notmatch "`"$VmName`"") {
    Write-Host "Importing $OvaPath as $VmName..."
    & $vbox import $OvaPath --vsys 0 --vmname $VmName
    if ($LASTEXITCODE -ne 0) { throw "VBoxManage import failed" }
} else {
    Write-Host "VM $VmName already exists; skipping import."
}

# Stop VM if currently running before modifying hardware
$running = & $vbox list runningvms
if ($running -match "`"$VmName`"") {
    Write-Host "Powering off running VM $VmName before reconfiguration..."
    & $vbox controlvm $VmName poweroff
    Start-Sleep -Seconds 2
}

Write-Host "Configuring $VmName ($Cpus vCPUs, ${MemoryMb}MB RAM, NAT 127.0.0.1:${HostSshPort}->22)..."
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $vbox modifyvm $VmName --natpf1 delete ssh 2>$null | Out-Null
$ErrorActionPreference = $prevEap

& $vbox modifyvm $VmName `
    --cpus $Cpus `
    --memory $MemoryMb `
    --vram 16 `
    --nested-hw-virt off `
    --paravirtprovider default `
    --audio-enabled off `
    --nic1 nat `
    --natpf1 "ssh,tcp,127.0.0.1,$HostSshPort,,22"
if ($LASTEXITCODE -ne 0) { throw "VBoxManage modifyvm failed" }

# Inspect storage controllers and attach cloud-init seed ISO
$info = & $vbox showvminfo $VmName --machinereadable
$ideCtl = ($info | Select-String '^storagecontrollername\d+="([^"]*IDE[^"]*)"').Matches | Select-Object -First 1
$sataCtl = ($info | Select-String '^storagecontrollername\d+="([^"]*SATA[^"]*)"').Matches | Select-Object -First 1

if ($ideCtl) {
    $ctlName = $ideCtl.Groups[1].Value
    Write-Host "Attaching $SeedIsoPath to IDE controller '$ctlName' (port 1, device 0)..."
    & $vbox storageattach $VmName --storagectl $ctlName --port 1 --device 0 --type dvddrive --medium $SeedIsoPath
} elseif ($sataCtl) {
    $ctlName = $sataCtl.Groups[1].Value
    Write-Host "Attaching $SeedIsoPath to SATA controller '$ctlName' (port 1, device 0)..."
    & $vbox storageattach $VmName --storagectl $ctlName --port 1 --device 0 --type dvddrive --medium $SeedIsoPath
} else {
    Write-Host "Adding IDE controller and attaching $SeedIsoPath..."
    & $vbox storagectl $VmName --name "IDE" --add ide
    & $vbox storageattach $VmName --storagectl "IDE" --port 0 --device 0 --type dvddrive --medium $SeedIsoPath
}
if ($LASTEXITCODE -ne 0) { throw "VBoxManage storageattach failed" }

# Disable serial console file if Ubuntu OVA sets a broken host-pipe/file path
$uart1 = $info | Select-String '^uartmode1='
if ($uart1 -and $uart1.Line -notmatch 'disconnected') {
    Write-Host "Disconnecting UART1 from OVA defaults to prevent headless boot hang..."
    & $vbox modifyvm $VmName --uartmode1 disconnected
}

Write-Host "Starting $VmName headless..."
& $vbox startvm $VmName --type headless
if ($LASTEXITCODE -ne 0) { throw "VBoxManage startvm failed" }

Write-Host "VM $VmName started headless!"
