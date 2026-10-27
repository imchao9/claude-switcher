import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from server.gateway import gateway_server_identity, xray_merge
from server.xui_contract import desired_revision
from server.xui_sync import (Desired, Files, SyncError, digest, inbound_identity, reconcile,
                             source_snapshot, validate_config)
from tests.test_xui_sync import FakePanel, GATEWAY, ROW, SERVER


WS = {'address': 'edge.example.com', 'port': 443, 'server_name': 'edge.example.com',
      'host': 'origin.example.com', 'path': '/fixture-ws', 'inbound_tag': 'ws',
      'tls_termination': 'external'}


def ws_desired(rows=None, status='active', backup=WS):
    gateway = dict(GATEWAY)
    if backup is not None:
        gateway['ws_backup'] = dict(backup)
    server = dict(SERVER, status=status, gateway=gateway)
    merge = xray_merge(server, gateway, [ROW] if rows is None else rows)
    return Desired(desired_revision(server, merge), merge, gateway)


class ManagedWSSyncTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.files = Files(self.directory / 'state.json', self.directory / 'status.json')
        self.panel = FakePanel()
        origin = self.panel.observed.inbounds[1]
        origin['listen'] = '127.0.0.1'
        origin['streamSettings']['wsSettings'] = {'path': WS['path'], 'headers': {'Host': WS['host']}}
        self.panel.render()
        self.desired = ws_desired()
        self.config = {'source_db': str(self.directory / 'source.db'), 'server_id': 'vpn',
                       'expected_host': SERVER['host'], 'inbound_id': 1, 'inbound_tag': 'reality',
                       'gateway_digest': digest(self.desired.gateway),
                       'inbound_digest': digest(inbound_identity(self.panel.observed.inbounds[0]))}
        self.pin_ws()

    def pin_ws(self):
        origin = self.panel.observed.inbounds[1]
        self.config['ws_backup'] = {'inbound_id': origin['id'], 'inbound_tag': origin['tag'],
                                    'inbound_digest': digest(inbound_identity(origin)),
                                    'tls_termination': self.desired.gateway.get('ws_backup', WS)['tls_termination']}

    def sync(self):
        return reconcile(self.config, self.files, lambda: self.desired, self.panel.observe, self.panel.factory)

    def status(self):
        return json.loads(self.files.status_path.read_text())

    def assert_routes(self, identities, expected):
        for email, target in identities.items():
            for domain in ('example.com', 'claude.ai'):
                self.assertEqual(self.panel.route(target['inbound_tag'], email, 'tcp', domain), expected)
                self.assertEqual(self.panel.route(target['inbound_tag'], email, 'udp', domain), 'cs-vpn-block')

    def test_external_create_keeps_origin_port_and_shared_residential_outbound(self):
        before = self.panel.observe()
        self.assertTrue(self.sync(), self.status())
        state = self.files.load()
        self.assertEqual(len(state['identities']), 2)
        self.assert_routes(state['identity_targets'], 'cs-vpn-residential-home')
        self.assertEqual(self.panel.observed.inbounds, before.inbounds)
        self.assertEqual(self.panel.observed.inbounds[1]['port'], 10000)
        self.assertEqual(self.panel.observed.clients['original'], before.clients['original'])
        for email, target in state['identity_targets'].items():
            record = self.panel.observed.clients[email]
            self.assertEqual(record['inboundIds'], [1 if target['inbound_tag'] == 'reality' else 2])
            self.assertEqual(record['client']['flow'], target['flow'])
        self.assertEqual([o['tag'] for o in self.panel.observed.template['outbounds']].count('cs-vpn-residential-home'), 1)
        events = copy.deepcopy(self.panel.events)
        self.assertTrue(self.sync())
        self.assertEqual(self.panel.events, events)

    def test_native_tls_origin_and_absent_optional_host_header(self):
        ws = dict(WS, tls_termination='xray', port=8443)
        self.desired = ws_desired(backup=ws)
        origin = self.panel.observed.inbounds[1]
        origin['listen'], origin['port'] = '0.0.0.0', 8443
        origin['streamSettings'] = {'network': 'ws', 'security': 'tls',
                                    'wsSettings': {'path': WS['path']},
                                    'tlsSettings': {'certificates': [{'certificateFile': '/fixture.pem'}],
                                                    'settings': {'fingerprint': 'chrome', 'serverName': 'share.example'}}}
        self.pin_ws()
        self.panel.render()
        def normalize(kind):
            if kind in ('template', 'restart'):
                self.panel.observed.runtime['inbounds'][1]['streamSettings']['tlsSettings'].pop('settings', None)
        self.panel.hook = normalize
        self.assertTrue(self.sync(), self.status())

    def test_native_tls_runtime_certificate_drift_restores_both_guards(self):
        for field in ('certificateFile', 'keyFile', 'minVersion', 'alpn'):
            with self.subTest(field=field):
                self.setUp()
                self.desired = ws_desired(backup=dict(WS, tls_termination='xray', port=8443))
                origin = self.panel.observed.inbounds[1]
                origin['port'] = 8443
                origin['streamSettings']['security'] = 'tls'
                origin['streamSettings']['tlsSettings'] = {
                    'certificates': [{'certificateFile': '/fixture.pem', 'keyFile': '/fixture.key'}],
                    'minVersion': '1.3', 'alpn': ['http/1.1']}
                self.pin_ws()
                self.panel.render()
                def corrupt(kind):
                    if kind == 'restart':
                        tls = self.panel.observed.runtime['inbounds'][1]['streamSettings']['tlsSettings']
                        if field in ('certificateFile', 'keyFile'):
                            tls['certificates'][0][field] = '/incorrect.pem'
                        elif field == 'minVersion':
                            tls[field] = '1.0'
                        else:
                            tls[field] = ['h2']
                self.panel.hook = corrupt
                self.assertFalse(self.sync())
                self.assertEqual(self.status()['code'], 'runtime_unverified')
                self.assertIsNone(self.files.load()['applied_revision'])
                self.assert_routes(self.files.load()['identity_targets'], 'cs-vpn-block')

    def test_wrong_origin_properties_refuse_before_mutations_even_when_pinned(self):
        changes = [({'listen': '0.0.0.0'}, {}), ({'listen': ''}, {}), ({'listen': 'localhost'}, {}),
                   ({'listen': '192.168.0.1'}, {}), ({}, {'security': 'tls'}),
                   ({}, {'wsSettings': {'path': '/wrong', 'headers': {'Host': WS['host']}}}),
                   ({}, {'wsSettings': {'path': WS['path'], 'headers': {'Host': 'wrong.example'}}})]
        for outer, stream in changes:
            with self.subTest(outer=outer, stream=stream):
                self.setUp()
                self.panel.observed.inbounds[1].update(outer)
                self.panel.observed.inbounds[1]['streamSettings'].update(stream)
                self.pin_ws()
                self.panel.render()
                self.assertFalse(self.sync())
                self.assertEqual(self.status()['code'], 'target_drift')
                self.assertEqual(self.panel.events, [])
                self.assertEqual(self.panel.tokens, 0)

    def test_native_public_port_must_match_origin(self):
        self.desired = ws_desired(backup=dict(WS, tls_termination='xray'))
        self.panel.observed.inbounds[1]['streamSettings']['security'] = 'tls'
        self.pin_ws()
        self.panel.render()
        self.assertFalse(self.sync())
        self.assertEqual(self.status()['code'], 'target_drift')
        self.assertEqual(self.panel.events, [])

    def test_rebind_preserves_both_identities_and_panel_subscription_ids(self):
        self.assertTrue(self.sync())
        before = copy.deepcopy(self.panel.observed.clients)
        self.desired = ws_desired([dict(ROW, ip_id='new-home', address='1.1.1.1')])
        self.assertTrue(self.sync(), self.status())
        self.assertEqual(before, self.panel.observed.clients)
        self.assert_routes(self.files.load()['identity_targets'], 'cs-vpn-residential-new-home')
        self.assertNotIn('cs-vpn-residential-home', [o['tag'] for o in self.panel.observed.template['outbounds']])

    def test_empty_bindings_and_server_disable_revoke_both_entries(self):
        for transition in (ws_desired([]), ws_desired(status='disabled')):
            with self.subTest(revision=transition.revision):
                self.setUp()
                self.assertTrue(self.sync())
                identities = self.files.load()['identity_targets']
                self.desired = transition
                self.assertTrue(self.sync(), self.status())
                self.assert_routes(identities, 'cs-vpn-block')
                self.assertTrue(all(not self.panel.observed.clients[email]['client']['enable'] for email in identities))
                self.assertEqual(self.files.load()['identity_targets'], identities)

    def test_removing_ws_retains_historic_pin_identity_and_deny(self):
        self.assertTrue(self.sync())
        identities = self.files.load()['identity_targets']
        self.desired = ws_desired(backup=None)
        self.assertTrue(self.sync(), self.status())
        for email, target in identities.items():
            if target['inbound_tag'] == 'ws':
                self.assertFalse(self.panel.observed.clients[email]['client']['enable'])
                self.assert_routes({email: target}, 'cs-vpn-block')
            else:
                self.assert_routes({email: target}, 'cs-vpn-residential-home')
        self.assertEqual(self.files.load()['identity_targets'], identities)
        self.assertTrue(self.sync())

    def test_missing_historic_pin_refuses_without_writing(self):
        self.assertTrue(self.sync())
        self.desired = ws_desired(backup=None)
        self.config.pop('ws_backup')
        self.panel.events.clear()
        self.assertFalse(self.sync())
        self.assertEqual(self.status()['code'], 'target_drift')
        self.assertEqual(self.panel.events, [])

    def test_old_state_is_migrated_only_to_primary_then_adds_ws(self):
        self.desired = ws_desired(backup=None)
        ws_pin = self.config.pop('ws_backup')
        self.assertTrue(self.sync())
        state = self.files.load()
        state.pop('identity_targets')
        self.files.save(state)
        self.config['ws_backup'] = ws_pin
        self.desired = ws_desired()
        self.assertTrue(self.sync(), self.status())
        self.assert_routes(self.files.load()['identity_targets'], 'cs-vpn-residential-home')
        self.assertEqual(len(self.files.load()['identity_targets']), 2)

    def test_old_state_with_ws_rules_cannot_be_claimed_as_primary(self):
        self.assertTrue(self.sync())
        state = self.files.load()
        state.pop('identity_targets')
        self.files.save(state)
        self.panel.events.clear()
        self.assertFalse(self.sync())
        self.assertEqual(self.status()['code'], 'target_drift')
        self.assertEqual(self.panel.events, [])

    def test_ws_conflicts_refuse_before_token(self):
        for collision in ('uuid', 'attachment', 'flow', 'pin'):
            with self.subTest(collision=collision):
                self.setUp()
                ws_client = self.desired.merge['additional_inbounds'][0]['clients'][0]
                if collision == 'uuid':
                    self.panel.observed.clients['foreign'] = {'client': dict(ws_client, email='foreign', enable=True), 'inboundIds': [2]}
                elif collision == 'pin':
                    self.panel.observed.inbounds[1]['port'] += 1
                else:
                    self.assertTrue(self.sync())
                    client = self.panel.observed.clients[ws_client['email']]
                    if collision == 'attachment':
                        client['inboundIds'] = [1, 2]
                    else:
                        client['client']['flow'] = 'xtls-rprx-vision'
                events, tokens = copy.deepcopy(self.panel.events), self.panel.tokens
                self.assertFalse(self.sync())
                self.assertEqual(self.panel.events, events)
                self.assertEqual(self.panel.tokens, tokens)
                self.assertEqual(self.status()['code'], 'target_drift' if collision == 'pin' else 'ownership_conflict')

    def test_fault_after_each_committed_stage_guards_both_targets_and_recovers(self):
        for failure in ('template', 'client', 'restart'):
            with self.subTest(failure=failure):
                self.setUp()
                self.panel.failure = failure
                self.assertFalse(self.sync())
                self.assert_routes(self.files.load()['identity_targets'], 'cs-vpn-block')
                self.assertTrue(self.files.load()['pending'])
                self.assertTrue(self.sync(), self.status())
                self.assert_routes(self.files.load()['identity_targets'], 'cs-vpn-residential-home')

    def test_crash_after_ws_client_commit_recovers_with_its_empty_flow(self):
        class Crash(BaseException):
            pass
        def crash(kind):
            if kind == 'client' and self.panel.events[-1][1]['flow'] == '':
                raise Crash()
        self.panel.hook = crash
        with self.assertRaises(Crash):
            self.sync()
        self.assert_routes(self.files.load()['identity_targets'], 'cs-vpn-block')
        self.panel.hook = None
        self.assertTrue(self.sync(), self.status())
        self.assert_routes(self.files.load()['identity_targets'], 'cs-vpn-residential-home')

    def test_ws_route_verification_failure_never_marks_applied(self):
        route = self.panel.route
        def wrong(tag, email, network, domain):
            if tag == 'ws' and domain == 'claude.ai':
                return 'direct'
            return route(tag, email, network, domain)
        self.panel.route = wrong
        self.assertFalse(self.sync())
        self.assertEqual(self.status()['code'], 'runtime_unverified')
        self.assertIsNone(self.files.load()['applied_revision'])
        self.panel.route = route
        self.assert_routes(self.files.load()['identity_targets'], 'cs-vpn-block')

    def test_ws_runtime_origin_drift_fails_and_restores_guards(self):
        def corrupt(kind):
            if kind == 'restart':
                self.panel.observed.runtime['inbounds'][1]['listen'] = '0.0.0.0'
        self.panel.hook = corrupt
        self.assertFalse(self.sync())
        self.assertEqual(self.status()['code'], 'runtime_unverified')
        self.assert_routes(self.files.load()['identity_targets'], 'cs-vpn-block')

    def test_runtime_identity_on_another_target_cannot_pass_final_verification(self):
        def duplicate(kind):
            if kind == 'restart':
                runtime = self.panel.observed.runtime['inbounds']
                primary = next(c for c in runtime[0]['settings']['clients']
                               if c['email'] in self.desired.clients)
                runtime[1]['settings']['clients'].append(copy.deepcopy(primary))
        self.panel.hook = duplicate
        self.assertFalse(self.sync())
        self.assertEqual(self.status()['code'], 'ownership_conflict')
        self.assertIsNone(self.files.load()['applied_revision'])
        self.assertEqual(self.panel.events[-1][0], 'stop')

    def test_runtime_duplicate_managed_email_or_identity_never_passes(self):
        for target_index in (0, 1):
            for same_uuid in (False, True):
                with self.subTest(target_index=target_index, same_uuid=same_uuid):
                    self.setUp()
                    def duplicate(kind):
                        if kind == 'restart':
                            clients = self.panel.observed.runtime['inbounds'][target_index]['settings']['clients']
                            entry = copy.deepcopy(next(c for c in clients if c['email'] in self.desired.clients))
                            if not same_uuid:
                                entry['id'] = 'c698a99c-e320-41f1-9a37-44af81d7a8be'
                            clients.insert(0, entry)
                    self.panel.hook = duplicate
                    self.assertFalse(self.sync())
                    self.assertEqual(self.status()['code'], 'ownership_conflict')
                    self.assertIsNone(self.files.load()['applied_revision'])
                    self.assertEqual(self.panel.events[-1][0], 'stop')

    def test_config_contract_and_client_only_hash_compatibility(self):
        validate_config(self.config)
        old = {key: value for key, value in self.config.items() if key != 'ws_backup'}
        validate_config(old)
        for changes in ({'inbound_id': True}, {'inbound_tag': 'reality'}, {'inbound_digest': 'bad'},
                        {'tls_termination': 'none'}, {'unrecognized': True}):
            with self.subTest(changes=changes), self.assertRaises(SyncError):
                validate_config(dict(self.config, ws_backup={**self.config['ws_backup'], **changes}))
        with self.assertRaises(SyncError):
            validate_config(dict(self.config, unknown=True))
        for capability in (False, True):
            gateway = dict(self.desired.gateway, support_x25519mlkem768=capability)
            self.assertEqual(digest(gateway_server_identity(gateway)), self.config['gateway_digest'])

    def test_source_accepts_mlkem_with_old_pin_and_requires_ws_pin(self):
        path = Path(self.config['source_db'])
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('''CREATE TABLE servers(id,host,status,gateway_json);
                CREATE TABLE users(id,ingress_uuid,status);
                CREATE TABLE ips(id,address,port,protocol,config_json,status,server_id);
                CREATE TABLE bindings(user_id,ip_id);''')
            db.execute('INSERT INTO servers VALUES(?,?,?,?)',
                       ('vpn', SERVER['host'], 'active', json.dumps(dict(GATEWAY, support_x25519mlkem768=True))))
            db.execute('INSERT INTO users VALUES(?,?,?)', ('u', ROW['ingress_uuid'], 'active'))
            db.execute('INSERT INTO ips VALUES(?,?,?,?,?,?,?)',
                       ('home', ROW['address'], 1080, 'socks5', ROW['config_json'], 'available', 'vpn'))
            db.execute('INSERT INTO bindings VALUES(?,?)', ('u', 'home'))
        config = dict(self.config, gateway_digest=digest(GATEWAY))
        config.pop('ws_backup')
        before = path.read_bytes()
        self.assertEqual(source_snapshot(config).revision, ws_desired(backup=None).revision)
        self.assertEqual(path.read_bytes(), before)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('UPDATE servers SET gateway_json=?', (json.dumps(self.desired.gateway),))
        config['gateway_digest'] = digest(self.desired.gateway)
        with self.assertRaisesRegex(SyncError, 'target_drift'):
            source_snapshot(config)
        config['ws_backup'] = self.config['ws_backup']
        self.assertEqual(source_snapshot(config).revision, self.desired.revision)


if __name__ == '__main__':
    unittest.main()
