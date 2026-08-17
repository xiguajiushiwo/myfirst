$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$logDirectory = Join-Path $root "logs"
$logPath = Join-Path $logDirectory "capture_agent.log"

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
Set-Location $root
$env:PYTHONUNBUFFERED = "1"

& (Join-Path $root ".venv\Scripts\python.exe") `
    -m client.capture_agent `
    --config client\client_config.json `
    --host 127.0.0.1 `
    --port 8812 *>> $logPath
