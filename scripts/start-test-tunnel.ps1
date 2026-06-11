# start-test-tunnel.ps1 — bring up the Invoice Processor test instance + dev tunnel.
#
# Usage (from repo root, in a normal PowerShell window):
#   .\scripts\start-test-tunnel.ps1
#
# What it does:
#   1. Starts the packaged app (dist\InvoiceProcessor\InvoiceProcessor.exe) on 127.0.0.1:8743,
#      without opening a local browser tab.
#   2. Waits until the app answers.
#   3. Hosts the persistent dev tunnel "lyfegen-invoice-test" in the foreground and prints
#      the public URL. Ctrl-C stops the tunnel, then the app is shut down too.
#
# Prerequisites (one-time, already done during setup):
#   winget install Microsoft.devtunnel
#   devtunnel user login                                  (Lyfegen M365 account)
#   devtunnel create lyfegen-invoice-test
#   devtunnel port create lyfegen-invoice-test -p 8743 --protocol http
#   devtunnel access create lyfegen-invoice-test --tenant
#
# Notes:
#   - The tunnel only exists while this window is open AND the machine is awake.
#     Laptop sleep / lid close = testers lose access until you rerun this script.
#   - The app runs in desktop mode: the settings popup is EDITABLE BY EVERY TESTER
#     (writes to %APPDATA%\InvoiceProcessor\settings.env). Acceptable for the test
#     phase; the user guide tells testers not to touch settings unasked.
#   - Tenant access grants expire after at most 30 days. If testers suddenly get
#     "access denied" after login, rerun:
#       devtunnel access create lyfegen-invoice-test --tenant

$ErrorActionPreference = "Stop"

$TunnelId = "lyfegen-invoice-test"
$Port     = 8743
$RepoRoot = Split-Path -Parent $PSScriptRoot
$AppExe   = Join-Path $RepoRoot "dist\InvoiceProcessor\InvoiceProcessor.exe"

# Resolve devtunnel: PATH first, winget package location as fallback (fresh installs
# only get the PATH entry in NEW shells).
$devtunnel = $null
$cmd = Get-Command devtunnel -ErrorAction SilentlyContinue
if ($cmd) { $devtunnel = $cmd.Source }
if (-not $devtunnel) {
    $fallback = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages\Microsoft.devtunnel_Microsoft.Winget.Source_8wekyb3d8bbwe\devtunnel.exe"
    if (Test-Path $fallback) { $devtunnel = $fallback }
}
if (-not $devtunnel) {
    Write-Error "devtunnel CLI not found. Install it with: winget install Microsoft.devtunnel"
}

if (-not (Test-Path $AppExe)) {
    Write-Error "App not built: $AppExe missing. Build with: pyinstaller desktop/InvoiceProcessor.spec --noconfirm"
}

# Start the app without a local browser tab (the tunnel URL is what testers use).
$env:INVOICE_NO_BROWSER = "1"
$appProc = $null
$alreadyUp = $false
try {
    Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/settings/status" -UseBasicParsing -TimeoutSec 2 | Out-Null
    $alreadyUp = $true
    Write-Host "App already running on port $Port - reusing it."
} catch {
    Write-Host "Starting Invoice Processor..."
    $appProc = Start-Process -FilePath $AppExe -PassThru -WindowStyle Minimized
}

# Wait for the app to answer (max 30 s).
if (-not $alreadyUp) {
    $deadline = (Get-Date).AddSeconds(30)
    $up = $false
    while ((Get-Date) -lt $deadline) {
        try {
            Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/settings/status" -UseBasicParsing -TimeoutSec 2 | Out-Null
            $up = $true; break
        } catch { Start-Sleep -Milliseconds 400 }
    }
    if (-not $up) {
        if ($appProc) { Stop-Process -Id $appProc.Id -Force -ErrorAction SilentlyContinue }
        Write-Error "App did not come up on port $Port within 30 s."
    }
}

Write-Host ""
Write-Host "========================================================"
Write-Host "  Invoice Processor - TEST TUNNEL"
Write-Host "  Local:  http://127.0.0.1:$Port"
Write-Host "  Tunnel: https://x3m2th39-8743.euw.devtunnels.ms"
Write-Host "          (also printed below as 'Connect via browser')"
Write-Host "  Access: Lyfegen Microsoft 365 sign-in required"
Write-Host ""
Write-Host "  Keep this window open. Machine must stay AWAKE."
Write-Host "  Ctrl-C stops the tunnel and the app."
Write-Host "========================================================"
Write-Host ""

# Foreground: host the tunnel until Ctrl-C, then clean up the app process.
try {
    & $devtunnel host $TunnelId
} finally {
    if ($appProc -and -not $appProc.HasExited) {
        Write-Host "Stopping Invoice Processor (pid $($appProc.Id))..."
        Stop-Process -Id $appProc.Id -Force -ErrorAction SilentlyContinue
    }
}
