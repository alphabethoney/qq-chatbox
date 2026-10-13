param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Repository,
    [Parameter(Mandatory=$true)][string]$Config,
    [string]$TaskName = 'QQ-Private-Sync',
    [string]$At = '03:00'
)
$ErrorActionPreference = 'Stop'
$Python = (Resolve-Path -LiteralPath $Python).Path
$Repository = (Resolve-Path -LiteralPath $Repository).Path
$Config = (Resolve-Path -LiteralPath $Config).Path
$Script = Join-Path $Repository 'tools\daily_private_sync.py'
if (-not (Test-Path -LiteralPath $Script -PathType Leaf)) { throw 'Daily sync entry is missing' }
if ($Python.Contains('"') -or $Config.Contains('"') -or $Script.Contains('"')) { throw 'Invalid quoted path' }
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { throw 'Task already exists; inspect before replacing' }
$clock = [datetime]::ParseExact($At, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
$action = New-ScheduledTaskAction -Execute $Python -Argument ('"{0}" --config "{1}"' -f $Script, $Config) -WorkingDirectory $Repository
$trigger = New-ScheduledTaskTrigger -Daily -At $clock
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Local private conversation sync; QQ must be offline; preserves other index conversations. No remote upload.'
Register-ScheduledTask -TaskName $TaskName -InputObject $task | Select-Object TaskName,State
Get-ScheduledTaskInfo -TaskName $TaskName | Select-Object NextRunTime,LastRunTime,LastTaskResult
