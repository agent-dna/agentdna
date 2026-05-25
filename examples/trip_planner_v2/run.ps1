#Requires -Version 5.1
$ErrorActionPreference = 'Stop'

$ScriptDir = $PSScriptRoot

if (-not (Test-Path -Path (Join-Path $ScriptDir '.env'))) {
    Write-Error 'ERROR: .env not found. Run: cp .env.sample .env and fill in AGENTDNA_API_KEY.'
    exit 1
}

Write-Host 'Setting up environment...'
Push-Location $ScriptDir
try {
    & uv sync --quiet
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed (exit $LASTEXITCODE)" }

    Write-Host 'Starting Trip Planner UI...'
    & uv run --no-sync streamlit run app.py
    if ($LASTEXITCODE -ne 0) { throw "streamlit exited with $LASTEXITCODE" }
}
finally {
    Pop-Location
}
