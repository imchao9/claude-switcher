import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from server.subscription_server import create_server, main, public_origin


class PublicSubscriptionURLTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / 'private.db')
        self.admin = 'synthetic-admin-token-12345'
        self.origin = 'https://subscriptions.example'
        self.server = create_server(self.db, self.admin, port=0, base_url=self.origin)
        self.store = self.server.store
        for suffix, address in [('a', '8.8.8.8'), ('b', '1.1.1.1')]:
            self.store.add_server({'id': 'edge-' + suffix, 'name': 'Edge ' + suffix, 'host': 'edge-' + suffix + '.example',
                'gateway': {'kind': 'xray-vless-tls', 'port': 443, 'server_name': 'edge-' + suffix + '.example',
                            'certificate_file': '/etc/xray/cert.pem', 'key_file': '/etc/xray/key.pem'}})
            self.store.add_ip({'id': 'ip-' + suffix, 'address': address, 'port': 1080,
                               'protocol': 'socks5', 'server_id': 'edge-' + suffix,
                               'config': {'username': 'fixture-name', 'password': 'fixture-secret'}})
        self.created = self.store.create_user({'id': 'user-a', 'ip_id': 'ip-a'}, self.origin)
        self.token = self.created['token']
        self.path = urlsplit(self.created['subscription_url']).path
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def request(self, method, path, body=None, auth=False):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        headers = {'Content-Type': 'application/json'}
        if auth:
            headers['Authorization'] = 'Bearer ' + self.admin
        conn.request(method, path, None if body is None else json.dumps(body).encode(), headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response, raw

    def convert(self, value, auth=True):
        return self.request('POST', '/api/subscriptions/convert', {'url': value}, auth)

    def dump(self):
        return list(self.store.db.iterdump())

    def test_create_import_rotate_canonical_paths_and_unbound_fallback(self):
        self.assertEqual(self.path, '/sub/' + self.token + '/edge-a.yaml')
        imported = self.store.import_ips({'text': '9.9.9.9:1080:fixture:synthetic',
                                         'protocol': 'socks5', 'server_id': 'edge-a',
                                         'dry_run': False, 'create_subscriptions': True}, self.origin)
        self.assertRegex(imported['subscriptions'][0]['subscription_url'],
                         r'^https://subscriptions\.example/sub/cs_[A-Za-z0-9_-]+/edge-a\.yaml$')
        rotated = self.store.rotate_token('user-a', self.origin)
        self.assertEqual(rotated['subscription_url'], self.origin + '/sub/' + rotated['token'] + '/edge-a.yaml')
        unbound = self.store.create_user({'id': 'unbound'}, self.origin)
        self.assertTrue(unbound['subscription_url'].endswith('/subscription.yaml'))
        self.assertEqual(self.request('GET', self.path)[0].status, 404)

    def test_old_and_new_urls_return_same_formats_and_headers(self):
        for fmt in ['clash', 'uri', 'base64']:
            old, old_body = self.request('GET', '/s/' + self.token + '?format=' + fmt)
            new, new_body = self.request('GET', self.path + '?format=' + fmt)
            self.assertEqual((old.status, new.status), (200, 200))
            self.assertEqual(old_body, new_body)
            for key in ['Content-Type', 'Cache-Control', 'Content-Length',
                        'X-Claude-Switcher-Egress-IP', 'X-Claude-Switcher-User']:
                self.assertEqual(old.getheader(key), new.getheader(key))
            self.assertEqual(new.getheader('Cache-Control'), 'no-store')
        self.assertEqual(self.request('GET', self.path + '?format=bad')[0].status, 400)

    def test_rebinding_keeps_old_filename_deliverable_and_conversion_uses_current_server(self):
        self.store.bind('user-a', 'ip-b')
        response, raw = self.request('GET', self.path)
        self.assertEqual(response.status, 200)
        self.assertIn(b'edge-b.example', raw)
        before = self.dump()
        response, raw = self.convert(self.created['subscription_url'])
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(raw)['subscription_url'], self.origin + '/sub/' + self.token + '/edge-b.yaml')
        self.assertEqual(self.dump(), before)

    def test_converter_accepts_only_configured_origin_or_explicit_local_origins(self):
        before = self.dump()
        for prefix in [self.origin, 'http://127.0.0.1:8787', 'http://localhost:8787',
                       'http://[::1]:8787', f'http://127.0.0.1:{self.server.server_port}']:
            for path in ['/s/' + self.token, self.path]:
                response, raw = self.convert(prefix + path)
                self.assertEqual(response.status, 200, raw)
                result = json.loads(raw)
                self.assertEqual(set(result), {'user', 'subscription_url'})
                self.assertEqual(result['user']['id'], 'user-a')
                self.assertEqual(result['subscription_url'], self.created['subscription_url'])
        self.assertEqual(self.dump(), before)

    def test_converter_is_pure_local_read_and_does_not_resolve_or_fetch_origin(self):
        before = self.dump()
        with patch('socket.getaddrinfo', side_effect=AssertionError('network forbidden')), \
             patch('urllib.request.urlopen', side_effect=AssertionError('fetch forbidden')):
            result = self.store.convert_subscription({'url': self.origin + '/s/' + self.token},
                                                     self.origin, self.server.server_port)
        self.assertEqual(result['subscription_url'], self.created['subscription_url'])
        self.assertEqual(self.dump(), before)

    def test_converter_auth_precedes_body_parsing(self):
        response, raw = self.convert('not a URL', auth=False)
        self.assertEqual(response.status, 401)
        self.assertEqual(json.loads(raw), {'error': 'admin_auth_required'})
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        conn.request('POST', '/api/subscriptions/convert', b'not-json')
        response = conn.getresponse()
        self.assertEqual(response.status, 401)
        response.read()
        conn.close()
        with patch.object(self.store, 'convert_subscription', side_effect=AssertionError('unauthorized read')):
            self.assertEqual(self.convert(self.created['subscription_url'], auth=False)[0].status, 401)

    def test_converter_rejects_untrusted_malformed_encoded_or_extra_fields_uniformly(self):
        bad = [
            'https://other.example/s/' + self.token,
            'http://subscriptions.example/s/' + self.token,
            'http://127.0.0.1:99/s/' + self.token,
            'https://user@subscriptions.example/s/' + self.token,
            self.created['subscription_url'] + '?format=unknown',
            self.created['subscription_url'] + '#fragment',
            self.created['subscription_url'] + ')',
            self.origin + '/sub/' + self.token + '/%65dge-a.yaml',
            self.origin + '/sub/' + self.token + '/../edge-a.yaml',
            self.origin + '/sub/' + self.token + '/edge-a.yaml/extra',
            self.origin + '/s/cs_unknown-synthetic-token',
            self.origin + '/s/' + self.token + '\n',
            self.origin + '\\evil/s/' + self.token,
            'x' * 2049, None, 5,
        ]
        before = self.dump()
        for value in bad:
            response, raw = self.convert(value)
            self.assertEqual(response.status, 400)
            self.assertEqual(json.loads(raw), {'error': 'invalid_subscription_reference'})
        response, raw = self.request('POST', '/api/subscriptions/convert',
                                     {'url': self.created['subscription_url'], 'extra': True}, True)
        self.assertEqual((response.status, json.loads(raw)),
                         (400, {'error': 'invalid_subscription_reference'}))
        self.assertEqual(self.dump(), before)

    def test_strict_new_and_legacy_route_grammar(self):
        for path in [self.path + '/extra', self.path.replace('/edge-a.yaml', '/../edge-a.yaml'),
                     self.path.replace('edge-a', '%65dge-a'), self.path.replace('edge-a', '\\edge-a'),
                     '/s/' + self.token + '/extra', '/s/' + self.token + '%20',
                     self.path.replace('/edge-a.yaml', '/.yaml')]:
            self.assertEqual(self.request('GET', path)[0].status, 404)

    def test_ineligible_and_revoked_tokens_cannot_be_converted(self):
        for sql in ["UPDATE ips SET status='disabled' WHERE id='ip-a'",
                    "UPDATE servers SET status='disabled' WHERE id='edge-a'"]:
            with self.store.db:
                self.store.db.execute(sql)
            before = self.dump()
            self.assertEqual(self.convert(self.created['subscription_url'])[0].status, 400)
            self.assertEqual(self.request('GET', self.path)[0].status, 404)
            self.assertEqual(self.dump(), before)
            with self.store.db:
                self.store.db.execute("UPDATE ips SET status='available'")
                self.store.db.execute("UPDATE servers SET status='active'")
        self.store.rotate_token('user-a', self.origin)
        self.assertEqual(self.convert(self.created['subscription_url'])[0].status, 400)
        self.store.disable('user-a')
        self.assertEqual(self.convert(self.created['subscription_url'])[0].status, 400)


class PublicOriginTests(unittest.TestCase):
    def test_origin_is_validated_before_database_or_socket_creation(self):
        invalid = ['http://public.example', 'https://user:pass@example.test', 'https://example.test/path',
                   'https://example.test?', 'https://example.test#', 'https://example.test:0',
                   'https://example.test:65536', 'https://example.test:', 'https://example.test\\evil',
                   'https://example.test\n', 'https://example..test', 'https://exa mple.test', 'https://%65xample.test', 'file:///tmp', '']
        for value in invalid:
            with patch('server.subscription_server.SubscriptionStore') as store, \
                 patch('server.subscription_server.ThreadingHTTPServer') as server:
                with self.assertRaises(ValueError):
                    create_server('/unused.db', 'synthetic-admin-token-12345', base_url=value)
                store.assert_not_called()
                server.assert_not_called()

    def test_origins_normalize_without_trusting_request_headers(self):
        for value, expected in [('https://Example.test:443/', 'https://example.test'),
                                ('http://localhost:8787/', 'http://localhost:8787'),
                                ('http://[::1]:8787', 'http://[::1]:8787'),
                                ('https://[2001:db8::1]/', 'https://[2001:db8::1]')]:
            self.assertEqual(public_origin(value), expected)

    def test_cli_environment_sets_public_origin(self):
        with patch.dict(os.environ, {'CLAUDE_SWITCHER_BASE_URL': 'https://env.example'}), \
             patch('server.subscription_server.create_server') as create:
            create.return_value.serve_forever.side_effect = KeyboardInterrupt
            self.assertEqual(main(['--admin-token', 'synthetic-admin-token-12345']), 0)
            self.assertEqual(create.call_args.args[4], 'https://env.example')

    def test_default_origin_uses_actual_ephemeral_port(self):
        with tempfile.TemporaryDirectory() as temp:
            server = create_server(str(Path(temp) / 'db'), 'synthetic-admin-token-12345', port=0)
            try:
                self.assertEqual(server.RequestHandlerClass.base_url, f'http://127.0.0.1:{server.server_port}')
            finally:
                server.server_close()
                server.store.close()


if __name__ == '__main__':
    unittest.main()
