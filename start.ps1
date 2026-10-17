param([int]$Port = 8099, [switch]$Doctor)
$ErrorActionPreference = 'Stop'
$anvilRoot = $PSScriptRoot
$anvilPython = Join-Path $anvilRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $anvilPython)) {
    throw '请先按 README 创建 .venv 并安装 requirements.txt。'
}
Push-Location -LiteralPath $anvilRoot
try {
    if ($Doctor) { & $anvilPython anvil.py --doctor }
    else { & $anvilPython anvil.py --port $Port }
    exit $LASTEXITCODE
} finally { Pop-Location }
