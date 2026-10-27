from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('subscription_acceptance', ROOT / 'deploy/probe_mihomo_subscription.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def subscription():
    return {'mixed-port': 7890, 'allow-lan': False, 'ipv6': False, 'mode': 'rule',
            'proxies': [{'name': 'user-gateway', 'type': 'vless', 'server': 'gateway.example',
                         'port': 443, 'uuid': 'fixture-private-identity', 'tls': True,
                         'servername': 'gateway.example', 'network': 'tcp', 'udp': False,
                         'skip-cert-verify': False}],
            'proxy-groups': [{'name': 'Residential', 'type': 'select', 'proxies': ['user-gateway']}],
            'rules': ['NETWORK,udp,REJECT',
                      'DOMAIN-SUFFIX,claude.ai,Residential',
                      'DOMAIN-SUFFIX,anthropic.com,Residential',
                      'DOMAIN-SUFFIX,claude.com,Residential',
                      'DOMAIN-SUFFIX,clau.de,Residential',
                      'DOMAIN-SUFFIX,claudeusercontent.com,Residential',
                      'MATCH,Residential'],
            'dns': {'enable': True, 'ipv6': False, 'enhanced-mode': 'fake-ip', 'respect-rules': True,
                    'nameserver': ['https://1.1.1.1/dns-query#Residential'],
                    'default-nameserver': ['https://1.1.1.1/dns-query#DIRECT'],
                    'proxy-server-nameserver': ['https://1.1.1.1/dns-query#DIRECT'],
                    'prefer-h3': False, 'use-hosts': False, 'use-system-hosts': False}}


class SubscriptionAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.config = self.directory / 'private.yaml'
        self.manifest = self.directory / 'manifest.json'
        self.config.write_text(json.dumps(subscription()))
        self.manifest.write_text(json.dumps({'expected_ip': '8.8.8.8', 'url': 'https://private.example/token'}))
        self.config.chmod(0o600)
        self.manifest.chmod(0o600)
        self.core = self.directory / 'fake-core'
        self.core.write_text('''#!/usr/bin/env python3
import json, os, socket, struct, sys, threading
config=json.load(sys.stdin)
if '-t' in sys.argv:
    assert config['tun']['enable'] is False
    assert config['allow-lan'] is False
    assert config['external-controller'] == ''
    assert config['bind-address'] == '127.0.0.1'
    assert config['proxy-groups'][0]['proxies'][0] == os.environ.get('FIXTURE_SELECTED', 'user-gateway')
    sys.exit(0)
with open(os.environ['FIXTURE_PID'], 'w') as handle: handle.write(str(os.getpid()))
tcp=socket.socket(); tcp.bind(('127.0.0.1',config['mixed-port'])); tcp.listen(8)
udp=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); udp.bind(('127.0.0.1',int(config['dns']['listen'].split(':')[-1])))
def answer():
    while True:
        query, target=udp.recvfrom(4096)
        kind=struct.unpack('!H',query[-4:-2])[0]
        soa_mode=os.environ.get('FIXTURE_SOA_MODE','success')
        count=1 if kind in {1,6} else 0
        flags=0x8180
        if kind == 6 and soa_mode != 'success':
            count=0
            if soa_mode == 'nxdomain': flags=0x8183
        body=query[:2]+struct.pack('!HHHHH',flags,1,count,0,0)+query[12:]
        if kind == 1: body+=b'\\xc0\\x0c'+struct.pack('!HHIH',1,1,60,4)+socket.inet_aton('198.18.0.1')
        if kind == 6 and count:
            data=b'\\xc0\\x0c\\xc0\\x0c'+struct.pack('!IIIII',1,3600,600,86400,60)
            body+=b'\\xc0\\x0c'+struct.pack('!HHIH',6,1,60,len(data))+data
        udp.sendto(body,target)
threading.Thread(target=answer,daemon=True).start()
while True:
    connection,_=tcp.accept(); connection.close()
''')
        self.core.chmod(0o755)
        self.curl = self.directory / 'curl'
        self.curl.write_text('''#!/usr/bin/env python3
import os, sys
config=sys.stdin.read()
assert 'socks5h://127.0.0.1:' in config
assert not any(key in os.environ for key in ['HTTPS_PROXY','ALL_PROXY','HTTP_PROXY'])
if 'cloudflare.com' in config:
    print('ip='+os.environ.get('FIXTURE_EGRESS','8.8.8.8'))
    print('200',end='')
else: print('204',end='')
''')
        self.curl.chmod(0o755)
        self.env = {**os.environ, 'PATH': str(self.directory) + os.pathsep + os.environ['PATH'],
                    'FIXTURE_PID': str(self.directory / 'pid'), 'HTTPS_PROXY': 'http://fixture-secret:1080'}

    def tearDown(self):
        self.temp.cleanup()

    def run_probe(self):
        with patch.dict(os.environ, self.env, clear=True):
            return probe.probe(self.config, self.manifest, self.core, allow_network=True)

    def test_opt_in_is_required_before_files_or_network_are_touched(self):
        with patch.object(probe, 'private_document', side_effect=AssertionError('must not read')):
            with self.assertRaisesRegex(probe.ProbeError, 'explicit_allow_network_required'):
                probe.probe(self.config, self.manifest, self.core)

    @unittest.skipUnless(importlib.util.find_spec('yaml'), 'optional PyYAML dependency')
    def test_real_entrypoint_fake_transport_checks_binding_dns_and_reaps_core(self):
        result = self.run_probe()
        self.assertTrue(result['passed'], result)
        self.assertTrue(result['dns_example_fake_ip'])
        self.assertTrue(result['dns_ipv6_suppressed'])
        self.assertTrue(result['dns_business_resolver_soa'])
        self.assertEqual(result['dns_leak_free'], 'unverified')
        self.assertFalse(result['claude_services_requested'])
        self.assertNotIn('8.8.8.8', json.dumps(result))
        self.assertNotIn('fixture-private', json.dumps(result))
        pid = int((self.directory / 'pid').read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual(json.loads(self.config.read_text()), subscription())
        self.assertEqual(set(self.directory.iterdir()), {self.config, self.manifest, self.core, self.curl, self.directory / 'pid'})

    @unittest.skipUnless(importlib.util.find_spec('yaml'), 'optional PyYAML dependency')
    def test_backup_can_be_selected_explicitly_without_claiming_other_node_passed(self):
        document = subscription()
        backup = {**document['proxies'][0], 'name': 'backup', 'network': 'ws',
                  'ws-opts': {'path': '/managed', 'headers': {'Host': 'gateway.example'}}}
        document['proxies'].append(backup)
        document['proxy-groups'][0]['proxies'].append('backup')
        self.config.write_text(json.dumps(document))
        self.env['FIXTURE_SELECTED'] = 'backup'
        with patch.dict(os.environ, self.env, clear=True):
            result = probe.probe(self.config, self.manifest, self.core, allow_network=True, node_index=1)
        self.assertTrue(result['passed'], result)
        self.assertEqual(result['tested_node_index'], 1)
        self.assertEqual(result['untested_node_count'], 1)
        with self.assertRaisesRegex(probe.ProbeError, 'invalid_node_index'):
            probe.probe(self.config, self.manifest, self.core, allow_network=True, node_index=2)

    @unittest.skipUnless(importlib.util.find_spec('yaml'), 'optional PyYAML dependency')
    def test_binding_mismatch_fails_without_disclosing_ip(self):
        self.env['FIXTURE_EGRESS'] = '1.1.1.1'
        result = self.run_probe()
        self.assertFalse(result['passed'])
        self.assertFalse(result['egress_matches_binding'])
        self.assertNotIn('1.1.1.1', json.dumps(result))

    def test_yaml_dependency_is_explicit_and_not_auto_installed(self):
        import builtins
        original = builtins.__import__
        def without_yaml(name, *args, **kwargs):
            if name == 'yaml':
                raise ImportError('fixture')
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=without_yaml):
            with self.assertRaisesRegex(probe.ProbeError, 'missing_dependency_pyyaml'):
                probe.private_document(self.config, yaml_format=True)

    @unittest.skipUnless(importlib.util.find_spec('yaml'), 'optional PyYAML dependency')
    def test_dns_upstream_soa_failure_prevents_acceptance(self):
        for mode in ['empty', 'nxdomain']:
            with self.subTest(mode=mode):
                self.env['FIXTURE_SOA_MODE'] = mode
                result = self.run_probe()
                self.assertTrue(result['dns_example_fake_ip'])
                self.assertTrue(result['dns_ipv6_suppressed'])
                self.assertFalse(result['dns_business_resolver_soa'])
                self.assertFalse(result['passed'])
                self.assertEqual(result['dns_leak_free'], 'unverified')

    def test_source_with_side_effect_capabilities_is_rejected(self):
        for key, value in [('tun', {'enable': True}), ('external-controller', '0.0.0.0:9090'),
                           ('proxy-providers', {}), ('listeners', []), ('script', {}),
                           ('external-ui', '/tmp'), ('hosts', {})]:
            with self.subTest(key=key):
                config = subscription()
                config[key] = value
                with self.assertRaisesRegex(probe.ProbeError, 'unsupported_subscription_fields'):
                    probe.validate_subscription(config)

    def test_direct_fallback_insecure_tls_and_unmanaged_udp_are_rejected(self):
        changes = [('bind', lambda c: c.update({'bind-address': '0.0.0.0'})),
                   ('proxies', lambda c: c['proxies'][0].update({'skip-cert-verify': True})),
                   ('udp', lambda c: c['proxies'][0].update({'udp': True})),
                   ('group', lambda c: c['proxy-groups'][0]['proxies'].append('DIRECT')),
                   ('reserved-group', lambda c: c['proxy-groups'][0].update({'name': 'DIRECT'})),
                   ('rules', lambda c: c.update({'rules': ['MATCH,DIRECT']})),
                   ('dns', lambda c: c['dns'].update({'respect-rules': False}))]
        for name, mutate in changes:
            with self.subTest(name=name):
                document = subscription()
                mutate(document)
                with self.assertRaises(probe.ProbeError):
                    probe.validate_subscription(document)

    def test_application_dns_cannot_fall_back_to_direct_or_system(self):
        for value in ['system', 'https://1.1.1.1/dns-query', 'https://1.1.1.1/dns-query#DIRECT',
                      'https://1.1.1.1/dns-query#RULES', 'https://dns.example/dns-query#Residential']:
            with self.subTest(value=value):
                document = subscription()
                document['dns']['nameserver'] = [value]
                with self.assertRaisesRegex(probe.ProbeError, 'fixed_route_doh_required'):
                    probe.validate_subscription(document)
        for key in ['default-nameserver', 'proxy-server-nameserver']:
            document = subscription()
            document['dns'][key] = ['https://dns.example/dns-query#DIRECT']
            with self.assertRaisesRegex(probe.ProbeError, 'fixed_route_doh_required'):
                probe.validate_subscription(document)
        document = subscription()
        document['dns']['nameserver-policy'] = {'+.example.com': ['system']}
        with self.assertRaisesRegex(probe.ProbeError, 'managed_dns_policy_required'):
            probe.validate_subscription(document)

    @unittest.skipUnless(importlib.util.find_spec('yaml'), 'optional PyYAML dependency')
    def test_private_permissions_and_missing_binding_fail_before_core(self):
        self.config.chmod(0o644)
        with self.assertRaisesRegex(probe.ProbeError, 'private_file_permissions_required'):
            self.run_probe()
        self.config.chmod(0o600)
        self.manifest.write_text('{}')
        with self.assertRaisesRegex(probe.ProbeError, 'valid_binding_required'):
            self.run_probe()
        self.assertFalse((self.directory / 'pid').exists())

    @unittest.skipUnless(importlib.util.find_spec('yaml'), 'optional PyYAML dependency')
    def test_network_failure_reaps_core(self):
        with patch.object(probe, 'curl_request', side_effect=subprocess.TimeoutExpired('sensitive', 14)):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.run_probe()
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.directory / 'pid').read_text()), 0)

    @unittest.skipUnless(importlib.util.find_spec('yaml'), 'optional PyYAML dependency')
    def test_syntax_failure_never_starts_core(self):
        self.core.write_text('#!/bin/sh\nexit 1\n')
        result = self.run_probe()
        self.assertFalse(result['passed'])
        self.assertFalse(result['config_valid'])
        self.assertFalse((self.directory / 'pid').exists())

    def test_cli_errors_are_sanitized(self):
        with patch.object(probe, 'probe', side_effect=OSError('fixture-private-identity')), contextlib.redirect_stdout(io.StringIO()) as output:
            status = probe.main(['--subscription', str(self.config), '--manifest', str(self.manifest),
                                 '--core', str(self.core), '--allow-network'])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue()), {'passed': False, 'error': 'probe_failed'})


if __name__ == '__main__':
    unittest.main()
