import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'core/claude-switch.sh').read_text()


def function(name):
    match = re.search(r'(?ms)^' + re.escape(name) + r'\(\) \{\n.*?^\}', SOURCE)
    if match is None:
        raise AssertionError(name)
    return match.group() + "\n"


class ProcessTimezoneTests(unittest.TestCase):
    def run_shell(self, code, env=None):
        return subprocess.run(['/bin/bash', '-c', code], text=True, capture_output=True,
                              timeout=10, env={**os.environ, 'LC_ALL': 'C', **(env or {})})

    def test_target_accepts_iana_and_rejects_paths_or_posix(self):
        for target, valid in [('America/Chicago', True), ('Etc/GMT+5', True), ('UTC', True),
                              ('../UTC', False), ('/etc/passwd', False), ('UTC0', False),
                              ('Mars/Olympus', False), ('', False)]:
            with self.subTest(target=target):
                result = self.run_shell(function('timezone_valid') + '\ntimezone_valid "$TEST_TZ"', {'TEST_TZ': target})
                self.assertEqual(result.returncode == 0, valid, result.stderr)

    def test_desktop_launch_quits_then_passes_target_without_new_instance(self):
        script = '''
TZ_TARGET=America/Chicago
quit_claude() { printf 'quit\n'; }
open() { printf '<%s>\n' "$@"; }
'''
        result = self.run_shell(script + function('timezone_valid') + function('launch_claude') + '\nlaunch_claude')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['quit', '<--env>', '<TZ=America/Chicago>', '<-a>', '<Claude>'])

    def test_desktop_invalid_target_or_quit_failure_never_opens(self):
        for target, quit_status in [('Mars/Olympus', 0), ('America/Chicago', 1)]:
            with self.subTest(target=target, quit_status=quit_status):
                script = 'TZ_TARGET="$TEST_TZ"\nquit_claude() { return "$TEST_QUIT"; }\nopen() { echo OPENED; }\n'
                result = self.run_shell(script + function('timezone_valid') + function('launch_claude') + '\nlaunch_claude',
                                        {'TEST_TZ': target, 'TEST_QUIT': str(quit_status)})
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('OPENED', result.stdout)

    def test_same_and_different_account_open_failure_cannot_report_success(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for current in ('A', 'B'):
                with self.subTest(current=current):
                    (base / 'current').write_text(current)
                    script = '''
BASE="$TEST_BASE"
DATA="$BASE/live"
TZ_TARGET=America/Chicago
mkdir -p "$DATA" "$BASE/accounts/A" "$BASE/accounts/B"
launch_gate() { return 0; }
quit_claude() { return 0; }
archive_current() { return 0; }
copy_tree() { mkdir -p "$2"; }
log() { :; }
open() { return 1; }
'''
                    result = self.run_shell(script + function('timezone_valid') + function('launch_claude') + function('switch_to') + '\nswitch_to A', {'TEST_BASE': directory})
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn('OK:', result.stdout)
                    self.assertNotIn('INFO:', result.stdout)

    def test_system_mismatch_does_not_block_check(self):
        with tempfile.TemporaryDirectory() as directory:
            script = '''
BASE="$TEST_BASE"
COUNTRY_TARGET=US
TZ_TARGET=America/Chicago
LOCALE_TARGET=en_US
proxy_alive() { return 0; }
tun_up() { return 0; }
curl() { case "$*" in *cdn-cgi/trace*) printf 'ip=192.0.2.10\nloc=US\n' ;; *) printf 200 ;; esac; }
subscription_ip_ok() { return 0; }
iphist_count() { printf 1; }
readlink() { echo SYSTEM_TIMEZONE_MUST_NOT_BE_READ; return 99; }
defaults() { case "$*" in *AppleLocale*) printf 'en_US\n' ;; *) printf '(\n en-US\n)\n' ;; esac; }
networksetup() { printf 'IPv6: Off\n'; }
dig() { printf '198.18.0.1\n'; }
webrtc_test() { printf 'RESULT:192.0.2.10\n'; }
launchctl() { return 0; }
merge_ok() { return 0; }
stun_ok() { return 0; }
route_ok() { return 0; }
'''
            result = self.run_shell(script + function('timezone_valid') + function('webrtc_result') + function('check') + '\ncheck', {'TEST_BASE': directory})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(result.stdout.startswith('OK:'), result.stdout)
            self.assertNotIn('时区异常', result.stdout)
            self.assertNotIn('SYSTEM_TIMEZONE', result.stdout)

    def test_macos_fix_region_and_watchdog_have_no_system_timezone_writer(self):
        for name in ('fix', 'set_region'):
            body = function(name)
            self.assertNotIn('systemsetup', body)
            self.assertNotIn('tz-watchdog.sh', body)
        watchdog = (ROOT / 'daemons/tz-watchdog.sh').read_text()
        self.assertNotIn('systemsetup', watchdog)
        result = self.run_shell(watchdog)
        self.assertEqual(result.returncode, 0)
