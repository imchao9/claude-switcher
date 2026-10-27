"""Run the macOS installer against a disposable HOME and mocked system tools.

No real installation, SDK changes, network calls, services, or account operations.
"""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
KEYS = "代理内核 TUN网卡 出口IP IP一致 时区 地区 IPv6 DNS WebRTC 保镖 规则置顶 客户端语言 ASN属性 生态可达性".split()

# All commands with external effects are replaced before running the fixture copy.
STUB = r'''
import json, os, pathlib, shlex, subprocess, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
base = pathlib.Path(os.environ['TEST_BASE'])
case = os.environ.get('TEST_CASE', '')
with (base / 'calls').open('a') as log:
    log.write(json.dumps([name, args]) + '\n')
if name == 'xcrun':
    if case == 'sdk': sys.exit(1)
    print(base / 'sdk')
elif name == 'swiftc':
    if case == 'compile': sys.exit(1)
    output = pathlib.Path(args[args.index('-o') + 1])
    output.write_bytes(b'fake binary')
    output.chmod(0o755)
elif name == 'curl':
    if case == 'ipinfo': sys.exit(28)
    print('{\n"country": "US",\n"timezone": "America/New_York"\n}')
elif name in ('sips', 'iconutil'):
    flag = '--out' if name == 'sips' else '-o'
    pathlib.Path(args[args.index(flag) + 1]).write_bytes(b'fake icon')
elif name == 'codesign':
    if case == 'sign' or (case == 'verify' and '--verify' in args): sys.exit(1)
elif name == 'sudo':
    if case == 'sudo': sys.exit(1)
    # Execute the actual privileged script, but redirect all system paths into
    # the fixture and keep its tools mocked. Never invoke real sudo.
    script = sys.stdin.read()
    script = script.replace('/Library/', str(base / 'system/Library') + '/')
    script = script.replace('export PATH=/usr/bin:/bin:/usr/sbin:/sbin',
                            'export PATH=' + shlex.quote(os.environ['PATH']))
    script = script.replace('/usr/sbin/systemsetup', shlex.quote(str(base / 'bin/systemsetup')))
    if '-e' not in args: raise RuntimeError('privileged shell must fail fast')
    sys.exit(subprocess.run(args, input=script, text=True).returncode)
elif name == 'cp':
    if case == 'core-copy' and pathlib.Path(args[0]).name == 'claude-switch.sh': sys.exit(1)
    if case == 'system-copy' and args[-1].endswith('/claude-switcher/'):
        sys.exit(1)
    sys.exit(subprocess.run(['/bin/cp', *args]).returncode)
elif name == 'mv':
    if case == 'publish' and pathlib.Path(args[0]).name == '.Claude切换器-installing.app': sys.exit(1)
    sys.exit(subprocess.run(['/bin/mv', *args]).returncode)
elif name == 'launchctl':
    if args[0] == 'disable':
        if case == 'watchdog-disable': sys.exit(1)
        (base / 'watchdog-disabled').touch()
    if args[0] == 'print-disabled':
        print('"local.tz-watchdog" => ' + ('false' if case == 'watchdog-disabled-readback' else 'disabled' if case == 'watchdog-disabled-word' else 'true'))
    if args[:2] == ['print', 'system/local.tz-watchdog']:
        if case == 'watchdog-print-error': sys.exit(1)
        if case in ('watchdog-loaded', 'watchdog-bootout') and not (base / 'watchdog-stopped').exists(): sys.exit(0)
        sys.exit(113)
    if args[:2] == ['bootout', 'system/local.tz-watchdog']:
        if case == 'watchdog-bootout': sys.exit(1)
        (base / 'watchdog-stopped').touch()
        sys.exit(0)
    if args[0] == 'bootout': sys.exit(113)  # Missing old services are expected.
    if case == 'system-bootstrap' and args[:2] == ['bootstrap', 'system']: sys.exit(1)
    if case == 'system-print' and args[0] == 'print' and args[1].startswith('system/'): sys.exit(1)
    if case == 'agent' and args[0] == 'bootstrap' and args[1].startswith('gui/'): sys.exit(1)
elif name == 'readlink':
    tz = base / 'timezone'
    target = tz.read_text() if tz.exists() else 'America/Chicago'
    print('/var/db/timezone/zoneinfo/' + target)
elif name == 'systemsetup':
    if case != 'timezone': (base / 'timezone').write_text(args[-1])
elif name == 'networksetup':
    if args[0] == '-setv6off':
        if case != 'ipv6': (base / 'ipv6-off').touch()
    else:
        print('IPv6: ' + ('Off' if (base / 'ipv6-off').exists() else 'Automatic'))
elif name == 'sed':
    # The production installer uses BSD sed; support the fixture on Linux too.
    if sys.platform != 'darwin' and args[:2] == ['-i', '']: del args[1]
    sys.exit(subprocess.run(['/usr/bin/sed', *args]).returncode)
'''


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='claude-installer-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / 'repo'
        self.home = self.base / 'home'
        self.bin = self.base / 'bin'
        for path in (self.repo, self.home, self.bin, self.base / 'sdk', self.base / 'tmp',
                     self.base / 'system/Library/LaunchDaemons', self.base / 'Claude.app'):
            path.mkdir(parents=True)
        for directory in ('installer', 'daemons'):
            shutil.copytree(ROOT / directory, self.repo / directory)
        for directory in ('core', 'app', 'assets'):
            (self.repo / directory).mkdir()
        (self.repo / 'core/stun-probe.py').write_text('# dummy probe\n')
        (self.repo / 'core/claude-code.py').write_text('# dummy Code helper\n')
        (self.repo / 'app/main.swift').write_text('import SwiftUI\n')
        (self.repo / 'assets/icon-2048.png').write_bytes(b'dummy icon')
        core = self.repo / 'core/claude-switch.sh'
        core.write_text('''#!/bin/bash
PROXY_MODE="default"
PROXY_GROUP="default"
COUNTRY_TARGET="default"
TZ_TARGET="default"
LOCALE_TARGET="default"
printf '%s\\n' "$1" >> "$TEST_BASE/core-calls"
case "$1" in
    write-merge-rules) [ "$TEST_CASE" != rules ] ;;
    checkdetail)
        cat "$TEST_BASE/checks"
        [ "$TEST_CASE" != check-command ] ;;
    *) exit 2 ;;
esac
''')
        self.script = self.repo / 'installer/安装.command'
        # Only the fixed app-detection path is redirected in the fixture copy.
        self.script.write_text(self.script.read_text().replace('/Applications/Claude.app', str(self.base / 'Claude.app')))
        clash = self.home / 'Library/Application Support/io.github.clash-verge-rev.clash-verge-rev'
        clash.mkdir(parents=True)
        (clash / 'clash-verge.yaml').write_text('proxy-groups:\n  - name: TestGroup\n    type: select\n')
        accounts = self.home / '.claude-switcher/accounts'
        accounts.mkdir(parents=True)
        (accounts / 'preserve').write_text('untouched')
        for command in ('xcrun', 'swiftc', 'curl', 'sips', 'iconutil', 'codesign',
                        'sudo', 'cp', 'mv', 'launchctl', 'readlink', 'systemsetup',
                        'networksetup', 'sed', 'chown', 'pkill', 'dscacheutil',
                        'killall', 'osascript', 'open', 'sleep', 'clear'):
            stub = self.bin / command
            stub.write_text('#!' + sys.executable + '\n' + STUB)
            stub.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update(HOME=str(self.home), PATH=str(self.bin) + ':/usr/bin:/bin:/usr/sbin:/sbin',
                        TEST_BASE=str(self.base), TEST_CASE='', TMPDIR=str(self.base / 'tmp'),
                        FORCE='', DRYRUN='')
        self.set_checks([key + '=ok:private-test-detail' for key in KEYS])

    def set_checks(self, rows):
        (self.base / 'checks').write_text('\n'.join(rows) + '\n')

    def run_installer(self, case='', **env):
        result = subprocess.run(['/bin/bash', str(self.script)],
                                env={**self.env, 'TEST_CASE': case, **env},
                                input='', capture_output=True, text=True, timeout=25)
        self.assertEqual((self.home / '.claude-switcher/accounts/preserve').read_text(), 'untouched')
        self.assertFalse(any((self.base / 'tmp').iterdir()), 'temporary build was not cleaned')
        return result

    def calls(self, name):
        path = self.base / 'calls'
        return [args for tool, args in map(json.loads, path.read_text().splitlines()) if tool == name] if path.exists() else []

    def assert_failed(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('✅ 安装完成', result.stdout)
        self.assertNotIn('private-test-detail', result.stdout + result.stderr)

    def seed_previous_install(self):
        app = self.home / 'Desktop/Claude切换器.app'
        app.mkdir(parents=True)
        (app / 'old-version').write_text('previous app')
        scripts = self.home / '.local/bin'
        scripts.mkdir(parents=True)
        (scripts / 'claude-switch.sh').write_text('previous core')
        (scripts / 'claude-bodyguard.sh').write_text('previous guard')
        return app, scripts

    def test_success_requires_sdk_signature_services_and_all_14_ok(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('✅ 安装完成', result.stdout)
        self.assertNotIn('private-test-detail', result.stdout)
        compile_args = self.calls('swiftc')[0]
        self.assertEqual(compile_args[compile_args.index('-sdk') + 1], str(self.base / 'sdk'))
        self.assertIn('-module-cache-path', compile_args)
        self.assertEqual(len(self.calls('codesign')), 2)
        self.assertTrue(self.calls('sudo'))
        self.assertTrue((self.home / 'Desktop/Claude切换器.app/Contents/MacOS/ClaudeSwitcher').is_file())
        self.assertEqual((self.home / '.claude-switcher/claude-code.py').read_text(), '# dummy Code helper\n')
        self.assertIn('PROXY_GROUP="TestGroup"', (self.home / '.local/bin/claude-switch.sh').read_text())
        for key in KEYS:
            self.assertIn(key + '=ok\n', result.stdout)

    def test_missing_source_fails_before_installation(self):
        (self.repo / 'core/stun-probe.py').unlink()
        result = self.run_installer()
        self.assert_failed(result)
        self.assertIn('缺少安装文件', result.stderr)
        self.assertFalse((self.home / '.local').exists())
        self.assertFalse(self.calls('swiftc'))

    def test_missing_code_helper_fails_before_installation(self):
        (self.repo / 'core/claude-code.py').unlink()
        result = self.run_installer()
        self.assert_failed(result)
        self.assertIn('缺少安装文件', result.stderr)
        self.assertFalse((self.home / '.local').exists())
        self.assertFalse(self.calls('swiftc'))

    def test_compile_failure_preserves_previous_app_core_and_guard(self):
        app, scripts = self.seed_previous_install()
        self.assert_failed(self.run_installer('compile'))
        self.assertEqual((app / 'old-version').read_text(), 'previous app')
        self.assertEqual((scripts / 'claude-switch.sh').read_text(), 'previous core')
        self.assertEqual((scripts / 'claude-bodyguard.sh').read_text(), 'previous guard')
        self.assertFalse(self.calls('pkill'))
        self.assertFalse(self.calls('sudo'))

    def test_missing_sdk_fails_before_compile(self):
        self.assert_failed(self.run_installer('sdk'))
        self.assertFalse(self.calls('swiftc'))

    def test_signing_and_verification_fail_before_modifying_installation(self):
        for case in ('sign', 'verify'):
            with self.subTest(case=case):
                self.assert_failed(self.run_installer(case))
                self.assertFalse((self.home / '.local').exists())
                self.assertFalse(self.calls('sudo'))

    def test_copy_failure_is_not_reported_as_core_installed(self):
        result = self.run_installer('core-copy')
        self.assert_failed(result)
        self.assertNotIn('✓ 核心文件已安装', result.stdout)
        self.assertFalse(self.calls('sudo'))

    def test_failed_app_publish_restores_previous_app(self):
        app, _ = self.seed_previous_install()
        self.assert_failed(self.run_installer('publish'))
        self.assertEqual((app / 'old-version').read_text(), 'previous app')
        self.assertFalse(self.calls('sudo'))
        self.assertFalse((self.home / 'Desktop/.Claude切换器-installing.app').exists())

    def test_existing_pending_app_is_not_deleted(self):
        app = self.home / 'Desktop/.Claude切换器-installing.app'
        app.mkdir(parents=True)
        (app / 'preserve').write_text('previous incomplete install')
        self.assert_failed(self.run_installer())
        self.assertEqual((app / 'preserve').read_text(), 'previous incomplete install')
        self.assertFalse(self.calls('sudo'))

    def test_rule_failure_stops_before_system_configuration(self):
        self.assert_failed(self.run_installer('rules'))
        self.assertFalse(self.calls('sudo'))

    def test_sudo_denied_keeps_previous_guard(self):
        _, scripts = self.seed_previous_install()
        self.assert_failed(self.run_installer('sudo'))
        self.assertEqual((scripts / 'claude-bodyguard.sh').read_text(), 'previous guard')
        self.assertFalse((self.home / 'Library/LaunchAgents').exists())

    def test_privileged_failure_stops_later_commands_and_keeps_previous_guard(self):
        _, scripts = self.seed_previous_install()
        result = self.run_installer('system-copy')
        self.assert_failed(result)
        self.assertNotIn('✓ 系统级配置完成', result.stdout)
        self.assertEqual((scripts / 'claude-bodyguard.sh').read_text(), 'previous guard')
        self.assertFalse(self.calls('launchctl'))

    def test_service_registration_and_readback_failures_are_fatal(self):
        for case in ('system-bootstrap', 'system-print', 'agent'):
            with self.subTest(case=case):
                self.assert_failed(self.run_installer(case))
                self.assertFalse((self.base / 'core-calls').read_text().endswith('checkdetail\n'))

    def test_timezone_is_user_managed_and_watchdog_is_retired(self):
        result = self.run_installer('watchdog-loaded')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.calls('systemsetup'))
        self.assertFalse((self.base / 'timezone').exists())
        self.assertIn(['disable', 'system/local.tz-watchdog'], self.calls('launchctl'))
        self.assertIn(['bootout', 'system/local.tz-watchdog'], self.calls('launchctl'))
        self.assertFalse(any(args[0] == 'bootstrap' and 'tz-watchdog' in ' '.join(args) for args in self.calls('launchctl')))
        self.assertFalse((self.base / 'system/Library/Application Support/claude-switcher/tz-watchdog.sh').exists())

    def test_watchdog_disabled_word_readback_is_accepted(self):
        result = self.run_installer('watchdog-disabled-word')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.calls('systemsetup'))

    def test_watchdog_retirement_errors_are_fatal(self):
        for case in ('watchdog-disable', 'watchdog-disabled-readback', 'watchdog-print-error', 'watchdog-bootout'):
            with self.subTest(case=case):
                (self.base / 'watchdog-stopped').unlink(missing_ok=True)
                self.assert_failed(self.run_installer(case))

    def test_process_timezone_warning_is_accepted(self):
        self.set_checks([key + ('=warn:请求未验证' if key == '时区' else '=ok') for key in KEYS])
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ipv6_change_must_be_read_back(self):
        result = self.run_installer('ipv6')
        self.assert_failed(result)
        self.assertIn('IPv6 关闭未生效', result.stderr)

    def test_nonzero_self_check_cannot_report_success(self):
        self.assert_failed(self.run_installer('check-command'))

    def test_red_warn_empty_missing_and_duplicate_checks_cannot_report_success(self):
        rows = [key + '=ok:private-test-detail' for key in KEYS]
        variants = {
            'red': [row.replace('DNS=ok', 'DNS=fail') for row in rows],
            'warn': [row.replace('ASN属性=ok', 'ASN属性=warn') for row in rows],
            'empty': [],
            'missing': rows[:-1],
            'duplicate': rows[:-1] + [rows[0]],
            'malformed': [row.replace('DNS=ok', 'DNS=okay') for row in rows],
        }
        for case, checks in variants.items():
            with self.subTest(case=case):
                self.set_checks(checks)
                self.assert_failed(self.run_installer())

    def test_dryrun_only_builds_and_never_claims_installation(self):
        result = self.run_installer(DRYRUN='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('[演练模式]', result.stdout)
        self.assertNotIn('✅ 安装完成', result.stdout)
        self.assertFalse((self.home / '.local').exists())
        self.assertFalse((self.home / 'Desktop').exists())
        self.assertFalse(self.calls('sudo'))
        self.assertFalse((self.base / 'core-calls').exists())

    def test_country_lookup_timeout_is_reported_before_installation(self):
        result = self.run_installer('ipinfo')
        self.assert_failed(result)
        self.assertIn('未能识别出口国家', result.stdout)
        self.assertFalse(self.calls('swiftc'))


if __name__ == '__main__':
    unittest.main()
