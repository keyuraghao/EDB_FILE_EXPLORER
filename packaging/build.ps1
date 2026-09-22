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

# Portable build: the "portable.txt" marker keeps settings / keys / caches in .\data, Uninstall.cmd removes it all.
$portable = "dist\$name-portable"
if (Test-Path $portable) { Remove-Item -Recurse -Force $portable }
Copy-Item -Recurse "dist\$name" $portable
Set-Content -Path "$portable\portable.txt" -Value "EDB Explorer portable build $version - user data is kept in the data folder next to this file." -Encoding ascii
Copy-Item packaging\windows\Uninstall.cmd, packaging\windows\README-portable.txt "$portable\"
$zip = "dist\EDB-Explorer-$version-windows-$arch-portable.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path $portable -DestinationPath $zip
(Get-FileHash $zip -Algorithm SHA256).Hash.ToLower() + "  " + (Split-Path $zip -Leaf) | Out-File -Encoding ascii "$zip.sha256"
Write-Host ">> Done: $zip"

# Installer (Start Menu + Desktop shortcut, optional PATH and .edb association) when Inno Setup is available.
$iscc = Get-Command iscc -ErrorAction SilentlyContinue
if (-not $iscc) {
    foreach ($p in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
        if (Test-Path $p) { $iscc = $p; break }
    }
} else { $iscc = $iscc.Source }
if ($iscc) {
    Write-Host ">> Building installer with $iscc"
    & $iscc "/DVersion=$version" "/DSource=dist\$name" "packaging\installer.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
    $setup = "dist\EDB-Explorer-$version-setup.exe"
    (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower() + "  " + (Split-Path $setup -Leaf) | Out-File -Encoding ascii "$setup.sha256"
    Write-Host ">> Done: $setup"
} else {
    Write-Host ">> Inno Setup not found - skipping installer (zip only)"
}
