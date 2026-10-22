[CmdletBinding()]
param(
    [switch]$VerifyOnly,
    [switch]$AddToUserPath,
    [switch]$SetUserEnvironment,
    [string]$ArchivePath
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$manifestPath = Join-Path $repositoryRoot "tools\ffmpeg-toolchain.json"
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json

if ($VerifyOnly -and ($AddToUserPath -or $SetUserEnvironment -or $ArchivePath)) {
    throw "-VerifyOnly cannot be combined with installation options"
}

if ($manifest.schema_version -ne "1.0" -or $manifest.platform -ne "windows-amd64") {
    throw "Unsupported FFmpeg toolchain manifest"
}

$relativeRoot = $manifest.install.windows_user_local_relative_root -replace "/", "\"
$installRoot = Join-Path $env:LOCALAPPDATA $relativeRoot
$binDir = Join-Path $installRoot $manifest.install.bin_relative_path

function Get-Sha256 {
    param([Parameter(Mandatory)][string]$Path)

    # Use the framework primitive directly: some Windows PowerShell 5.1
    # installations cannot autoload the Get-FileHash script module.
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $algorithm = [System.Security.Cryptography.SHA256]::Create()
        try {
            $digest = $algorithm.ComputeHash($stream)
        } finally {
            $algorithm.Dispose()
        }
        return [System.BitConverter]::ToString($digest).Replace("-", "").ToLowerInvariant()
    } finally {
        $stream.Dispose()
    }
}

function Assert-RegularFile {
    param([Parameter(Mandatory)][string]$Path)

    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    if (-not ($item -is [System.IO.FileInfo]) -or $item.LinkType) {
        throw "FFmpeg executable is not a regular non-link file: $Path"
    }

    $currentPath = [System.IO.Path]::GetFullPath($Path)
    while ($true) {
        $current = Get-Item -LiteralPath $currentPath -Force -ErrorAction Stop
        if (($current.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "FFmpeg executable path contains a reparse point: $Path"
        }
        $parentPath = Split-Path -Parent $currentPath
        if (-not $parentPath -or $parentPath -eq $currentPath) {
            break
        }
        $currentPath = $parentPath
    }
}

function Get-VersionLine {
    param([Parameter(Mandatory)][string]$Path)

    $output = @(& $Path -hide_banner -version 2>&1)
    if ($LASTEXITCODE -ne 0 -or $output.Count -eq 0) {
        throw "FFmpeg version probe failed: $Path"
    }
    return [string]$output[0]
}

function Assert-InstalledToolchain {
    foreach ($toolName in @("ffmpeg", "ffprobe")) {
        $tool = $manifest.executables.$toolName
        $path = Join-Path $binDir $tool.filename
        Assert-RegularFile -Path $path

        $actualHash = Get-Sha256 -Path $path
        if ($actualHash -ne $tool.sha256) {
            throw "Installed $toolName SHA-256 does not match the pinned manifest"
        }

        $versionLine = Get-VersionLine -Path $path
        if (-not $versionLine.StartsWith($tool.version_prefix, [System.StringComparison]::Ordinal)) {
            throw "Installed $toolName version does not match the pinned manifest"
        }

        Write-Output "$toolName`: $versionLine"
        Write-Output "$toolName SHA256: $actualHash"
    }
}

function Ensure-UserPath {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $entries = @()
    if ($userPath) {
        $entries = @($userPath -split ";" | Where-Object { $_ })
    }

    $normalizedBinDir = $binDir.TrimEnd("\")
    $remaining = @($entries | Where-Object { $_.TrimEnd("\") -ine $normalizedBinDir })
    $updatedPath = @($binDir, $remaining) -join ";"
    if ($userPath -ine $updatedPath) {
        [Environment]::SetEnvironmentVariable("Path", $updatedPath, "User")
        Write-Output "User PATH updated with pinned FFmpeg first: $binDir"
    } else {
        Write-Output "User PATH already starts with pinned FFmpeg: $binDir"
    }
}

function Ensure-UserToolchainEnvironment {
    $existing = [Environment]::GetEnvironmentVariable("VEP_FFMPEG_BIN_DIR", "User")
    if ($existing -ine $binDir) {
        [Environment]::SetEnvironmentVariable("VEP_FFMPEG_BIN_DIR", $binDir, "User")
        Write-Output "User VEP_FFMPEG_BIN_DIR set to pinned FFmpeg: $binDir"
    } else {
        Write-Output "User VEP_FFMPEG_BIN_DIR already points to pinned FFmpeg: $binDir"
    }
}

$temporaryRoot = $null
try {
    if (Test-Path -LiteralPath $installRoot) {
        Assert-InstalledToolchain
    } elseif ($VerifyOnly) {
        throw "Pinned FFmpeg installation is missing: $installRoot"
    } else {
        $temporaryRoot = Join-Path $env:TEMP ("codex\video-evidence-pipeline\ffmpeg-install-{0}" -f ([guid]::NewGuid().ToString("N")))
        if (Test-Path -LiteralPath $temporaryRoot) {
            throw "Temporary download root already exists: $temporaryRoot"
        }
        New-Item -ItemType Directory -Path $temporaryRoot -Force | Out-Null

        $archivePath = Join-Path $temporaryRoot $manifest.archive.name
        if ($ArchivePath) {
            $sourceArchive = (Resolve-Path -LiteralPath $ArchivePath -ErrorAction Stop).Path
            Assert-RegularFile -Path $sourceArchive
            Copy-Item -LiteralPath $sourceArchive -Destination $archivePath
        } else {
            Invoke-WebRequest -Uri $manifest.archive.url -OutFile $archivePath
        }
        $archiveHash = Get-Sha256 -Path $archivePath
        if ($archiveHash -ne $manifest.archive.sha256) {
            throw "Downloaded FFmpeg archive SHA-256 does not match the pinned manifest"
        }

        $extractRoot = Join-Path $temporaryRoot "extract"
        Expand-Archive -LiteralPath $archivePath -DestinationPath $extractRoot
        $sourceRoot = Join-Path $extractRoot $manifest.archive.root_directory
        $sourceBinDir = Join-Path $sourceRoot $manifest.install.bin_relative_path
        foreach ($toolName in @("ffmpeg", "ffprobe")) {
            $tool = $manifest.executables.$toolName
            $sourcePath = Join-Path $sourceBinDir $tool.filename
            Assert-RegularFile -Path $sourcePath
            $actualHash = Get-Sha256 -Path $sourcePath
            if ($actualHash -ne $tool.sha256) {
                throw "Extracted $toolName SHA-256 does not match the pinned manifest"
            }
        }

        $installParent = Split-Path $installRoot -Parent
        New-Item -ItemType Directory -Path $installParent -Force | Out-Null
        Move-Item -LiteralPath $sourceRoot -Destination $installRoot
        Assert-InstalledToolchain
    }

    if ($VerifyOnly) {
        Write-Output "Pinned FFmpeg toolchain verification passed."
    } else {
        if ($SetUserEnvironment -or $AddToUserPath) {
            Ensure-UserToolchainEnvironment
        }
        if ($AddToUserPath) {
            Ensure-UserPath
            Write-Output "Pinned FFmpeg toolchain installation passed. Restart the shell before using PATH-based commands."
        } else {
            $escapedBinDir = $binDir.Replace("'", "''")
            Write-Output "Pinned FFmpeg toolchain installation passed. For this PowerShell session run:"
            Write-Output "`$env:VEP_FFMPEG_BIN_DIR = '$escapedBinDir'"
            if (-not $SetUserEnvironment) {
                Write-Output "User environment unchanged. -SetUserEnvironment opts into persistent VEP_FFMPEG_BIN_DIR."
            }
        }
    }
} finally {
    if ($temporaryRoot -and (Test-Path -LiteralPath $temporaryRoot)) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}
