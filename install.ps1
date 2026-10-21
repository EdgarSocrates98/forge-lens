# Forge account install — wheel runtime + user-scoped environment.
# Persists HKCU variables and can refresh current session when dot-sourced.
# The default installs a built wheel (no checkout dependence). Pass --editable
# for the development flow (CLI tied to this checkout).
$ErrorActionPreference = "Stop"
$isDotSourced = $MyInvocation.InvocationName -eq "."
$oldLocation = Get-Location
try {
    Set-Location -LiteralPath $PSScriptRoot
    $py = $null
    $pyArgs = @()
    foreach ($c in @("python3", "python", "py")) {
        $cmd = Get-Command $c -CommandType Application -ErrorAction SilentlyContinue
        if ($cmd) {
            $py = $cmd.Source
            if ([IO.Path]::GetFileName($py) -in @("py.exe", "py")) { $pyArgs = @("-3") }
            break
        }
    }
    if (-not $py) { throw "install: no Python executable on PATH (need >=3.11)" }
    $installArgs = @("--persist-env", "--allow-download") + $args
    & $py @pyArgs scripts/forge_bootstrap.py @installArgs
    $status = $LASTEXITCODE
    if ($status -eq 0) {
        if ($args -contains "--dry-run") {
            Write-Host "Install plan complete. No files, variables or registry entries were written."
            return
        }
        # Bootstrap writes HKCU. Copy canonical variables into this process too;
        # dot-sourcing this script therefore refreshes the caller's session.
        $config = Get-Content -LiteralPath (Join-Path $PWD "forge.json") -Raw | ConvertFrom-Json
        $prefix = ([string]$config.forge_id -replace "[^A-Za-z0-9]", "_").ToUpperInvariant()
        $userEnv = [Environment]::GetEnvironmentVariables("User")
        foreach ($suffix in @("HOME", "INSTALL_ROOT", "VENV", "PYTHON", "CLI", "LAUNCHER")) {
            $name = "${prefix}_${suffix}"
            if ($userEnv.Contains($name)) { Set-Item -Path ("Env:" + $name) -Value ([string]$userEnv[$name]) }
        }
        $envFile = Join-Path $(if ($env:XDG_CONFIG_HOME) { $env:XDG_CONFIG_HOME } else { Join-Path $HOME ".config" }) "forjas\env.sh"
        Write-Host "Install complete. User variables persisted in HKCU:\Environment."
        Write-Host "Open a new terminal. Git Bash environment file: $envFile"
        Write-Host "Dot-source to refresh current PowerShell session: .\install.ps1"
    }
} catch {
    Write-Error $_
    $status = 2
} finally {
    Set-Location $oldLocation
}
if ($isDotSourced) { return $status }
exit $status
