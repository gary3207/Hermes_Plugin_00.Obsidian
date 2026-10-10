param([string]$ConfigPath)
$ErrorActionPreference = 'Stop'
if (-not $ConfigPath) { $ConfigPath = Join-Path $PSScriptRoot 'archive-config.json' }
$ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path
$Config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$Python = $Config.python_executable
if (-not $Python) { $Python = (Get-Command python.exe -ErrorAction Stop).Source }
if ([IO.Path]::GetFileName($Python) -ieq 'pythonw.exe') { $Python = Join-Path (Split-Path $Python) 'python.exe' }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Python executable was not found.' }
New-Item -ItemType Directory -Force -Path $Config.runtime | Out-Null
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
$env:HERMES_HOME = $Config.base_home
$Source = if ($Config.source) { $Config.source } else { $PSScriptRoot }
$Archive = Join-Path $Source 'archive.py'
$Arguments = '"{0}" --config "{1}"' -f $Archive, $ConfigPath
$Process = Start-Process -FilePath $Python -ArgumentList $Arguments -WorkingDirectory $Source `
    -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput (Join-Path $Config.runtime 'last-task.json') `
    -RedirectStandardError (Join-Path $Config.runtime 'last-task.stderr.log')
exit $Process.ExitCode
