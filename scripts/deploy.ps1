param(
    [string]$TargetHost = "agy-staging",
    [string]$RemoteDir = "~/agy-server"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$TmpTar = Join-Path $env:TEMP "agy-server-deploy-$([guid]::NewGuid().ToString('N')).tar.gz"

try {
    Write-Host "Packing $RepoRoot -> $TmpTar..."
    tar.exe -C $RepoRoot --exclude=".git" --exclude="vm" --exclude=".env" --exclude=".venv" --exclude="__pycache__" -czf $TmpTar .
    if ($LASTEXITCODE -ne 0) { throw "tar pack failed" }

    Write-Host "Uploading and extracting to ${TargetHost}:${RemoteDir}..."
    ssh $TargetHost "mkdir -p $RemoteDir"
    if ($LASTEXITCODE -ne 0) { throw "SSH mkdir failed" }

    scp -q $TmpTar "${TargetHost}:/tmp/agy-server-deploy.tar.gz"
    if ($LASTEXITCODE -ne 0) { throw "scp upload failed" }

    ssh $TargetHost "tar -xzf /tmp/agy-server-deploy.tar.gz -C $RemoteDir && rm -f /tmp/agy-server-deploy.tar.gz"
    if ($LASTEXITCODE -ne 0) { throw "Remote tar extract failed" }

    Write-Host "Installing and restarting agy-telegram.service..."
    ssh $TargetHost "sed -i 's/\r$//' $RemoteDir/systemd/agy-telegram.service $RemoteDir/server/bot.py && chmod +x $RemoteDir/server/bot.py && sudo cp $RemoteDir/systemd/agy-telegram.service /etc/systemd/system/agy-telegram.service && sudo systemctl daemon-reload && sudo systemctl enable --now agy-telegram.service && sudo systemctl restart agy-telegram.service && sudo systemctl is-active agy-telegram.service"
    if ($LASTEXITCODE -ne 0) { throw "Remote systemd deploy failed" }

    Write-Host "Deploy complete!"
} finally {
    if (Test-Path $TmpTar) {
        Remove-Item -Path $TmpTar -Force -ErrorAction SilentlyContinue
    }
}
