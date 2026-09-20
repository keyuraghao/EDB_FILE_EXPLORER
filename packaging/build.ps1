# Build the Windows distributable: dist\edb-explorer-<version>-windows-x64.zip
# Usage (from an activated venv or with python on PATH):  powershell -ExecutionPolicy Bypass -File packaging\build.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$py = "python"
if (Test-Path ".venv\Scripts\python.exe") { $py = ".venv\Scripts\python.exe" }

$version = & $py -c "import sys; sys.path.insert(0,'src'); import edb_explorer; print(edb_explorer.__version__)"
$name = "edb-explorer-$version"
$arch = if ([Environment]::Is64BitOperatingSystem) { "x64" } else { "x86" }

Write-Host ">> Building $name for windows/$arch"
& $py -m PyInstaller packaging\edb-explorer.spec --noconfirm --clean --distpath dist --workpath build\pyinstaller
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Write-Host ">> Smoke test"
& "dist\$name\edb-explorer.exe" --version
if ($LASTEXITCODE -ne 0) { throw "Smoke test failed" }

Write-Host ">> Packaging"
Copy-Item README.md, LICENSE, CHANGELOG.md "dist\$name\"
$zip = "dist\$name-windows-$arch.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "dist\$name" -DestinationPath $zip
(Get-FileHash $zip -Algorithm SHA256).Hash.ToLower() + "  " + (Split-Path $zip -Leaf) | Out-File -Encoding ascii "$zip.sha256"
Write-Host ">> Done: $zip"
