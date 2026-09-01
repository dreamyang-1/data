param(
    [string]$Python = "C:\Users\lenovo\miniconda3\python.exe",
    [ValidateSet("model", "hybrid")]
    [string]$Mode = "hybrid"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$dashscopeHost = "dashscope.aliyuncs.com"

Write-Host "[1/4] Checking Python and project directory"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python not found: $Python. Pass the actual python.exe with -Python."
}
Set-Location -LiteralPath $projectRoot

Write-Host "[2/4] Checking DashScope DNS and TCP 443"
$addresses = @(
    Resolve-DnsName $dashscopeHost -Type A |
        Where-Object { $_.IPAddress } |
        Select-Object -ExpandProperty IPAddress -Unique
)
if ($addresses.Count -eq 0) {
    throw "DNS cannot resolve $dashscopeHost. Ask Operations to check DNS."
}
Write-Host ("Resolved addresses: " + ($addresses -join ", "))

$reservedRedirect = $addresses | Where-Object {
    $parts = $_ -split "\."
    $parts.Count -eq 4 -and [int]$parts[0] -eq 198 -and [int]$parts[1] -in 18, 19
}
if ($reservedRedirect) {
    Write-Warning @"
DashScope resolved to reserved test range: $($reservedRedirect -join ', ').
This can be caused by a restricted sandbox, or by a working TUN/Fake-IP proxy.
The script will continue and use the TCP/Qwen probes as the source of truth.
"@
}

$tcp = Test-NetConnection $dashscopeHost -Port 443 -WarningAction SilentlyContinue
if (-not $tcp.TcpTestSucceeded) {
    throw "Cannot connect to $dashscopeHost`:443. Allow HTTPS egress or configure the corporate HTTPS_PROXY."
}

Write-Host "[3/4] Sending one real Qwen probe (API key is never printed)"
$probe = & $Python evals/run_intent_eval.py --mode model --dataset intent_probe.json | Out-String
$probeObject = $probe | ConvertFrom-Json
if (-not $probeObject.execution_valid) {
    $errors = $probeObject.error_counts | ConvertTo-Json -Compress
    throw "Qwen probe failed: $errors"
}
Write-Host ("Probe succeeded: model={0}, accuracy={1}, latency_p50={2}ms" -f `
    $probeObject.model, $probeObject.accuracy, $probeObject.latency_ms_p50)

Write-Host "[4/4] Running the 200-case $Mode intent evaluation"
$result = & $Python evals/run_intent_eval.py `
    --mode $Mode `
    --dataset intent_realistic_100.json intent_realistic_extra_100.json |
    Out-String
$resultObject = $result | ConvertFrom-Json
if (-not $resultObject.execution_valid) {
    $errors = $resultObject.error_counts | ConvertTo-Json -Compress
    throw "The 200-case evaluation produced no valid model result: $errors"
}

$resultsDirectory = Join-Path $projectRoot "evals\results"
New-Item -ItemType Directory -Path $resultsDirectory -Force | Out-Null
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$resultPath = Join-Path $resultsDirectory "qwen3.6-plus-$Mode-intent-200-$timestamp.json"
$result | Set-Content -LiteralPath $resultPath -Encoding UTF8

Write-Host "Evaluation completed"
Write-Host ("Accuracy: {0:P2}" -f [double]$resultObject.accuracy)
Write-Host ("Macro F1: {0:N4}" -f [double]$resultObject.macro_f1)
Write-Host ("Schema valid rate: {0:P2}" -f [double]$resultObject.schema_valid_rate)
Write-Host ("P50/P95: {0}ms / {1}ms" -f $resultObject.latency_ms_p50, $resultObject.latency_ms_p95)
Write-Host "Full result: $resultPath"
