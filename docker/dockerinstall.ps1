# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311
#
# dockerinstall.ps1 — Windows one-shot Docker installer for Agent2.
#
# This is a thin entry point: it locates a Python interpreter and hands off to
# `run.py --docker`, which is the SINGLE source of truth for the whole Docker
# flow (pick OS -> ensure/auto-install Docker -> install the global `agent2`
# command -> first build/start). Running this script gives the EXACT same result
# as `python run.py --docker`.
#
# Usage (from anywhere):
#   powershell -ExecutionPolicy Bypass -File docker\dockerinstall.ps1
#   # or, once unblocked:
#   .\docker\dockerinstall.ps1

$ErrorActionPreference = "Stop"

# Project root = the parent of this script's folder (docker\..).
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root      = Split-Path -Parent $ScriptDir
$RunPy     = Join-Path $Root "run.py"

Write-Host ""
Write-Host "  == Agent2 Docker Installer (Windows) ==" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path $RunPy)) {
    Write-Host "  [ERR] run.py not found at $RunPy" -ForegroundColor Red
    Write-Host "        Run this script from inside the Agent2 project." -ForegroundColor DarkGray
    exit 1
}

# Find a Python launcher: prefer the `py` launcher, then python / python3.
$Py = $null
foreach ($cand in @("py", "python", "python3")) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) { $Py = $cmd.Source; break }
}

if (-not $Py) {
    Write-Host "  [ERR] Python 3.9+ was not found on PATH." -ForegroundColor Red
    Write-Host "        Install it from https://www.python.org/downloads/ and retry." -ForegroundColor DarkGray
    exit 1
}

Write-Host "  [OK] Python: $Py" -ForegroundColor Green
Write-Host "  [>>] Handing off to run.py --docker ..." -ForegroundColor Cyan
Write-Host ""

# Hand off. run.py --docker owns everything from here (incl. auto-installing
# Docker via winget/choco and installing the global `agent2` command).
& $Py $RunPy --docker
exit $LASTEXITCODE
