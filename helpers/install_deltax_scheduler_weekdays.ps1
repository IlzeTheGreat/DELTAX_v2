param(
    [string]$TaskName = "DELTAX-Scheduler"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Scheduler = Join-Path $ProjectRoot "deltax\scheduler.py"

if (!(Test-Path $Python)) {
    throw "Python venv not found: $Python"
}

if (!(Test-Path $Scheduler)) {
    throw "Scheduler not found: $Scheduler"
}

# Weekdays at 15:15 local Windows time.
$Trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -WeeksInterval 1 `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At 3:15PM

$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "`"$Scheduler`"" `
    -WorkingDirectory $ProjectRoot

# 15:15 -> 23:05 = 7h 50m.
# Task Scheduler will terminate the scheduler process after this duration.
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 7 -Minutes 50)

$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

$Task = New-ScheduledTask `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal

Register-ScheduledTask `
    -TaskName $TaskName `
    -InputObject $Task `
    -Force | Out-Null

$Info = Get-ScheduledTaskInfo -TaskName $TaskName
$TaskState = Get-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "DELTAX scheduler installed."
Write-Host "Task: $TaskName"
Write-Host "Schedule: Monday-Friday 15:15"
Write-Host "Automatic stop: 23:05 (7h 50m execution limit)"
Write-Host "State now: $($TaskState.State)"
Write-Host "Next run: $($Info.NextRunTime)"
Write-Host ""
Write-Host "NOTE: installer does NOT start the task immediately."
Write-Host ""
Write-Host "Check:"
Write-Host "  Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Get-ScheduledTaskInfo -TaskName '$TaskName'"
Write-Host ""
Write-Host "Manual stop:"
Write-Host "  Stop-ScheduledTask -TaskName '$TaskName'"
