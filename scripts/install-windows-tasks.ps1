# Registers TARS with Task Scheduler for the current user:
#   - "TARS" runs `tars voice` at logon (needs your audio session, so not a service)
#   - "TARS vault backup" runs `tars backup` nightly at 3:00, waking the PC if asleep
# Run from the repo root after installing into .venv (`uv sync --locked --extra voice`).

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$tars = Join-Path $repo ".venv\Scripts\tars.exe"
if (-not (Test-Path $tars)) { throw "Couldn't find $tars. Install TARS into .venv first." }

$user = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

$voice = New-ScheduledTaskAction -Execute $tars -Argument "voice" -WorkingDirectory $repo
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
$voiceSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "TARS" -Action $voice -Trigger $atLogon -Principal $principal `
    -Settings $voiceSettings -Description "TARS voice assistant (push-to-talk)" -Force | Out-Null

$backup = New-ScheduledTaskAction -Execute $tars -Argument "backup" -WorkingDirectory $repo
$nightly = New-ScheduledTaskTrigger -Daily -At 3:00am
$backupSettings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName "TARS vault backup" -Action $backup -Trigger $nightly -Principal $principal `
    -Settings $backupSettings -Description "Nightly copy of the TARS memory vault (kept 30 days)" -Force | Out-Null

Write-Host "Registered 'TARS' (at logon) and 'TARS vault backup' (nightly at 3:00)."
Write-Host "Wake timers must be allowed: Power Options > Sleep > Allow wake timers > Enable."
