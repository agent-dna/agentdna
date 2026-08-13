$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
Push-Location (Split-Path -Parent $root)
try {
    $server = Start-Process -FilePath $python -ArgumentList "-m", "github_repository_agent.mcp_server" -PassThru -NoNewWindow
    Start-Sleep -Seconds 1
    & $python -m github_repository_agent.automated
}
finally {
    if ($server -and -not $server.HasExited) { Stop-Process -Id $server.Id }
    Pop-Location
}