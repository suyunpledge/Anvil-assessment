param([int]$Port = 8099, [switch]$Doctor, [switch]$CodeOnly)
$ErrorActionPreference = 'Stop'
$anvilRoot = $PSScriptRoot
$anvilPython = Join-Path $anvilRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $anvilPython)) {
    throw '请先按 README 创建 .venv 并安装 requirements.txt。'
}
Push-Location -LiteralPath $anvilRoot
try {
    if ($CodeOnly) { $env:ANVIL_HR = '0' }
    if ($Doctor) {
        $anvilScope = if ($CodeOnly) { 'code' } else { 'full' }
        & $anvilPython anvil.py --doctor --scope $anvilScope
    }
    else { & $anvilPython anvil.py --port $Port }
    exit $LASTEXITCODE
} finally { Pop-Location }
