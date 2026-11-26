# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

param(
    [string]$TargetHost = "agy-staging"
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$form = New-Object System.Windows.Forms.Form
$form.Text = "Alpha Bridge - Telegram Bot Token"
$form.Size = New-Object System.Drawing.Size(460, 175)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.TopMost = $true

$label = New-Object System.Windows.Forms.Label
$label.Location = New-Object System.Drawing.Point(16, 16)
$label.Size = New-Object System.Drawing.Size(415, 36)
$label.Text = "Paste your Telegram Bot Token for $TargetHost (input is masked and never logged):"
$form.Controls.Add($label)

$textBox = New-Object System.Windows.Forms.TextBox
$textBox.Location = New-Object System.Drawing.Point(16, 56)
$textBox.Size = New-Object System.Drawing.Size(412, 24)
$textBox.UseSystemPasswordChar = $true
$form.Controls.Add($textBox)

$okButton = New-Object System.Windows.Forms.Button
$okButton.Location = New-Object System.Drawing.Point(268, 96)
$okButton.Size = New-Object System.Drawing.Size(75, 28)
$okButton.Text = "Save"
$okButton.DialogResult = [System.Windows.Forms.DialogResult]::OK
$form.AcceptButton = $okButton
$form.Controls.Add($okButton)

$cancelButton = New-Object System.Windows.Forms.Button
$cancelButton.Location = New-Object System.Drawing.Point(353, 96)
$cancelButton.Size = New-Object System.Drawing.Size(75, 28)
$cancelButton.Text = "Cancel"
$cancelButton.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$form.CancelButton = $cancelButton
$form.Controls.Add($cancelButton)

$form.Add_Shown({ $form.Activate(); $textBox.Focus() })

$result = $form.ShowDialog()
if ($result -ne [System.Windows.Forms.DialogResult]::OK) {
    Write-Host "Canceled by user."
    exit 1
}

$tok = $textBox.Text.Trim()
$textBox.Text = ""
if ([string]::IsNullOrWhiteSpace($tok)) {
    Write-Host "Empty token entered."
    exit 1
}

# Basic format validation without printing the token
if ($tok -notmatch '^\d+:[A-Za-z0-9_-]+$') {
    Write-Host "Token does not match expected Telegram format (<digits>:<secret>)."
    exit 1
}

"TELEGRAM_BOT_TOKEN=$tok" | ssh -T -o BatchMode=yes $TargetHost python3 ~/agy-server/server/set_env.py
$rc = $LASTEXITCODE
$tok = $null

if ($rc -ne 0) {
    Write-Host "Failed to save token on $TargetHost (exit $rc)."
    exit $rc
}

ssh -o BatchMode=yes $TargetHost sudo systemctl restart agy-telegram.service
if ($LASTEXITCODE -ne 0) {
    [System.Windows.Forms.MessageBox]::Show("Token saved, but restarting agy-telegram.service failed.", "Alpha Bridge") | Out-Null
    exit $LASTEXITCODE
}

Start-Sleep -Seconds 2
$health = ssh -o BatchMode=yes $TargetHost curl -fsS http://127.0.0.1:8765/health
if ($LASTEXITCODE -ne 0) {
    [System.Windows.Forms.MessageBox]::Show("Bridge restarted, but /health check failed.", "Alpha Bridge") | Out-Null
    exit $LASTEXITCODE
}

Write-Host "Token saved securely to ${TargetHost}:~/agy-server/.env (0600) and agy-telegram.service restarted."
Write-Host "Bridge health: $health"
[System.Windows.Forms.MessageBox]::Show("Token saved & bridge restarted.`nHealth: $health", "Alpha Bridge") | Out-Null
