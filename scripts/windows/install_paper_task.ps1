param(
    [string]$TaskName = "AStockQuantPaperTrading",
    [string]$Profile = "paper",
    [string]$Account = "",
    [string]$Python = "",
    [string]$Database = ""
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Script = Join-Path $PSScriptRoot "run_paper.ps1"
$ArgumentList = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$Script`"",
    "-Profile", "`"$Profile`""
)
if ($Account) {
    $ArgumentList += @("-Account", "`"$Account`"")
}
if ($Python) {
    $ArgumentList += @("-Python", "`"$Python`"")
}
if ($Database) {
    $ArgumentList += @("-Database", "`"$Database`"")
}

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument ($ArgumentList -join " ") `
    -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -User $env:USERNAME `
    -RunLevel Highest `
    -Force

Start-ScheduledTask -TaskName $TaskName
Write-Host "已安装并启动计划任务：$TaskName"
