param(
    [string]$TargetHost = "agy-staging",
    [string]$RemoteDir = "~/agy-server"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$TmpTar = Join-Path $env:TEMP "agy-server-deploy-$([guid]::NewGuid().ToString('N')).tar.gz"

try {
    Write-Host "Packing $RepoRoot -> $TmpTar..."
    tar.exe -C $RepoRoot --exclude=".git" --exclude="vm" --exclude=".env" --exclude=".venv" --exclude="__pycache__" --exclude="data" -czf $TmpTar .
    if ($LASTEXITCODE -ne 0) { throw "tar pack failed" }

    Write-Host "Uploading and extracting to ${TargetHost}:${RemoteDir}..."
    ssh $TargetHost "mkdir -p $RemoteDir $RemoteDir/data ~/alpha-assistant/.agents ~/alpha-assistant/memory ~/alpha-assistant/inbox ~/.local/bin"
    if ($LASTEXITCODE -ne 0) { throw "SSH mkdir failed" }

    scp -q $TmpTar "${TargetHost}:/tmp/agy-server-deploy.tar.gz"
    if ($LASTEXITCODE -ne 0) { throw "scp upload failed" }

    ssh $TargetHost "tar -xzf /tmp/agy-server-deploy.tar.gz -C $RemoteDir && rm -f /tmp/agy-server-deploy.tar.gz"
    if ($LASTEXITCODE -ne 0) { throw "Remote tar extract failed" }

    Write-Host "Syncing Alpha workspace, hooks, alpha-ssh CLI, and systemd unit..."
    $remoteCmd = @'
set -e
find ~/agy-server/server ~/agy-server/systemd ~/agy-server/alpha-workspace ~/agy-server/tests -type f -exec sed -i 's/\r$//' {} +
chmod +x ~/agy-server/server/bot.py ~/agy-server/server/hook_notify.py ~/agy-server/server/alpha_ssh.py
cp ~/agy-server/alpha-workspace/GEMINI.md ~/alpha-assistant/GEMINI.md
cp ~/agy-server/alpha-workspace/.agents/hooks.json ~/alpha-assistant/.agents/hooks.json
ln -sf ~/agy-server/server/alpha_ssh.py ~/.local/bin/alpha-ssh
sudo cp ~/agy-server/systemd/agy-telegram.service /etc/systemd/system/agy-telegram.service
sudo systemctl daemon-reload
sudo systemctl enable --now agy-telegram.service
sudo systemctl restart agy-telegram.service
sudo systemctl is-active agy-telegram.service
'@
    $remoteCmd | ssh -T $TargetHost "tr -d '\r' | bash -s"
    if ($LASTEXITCODE -ne 0) { throw "Remote deploy failed" }

    Write-Host "Deploy complete!"
} finally {
    if (Test-Path $TmpTar) {
        Remove-Item -Path $TmpTar -Force -ErrorAction SilentlyContinue
    }
}
