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
$venvPython = Join-Path $project ".venv\Scripts\python.exe"
$systemPython = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
$pythonPathFile = Join-Path $project ".runtime\python-path.txt"
$savedPython = $null
if (Test-Path -LiteralPath $pythonPathFile -PathType Leaf) {
    $savedPython = (Get-Content -LiteralPath $pythonPathFile -TotalCount 1).Trim()
}
$candidates = @($venvPython, $systemPython.Source, $savedPython)
foreach ($candidate in $candidates) {
    if (-not $candidate -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
    try {
        & $candidate -c "import importlib.util as u, sys; sys.exit(not (sys.version_info >= (3, 12) and u.find_spec('PIL') and u.find_spec('cryptography')))"
        if ($LASTEXITCODE -eq 0) {
            $pythonPath = $candidate
            break
        }
    } catch {
        continue
    }
}
if (-not $pythonPath) {
    if (-not $systemPython) { throw "Python 3.12+ is required." }
    & $systemPython.Source -c "import sys; sys.exit(sys.version_info < (3, 12))"
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12+ is required." }
    & $systemPython.Source -m venv (Join-Path $project ".venv")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $pythonPath = $venvPython
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
