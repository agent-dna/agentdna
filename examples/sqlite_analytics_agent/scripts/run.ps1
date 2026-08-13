$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
Push-Location (Split-Path -Parent $root)
try { & $python -m sqlite_analytics_agent.automated } finally { Pop-Location }