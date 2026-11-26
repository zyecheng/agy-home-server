# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

param(
    [string]$TargetHost = "agy-staging",
    [string]$RemoteDir = "~/agy-server"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DeployId = [guid]::NewGuid().ToString("N")
$TmpTar = Join-Path $env:TEMP "agy-server-deploy-$DeployId.tar.gz"
$RemoteArchive = "$RemoteDir/.deploy-$DeployId.tar.gz"

try {
    Write-Host "Packing $RepoRoot -> $TmpTar..."
    tar.exe -C $RepoRoot --exclude=".git" --exclude="vm" --exclude=".env" --exclude=".venv" --exclude="__pycache__" --exclude="data" -czf $TmpTar .
    if ($LASTEXITCODE -ne 0) { throw "tar pack failed" }

    Write-Host "Uploading and extracting to ${TargetHost}:${RemoteDir}..."
    ssh $TargetHost "mkdir -p $RemoteDir $RemoteDir/data ~/alpha-assistant/.agents ~/alpha-assistant/memory ~/alpha-assistant/inbox ~/.local/bin && chmod 700 $RemoteDir/data"
    if ($LASTEXITCODE -ne 0) { throw "SSH mkdir failed" }

    scp -q $TmpTar "${TargetHost}:${RemoteArchive}"
    if ($LASTEXITCODE -ne 0) { throw "scp upload failed" }

    Write-Host "Syncing Alpha workspace, hooks, alpha-ssh CLI, and systemd unit..."
    $remoteCmd = @"
set -euo pipefail
REMOTE_DIR=$RemoteDir
ARCHIVE=$RemoteArchive
trap 'rm -f "`$ARCHIVE"' EXIT
tar -xzf "`$ARCHIVE" -C "`$REMOTE_DIR"
rm -f "`$ARCHIVE"
find "`$REMOTE_DIR/server" "`$REMOTE_DIR/systemd" "`$REMOTE_DIR/alpha-workspace" "`$REMOTE_DIR/tests" -type f -exec sed -i 's/\r`$//' {} +
chmod +x "`$REMOTE_DIR/server/bot.py" "`$REMOTE_DIR/server/hook_notify.py" "`$REMOTE_DIR/server/alpha_ssh.py" "`$REMOTE_DIR/server/set_env.py"
cp "`$REMOTE_DIR/alpha-workspace/GEMINI.md" ~/alpha-assistant/GEMINI.md
cp "`$REMOTE_DIR/alpha-workspace/.agents/hooks.json" ~/alpha-assistant/.agents/hooks.json
ln -sf "`$REMOTE_DIR/server/alpha_ssh.py" ~/.local/bin/alpha-ssh
sudo cp "`$REMOTE_DIR/systemd/agy-telegram.service" /etc/systemd/system/agy-telegram.service
sudo systemctl daemon-reload
sudo systemctl enable agy-telegram.service
sudo systemctl restart agy-telegram.service
sudo systemctl is-active agy-telegram.service
for i in 1 2 3 4 5; do
    if curl -fsS http://127.0.0.1:8765/health; then
        echo ""
        exit 0
    fi
    sleep 1
done
echo "Bridge health check failed" >&2
exit 1
"@
    $remoteCmd | ssh -T $TargetHost "tr -d '\r' | bash -s"
    if ($LASTEXITCODE -ne 0) { throw "Remote deploy failed" }

    Write-Host "Deploy complete!"
} finally {
    if (Test-Path $TmpTar) {
        Remove-Item -Path $TmpTar -Force -ErrorAction SilentlyContinue
    }
}
