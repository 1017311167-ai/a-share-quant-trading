param(
    [string]$TaskName = "AStockQuantPaperTrading",
    [string]$StopFile = "logs\paper.stop"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$StopPath = Join-Path $Root $StopFile
New-Item -ItemType File -Force -Path $StopPath | Out-Null

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName
}
Write-Host "停止信号已写入：$StopPath"
