"""Isolated subscription CLI tests; no real proxy or install state is touched."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import socket
import threading
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "core/claude-switch.sh"


class SubscriptionCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        self.installed = self.root / "claude-switch.sh"
        shutil.copyfile(SCRIPT, self.installed)
        self.installed.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), PATH=str(self.bin) + os.pathsep + os.defpath)

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args, stdin="", env=None):
        return subprocess.run(["/bin/bash", str(self.installed), *args], input=stdin,
                              text=True, capture_output=True, timeout=10,
                              env=self.env if env is None else env)

    def runtime_fixture(self, port, *, count=1, denied=False):
        clash = self.home / "Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"
        clash.mkdir(parents=True, exist_ok=True)
        (clash / "clash-verge.yaml").write_text('secret: "fixture-controller-secret"\nmixed-port: 7890\n')
        ps = self.bin / "ps"
        ps.write_text("#!/bin/sh\n" + "\n".join(
            "printf '%s\\n' '123 /Applications/Clash Verge.app/Contents/MacOS/verge-mihomo -d " +
            str(clash) + " -ext-ctl-unix /tmp/Clash socket-" + str(i) + ".sock'" for i in range(count)) + "\n")
        ps.chmod(0o755)
        curl = self.bin / "curl"
        curl.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$HOME/curl-argv\"\ncat > \"$HOME/curl-stdin\"\n" +
                        ("exit 22\n" if denied else "printf '%s' '" + json.dumps({"mixed-port": port, "port": 0, "socks-port": 0}) + "'\n"))
        curl.chmod(0o755)

    def test_ports_distinguishes_runtime_from_download_template_and_hides_auth(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            actual = listener.getsockname()[1]
            self.runtime_fixture(actual)
            base = self.home / ".claude-switcher"
            base.mkdir()
            (base / "subscription.yaml").write_text("mixed-port: 7890\n")
            result = self.run_cli("subscription", "ports", "--json")
            data = json.loads(result.stdout)
            self.assertTrue(data["runtime_verified"], result.stdout + result.stderr)
            self.assertEqual(data["ports"]["mixed-port"], actual)
            self.assertFalse(data["template_matches_runtime"])
            self.assertNotIn("fixture-controller-secret", result.stdout + result.stderr + (self.home / "curl-argv").read_text())
            self.assertIn("fixture-controller-secret", (self.home / "curl-stdin").read_text())
            self.assertIn("Clash socket-0.sock", (self.home / "curl-argv").read_text())
            self.assertEqual((base / "subscription.yaml").read_text(), "mixed-port: 7890\n")

    def test_ports_auth_failure_reports_candidate_not_runtime(self):
        self.runtime_fixture(1080, denied=True)
        result = self.run_cli("subscription", "ports", "--json")
        data = json.loads(result.stdout)
        self.assertFalse(data["runtime_verified"])
        self.assertEqual(data["status"], "warn")
        self.assertEqual(data["source"], "configuration_candidate")
        self.assertEqual(data["ports"]["mixed-port"], 7890)
        self.assertEqual(data["reason"], "api_unavailable_or_unauthorized")

    def test_ports_rejects_ambiguous_core_and_unreachable_listener(self):
        self.runtime_fixture(0, count=2)
        data = json.loads(self.run_cli("subscription", "ports", "--json").stdout)
        self.assertFalse(data["runtime_verified"])
        self.assertEqual(data["reason"], "ambiguous_runtime")
        self.assertFalse((self.home / "curl-argv").exists())
        with socket.socket() as closed:
            closed.bind(("127.0.0.1", 0))
            port = closed.getsockname()[1]
        self.runtime_fixture(port)
        data = json.loads(self.run_cli("subscription", "ports", "--json").stdout)
        self.assertFalse(data["runtime_verified"])
        self.assertEqual(data["reason"], "listener_unreachable")

    def test_ports_does_not_ignore_a_second_core_without_unix_controller(self):
        self.runtime_fixture(1080)
        ps = self.bin / 'ps'
        ps.write_text(ps.read_text() + "printf '%s\\n' '456 /usr/local/bin/mihomo -f /tmp/another.yaml'\n")
        data = json.loads(self.run_cli('subscription', 'ports', '--json').stdout)
        self.assertFalse(data['runtime_verified'])
        self.assertEqual(data['reason'], 'ambiguous_runtime')
        self.assertFalse((self.home / 'curl-argv').exists())

    def test_ports_unknown_options_do_not_trigger_inspection(self):
        result = self.run_cli("subscription", "ports", "--private-token")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("private-token", result.stdout + result.stderr)

    def test_set_is_atomic_private_and_does_not_echo_url(self):
        url = "https://sub.example.com/s/cs_private-token-123456"
        result = self.run_cli("subscription", "set", stdin=url + "\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(url, result.stdout + result.stderr)
        manifest = self.home / ".claude-switcher/subscription.json"
        self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)
        self.assertEqual(json.loads(manifest.read_text())["url"], url)

    def test_insecure_url_is_rejected_unless_explicitly_allowed(self):
        result = self.run_cli("subscription", "set", stdin="http://sub.example.com/s/token\n")
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.home / ".claude-switcher/subscription.json").exists())
        result = self.run_cli("subscription", "set", stdin="http://sub.example.com/s/token\n",
                              env=dict(self.env, CLAUDE_SWITCHER_ALLOW_INSECURE_SUBSCRIPTION="1"))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_url_argument_is_not_accepted(self):
        result = self.run_cli("subscription", "set", "https://sub.example.com/s/private-token")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("private-token", result.stdout + result.stderr)

    def test_fetch_records_binding_ip_and_writes_private_clash_file(self):
        url = "https://sub.example.com/s/cs_private-token-123456"
        self.assertEqual(self.run_cli("subscription", "set", stdin=url + "\n").returncode, 0)
        pgrep = self.bin / "pgrep"
        pgrep.write_text("#!/bin/sh\nexit 0\n")
        pgrep.chmod(0o755)
        ifconfig = self.bin / "ifconfig"
        ifconfig.write_text("#!/bin/sh\nprintf 'inet 198.18.0.2\\n'\n")
        ifconfig.chmod(0o755)
        curl = self.bin / "curl"
        curl.write_text("""#!/bin/sh
printf '%s\\n' "$@" > "__CAPTURE__"
headers= body=
while [ "$#" -gt 0 ]; do
  case "$1" in
    -D) headers=$2; shift 2 ;;
    -o) body=$2; shift 2 ;;
    *) shift ;;
  esac
done
printf 'X-Claude-Switcher-Egress-IP: 198.51.100.10\\r\\n' > "$headers"
printf 'proxies:\\n  - name: user\\n    type: vless\\n    server: 198.51.100.10\\n    port: 443\\n' > "$body"
""")
        curl.write_text(curl.read_text().replace("__CAPTURE__", str(self.root / "curl-argv")))
        curl.chmod(0o755)
        result = self.run_cli("subscription", "fetch")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("private-token", result.stdout + result.stderr)
        self.assertNotIn("private-token", (self.root / "curl-argv").read_text())
        manifest = json.loads((self.home / ".claude-switcher/subscription.json").read_text())
        self.assertEqual(manifest["expected_ip"], "198.51.100.10")
        subscription = self.home / ".claude-switcher/subscription.yaml"
        self.assertEqual(stat.S_IMODE(subscription.stat().st_mode), 0o600)
        self.assertIn("198.51.100.10", subscription.read_text())
        self.assertEqual(list((self.home / ".claude-switcher").glob("subscription.headers.*")), [])
        self.assertEqual(list((self.home / ".claude-switcher").glob("subscription.body.*")), [])

    def test_legacy_static_subscription_uses_trusted_compatibility_shape(self):
        url = "https://sub.610247.xyz/sub/" + "a" * 43 + "/vpn-host1.yaml"
        self.assertEqual(self.run_cli("subscription", "set", stdin=url + "\n").returncode, 0)
        pgrep = self.bin / "pgrep"
        pgrep.write_text("#!/bin/sh\nexit 0\n")
        pgrep.chmod(0o755)
        ifconfig = self.bin / "ifconfig"
        ifconfig.write_text("#!/bin/sh\nprintf 'inet 198.18.0.2\\n'\n")
        ifconfig.chmod(0o755)
        curl = self.bin / "curl"
        curl.write_text("""#!/bin/sh
headers= body=
while [ "$#" -gt 0 ]; do
  case "$1" in
    -D) headers=$2; shift 2 ;;
    -o) body=$2; shift 2 ;;
    *) shift ;;
  esac
done
: > "$headers"
cat > "$body" <<'YAML'
proxies:
  - name: 住宅出口
    type: socks5
    server: 8.8.8.8
    port: 1337
  - name: vpn-host1
    type: vless
    server: 203.0.113.10
    port: 443
rules:
  - DOMAIN-SUFFIX,claude.ai,VPN-HOST1
  - DOMAIN-SUFFIX,anthropic.com,VPN-HOST1
  - DOMAIN-SUFFIX,claude.com,VPN-HOST1
  - DOMAIN-SUFFIX,clau.de,VPN-HOST1
  - DOMAIN-SUFFIX,claudeusercontent.com,VPN-HOST1
  - GEOIP,CN,DIRECT
  - MATCH,VPN-HOST1
YAML
""")
        curl.chmod(0o755)
        result = self.run_cli("subscription", "fetch")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("旧静态订阅兼容模式", result.stdout)
        manifest = json.loads((self.home / ".claude-switcher/subscription.json").read_text())
        self.assertEqual(manifest["expected_ip"], "8.8.8.8")
        self.assertEqual(manifest["subscription_mode"], "legacy-static")

    def test_legacy_compatibility_does_not_accept_untrusted_host(self):
        url = "https://other.example/sub/" + "a" * 43 + "/vpn-host1.yaml"
        self.assertEqual(self.run_cli("subscription", "set", stdin=url + "\n").returncode, 0)
        pgrep = self.bin / "pgrep"
        pgrep.write_text("#!/bin/sh\nexit 0\n")
        pgrep.chmod(0o755)
        ifconfig = self.bin / "ifconfig"
        ifconfig.write_text("#!/bin/sh\nprintf 'inet 198.18.0.2\\n'\n")
        ifconfig.chmod(0o755)
        curl = self.bin / "curl"
        curl.write_text("#!/bin/sh\nwhile [ \"$#\" -gt 0 ]; do case \"$1\" in -D) h=$2; shift 2;; -o) b=$2; shift 2;; *) shift;; esac; done; : > \"$h\"; printf 'proxies:\\n' > \"$b\"\n")
        curl.chmod(0o755)
        result = self.run_cli("subscription", "fetch")
        self.assertEqual(result.returncode, 1)
        self.assertIn("不符合旧订阅兼容规则", result.stdout)

    def test_status_is_sanitized_and_clear_removes_local_secret(self):
        url = "https://sub.example.com/s/cs_private-token-123456"
        self.assertEqual(self.run_cli("subscription", "set", stdin=url + "\n").returncode, 0)
        result = self.run_cli("subscription", "status")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("private-token", result.stdout)
        self.assertIn("subscription=warn:待同步", result.stdout)
        result = self.run_cli("subscription", "clear")
        self.assertEqual(result.returncode, 0)
        self.assertFalse((self.home / ".claude-switcher/subscription.json").exists())
        self.assertFalse((self.home / ".claude-switcher/subscription.yaml").exists())

    def test_install_writes_a_private_clash_profile(self):
        base = self.home / ".claude-switcher"
        base.mkdir(parents=True)
        (base / "subscription.yaml").write_text("proxies:\n  - name: private\n")
        result = self.run_cli("subscription", "install")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        profile = self.home / "Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/profiles/claude-switcher-subscription.yaml"
        self.assertEqual(stat.S_IMODE(profile.stat().st_mode), 0o600)
        self.assertIn("proxies:", profile.read_text())

    def test_check_fails_closed_when_current_egress_differs_from_binding(self):
        manifest = self.home / ".claude-switcher/subscription.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"url": "https://sub.example.com/s/token",
                                        "expected_ip": "198.51.100.10", "updated_at": "now"}))
        pgrep = self.bin / "pgrep"
        pgrep.write_text("#!/bin/sh\nexit 0\n")
        pgrep.chmod(0o755)
        ifconfig = self.bin / "ifconfig"
        ifconfig.write_text("#!/bin/sh\nprintf 'inet 198.18.0.2\\n'\n")
        ifconfig.chmod(0o755)
        curl = self.bin / "curl"
        curl.write_text("#!/bin/sh\nprintf 'ip=198.51.100.10\\n'\n")
        curl.chmod(0o755)
        result = self.run_cli("subscription", "check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("subscription_check=ok", result.stdout)
        curl.write_text("#!/bin/sh\nprintf 'ip=198.51.100.11\\n'\n")
        result = self.run_cli("subscription", "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("subscription_check=fail", result.stdout)


if __name__ == "__main__":
    unittest.main()
