"""Standalone Windows archive setup. Only Vault and relative folder are required."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import sys
import traceback

PLUGIN = 'hermes-archive-context'
PAYLOAD = ('archive.py', 'setup_archive.py', '__init__.py', 'plugin.yaml',
           'run-archive-hidden.pyw', 'run-archive.ps1', 'register-task.ps1',
           'archive-profile/__init__.py', 'archive-profile/plugin.yaml', 'archive-profile/SKILL.md')


def default_home():
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()).resolve()
    except ImportError:
        return Path(os.environ.get('HERMES_HOME') or
                    str(Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'hermes')).resolve()


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}


def atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.setup-tmp')
    if temp.is_symlink() or temp.is_junction():
        raise ValueError('Unsafe installation temporary link')
    with temp.open('wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    if path.read_bytes() != data:
        raise OSError('Installation readback mismatch')


def save_json(path, value):
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def launcher():
    found = shutil.which('hermes')
    candidates = ([Path(found).with_suffix('.exe'), Path(found)] if found else [])
    candidates += [Path.home() / '.hermes/bin/hermes.exe']
    for candidate in candidates:
        if not candidate.is_file():
            continue
        candidate = candidate.resolve()
        try:
            result = subprocess.run([str(candidate), '--version'], capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', timeout=30,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return str(candidate)
    raise FileNotFoundError('Verified Hermes launcher not found')


def cli_runner(command):
    def call(home, *args):
        result = subprocess.run([command, *args], env={**os.environ, 'HERMES_HOME': str(home),
                                'PYTHONIOENCODING': 'utf-8'}, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=90,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            missing = (result.stdout + result.stderr).strip().splitlines()
            if args[:2] == ('config', 'get') and result.returncode == 1 and missing and missing[-1] == f'Config key not set: {args[2]}':
                if args[2] == 'plugins.disabled':
                    return '[]'
                if args[2] == 'sessions.auto_prune':
                    return 'unset'
            # Never include raw CLI output: it may contain user configuration/secrets.
            error = RuntimeError(f'Hermes CLI {args[0]} failed (exit {result.returncode})')
            setattr(error, 'cli_context', {'operation': list(args[:2]), 'key': args[2] if args[:2] == ('config', 'get') else None,
                                           'exit_code': result.returncode})
            raise error
        return result.stdout.strip()
    return call


def managed_plugin(path):
    if not path.exists() or not any(path.iterdir()):
        return
    manifest = path / 'plugin.yaml'
    if not manifest.is_file() or not re.search(
            r'(?m)^\s*(?:name|id)\s*:\s*[\"\']?' + re.escape(PLUGIN) + r'[\"\']?\s*$',
            manifest.read_text(encoding='utf-8-sig')):
        raise RuntimeError('Unrelated plugin collision; preserved')


def load_collector(source):
    import importlib.util
    spec = importlib.util.spec_from_file_location('_archive_setup_payload', Path(source) / 'archive.py')
    if spec is None or spec.loader is None:
        raise ImportError('Collector payload cannot be loaded')
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)
    return collector


def installation_state(cfg, source):
    # Reuse collector validation without native exporter discovery or database reads.
    collector = load_collector(source)
    collector.normalize_config(cfg)
    check = collector.Archive.__new__(collector.Archive)
    check.root = Path(cfg['archive_root'])
    mirror_path = check.output_path('60_Operations/.archive-state.json')
    mirror = read_json(mirror_path) if mirror_path.exists() else None
    mirror_hash = digest(mirror_path.read_bytes()) if mirror is not None else None
    if mirror is not None:
        if not isinstance(mirror, dict) or mirror.get('owner') != PLUGIN or set(mirror) != {'owner', 'version', 'files', 'sessions', 'projects', 'groups'}:
            raise RuntimeError('Foreign or malformed archive mirror; preserved')
        check.validate_state(mirror)
    state_path = Path(cfg['runtime']) / 'state.json'
    state = read_json(state_path) if state_path.exists() else (mirror or {'version': collector.VERSION, 'files': {}, 'sessions': {}})
    check.validate_state(state)
    if state.get('archive_root') not in (None, cfg['archive_root']):
        raise RuntimeError('Runtime archive root mismatch; preserved')
    if state.get('mirror_digest') not in (None, mirror_hash):
        raise RuntimeError('Archive mirror changed; preserved')
    state['archive_root'] = cfg['archive_root']
    state['mirror_digest'] = mirror_hash
    return state


def setup(vault, folder, *, home=None, source=None, replace_managed=False,
          schedule=True, collect=True, cli=None):
    home = Path(home or default_home()).resolve()
    if not (home / 'config.yaml').is_file():
        raise FileNotFoundError('Initialize Hermes configuration before archive setup')
    source = Path(source or Path(__file__).resolve().parent).resolve()
    vault = Path(vault).resolve()
    folder = str(folder).replace('\\', '/')
    parts = folder.split('/')
    if (not vault.is_dir() or PureWindowsPath(folder).drive or folder.startswith('/') or
            any(p in ('', '.', '..') or p.lower() == '.obsidian' or p.endswith((' ', '.')) for p in parts) or
            re.search(r'[\x00-\x1f\x7f\[\]`|<>:#*?\"{}]', folder)):
        raise ValueError('Vault must exist and folder must be a safe relative path')
    root = (vault / folder).resolve()
    if not root.is_relative_to(vault) or root == vault or root.is_relative_to(home):
        raise ValueError('Archive root escapes Vault or overlaps Hermes home')
    if root.exists() and not root.is_dir():
        raise ValueError('Archive root is not a directory')
    runtime = home / 'archive-obsidian'
    if not runtime.resolve().is_relative_to(home):
        raise ValueError('Private runtime escapes Hermes home')
    for config_path in (runtime / 'config.json', source / 'archive-config.json'):
        previous_cfg = read_json(config_path)
        previous_root = previous_cfg.get('archive_root')
        if previous_root and Path(previous_root).resolve() != root:
            raise ValueError('Archive migration was not requested; existing root differs')
    target = home / 'plugins' / PLUGIN
    command = launcher() if cli is None else sys.executable
    cli = cli or cli_runner(command)
    cfg = dict(base_home=str(home), vault_path=str(vault), archive_folder=folder,
               archive_root=str(root), runtime=str(runtime), source=str(target),
               hermes_command=command, python_executable=sys.executable, bootstrap_profiles=True)
    payload = {name: (source / name).read_bytes() for name in PAYLOAD}
    if schedule:
        scheduled('Pause', source, runtime / 'config.json')
    # Read after quiescence: an in-flight collector may have just updated ownership.
    state = installation_state(cfg, source)
    boot = state.setdefault('bootstrap', {})
    manifest = read_json(runtime / 'install.json')
    hashes = dict(manifest.get('files', {}))
    for record in boot.values():
        hashes.update(record.get('files', {}))
    homes = [('default', home)] + [(p.name, p.resolve()) for p in sorted((home / 'profiles').glob('*'))
                                  if p.is_dir() and not p.is_symlink() and (p / 'config.yaml').is_file()]
    plan = {target / name: data for name, data in payload.items()}
    profile_files = {}
    for profile, profile_home in homes:
        plugin = profile_home / 'plugins' / PLUGIN
        managed_plugin(plugin)
        assignments = [('archive-profile/SKILL.md', profile_home / 'skills/note-taking/hermes-archive-records/SKILL.md'),
                       ('archive-profile/SKILL.md', plugin / 'SKILL.md')]
        if profile != 'default':
            assignments += [('archive-profile/plugin.yaml', plugin / 'plugin.yaml'),
                            ('archive-profile/__init__.py', plugin / '__init__.py')]
        files = {}
        for origin, destination in assignments:
            plan[destination] = payload[origin]
            files[str(destination)] = digest(payload[origin])
        settings = plugin / 'archive-settings.json'
        settings_data = {**read_json(settings), **{key: cfg[key] for key in
                         ('base_home', 'vault_path', 'archive_folder', 'archive_root', 'runtime', 'source')}}
        plan[settings] = json.dumps(settings_data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        files[str(settings)] = digest(plan[settings])
        profile_files[profile] = files
    conflicts = []
    for destination, data in plan.items():
        # Refuse junction/symlink writes outside this installation's home.
        if not destination.resolve().is_relative_to(home):
            raise RuntimeError('Managed destination escapes Hermes home')
        if destination.exists() and destination.read_bytes() != data:
            if digest(destination.read_bytes()) != hashes.get(str(destination)):
                conflicts.append(destination)
    if conflicts and not replace_managed:
        raise RuntimeError('Archive-managed files were modified; preserved (use --replace-managed to back up)')
    backup = runtime / 'backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    for destination in conflicts:
        atomic_bytes(backup / destination.relative_to(home), destination.read_bytes())
    for destination, data in plan.items():
        if not destination.exists() or destination.read_bytes() != data:
            atomic_bytes(destination, data)
    save_json(runtime / 'config.json', cfg)
    save_json(runtime / 'install.json', {'files': {str(p): digest(data) for p, data in plan.items()}})
    for profile, profile_home in homes:
        files = profile_files[profile]
        previous = boot.get(profile, {})
        disabled = PLUGIN in cli(profile_home, 'config', 'get', 'plugins.disabled')
        if not disabled:
            cli(profile_home, 'plugins', 'enable', PLUGIN, '--no-allow-tool-override')
        observed = json.loads(cli(profile_home, 'plugins', 'list', '--json'))
        status = next((p['status'] for p in observed if p['name'] == PLUGIN), 'missing')
        if (not disabled and status != 'enabled') or (disabled and status == 'enabled'):
            raise RuntimeError('Plugin enable/disable readback failed')
        old_prune = previous.get('previous_auto_prune')
        observed_prune = cli(profile_home, 'config', 'get', 'sessions.auto_prune').strip().lower()
        if old_prune is None:
            old_prune = observed_prune
        if observed_prune != 'false':
            cli(profile_home, 'config', 'set', 'sessions.auto_prune', 'false')
            if cli(profile_home, 'config', 'get', 'sessions.auto_prune').strip().lower() != 'false':
                raise RuntimeError('Retention readback failed')
        boot[profile] = dict(files=files, previous_auto_prune=old_prune,
                             status='disabled-by-user' if disabled else status,
                             verified=datetime.now(timezone.utc).isoformat(),
                             config_stamp=(profile_home / 'config.yaml').stat().st_mtime_ns)
        save_json(runtime / 'state.json', state)
    collector = load_collector(target)
    with collector.lock(runtime):
        collector.Archive(cfg).save_state()
    if collect:
        collect_once(cfg)
    if schedule:
        scheduled('Register', target, runtime / 'config.json', no_start=not collect)
    return cfg


def scheduled(mode, source, config, *, no_start=False):
    if os.name != 'nt':
        raise OSError('Windows scheduler is required (or use --no-schedule)')
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    args = [str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-File', str(Path(source) / 'register-task.ps1'), '-Mode', mode,
            '-ConfigPath', str(Path(config).resolve())]
    if no_start:
        args.append('-NoStart')
    process = subprocess.run(args, capture_output=True, text=True, encoding='utf-8',
                             errors='replace', timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
    if process.returncode:
        raise RuntimeError(f'Scheduler {mode} failed (exit {process.returncode}); no raw output logged')
    return json.loads(process.stdout)


def collect_once(cfg):
    runtime = Path(cfg['runtime'])
    with (runtime / 'last-task.json').open('w', encoding='utf-8') as out, \
            (runtime / 'last-task.stderr.log').open('w', encoding='utf-8') as err:
        process = subprocess.run([cfg['python_executable'], str(Path(cfg['source']) / 'archive.py'),
                                  '--config', str(runtime / 'config.json')],
                                 cwd=cfg['source'], env={**os.environ, 'HERMES_HOME': cfg['base_home'],
                                 'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1'},
                                 stdout=out, stderr=err, timeout=600,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    output = read_json(runtime / 'last-task.json')
    if process.returncode or output.get('errors') != []:
        raise RuntimeError(f'Collector validation failed (exit {process.returncode}); inspect private runtime logs')
    return output


def status(*, home=None, schedule=True):
    home = Path(home or default_home()).resolve()
    runtime = home / 'archive-obsidian'
    cfg = read_json(runtime / 'config.json')
    if not cfg:
        return {'status': 'not-installed'}
    result = {'status': 'installed', 'config_path': str(runtime / 'config.json'),
              'config': {key: cfg.get(key) for key in ('base_home', 'vault_path', 'archive_folder',
                         'archive_root', 'runtime', 'source', 'python_executable')}}
    output = read_json(runtime / 'last-task.json')
    result['last_collection_errors'] = len(output.get('errors', [])) if output else None
    if schedule:
        result['scheduler'] = scheduled('Status', Path(cfg['source']), runtime / 'config.json')
    return result


def log_failure(home, operation, exc):
    # Trace locations and exception types, never configuration, raw output or exception messages.
    runtime = Path(home) / 'archive-obsidian'
    if not runtime.resolve().is_relative_to(Path(home).resolve()):
        raise OSError('Private diagnostic destination escapes Hermes home')
    runtime.mkdir(parents=True, exist_ok=True)
    path = runtime / 'setup-errors.jsonl'
    frames = [{'file': frame.filename, 'line': frame.lineno, 'function': frame.name}
              for frame in traceback.extract_tb(exc.__traceback__)]
    causes = []
    current = exc
    while current is not None and type(current).__name__ not in causes:
        causes.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    record = {'timestamp': datetime.now(timezone.utc).isoformat(), 'operation': operation,
              'cause_types': causes, 'stack': frames, 'cli': getattr(exc, 'cli_context', None)}
    if path.exists() and path.stat().st_size > 1024 * 1024:
        os.replace(path, path.with_suffix('.previous.jsonl'))
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + '\n')
    return str(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', type=Path)
    parser.add_argument('--folder')
    parser.add_argument('--home', type=Path)
    parser.add_argument('--replace-managed', action='store_true')
    parser.add_argument('--no-schedule', action='store_true')
    parser.add_argument('--no-collect', action='store_true')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--status', action='store_true')
    modes.add_argument('--uninstall', action='store_true')
    args = parser.parse_args(argv)
    if not args.status and not args.uninstall and (not args.vault or not args.folder):
        parser.error('--vault and --folder are required for setup')
    home = args.home or default_home()
    operation = 'status' if args.status else 'uninstall' if args.uninstall else 'setup'
    try:
        if args.status:
            result = status(home=home, schedule=not args.no_schedule)
        elif args.uninstall:
            result = uninstall(home=home, schedule=not args.no_schedule)
        else:
            result = setup(args.vault, args.folder, home=home, replace_managed=args.replace_managed,
                           schedule=not args.no_schedule, collect=not args.no_collect)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        try:
            log = log_failure(home, operation, exc)
        except OSError:
            log = None
        print(json.dumps({'status': 'failed', 'operation': operation, 'error_type': type(exc).__name__,
                          'log': log, 'scheduler_note': 'Owned task remains paused if setup failed after quiescence.'}), file=sys.stderr)
        return 1


def uninstall(*, home=None, source=None, cli=None, schedule=True):
    home = Path(home or default_home()).resolve()
    source = Path(source or Path(__file__).resolve().parent).resolve()
    runtime = home / 'archive-obsidian'
    config_path = runtime / 'config.json'
    if not config_path.is_file():
        config_path = source / 'archive-config.json'
    cfg = read_json(config_path)
    if not cfg:
        return {'status': 'not-installed'}
    if Path(cfg['base_home']).resolve() != home or not runtime.resolve().is_relative_to(home):
        raise ValueError('Uninstall configuration does not match this Hermes home')
    boot = read_json(runtime / 'state.json').get('bootstrap', {})
    collector = load_collector(source)
    # Preflight every profile before removing the task or mutating any configuration.
    for profile in boot:
        profile_home = home if profile == 'default' else home / 'profiles' / profile
        collector.assert_managed_paths(home, [profile_home])
        collector.assert_managed_paths(profile_home, [profile_home / 'config.yaml',
                                      profile_home / 'plugins' / PLUGIN,
                                      profile_home / 'skills/note-taking/hermes-archive-records/SKILL.md'])
    cli = cli or cli_runner(launcher())
    if schedule:
        scheduled('Remove', source, config_path)
    for profile, record in boot.items():
        profile_home = home if profile == 'default' else home / 'profiles' / profile
        if not profile_home.is_dir():
            continue
        managed_plugin(profile_home / 'plugins' / PLUGIN)
        cli(profile_home, 'plugins', 'disable', PLUGIN)
        observed = json.loads(cli(profile_home, 'plugins', 'list', '--json'))
        if any(p['name'] == PLUGIN and p['status'] == 'enabled' for p in observed):
            raise RuntimeError('Plugin disable readback failed')
        old = record.get('previous_auto_prune')
        if old in ('true', 'false', 'unset') and cli(profile_home, 'config', 'get', 'sessions.auto_prune').strip().lower() == 'false':
            if old == 'unset':
                cli(profile_home, 'config', 'unset', 'sessions.auto_prune')
            else:
                cli(profile_home, 'config', 'set', 'sessions.auto_prune', old)
            if cli(profile_home, 'config', 'get', 'sessions.auto_prune').strip().lower() != old:
                raise RuntimeError('Retention restore readback failed')
    return {'status': 'disabled', 'preserved': ['Vault', 'code', 'runtime-state']}


if __name__ == '__main__':
    sys.exit(main())
