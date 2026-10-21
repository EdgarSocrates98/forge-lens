# Forge bootstrap - vendored entry point. See scripts/forge_bootstrap.py.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = $null
foreach ($c in @("python3", "python", "py")) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source; break }
}
if (-not $py) { Write-Error "setup: no python on PATH (need >=3.11)"; exit 2 }
if ($py.EndsWith("py.exe")) { & $py -3 scripts/forge_bootstrap.py @args } else { & $py scripts/forge_bootstrap.py @args }
exit $LASTEXITCODE
