param(
    [string]$Profile = "paper",
    [string]$Account = "",
    [string]$Python = "",
    [string]$Database = ""
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root

if (-not $Python) {
    $VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $VenvPython) {
        $Python = $VenvPython
    } else {
        $Python = "python"
    }
}

$RunnerArgs = @("--broker", "qmt")
if ($Database) {
    $RunnerArgs += @("--database", $Database)
}
if ($Account) {
    $RunnerArgs += @("--account", $Account)
}

$ServiceArgs = @(
    "-m", "ops.service",
    "--profile", $Profile
)
if ($Account) {
    $ServiceArgs += @("--account", $Account)
}
$ServiceArgs += @("--")
$ServiceArgs += $RunnerArgs

$SupervisorArgs = @(
    "-m", "ops.supervisor",
    "--stop-file", "logs\paper.stop",
    "--state-file", "logs\supervisor.json",
    "--max-restarts", "50",
    "--restart-window", "3600",
    "--",
    $Python
)
$SupervisorArgs += $ServiceArgs

& $Python @SupervisorArgs
exit $LASTEXITCODE
