# Register a Windows scheduled task that runs autostart.py at every logon.
# The watcher then launches monitor.py automatically when TEMS Discovery opens.
$ErrorActionPreference = "Stop"

$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script = Join-Path $Base "autostart.py"

$RealPython = (& python -c "import sys; print(sys.executable)").Trim()
if (-not (Test-Path $RealPython)) { throw "python not found" }
$Pythonw = Join-Path (Split-Path $RealPython -Parent) "pythonw.exe"
$Exe = if (Test-Path $Pythonw) { $Pythonw } else { $RealPython }

$TaskName = "TEMS Monitor Autostart"
$Action = New-ScheduledTaskAction -Execute $Exe -Argument "`"$Script`"" -WorkingDirectory $Base
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Description "Starts the TEMS monitor when TEMS Discovery starts" -Force | Out-Null

Write-Host "OK registered: $TaskName"
Write-Host "  exe : $Exe"
Write-Host "  args: $Script"
