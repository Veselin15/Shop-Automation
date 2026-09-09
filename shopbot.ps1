# Пуска shopbot от venv-а на проекта, без значение дали е активиран.
# Употреба:  .\shopbot.ps1 status
$ErrorActionPreference = "Stop"

$bin = Join-Path $PSScriptRoot ".venv\Scripts\shopbot.exe"

if (-not (Test-Path $bin)) {
    Write-Host "Няма $bin" -ForegroundColor Red
    Write-Host "Инсталирай проекта: .\.venv\Scripts\pip install -e ."
    exit 1
}

# Кирилицата в изхода иска UTF-8, иначе конзолата гърми.
if (-not $env:PYTHONIOENCODING) { $env:PYTHONIOENCODING = "utf-8" }

& $bin @args
exit $LASTEXITCODE
