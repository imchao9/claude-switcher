"""Cleanup uses a synthetic HOME, fake process list, and a fixture Applications directory."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'core/claude-switch.sh'


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.base = self.home / '.claude-switcher'
        self.desktop = self.home / 'Library/Application Support/Claude'
        self.apps = self.root / 'Applications'
        self.fakebin = self.root / 'bin'
        self.fakebin.mkdir()
        ps = self.fakebin / 'ps'
        ps.write_text('#!/bin/sh\ncat "$PROCESS_FIXTURE"\n')
        ps.chmod(0o755)
        self.process_file = self.root / 'processes'
        self.process_file.write_text('')
        self.script = self.root / 'core.sh'
        self.script.write_text(SOURCE.read_text().replace(
            'Path("/Applications/Claude.app")', 'Path(' + repr(str(self.apps / 'Claude.app')) + ')'))
        self.env = {'HOME': str(self.home), 'PATH': f'{self.fakebin}:/usr/bin:/bin',
                    'CLAUDE_SWITCHER_PYTHON': sys.executable, 'PROCESS_FIXTURE': str(self.process_file)}

    def write(self, path, text='private-session-secret'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_core(self, *args):
        result = subprocess.run(['/bin/bash', str(self.script), *args], env=self.env,
                                capture_output=True, text=True, timeout=15)
        self.assertNotIn('private-session-secret', result.stdout + result.stderr)
        return result

    def plan(self):
        result = self.run_core('cleanup-info')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def clean(self, scopes, token=None, phrase='移入废纸篓'):
        return self.run_core('cleanup-data', scopes, token or self.plan()['token'], phrase)

    def test_preview_readonly_and_no_contents(self):
        self.write(self.desktop / 'session')
        plan = self.plan()
        self.assertFalse(self.base.exists())
        self.assertFalse((self.home / '.Trash').exists())
        row = next(i for i in plan['items'] if i['path'].endswith('Application Support/Claude'))
        self.assertEqual(row['state'], 'present')
        self.assertGreater(row['bytes'], 0)
        self.assertEqual(self.plan()['token'], plan['token'])

    def test_desktop_preserves_archives_and_code_and_recovers_data(self):
        self.write(self.desktop / 'session')
        self.write(self.base / 'accounts/A/session', 'archive')
        self.write(self.base / 'current', 'A')
        self.write(self.home / '.claude/history', 'code')
        result = self.clean('desktop-data')
        self.assertEqual(result.returncode, 0, result.stdout)
        report = json.loads(result.stdout)
        self.assertTrue(report['complete'])
        self.assertFalse(self.desktop.exists())
        self.assertFalse((self.base / 'current').exists())
        self.assertEqual((self.base / 'accounts/A/session').read_text(), 'archive')
        self.assertEqual((self.home / '.claude/history').read_text(), 'code')
        trash = self.home / report['recovery'].removeprefix('~/')
        mapping = json.loads((trash / 'restore-map.json').read_text())
        data = next(i for i in mapping['items'] if i['original'].endswith('Application Support/Claude'))
        self.assertEqual((trash / data['stored'] / 'session').read_text(), 'private-session-secret')
        self.assertEqual(trash.stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.base / 'cli-operation.lock').exists())

    def test_all_cli_slots_and_json_are_moved(self):
        paths = [self.home / '.claude/config', self.home / '.claude.json', self.home / '.claude.json.backup']
        paths += [self.base / f'cli-accounts/{slot}/history' for slot in 'BCD']
        for path in paths:
            self.write(path)
        self.assertEqual(self.clean('code-data').returncode, 0)
        for path in paths:
            self.assertFalse(path.exists())

    def test_wrong_or_missing_confirmation_never_moves(self):
        self.write(self.desktop / 'session')
        for phrase in ('', 'delete', '移入废纸篓 '):
            self.assertNotEqual(self.clean('desktop-data', phrase=phrase).returncode, 0)
        self.assertNotEqual(self.run_core('cleanup-data').returncode, 0)
        self.assertTrue(self.desktop.exists())
        self.assertFalse(self.base.exists())

    def test_invalid_scope_rejected(self):
        self.write(self.desktop / 'session')
        for scope in ('all', '../', '', 'desktop-data,desktop-data', 'desktop-data,unknown'):
            self.assertNotEqual(self.clean(scope).returncode, 0)
        self.assertTrue(self.desktop.exists())

    def test_changed_plan_rejected(self):
        self.write(self.desktop / 'session')
        token = self.plan()['token']
        self.write(self.desktop / 'new-file')
        result = self.clean('desktop-data', token)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('预览已变化', result.stdout)
        self.assertTrue(self.desktop.exists())

    def test_ancestor_link_blocks_entire_selection(self):
        outside = self.root / 'outside'
        outside.mkdir()
        self.write(outside / 'session')
        (self.home / 'Library').symlink_to(outside, target_is_directory=True)
        self.write(self.home / '.claude/history')
        result = self.clean('desktop-data,code-data')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((outside / 'session').exists())
        self.assertTrue((self.home / '.claude/history').exists())

    def test_leaf_link_moves_only_link(self):
        outside = self.root / 'outside'
        self.write(outside / 'session')
        (self.home / '.claude').symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.clean('code-data').returncode, 0)
        self.assertFalse((self.home / '.claude').is_symlink())
        self.assertEqual((outside / 'session').read_text(), 'private-session-secret')

    def test_trash_link_refused(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.home / '.Trash').symlink_to(outside)
        self.write(self.desktop / 'session')
        self.assertNotEqual(self.clean('desktop-data').returncode, 0)
        self.assertTrue(self.desktop.exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_base_link_refused(self):
        outside = self.root / 'outside'
        outside.mkdir()
        self.base.symlink_to(outside)
        self.write(self.desktop / 'session')
        self.assertNotEqual(self.clean('desktop-data').returncode, 0)
        self.assertTrue(self.desktop.exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_existing_switch_lock_blocks_cleanup(self):
        self.write(self.desktop / 'session')
        (self.base / 'cli-operation.lock').mkdir(parents=True)
        self.assertNotEqual(self.clean('desktop-data').returncode, 0)
        self.assertTrue(self.desktop.exists())
        self.assertTrue((self.base / 'cli-operation.lock').is_dir())

    def test_pending_recovery_blocks_cleanup(self):
        for relative in ('accounts/A.new', 'accounts/B.previous', 'keep-stage'):
            with self.subTest(relative=relative):
                path = self.base / relative
                self.write(path)
                self.write(self.desktop / 'session')
                result = self.clean('desktop-data,archives')
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(path.exists())
                self.assertTrue(self.desktop.exists())
                path.unlink()

    def test_running_desktop_and_cli_block_without_killing(self):
        self.write(self.desktop / 'session')
        for command in ('/Applications/Claude.app/Contents/MacOS/Claude', '/usr/bin/claude',
                        '/usr/bin/node /x/@anthropic-ai/claude-code/cli.js',
                        '/usr/bin/bun /x/@anthropic-ai/claude-code/cli.js'):
            with self.subTest(command=command):
                self.process_file.write_text(f'123456 {os.getuid()} {command.split()[0]} {command}\n')
                result = self.clean('desktop-data')
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertTrue(self.desktop.exists())
                self.assertFalse((self.home / '.Trash').exists())

    def test_versioned_native_process_blocks(self):
        self.write(self.home / '.claude/history')
        binary = self.home / '.local/share/claude/versions/2.0.0'
        self.process_file.write_text(f'123456 {os.getuid()} {binary} {binary} --resume\n')
        self.assertNotEqual(self.clean('code-data').returncode, 0)
        self.assertTrue((self.home / '.claude/history').exists())

    def test_nested_same_size_changes_invalidate_preview(self):
        session = self.home / '.claude/projects/project/session.jsonl'
        self.write(session, 'before')
        token = self.plan()['token']
        self.write(session, 'after!')
        self.assertNotEqual(self.clean('code-data', token).returncode, 0)
        self.assertTrue(session.exists())
        self.assertFalse((self.home / '.Trash').exists())

    def test_process_probe_error_blocks(self):
        (self.fakebin / 'ps').write_text('#!/bin/sh\nexit 1\n')
        self.write(self.desktop / 'session')
        self.assertNotEqual(self.clean('desktop-data').returncode, 0)
        self.assertTrue(self.desktop.exists())

    def test_apps_and_native_code_moved_without_other_scopes(self):
        paths = [self.apps / 'Claude.app/Contents/app', self.home / 'Applications/Claude.app/app',
                 self.home / '.local/share/claude/versions/1.0']
        for path in paths:
            self.write(path)
        launcher = self.home / '.local/bin/claude'
        launcher.parent.mkdir(parents=True)
        launcher.symlink_to(paths[-1])
        self.write(self.desktop / 'session')
        result = self.clean('desktop-app,code-app')
        self.assertEqual(result.returncode, 0, result.stdout)
        for path in paths:
            self.assertFalse(path.exists())
        self.assertFalse(launcher.is_symlink())
        self.assertTrue(self.desktop.exists())

    def test_explicit_archive_cleanup_preserves_directory_structure_in_trash(self):
        self.write(self.base / 'accounts/A/session')
        self.assertEqual(self.clean('archives').returncode, 0)
        # Find via mapping rather than rely on target ordering.
        maps = list((self.home / '.Trash').glob('*/restore-map.json'))
        mapping = json.loads(maps[0].read_text())
        item = next(i for i in mapping['items'] if i['original'].endswith('/accounts'))
        self.assertTrue((maps[0].parent / item['stored'] / 'A/session').exists())

    def test_mid_operation_failure_keeps_recovery_map_and_reports_partial(self):
        self.write(self.desktop / 'session')
        self.write(self.home / 'Library/Caches/Claude/cache')
        self.script.write_text(self.script.read_text().replace(
            'os.rename(path, destination / row["id"])',
            'os.rename(path, destination / row["id"]) if row["id"] == "0" else (_ for _ in ()).throw(PermissionError())'))
        result = self.clean('desktop-data')
        self.assertNotEqual(result.returncode, 0)
        report = json.loads(result.stdout)
        self.assertFalse(report['complete'])
        self.assertEqual([i['state'] for i in report['results']], ['ok', 'fail'])
        self.assertFalse(self.desktop.exists())
        self.assertTrue((self.home / 'Library/Caches/Claude/cache').exists())
        self.assertTrue(list((self.home / '.Trash').glob('*/restore-map.json')))

    def test_missing_targets_report_skip(self):
        result = self.clean('code-data')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(all(i['state'] == 'skip' for i in json.loads(result.stdout)['results']))

    def test_project_data_and_unrelated_apps_preserved(self):
        project = self.home / 'project/.claude/settings.json'
        other = self.home / 'Library/Application Support/Other/session'
        self.write(project)
        self.write(other)
        self.write(self.home / '.claude/config')
        self.assertEqual(self.clean('code-data,desktop-data').returncode, 0)
        self.assertTrue(project.exists())
        self.assertTrue(other.exists())


if __name__ == '__main__':
    unittest.main()
