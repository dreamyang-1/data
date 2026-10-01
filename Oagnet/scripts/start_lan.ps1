param(
    [string]$BindAddress = "0.0.0.0",
    [ValidateRange(1, 65535)]
    [int]$Port = 8021,
    [string]$PythonPath = "C:\Users\lenovo\miniconda3\python.exe"
)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSScriptRoot

if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw "Python executable not found: $PythonPath"
}

$env:OAGNET_HOST = $BindAddress
$env:OAGNET_PORT = [string]$Port

Write-Host "Starting Oagnet on $BindAddress`:$Port"
Set-Location -LiteralPath $projectDir
& $PythonPath -m uvicorn api:app --host $BindAddress --port $Port
