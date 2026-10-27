from __future__ import annotations

import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from server.gateway import xray_merge
from server.xui_contract import desired_revision
from server.xui_sync import (Desired, Files, Observed, PanelAPI, SyncError, digest, inbound_identity,
                             reconcile, source_snapshot)

UUID = 'b9303c18-3580-4757-8c75-50297d895d5b'
GATEWAY = {'kind': 'xray-vless-reality', 'port': 443, 'server_name': 'example.com',
           'public_key': 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA', 'short_id': 'ab12',
           'inbound_tag': 'reality', 'flow': 'xtls-rprx-vision'}
SERVER = {'id': 'vpn', 'host': 'vpn.example.com', 'status': 'active', 'gateway': GATEWAY}
ROW = {'ingress_uuid': UUID, 'ip_id': 'home', 'address': '8.8.4.4', 'port': 1080,
       'protocol': 'socks5', 'config_json': '{"username":"fixture-user","password":"fixture-password"}'}


def desired(rows=None, status='active'):
    server = dict(SERVER, status=status)
    merge = xray_merge(server, GATEWAY, [ROW] if rows is None else rows)
    return Desired(desired_revision(server, merge), merge)


def fixture():
    inbound = {'id': 1, 'tag': 'reality', 'protocol': 'vless', 'port': 443, 'listen': '',
               'enable': True, 'nodeId': None, 'settings': {'clients': [], 'decryption': 'none'},
               'sniffing': {}, 'streamSettings': {'security': 'reality', 'network': 'tcp',
                                               'realitySettings': {'privateKey': 'fixture-private-key'}}}
    ws = dict(copy.deepcopy(inbound), id=2, tag='ws', port=10000,
              streamSettings={'security': 'none', 'network': 'ws'})
    template = {'outbounds': [{'tag': 'direct', 'protocol': 'freedom', 'settings': {}}],
                'routing': {'rules': [{'type': 'field', 'network': 'tcp,udp', 'outboundTag': 'direct'}]},
                'log': {'loglevel': 'warning'}}
    client = {'id': '49db8d8a-c5ac-4b52-8d77-98bd42468539', 'email': 'original', 'enable': True,
              'flow': 'xtls-rprx-vision', 'subId': 'original-sub'}
    return Observed(template, 'https://example.com', [inbound, ws],
                    {'original': {'client': client, 'inboundIds': [1]}}, {}, 'pid:1', 'http://unused')


class FakePanel:
    def __init__(self):
        self.observed = fixture()
        self.events = []
        self.tokens = 0
        self.failure = None
        self.hook = None
        self.render()

    def render(self):
        self.observed.runtime = copy.deepcopy(self.observed.template)
        self.observed.runtime['inbounds'] = copy.deepcopy(self.observed.inbounds)
        for inbound in self.observed.runtime['inbounds']:
            inbound['settings']['clients'] = [copy.deepcopy(r['client']) for r in self.observed.clients.values()
                                              if inbound['id'] in r['inboundIds'] and r['client']['enable']]
        self.observed.runtime['routing']['rules'].insert(0, {'type': 'field', 'inboundTag': ['api'],
                                                            'outboundTag': 'api'})

    def observe(self):
        return copy.deepcopy(self.observed)

    def factory(self, base):
        self.tokens += 1
        return self

    def after(self, kind):
        if self.hook:
            self.hook(kind)
        if self.failure == kind:
            self.failure = None
            raise SyncError('apply_failed')

    def template(self, template, test_url):
        self.events.append(('template', copy.deepcopy(template)))
        self.observed.template = copy.deepcopy(template)
        self.observed.test_url = test_url
        self.render()
        self.after('template')

    def client(self, client, inbound_id, exists):
        self.events.append(('client', copy.deepcopy(client)))
        tag = next(i['tag'] for i in self.observed.inbounds if i['id'] == inbound_id)
        for network in ('tcp', 'udp'):
            assert self.route(tag, client['email'], network, 'example.com') == 'cs-vpn-block'
        self.observed.clients[client['email']] = {'client': copy.deepcopy(client), 'inboundIds': [inbound_id]}
        self.render()
        self.after('client')

    def restart(self):
        self.events.append(('restart', None))
        self.observed.process = 'pid:' + str(int(self.observed.process.split(':')[1]) + 1)
        self.render()
        self.after('restart')

    def stop(self):
        self.events.append(('stop', None))
        self.observed.process = ''

    def route(self, tag, email, network, domain):
        for rule in self.observed.runtime['routing']['rules']:
            if rule.get('inboundTag') and tag not in rule['inboundTag']:
                continue
            if rule.get('user') and email not in rule['user']:
                continue
            if rule.get('network') and network not in rule['network'].split(','):
                continue
            return rule['outboundTag']
        return 'direct'


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.files = Files(Path(self.temp.name) / 'state.json', Path(self.temp.name) / 'status.json')
        self.panel = FakePanel()
        self.config = {'server_id': 'vpn', 'expected_host': 'vpn.example.com', 'inbound_id': 1,
                       'inbound_tag': 'reality', 'gateway_digest': digest(GATEWAY),
                       'inbound_digest': digest(inbound_identity(self.panel.observed.inbounds[0]))}
        self.desired = desired()

    def run_sync(self):
        return reconcile(self.config, self.files, lambda: self.desired, self.panel.observe, self.panel.factory)

    def status(self):
        return json.loads(self.files.status_path.read_text())

    def test_create_preserves_original_and_ws_and_guards_before_client(self):
        before = self.panel.observe()
        self.assertTrue(self.run_sync())
        self.assertEqual(before.inbounds, self.panel.observed.inbounds)
        self.assertEqual(before.clients['original'], self.panel.observed.clients['original'])
        self.assertEqual(before.template['outbounds'][0], self.panel.observed.template['outbounds'][0])
        self.assertEqual(before.template['routing']['rules'][-1], self.panel.observed.template['routing']['rules'][-1])
        self.assertEqual(self.panel.events[0][0], 'template')
        email = self.desired.merge['clients'][0]['email']
        self.assertEqual(self.panel.route('reality', email, 'tcp', 'claude.ai'), 'cs-vpn-residential-home')
        self.assertEqual(self.panel.route('reality', email, 'udp', 'claude.ai'), 'cs-vpn-block')
        self.assertEqual(self.status()['state'], 'applied')
        self.assertEqual(self.files.state_path.stat().st_mode & 0o777, 0o600)

    def test_noop_has_zero_panel_writes_restart_or_token(self):
        self.assertTrue(self.run_sync())
        events, tokens = copy.deepcopy(self.panel.events), self.panel.tokens
        self.assertTrue(self.run_sync())
        self.assertEqual(events, self.panel.events)
        self.assertEqual(tokens, self.panel.tokens)

    def test_panel_api_rule_normalization_keeps_managed_users_guarded(self):
        api_rule = {'type': 'field', 'inboundTag': ['api'], 'outboundTag': 'api'}
        self.panel.observed.template['routing']['rules'].insert(0, api_rule)
        def normalize(kind):
            if kind == 'template':
                rules = self.panel.observed.template['routing']['rules']
                rules.remove(api_rule)
                rules.insert(0, copy.deepcopy(api_rule))
                self.panel.render()
        self.panel.hook = normalize
        self.assertTrue(self.run_sync(), self.status())
        email = self.desired.merge['clients'][0]['email']
        self.assertEqual(self.panel.route('reality', email, 'tcp', 'example.com'), 'cs-vpn-residential-home')
        self.assertEqual(self.panel.route('reality', email, 'udp', 'example.com'), 'cs-vpn-block')
        self.assertEqual(self.panel.observed.template['routing']['rules'][0], api_rule)

    def test_broad_direct_rule_cannot_precede_managed_guards(self):
        direct_rule = copy.deepcopy(self.panel.observed.template['routing']['rules'][0])
        def reorder(kind):
            if kind == 'template':
                rules = self.panel.observed.template['routing']['rules']
                rules.remove(direct_rule)
                rules.insert(0, copy.deepcopy(direct_rule))
                self.panel.render()
        self.panel.hook = reorder
        self.assertFalse(self.run_sync())
        self.assertEqual(self.status()['state'], 'blocked')
        self.assertEqual(self.status()['code'], 'runtime_unverified')

    def test_rebind_keeps_uuid_and_panel_subscription_and_restarts_under_guard(self):
        self.assertTrue(self.run_sync())
        email = self.desired.merge['clients'][0]['email']
        before = copy.deepcopy(self.panel.observed.clients[email])
        self.panel.events.clear()
        self.desired = desired([dict(ROW, ip_id='home2', address='1.1.1.1')])
        self.assertTrue(self.run_sync())
        self.assertEqual(before, self.panel.observed.clients[email])
        self.assertEqual([kind for kind, _ in self.panel.events].count('restart'), 2)
        first_restart = next(n for n, item in enumerate(self.panel.events) if item[0] == 'restart')
        self.assertEqual(first_restart, 1)
        self.assertEqual(self.panel.route('reality', email, 'tcp', 'example.com'), 'cs-vpn-residential-home2')

    def test_empty_source_and_server_disable_retain_disabled_tombstones(self):
        for transition in (desired([]), desired(status='disabled')):
            with self.subTest(revision=transition.revision):
                self.desired = desired()
                self.assertTrue(self.run_sync())
                email = self.desired.merge['clients'][0]['email']
                self.desired = transition
                self.assertTrue(self.run_sync())
                self.assertFalse(self.panel.observed.clients[email]['client']['enable'])
                self.assertIn(email, self.files.load()['identities'])
                self.assertEqual(self.panel.route('reality', email, 'tcp', 'example.com'), 'cs-vpn-block')

    def test_email_uuid_and_tag_collisions_refused_before_token(self):
        for kind in ('email', 'uuid', 'tag'):
            with self.subTest(kind=kind):
                self.panel = FakePanel()
                client = self.desired.merge['clients'][0]
                if kind == 'tag':
                    self.panel.observed.template['outbounds'].append(self.desired.merge['outbounds'][0])
                else:
                    email = client['email'] if kind == 'email' else 'foreign-email'
                    self.panel.observed.clients[email] = {'client': dict(client, email=email), 'inboundIds': [1]}
                self.assertFalse(self.run_sync())
                self.assertEqual(self.status()['code'], 'ownership_conflict')
                self.assertEqual(self.panel.tokens, 0)
                self.assertEqual(self.panel.events, [])

    def test_prefix_alone_never_claims_object(self):
        self.panel.observed.template['outbounds'].append({'tag': 'cs-vpn-foreign', 'protocol': 'freedom'})
        self.assertTrue(self.run_sync())
        self.assertIn({'tag': 'cs-vpn-foreign', 'protocol': 'freedom'}, self.panel.observed.template['outbounds'])

    def test_extra_attachment_refuses_update(self):
        self.assertTrue(self.run_sync())
        email = self.desired.merge['clients'][0]['email']
        self.panel.observed.clients[email]['inboundIds'] = [1, 2]
        self.panel.events.clear()
        self.desired = desired([])
        self.assertFalse(self.run_sync())
        self.assertEqual(self.status()['code'], 'ownership_conflict')
        self.assertEqual(self.panel.events, [])

    def test_failures_after_commit_leave_deny_and_recover_from_pending(self):
        for failure in ('template', 'client', 'restart'):
            with self.subTest(failure=failure):
                self.setUp()
                self.panel.failure = failure
                self.assertFalse(self.run_sync())
                self.assertEqual(self.status()['state'], 'blocked')
                self.assertTrue(self.files.load()['pending'])
                email = self.desired.merge['clients'][0]['email']
                self.assertEqual(self.panel.route('reality', email, 'tcp', 'example.com'), 'cs-vpn-block')
                self.assertTrue(self.run_sync())
                self.assertEqual(self.status()['state'], 'applied')

    def test_initial_empty_snapshot_converges_and_then_is_noop(self):
        self.desired = desired([])
        self.assertTrue(self.run_sync())
        calls = len(self.panel.events)
        self.assertTrue(self.run_sync())
        self.assertEqual(len(self.panel.events), calls)
        self.assertEqual(self.status()['managed_count'], 0)

    def test_crash_after_client_commit_recovers_exact_pending_identity(self):
        class Crash(BaseException):
            pass
        def crash(kind):
            if kind == 'client':
                raise Crash()
        self.panel.hook = crash
        with self.assertRaises(Crash):
            self.run_sync()
        self.assertTrue(self.files.load()['pending'])
        email = self.desired.merge['clients'][0]['email']
        self.assertEqual(self.panel.route('reality', email, 'tcp', 'example.com'), 'cs-vpn-block')
        self.panel.hook = None
        self.assertTrue(self.run_sync())

    def test_runtime_only_tag_collision_blocks_before_mutations(self):
        self.panel.observed.runtime['outbounds'].append(copy.deepcopy(self.desired.merge['outbounds'][0]))
        self.assertFalse(self.run_sync())
        self.assertEqual(self.status()['code'], 'ownership_conflict')
        self.assertEqual(self.panel.tokens, 0)

    def test_route_verification_failure_never_marks_applied(self):
        original = self.panel.route
        def mismatch(tag, email, network, domain):
            if domain == 'claude.ai':
                return 'direct'
            return original(tag, email, network, domain)
        self.panel.route = mismatch
        self.assertFalse(self.run_sync())
        self.assertIsNone(self.files.load()['applied_revision'])
        self.assertEqual(self.status()['code'], 'runtime_unverified')
        email = self.desired.merge['clients'][0]['email']
        self.assertEqual(original('reality', email, 'tcp', 'example.com'), 'cs-vpn-block')

    def test_source_change_during_apply_keeps_guard(self):
        original = self.desired
        def change(kind):
            if kind == 'client':
                self.desired = desired([])
        self.panel.hook = change
        self.assertFalse(self.run_sync())
        self.assertEqual(self.status()['code'], 'source_changed')
        email = original.merge['clients'][0]['email']
        self.assertEqual(self.panel.route('reality', email, 'tcp', 'example.com'), 'cs-vpn-block')

    def test_source_failure_never_becomes_revocation(self):
        self.assertTrue(self.run_sync())
        self.panel.events.clear()
        def fail():
            raise SyncError('source_invalid')
        self.assertFalse(reconcile(self.config, self.files, fail, self.panel.observe, self.panel.factory))
        self.assertEqual(self.panel.events, [])
        self.assertEqual(self.status()['code'], 'source_invalid')

    def test_target_drift_refuses_all_writes(self):
        self.panel.observed.inbounds[0]['streamSettings']['realitySettings']['privateKey'] = 'changed'
        self.assertFalse(self.run_sync())
        self.assertEqual(self.status()['code'], 'target_drift')
        self.assertEqual(self.panel.tokens, 0)

    def test_public_receipt_has_no_client_proxy_credentials_or_exception(self):
        def fail(_):
            raise RuntimeError('fixture-password private/path 192.0.2.1')
        self.assertFalse(reconcile(self.config, self.files, lambda: self.desired, self.panel.observe, fail))
        text = self.files.status_path.read_text()
        for forbidden in ('fixture-password', '192.0.2.1', UUID, 'private/path', 'reality'):
            self.assertNotIn(forbidden, text)
        self.assertEqual(self.status()['code'], 'internal_error')

    def test_panel_drift_reconciles_instead_of_false_noop(self):
        self.assertTrue(self.run_sync())
        self.panel.observed.runtime['routing']['rules'] = []
        tokens = self.panel.tokens
        self.assertTrue(self.run_sync())
        self.assertGreater(self.panel.tokens, tokens)

    def test_unmanaged_edit_during_apply_blocks_without_overwriting(self):
        changed = False
        def edit(kind):
            nonlocal changed
            if kind == 'client' and not changed:
                changed = True
                self.panel.observed.template['log']['loglevel'] = 'error'
        self.panel.hook = edit
        self.assertFalse(self.run_sync())
        self.assertEqual(self.status()['code'], 'ownership_conflict')
        self.assertEqual(self.panel.observed.template['log']['loglevel'], 'error')
        self.assertEqual(self.panel.events[-1][0], 'stop')


class SnapshotTests(unittest.TestCase):
    def test_source_readonly_matches_shared_revision_and_preserves_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.db'
            with sqlite3.connect(path) as db:
                db.executescript('''CREATE TABLE servers(id,host,status,gateway_json);
                    CREATE TABLE users(id,ingress_uuid,status);
                    CREATE TABLE ips(id,address,port,protocol,config_json,status,server_id);
                    CREATE TABLE bindings(user_id,ip_id);''')
                db.execute('INSERT INTO servers VALUES(?,?,?,?)', ('vpn', 'vpn.example.com', 'active', json.dumps(GATEWAY)))
                db.execute('INSERT INTO users VALUES(?,?,?)', ('u', UUID, 'active'))
                db.execute('INSERT INTO ips VALUES(?,?,?,?,?,?,?)', ('home', ROW['address'], 1080, 'socks5', ROW['config_json'], 'available', 'vpn'))
                db.execute('INSERT INTO bindings VALUES(?,?)', ('u', 'home'))
            before = path.read_bytes()
            config = {'source_db': str(path), 'server_id': 'vpn', 'expected_host': 'vpn.example.com',
                      'gateway_digest': digest(GATEWAY), 'inbound_tag': 'reality'}
            self.assertEqual(source_snapshot(config).revision, desired().revision)
            self.assertEqual(path.read_bytes(), before)
            with sqlite3.connect(path) as db:
                db.execute("UPDATE servers SET status='disabled'")
            self.assertEqual(source_snapshot(config).revision, desired(status='disabled').revision)

    def test_panel_snapshot_reads_normalized_sqlite_without_writes(self):
        from server import xui_sync
        panel = FakePanel()
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'panel.db'
            runtime = Path(directory) / 'config.json'
            runtime.write_text(json.dumps(panel.observed.runtime))
            with sqlite3.connect(database) as db:
                db.executescript("""CREATE TABLE settings(key,value);
                    CREATE TABLE inbounds(id,tag,protocol,port,listen,enable,node_id,settings,stream_settings,sniffing);
                    CREATE TABLE clients(id,email,uuid,sub_id,flow,enable,reverse);
                    CREATE TABLE client_inbounds(client_id,inbound_id,flow_override,created_at);""")
                settings = {'webPort': '24680', 'webBasePath': '/private/', 'webListen': '127.0.0.1',
                            'xrayTemplateConfig': json.dumps(panel.observed.template),
                            'xrayOutboundTestUrl': panel.observed.test_url}
                db.executemany('INSERT INTO settings VALUES(?,?)', settings.items())
                for inbound in panel.observed.inbounds:
                    db.execute('INSERT INTO inbounds VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (inbound['id'], inbound['tag'], inbound['protocol'], inbound['port'],
                                inbound['listen'], 1, None, json.dumps(inbound['settings']),
                                json.dumps(inbound['streamSettings']), '{}'))
                db.execute('INSERT INTO clients VALUES(?,?,?,?,?,?,?)', (1, 'original', UUID, 'sub',
                           'xtls-rprx-vision', 1, ''))
                db.execute('INSERT INTO client_inbounds VALUES(?,?,?,?)', (1, 1, '', 0))
            before = database.read_bytes()
            with mock.patch.object(xui_sync, 'PANEL_DB', database), mock.patch.object(xui_sync, 'RUNTIME', runtime), \
                    mock.patch.object(xui_sync, 'process_marker', return_value='pid:1'):
                observed = xui_sync.panel_snapshot()
            self.assertEqual(observed.base_url, 'http://127.0.0.1:24680/private/panel/api')
            self.assertEqual(observed.clients['original']['client']['id'], UUID)
            self.assertEqual(observed.clients['original']['inboundIds'], [1])
            self.assertEqual(database.read_bytes(), before)
            self.assertEqual(digest(inbound_identity(observed.inbounds[0])),
                             digest(inbound_identity(panel.observed.inbounds[0])))

    def test_revision_ignores_display_fields_and_accepts_gateway_json(self):
        merge = desired().merge
        alternate = {**SERVER, 'name': 'changed', 'gateway_json': json.dumps(GATEWAY)}
        alternate.pop('gateway')
        self.assertEqual(desired_revision(SERVER, merge), desired_revision(alternate, merge))

    def test_api_uses_flat_update_and_wrapped_add(self):
        api = PanelAPI.__new__(PanelAPI)
        api.request = mock.Mock()
        client = {'id': UUID, 'email': 'a@b', 'enable': False}
        api.client(client, 1, False)
        api.request.assert_called_with('/clients/add', {'client': client, 'inboundIds': [1]})
        api.client(client, 1, True)
        api.request.assert_called_with('/clients/update/a%40b?inboundIds=1', client)
        api.template({'outbounds': []}, 'https://example.com')
        api.request.assert_called_with('/xray/update', {'xraySetting': '{"outbounds": []}',
                                                       'outboundTestUrl': 'https://example.com'}, True)

    def test_route_waits_for_grpc_readiness_after_restart(self):
        api = PanelAPI.__new__(PanelAPI)
        api.base_url, api.token = 'http://127.0.0.1:24680/private/panel/api', 'synthetic-token'
        api.opener = mock.Mock()
        responses = [
            {'success': False, 'msg': 'rpc error: code = Unavailable desc = dial tcp 127.0.0.1:62789: connection refused'},
            {'success': True, 'obj': {'matched': True, 'outboundTag': 'cs-vpn-block'}},
        ]
        contexts = []
        for body in responses:
            context = mock.MagicMock()
            context.__enter__.return_value.read.return_value = json.dumps(body).encode()
            contexts.append(context)
        api.opener.open.side_effect = contexts
        self.assertEqual(api.route('reality', 'fixture@users.invalid', 'tcp', 'example.com'), 'cs-vpn-block')
        self.assertEqual(api.opener.open.call_count, 2)

    def test_route_does_not_retry_business_errors(self):
        api = PanelAPI.__new__(PanelAPI)
        api.request = mock.Mock(side_effect=SyncError('apply_failed'))
        with self.assertRaisesRegex(SyncError, 'apply_failed'):
            api.route('reality', 'fixture@users.invalid', 'tcp', 'example.com')
        self.assertEqual(api.request.call_count, 1)


if __name__ == '__main__':
    unittest.main()
