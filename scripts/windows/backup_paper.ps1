param(
    [string]$Python = "",
    [string]$Database = "data\paper_trading.db",
    [string]$BackupDir = "backups",
    [int]$Retain = 30
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

& $Python -m ops.backup create `
    --database $Database `
    --backup-dir $BackupDir `
    --label scheduled `
    --retain $Retain
exit $LASTEXITCODE
