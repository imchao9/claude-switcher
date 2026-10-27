"""Global template persistence, delivery, validation and concurrent administrator edits."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from server.subscription_server import SubscriptionStore, create_server
from server.subscription_template import DEFAULT_TEMPLATE, validate_template
from server.gateway import DomainError


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / 'state.db')
        self.http = create_server(self.db, 'fixture-template-admin', port=0)
        self.store = self.http.store
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.store.add_server({'id': 'cloud', 'name': 'Cloud fixture', 'host': 'cloud.example.invalid', 'gateway': {
            'kind': 'xray-vless-ws', 'address': 'cloud.example.invalid', 'port': 443,
            'server_name': 'cloud.example.invalid', 'host': 'origin.example.invalid',
            'path': '/ws', 'inbound_tag': 'managed-ws', 'tls_termination': 'external'}})
        self.store.add_ip({'id': 'home', 'address': '8.8.4.4', 'port': 1080,
                           'protocol': 'socks5', 'server_id': 'cloud', 'config': {
                               'username': 'upstream-private', 'password': 'upstream-secret'}})
        self.user = self.store.create_user({'id': 'person', 'ip_id': 'home'}, 'https://fixture.invalid')

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.store.close()
        self.temp.cleanup()

    def request(self, method, path, payload=None, auth=True):
        client = http.client.HTTPConnection(*self.http.server_address, timeout=5)
        headers = {'Authorization': 'Bearer fixture-template-admin'} if auth else {}
        client.request(method, path, None if payload is None else json.dumps(payload), headers)
        response = client.getresponse()
        data = response.read()
        client.close()
        return response.status, data

    def test_http_auth_atomic_save_and_existing_url_uses_new_template(self):
        for method in ('GET', 'PUT'):
            self.assertEqual(self.request(method, '/api/subscription-template', {}, auth=False)[0], 401)
        status, raw = self.request('GET', '/api/subscription-template')
        self.assertEqual(status, 200)
        state = json.loads(raw)
        self.assertEqual(state['template']['config']['mixed-port'], 7897)
        state['template']['config']['mixed-port'] = 9097
        state['template']['rules'].append('DOMAIN-SUFFIX,custom.example,CLAUDE-SWITCHER')
        status, raw = self.request('PUT', '/api/subscription-template', {
            'template': state['template'], 'revision': state['revision']})
        self.assertEqual(status, 200, raw)
        self.assertNotEqual(json.loads(raw)['revision'], state['revision'])
        status, clash = self.request('GET', '/s/' + self.user['token'])
        self.assertEqual(status, 200)
        self.assertIn(b'mixed-port: 9097', clash)
        self.assertIn(b'custom.example', clash)
        self.assertIn(b'network: "ws"', clash)
        self.assertNotIn(b'reality-opts', clash)
        self.assertNotIn(b'xtls-rprx-vision', clash)
        for secret in (b'upstream-private', b'upstream-secret', b'8.8.4.4', self.user['token'].encode()):
            self.assertNotIn(secret, clash)
        uri_before = self.store.subscription(self.user['token'], 'uri')[0]
        self.assertNotIn(b'mixed-port', uri_before)
        # A second process sees the committed template, without losing token or binding.
        reopened = SubscriptionStore(self.db)
        try:
            self.assertEqual(reopened.subscription_template()['template'], state['template'])
            self.assertIn(b'mixed-port: 9097', reopened.subscription(self.user['token'], 'clash')[0])
            self.assertEqual(reopened.subscription(self.user['token'], 'uri')[0], uri_before)
        finally:
            reopened.close()
        status, _ = self.request('PUT', '/api/subscription-template', {
            'template': DEFAULT_TEMPLATE, 'revision': state['revision']})
        self.assertEqual(status, 409)
        self.assertEqual(self.store.subscription_template()['template'], state['template'])

    def test_invalid_save_does_not_change_subscription(self):
        state = self.store.subscription_template()
        cases = []
        for path, value in [
            (('mixed-port',), True), (('mixed-port',), 0), (('mixed-port',), 65536),
            (('allow-lan',), True), (('tun', 'enable'), True),
            (('dns', 'listen'), '0.0.0.0:1053'), (('dns', 'enable'), False),
            (('dns', 'nameserver'), ['https://1.1.1.1/dns-query#DIRECT']),
            (('dns', 'default-nameserver'), ['https://dns.example/dns-query#DIRECT']),
            (('geox-url', 'geosite'), 'https://user:password@example/geosite.dat'),
            (('geox-url', 'geosite'), 'https://['),
            (('dns', 'nameserver'), ['https://1.1.1.1:99999/dns-query#CLAUDE-SWITCHER']),
            (('dns', 'nameserver'), ['https://127.0.0.1/dns-query#CLAUDE-SWITCHER']),
            (('sniffer', 'sniff', 'TLS', 'ports'), [True]),
            (('sniffer', 'sniff', 'HTTP', 'ports'), [70000]),
        ]:
            template = copy.deepcopy(DEFAULT_TEMPLATE)
            target = template['config']
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            cases.append(template)
        for patch in ({'proxies': []}, {'secret': 'private'}, {'dns': {'fallback': ['system']}}):
            template = copy.deepcopy(DEFAULT_TEMPLATE)
            template['config'].update(patch)
            cases.append(template)
        for rules in (['MATCH,DIRECT'], ['DOMAIN,claude.ai,DIRECT'], ['DST-PORT,no,REJECT'],
                      ['IP-CIDR,not-a-network,REJECT'], ['DOMAIN,a.example,UNKNOWN'], [None], 'text'):
            template = copy.deepcopy(DEFAULT_TEMPLATE)
            template['rules'] = rules
            cases.append(template)
        for template in cases:
            with self.subTest(template=template):
                status, _ = self.request('PUT', '/api/subscription-template', {
                    'template': template, 'revision': state['revision']})
                self.assertEqual(status, 400)
                self.assertEqual(self.store.subscription_template()['revision'], state['revision'])

    def test_defaults_are_independent_and_domestic_rules_follow_ai_protection(self):
        first = self.store.subscription_template()
        first['template']['config']['mixed-port'] = 1111
        self.assertEqual(self.store.subscription_template()['template']['config']['mixed-port'], 7897)
        for profile in ('strict', 'domestic-direct'):
            status, raw = self.request('GET', '/s/' + self.user['token'] + '?format=clash&routing=' + profile)
            self.assertEqual(status, 200)
            self.assertIn(b'https://8.8.8.8/dns-query#CLAUDE-SWITCHER', raw)
            self.assertIn(b'GEOSITE,category-ads-all,REJECT', raw)
            if profile == 'strict':
                self.assertNotIn(b'GEOSITE,cn,DIRECT', raw)
            else:
                self.assertLess(raw.index(b'DOMAIN-SUFFIX,claude.ai,CLAUDE-SWITCHER'),
                                raw.index(b'GEOSITE,cn,DIRECT'))
            preview = self.store.subscription_template()['previews'][profile]
            self.assertIn('example.invalid', preview)
            self.assertNotIn(self.user['token'], preview)
        self.assertEqual(validate_template(DEFAULT_TEMPLATE), DEFAULT_TEMPLATE)


if __name__ == '__main__':
    unittest.main()
