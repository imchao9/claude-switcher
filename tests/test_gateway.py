import base64
import http.client
import json
import sqlite3
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from urllib.parse import parse_qs, urlsplit

from server.subscription_server import DomainError, SubscriptionStore, create_server


GATEWAY = {'kind': 'xray-vless-tls', 'port': 8443, 'server_name': 'tls.example',
           'certificate_file': '/etc/xray/fullchain.pem', 'key_file': '/etc/xray/private.key'}


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'store.db'
        self.http = create_server(str(self.path), 'fixture-admin-gateway', port=0)
        self.store = self.http.store
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.server('a')
        self.server('b')

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.store.close()
        self.temp.cleanup()

    def server(self, name, **changes):
        return self.store.add_server({'id': name, 'name': name.upper(), 'host': name + '.example',
                                      'gateway': GATEWAY, **changes})

    def ip(self, name='exit-a', server_id='a', address='8.8.8.8', **changes):
        return self.store.add_ip({'id': name, 'server_id': server_id, 'address': address, 'port': 1080,
                                  'protocol': 'socks5', 'config': {'username': name + '-login',
                                  'password': name + '-private'}, **changes})

    def user(self, name='person-a', ip_id='exit-a'):
        return self.store.create_user({'id': name, 'ip_id': ip_id}, 'https://fixture.invalid')

    def request(self, path, auth=True):
        conn = http.client.HTTPConnection(*self.http.server_address, timeout=5)
        conn.request('GET', path, headers={'Authorization': 'Bearer fixture-admin-gateway'} if auth else {})
        response = conn.getresponse()
        raw = response.read()
        headers = dict(response.getheaders())
        conn.close()
        return response.status, headers, raw

    def identity(self, name='person-a'):
        return self.store.db.execute('SELECT ingress_uuid FROM users WHERE id=?', (name,)).fetchone()[0]

    def test_cloud_export_contains_only_its_active_bindings_and_blocks_every_other_route(self):
        self.ip()
        self.ip('exit-b', 'b', '1.1.1.1', protocol='http')
        self.user()
        self.user('person-b', 'exit-b')
        for cloud, included, excluded in [('a', 'exit-a', 'exit-b'), ('b', 'exit-b', 'exit-a')]:
            status, headers, raw = self.request('/api/servers/' + cloud + '/xray-config')
            self.assertEqual(status, 200)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertIn('attachment;', headers['Content-Disposition'])
            self.assertIn((included + '-private').encode(), raw)
            self.assertNotIn((excluded + '-private').encode(), raw)
            config = json.loads(raw)
            inbound = config['inbounds'][0]
            self.assertEqual(inbound['streamSettings']['tlsSettings']['certificates'], [
                {'certificateFile': GATEWAY['certificate_file'], 'keyFile': GATEWAY['key_file']}])
            self.assertEqual(len(inbound['settings']['users']), 1)
            self.assertEqual(config['outbounds'][0]['protocol'], 'blackhole')
            self.assertEqual(len(config['outbounds']), 2)
            rule = config['routing']['rules'][1]
            self.assertEqual(rule['user'], [inbound['settings']['users'][0]['email']])
            self.assertEqual(rule['inboundTag'], ['shared-ingress'])
            self.assertEqual(rule['network'], 'tcp')
            self.assertEqual(config['routing']['rules'][-1]['network'], 'tcp,udp')
            self.assertEqual(config['routing']['rules'][-1]['outboundTag'], 'block')
            for forbidden in ['freedom', 'direct', 'fallback', 'balancer']:
                self.assertNotIn(forbidden, raw.decode().lower())
        self.store.disable('person-a')
        config = self.store.export_xray_config('a')
        self.assertEqual(config['inbounds'][0]['settings']['users'], [])
        self.assertEqual(len(config['outbounds']), 1)

    def test_three_client_formats_use_only_cloud_and_user_identity_and_keep_residential_header(self):
        self.ip()
        created = self.user()
        identity = self.identity()
        self.assertEqual(uuid.UUID(identity).version, 4)
        uri = None
        for fmt in ['clash', 'uri', 'base64']:
            status, headers, body = self.request('/s/' + created['token'] + '?format=' + fmt, False)
            self.assertEqual(status, 200)
            self.assertEqual(headers['X-Claude-Switcher-Egress-IP'], '8.8.8.8')
            if fmt == 'base64':
                body = base64.b64decode(body)
            for secret in ['8.8.8.8', 'exit-a-login', 'exit-a-private', created['token']]:
                self.assertNotIn(secret.encode(), body)
            self.assertIn(b'a.example', body)
            self.assertIn(identity.encode(), body)
            if fmt == 'clash':
                for required in ['tls: true', 'skip-cert-verify: false', 'udp: false', 'ipv6: false',
                                 'NETWORK,udp,REJECT', 'MATCH,CLAUDE-SWITCHER']:
                    self.assertIn(required.encode(), body)
                self.assertNotIn(b'"DIRECT"', body)
                self.assertNotIn(b'MATCH,DIRECT', body)
                self.assertIn(b'https://1.1.1.1/dns-query#CLAUDE-SWITCHER', body)
            else:
                parsed = urlsplit(body.decode().strip())
                self.assertEqual((parsed.scheme, parsed.hostname, parsed.port, parsed.username),
                                 ('vless', 'a.example', 8443, identity))
                self.assertEqual(parse_qs(parsed.query), {'encryption': ['none'], 'security': ['tls'],
                                 'sni': ['tls.example'], 'type': ['tcp'], 'allowInsecure': ['0'], 'udp': ['false']})
                if uri is None:
                    uri = body
                else:
                    self.assertEqual(body.rstrip(), uri.rstrip())

    def test_no_identity_or_upstream_credentials_leak_through_admin_lists_or_unauthenticated_export(self):
        self.ip()
        self.user()
        for path in ['/api/users', '/api/ips', '/api/servers']:
            status, _, raw = self.request(path)
            self.assertEqual(status, 200)
            for secret in [self.identity(), 'ingress_uuid', 'exit-a-private', 'exit-a-login', 'config_json']:
                self.assertNotIn(secret.encode(), raw)
        status, headers, raw = self.request('/api/servers/a/xray-config', False)
        self.assertEqual(status, 401)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn(self.identity().encode(), raw)
        self.assertNotIn(b'exit-a-private', raw)

    def test_export_unknown_and_unconfigured_server_statuses_require_authentication_first(self):
        self.server('a', gateway=None)
        for path, expected in [('/api/servers/missing/xray-config', 404),
                               ('/api/servers/a/xray-config', 409)]:
            self.assertEqual(self.request(path, False)[0], 401)
            status, headers, raw = self.request(path)
            self.assertEqual(status, expected)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertNotIn(b'fixture-admin-gateway', raw)

    def test_disabled_server_exports_deny_all_and_subscriptions_refuse(self):
        self.ip()
        created = self.user()
        self.server('a', status='disabled')
        config = self.store.export_xray_config('a')
        self.assertEqual(config['inbounds'][0]['settings']['users'], [])
        self.assertEqual(config['outbounds'], [{'tag': 'block', 'protocol': 'blackhole', 'settings': {}}])
        self.assertEqual(len(config['routing']['rules']), 2)
        for fmt in ['clash', 'uri', 'base64']:
            self.assertEqual(self.request('/s/' + created['token'] + '?format=' + fmt, False)[0], 404)

    def test_token_rotation_and_reopen_preserve_ingress_identity(self):
        self.ip()
        created = self.user()
        identity = self.identity()
        rotated = self.store.rotate_token('person-a', 'https://fixture.invalid')
        self.assertNotEqual(created['token'], rotated['token'])
        self.assertEqual(self.identity(), identity)
        self.assertEqual(self.request('/s/' + created['token'], False)[0], 404)
        other = SubscriptionStore(self.path)
        try:
            self.assertEqual(other.db.execute('SELECT ingress_uuid FROM users').fetchone()[0], identity)
            self.assertEqual(len(other.subscription(rotated['token'], 'uri')[0]), len(self.store.subscription(rotated['token'], 'uri')[0]))
            with self.assertRaises(sqlite3.IntegrityError):
                with other.db:
                    other.db.execute('INSERT INTO users(id,name,token_hash,token_hint,status,created_at,updated_at,ingress_uuid) SELECT id || ?, name, token_hash || ?, token_hint,status,created_at,updated_at,ingress_uuid FROM users', ('-copy', '-copy'))
        finally:
            other.close()

    def test_no_gateway_permits_inventory_but_refuses_new_bindings_import_preview_and_confirm(self):
        self.server('a', gateway=None)
        self.assertFalse(self.store.list_servers()[0]['gateway_ready'])
        self.ip()
        self.assertEqual(self.store.list_servers()[0]["available_count"], 0)
        for dry_run in [True, False]:
            with self.assertRaisesRegex(DomainError, 'gateway is unavailable'):
                self.store.import_ips({'server_id': 'a', 'text': '1.1.1.1:1080:u:p', 'protocol': 'socks5',
                                       'create_subscriptions': True, 'dry_run': dry_run}, 'https://fixture.invalid')
        with self.assertRaisesRegex(DomainError, 'gateway is unavailable'):
            self.user()
        self.assertEqual(self.store.list_users(), [])
        result = self.store.import_ips({'server_id': 'a', 'text': '1.1.1.1:1080:u:p', 'protocol': 'http',
                                       'dry_run': False}, 'https://fixture.invalid')
        self.assertEqual(result['imported'], 1)
        self.assertEqual(self.store.list_users(), [])

    def test_missing_gateway_on_existing_nonnull_assignment_never_renders_direct(self):
        self.ip()
        created = self.user()
        with self.store.db:
            self.store.db.execute("UPDATE servers SET gateway_json=NULL WHERE id='a'")
        for fmt in ['clash', 'uri', 'base64']:
            with self.assertRaisesRegex(DomainError, 'subscription unavailable'):
                self.store.subscription(created['token'], fmt)

    def test_gateway_update_omission_preserves_and_null_requires_no_bindings(self):
        before = self.store.list_servers()[0]['gateway']
        updated = self.store.add_server({'id': 'a', 'name': 'Renamed', 'host': 'a.example'})
        self.assertEqual(updated['gateway'], before)
        self.ip()
        self.user()
        with self.assertRaisesRegex(DomainError, 'gateway_in_use'):
            self.server('a', gateway=None)
        self.assertEqual(self.store.list_servers()[0]['gateway'], before)
        self.store.bind('person-a', None)
        self.assertIsNone(self.server('a', gateway=None)['gateway'])

    def test_strict_gateway_allowlist_hosts_and_absolute_remote_paths(self):
        for changes in [{'kind': 'other'}, {'port': True}, {'port': 0}, {'port': 65536}, {'port': '443'},
                        {'server_name': 'host/path'}, {'server_name': 'bad_name'}, {'server_name': 'user@host'},
                        {'server_name': 'https://private-secret'}, {'private_key': 'private-secret'},
                        {'certificate_file': 'relative.pem'}, {'key_file': '/etc/../private-secret'},
                        {'key_file': '/private-secret\n'}, {'key_file': '/etc//private-secret'},
                        {'key_file': '/etc/./private-secret'}, {'key_file': '/'}, {'key_file': []}]:
            with self.subTest(changes=changes):
                before = list(self.store.db.iterdump())
                with self.assertRaisesRegex(DomainError, '^invalid gateway configuration$') as error:
                    self.server('a', gateway={**GATEWAY, **changes})
                self.assertNotIn('private-secret', str(error.exception))
                self.assertEqual(list(self.store.db.iterdump()), before)
        for host in ['127.0.0.1', '2001:db8::1', 'tls.example', 'private-alias']:
            self.server('a', gateway={**GATEWAY, 'server_name': host})

    def test_upstream_protocol_fields_and_credentials_are_validated_before_write_or_export(self):
        for changes in [{'protocol': 'vless'}, {'config': {'password': 'private-secret'}},
                        {'config': {'username': 'x', 'password': 'private-secret\n'}},
                        {'config': {'username': 'x', 'password': 'private-secret\u0080'}},
                        {'config': {'username': 'x', 'password': 'p' * 256}},
                        {'config': {'streamSettings': {'network': 'private-secret'}}},
                        {'config': {'username': [], 'password': 'private-secret'}}]:
            with self.subTest(changes=changes):
                with self.assertRaises(DomainError) as error:
                    self.ip(**changes)
                self.assertNotIn('private-secret', str(error.exception))
                self.assertEqual(self.store.list_ips(), [])
        self.ip()
        self.user()
        with self.store.db:
            self.store.db.execute("UPDATE ips SET config_json=?", (json.dumps({'password': 'private-secret\n'}),))
        with self.assertRaisesRegex(DomainError, 'invalid upstream configuration'):
            self.store.export_xray_config('a')

    def test_bound_updates_and_disabled_user_binding_fail_atomically(self):
        self.ip()
        self.ip('exit-b', 'a', '1.1.1.1')
        self.user()
        before = list(self.store.db.iterdump())
        with self.assertRaises(DomainError):
            self.ip(protocol='vless', config={'uuid': 'private-secret'})
        self.assertEqual(list(self.store.db.iterdump()), before)
        self.store.disable('person-a')
        with self.assertRaisesRegex(DomainError, 'user is unavailable'):
            self.store.bind('person-a', 'exit-b')
        self.assertIsNone(self.store.get_user('person-a')['ip_id'])


if __name__ == '__main__':
    unittest.main()
