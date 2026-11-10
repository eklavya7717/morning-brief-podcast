# Registers "The Morning Brief" as a daily Windows scheduled task.
# Runs every morning at the configured time even if the app is closed.
# Needs the PC to be on (or asleep with wake timers enabled); it will wake from sleep.
#
# Usage (no admin needed for the current user):
#   powershell -ExecutionPolicy Bypass -File tools\install_schedule.ps1
#   powershell -ExecutionPolicy Bypass -File tools\install_schedule.ps1 -Time 06:20
#   powershell -ExecutionPolicy Bypass -File tools\install_schedule.ps1 -Remove

param(
    [string]$Time = "06:25",
    [string]$TaskName = "The Morning Brief",
    [switch]$Remove
)

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$script = Join-Path $root "run.ps1"

if ($Remove) {
    schtasks /Delete /TN $TaskName /F 2>&1 | Out-String | Write-Host
    Write-Host "Removed scheduled task '$TaskName'."
    return
}

if (-not (Test-Path $script)) { throw "run.ps1 not found at $script" }

$ps = (Get-Command powershell.exe).Source
$action = New-ScheduledTaskAction -Execute $ps -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
$settings.DisallowStartIfOnBatteries = $false

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Builds and pushes The Morning Brief (PDF + audio) to Eklav's iPad/iPhone at $Time." -Force | Out-Null

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "Registered '$TaskName' - runs daily at $Time."
Write-Host ("Next run: " + $info.NextRunTime)
Write-Host ("State: " + $task.State)
Write-Host ""
Write-Host "Test it now with:  schtasks /Run /TN `"$TaskName`""
Write-Host "Follow the run:    Get-Content logs\run-$(Get-Date -Format yyyy-MM-dd).log -Wait"
