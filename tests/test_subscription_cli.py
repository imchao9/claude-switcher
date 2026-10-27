"""Isolated subscription CLI tests; no real proxy or install state is touched."""

from __future__ import annotations

import json
import hashlib
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

    def legacy_fixture(self, *, trusted=True, text=None):
        url = "https://sub.610247.xyz/sub/" + "a" * 43 + "/vpn-host1.yaml"
        # 生产只允许用户指定的 URL 摘要；隔离副本允许合成令牌。
        self.installed.write_text(self.installed.read_text().replace(
            "30692aa2b68b8561cec027dff22af7c1d8fd010e6cc687c89ed2be395d42a0c1",
            hashlib.sha256(url.encode()).hexdigest()))
        if not trusted:
            url = url.replace("sub.610247.xyz", "other.example")
        self.assertEqual(self.run_cli("subscription", "set", stdin=url + "\n").returncode, 0)
        for name, contents in (("pgrep", "exit 0"), ("ifconfig", "printf 'inet 198.18.0.2\\n'")):
            tool = self.bin / name
            tool.write_text("#!/bin/sh\n" + contents + "\n")
            tool.chmod(0o755)
        fixture = self.root / "legacy.yaml"
        fixture.write_text(text if text is not None else (ROOT / "server/examples/legacy-subscription.yaml").read_text())
        curl = self.bin / "curl"
        curl.write_text("""#!/bin/sh
while [ "$#" -gt 0 ]; do
  case "$1" in
    -D) h=$2; shift 2 ;;
    -o) b=$2; shift 2 ;;
    *) shift ;;
  esac
done
: > "$h"
cp '__FIXTURE__' "$b"
""".replace("__FIXTURE__", str(fixture)))
        curl.chmod(0o755)
        return fixture

    def normalized_legacy(self):
        # 输出的顶层字段使用 JSON flow 值，无需为客户端引入 YAML 依赖。
        text = (self.home / ".claude-switcher/subscription.yaml").read_text()
        document = {line.split(": ", 1)[0]: json.loads(line.split(": ", 1)[1])
                    for line in text.splitlines() if line and line[0] != " " and ": " in line}
        document["proxies"] = [json.loads(line[4:]) for line in text.splitlines() if line.startswith("  - ")]
        return document

    def test_legacy_static_subscription_normalizes_actual_old_layout(self):
        self.legacy_fixture()
        result = self.run_cli("subscription", "fetch")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("旧静态订阅兼容模式", result.stdout)
        manifest = json.loads((self.home / ".claude-switcher/subscription.json").read_text())
        self.assertEqual(manifest["expected_ip"], "8.8.8.8")
        self.assertEqual(manifest["subscription_mode"], "legacy-static")
        doc = self.normalized_legacy()
        group = doc["proxy-groups"][0]
        self.assertEqual(group, {"name": "CLAUDE-SWITCHER", "type": "select",
                                "proxies": ["旧订阅住宅-Reality", "旧订阅住宅-WS"]})
        nodes = {node["name"]: node for node in doc["proxies"]}
        for name, entry in zip(group["proxies"], ("vpn-host1", "vpn-host1-iOS-WS")):
            self.assertEqual(nodes[name]["server"], "8.8.8.8")
            self.assertEqual(nodes[name]["dialer-proxy"], entry)
            self.assertEqual(nodes[name]["password"], "fixture-password")
        self.assertTrue(all(n["udp"] is False for n in nodes.values()))
        self.assertEqual(doc["rules"][0], "NETWORK,udp,REJECT")
        self.assertEqual(len(doc["rules"]), 7)
        self.assertEqual(doc["rules"][-1], "MATCH,CLAUDE-SWITCHER")
        self.assertFalse(any("DIRECT" in rule for rule in doc["rules"]))
        self.assertEqual(doc["dns"]["nameserver"], ["https://1.1.1.1/dns-query#CLAUDE-SWITCHER"])
        self.assertNotIn("fallback", doc["dns"])
        self.assertNotIn("direct-nameserver", doc["dns"])
        self.assertNotIn("fixture-password", result.stdout + result.stderr)
        self.assertEqual(stat.S_IMODE((self.home / ".claude-switcher/subscription.yaml").stat().st_mode), 0o600)

    def test_legacy_compatibility_accepts_indented_lists_and_quoted_secrets(self):
        fixture = self.legacy_fixture()
        text = fixture.read_text()
        start = text.index("proxies:\n") + len("proxies:\n")
        end = text.index("mixed-port:")
        text = text[:start] + "\n".join("  " + line for line in text[start:end].splitlines()) + "\n" + text[end:]
        text = text.replace("password: fixture-password", "password: 'fixture: # password''value'")
        fixture.write_text(text)
        result = self.run_cli("subscription", "fetch")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        doc = self.normalized_legacy()
        self.assertEqual(doc["proxies"][-1]["password"], "fixture: # password'value")

    def test_legacy_compatibility_rejects_duplicate_top_level_proxies(self):
        fixture = self.legacy_fixture()
        fixture.write_text(fixture.read_text().replace("proxies:", "proxies: []\nproxies:", 1))
        self.assertEqual(self.run_cli("subscription", "fetch").returncode, 1)
        self.assertFalse((self.home / ".claude-switcher/subscription.yaml").exists())

    def test_legacy_compatibility_rejects_other_urls(self):
        self.legacy_fixture()
        urls = ["https://other.example/sub/" + "a" * 43 + "/vpn-host1.yaml",
                "https://sub.610247.xyz/sub/" + "b" * 43 + "/vpn-host1.yaml",
                "https://sub.610247.xyz/sub/" + "a" * 43 + "/vpn-host1.yaml?format=clash"]
        for url in urls:
            with self.subTest(url_kind=url.split("/")[2]):
                self.run_cli("subscription", "set", stdin=url + "\n")
                result = self.run_cli("subscription", "fetch")
                self.assertEqual(result.returncode, 1)
                self.assertIn("不符合旧订阅兼容规则", result.stdout)
                self.assertFalse((self.home / ".claude-switcher/subscription.yaml").exists())

    def test_legacy_compatibility_fails_closed_on_unsupported_nodes(self):
        fixture = self.legacy_fixture()
        original = fixture.read_text()
        changes = [
            ("server: 8.8.8.8", "server: 127.0.0.1"),
            ("type: socks5", "type: direct"),
            ("dialer-proxy: vpn-host1", "dialer-proxy: DIRECT"),
            ("skip-cert-verify: false", "skip-cert-verify: true"),
            ("tls: true", "tls: false"),
            ("  password: fixture-password", "  password: fixture-password\n  password: other"),
            ("  password: fixture-password", "  password: *credential"),
            ("  password: fixture-password", "  password: !tag credential"),
            ("  type: socks5", "  type: socks5\n  unknown-option: true"),
            ("proxies:", "proxies: []\nproxies:"),
            ("name: vpn-host1-iOS-WS", "name: vpn-host1"),
        ]
        for old, new in changes:
            with self.subTest(field=old.split(":")[0].strip()):
                fixture.write_text(original.replace(old, new))
                result = self.run_cli("subscription", "fetch")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertFalse((self.home / ".claude-switcher/subscription.yaml").exists())
                self.assertNotIn("fixture-password", result.stdout + result.stderr)
        fixture.write_text(original)
        self.assertEqual(self.run_cli("subscription", "fetch").returncode, 0)
        saved = (self.home / ".claude-switcher/subscription.yaml").read_bytes()
        fixture.write_text(original.replace("server: 8.8.8.8", "server: 127.0.0.1"))
        self.assertEqual(self.run_cli("subscription", "fetch").returncode, 1)
        self.assertEqual((self.home / ".claude-switcher/subscription.yaml").read_bytes(), saved)

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
