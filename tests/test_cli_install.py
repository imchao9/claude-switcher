import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CURL = r'''import json, os, pathlib, sys
base = pathlib.Path(os.environ['TEST_BASE'])
(base / 'curl-args').write_text(json.dumps(sys.argv[1:]))
mode = (base / 'mode').read_text()
output = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])
(base / 'staging-mode').write_text(str(output.parent.stat().st_mode & 0o777))
if mode == 'download-hang': os.execv(sys.executable, [sys.executable, str(base / 'hang.py')])
if mode == 'download-fail':
    print('secret-download-token', file=sys.stderr)
    sys.exit(22)
if mode == 'empty': output.write_bytes(b'')
elif mode == 'oversize': output.write_bytes(b'x' * (1024 * 1024 + 1))
else: output.write_bytes((base / 'installer-source').read_bytes())
'''
INSTALLER = r'''#!/bin/bash
printf '%s' "$#" > "$TEST_BASE/installer-argc"
if [ "$(cat "$TEST_BASE/mode")" = fail ]; then echo secret-install-token; exit 7; fi
if [ "$(cat "$TEST_BASE/mode")" = missing ]; then exit 0; fi
if [ "$(cat "$TEST_BASE/mode")" = hang ]; then
    exec "$TEST_PYTHON" "$TEST_BASE/hang.py"
fi
mkdir -p "$HOME/.local/bin"
cp "$TEST_BASE/cli-source" "$HOME/.local/bin/claude"
chmod 755 "$HOME/.local/bin/claude"
'''
CLI = r'''import json, os, pathlib, sys, time
base = pathlib.Path(os.environ['TEST_BASE'])
if sys.argv[1:] == ['--version']:
    mode = (base / 'mode').read_text()
    if mode == 'bad-version': print('secret-version-token')
    elif mode == 'version-fail': print('2.1.0 (Claude Code)'); sys.exit(3)
    elif mode == 'version-long': print('x' * 1000000)
    elif mode == 'version-hang': time.sleep(120)
    else: print('2.1.0 (Claude Code)')
else:
    (base / 'started').write_text(json.dumps({'tz': os.getenv('TZ'), 'argv': sys.argv[1:], 'executable': sys.argv[0]}))
'''
HANG = r'''import json, os, pathlib, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)'])
pathlib.Path(os.environ['TEST_BASE'], 'children').write_text(json.dumps([os.getpid(), child.pid]))
while True: time.sleep(.05)
'''


class CLIInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cli-install-test-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.home, self.bin = self.base / 'home', self.base / 'bin'
        self.home.mkdir(); self.bin.mkdir()
        self.helper = self.base / 'claude-code.py'
        self.helper.write_text((ROOT / 'core/claude-code.py').read_text().replace(
            'INSTALL_TIMEOUT = 240', 'INSTALL_TIMEOUT = 2').replace('STOP_TIMEOUT = 2', 'STOP_TIMEOUT = 0.15'))
        self.shell = self.base / 'health.sh'
        self.shell.write_text("#!/bin/bash\nprintf 'proxy=ok\\ntun=ok\\ncountry=ok\\nsubscription=ok\\n'\n")
        self.mode('good')
        (self.base / 'installer-source').write_text(INSTALLER)
        (self.base / 'cli-source').write_text('#!' + sys.executable + '\n' + CLI)
        (self.base / 'hang.py').write_text(HANG)
        curl = self.bin / 'curl'
        curl.write_text('#!' + sys.executable + '\n' + CURL); curl.chmod(0o755)
        self.env = {'HOME': str(self.home), 'PATH': str(self.bin) + ':/usr/bin:/bin',
                    'TEST_BASE': str(self.base), 'TEST_PYTHON': sys.executable, 'LC_ALL': 'C', 'LANG': 'C'}

    def mode(self, value):
        (self.base / 'mode').write_text(value)

    def args(self, *extra):
        return [sys.executable, str(self.helper), str(self.shell), *extra]

    def invoke(self, *args):
        return subprocess.run(self.args(*args), env=self.env, text=True, capture_output=True, timeout=8)

    def install(self):
        result = self.invoke('install', '--json')
        report = json.loads(result.stdout)
        self.assertEqual(report['kind'], 'claude_code_install')
        self.assertEqual(report['schema_version'], 1)
        self.assertNotIn('secret-', result.stdout + result.stderr)
        self.assertNotIn(str(self.base), result.stdout + result.stderr)
        self.assertFalse((self.home / '.claude-switcher').exists())
        return result, report

    def seed_cli(self, directory=None):
        path = (directory or self.home / '.local/bin') / 'claude'
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.base / 'cli-source', path); path.chmod(0o755)
        return path

    def test_success_uses_fixed_https_installer_then_second_call_skips(self):
        result, report = self.install()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report['status'], 'installed')
        self.assertEqual(report['version'], '2.1.0 (Claude Code)')
        args = json.loads((self.base / 'curl-args').read_text())
        self.assertEqual(args[0], '-q')
        self.assertEqual(args[-1], 'https://claude.ai/install.sh')
        self.assertEqual(args[args.index('--proto') + 1], '=https')
        self.assertEqual(args[args.index('--proto-redir') + 1], '=https')
        self.assertEqual(args[args.index('--max-filesize') + 1], '1048576')
        staged = Path(args[args.index('--output') + 1])
        self.assertFalse(staged.exists())
        self.assertEqual((self.base / 'staging-mode').read_text(), '448')
        self.assertEqual((self.base / 'installer-argc').read_text(), '0')
        (self.base / 'curl-args').unlink()
        self.mode('fail')
        result, report = self.install()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report['status'], 'already_present')
        self.assertIsNone(report['version'])
        self.assertFalse((self.base / 'curl-args').exists())

    def test_existing_executable_is_never_executed_or_downloaded(self):
        cli = self.seed_cli()
        cli.write_text('#!/bin/bash\necho secret-existing-token\ntouch "' + str(self.base / 'executed') + '"\nexit 1\n')
        _, report = self.install()
        self.assertEqual(report['status'], 'already_present')
        self.assertFalse((self.base / 'executed').exists())
        self.assertFalse((self.base / 'curl-args').exists())

    def test_path_command_takes_priority_over_home_fallback(self):
        cli = self.seed_cli(self.bin)
        cli.write_text('#!/bin/bash\necho secret-existing-token\nexit 1\n')
        fallback = self.seed_cli()
        fallback.write_text('#!/bin/bash\ntouch "' + str(self.base / 'fallback') + '"\n')
        self.assertEqual(self.install()[1]['status'], 'already_present')
        self.assertFalse((self.base / 'fallback').exists())

    def test_local_bin_fallback_works_for_check_and_run(self):
        self.seed_cli()
        result = self.invoke('check', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['checks'][0]['status'], 'ok')
        result = self.invoke('run', '--', 'hello')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.base / 'started').read_text())['argv'], ['hello'])
        self.assertFalse((self.base / 'curl-args').exists())

    def test_symlink_launch_path_remains_stable(self):
        target = self.seed_cli(self.base / 'versions/2.1.0')
        stable = self.home / '.local/bin/claude'
        stable.parent.mkdir(parents=True)
        stable.symlink_to(target)
        result = self.invoke('run', '--', 'hello')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.base / 'started').read_text())['executable'], str(stable))

    def test_reparented_before_install_is_cancelled_without_download(self):
        spec = importlib.util.spec_from_file_location('cli_install_parent_test', self.helper)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with patch.object(module, 'find_cli', return_value=None), patch.object(module.os, 'getppid', return_value=1), patch.object(module.subprocess, 'Popen') as child:
            self.assertEqual(module.install_cli()['status'], 'cancelled')
            child.assert_not_called()

    def test_missing_cli_check_does_not_install(self):
        result = self.invoke('check', '--json')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['checks'][0]['status'], 'fail')
        self.assertFalse((self.base / 'curl-args').exists())

    def test_nonexecutable_and_directory_are_not_cli(self):
        cli = self.seed_cli(); cli.chmod(0o644)
        self.assertEqual(json.loads(self.invoke('check', '--json').stdout)['checks'][0]['status'], 'fail')
        cli.unlink(); cli.mkdir()
        self.assertEqual(json.loads(self.invoke('check', '--json').stdout)['checks'][0]['status'], 'fail')

    def test_install_rejects_profile_route_timezone_and_claude_arguments(self):
        for options in [('A',), ('default',), ('--route', 'tun'), ('--route', 'antify'),
                        ('--timezone', 'America/Chicago'), ('--',), ('--', '--version')]:
            with self.subTest(options=options):
                result = self.invoke('install', *options, '--json')
                self.assertEqual(result.returncode, 2)
                self.assertFalse((self.base / 'curl-args').exists())

    def test_failed_or_invalid_download_never_runs_installer(self):
        for mode in ('download-fail', 'empty', 'oversize'):
            with self.subTest(mode=mode):
                self.mode(mode)
                result, report = self.install()
                self.assertEqual(result.returncode, 1)
                self.assertEqual(report['status'], 'failed')
                self.assertIsNone(report['version'])
                self.assertFalse((self.home / '.local/bin/claude').exists())

    def test_installer_failure_missing_cli_and_bad_version_never_pass(self):
        for mode in ('fail', 'missing', 'bad-version', 'version-fail', 'version-long', 'version-hang'):
            with self.subTest(mode=mode):
                cli = self.home / '.local/bin/claude'
                if cli.exists(): cli.unlink()
                self.mode(mode)
                result, report = self.install()
                self.assertEqual(result.returncode, 1)
                self.assertEqual(report['status'], 'failed')
                self.assertIsNone(report['version'])

    def wait_file(self, name):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (self.base / name).exists(): return self.base / name
            time.sleep(.025)
        self.fail(name + ' not created')

    def assert_gone(self, pid):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = subprocess.run(['/bin/ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True)
            if result.returncode or result.stdout.strip().startswith('Z'): return
            time.sleep(.025)
        self.fail('owned process survived cleanup')

    def test_timeout_terminates_installer_and_grandchild(self):
        self.mode('hang')
        result, report = self.install()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report['status'], 'failed')
        for pid in json.loads(self.wait_file('children').read_text()): self.assert_gone(pid)

    def test_download_timeout_cleans_owned_children(self):
        self.mode('download-hang')
        result, report = self.install()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report['status'], 'failed')
        for pid in json.loads(self.wait_file('children').read_text()): self.assert_gone(pid)

    def test_signal_cancels_and_cleans_installer_and_grandchild(self):
        self.mode('hang')
        process = subprocess.Popen(self.args('install', '--json'), env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        pids = json.loads(self.wait_file('children').read_text())
        process.send_signal(signal.SIGTERM)
        time.sleep(.025)
        process.send_signal(signal.SIGINT)
        output, error = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 130, error)
        self.assertEqual(json.loads(output)['status'], 'cancelled')
        for pid in pids: self.assert_gone(pid)

    def test_parent_exit_cleans_installer_and_grandchild(self):
        self.mode('hang')
        launcher = r'''import os,pathlib,subprocess,sys,time
base=pathlib.Path(os.environ['TEST_BASE'])
with (base/'parent-result').open('w') as output:
    child=subprocess.Popen(sys.argv[1:], stdout=output, stderr=subprocess.DEVNULL)
    (base/'helper-pid').write_text(str(child.pid))
    while not (base/'children').exists(): time.sleep(.025)
'''
        parent = subprocess.Popen([sys.executable, '-c', launcher, *self.args('install', '--json')], env=self.env)
        parent.wait(timeout=5)
        pids = json.loads(self.wait_file('children').read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try: report = json.loads((self.base / 'parent-result').read_text()); break
            except (ValueError, FileNotFoundError): time.sleep(.025)
        else: self.fail('parent cancellation produced no result')
        self.assertEqual(report['status'], 'cancelled')
        for pid in pids: self.assert_gone(pid)
        self.assert_gone(int(self.wait_file('helper-pid').read_text()))

    @unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'), 'requires macOS Swift compiler')
    def test_gui_install_result_decoder_rejects_inconsistent_or_untrusted_results(self):
        source = (ROOT / 'app/main.swift').read_text()
        definition = source[source.index('struct CodeInstallReport:'):source.index('struct CheckRow:')]
        fixture = r'''
func decode(_ value: [String: Any], exit: Int32 = 0) -> CodeInstallReport? {
 let data = try! JSONSerialization.data(withJSONObject: value)
 return CodeInstallReport.decode(String(decoding: data, as: UTF8.self), exit: exit)
}
let good: [String: Any] = ["schema_version": 1, "kind": "claude_code_install", "status": "installed", "detail": "Claude Code 已安装；首次使用请自行登录", "version": "2.1.0 (Claude Code)"]
precondition(decode(good) != nil)
precondition(decode(good, exit: 1) == nil)
for (key, value) in [("schema_version", 2 as Any), ("kind", "other"), ("status", "unknown"), ("detail", "secret-token"), ("version", NSNull()), ("version", "secret-token"), ("version", "2.1.0\nsecret"), ("version", "2.1.0\n")] {
 var wrong = good; wrong[key] = value; precondition(decode(wrong) == nil)
}
let skipped: [String: Any] = ["schema_version": 1, "kind": "claude_code_install", "status": "already_present", "detail": "已找到 Claude Code；保留现有安装，未执行或验证安装来源", "version": NSNull()]
precondition(decode(skipped) != nil)
var wrong = skipped; wrong["version"] = "2.1.0"; precondition(decode(wrong) == nil)
precondition(CodeInstallReport.decode("not-json", exit: 0) == nil)
'''
        path = self.base / 'decoder.swift'
        path.write_text('import Foundation\n' + definition + fixture)
        binary = self.base / 'decoder'
        sdk = subprocess.check_output(['xcrun', '--sdk', 'macosx', '--show-sdk-path'], text=True).strip()
        compiled = subprocess.run(['swiftc', '-sdk', sdk, str(path), '-o', str(binary)], capture_output=True, text=True, timeout=45)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        subprocess.run([str(binary)], check=True, timeout=5)


if __name__ == '__main__':
    unittest.main()
