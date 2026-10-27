import errno
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import time
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, signal, subprocess, sys, termios, time
base = pathlib.Path(os.environ['TEST_BASE'])
name = pathlib.Path(sys.argv[0]).name
mode = (base / 'mode').read_text()
if name == 'pgrep':
    sys.exit(1 if mode == 'offline' else 0)
if name == 'ifconfig':
    print('inet 198.18.0.1' if mode != 'no-tun' else '')
elif name == 'curl':
    if mode == 'hang':
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        (base / 'probe-pids').write_text(json.dumps([os.getpid(), child.pid]))
        time.sleep(120)
    if mode == 'curl-fail': sys.exit(7)
    print((base / 'trace').read_text())
elif name == 'claude':
    (base / 'started').write_text(json.dumps({'args': sys.argv[1:], 'config': os.getenv('CLAUDE_CONFIG_DIR'), 'cwd': os.getcwd(), 'pid': os.getpid(), 'tz': os.getenv('TZ')}))
    action = os.getenv('TEST_CLAUDE', 'echo')
    if action == 'echo':
        sys.stdout.write(sys.stdin.read())
        sys.stderr.write('claude-stderr\n')
        sys.exit(int(os.getenv('TEST_EXIT', '0')))
    elif action == 'profile':
        pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'], 'login-marker').write_text(os.environ['TEST_LOGIN'])
    elif action == 'signal':
        os.kill(os.getpid(), int(os.getenv('TEST_SIGNAL', str(signal.SIGTERM))))
    elif action == 'wait':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        (base / 'child-pids').write_text(json.dumps([os.getpid(), child.pid]))
        while True: time.sleep(.1)
    elif action == 'tty':
        attributes = termios.tcgetattr(0)
        attributes[3] &= ~termios.ECHO
        termios.tcsetattr(0, termios.TCSANOW, attributes)
        def interrupt(sig, frame):
            print('INTERRUPTED', flush=True)
            sys.exit(42)
        signal.signal(signal.SIGINT, interrupt)
        signal.signal(signal.SIGWINCH, lambda s, f: print('RESIZED', flush=True))
        print('TTY=' + str(os.isatty(0) and os.isatty(1)) + ' FG=' + str(os.tcgetpgrp(0) == os.getpgrp()), flush=True)
        for line in sys.stdin:
            print('INPUT=' + line.strip(), flush=True)
            if line.strip() == 'quit': break
'''


class CodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='claude-code-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / 'home'
        self.bin = self.base / 'bin'
        self.repo = self.base / 'repo'
        for directory in (self.home, self.bin, self.repo): directory.mkdir()
        self.script = self.repo / 'claude-switch.sh'
        shutil.copy2(ROOT / 'core/claude-switch.sh', self.script)
        helper = (ROOT / 'core/claude-code.py').read_text().replace('HEALTH_INTERVAL = 5', 'HEALTH_INTERVAL = 0.15').replace('HEALTH_TIMEOUT = 15', 'HEALTH_TIMEOUT = 5').replace('STOP_TIMEOUT = 2', 'STOP_TIMEOUT = 0.15')
        (self.repo / 'claude-code.py').write_text(helper)
        for name in ('pgrep', 'ifconfig', 'curl', 'claude'):
            path = self.bin / name
            path.write_text(FAKE)
            path.chmod(0o755)
        (self.bin / 'python3').symlink_to(sys.executable)
        self.env = {'HOME': str(self.home), 'PATH': str(self.bin) + ':/usr/bin:/bin', 'TEST_BASE': str(self.base), 'LC_ALL': 'C', 'LANG': 'C'}
        (self.base / 'mode').write_text('good')
        (self.base / 'trace').write_text('loc=US\nip=192.0.2.50\n')

    def run_code(self, *args, **kwargs):
        env = self.env | kwargs.pop('env', {})
        return subprocess.run(['/bin/bash', str(self.script), 'code', *args], env=env, cwd=self.base,
                              text=True, capture_output=True, timeout=12, **kwargs)

    def wait_file(self, name, timeout=10):
        path = self.base / name
        deadline = time.monotonic() + timeout
        while not path.exists() and time.monotonic() < deadline: time.sleep(.025)
        self.assertTrue(path.exists(), name)
        return path

    def assert_gone(self, pid):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = subprocess.run(['/bin/ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True)
            if result.returncode or result.stdout.strip().startswith('Z'): return
            time.sleep(.025)
        self.fail('owned process survived cleanup')

    def test_explicit_timezone_is_passed_only_to_child(self):
        result = self.run_code('run', '--timezone', 'Asia/Tokyo', '--', input='', env={'TZ': 'America/Chicago'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.base / 'started').read_text())['tz'], 'Asia/Tokyo')

    def test_unspecified_timezone_preserves_environment(self):
        result = self.run_code('run', '--', input='', env={'TZ': 'America/Chicago'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.base / 'started').read_text())['tz'], 'America/Chicago')

    def test_invalid_timezone_rejected_before_profile_creation_or_probe(self):
        for timezone in ('../UTC', '/etc/passwd', 'Mars/Olympus', 'UTC0', ''):
            with self.subTest(timezone=timezone):
                result = self.run_code('run', 'A', '--timezone', timezone, '--', input='')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(list(self.home.iterdir()), [])
                self.assertFalse((self.base / 'started').exists())

    def test_json_timezone_request_keeps_six_checks(self):
        result = self.run_code('check', '--timezone', 'Asia/Tokyo', '--json')
        report = json.loads(result.stdout)
        self.assertEqual(len(report['checks']), 6)
        self.assertIn('TZ=Asia/Tokyo', report['checks'][1]['detail'])
        self.assertIn('实际生效未验证', report['checks'][1]['detail'])

    def test_check_is_read_only(self):
        result = self.run_code('check', 'A')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('country=ok', result.stdout)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertFalse((self.base / 'started').exists())

    def report(self, profile="default"):
        result = self.run_code('check', profile, '--json')
        report = json.loads(result.stdout)
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual(report['profile'], profile)
        self.assertEqual([row['id'] for row in report['checks']], ['cli', 'profile', 'proxy', 'tun', 'country', 'subscription'])
        self.assertNotIn('192.0.2.', result.stdout)
        self.assertNotIn(str(self.home), result.stdout)
        return result, {row['id']: row for row in report['checks']}

    def test_json_unconfigured_profile_and_binding_are_warnings(self):
        for command in ('systemsetup', 'networksetup', 'defaults', 'launchctl'):
            path = self.bin / command
            path.write_text('#!/bin/sh\ntouch "$TEST_BASE/system-write"\nexit 1\n')
            path.chmod(0o755)
        result, rows = self.report('A')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(rows['cli']['status'], 'ok')
        self.assertEqual(rows['profile']['status'], 'warn')
        self.assertEqual(rows['subscription']['status'], 'warn')
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertFalse((self.base / 'started').exists())
        self.assertFalse((self.base / 'system-write').exists())

    def test_json_binding_and_existing_profile(self):
        base = self.home / '.claude-switcher'
        profile = base / 'code/A'
        profile.mkdir(parents=True, mode=0o700)
        profile.parent.chmod(0o700)
        (base / 'subscription.json').write_text('{"expected_ip": "192.0.2.50"}')
        result, rows = self.report('A')
        self.assertEqual(result.returncode, 0)
        self.assertEqual({row['status'] for row in rows.values()}, {'ok'})

    def test_json_failures_skip_dependent_checks(self):
        for mode, failed in [('offline', 'proxy'), ('no-tun', 'tun'), ('curl-fail', 'country')]:
            with self.subTest(mode=mode):
                (self.base / 'mode').write_text(mode)
                result, rows = self.report()
                self.assertEqual(result.returncode, 1)
                self.assertEqual(rows[failed]['status'], 'fail')
                after = False
                for key, row in rows.items():
                    if after: self.assertEqual(row['status'], 'skipped')
                    if key == failed: after = True

    def test_json_missing_cli_still_checks_network(self):
        (self.bin / 'claude').unlink()
        result, rows = self.report()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(rows['cli']['status'], 'fail')
        self.assertEqual(rows['country']['status'], 'ok')
        self.assertEqual(list(self.home.iterdir()), [])

    def test_json_profile_errors_are_sanitized(self):
        result = self.run_code('check', 'A', '--json', env={'ANTHROPIC_API_KEY': 'secret-never-display'})
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('secret-never-display', result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['checks'][1]['status'], 'fail')
        target = self.base / 'target'
        target.mkdir()
        (self.home / '.claude-switcher').symlink_to(target)
        result, rows = self.report('B')
        self.assertEqual(rows['profile']['status'], 'fail')
        self.assertEqual(list(target.iterdir()), [])

    def test_json_incomplete_health_is_not_success(self):
        (self.base / 'trace').write_text('loc=JP\nip=192.0.2.50')
        result, rows = self.report()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(rows['country']['status'], 'fail')
        self.assertEqual(rows['subscription']['status'], 'skipped')

    def test_json_timeout_is_bounded_and_cleans_children(self):
        (self.base / 'mode').write_text('hang')
        result, rows = self.report()
        self.assertEqual(result.returncode, 1)
        self.assertTrue(any(row['status'] == 'fail' for row in rows.values()))
        for pid in json.loads((self.base / 'probe-pids').read_text()): self.assert_gone(pid)

    def test_json_cannot_be_used_for_run(self):
        result = self.run_code('run', '--json')
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.base / 'started').exists())

    @unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'), 'requires macOS Swift compiler')
    def test_swift_report_contract_rejects_invalid_results(self):
        source = (ROOT / 'app/main.swift').read_text()
        models = source[source.index('enum RowState:'):source.index('struct CheckRow:')]
        harness = r'''
let checks: [[String: Any]] = CodeReport.ids.map { ["id": $0, "status": "ok", "detail": "checked"] }
let valid: [String: Any] = ["schema_version": 1, "profile": "A", "checks": checks]
func decode(_ object: [String: Any], profile: String = "A") -> CodeReport? {
    let data = try! JSONSerialization.data(withJSONObject: object)
    return CodeReport.decode(String(decoding: data, as: UTF8.self), profile: profile)
}
precondition(decode(valid) != nil)
precondition(decode(valid, profile: "B") == nil)
for version in [0, 2] { var bad = valid; bad["schema_version"] = version; precondition(decode(bad) == nil) }
for status in ["pending", "unknown"] {
    var items = checks; items[0]["status"] = status
    var bad = valid; bad["checks"] = items; precondition(decode(bad) == nil)
}
var duplicate = checks; duplicate[0] = duplicate[1]
var missing = checks; missing.removeLast()
var unknown = checks; unknown[0]["id"] = "other"
for items in [duplicate, missing, unknown] {
    var bad = valid; bad["checks"] = items; precondition(decode(bad) == nil)
}
precondition(CodeReport.decode("not-json", profile: "A") == nil)
print("validated report contract")
'''
        script = self.base / 'report.swift'
        script.write_text('import Foundation\n' + models + harness)
        binary = self.base / 'report-test'
        sdk = subprocess.check_output(['xcrun', '--sdk', 'macosx', '--show-sdk-path'], text=True).strip()
        compiled = subprocess.run(['swiftc', '-sdk', sdk, str(script), '-o', str(binary)], capture_output=True, text=True, timeout=60)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('validated report contract', result.stdout)

    def test_arguments_streams_working_directory_and_exit(self):
        args = ['-p', 'hello world', '', '--model', 'sonnet', 'a;echo nope', '--', '$HOME']
        result = self.run_code('run', '--', *args, input='pipe input\n', env={'TEST_EXIT': '17'})
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(result.stdout, 'pipe input\n')
        self.assertEqual(result.stderr, 'claude-stderr\n')
        info = json.loads((self.base / 'started').read_text())
        self.assertEqual(info['args'], args)
        self.assertEqual(info['cwd'], str(self.base.resolve()))
        self.assertIsNone(info['config'])
        self.assertEqual(list(self.home.iterdir()), [])

    def test_profiles_keep_separate_login_and_default_untouched(self):
        normal = self.home / '.claude'
        normal.mkdir()
        (normal / 'settings.json').write_text('default untouched')
        for profile in ('A', 'B', 'A'):
            result = self.run_code('run', profile, '--', env={'TEST_CLAUDE': 'profile', 'TEST_LOGIN': profile})
            self.assertEqual(result.returncode, 0, result.stderr)
        for profile in ('A', 'B'):
            path = self.home / '.claude-switcher/code' / profile
            self.assertEqual((path / 'login-marker').read_text(), profile)
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual((normal / 'settings.json').read_text(), 'default untouched')
        self.assertFalse((self.home / '.claude-switcher/accounts').exists())
        self.assertFalse((self.home / '.claude-switcher/iphistory.log').exists())

    def test_named_profiles_reject_auth_overrides(self):
        for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_USE_BEDROCK', 'AWS_PROFILE', 'CLAUDE_CONFIG_DIR'):
            with self.subTest(key=key):
                result = self.run_code('run', 'A', '--', env={key: 'do-not-print-this'})
                self.assertEqual(result.returncode, 2)
                self.assertNotIn('do-not-print-this', result.stderr + result.stdout)
                self.assertFalse((self.base / 'started').exists())
        self.assertEqual(list(self.home.iterdir()), [])

    def test_symlink_profile_rejected(self):
        target = self.base / 'target'
        target.mkdir()
        (self.home / '.claude-switcher').symlink_to(target, target_is_directory=True)
        result = self.run_code('run', 'A', '--')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse((self.base / 'started').exists())

    def test_network_failures_block_execution(self):
        for mode in ('offline', 'no-tun', 'curl-fail', 'hang'):
            with self.subTest(mode=mode):
                (self.base / 'mode').write_text(mode)
                result = self.run_code('run', 'A', '--')
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.base / 'started').exists())
                self.assertEqual(list(self.home.iterdir()), [])
        for pid in json.loads((self.base / 'probe-pids').read_text()): self.assert_gone(pid)

    def test_trace_and_binding_are_fail_closed(self):
        invalid = ('', 'loc=JP\nip=192.0.2.50', 'loc=US', 'loc=US\nip=not-an-ip', 'loc=US\nloc=US\nip=192.0.2.50')
        for trace in invalid:
            (self.base / 'trace').write_text(trace)
            self.assertNotEqual(self.run_code('run', '--').returncode, 0)
        (self.base / 'trace').write_text('loc=US\nip=192.0.2.50')
        base = self.home / '.claude-switcher'
        base.mkdir()
        for contents in ('{', '[]', '{}', '{"expected_ip": "192.0.2.51"}', '{"expected_ip": false}'):
            (base / 'subscription.json').write_text(contents)
            result = self.run_code('run', '--')
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('192.0.2.', result.stderr + result.stdout)
        self.assertFalse((self.base / 'started').exists())
        (base / 'subscription.json').write_text('{"expected_ip": "192.0.2.50"}')
        self.assertEqual(self.run_code('check').returncode, 0)
        self.assertEqual(sorted(p.name for p in base.iterdir()), ['subscription.json'])

    def test_missing_cli_blocks_before_profile_creation(self):
        (self.bin / 'claude').unlink()
        result = self.run_code('run', 'B', '--')
        self.assertEqual(result.returncode, 2)
        self.assertIn('CLI', result.stderr)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_runtime_offline_terminates_only_owned_group(self):
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        self.addCleanup(lambda: (unrelated.terminate(), unrelated.wait()))
        env = self.env | {'TEST_CLAUDE': 'wait'}
        process = subprocess.Popen(['/bin/bash', str(self.script), 'code', 'run', '--'], env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.poll() is None and (process.terminate(), process.wait()))
        pids = json.loads(self.wait_file('child-pids').read_text())
        (self.base / 'mode').write_text('offline')
        stdout, stderr = process.communicate(timeout=12)
        self.assertEqual(process.returncode, 125, stderr)
        self.assertEqual(stdout, b'')
        self.assertIsNone(unrelated.poll())
        for pid in pids: self.assert_gone(pid)

    def test_signal_exit_is_preserved(self):
        result = self.run_code('run', '--', env={'TEST_CLAUDE': 'signal'})
        self.assertEqual(result.returncode, -signal.SIGTERM)
        result = self.run_code('run', '--', env={'TEST_CLAUDE': 'signal', 'TEST_SIGNAL': str(signal.SIGKILL)})
        self.assertEqual(result.returncode, -signal.SIGKILL)
        result = self.run_code('run', '--', input='', env={'TEST_EXIT': '143'})
        self.assertEqual(result.returncode, 143)

    def test_preflight_signal_cleans_probe(self):
        (self.base / 'mode').write_text('hang')
        process = subprocess.Popen(['/bin/bash', str(self.script), 'code', 'run', '--'],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.poll() is None and (process.kill(), process.communicate()))
        pids = json.loads(self.wait_file('probe-pids').read_text())
        process.terminate()
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, -signal.SIGTERM)
        for pid in pids: self.assert_gone(pid)
        self.assertFalse((self.base / 'started').exists())

    def test_unmodified_helper_pipeline_and_runtime(self):
        shutil.copy2(ROOT / 'core/claude-code.py', self.repo / 'claude-code.py')
        result = self.run_code('run', '--', '-p', 'verbatim', input='real artifact\n')
        self.assertEqual(result.stdout, 'real artifact\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.test_runtime_offline_terminates_only_owned_group()

    def test_missing_helper_migration_guidance(self):
        (self.repo / 'claude-code.py').unlink()
        result = self.run_code('run', '--')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('claude-switch.sh update', result.stderr)
        self.assertFalse((self.base / 'started').exists())

    def tty_start(self):
        pid, fd = pty.fork()
        if pid == 0:
            os.execve('/bin/bash', ['/bin/bash', str(self.script), 'code', 'run', '--'], self.env | {'TEST_CLAUDE': 'tty'})
        def cleanup():
            try: os.kill(pid, signal.SIGCONT)
            except ProcessLookupError: pass
            try: os.kill(pid, signal.SIGTERM)
            except ProcessLookupError: pass
            os.close(fd)
            try: os.waitpid(pid, 0)
            except ChildProcessError: pass
        self.addCleanup(cleanup)
        self.tty_read(fd, b'TTY=True FG=True')
        return pid, fd

    def tty_read(self, fd, expected):
        output = b''
        deadline = time.monotonic() + 10
        while expected not in output and time.monotonic() < deadline:
            if select.select([fd], [], [], .1)[0]:
                try: output += os.read(fd, 4096)
                except OSError as error:
                    if error.errno == errno.EIO: break
                    raise
        self.assertIn(expected, output)

    def test_tty_input_foreground_and_ctrl_c(self):
        shutil.copy2(ROOT / 'core/claude-code.py', self.repo / 'claude-code.py')
        pid, fd = self.tty_start()
        os.write(fd, b'hello tty\n')
        self.tty_read(fd, b'INPUT=hello tty')
        os.kill(pid, signal.SIGWINCH)
        self.tty_read(fd, b'RESIZED')
        os.write(fd, b'\x03')
        self.tty_read(fd, b'INTERRUPTED')
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.WEXITSTATUS(status), 42)

    def test_tty_ctrl_z_resume_and_terminal_restore(self):
        pid, fd = self.tty_start()
        os.write(fd, b'\x1a')
        deadline = time.monotonic() + 5
        stopped = False
        while time.monotonic() < deadline:
            waited, status = os.waitpid(pid, os.WNOHANG | os.WUNTRACED)
            if waited:
                self.assertTrue(os.WIFSTOPPED(status))
                stopped = True
                break
            time.sleep(.025)
        self.assertTrue(stopped)
        self.assertEqual(os.tcgetpgrp(fd), pid)
        self.assertTrue(termios.tcgetattr(fd)[3] & termios.ECHO)
        os.kill(pid, signal.SIGCONT)
        os.write(fd, b'after resume\n')
        self.tty_read(fd, b'INPUT=after resume')
        self.assertFalse(termios.tcgetattr(fd)[3] & termios.ECHO)
        os.write(fd, b'quit\n')
        self.tty_read(fd, b'INPUT=quit')
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.WEXITSTATUS(status), 0)



class CodeUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='claude-code-update-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / 'home'
        self.bin = self.base / 'bin'
        self.install = self.home / '.local/bin'
        self.data = self.home / '.claude-switcher'
        for directory in (self.home, self.bin, self.install, self.data): directory.mkdir(parents=True, exist_ok=True)
        self.script = self.install / 'claude-switch.sh'
        self.source = (ROOT / 'core/claude-switch.sh').read_text()
        self.source = self.source.replace('APPDIR="/Library/Application Support/claude-switcher"', 'APPDIR="' + str(self.base / 'system') + '"')
        marker = 'case "${1:-}" in\n    code)'
        self.source = self.source.replace(marker, 'checkdetail() { echo "fixture=${TEST_CHECK:-ok}"; }\n\n' + marker)
        self.script.write_text(self.source)
        self.script.chmod(0o755)
        self.helper = self.data / 'claude-code.py'
        self.helper.write_text('# previous helper\n')
        self.package = self.base / 'package'
        (self.package / 'core').mkdir(parents=True)
        (self.package / 'core/claude-switch.sh').write_text(self.source)
        (self.package / 'core/claude-code.py').write_text((ROOT / 'core/claude-code.py').read_text())
        fake = r"""#!/usr/bin/env python3
import os, pathlib, shutil, subprocess, sys
base = pathlib.Path(os.environ['TEST_BASE'])
name = pathlib.Path(sys.argv[0]).name
if name == 'curl':
    dest = sys.argv[sys.argv.index('-o') + 1]
    src = base / ('package.zip' if dest.endswith('main.zip') else 'SUMS')
    shutil.copyfile(src, dest)
elif name == 'id':
    print('0' if os.environ.get('TEST_ROOT') else '501')
elif name == 'stat':
    print('fixture-user')
elif name == 'sed':
    args = sys.argv[1:]
    if sys.platform != 'darwin' and args[:2] == ['-i', '']: del args[1]
    sys.exit(subprocess.call(['/usr/bin/sed', *args]))
elif name == 'sudo':
    with (base / 'sudo-calls').open('a') as handle: handle.write(repr(sys.argv[1:]) + '\n')
    args = sys.argv[1:]
    if args[:1] == ['-H']: args = args[1:]
    if args[:1] == ['-u']: args = args[2:]
    sys.exit(subprocess.call(args, env=os.environ | {'HOME': os.environ['TEST_USER_HOME']}))
"""
        for name in ('curl', 'id', 'stat', 'sed', 'sudo'):
            path = self.bin / name
            path.write_text(fake)
            path.chmod(0o755)
        (self.bin / 'python3').symlink_to(sys.executable)
        self.env = {'HOME': str(self.home), 'PATH': str(self.bin) + ':/usr/bin:/bin', 'TEST_BASE': str(self.base), 'TEST_USER_HOME': str(self.home)}

    def package_files(self, bad=False):
        paths = ['core/claude-switch.sh', 'core/claude-code.py']
        sums = ''.join(hashlib.sha256((self.package / name).read_bytes()).hexdigest() + '  ' + name + '\n' for name in paths)
        if bad: sums = sums.replace('core/claude-code.py', 'core/missing.py')
        (self.base / 'SUMS').write_text(sums)
        with zipfile.ZipFile(self.base / 'package.zip', 'w') as archive:
            for name in paths: archive.write(self.package / name, 'claude-switcher-main/' + name)

    def update(self, **env):
        return subprocess.run(['/bin/bash', str(self.script), 'update'], env=self.env | env,
                              capture_output=True, text=True, timeout=12)

    def test_helper_only_update_and_no_change(self):
        self.package_files()
        result = self.update()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.helper.read_bytes(), (ROOT / 'core/claude-code.py').read_bytes())
        self.assertEqual(self.helper.with_suffix('.py.bak').read_text(), '# previous helper\n')
        self.assertIn('已是最新', self.update().stdout)

    def test_script_growth_update_exits_without_reading_old_offset(self):
        (self.package / 'core/claude-switch.sh').write_text('# changed script length\n' + self.source)
        self.package_files()
        result = self.update()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(self.script.read_text().startswith('# changed script length'))

    def test_helper_and_script_rollback(self):
        self.package_files()
        result = self.update(TEST_CHECK='fail')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.helper.read_text(), '# previous helper\n')
        self.assertEqual(self.script.read_text(), self.source)
        self.helper.unlink()
        result = self.update(TEST_CHECK='fail')
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.helper.exists())

    def test_missing_helper_checksum_blocks_install(self):
        self.package_files(bad=True)
        result = self.update()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.helper.read_text(), '# previous helper\n')

    def test_legacy_root_home_uses_installed_user_home(self):
        self.helper.unlink()
        self.data.rmdir()
        self.package_files()
        root_home = self.base / 'root-home'
        root_home.mkdir()
        result = self.update(HOME=str(root_home), TEST_ROOT='1')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.helper.read_bytes(), (ROOT / 'core/claude-code.py').read_bytes())
        self.assertFalse((root_home / '.claude-switcher/claude-code.py').exists())
        calls = (self.base / 'sudo-calls').read_text()
        for command in ('mkdir', 'tee'):
            self.assertIn(repr(command), calls)
        self.assertIn("'-H', '-u'", calls)


if __name__ == '__main__': unittest.main()
