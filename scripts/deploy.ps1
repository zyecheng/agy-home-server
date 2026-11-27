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
    tar.exe -C $RepoRoot --exclude=".git" --exclude="vm" --exclude=".env" --exclude=".venv" --exclude="__pycache__" --exclude="data" --exclude="watcher-ipc" -czf $TmpTar .
    if ($LASTEXITCODE -ne 0) { throw "tar pack failed" }

    Write-Host "Uploading and extracting to ${TargetHost}:${RemoteDir}..."
    ssh $TargetHost "mkdir -p $RemoteDir $RemoteDir/data $RemoteDir/data/backups $RemoteDir/watcher-ipc ~/alpha-assistant/.agents/skills/watch-missions ~/alpha-assistant/memory ~/alpha-assistant/inbox ~/alpha-assistant/watchers/missions ~/alpha-assistant/watchers/state ~/alpha-assistant/watchers/triggers ~/agy-scout-sandbox/.agents ~/.local/bin && chmod 700 $RemoteDir/data $RemoteDir/data/backups $RemoteDir/watcher-ipc ~/alpha-assistant/watchers/state ~/alpha-assistant/watchers/triggers"
    if ($LASTEXITCODE -ne 0) { throw "SSH mkdir failed" }

    scp -q $TmpTar "${TargetHost}:${RemoteArchive}"
    if ($LASTEXITCODE -ne 0) { throw "scp upload failed" }

    Write-Host "Syncing Alpha workspace, scout sandbox, hooks, skills, and systemd units..."
    $remoteCmd = @"
set -euo pipefail
REMOTE_DIR=$RemoteDir
ARCHIVE=$RemoteArchive
trap 'rm -f "`$ARCHIVE"' EXIT
tar -xzf "`$ARCHIVE" -C "`$REMOTE_DIR"
rm -f "`$ARCHIVE"
find "`$REMOTE_DIR/server" "`$REMOTE_DIR/systemd" "`$REMOTE_DIR/alpha-workspace" "`$REMOTE_DIR/scout-sandbox" "`$REMOTE_DIR/tests" -type f -exec sed -i 's/\r`$//' {} +
chmod +x "`$REMOTE_DIR/server/bot.py" "`$REMOTE_DIR/server/hook_notify.py" "`$REMOTE_DIR/server/hook_approve.py" "`$REMOTE_DIR/server/hook_scout_guard.py" "`$REMOTE_DIR/server/alpha_ssh.py" "`$REMOTE_DIR/server/set_env.py" "`$REMOTE_DIR/tests/fake_agy.py"
cp "`$REMOTE_DIR/alpha-workspace/GEMINI.md" ~/alpha-assistant/GEMINI.md
cp "`$REMOTE_DIR/alpha-workspace/.agents/hooks.json" ~/alpha-assistant/.agents/hooks.json
cp "`$REMOTE_DIR/alpha-workspace/.agents/skills/watch-missions/SKILL.md" ~/alpha-assistant/.agents/skills/watch-missions/SKILL.md
cp "`$REMOTE_DIR/scout-sandbox/.agents/hooks.json" ~/agy-scout-sandbox/.agents/hooks.json
cp "`$REMOTE_DIR/scout-sandbox/.agents/settings.json" ~/agy-scout-sandbox/.agents/settings.json
ln -sf "`$REMOTE_DIR/server/alpha_ssh.py" ~/.local/bin/alpha-ssh
sudo cp "`$REMOTE_DIR/systemd/agy-telegram.service" /etc/systemd/system/agy-telegram.service
sudo cp "`$REMOTE_DIR/systemd/agy-watcher.service" /etc/systemd/system/agy-watcher.service
sudo cp "`$REMOTE_DIR/systemd/agy-watcher.timer" /etc/systemd/system/agy-watcher.timer
sudo cp "`$REMOTE_DIR/systemd/agy-watcher-run.path" /etc/systemd/system/agy-watcher-run.path
sudo cp "`$REMOTE_DIR/systemd/agy-health.service" /etc/systemd/system/agy-health.service
sudo cp "`$REMOTE_DIR/systemd/agy-health.timer" /etc/systemd/system/agy-health.timer
sudo systemctl daemon-reload
sudo systemctl enable agy-telegram.service
sudo systemctl restart agy-telegram.service
sudo systemctl is-active agy-telegram.service
sudo systemctl enable --now agy-watcher.timer agy-watcher-run.path agy-health.timer
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
