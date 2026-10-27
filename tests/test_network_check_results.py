import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest


CORE = Path(__file__).resolve().parents[1] / "core/claude-switch.sh"


def function(name):
    match = re.search(r"^" + re.escape(name) + r"\(\) \{\n.*?^\}\n", CORE.read_text(), re.M | re.S)
    if match is None:
        raise AssertionError("missing shell function: " + name)
    return match.group()


class NetworkResultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "profiles").mkdir()
        self.env = dict(os.environ, HOME=str(self.root), LC_ALL="C", LANG="C")

    def tearDown(self):
        self.temp.cleanup()

    def run_shell(self, source):
        setup = "BASE=" + shlex.quote(str(self.root)) + '\nCLASH_DIR="$BASE"\n'
        return subprocess.run(["/bin/bash", "-c", setup + source], capture_output=True,
                              text=True, timeout=10, env=self.env)

    def run_public(self, name, probes, traces=None, overrides=""):
        for pattern in ("probe-*", "trace-*", "curl-count", "events"):
            for old in self.root.glob(pattern):
                old.unlink()
        if traces is None:
            traces = [("198.51.100.10", 0)]
        for n, (value, status) in enumerate(probes, 1):
            (self.root / f"probe-{n}").write_text(value)
            (self.root / f"probe-status-{n}").write_text(str(status))
        for n, (ip, status) in enumerate(traces, 1):
            (self.root / f"trace-{n}").write_text(f"ip={ip}\nloc=US\n")
            (self.root / f"trace-status-{n}").write_text(str(status))
        definitions = function(name)
        if "webrtc_result()" in CORE.read_text():
            definitions += function("webrtc_result")
        definitions += function("timezone_valid")
        mocks = r'''
PROXY_MODE=clash-verge
PROXY_GROUP=Proxy
COUNTRY_TARGET=US
TZ_TARGET=America/New_York
LOCALE_TARGET=en_US
proxy_alive() { return 0; }
proxy_name() { printf 'Clash'; }
proxy_app_name() { printf 'Clash'; }
tun_up() { return 0; }
merge_ok() { return 0; }
stun_ok() { return 0; }
route_ok() { return 0; }
subscription_ip_ok() { return 0; }
iphist_count() { printf '1'; }
pgrep() { return 0; }
launchctl() { return 0; }
sleep() { :; }
sudo() { printf 'unexpected sudo\n' >> "$BASE/events"; return 99; }
open() { printf 'open\n' >> "$BASE/events"; }
osascript() { printf 'osascript\n' >> "$BASE/events"; }
write_merge_rules() { printf 'write_merge_rules\n' >> "$BASE/events"; }
readlink() { printf '/var/db/timezone/zoneinfo/America/New_York'; }
networksetup() { printf 'IPv6: Off\n'; }
defaults() {
    case "$*" in
        *AppleLocale*) printf 'en_US\n' ;;
        *) printf '(\n en-US\n)\n' ;;
    esac
}
dig() { printf '198.18.0.1\n'; }
curl() {
    case "$*" in
        *cdn-cgi/trace*)
            n=$(cat "$BASE/curl-count" 2>/dev/null || printf '0')
            n=$((n + 1)); printf '%s' "$n" > "$BASE/curl-count"
            if [ -f "$BASE/trace-$n" ]; then
                cat "$BASE/trace-$n"
                return "$(cat "$BASE/trace-status-$n")"
            fi
            printf 'ip=198.51.100.10\nloc=US\n'
            ;;
        *ipinfo.io*) printf '{"org":"AS7922 Comcast"}\n' ;;
        *) printf '200' ;;
    esac
}
webrtc_test() {
    n=$(cat "$BASE/probe-count" 2>/dev/null || printf '0')
    n=$((n + 1)); printf '%s' "$n" > "$BASE/probe-count"
    cat "$BASE/probe-$n"
    return "$(cat "$BASE/probe-status-$n")"
}
'''
        return self.run_shell(definitions + mocks + overrides + "\n" + name)

    def test_system_timezone_mismatch_reports_unverified_process_request(self):
        result = self.run_public('checkdetail', [('RESULT:198.51.100.10', 0)],
                                 overrides='readlink() { return 99; }\nTZ_TARGET=America/Chicago\n')
        rows = result.stdout.splitlines()
        self.assertEqual(len(rows), 14, result.stdout)
        self.assertIn('时区=warn:启动时请求 TZ=America/Chicago；客户端实际生效未验证，系统时区由用户设置', rows)
        self.assertFalse(any(row.startswith('时区=fail') for row in rows))

    def test_fix_missing_reference_cannot_pass(self):
        result = self.run_public("fix", [("RESULT:198.51.100.20", 0)], [("", 0)])
        self.assertNotIn("FIX:WebRTC ✓", result.stdout)
        self.assertIn("FIX:WebRTC ✗", result.stdout)

    def test_fix_empty_retest_cannot_pass(self):
        result = self.run_public("fix", [("RESULT:198.51.100.20", 0), ("", 0)])
        self.assertNotIn("FIX:WebRTC ✓", result.stdout)
        self.assertIn("FIX:WebRTC ✗", result.stdout)

    def test_unloaded_direct_stun_rule_cannot_pass(self):
        (self.root / "profiles/Merge.yaml").write_text("prepend-rules:\n  - DST-PORT,3478,DIRECT\n")
        result = self.run_shell("PROXY_GROUP=Proxy\n" + function("stun_ok") + "stun_ok")
        self.assertNotEqual(result.returncode, 0)

    def classify(self, raw, status=0, expected="198.51.100.10"):
        args = " ".join(shlex.quote(str(value)) for value in (raw, status, expected))
        result = self.run_shell(function("webrtc_result") + "webrtc_result " + args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_classifier_matched_ipv4_and_equivalent_ipv6(self):
        self.assertEqual(self.classify("RESULT:198.51.100.10,198.51.100.10"), "matched")
        self.assertEqual(self.classify("RESULT:2001:db8::1", expected="2001:0db8::1"), "matched")

    def test_classifier_any_different_address_is_leak(self):
        self.assertEqual(self.classify("RESULT:198.51.100.10,198.51.100.20"), "leak")
        self.assertEqual(self.classify("RESULT:2001:db8::1"), "leak")

    def test_classifier_no_response_is_distinct(self):
        self.assertEqual(self.classify("RESULT:NONE"), "no_response")
        self.assertEqual(self.classify("RESULT:NONE", expected=""), "no_response")
        self.assertEqual(self.classify("RESULT:NONE", status=1), "unknown")

    def test_classifier_rejects_malformed_output(self):
        values = ["", "RESULT:", "garbage", "RESULT:garbage", "RESULT:198.51.100.10,",
                  "RESULT:,198.51.100.10", "RESULT:198.51.100.10,,198.51.100.10",
                  "RESULT:198.51.100.10\nRESULT:198.51.100.10", "RESULT:198.51.100.10\n",
                  "RESULT:198.51.100.10 ", " RESULT:198.51.100.10", "RESULT:NONE,198.51.100.10"]
        for raw in values:
            with self.subTest(raw=raw):
                self.assertEqual(self.classify(raw), "unknown")

    def test_classifier_rejects_failed_probe_and_invalid_reference(self):
        for status, reference in [(1, "198.51.100.10"), (127, "198.51.100.10"),
                                  (0, ""), (0, "garbage"), (0, "198.51.100.10\n")]:
            with self.subTest(status=status, reference=reference):
                self.assertEqual(self.classify("RESULT:198.51.100.10", status, reference), "unknown")

    def test_public_checks_only_accept_matched(self):
        cases = [("RESULT:198.51.100.10", 0, True), ("RESULT:NONE", 0, False),
                 ("", 0, False), ("RESULT:", 0, False), ("wrong", 0, False),
                 ("RESULT:198.51.100.10", 1, False), ("RESULT:198.51.100.20", 0, False)]
        for name in ("check", "checkdetail", "fix"):
            for raw, status, accepted in cases:
                with self.subTest(name=name, raw=raw, status=status):
                    result = self.run_public(name, [(raw, status)], overrides="PROXY_MODE=surge\n")
                    if name == "check":
                        self.assertEqual(result.returncode == 0, accepted, result.stdout + result.stderr)
                    elif name == "checkdetail":
                        self.assertEqual("WebRTC=ok:" in result.stdout, accepted, result.stdout)
                        self.assertEqual(len(result.stdout.splitlines()), 14, result.stdout)
                    else:
                        self.assertEqual("FIX:WebRTC ✓" in result.stdout, accepted, result.stdout)
                    messages = [line for line in result.stdout.splitlines() if "WebRTC" in line]
                    self.assertNotIn("198.51.100.20", "\n".join(messages))
                    self.assertNotIn("零暴露", result.stdout)

    def test_public_checks_reject_failed_reference_even_with_valid_body(self):
        for name in ("check", "checkdetail", "fix"):
            with self.subTest(name=name):
                result = self.run_public(name, [("RESULT:198.51.100.10", 0)], [("198.51.100.10", 28)])
                self.assertNotIn("WebRTC=ok", result.stdout)
                self.assertNotIn("FIX:WebRTC ✓", result.stdout)
                if name == "check":
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("WebRTC", result.stdout)

    def test_public_checks_reject_unavailable_classifier(self):
        for name in ("check", "checkdetail", "fix"):
            with self.subTest(name=name):
                result = self.run_public(name, [("RESULT:198.51.100.10", 0)],
                                         overrides="python3() { return 127; }\n")
                self.assertNotIn("WebRTC=ok", result.stdout)
                self.assertNotIn("FIX:WebRTC ✓", result.stdout)
                if name == "check":
                    self.assertNotEqual(result.returncode, 0)

    def test_fix_reloads_only_confirmed_clash_tun_leak(self):
        for raw, status, override, reloads in [
            ("RESULT:198.51.100.20", 0, "", True),
            ("RESULT:NONE", 0, "", False), ("RESULT:", 0, "", False),
            ("RESULT:198.51.100.20", 1, "", False),
            ("RESULT:198.51.100.20", 0, "PROXY_MODE=surge", False),
            ("RESULT:198.51.100.20", 0, "tun_up() { return 1; }", False),
        ]:
            with self.subTest(raw=raw, status=status, override=override):
                self.run_public("fix", [(raw, status), ("RESULT:198.51.100.10", 0)], overrides=override)
                events = self.root / "events"
                self.assertEqual(events.exists(), reloads)
                if reloads:
                    self.assertEqual(events.read_text().splitlines(), ["osascript", "open"])

    def test_fix_retry_uses_new_reference(self):
        result = self.run_public("fix", [("RESULT:198.51.100.20", 0), ("RESULT:198.51.100.30", 0)],
                                 [("198.51.100.10", 0), ("198.51.100.30", 0)])
        self.assertIn("FIX:WebRTC ✓", result.stdout)
        self.assertEqual((self.root / "curl-count").read_text(), "3")

    def test_fix_retry_does_not_accept_old_reference(self):
        result = self.run_public("fix", [("RESULT:198.51.100.20", 0), ("RESULT:198.51.100.10", 0)],
                                 [("198.51.100.10", 0), ("198.51.100.30", 0)])
        self.assertNotIn("FIX:WebRTC ✓", result.stdout)
        self.assertIn("FIX:WebRTC ✗", result.stdout)

    def test_fix_retry_failed_reference_or_probe_cannot_pass(self):
        for ip, trace_status, raw, probe_status in [
            ("", 0, "RESULT:198.51.100.10", 0),
            ("198.51.100.10", 28, "RESULT:198.51.100.10", 0),
            ("198.51.100.10", 0, "RESULT:198.51.100.10", 1),
            ("198.51.100.10", 0, "RESULT:NONE", 0),
            ("198.51.100.10", 0, "RESULT:", 0),
        ]:
            with self.subTest(ip=ip, trace_status=trace_status, raw=raw, probe_status=probe_status):
                result = self.run_public("fix", [("RESULT:198.51.100.20", 0), (raw, probe_status)],
                                         [("198.51.100.10", 0), (ip, trace_status)])
                self.assertNotIn("FIX:WebRTC ✓", result.stdout)
                self.assertIn("FIX:WebRTC ✗", result.stdout)

    def stun(self, text, group="Proxy"):
        (self.root / "clash-verge.yaml").write_text(text)
        return self.run_shell("PROXY_GROUP=" + shlex.quote(group) + "\n" + function("stun_ok") + "stun_ok")

    def rules(self, group="Proxy", indent="  ", quote=""):
        values = [f"DST-PORT,{port},{group}" for port in (3478, 5349, 19302)]
        if quote == "'":
            values = ["'" + value.replace("'", "''") + "'" for value in values]
        elif quote == '"':
            import json
            values = [json.dumps(value, ensure_ascii=False) for value in values]
        return "rules:\n" + "".join(indent + "- " + value + "\n" for value in values)

    def test_stun_accepts_generated_indented_and_indentless_sequences(self):
        for indent in ("", "  "):
            with self.subTest(indent=indent):
                text = "mixed-port: 1080\n" + self.rules(indent=indent) + "dns:\n  enable: true\n"
                self.assertEqual(self.stun(text).returncode, 0)

    def test_stun_accepts_quoted_group_names_and_comments(self):
        for quote in ("", "'", '"'):
            with self.subTest(quote=quote):
                group = "住宅线路" if not quote else "住宅 #1's 线路"
                text = self.rules(group=group, quote=quote).replace("rules:", "rules: # generated")
                text += "  - MATCH,DIRECT # after the pinned prefix\n"
                self.assertEqual(self.stun(text, group).returncode, 0)

    def test_stun_rejects_missing_or_similarly_numbered_ports(self):
        text = self.rules()
        for wrong in [text.replace("  - DST-PORT,5349,Proxy\n", ""),
                      text.replace("3478,", "13478,"), text.replace("3478,", "3478-3479,"),
                      text.replace("3478,Proxy", "3478,Proxy,no-resolve")]:
            with self.subTest(text=wrong):
                self.assertNotEqual(self.stun(wrong).returncode, 0)

    def test_stun_rejects_builtin_target_policies(self):
        for group in ("DIRECT", "REJECT", "REJECT-DROP", "PASS"):
            with self.subTest(group=group):
                self.assertNotEqual(self.stun(self.rules(group=group), group).returncode, 0)

    def test_stun_requires_exact_policy(self):
        for policy in ("DIRECT", "REJECT", "Proxy extra", "Proxy2"):
            with self.subTest(policy=policy):
                self.assertNotEqual(self.stun(self.rules().replace("3478,Proxy", "3478," + policy)).returncode, 0)

    def test_stun_rejects_comment_nested_and_duplicate_rules(self):
        text = self.rules()
        cases = ["# " + text.replace("\n", "\n# "),
                 "profile:\n" + "\n".join("  " + line for line in text.splitlines()) + "\n",
                 text + self.rules(), text + "'rules': []\n", text + '"rules": []\n',
                 "rules: []\n" + text, "rules: &ports\n" + text,
                 text.replace("  - DST-PORT,5349", "    - DST-PORT,5349"),
                 text + "---\n" + text]
        for value in cases:
            with self.subTest(text=value):
                self.assertNotEqual(self.stun(value).returncode, 0)

    def test_stun_rejects_shadowing_and_unknown_prefixes(self):
        for prefix in ["DST-PORT,3478,DIRECT", "MATCH,DIRECT", "RULE-SET,all,DIRECT",
                       "IP-CIDR,0.0.0.0/0,DIRECT,no-resolve", "UNKNOWN,x,Proxy",
                       "AND,((NETWORK,UDP),(DST-PORT,3478)),DIRECT"]:
            with self.subTest(prefix=prefix):
                text = self.rules().replace("rules:\n", "rules:\n  - " + prefix + "\n")
                self.assertNotEqual(self.stun(text).returncode, 0)

    def test_stun_accepts_known_same_policy_prefix(self):
        prefix = "  - DOMAIN-SUFFIX,example.test,Proxy\n  - IP-CIDR,192.0.2.0/24,Proxy,no-resolve\n"
        self.assertEqual(self.stun(self.rules().replace("rules:\n", "rules:\n" + prefix)).returncode, 0)

    def test_stun_rejects_unsupported_yaml(self):
        for value in ["[DST-PORT,3478,Proxy]", "*shared", "!!str DST-PORT,3478,Proxy",
                      "|", "'DST-PORT,3478,Proxy'#bad", '"DST-PORT,3478,Proxy"#bad',
                      '"DST-PORT,3478,Proxy\\n"']:
            with self.subTest(value=value):
                text = self.rules().replace("DST-PORT,3478,Proxy", value)
                self.assertNotEqual(self.stun(text).returncode, 0)

    def test_public_clash_checks_require_stun_config(self):
        for name in ("check", "checkdetail"):
            with self.subTest(name=name):
                result = self.run_public(name, [("RESULT:198.51.100.10", 0)],
                                         overrides="stun_ok() { return 1; }")
                if name == "check":
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("STUN", result.stdout)
                else:
                    self.assertIn("规则置顶=fail:", result.stdout)
                    self.assertEqual(len(result.stdout.splitlines()), 14)

    def test_other_clients_keep_existing_route_check(self):
        for name in ("check", "checkdetail"):
            with self.subTest(name=name):
                result = self.run_public(name, [("RESULT:198.51.100.10", 0)],
                                         overrides="PROXY_MODE=surge\nstun_ok() { return 1; }")
                if name == "check":
                    self.assertEqual(result.returncode, 0, result.stdout)
                else:
                    self.assertIn("规则置顶=ok:", result.stdout)

    def test_fix_does_not_claim_rules_repaired_when_generated_ports_missing(self):
        result = self.run_public("fix", [("RESULT:198.51.100.10", 0)],
                                 overrides="stun_ok() { return 1; }")
        self.assertIn("FIX:置顶规则 ✗", result.stdout)
        self.assertNotIn("FIX:置顶规则 ✓", result.stdout)
        self.assertIn("write_merge_rules", (self.root / "events").read_text())


if __name__ == "__main__":
    unittest.main()
