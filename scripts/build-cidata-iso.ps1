# Copyright (c) 2026 zyecheng
# SPDX-License-Identifier: BSD-3-Clause

param(
    [Parameter(Mandatory = $true)]
    [string]$SourceDir,
    [Parameter(Mandatory = $true)]
    [string]$OutputIso,
    [string]$VolumeLabel = "CIDATA"
)

$ErrorActionPreference = "Stop"

$srcPath = (Resolve-Path $SourceDir).Path
$outPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputIso)
$outDir = Split-Path -Parent $outPath
if (-not (Test-Path $outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}
if (Test-Path $outPath) {
    Remove-Item -Path $outPath -Force
}

# Add helper to copy COM IStream to a file
$compilerCode = @"
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

public static class IsoStreamWriter {
    public static void WriteIStreamToFile(object comStream, string outputPath) {
        IStream stream = (IStream)comStream;
        byte[] buffer = new byte[65536];
        IntPtr bytesReadPtr = Marshal.AllocHGlobal(sizeof(int));
        try {
            using (FileStream fs = new FileStream(outputPath, FileMode.Create, FileAccess.Write)) {
                while (true) {
                    stream.Read(buffer, buffer.Length, bytesReadPtr);
                    int bytesRead = Marshal.ReadInt32(bytesReadPtr);
                    if (bytesRead <= 0) break;
                    fs.Write(buffer, 0, bytesRead);
                }
            }
        } finally {
            Marshal.FreeHGlobal(bytesReadPtr);
        }
    }
}
"@

if (-not ("IsoStreamWriter" -as [type])) {
    Add-Type -TypeDefinition $compilerCode -Language CSharp
}

$fsi = New-Object -ComObject IMAPI2FS.MsftFileSystemImage
# 1 = ISO9660, 2 = Joliet -> 3 = ISO9660 + Joliet (preserves lowercase 'user-data' & 'meta-data')
$fsi.FileSystemsToCreate = 3
$fsi.VolumeName = $VolumeLabel
$fsi.FreeMediaBlocks = 0

$fsi.Root.AddTree($srcPath, $false)

$result = $fsi.CreateResultImage()
[IsoStreamWriter]::WriteIStreamToFile($result.ImageStream, $outPath)

Write-Host "Created ISO: $outPath ($((Get-Item $outPath).Length) bytes)"
