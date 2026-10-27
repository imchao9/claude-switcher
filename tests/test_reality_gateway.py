import base64
from copy import deepcopy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from server.gateway import (DomainError, gateway_server_identity, managed_nodes, managed_uri,
                            validate_gateway, ws_identity, xray_merge)
from server.xui_contract import desired_revision
from server.subscription_server import create_server

REALITY = {'kind': 'xray-vless-reality', 'port': 443, 'server_name': 'public.example',
           'public_key': base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip('='),
           'short_id': '12ab', 'inbound_tag': 'existing-reality', 'flow': 'xtls-rprx-vision'}
WS = {'address': 'edge.example', 'port': 443, 'server_name': 'tls.example',
      'host': 'origin.example', 'path': '/socket%20route?ed=2560',
      'inbound_tag': 'managed-ws', 'tls_termination': 'external'}


class ManagedGatewayContractTests(unittest.TestCase):
    def setUp(self):
        self.user = {'id': 'fixture', 'ingress_uuid': '65a847df-0c10-4b14-a221-5788b7c22dbf'}
        self.server = {'id': 'one', 'host': 'gateway.example', 'status': 'active', 'gateway': REALITY}
        self.binding = {**self.user, 'ip_id': 'home', 'protocol': 'socks5', 'address': '8.8.4.4',
                        'port': 1080, 'config_json': '{"username":"fixture-user","password":"fixture-secret"}'}

    def test_mlkem_is_explicit_per_server_and_does_not_change_old_route_revision(self):
        old_merge = xray_merge(self.server, REALITY, [self.binding])
        old_revision = desired_revision(self.server, old_merge)
        for value in (None, False, True):
            with self.subTest(value=value):
                gateway = dict(REALITY)
                if value is not None:
                    gateway['support_x25519mlkem768'] = value
                gateway = validate_gateway(gateway)
                opts = managed_nodes(self.user, self.server, gateway)[0]['reality-opts']
                if value is None:
                    self.assertNotIn('support-x25519mlkem768', opts)
                else:
                    self.assertIs(opts['support-x25519mlkem768'], value)
                self.assertEqual(gateway_server_identity(gateway), REALITY)
                merge = xray_merge(self.server, gateway, [self.binding])
                self.assertEqual(merge, old_merge)
                self.assertEqual(desired_revision(dict(self.server, gateway=gateway), merge), old_revision)
        self.assertNotIn('support-x25519mlkem768', managed_nodes(self.user, self.server, REALITY)[0]['reality-opts'])
        for value in (None, 0, 1, 'true', [], {}):
            with self.subTest(invalid=value), self.assertRaises(DomainError):
                validate_gateway(dict(REALITY, support_x25519mlkem768=value))

    def test_ws_public_contract_rejects_unknown_and_unsafe_fields(self):
        self.assertEqual(validate_gateway(dict(REALITY, ws_backup=WS))['ws_backup'], WS)
        invalid = [dict(WS, private_key='secret'), dict(WS, port=True), dict(WS, port=0),
                   dict(WS, inbound_tag=REALITY['inbound_tag']), dict(WS, tls_termination='none'),
                   dict(WS, path='socket'), dict(WS, path='//host/socket'), dict(WS, path='/a#frag'),
                   dict(WS, path='/a\r\nheader'), dict(WS, path='/a\u00a0b'),
                   dict(WS, path='/a\\b'), dict(WS, path='/' + 'x' * 1024)]
        invalid.extend(dict(WS, **{key: value}) for key in ('address', 'server_name', 'host')
                       for value in ('https://example.com', 'name:443', 'user@host', 'host\r\nx'))
        invalid.extend({key: value for key, value in WS.items() if key != omitted} for omitted in WS)
        for value in invalid:
            with self.subTest(value=value), self.assertRaisesRegex(DomainError, 'invalid gateway configuration'):
                validate_gateway(dict(REALITY, ws_backup=value))
        tls = {'kind': 'xray-vless-tls', 'port': 443, 'server_name': 'tls.example',
               'certificate_file': '/etc/cert.pem', 'key_file': '/etc/key.pem'}
        self.assertEqual(validate_gateway(tls), tls)
        for key, value in (('ws_backup', WS), ('support_x25519mlkem768', True)):
            with self.assertRaises(DomainError):
                validate_gateway({**tls, key: value})

    def test_ws_nodes_and_uri_keep_distinct_stable_identity_and_verified_tls(self):
        gateway = validate_gateway(dict(REALITY, ws_backup=WS, support_x25519mlkem768=True))
        primary, backup = managed_nodes(self.user, self.server, gateway)
        self.assertEqual(primary['uuid'], self.user['ingress_uuid'])
        self.assertEqual(backup['uuid'], ws_identity(self.user['ingress_uuid']))
        self.assertNotEqual(primary['uuid'], backup['uuid'])
        self.assertEqual(backup['server'], WS['address'])
        self.assertNotEqual(primary['server'], backup['server'])
        self.assertEqual(primary['port'], backup['port'])
        self.assertTrue(backup['tls'])
        self.assertFalse(backup['skip-cert-verify'])
        self.assertFalse(backup['udp'])
        self.assertNotIn('flow', backup)
        self.assertNotIn('reality-opts', backup)
        changed = dict(gateway, ws_backup=dict(WS, port=8443, path='/changed'))
        self.assertEqual(managed_nodes(self.user, self.server, changed)[1]['uuid'], backup['uuid'])
        uri = managed_uri(backup)
        query = parse_qs(urlsplit(uri).query)
        self.assertEqual(query['security'], ['tls'])
        self.assertEqual(query['type'], ['ws'])
        self.assertEqual(query['path'], [WS['path']])
        self.assertEqual(query['host'], [WS['host']])
        self.assertEqual(query['sni'], [WS['server_name']])
        self.assertEqual(query['allowInsecure'], ['0'])
        self.assertNotIn('flow', query)
        self.assertNotIn('pbk', query)
        for secret in ('fixture-secret', 'fixture-user', '8.8.4.4'):
            self.assertNotIn(secret, json.dumps([primary, backup]) + uri)

    def test_merge_adds_ws_to_same_egress_and_keeps_legacy_shape(self):
        legacy = xray_merge(self.server, REALITY, [self.binding])
        self.assertNotIn('additional_inbounds', legacy)
        gateway = dict(REALITY, ws_backup=WS)
        server = dict(self.server, gateway=gateway)
        merge = xray_merge(server, gateway, [self.binding])
        self.assertEqual(merge['format'], legacy['format'])
        self.assertEqual(merge['clients'], legacy['clients'])
        self.assertEqual(merge['outbounds'], legacy['outbounds'])
        ws = merge['additional_inbounds'][0]
        self.assertEqual(ws['inbound_tag'], WS['inbound_tag'])
        self.assertEqual(ws['clients'][0]['flow'], '')
        self.assertEqual(ws['clients'][0]['id'], ws_identity(self.user['ingress_uuid']))
        tcp = [rule for rule in merge['routing_rules'] if rule['network'] == 'tcp']
        udp = [rule for rule in merge['routing_rules'] if rule['network'] == 'udp']
        self.assertEqual({rule['outboundTag'] for rule in tcp}, {'cs-one-residential-home'})
        self.assertEqual({rule['outboundTag'] for rule in udp}, {'cs-one-block'})
        self.assertNotEqual(desired_revision(server, merge), desired_revision(self.server, legacy))
        empty = xray_merge(dict(server, status='disabled'), gateway, [self.binding])
        self.assertEqual(empty['clients'], [])
        self.assertEqual(empty['additional_inbounds'][0]['clients'], [])
        self.assertEqual(empty['routing_rules'], [])


class RealityGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.http = create_server(str(Path(self.temp.name) / 'db'), 'fixture-reality-admin', port=0)
        self.store = self.http.store
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        for identifier in ['one', 'two']:
            self.store.add_server({'id': identifier, 'name': identifier, 'host': identifier + '.example', 'gateway': REALITY})
            self.store.add_ip({'id': identifier + '-exit', 'server_id': identifier, 'address': '8.8.4.4' if identifier == 'one' else '1.1.1.1',
                               'port': 1080, 'protocol': 'socks5', 'config': {'username': identifier + '-login', 'password': identifier + '-secret'}})
        self.created = self.store.create_user({'id': 'user', 'ip_id': 'one-exit'}, 'https://fixture.invalid')

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.store.close()
        self.temp.cleanup()

    def request(self, path, auth=True):
        connection = http.client.HTTPConnection(*self.http.server_address, timeout=5)
        connection.request('GET', path, headers={'Authorization': 'Bearer fixture-reality-admin'} if auth else {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_strict_discriminant_public_key_flow_and_unknown_fields(self):
        invalid = [{'public_key': 'A' * 42}, {'public_key': 'A' * 42 + 'B'}, {'public_key': 'A' * 43 + '='},
                   {'public_key': 'private secret'}, {'short_id': ''}, {'short_id': 'f'}, {'short_id': 'abcg'},
                   {'short_id': '00' * 9}, {'inbound_tag': '../secret'}, {'inbound_tag': 'x' * 65},
                   {'flow': ''}, {'flow': 'xtls-rprx-vision-udp443'}, {'fingerprint': 'chrome'},
                   {'private_key': 'secret'}, {'certificate_file': '/secret'}, {'kind': []}, {'port': True}]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaisesRegex(DomainError, '^invalid gateway configuration$'):
                validate_gateway({**REALITY, **changes})
        for field in REALITY:
            with self.assertRaises(DomainError):
                validate_gateway({key: value for key, value in REALITY.items() if key != field})
        self.assertEqual(validate_gateway(REALITY), REALITY)

    def test_three_client_formats_use_reality_only_without_upstream_credentials(self):
        bodies = []
        for format in ['clash', 'uri', 'base64']:
            status, headers, body = self.request('/s/' + self.created['token'] + '?format=' + format, False)
            self.assertEqual(status, 200)
            self.assertEqual(headers['X-Claude-Switcher-Egress-IP'], '8.8.4.4')
            if format == 'base64':
                body = base64.b64decode(body)
            for secret in [b'8.8.4.4', b'one-login', b'one-secret', self.created['token'].encode()]:
                self.assertNotIn(secret, body)
            if format == 'clash':
                for expected in [b'reality-opts:', b'public-key:', b'short-id:', b'flow: "xtls-rprx-vision"', b'client-fingerprint: "chrome"', b'udp: false']:
                    self.assertIn(expected, body)
            else:
                query = parse_qs(urlsplit(body.decode().strip()).query)
                self.assertEqual(query['security'], ['reality'])
                self.assertEqual(query['pbk'], [REALITY['public_key']])
                self.assertEqual(query['sid'], [REALITY['short_id']])
                self.assertEqual(query['flow'], ['xtls-rprx-vision'])
                self.assertEqual(query['fp'], ['chrome'])
                bodies.append(body.strip())
        self.assertEqual(bodies[0], bodies[1])

    def test_full_config_rejected_merge_auth_and_manual_package_scope(self):
        for endpoint in ['xray-config', 'xray-merge']:
            self.assertEqual(self.request('/api/servers/one/' + endpoint, False)[0], 401)
        status, _, raw = self.request('/api/servers/one/xray-config')
        self.assertEqual((status, json.loads(raw)), (409, {'error': 'manual_panel_merge_required'}))
        status, headers, raw = self.request('/api/servers/one/xray-merge')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn('attachment;', headers['Content-Disposition'])
        package = json.loads(raw)
        self.assertEqual(package['format'], 'claude-switcher-3xui-merge-v1')
        self.assertEqual(package['application_status'], 'not_applied')
        self.assertEqual(package['inbound_tag'], REALITY['inbound_tag'])
        self.assertEqual(set(package['clients'][0]), {'id', 'email', 'flow'})
        self.assertEqual(package['clients'][0]['flow'], 'xtls-rprx-vision')
        self.assertEqual([rule['network'] for rule in package['routing_rules']], ['tcp', 'udp'])
        for rule in package['routing_rules']:
            self.assertEqual(rule['inboundTag'], ['existing-reality'])
            self.assertEqual(rule['user'], [package['clients'][0]['email']])
            self.assertTrue(rule['outboundTag'].startswith('cs-one-'))
        for tag in [item['tag'] for item in package['outbounds']]:
            self.assertTrue(tag.startswith('cs-one-'))
        self.assertNotIn(b'two-secret', raw)
        self.assertNotIn('inbounds', package)
        self.assertNotIn('routing', package)
        self.assertNotIn('privateKey', raw.decode())
        manifest = package['managed_objects']['clients'][0]
        self.assertEqual(manifest['uuid'], package['clients'][0]['id'])
        self.assertEqual(manifest['email'], package['clients'][0]['email'])
        self.assertEqual(manifest['outbound_tag'], package['routing_rules'][0]['outboundTag'])
        instructions = ' '.join(package['instructions'])
        for phrase in ['Stop on any same-name conflict', 'BEFORE', 'empty list is not a revocation', 'WS', 'private keys']:
            self.assertIn(phrase, instructions)

    def test_manual_merge_preserves_unmanaged_reality_and_ws_objects(self):
        existing = {'inbounds': [{'tag': 'existing-reality', 'settings': {'clients': [{'id': 'unmanaged', 'email': 'existing@user'}]},
                                  'streamSettings': {'realitySettings': {'privateKey': 'fixture-private'}}},
                                 {'tag': 'ws', 'port': 10000, 'settings': {'clients': [{'id': 'ws-user'}]}}],
                    'outbounds': [{'tag': 'direct', 'protocol': 'freedom'}],
                    'routing': {'rules': [{'inboundTag': ['existing-reality'], 'outboundTag': 'direct'}]}}
        before = deepcopy(existing)
        package = self.store.export_xray_config('one', merge=True)
        candidate = deepcopy(existing)
        candidate['inbounds'][0]['settings']['clients'].extend(package['clients'])
        candidate['outbounds'].extend(package['outbounds'])
        candidate['routing']['rules'] = package['routing_rules'] + candidate['routing']['rules']
        self.assertEqual(candidate['inbounds'][1], before['inbounds'][1])
        self.assertEqual(candidate['inbounds'][0]['streamSettings'], before['inbounds'][0]['streamSettings'])
        self.assertEqual(candidate['inbounds'][0]['settings']['clients'][0], before['inbounds'][0]['settings']['clients'][0])
        self.assertEqual(candidate['routing']['rules'][-1], before['routing']['rules'][0])
        self.assertEqual(candidate['outbounds'][0], before['outbounds'][0])
        self.assertEqual(existing, before)

    def test_legal_block_ip_id_cannot_collide_with_block_outbound(self):
        self.store.add_ip({'id': 'block', 'server_id': 'one', 'address': '9.9.9.9', 'port': 1080,
                           'protocol': 'http', 'config': {}})
        self.store.create_user({'id': 'other-user', 'ip_id': 'block'}, 'https://fixture.invalid')
        package = self.store.export_xray_config('one', merge=True)
        tags = [item['tag'] for item in package['outbounds']]
        self.assertEqual(len(tags), len(set(tags)))
        self.assertIn('cs-one-residential-block', tags)
        self.assertIn('cs-one-block', tags)

    def test_disabled_or_unbound_clients_never_export_and_empty_is_not_revocation(self):
        self.store.disable('user')
        package = self.store.export_xray_config('one', merge=True)
        self.assertEqual(package['clients'], [])
        self.assertEqual(package['outbounds'], [])
        self.assertEqual(package['routing_rules'], [])
        self.assertIn('empty list is not a revocation', ' '.join(package['instructions']))
        self.assertEqual(self.request('/api/servers/missing/xray-merge')[0], 404)


if __name__ == '__main__':
    unittest.main()
