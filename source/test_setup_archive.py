"""Installer checks: temporary homes only; never invoke live CLI/scheduler."""
import importlib.util
import json
import os
import sys
from pathlib import Path
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parent


class InstallerTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ['LOCALAPPDATA']) / 'hermes/cache/scratch'
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'home'
        self.vault = self.root / 'vault'
        self.bundle = self.root / 'bundle'
        self.home.mkdir()
        self.vault.mkdir()
        self.bundle.mkdir()
        self.calls = []
        self.values = {}
        self.statuses = {}
        self.home.joinpath('config.yaml').write_text('fake: true\n')
        self.vault.joinpath('human.md').write_bytes(b'human-owned\n')
        path = SOURCE / 'setup_archive.py'
        self.assertTrue(path.is_file(), 'standalone installer does not exist')
        spec = importlib.util.spec_from_file_location('setup_archive', path)
        assert spec is not None and spec.loader is not None
        self.installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.installer)
        for name in self.installer.PAYLOAD:
            target = self.bundle / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('name: hermes-archive-context\n' if name.endswith('plugin.yaml') else '# managed ' + name + '\n')
        loader = self.installer.load_collector
        def fake_native_loader(source):
            collector = loader(source)
            # Native exporter discovery is the only substituted collector seam.
            # Ownership validation, lock, mirror writes and readbacks remain real.
            collector.native_helpers = lambda cfg: (lambda value, **kwargs: value, lambda *args, **kwargs: '')
            return collector
        setattr(self.installer, 'load_collector', fake_native_loader)
        self.bundle.joinpath('archive.py').write_bytes(SOURCE.joinpath('archive.py').read_bytes())
        self.bundle.joinpath('archive-config.json').write_text('{"token":"MUST_NOT_COPY"}')
        self.bundle.joinpath('.env').write_text('SECRET=MUST_NOT_COPY')

    def cli(self, home, *args):
        self.calls.append((str(home), args))
        key = str(home)
        values = self.values.setdefault(key, {'plugins.disabled': '[]', 'sessions.auto_prune': 'true'})
        if args[:2] == ('config', 'get'):
            return values[args[2]]
        if args[:2] == ('config', 'set'):
            values[args[2]] = args[3]
            return ''
        if args[:2] in [('plugins', 'enable'), ('plugins', 'disable')]:
            self.statuses[key] = 'enabled' if args[1] == 'enable' else 'disabled'
            return ''
        if args[:2] == ('plugins', 'list'):
            return json.dumps([{'name': 'hermes-archive-context', 'status': self.statuses.get(key, 'disabled')}])
        raise AssertionError(args)

    def install(self, **kwargs):
        return self.installer.setup(self.vault, 'Notes/Archive', home=self.home,
                                    source=self.bundle, cli=self.cli, schedule=False,
                                    collect=False, **kwargs)

    def test_install_is_portable_idempotent_and_never_copies_private_config(self):
        cfg = self.install()
        target = self.home / 'plugins/hermes-archive-context'
        self.assertEqual(cfg['archive_root'], str((self.vault / 'Notes/Archive').resolve()))
        self.assertEqual(cfg['source'], str(target.resolve()))
        self.assertTrue(cfg['bootstrap_profiles'])
        for name in self.installer.PAYLOAD:
            self.assertEqual((target / name).read_bytes(), (self.bundle / name).read_bytes())
        self.assertFalse((target / 'archive-config.json').exists())
        self.assertFalse((target / '.env').exists())
        self.assertEqual(self.vault.joinpath('human.md').read_bytes(), b'human-owned\n')
        self.assertEqual(self.install(), cfg)
        self.assertEqual(json.loads((self.home / 'archive-obsidian/config.json').read_text()), cfg)

    def test_invalid_paths_fail_before_any_home_or_vault_write(self):
        for folder in ['', '.', '..', 'a/../b', '/absolute', 'C:\\absolute', '.obsidian/archive', 'x\n# injected', 'x[[link]]', 'x`code', 'x|table', 'a//b']:
            with self.subTest(folder=folder), self.assertRaises(ValueError):
                self.installer.setup(self.vault, folder, home=self.home, source=self.bundle,
                                     cli=self.cli, schedule=False, collect=False)
        missing = self.root / 'missing'
        with self.assertRaises(ValueError):
            self.installer.setup(missing, 'Archive', home=self.home, source=self.bundle,
                                 cli=self.cli, schedule=False, collect=False)
        with self.assertRaises(ValueError):
            self.installer.setup(self.home, 'Archive', home=self.home, source=self.bundle,
                                 cli=self.cli, schedule=False, collect=False)
        self.assertFalse((self.home / 'plugins').exists())
        self.assertFalse(missing.exists())
        self.assertEqual(self.calls, [])

    def test_reinstall_refuses_vault_or_folder_migration(self):
        self.install()
        with self.assertRaises(ValueError):
            self.installer.setup(self.vault, 'Different', home=self.home, source=self.bundle,
                                 cli=self.cli, schedule=False, collect=False)

    def test_legacy_configuration_refuses_unrequested_migration(self):
        self.bundle.joinpath('archive-config.json').write_text(json.dumps({'archive_root': str(self.vault / 'Other')}))
        with self.assertRaises(ValueError):
            self.install()
        self.assertFalse((self.home / 'plugins').exists())

    def test_symlink_escape_is_rejected_when_supported(self):
        try:
            (self.vault / 'escape').symlink_to(self.home, target_is_directory=True)
        except OSError:
            self.skipTest('Windows symlink privilege unavailable')
        with self.assertRaises(ValueError):
            self.installer.setup(self.vault, 'escape/Archive', home=self.home,
                                 source=self.bundle, cli=self.cli, schedule=False, collect=False)

    def test_modified_managed_files_require_explicit_replacement_and_backup(self):
        self.install()
        target = self.home / 'plugins/hermes-archive-context/archive.py'
        skill = self.home / 'skills/note-taking/hermes-archive-records/SKILL.md'
        target.write_bytes(b'user-maintained collector')
        skill.write_bytes(b'user-maintained skill')
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertEqual(target.read_bytes(), b'user-maintained collector')
        self.assertEqual(skill.read_bytes(), b'user-maintained skill')
        self.install(replace_managed=True)
        backups = list((self.home / 'archive-obsidian/backups').rglob('*'))
        contents = [p.read_bytes() for p in backups if p.is_file()]
        self.assertIn(b'user-maintained collector', contents)
        self.assertIn(b'user-maintained skill', contents)
        self.assertEqual(skill.read_bytes(), (self.bundle / 'archive-profile/SKILL.md').read_bytes())
        self.install()  # refreshed ownership hashes make subsequent setup safe

    def test_unrelated_plugin_collision_refused_even_with_replace(self):
        target = self.home / 'plugins/hermes-archive-context'
        target.mkdir(parents=True)
        target.joinpath('plugin.yaml').write_text('name: unrelated-plugin\n')
        with self.assertRaises(RuntimeError):
            self.install(replace_managed=True)
        self.assertFalse((target / 'archive.py').exists())

    def test_payload_update_preserves_provenance_and_local_settings(self):
        self.install()
        target = self.home / 'plugins/hermes-archive-context'
        target.joinpath('.git').mkdir()
        target.joinpath('.git/HEAD').write_bytes(b'provenance')
        target.joinpath('local-settings.json').write_bytes(b'private settings')
        updated = self.bundle.joinpath('archive.py').read_bytes() + b'\n# new release\n'
        self.bundle.joinpath('archive.py').write_bytes(updated)
        self.install()
        self.assertEqual(target.joinpath('archive.py').read_bytes(), updated)
        self.assertEqual(target.joinpath('.git/HEAD').read_bytes(), b'provenance')
        self.assertEqual(target.joinpath('local-settings.json').read_bytes(), b'private settings')

    def test_missing_payload_preflight_writes_nothing(self):
        self.bundle.joinpath('archive-profile/SKILL.md').unlink()
        with self.assertRaises(FileNotFoundError):
            self.install()
        self.assertFalse((self.home / 'plugins').exists())

    def test_uninstall_preserves_every_file_and_restores_only_owned_retention(self):
        self.install()
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.installer.uninstall(home=self.home, cli=self.cli, schedule=False)
        for path, data in before.items():
            self.assertEqual((self.root / path).read_bytes(), data)
        self.assertEqual(self.values[str(self.home)]['sessions.auto_prune'], 'true')
        self.assertEqual(self.statuses[str(self.home)], 'disabled')

    @unittest.skipUnless(os.name == 'nt', 'Windows junction regression')
    def test_uninstall_rejects_owned_paths_replaced_by_junctions_before_any_mutation(self):
        import shutil
        import subprocess
        from unittest.mock import patch
        profile = self.home / 'profiles/work'
        profile.mkdir(parents=True)
        profile.joinpath('config.yaml').write_text('fake: true\n')
        self.install()
        for relative in ('.', 'plugins', 'plugins/hermes-archive-context',
                         'skills', 'skills/note-taking/hermes-archive-records'):
            with self.subTest(relative=relative):
                linked = profile / relative
                outside = self.root / 'outside'
                shutil.move(str(linked), str(outside))
                try:
                    process = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(linked), str(outside)],
                                             capture_output=True)
                    self.assertEqual(process.returncode, 0, process.stderr)
                    self.assertTrue(linked.is_junction())
                    before = {p.relative_to(outside): p.read_bytes() for p in outside.rglob('*') if p.is_file()}
                    vault_before = {p.relative_to(self.vault): p.read_bytes() for p in self.vault.rglob('*') if p.is_file()}
                    self.calls.clear()
                    rejected = None
                    with patch.object(self.installer, 'scheduled') as scheduler:
                        try:
                            self.installer.uninstall(home=self.home, source=self.bundle, cli=self.cli)
                        except ValueError as exc:
                            rejected = exc
                    self.assertEqual(self.calls, [], 'Unsafe uninstall invoked CLI before preflight')
                    scheduler.assert_not_called()
                    self.assertEqual(before, {p.relative_to(outside): p.read_bytes() for p in outside.rglob('*') if p.is_file()})
                    self.assertEqual(vault_before, {p.relative_to(self.vault): p.read_bytes() for p in self.vault.rglob('*') if p.is_file()})
                    self.assertIsNotNone(rejected, 'Unsafe uninstall was not rejected')
                finally:
                    if linked.is_junction():
                        linked.rmdir()  # remove only the junction, never the external target
                    shutil.move(str(outside), str(linked))

    def test_explicit_disabled_plugin_remains_disabled_and_changed_retention_is_preserved(self):
        self.values[str(self.home)] = {'plugins.disabled': '["hermes-archive-context"]', 'sessions.auto_prune': 'true'}
        self.install()
        self.assertFalse(any(args[:2] == ('plugins', 'enable') for _, args in self.calls))
        self.values[str(self.home)]['sessions.auto_prune'] = 'true'
        count = len(self.calls)
        self.installer.uninstall(home=self.home, cli=self.cli, schedule=False)
        self.assertFalse(any(args[:3] == ('config', 'set', 'sessions.auto_prune') for _, args in self.calls[count:]))

    def test_standard_profiles_are_provisioned_without_copying_collector(self):
        profile = self.home / 'profiles/work'
        profile.mkdir(parents=True)
        profile.joinpath('config.yaml').write_text('fake: true\n')
        self.install()
        plugin = profile / 'plugins/hermes-archive-context'
        self.assertTrue(plugin.joinpath('SKILL.md').is_file())
        self.assertTrue(plugin.joinpath('archive-settings.json').is_file())
        self.assertFalse(plugin.joinpath('archive.py').exists())
        self.assertEqual(json.loads(plugin.joinpath('archive-settings.json').read_text())['archive_folder'], 'Notes/Archive')

    def test_scheduled_setup_quiesces_before_writes_and_registers_after(self):
        from unittest.mock import patch
        events = []
        def scheduler(mode, source, config, **kwargs):
            events.append(mode)
            if mode == 'Pause':
                self.assertFalse((self.home / 'plugins').exists())
                return {'Exists': True, 'Enabled': True}
            if mode == 'Register':
                self.assertTrue((self.home / 'archive-obsidian/config.json').is_file())
            return {}
        with patch.object(self.installer, 'scheduled', side_effect=scheduler):
            self.installer.setup(self.vault, 'Notes/Archive', home=self.home,
                                 source=self.bundle, cli=self.cli, collect=False)
        self.assertEqual(events, ['Pause', 'Register'])

    def test_scheduler_failure_prevents_payload_write(self):
        from unittest.mock import patch
        with patch.object(self.installer, 'scheduled', side_effect=RuntimeError('foreign task')):
            with self.assertRaises(RuntimeError):
                self.installer.setup(self.vault, 'Notes/Archive', home=self.home,
                                     source=self.bundle, cli=self.cli, collect=False)
        self.assertFalse((self.home / 'plugins').exists())

    def test_collect_uses_installed_payload_and_runtime_config(self):
        from unittest.mock import patch
        with patch.object(self.installer, 'collect_once') as collect:
            cfg = self.installer.setup(self.vault, 'Notes/Archive', home=self.home,
                                       source=self.bundle, cli=self.cli, schedule=False)
        collect.assert_called_once_with(cfg)

    def test_command_line_requires_only_vault_and_folder(self):
        from unittest.mock import patch
        from contextlib import redirect_stderr, redirect_stdout
        import io
        with redirect_stdout(io.StringIO()), patch.object(self.installer, 'setup', return_value={'status': 'ok'}) as setup:
            self.assertEqual(self.installer.main(['--vault', str(self.vault), '--folder', 'Notes/Archive', '--home', str(self.home), '--no-schedule', '--no-collect']), 0)
        self.assertFalse(setup.call_args.kwargs['schedule'])
        self.assertFalse(setup.call_args.kwargs['collect'])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.installer.main(['--vault', str(self.vault)])

    def test_no_collect_setup_seeds_portable_mirror_from_runtime_state(self):
        self.install()
        mirror = self.vault / 'Notes/Archive/60_Operations/.archive-state.json'
        self.assertTrue(mirror.is_file())
        data = json.loads(mirror.read_text())
        self.assertEqual(data['owner'], 'hermes-archive-context')
        self.assertEqual(data['files'], {})
        self.assertNotIn('bootstrap', data)
        self.assertNotIn(str(self.home), mirror.read_text())

    def test_new_install_state_is_compatible_and_reconnects_vault_mirror(self):
        import hashlib
        mirror = self.vault / 'Notes/Archive/60_Operations/.archive-state.json'
        mirror.parent.mkdir(parents=True)
        portable = {'owner': 'hermes-archive-context', 'version': '1', 'files': {'human.md': hashlib.sha256(b'human-owned\n').hexdigest()},
                    'sessions': {}, 'projects': {}, 'groups': {}}
        mirror.write_text(json.dumps(portable))
        before = mirror.read_bytes()
        cfg = self.install()
        state = json.loads((self.home / 'archive-obsidian/state.json').read_text())
        self.assertEqual(state['version'], '1')
        self.assertEqual(state['files'], portable['files'])
        self.assertEqual(state['sessions'], {})
        self.assertEqual(state['archive_root'], cfg['archive_root'])
        self.assertEqual(json.loads(mirror.read_text()), json.loads(before))

    def test_corrupt_mirror_and_runtime_state_are_preserved_before_payload_write(self):
        mirror = self.vault / 'Notes/Archive/60_Operations/.archive-state.json'
        mirror.parent.mkdir(parents=True)
        mirror.write_text('{"owner":"foreign"}')
        with self.assertRaises((ValueError, RuntimeError)):
            self.install()
        self.assertFalse((self.home / 'plugins').exists())
        self.assertEqual(mirror.read_text(), '{"owner":"foreign"}')

    def test_atomic_temporary_link_cannot_redirect_a_write(self):
        target = self.home / 'safe.json'
        outside = self.vault / 'human.md'
        temporary = target.with_name(target.name + '.setup-tmp')
        from unittest.mock import patch
        with patch.object(Path, 'is_symlink', return_value=True), self.assertRaises(ValueError):
            self.installer.atomic_bytes(target, b'overwritten')
        self.assertEqual(outside.read_bytes(), b'human-owned\n')

    def test_install_rejects_directory_junction_outside_home(self):
        import subprocess
        plugin = self.home / 'plugins'
        process = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(plugin), str(self.vault)], capture_output=True)
        if process.returncode:
            self.skipTest('Windows junction unavailable')
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertFalse(self.vault.joinpath('hermes-archive-context/archive.py').exists())
        plugin.rmdir()  # remove the test junction, never its target

    def test_private_failure_log_contains_locations_not_exception_messages(self):
        try:
            raise RuntimeError('password=NEVER_LOG_THIS')
        except RuntimeError as exc:
            path = Path(self.installer.log_failure(self.home, 'setup', exc))
        data = path.read_text()
        self.assertNotIn('NEVER_LOG_THIS', data)
        record = json.loads(data)
        self.assertEqual(record['operation'], 'setup')
        self.assertTrue(record['timestamp'])
        self.assertTrue(record['stack'])

    def test_status_reads_runtime_config_without_exposing_private_output(self):
        self.install()
        runtime = self.home / 'archive-obsidian'
        runtime.joinpath('last-task.json').write_text('{"errors":["password=NEVER_PRINT_THIS"],"secret":"private"}')
        report = self.installer.status(home=self.home, schedule=False)
        self.assertEqual(report['last_collection_errors'], 1)
        self.assertNotIn('NEVER_PRINT_THIS', json.dumps(report))
        self.assertEqual(report['config_path'], str(runtime / 'config.json'))

    def test_legacy_uninstall_uses_source_config_and_preserves_notes(self):
        cfg = self.install()
        (self.home / 'archive-obsidian/config.json').unlink()
        self.bundle.joinpath('archive-config.json').write_text(json.dumps(cfg))
        report = self.installer.uninstall(home=self.home, source=self.bundle, cli=self.cli, schedule=False)
        self.assertEqual(report['status'], 'disabled')
        self.assertEqual(self.statuses[str(self.home)], 'disabled')
        self.assertEqual(self.vault.joinpath('human.md').read_bytes(), b'human-owned\n')

    def test_runtime_junction_is_rejected_before_writes(self):
        import subprocess
        runtime = self.home / 'archive-obsidian'
        process = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(runtime), str(self.vault)], capture_output=True)
        if process.returncode:
            self.skipTest('Windows junction unavailable')
        try:
            with self.assertRaises(ValueError):
                self.install()
            self.assertFalse((self.home / 'plugins').exists())
            self.assertFalse(self.vault.joinpath('config.json').exists())
        finally:
            runtime.rmdir()

    def test_missing_hermes_configuration_prevents_partial_install(self):
        self.home.joinpath('config.yaml').unlink()
        with self.assertRaises(FileNotFoundError):
            self.install()
        self.assertFalse((self.home / 'plugins').exists())

    def test_reinstall_after_uninstall_reestablishes_source_retention(self):
        self.install()
        self.installer.uninstall(home=self.home, cli=self.cli, schedule=False)
        self.assertEqual(self.values[str(self.home)]['sessions.auto_prune'], 'true')
        self.install()
        self.assertEqual(self.values[str(self.home)]['sessions.auto_prune'], 'false')

    def test_standalone_cli_install_in_fake_home_with_real_hermes(self):
        import subprocess
        for name in self.installer.PAYLOAD:
            self.bundle.joinpath(name).write_bytes(SOURCE.joinpath(name).read_bytes())
        command = [sys.executable, str(self.bundle / 'setup_archive.py'),
                   '--vault', str(self.vault), '--folder', 'Notes/Archive', '--home', str(self.home),
                   '--no-schedule', '--no-collect']
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=180)
        diagnostics = self.home / 'archive-obsidian/setup-errors.jsonl'
        self.assertEqual(result.returncode, 0, result.stderr + (diagnostics.read_text() if diagnostics.exists() else ''))
        cfg = json.loads(result.stdout)
        self.assertEqual(cfg['base_home'], str(self.home))
        self.assertTrue((self.vault / 'Notes/Archive/60_Operations/.archive-state.json').is_file())
        self.assertEqual(self.vault.joinpath('human.md').read_bytes(), b'human-owned\n')

    def test_launcher_prefers_verified_native_executable_over_cmd(self):
        from unittest.mock import patch
        import subprocess
        cmd = self.root / 'hermes.cmd'
        exe = self.root / 'hermes.exe'
        cmd.write_text('fake')
        exe.write_text('fake')
        with patch.object(self.installer.shutil, 'which', return_value=str(cmd)), \
                patch.object(self.installer.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'Hermes', '')) as run:
            self.assertEqual(self.installer.launcher(), str(exe.resolve()))
        self.assertEqual(run.call_args.args[0][0], str(exe.resolve()))
        self.assertNotIn('shell', run.call_args.kwargs)


if __name__ == '__main__':
    unittest.main()
