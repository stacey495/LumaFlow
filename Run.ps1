param(
    [ValidateSet("start","stop","status","open","admin","build","verify","docker-demo")]
    [string]$Action = "start",
    [ValidateSet("native","docker")]
    [string]$Mode = "native",
    [switch]$NoBrowser,
    [switch]$Live
)
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONDONTWRITEBYTECODE = "1"
if ($Mode -eq "docker" -or $Action -in @("build", "docker-demo")) {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        $dockerCandidates = @(
            (Join-Path $env:ProgramFiles "Docker\Docker\resources\bin"),
            (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin")
        )
        foreach ($dockerBin in $dockerCandidates) {
            if (Test-Path -LiteralPath (Join-Path $dockerBin "docker.exe")) {
                $env:PATH = "$dockerBin;$env:PATH"
                break
            }
        }
    }
}
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
if ($Action -eq "verify") {
    $commandArgs = @((Join-Path $project "verify.py"))
    if ($Live -or $Mode -eq "docker") { $commandArgs += @("--live", $Mode) }
}
if ($Action -eq "start") {
    $commandArgs += @("--mode", $Mode)
    if (-not $NoBrowser) { $commandArgs += "--open" }
}
if ($Action -eq "docker-demo" -and -not $NoBrowser) { $commandArgs += "--open" }
& $pythonPath @commandArgs
exit $LASTEXITCODE
