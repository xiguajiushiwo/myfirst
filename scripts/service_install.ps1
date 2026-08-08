# Yunxiaoquan QC service - autostart installer (Windows Task Scheduler, no NSSM needed)
#
# Registers a scheduled task that starts the self-guarding run_server.bat at user logon
# (run_server.bat has an internal :loop that auto-restarts uvicorn on crash -> double safety).
#
# Usage (PowerShell):
#     powershell -ExecutionPolicy Bypass -File scripts\service_install.ps1
#   Uninstall:
#     powershell -ExecutionPolicy Bypass -File scripts\service_install.ps1 -Uninstall
#
# Runs as the CURRENT USER at logon (GigE cameras + GPU + .env are only reachable in the
# user session; SYSTEM/session-0 usually cannot access them). Pair with station auto-login
# for boot autostart.

param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$TaskName = 'YunxiaoquanQC'
$root = Split-Path -Parent $PSScriptRoot
$bat  = Join-Path $root 'scripts\run_server.bat'

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Uninstalled task: $TaskName"
    } else {
        Write-Output "Task not found: $TaskName"
    }
    return
}

if (-not (Test-Path $bat)) { throw "run_server.bat not found: $bat" }

$me = "$env:USERDOMAIN\$env:USERNAME"
$action    = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$bat`""
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User $me
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable `
                -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
                -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $me -RunLevel Highest -LogonType Interactive

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Output "Registered autostart task: $TaskName (user=$me, at logon, restart-on-fail)"
Write-Output "  start now:  Start-ScheduledTask -TaskName $TaskName"
Write-Output "  status:     Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Output "  stop:       Stop-ScheduledTask -TaskName $TaskName"
