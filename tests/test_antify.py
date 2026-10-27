import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load('antify_installer', 'installer/install-antify.py')
code = load('antify_code', 'core/claude-code.py')


class AntifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.app = self.base / 'Antify.app'
        (self.app / 'Contents').mkdir(parents=True)
        self.info = {'CFBundleIdentifier': installer.BUNDLE_ID, 'CFBundleShortVersionString': '0.0.82'}
        self.write_info()
        self.calls = []

    def write_info(self):
        (self.app / 'Contents/Info.plist').write_bytes(plistlib.dumps(self.info))

    def command(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == '/usr/bin/ditto':
            shutil.copytree(args[1], args[2], symlinks=True)
        stderr = b'TeamIdentifier=H6PU3TBL97\n'
        stdout = b''
        if 'systemextensionsctl' in args[0]:
            stdout = (b'* * H6PU3TBL97 com.lester.Antify.AntifyCore (0.0.82/82) Antify [activated enabled]\n'
                      b'H6PU3TBL97 com.lester.Antify.AntifyCore (0.0.77/77) Antify [terminated waiting to uninstall on reboot]\n')
        if kwargs.get('text'):
            stdout, stderr = stdout.decode(), stderr.decode()
        return subprocess.CompletedProcess(args, 0, stdout, stderr)

    def report(self, profile="default"):
        with patch.object(code, 'ANTIFY_APP', self.app), patch.object(code, 'find_cli', return_value='/fake/claude'), patch.object(code.subprocess, 'run', side_effect=self.command):
            return code.antify_report(profile)

    def test_report_does_not_approve_route_or_running_provider(self):
        report = self.report()
        rows = {row['id']: row for row in report['checks']}
        self.assertEqual(report['kind'], 'antify_diagnostics')
        self.assertIs(report['launch_supported'], False)
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows['antify_extension']['status'], 'ok')
        self.assertIn('不能证明', rows['antify_extension']['detail'])
        self.assertEqual(rows['antify_route']['status'], 'warn')
        self.assertEqual(rows['antify_protection']['status'], 'warn')
        self.assertNotIn(str(self.base), json.dumps(report))
        self.assertTrue(all(call[0] in ('/usr/bin/codesign', '/usr/bin/systemextensionsctl') for call in self.calls))

    def test_old_version_has_no_kill_switch(self):
        self.info['CFBundleShortVersionString'] = '0.0.77'
        self.write_info()
        rows = {row['id']: row for row in self.report()['checks']}
        self.assertEqual(rows['antify_install']['status'], 'fail')
        self.assertEqual(rows['antify_protection']['status'], 'fail')

    def test_extension_query_failure_is_not_absent_or_approved(self):
        with patch.object(code, 'ANTIFY_APP', self.app), patch.object(code.subprocess, 'run', side_effect=subprocess.TimeoutExpired('secret-token', 10)):
            report = code.antify_report('default')
        rows = {row['id']: row for row in report['checks']}
        self.assertEqual(rows['antify_extension']['status'], 'warn')
        self.assertNotIn('secret-token', json.dumps(report))

    def test_antify_run_refuses_before_cli_or_network(self):
        with patch.object(code, 'antify_report') as report, patch.object(code, 'check') as network, patch.object(code, 'profile_path') as profile:
            result = code.main(['unused', 'run', 'A', '--route', 'antify', '--', '--version'])
        self.assertEqual(result, 2)
        report.assert_not_called()
        network.assert_not_called()
        profile.assert_not_called()

    def test_antify_profile_permission_failure(self):
        home = self.base / 'home'
        path = home / '.claude-switcher/code/A'
        path.mkdir(parents=True)
        path.chmod(0o755)
        with patch.object(code.Path, 'home', return_value=home), patch.dict(os.environ, {}, clear=True):
            report = self.report('A')
        self.assertEqual(report['checks'][1]['status'], 'fail')

    def test_inactive_and_other_extensions_are_not_approved(self):
        for output in (
            '* * OTHERTEAM com.lester.Antify.AntifyCore (0.0.82/82) [activated enabled]',
            '* * H6PU3TBL97 com.lester.Antify.AntifyCoreOther (0.0.82/82) [activated enabled]',
            'H6PU3TBL97 com.lester.Antify.AntifyCore (0.0.77/77) [terminated waiting to uninstall on reboot]',
        ):
            def command(args, **kwargs):
                result = self.command(args, **kwargs)
                if 'systemextensionsctl' in args[0]: result.stdout = output
                return result
            with patch.object(code, 'ANTIFY_APP', self.app), patch.object(code.subprocess, 'run', side_effect=command):
                rows = {row['id']: row for row in code.antify_report('default')['checks']}
            self.assertEqual(rows['antify_extension']['status'], 'warn')

    def test_code_only_install_targets_are_separate_and_network_untouched(self):
        home = self.base / 'home'
        app, tools = self.base / 'new-app', self.base / 'new-tools'
        with patch.object(installer.platform, 'system', return_value='Darwin'), patch.object(installer.platform, 'mac_ver', return_value=('14.0', (), '')), patch.object(installer, 'needs_antify', return_value=False), patch.object(installer, 'build_code_app', return_value=(app, tools)), patch.object(installer, 'publish') as publish, patch.object(installer, 'run') as run:
            installer.install(ROOT, home)
        targets = [entry[1] for entry in publish.call_args.args[0]]
        self.assertEqual(targets, [home / '.claude-switcher/code-tools', home / 'Desktop' / installer.APP_NAME])
        self.assertEqual([call.args[0][0] for call in run.call_args_list], ['/usr/bin/open', '/usr/bin/open'])

    def test_verify_accepts_official_signature(self):
        with patch.object(installer.subprocess, 'run', side_effect=self.command):
            self.assertEqual(installer.verify_antify(self.app), (0, 0, 82))
        self.assertIn('/usr/sbin/spctl', [call[0] for call in self.calls])

    def test_wrong_signature_team_and_bundle_are_rejected(self):
        for kind in ('signature', 'team', 'bundle', 'gatekeeper'):
            with self.subTest(kind=kind):
                self.info['CFBundleIdentifier'] = 'wrong' if kind == 'bundle' else installer.BUNDLE_ID
                self.write_info()
                def command(args, **kwargs):
                    result = self.command(args, **kwargs)
                    if kind == 'signature' and '--verify' in args or kind == 'gatekeeper' and 'spctl' in args[0]:
                        result.returncode = 1
                    if kind == 'team': result.stderr = b'TeamIdentifier=OTHER\n'
                    return result
                with patch.object(installer.subprocess, 'run', side_effect=command), self.assertRaises(installer.InstallError):
                    installer.verify_antify(self.app)

    def test_verified_existing_version_is_preserved(self):
        with patch.object(installer.subprocess, 'run', side_effect=self.command):
            self.assertFalse(installer.needs_antify(self.app))
        self.assertFalse(any('pgrep' in call[0] for call in self.calls))

    def test_old_running_app_is_not_replaced(self):
        with patch.object(installer, 'verify_antify', return_value=(0, 0, 77)), patch.object(installer.subprocess, 'run', side_effect=self.command):
            with self.assertRaisesRegex(installer.InstallError, '正在运行'):
                installer.needs_antify(self.app)
        self.assertTrue(self.app.exists())

    def test_download_failure_does_not_mount_or_publish(self):
        with patch.object(installer.subprocess, 'run', return_value=subprocess.CompletedProcess([], 22, b'', b'secret')) as command:
            with self.assertRaisesRegex(installer.InstallError, '官方下载失败'):
                with installer.official_candidate(self.base):
                    self.fail('failed download yielded candidate')
        self.assertEqual(command.call_count, 1)
        self.assertIn(installer.DMG_URL, command.call_args.args[0])

    def test_mount_is_detached_after_verification_failure(self):
        def command(args, message, timeout=30):
            self.calls.append(args)
            if 'attach' in args:
                return subprocess.CompletedProcess(args, 0, plistlib.dumps({'system-entities': [{'mount-point': str(self.base)}]}), b'')
            return subprocess.CompletedProcess(args, 0, b'', b'')
        with patch.object(installer, 'run', side_effect=command), patch.object(installer, 'verify_antify', side_effect=installer.InstallError('signature')):
            with self.assertRaises(installer.InstallError):
                with installer.official_candidate(self.base): pass
        self.assertEqual(self.calls[-1][:2], ['/usr/bin/hdiutil', 'detach'])

    def test_publish_rolls_back_all_targets_after_postcopy_failure(self):
        sources, targets = [], []
        for index in range(2):
            source, target = self.base / f'source{index}', self.base / f'target{index}'
            source.mkdir(); target.mkdir()
            (source / 'value').write_text('new')
            (target / 'value').write_text('old')
            sources.append(source); targets.append(target)
        def verify(path):
            if path == targets[1]: raise installer.InstallError('postcopy failed')
        with patch.object(installer.subprocess, 'run', side_effect=self.command), self.assertRaisesRegex(installer.InstallError, 'postcopy failed'):
            installer.publish([(source, target, verify) for source, target in zip(sources, targets)])
        self.assertEqual([(target / 'value').read_text() for target in targets], ['old', 'old'])
        self.assertFalse(list(self.base.glob('.claude-code-install-*')))

    def signal_publish(self, first_signal, point):
        home = self.base / (str(first_signal) + point)
        home.mkdir()
        for index in range(2):
            for prefix, value in (('source', 'new'), ('target', 'old')):
                path = home / (prefix + str(index))
                path.mkdir()
                (path / 'value').write_text(value)
        script = r"""
import importlib.util, os, pathlib, shutil, signal, subprocess, sys, time
root, base, point, first = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
spec = importlib.util.spec_from_file_location('installer', root / 'installer/install-antify.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
previous = {sig: signal.getsignal(sig) for sig in m.CANCELLATION_SIGNALS}
original_rename, original_rmtree = pathlib.Path.rename, shutil.rmtree

def command(args, message, timeout=30):
    if args[0] != '/usr/bin/ditto': raise AssertionError('unexpected system command')
    shutil.copytree(args[1], args[2])
    return subprocess.CompletedProcess(args, 0, b'', b'')

def rename(path, target):
    result = original_rename(path, target)
    if point == 'backup' and path == base / 'target1' and pathlib.Path(target).name == 'backup':
        os.kill(os.getpid(), first)
    if point == 'candidate' and path.name == 'candidate' and pathlib.Path(target) == base / 'target1':
        os.kill(os.getpid(), first)
    return result

def remove(path, *args, **kwargs):
    if point == 'verify' and pathlib.Path(path) == base / 'target1':
        (base / 'rollback').touch()
        time.sleep(.5)
    return original_rmtree(path, *args, **kwargs)

def verify(path):
    if point == 'verify' and path == base / 'target1':
        (base / 'waiting').touch()
        time.sleep(20)

m.run = command
pathlib.Path.rename, shutil.rmtree = rename, remove
try:
    m.publish([(base / ('source' + str(i)), base / ('target' + str(i)), verify) for i in range(2)])
except m.InstallError as error:
    print(str(error), file=sys.stderr)
    assert all(signal.getsignal(sig) == previous[sig] for sig in previous)
    sys.exit(17)
raise AssertionError('cancellation did not abort')
"""
        process = subprocess.Popen([sys.executable, '-c', script, str(ROOT), str(home), point, str(first_signal)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            if point == 'verify':
                for marker in ('waiting', 'rollback'):
                    deadline = time.monotonic() + 5
                    while not (home / marker).exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(.01)
                    if process.poll() is not None:
                        output, error = process.communicate()
                        self.fail(marker + " missing: " + error.decode() + " exit=" + str(process.returncode))
                    self.assertTrue((home / marker).exists(), marker)
                    if marker == 'waiting':
                        process.send_signal(first_signal)
                process.send_signal(signal.SIGHUP if first_signal == signal.SIGTERM else signal.SIGTERM)
            output, error = process.communicate(timeout=8)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()
        self.assertEqual(process.returncode, 17, error.decode())
        self.assertIn('安装已中断', error.decode())
        self.assertEqual([(home / ('target' + str(i)) / 'value').read_text() for i in range(2)], ['old', 'old'])
        self.assertFalse(list(home.glob('.claude-code-install-*')))

    def test_termination_during_verification_rolls_back_despite_repeated_signal(self):
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig):
                self.signal_publish(sig, 'verify')

    def test_termination_at_rename_boundaries_preserves_backups(self):
        for sig in (signal.SIGTERM, signal.SIGHUP):
            for point in ('backup', 'candidate'):
                with self.subTest(signal=sig, point=point):
                    self.signal_publish(sig, point)

    def test_dry_run_does_not_run_commands_or_write_home(self):
        home = self.base / 'home'
        with patch.object(installer.platform, 'system', return_value='Darwin'), patch.object(installer.platform, 'mac_ver', return_value=('14.0', (), '')), patch.object(installer.subprocess, 'run') as command:
            installer.install(ROOT, home, dry_run=True)
        command.assert_not_called()
        self.assertFalse(home.exists())

    def test_code_only_dispatch_precedes_desktop_operations(self):
        result = subprocess.run(['/bin/bash', str(ROOT / 'installer/安装.command'), '--code-antify', '--dry-run'], capture_output=True, text=True, timeout=10)
        if sys.platform == 'darwin':
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('预演完成', result.stdout)
        self.assertNotIn('请选择', result.stdout)

    @unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'), 'requires macOS Swift compiler')
    def test_swift_antify_report_contract(self):
        source = (ROOT / 'app/main.swift').read_text()
        models = source[source.index('enum RowState:'):source.index('struct CheckRow:')]
        script = self.base / 'report.swift'
        script.write_text('import Foundation\n' + models + r'''
let items = AntifyReport.ids.map { ["id": $0, "status": $0 == "antify_route" || $0 == "antify_protection" ? "warn" : "ok", "detail": "checked"] }
let good: [String: Any] = ["schema_version": 1, "kind": "antify_diagnostics", "profile": "A", "launch_supported": false, "checks": items]
func decode(_ value: [String: Any], exit: Int32 = 0) -> AntifyReport? {
    let data = try! JSONSerialization.data(withJSONObject: value)
    return AntifyReport.decode(String(decoding: data, as: UTF8.self), profile: "A", exit: exit)
}
precondition(decode(good) != nil)
precondition(decode(good, exit: 2) == nil)
for (key, value) in [("schema_version", 2 as Any), ("kind", "wrong"), ("profile", "B"), ("launch_supported", true)] {
    var wrong = good; wrong[key] = value; precondition(decode(wrong) == nil)
}
var wrong = good
wrong["checks"] = Array(items.dropLast()); precondition(decode(wrong) == nil)
wrong["checks"] = items + [items[0]]; precondition(decode(wrong) == nil)
wrong["checks"] = items.map { item in var row = item; row["status"] = "ok"; return row }; precondition(decode(wrong) == nil)
print("Antify contract passed")
''')
        sdk = subprocess.check_output(['xcrun', '--sdk', 'macosx', '--show-sdk-path'], text=True).strip()
        binary = self.base / 'report'
        compiled = subprocess.run(['swiftc', '-sdk', sdk, str(script), '-o', str(binary)], capture_output=True, text=True, timeout=90)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        self.assertEqual(subprocess.run([str(binary)], capture_output=True, timeout=10).returncode, 0)


if __name__ == '__main__':
    unittest.main()
