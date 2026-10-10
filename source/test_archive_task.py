"""Run the real scheduled task and check for console-window flashes."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

USER32 = ctypes.WinDLL('user32', use_last_error=True)
CALLBACK = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
USER32.IsWindowVisible.argtypes = [wintypes.HWND]
USER32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
USER32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
USER32.EnumWindows.argtypes = [CALLBACK, wintypes.LPARAM]


def consoles():
    found = set()
    @CALLBACK
    def visit(hwnd, _):
        name = ctypes.create_unicode_buffer(256)
        USER32.GetClassNameW(hwnd, name, len(name))
        if USER32.IsWindowVisible(hwnd) and name.value == 'ConsoleWindowClass':
            pid = wintypes.DWORD()
            USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.add((int(hwnd), pid.value))
        return True
    USER32.EnumWindows(visit, 0)
    return found


def ps(command):
    return subprocess.check_output(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command], creationflags=subprocess.CREATE_NO_WINDOW, text=True)


import unittest
from unittest.mock import patch
import runpy
import tempfile
import os


def runtime_config():
    from setup_archive import default_home
    path = default_home() / 'archive-obsidian/config.json'
    if not path.is_file():
        path = Path(__file__).resolve().parent / 'archive-config.json'  # pre-installer legacy
    return json.loads(path.read_text(encoding='utf-8-sig'))


class RunnerTests(unittest.TestCase):
    def test_hidden_runner_forwards_absolute_config_and_exit_code(self):
        root = Path(__file__).resolve().parent
        config = root / 'fake runtime/config.json'
        with patch.object(sys, 'argv', ['run-archive-hidden.pyw', '--config', str(config)]), \
                patch('subprocess.call', return_value=17) as call:
            with self.assertRaises(SystemExit) as exit_status:
                runpy.run_path(str(root / 'run-archive-hidden.pyw'), run_name='__main__')
        self.assertEqual(exit_status.exception.code, 17)
        args = call.call_args.args[0]
        self.assertIn('-ConfigPath', args)
        self.assertEqual(args[args.index('-ConfigPath') + 1], str(config.resolve()))
        self.assertEqual(call.call_args.kwargs['creationflags'], subprocess.CREATE_NO_WINDOW)

    def test_real_powershell_runner_uses_fake_payload_and_propagates_failure(self):
        root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(dir=Path(os.environ['LOCALAPPDATA']) / 'hermes/cache/scratch') as temp:
            work = Path(temp).resolve()
            payload = work / 'fake payload'
            payload.mkdir()
            runtime = work / 'runtime'
            config = work / 'config.json'
            config.write_text(json.dumps({'base_home': str(work / 'fake home'), 'runtime': str(runtime),
                                         'source': str(payload), 'python_executable': sys.executable}))
            payload.joinpath('archive.py').write_text('import json, os, sys\nprint(json.dumps({"home": os.environ["HERMES_HOME"], "args": sys.argv[1:]}))\nsys.exit(23)\n')
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                     '-File', str(root / 'run-archive.ps1'), '-ConfigPath', str(config)],
                                    capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 23, result.stderr)
            output = json.loads(runtime.joinpath('last-task.json').read_text(encoding='utf-8-sig'))
            self.assertEqual(output['home'], str(work / 'fake home'))
            self.assertEqual(output['args'], ['--config', str(config)])

    def test_scheduler_ownership_update_pause_remove_using_fake_cmdlets(self):
        root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(dir=Path(os.environ['LOCALAPPDATA']) / 'hermes/cache/scratch') as temp:
            work = Path(temp).resolve()
            config = work / 'config.json'
            config.write_text(json.dumps({'source': str(root), 'python_executable': sys.executable}))
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            harness = r'''
$global:FakeTask = [pscustomobject]@{TaskName='Hermes-Obsidian-Archive'; Description='Hermes local read-only Obsidian archive v1'; State='Ready'; Settings=[pscustomobject]@{Enabled=$true}; Triggers=@([pscustomobject]@{Repetition=[pscustomobject]@{Interval='PT7M'}}); Actions=@(); Principal='unchanged'}
$global:Started = $false
function Get-ScheduledTask { param($TaskName,$TaskPath,$ErrorAction) return $global:FakeTask }
function Set-ScheduledTask { param($TaskName,$TaskPath,$Action) $global:FakeTask.Actions=@($Action) }
function New-ScheduledTaskAction { param($Execute,$Argument,$WorkingDirectory) [pscustomobject]@{Execute=$Execute;Arguments=$Argument;WorkingDirectory=$WorkingDirectory} }
function Enable-ScheduledTask { param($TaskName,$TaskPath) $global:FakeTask.Settings.Enabled=$true }
function Disable-ScheduledTask { param($TaskName,$TaskPath) $global:FakeTask.Settings.Enabled=$false; $global:FakeTask.State='Disabled' }
function Unregister-ScheduledTask { param($TaskName,$TaskPath,$Confirm) $global:FakeTask=$null }
function Start-ScheduledTask { param($TaskName,$TaskPath) $global:Started=$true }
function Register-ScheduledTask { throw 'Unexpected trigger replacement' }
'''
            invocation = '& ' + quote(root / 'register-task.ps1')
            check = (harness + invocation + ' -Mode Register -NoStart -ConfigPath ' + quote(config) + ' | Out-Null; '
                     + "if($global:Started -or $global:FakeTask.Triggers[0].Repetition.Interval -ne 'PT7M' -or $global:FakeTask.Principal -ne 'unchanged'){throw 'Schedule changed'}; "
                     + invocation + " -Mode Pause | Out-Null; if($global:FakeTask.Settings.Enabled){throw 'Pause failed'}; "
                     + invocation + " -Mode Remove | Out-Null; if($global:FakeTask){throw 'Remove failed'}; 'PASS'")
            self.assertEqual(ps(check).strip(), 'PASS')
            foreign = harness + "$global:FakeTask.Description='foreign'; try { " + invocation + " -Mode Remove | Out-Null; throw 'Unexpected success' } catch { if($_.Exception.Message -notlike '*not owned*'){throw}; if(-not $global:FakeTask.Settings.Enabled){throw 'Foreign task changed'}; 'PASS' }"
            self.assertEqual(ps(foreign).strip(), 'PASS')

    def test_live_check_prefers_runtime_config_over_legacy(self):
        with tempfile.TemporaryDirectory(dir=Path(os.environ['LOCALAPPDATA']) / 'hermes/cache/scratch') as temp:
            home = Path(temp).resolve()
            home.joinpath('archive-obsidian').mkdir()
            config = {'runtime': str(home / 'archive-obsidian'), 'marker': 'runtime'}
            home.joinpath('archive-obsidian/config.json').write_text(json.dumps(config))
            with patch.dict(os.environ, {'HERMES_HOME': str(home)}):
                self.assertEqual(runtime_config(), config)

    def test_powershell_scripts_parse_and_accept_runtime_config(self):
        root = Path(__file__).resolve().parent
        for name in ('run-archive.ps1', 'register-task.ps1'):
            script = str(root / name).replace("'", "''")
            result = ps("$tokens=$null;$errors=$null;$ast=[System.Management.Automation.Language.Parser]::ParseFile('" + script + "',[ref]$tokens,[ref]$errors); if($errors.Count){exit 1}; $ast.ParamBlock.Parameters.Name.VariablePath.UserPath | ConvertTo-Json")
            self.assertIn('ConfigPath', json.loads(result))
        runner = (root / 'run-archive.ps1').read_text()
        self.assertIn('python_executable', runner)
        self.assertNotIn("-Filter 'python-*'", runner)
        register = (root / 'register-task.ps1').read_text()
        self.assertIn('--config', register)
        self.assertIn('Disable-ScheduledTask', register)
        self.assertIn('Unregister-ScheduledTask', register)


if __name__ == '__main__':
    baseline = consoles()
    seen = set()
    stop = threading.Event()
    def watch():
        while not stop.is_set():
            seen.update(consoles() - baseline)
            time.sleep(0.005)
    thread = threading.Thread(target=watch)
    thread.start()
    try:
        ps('Start-ScheduledTask -TaskName "Hermes-Obsidian-Archive"')
        time.sleep(8)
    finally:
        stop.set()
        thread.join()
    result = json.loads(ps('$t=Get-ScheduledTask -TaskName "Hermes-Obsidian-Archive"; $i=$t|Get-ScheduledTaskInfo; @{State=[string]$t.State;Result=$i.LastTaskResult;Execute=$t.Actions[0].Execute;Arguments=$t.Actions[0].Arguments;Interval=$t.Triggers[0].Repetition.Interval;LastRun=$i.LastRunTime.ToString("o")} | ConvertTo-Json'))
    result['new_visible_consoles'] = sorted(seen)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if '--observe' not in sys.argv:
        assert result['Execute'].lower().endswith('pythonw.exe'), result
        assert result['Result'] == 0 and result['State'] == 'Ready', result
        assert result['Interval'] == 'PT5M', result
        assert not seen, result
        config = runtime_config()
        output = json.loads((Path(config['runtime']) / 'last-task.json').read_text(encoding='utf-8-sig'))
        assert output.get('errors') == [], output
        print('PASS: hidden scheduled launch, exit 0, PT5M, collector errors=[]')
