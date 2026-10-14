# Run manually to restart this project's locally managed Windows preview.
param([switch]$Lan)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$workPath = Join-Path $projectRoot 'work'
$pidPath = Join-Path $workPath 'windows-test.pid'
$launcherPath = Join-Path $projectRoot 'start_windows.py'
$pythonPath = $null

foreach ($candidate in @('py', 'python')) {
    if (-not (Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
    $candidateArguments = @()
    if ($candidate -eq 'py') { $candidateArguments += '-3' }
    $candidateArguments += @('-c', 'import sys; print(sys.executable); raise SystemExit(sys.version_info < (3, 12))')
    $probeResult = & $candidate @candidateArguments 2>$null
    if ($LASTEXITCODE -eq 0 -and $probeResult) {
        $pythonPath = [string]($probeResult | Select-Object -Last 1)
        break
    }
}
if (-not $pythonPath) { throw 'Python 3.12 or newer is required.' }
& $pythonPath -c 'from PIL import Image; from importlib.metadata import version; version("numpy"); version("opencv-python-headless")' 2>$null
if ($LASTEXITCODE -ne 0) { throw 'Install requirements.txt with the same Python before restarting.' }

$versionCode = @'
from pathlib import Path
import ast, sys
tree = ast.parse((Path(sys.argv[1]) / 'server.py').read_text(encoding='utf-8'))
print(next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
           and any(isinstance(target, ast.Name) and target.id == 'VERSION' for target in node.targets)))
'@
$expectedVersion = (& $pythonPath -c $versionCode $projectRoot | Select-Object -Last 1).Trim()
if ($LASTEXITCODE -ne 0 -or $expectedVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw 'Unable to read the project version. The existing reader was left running.'
}

$listeners = @(Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue)
$managedProcess = $null
if ($listeners.Count) {
    if (-not (Test-Path -LiteralPath $pidPath)) {
        throw 'Port 8080 is occupied by an unmanaged service. Close the old reader window first.'
    }
    $managedPid = 0
    if (-not [int]::TryParse((Get-Content -LiteralPath $pidPath -Raw).Trim(), [ref]$managedPid)) {
        throw 'Invalid managed process file. No process was stopped.'
    }
    if (@($listeners | Where-Object { $_.OwningProcess -ne $managedPid }).Count) {
        throw 'Port 8080 belongs to a different process. No process was stopped.'
    }
    $managedProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$managedPid"
    if (-not $managedProcess -or
        [IO.Path]::GetFileName($managedProcess.ExecutablePath) -notmatch '^python(w)?\.exe$' -or
        $managedProcess.CommandLine -notmatch '(?i)(^|[\s"\\])start_windows\.py([\s"]|$)') {
        throw 'The managed process does not match the Windows reader launcher. No process was stopped.'
    }
}

# SQLite's online backup includes committed WAL changes without exposing account material.
$backupCode = @'
from pathlib import Path
from contextlib import closing
from datetime import datetime, timezone
import os, secrets, sqlite3, sys
data = Path(sys.argv[1]) / 'data-windows'
source = data / 'library.sqlite3'
backups = data / 'backups'
if source.exists():
    if backups.is_symlink() or not backups.resolve().is_relative_to(data.resolve()):
        raise RuntimeError('Backup directory must stay inside the data directory.')
    backups.mkdir(mode=0o700, exist_ok=True)
    backup = backups / ('before-restart-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
                        + '-' + secrets.token_hex(4) + '.sqlite3')
    descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as db:
        with closing(sqlite3.connect(backup)) as target:
            db.backup(target)
    print('Saved ' + str(backup.relative_to(data)))
'@
& $pythonPath -c $backupCode $projectRoot
if ($LASTEXITCODE -ne 0) { throw 'Backup failed. The existing reader was left running.' }

if ($managedProcess) {
    $currentProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$managedPid"
    if (-not $currentProcess -or $currentProcess.CreationDate -ne $managedProcess.CreationDate) {
        throw 'The process changed during preparation. No process was stopped.'
    }
    Stop-Process -Id $managedPid
    Wait-Process -Id $managedPid -Timeout 10 -ErrorAction SilentlyContinue
}
New-Item -ItemType Directory -Path $workPath -Force | Out-Null
$stdoutPath = Join-Path $workPath 'windows-test.stdout.log'
$stderrPath = Join-Path $workPath 'windows-test.stderr.log'
$launcherArguments = '"' + $launcherPath + '"'
if ($Lan) { $launcherArguments += ' --lan' }
$startedProcess = Start-Process -FilePath $pythonPath -ArgumentList $launcherArguments `
    -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
Set-Content -LiteralPath $pidPath -Value $startedProcess.Id -Encoding ascii
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8080/api/health' -TimeoutSec 1
        if ($health.version -eq $expectedVersion) {
            Write-Host "Gravelle $expectedVersion is ready: http://127.0.0.1:8080"
            if ($Lan) { Write-Host 'LAN access is enabled. Use this computer''s IPv4 address with port 8080.' }
            exit 0
        }
    } catch { }
    $startedProcess.Refresh()
    if ($startedProcess.HasExited) { break }
    Start-Sleep -Milliseconds 250
}
throw 'The reader did not become ready. See work/windows-test.stderr.log for details.'
