$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
Push-Location $root
try {
    # The GitHub MCP server, and the AgentDNA MCP Gateway in front of it.
    # Both read this project's .env.
    $server = Start-Process -FilePath $python -ArgumentList "mcp_server.py" -PassThru -NoNewWindow
    $gateway = Start-Process -FilePath $python -ArgumentList "-m", "agentdna.mcp.gateway" -PassThru -NoNewWindow

    # The gateway initialises its AgentDNA identity before it starts listening.
    & $python scripts/wait_for_gateway.py
    if ($LASTEXITCODE -ne 0) { throw "AgentDNA MCP Gateway did not start" }

    & $python automated.py
}
finally {
    foreach ($process in @($gateway, $server)) {
        if ($process -and -not $process.HasExited) { Stop-Process -Id $process.Id }
    }
    Pop-Location
}
