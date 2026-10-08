param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8021
)

$ErrorActionPreference = "Stop"
$ruleName = "YouoAgent Oagnet TCP $Port"
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)

if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Administrator privileges are required. Run PowerShell as Administrator."
}

$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if ($null -eq $existing) {
    New-NetFirewallRule `
        -DisplayName $ruleName `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort $Port `
        -Profile Domain,Private `
        -Description "Allow internal platform access to Oagnet API" | Out-Null
    Write-Host "Created firewall rule: $ruleName"
} else {
    Enable-NetFirewallRule -DisplayName $ruleName
    Write-Host "Firewall rule already exists and has been enabled: $ruleName"
}
