param(
    [string]$ConfigPath,
    [string]$PythonW,
    [ValidateSet('Register', 'Pause', 'Remove', 'Status')][string]$Mode = 'Register',
    [switch]$NoStart
)
$ErrorActionPreference = 'Stop'
$Name = 'Hermes-Obsidian-Archive'
$Description = 'Hermes local read-only Obsidian archive v1'
$Existing = Get-ScheduledTask -TaskName $Name -TaskPath '\' -ErrorAction SilentlyContinue
if ($Existing -and $Existing.Description -ne $Description) { throw 'A task with this name is not owned by this archive.' }
if ($Mode -eq 'Status') {
    @{ Exists = [bool]$Existing; State = if ($Existing) { [string]$Existing.State } else { 'Missing' } } | ConvertTo-Json
    return
}
if ($Mode -eq 'Pause' -or $Mode -eq 'Remove') {
    $Enabled = $Existing -and $Existing.Settings.Enabled
    if ($Existing) {
        Disable-ScheduledTask -TaskName $Name -TaskPath '\' | Out-Null
        if ((Get-ScheduledTask -TaskName $Name -TaskPath '\').Settings.Enabled) { throw 'Scheduled task disable readback failed.' }
        $Deadline = (Get-Date).AddSeconds(60)
        while ((Get-ScheduledTask -TaskName $Name -TaskPath '\').State -eq 'Running') {
            if ((Get-Date) -ge $Deadline) {
                if ($Enabled) { Enable-ScheduledTask -TaskName $Name -TaskPath '\' | Out-Null }
                throw 'Collector still running after 60 seconds; no payload updated.'
            }
            Start-Sleep -Milliseconds 250
        }
        if ($Mode -eq 'Remove') {
            Unregister-ScheduledTask -TaskName $Name -TaskPath '\' -Confirm:$false
            if (Get-ScheduledTask -TaskName $Name -TaskPath '\' -ErrorAction SilentlyContinue) { throw 'Scheduled task removal readback failed.' }
        }
    }
    @{ Exists = [bool]$Existing; Enabled = [bool]$Enabled } | ConvertTo-Json
    return
}
if (-not $ConfigPath) { $ConfigPath = Join-Path $PSScriptRoot 'archive-config.json' }
$ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path
$Config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $PythonW -and $Config.python_executable) { $PythonW = Join-Path (Split-Path $Config.python_executable) 'pythonw.exe' }
if (-not $PythonW) { $PythonW = (Get-Command pythonw.exe -ErrorAction Stop).Source }
if (-not (Test-Path -LiteralPath $PythonW -PathType Leaf)) { throw 'Windowless Python executable was not found.' }
$Source = if ($Config.source) { $Config.source } else { $PSScriptRoot }
$Runner = Join-Path $Source 'run-archive-hidden.pyw'
if (-not (Test-Path -LiteralPath $Runner -PathType Leaf)) { throw 'Hidden archive runner was not found.' }
$Arguments = '"{0}" --config "{1}"' -f $Runner, $ConfigPath
$Action = New-ScheduledTaskAction -Execute $PythonW -Argument $Arguments -WorkingDirectory $Source
if ($Existing) {
    # Preserve triggers, principal, settings and task identity.
    Set-ScheduledTask -TaskName $Name -TaskPath '\' -Action $Action | Out-Null
} else {
    $User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $Periodic = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval ([TimeSpan]::FromMinutes(5))
    $Logon = New-ScheduledTaskTrigger -AtLogOn -User $User
    $Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
    $Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::FromMinutes(10))
    Register-ScheduledTask -TaskName $Name -TaskPath '\' -Description $Description -Action $Action `
        -Trigger @($Periodic, $Logon) -Principal $Principal -Settings $Settings | Out-Null
}
Enable-ScheduledTask -TaskName $Name -TaskPath '\' | Out-Null
$Readback = Get-ScheduledTask -TaskName $Name -TaskPath '\'
if ($Readback.Actions.Count -ne 1 -or $Readback.Actions[0].Arguments -ne $Arguments -or
    $Readback.Actions[0].Execute -ne $PythonW -or $Readback.Actions[0].WorkingDirectory -ne $Source -or
    -not $Readback.Settings.Enabled -or $Readback.Description -ne $Description) { throw 'Scheduled task action readback mismatch.' }
if (-not $Existing -and $Readback.Triggers[0].Repetition.Interval -ne 'PT5M') { throw 'Scheduled task interval readback failed.' }
if (-not $NoStart) { Start-ScheduledTask -TaskName $Name -TaskPath '\' }
@{ TaskName = $Readback.TaskName; State = [string]$Readback.State;
   Action = $Readback.Actions[0].Execute; Arguments = $Readback.Actions[0].Arguments;
   Interval = $Readback.Triggers[0].Repetition.Interval } | ConvertTo-Json -Depth 5
