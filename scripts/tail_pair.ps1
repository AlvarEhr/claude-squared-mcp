# tail_pair.ps1 — open a console that tails a pair's live activity log
#
# Usage (from anywhere):
#   powershell -File <repo>/scripts/tail_pair.ps1 <pair_name>
#
# Or invoke via pair_tail's `Start-Process` snippet for inline launching.

param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$PairName,

    [int]$LastLines = 50
)

$logPath = Join-Path $env:USERPROFILE ".claude\pairs\logs\$PairName.log"

if (-not (Test-Path $logPath)) {
    Write-Host "Log file not found yet: $logPath" -ForegroundColor Yellow
    Write-Host "It will be created on the first pair_send to '$PairName'. Waiting..." -ForegroundColor Gray
    while (-not (Test-Path $logPath)) {
        Start-Sleep -Seconds 1
    }
    Write-Host "Log appeared — tailing now." -ForegroundColor Green
}

$Host.UI.RawUI.WindowTitle = "pair: $PairName (live)"
Write-Host "Tailing live activity for pair '$PairName'" -ForegroundColor Cyan
Write-Host "  Source: $logPath" -ForegroundColor Gray
Write-Host "  Ctrl+C to stop`n" -ForegroundColor Gray

Get-Content -Path $logPath -Wait -Tail $LastLines
