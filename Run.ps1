param(
    [ValidateSet("start","stop","status","open","admin","build")]
    [string]$Action = "start",
    [ValidateSet("native","docker")]
    [string]$Mode = "native",
    [switch]$NoBrowser
)
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONDONTWRITEBYTECODE = "1"
$project = $PSScriptRoot
$pythonPath = $null
$bundled = $env:CODEX_PRIMARY_RUNTIME_PYTHON
$savedPython = Join-Path $project ".runtime\python-path.txt"
if (-not $bundled -and (Test-Path -LiteralPath $savedPython)) {
    $bundled = (Get-Content -LiteralPath $savedPython -TotalCount 1).Trim()
}
if (-not $bundled) {
    $node = Get-Command node -ErrorAction SilentlyContinue
    if ($node) {
        $bundled = Join-Path (Split-Path (Split-Path (Split-Path $node.Source))) "python\python.exe"
    }
}
if (-not $bundled -or -not (Test-Path -LiteralPath $bundled)) {
    try {
        $package = Get-AppxPackage -Name "OpenAI.CodexPrimaryRuntime*" -ErrorAction Stop | Select-Object -First 1
        if ($package) { $bundled = Join-Path $package.InstallLocation "dependencies\python\python.exe" }
    } catch {
        $bundled = $null
    }
}
$systemPython = Get-Command python -ErrorAction SilentlyContinue
$candidates = @((Join-Path $project ".venv\Scripts\python.exe"), $bundled, $systemPython.Source)
foreach ($candidate in $candidates) {
    if ($candidate -and (Test-Path -LiteralPath $candidate)) {
        & $candidate -c "import importlib.util as u, sys; sys.exit(not (sys.version_info >= (3, 12) and u.find_spec('PIL') and u.find_spec('cryptography')))"
        if ($LASTEXITCODE -eq 0) {
            $pythonPath = $candidate
            break
        }
    }
}
if (-not $pythonPath) {
    if (-not $systemPython) { throw "Python 3.12+ is required." }
    & $systemPython.Source -m venv (Join-Path $project ".venv")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $pythonPath = Join-Path $project ".venv\Scripts\python.exe"
    & $pythonPath -m pip install -r (Join-Path $project "requirements.txt")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
$commandArgs = @((Join-Path $project "manage.py"), $Action)
if ($Action -eq "start") {
    $commandArgs += @("--mode", $Mode)
    if (-not $NoBrowser) { $commandArgs += "--open" }
}
& $pythonPath @commandArgs
exit $LASTEXITCODE
