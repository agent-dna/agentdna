$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
Push-Location $root
try {
    $server = Start-Process -FilePath $python -ArgumentList "mcp_server.py" -PassThru -NoNewWindow
    Start-Sleep -Seconds 1
    & $python automated.py
}
finally {
    if ($server -and -not $server.HasExited) { Stop-Process -Id $server.Id }
    Pop-Location
}