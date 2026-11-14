# Builds dist\CoDriverStudio\CoDriverStudio.exe and dist\CoDriverStudio-win64.zip
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = ".\.venv\Scripts\python.exe"
& $py -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "Tests failed - not building." }
& $py -m PyInstaller --noconfirm --clean CoDriverStudio.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }
$zip = "dist\CoDriverStudio-win64.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path "dist\CoDriverStudio" -DestinationPath $zip
Write-Host "Built $zip"
