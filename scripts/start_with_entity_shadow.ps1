$ErrorActionPreference = "Stop"
$agentRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$env:DATA_AGENT_ENTITY_EXTRACTOR_MODE = "shadow"
$env:DATA_AGENT_ENTITY_EXTRACTOR_URL = "http://127.0.0.1:8031/entities"
$env:DATA_AGENT_GLINER_THRESHOLD = "0.55"
$env:DATA_AGENT_GLINER_TIMEOUT_SECONDS = "1.2"
Set-Location -LiteralPath $agentRoot
& "C:\Users\lenovo\miniconda3\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8088
