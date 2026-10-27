"""Exercise downloaded managed subscriptions, including their real client parser."""
import base64
import copy
import http.client
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from server.gateway import managed_nodes, ws_identity
from server.subscription_server import DomainError, create_server, managed_clash, subscription_routing
from tests.test_reality_gateway import REALITY

try:
    import yaml
except ImportError:
    yaml = None


WS = {'address': 'edge.example', 'port': 443, 'server_name': 'edge.example',
      'host': 'origin.example', 'path': '/managed?transport=ws',
      'inbound_tag': 'managed-ws', 'tls_termination': 'external'}


class ManagedSubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.http = create_server(str(Path(self.temp.name) / 'db'), 'fixture-only-admin', port=0)
        self.store = self.http.store
        self.gateway = dict(REALITY, support_x25519mlkem768=True, ws_backup=copy.deepcopy(WS))
        self.server('one', self.gateway)
        self.server('two', REALITY)
        self.store.add_ip({'id': 'home', 'server_id': 'one', 'address': '8.8.4.4', 'port': 1337,
                           'protocol': 'socks5', 'config': {'username': 'private-fixture-user',
                                                          'password': 'private-fixture-password'}})
        self.created = self.store.create_user({'id': 'person', 'ip_id': 'home'}, 'https://fixture.invalid')
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.store.close()
        self.temp.cleanup()

    def server(self, identifier, gateway):
        return self.store.add_server({'id': identifier, 'name': identifier,
                                     'host': identifier + '.example', 'gateway': gateway})

    def download(self, fmt):
        connection = http.client.HTTPConnection(*self.http.server_address, timeout=5)
        connection.request('GET', '/s/' + self.created['token'] + '?format=' + fmt)
        response = connection.getresponse()
        body, headers = response.read(), dict(response.getheaders())
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(headers['X-Claude-Switcher-Egress-IP'], '8.8.4.4')
        for secret in ['private-fixture-user', 'private-fixture-password', '8.8.4.4', self.created['token']]:
            self.assertNotIn(secret.encode(), body)
        return body

    def download_with_query(self, query):
        connection = http.client.HTTPConnection(*self.http.server_address, timeout=5)
        connection.request('GET', '/s/' + self.created['token'] + '?' + query)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response, body

    def test_routing_query_is_explicit_and_strict_by_default(self):
        self.assertEqual(subscription_routing(''), 'strict')
        self.assertEqual(subscription_routing('format=clash&routing=domestic-direct'), 'domestic-direct')
        response, body = self.download_with_query('format=clash&routing=domestic-direct')
        self.assertEqual(response.status, 200)
        document = yaml.safe_load(body) if yaml else None
        if document:
            self.assertEqual(document['rules'][1], 'DOMAIN-SUFFIX,claude.ai,CLAUDE-SWITCHER')
            self.assertIn('GEOIP,CN,DIRECT,no-resolve', document['rules'])
        response, _ = self.download_with_query('format=clash&routing=unknown')
        self.assertEqual(response.status, 400)
        response, _ = self.download_with_query('format=clash&routing=strict&routing=domestic-direct')
        self.assertEqual(response.status, 400)
        response, _ = self.download_with_query('format=clash&unexpected=value')
        self.assertEqual(response.status, 400)
        response, _ = self.download_with_query('format=uri&routing=domestic-direct')
        self.assertEqual(response.status, 400)
        with self.assertRaisesRegex(DomainError, 'invalid subscription format'):
            self.store.subscription(self.created['token'], 'invalid')
        with self.assertRaisesRegex(DomainError, 'routing requires clash format'):
            self.store.subscription(self.created['token'], 'uri', routing_profile='domestic-direct')

    @unittest.skipUnless(yaml, 'optional PyYAML client parser')
    def test_downloaded_yaml_is_self_contained_and_has_only_managed_choices(self):
        document = yaml.safe_load(self.download('clash'))
        self.assertEqual(document['bind-address'], '127.0.0.1')
        self.assertFalse(document['allow-lan'])
        self.assertEqual(document['rules'], [
            'NETWORK,udp,REJECT',
            'DOMAIN-SUFFIX,claude.ai,CLAUDE-SWITCHER',
            'DOMAIN-SUFFIX,anthropic.com,CLAUDE-SWITCHER',
            'DOMAIN-SUFFIX,claude.com,CLAUDE-SWITCHER',
            'DOMAIN-SUFFIX,clau.de,CLAUDE-SWITCHER',
            'DOMAIN-SUFFIX,claudeusercontent.com,CLAUDE-SWITCHER',
            'MATCH,CLAUDE-SWITCHER'])
        self.assertNotIn('DIRECT', '\n'.join(document['rules']))
        domestic = managed_clash(managed_nodes({'id': 'person', 'ingress_uuid': 'b9303c18-3580-4757-8c75-50297d895d5b'}, {'host': 'one.example'}, self.gateway), routing_profile='domestic-direct')
        self.assertIn('GEOIP,CN,DIRECT,no-resolve', domestic['rules'])
        nodes = document['proxies']
        self.assertEqual([n['server'] for n in nodes], ['one.example', WS['address']])
        self.assertEqual(document['proxy-groups'], [{'name': 'CLAUDE-SWITCHER', 'type': 'select',
                                                   'proxies': [n['name'] for n in nodes]}])
        self.assertEqual(nodes[0]['reality-opts']['support-x25519mlkem768'], True)
        self.assertEqual(nodes[1]['ws-opts'], {'path': WS['path'], 'headers': {'Host': WS['host']}})
        self.assertNotIn('flow', nodes[1])
        self.assertNotIn('reality-opts', nodes[1])
        self.assertEqual(nodes[1]['uuid'], ws_identity(nodes[0]['uuid']))
        for node in nodes:
            self.assertTrue(node['tls'])
            self.assertFalse(node['skip-cert-verify'])
            self.assertFalse(node['udp'])
        dns = document['dns']
        self.assertEqual(dns['nameserver'], ['https://1.1.1.1/dns-query#CLAUDE-SWITCHER'])
        for key in ['default-nameserver', 'proxy-server-nameserver']:
            self.assertEqual(dns[key], ['https://1.1.1.1/dns-query#DIRECT'])
        for key in ['ipv6', 'prefer-h3', 'use-hosts', 'use-system-hosts']:
            self.assertIs(dns[key], False)
        self.assertEqual(dns['fake-ip-filter'], ['localhost', '+.local', '+.lan'])
        for forbidden in ['fallback', 'direct-nameserver', 'nameserver-policy']:
            self.assertNotIn(forbidden, dns)
        self.assertEqual(dns['listen'], '127.0.0.1:1053')
        self.assertTrue(dns['respect-rules'])
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('managed_probe_contract', root / 'deploy/probe_mihomo_subscription.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.validate_subscription(document), document)

    def test_uri_and_base64_downloads_have_both_identities_and_ws_query_encoding(self):
        uris = self.download('uri').decode().splitlines()
        encoded = base64.b64decode(self.download('base64')).decode().splitlines()
        self.assertEqual(uris, encoded)
        self.assertEqual(len(uris), 2)
        parsed = [urlsplit(uri) for uri in uris]
        self.assertEqual([uri.hostname for uri in parsed], ['one.example', WS['address']])
        self.assertEqual(parsed[1].username, ws_identity(parsed[0].username))
        query = parse_qs(parsed[1].query)
        self.assertEqual(query['path'], [WS['path']])
        self.assertEqual(query['host'], [WS['host']])
        self.assertEqual(query['security'], ['tls'])
        self.assertEqual(query['type'], ['ws'])
        self.assertEqual(query['allowInsecure'], ['0'])
        self.assertNotIn('flow', query)
        self.assertNotIn('pbk', query)
        merge = self.store.export_xray_config('one', merge=True)
        self.assertEqual(merge['clients'][0]['id'], parsed[0].username)
        self.assertEqual(merge['additional_inbounds'][0]['clients'][0]['id'], parsed[1].username)
        tcp_routes = [r for r in merge['routing_rules'] if r['network'] == 'tcp']
        self.assertEqual(len(tcp_routes), 2)
        self.assertEqual(len({r['outboundTag'] for r in tcp_routes}), 1)

    def test_unconfigured_server_and_removed_backup_return_one_node(self):
        user = {'id': 'fixture', 'ingress_uuid': 'b9303c18-3580-4757-8c75-50297d895d5b'}
        one = managed_nodes(user, {'host': 'two.example'}, REALITY)
        self.assertEqual(len(one), 1)
        self.assertNotIn('support-x25519mlkem768', one[0]['reality-opts'])
        self.server('one', REALITY)
        self.assertEqual(len(self.download('uri').decode().splitlines()), 1)
        self.server('one', dict(REALITY, support_x25519mlkem768=False))
        if yaml:
            self.assertIs(yaml.safe_load(self.download('clash'))['proxies'][0]['reality-opts']['support-x25519mlkem768'], False)

    def test_generated_document_is_accepted_by_actual_mihomo_without_network(self):
        binary = Path(os.environ.get('MIHOMO_BIN', '/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo'))
        if not binary.is_file():
            self.skipTest('optional local Mihomo executable')
        user = {'id': 'fixture', 'ingress_uuid': 'b9303c18-3580-4757-8c75-50297d895d5b'}
        document = managed_clash(managed_nodes(user, {'host': '192.0.2.1'}, self.gateway))
        result = subprocess.run([str(binary), '-d', self.temp.name, '-f', '/dev/stdin', '-t'],
                                input=json.dumps(document).encode(), capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, 'Mihomo rejected synthetic generated document')

    def test_actual_mihomo_fake_ip_and_aaaa_on_isolated_loopback_listener(self):
        binary = Path(os.environ.get('MIHOMO_BIN', '/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo'))
        if not binary.is_file():
            self.skipTest('optional local Mihomo executable')
        from deploy.probe_mihomo_subscription import dns_probe
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
            listener.bind(('127.0.0.1', 0))
            dns_port = listener.getsockname()[1]
        document = managed_clash([{'name': 'fixture', 'type': 'vless', 'server': '192.0.2.1',
                                  'port': 443, 'uuid': 'b9303c18-3580-4757-8c75-50297d895d5b',
                                  'tls': True, 'servername': 'example.com', 'skip-cert-verify': False,
                                  'network': 'tcp', 'udp': False}])
        document.update({'mixed-port': 0, 'tun': {'enable': False}, 'external-controller': '',
                         'external-controller-unix': '', 'external-controller-pipe': '', 'log-level': 'silent'})
        document['dns']['listen'] = '127.0.0.1:' + str(dns_port)
        process = subprocess.Popen([str(binary), '-d', self.temp.name, '-f', '/dev/stdin'],
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            process.stdin.write(json.dumps(document).encode())
            process.stdin.close()
            time.sleep(.4)
            self.assertIsNone(process.poll(), 'synthetic DNS core exited before query')
            self.assertTrue(dns_probe(dns_port, 1), 'fake-IP answer missing')
            self.assertTrue(dns_probe(dns_port, 28), 'AAAA response was not empty')
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
