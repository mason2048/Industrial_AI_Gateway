$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$gatewayPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $gatewayPython)) {
    throw 'Project Python environment is missing. Run start.bat to initialize it.'
}
& $gatewayPython -m scripts.manage stop
exit $LASTEXITCODE
