param(
    [string]$Version = "v1.0.0"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

python -m PyInstaller --noconfirm --clean --onedir --noconsole --name VideoOverlapMerger "$root\merge_gui.py"

$pkgName = "VideoOverlapMerger-$Version-win64"
$pkgDir = Join-Path $root "release_pkg\$pkgName"
if (Test-Path $pkgDir) { Remove-Item $pkgDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $pkgDir | Out-Null

Copy-Item "$root\dist\VideoOverlapMerger\*" $pkgDir -Recurse -Force

$ffmpeg = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
$ffprobe = (Get-Command ffprobe -ErrorAction SilentlyContinue).Source
if (-not $ffmpeg -or -not $ffprobe) {
    throw "ffmpeg.exe / ffprobe.exe not found in PATH."
}
Copy-Item $ffmpeg "$pkgDir\ffmpeg.exe" -Force
Copy-Item $ffprobe "$pkgDir\ffprobe.exe" -Force
Copy-Item "$root\README.md", "$root\LICENSE", "$root\THIRD_PARTY_LICENSES.txt" $pkgDir -Force

$zipPath = Join-Path $root "release_pkg\$pkgName.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -Path $pkgDir -DestinationPath $zipPath -CompressionLevel Optimal -Force

Write-Host "Built: $zipPath"
