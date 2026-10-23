param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet("mcp-run-python", "run-python", "playwright-mcp")]
    [string]$Tool,

    [Parameter(Position = 1)]
    [ValidateSet("stdio", "http")]
    [string]$Mode = "stdio",

    [Parameter(Position = 2)]
    [ValidateRange(1, 65535)]
    [int]$Port = 8109
)

$ErrorActionPreference = "Stop"
$ToolRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($Tool -eq "playwright-mcp") {
    if ($Mode -ne "stdio") {
        throw "playwright-mcp currently supports stdio mode only in this launcher."
    }

    $NodeRuntime = Get-ChildItem (Join-Path $ToolRoot "runtime\node") -Directory -ErrorAction SilentlyContinue |
        Where-Object Name -Like "node-*-win-x64" |
        Sort-Object Name -Descending |
        Select-Object -First 1
    if (-not $NodeRuntime) {
        throw "Bundled Node.js was not found under runtime\node."
    }

    $ServerCommand = Join-Path $ToolRoot "npm\node_modules\.bin\playwright-mcp-server.cmd"
    if (-not (Test-Path -LiteralPath $ServerCommand)) {
        throw "playwright-mcp is not installed. Run npm install in the mcp-tools\npm directory."
    }

    $env:PATH = "$($NodeRuntime.FullName);$env:PATH"
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $ToolRoot "runtime\playwright"
    & $ServerCommand
    exit $LASTEXITCODE
}

$BundledPython = Join-Path $ToolRoot "runtime\python\python.exe"
$BundledDeno = Join-Path $ToolRoot "runtime\deno\deno.exe"

if (Test-Path -LiteralPath $BundledPython) {
    $PythonPath = $BundledPython
} else {
    $Python = Get-Command python -ErrorAction SilentlyContinue
    $PythonPath = if ($Python) { $Python.Source } else { $null }
}

if (Test-Path -LiteralPath $BundledDeno) {
    $DenoPath = $BundledDeno
    $env:PATH = "$(Split-Path -Parent $BundledDeno);$env:PATH"
} else {
    $Deno = Get-Command deno -ErrorAction SilentlyContinue
    $DenoPath = if ($Deno) { $Deno.Source } else { $null }
}

if (-not $PythonPath -or $PythonPath -like "*WindowsApps*") {
    throw "Python 3.10+ is not installed or is only a Microsoft Store alias. Install Python and add it to PATH."
}
if (-not $DenoPath) {
    throw "Deno is not installed. Install Deno and add it to PATH."
}

$PipPath = Join-Path $ToolRoot "pip"
if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$PipPath;$env:PYTHONPATH"
} else {
    $env:PYTHONPATH = $PipPath
}

$Arguments = @("-m", "mcp_run_python")
if ($Mode -eq "http") {
    $Arguments += @("streamable-http", "--port", $Port)
} else {
    $Arguments += "stdio"
}
$Arguments += "--disable-networking"

& $PythonPath @Arguments
exit $LASTEXITCODE
