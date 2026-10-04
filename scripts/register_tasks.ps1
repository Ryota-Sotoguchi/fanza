# Registers the Windows Task Scheduler task for FANZA poster.
#   powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -Unregister
#
# The task runs scripts\tick.bat every 10 minutes from 11:50 to about 01:50,
# with "Wake the computer to run this task" (WakeToRun) enabled.
param(
    [string]$StartTime = "11:50",
    [int]$IntervalMinutes = 10,
    [int]$DurationMinutes = 840,
    [string]$TaskName = "FanzaPoster_Tick",
    [switch]$Unregister
)
$ErrorActionPreference = "Stop"
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed task: $TaskName"
    exit 0
}

$tickBat = Join-Path $ProjectDir "scripts\tick.bat"
$action = New-ScheduledTaskAction -Execute $tickBat -WorkingDirectory $ProjectDir

$trigger = New-ScheduledTaskTrigger -Daily -At $StartTime
$repeat = New-ScheduledTaskTrigger -Once -At $StartTime `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Minutes $DurationMinutes)
$trigger.Repetition = $repeat.Repetition

$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

# Run only while the user is logged on: the browser window must be able to open.
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "FANZA sale poster: runs tick.bat every $IntervalMinutes minutes" -Force | Out-Null

Write-Host "Registered task: $TaskName"
Write-Host "  Runs: $tickBat"
Write-Host "  Daily from $StartTime, every $IntervalMinutes min, for $DurationMinutes min"
Write-Host "  Wake the computer to run this task: ON"
Write-Host ""
Write-Host "Next: enable 'Allow wake timers' in Power Options (see README)."
