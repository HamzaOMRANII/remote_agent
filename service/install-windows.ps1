# Runs the Remote agent by itself on Windows: a scheduled task that starts when you sign in, restarts after a
# crash (every minute, without limit) and keeps running on battery. Output goes to logs\agent.log.
#
#   powershell -ExecutionPolicy Bypass -File service\install-windows.ps1              install + start
#   powershell -ExecutionPolicy Bypass -File service\install-windows.ps1 -Uninstall   stop + remove
param([switch]$Uninstall)
$ErrorActionPreference = 'Stop'
$TaskName = 'Remote agent'
$Dir = Split-Path -Parent $PSScriptRoot

if ($Uninstall) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host 'Removed. The agent no longer starts by itself.'
    exit 0
}

# Python: the "py" launcher first (python.org installer), then python on PATH
$Python = $null
foreach ($c in @('py', 'python')) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    # skip the Microsoft Store placeholder (WindowsApps\python.exe opens the Store instead of running Python)
    if ($cmd -and $cmd.Source -notlike '*\WindowsApps\*') { $Python = $cmd.Source; break }
}
if (-not $Python) { throw 'Python not found: install Python 3 from python.org (tick "Add python.exe to PATH").' }
$PyArgs = if ((Split-Path -Leaf $Python) -like 'py*.exe' -and (Split-Path -Leaf $Python) -notlike 'python*') { '-3 -u' } else { '-u' }

$Config = Join-Path $Dir 'config.json'
if (-not (Test-Path $Config)) { throw 'No config.json yet: pair this device first (see README, --pair).' }
if (-not (Select-String -Path $Config -Pattern '"token"' -Quiet)) { throw 'config.json has no token: pair this device first.' }

# Video (M9): this laptop's own camera and the network cameras need aiortc (bundles ffmpeg). Installed once, for this user.
$PyPre = if ($PyArgs -like '-3*') { @('-3') } else { @() }
& $Python @PyPre -c "import aiortc" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing video support (aiortc, one time, ~1 minute)...'
    & $Python @PyPre -m pip install --user --disable-pip-version-check aiortc
    if ($LASTEXITCODE -ne 0) { Write-Warning 'Could not install aiortc: cameras stay off. Try:  py -m pip install aiortc' }
}

$Logs = Join-Path $Dir 'logs'
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
# A small wrapper so the output lands in a log file (a scheduled task has no console)
$Runner = Join-Path $Dir 'service\run-agent.cmd'
@"
@echo off
cd /d "$Dir"
"$Python" $PyArgs agent\remote_agent.py --config "$Config" >> "$Logs\agent.log" 2>&1
"@ | Set-Content -Encoding ASCII -Path $Runner

$Action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$Runner`"" -WorkingDirectory $Dir
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 9999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description 'Remote agent (remote.omranisystems.com): camera, scripts, discovery' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host 'Installed: the agent starts when you sign in and restarts after a crash.'
Write-Host "Log: $Logs\agent.log    Remove: powershell -ExecutionPolicy Bypass -File service\install-windows.ps1 -Uninstall"
