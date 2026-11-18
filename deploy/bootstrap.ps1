param(
    [ValidateSet('code', 'test', 'full')][string]$Profile = 'code',
    [string]$Wheelhouse = ''
)
$ErrorActionPreference = 'Stop'
$anvilRoot = Split-Path -Parent $PSScriptRoot
$anvilPython = Join-Path $anvilRoot '.venv\Scripts\python.exe'
$anvilLock = Join-Path $PSScriptRoot "requirements-windows-py312-$Profile.lock"
Push-Location -LiteralPath $anvilRoot
try {
    if (-not (Test-Path -LiteralPath $anvilPython)) {
        & py -3.12 -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 venv creation failed.' }
    }
    & $anvilPython -c "import sys,platform; assert sys.version_info[:2] == (3,12) and sys.platform == 'win32' and platform.machine().lower() in ('amd64','x86_64'), 'Requires Windows x64 / Python 3.12'"
    if ($LASTEXITCODE -ne 0) { throw 'Unsupported interpreter.' }
    $anvilInstallArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', '--only-binary=:all:', '-r', $anvilLock)
    if ($Wheelhouse) {
        $anvilInstallArgs += @('--no-index', '--find-links', (Resolve-Path -LiteralPath $Wheelhouse).Path)
    }
    & $anvilPython @anvilInstallArgs
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed; inspect the pip error above.' }
    & $anvilPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Dependency validation failed.' }
    Write-Output "Installed profile: $Profile. Next: .\start.ps1 -CodeOnly -Doctor"
} finally { Pop-Location }
