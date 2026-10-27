import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from server.inspect_host import REMOTE_INSPECTION, SSH_OPTIONS, configured_alias, inspect, main, public_options, write_profile
from tests.test_reality_gateway import REALITY


class InspectHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / 'config'
        self.config.write_text('Host vpn-host1\n HostName fixture.example\n')

    def tearDown(self):
        self.temp.cleanup()

    def test_only_explicit_configured_alias_allowed_with_bounded_includes(self):
        included = self.root / 'config.d'
        included.mkdir()
        (included / 'one').write_text('Host included-alias\n HostName host.example\n')
        self.config.write_text('Host *\n User fixture\nInclude config.d/*\nHost vpn-host1\n HostName fixture.example\n')
        self.assertTrue(configured_alias('vpn-host1', self.config))
        self.assertTrue(configured_alias('included-alias', self.config))
        for target in ['vpn-hosts1', 'fixture.example', '127.0.0.1', '-oProxyCommand=secret', 'a;touch-secret', 'a\nsecret']:
            self.assertFalse(configured_alias(target, self.config))
        with patch('server.inspect_host.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'explicit configured SSH alias required'):
                inspect('vpn-hosts1', self.config)
            run.assert_not_called()
        self.config.write_text('Host vpn-host1\nMatch exec "echo secret"\n')
        with self.assertRaisesRegex(ValueError, 'unsupported SSH configuration'):
            configured_alias('vpn-host1', self.config)

    def test_fake_ssh_commands_fixed_and_profile_contains_public_fields_only(self):
        calls = []
        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            output = 'hostname fixture.example\nuser root\n' if '-G' in command else json.dumps(REALITY)
            return subprocess.CompletedProcess(command, 0, output, '')
        with patch('server.inspect_host.subprocess.run', side_effect=fake_run):
            profile = inspect('vpn-host1', self.config)
        self.assertEqual(set(profile), {'id', 'name', 'host', 'region', 'status', 'gateway'})
        self.assertEqual(profile['host'], 'fixture.example')
        self.assertEqual(profile['gateway'], REALITY)
        self.assertEqual(len(calls), 2)
        for command, kwargs in calls:
            self.assertEqual(command[:1], ['ssh'])
            self.assertEqual(command[1:1 + len(SSH_OPTIONS)], SSH_OPTIONS)
            self.assertNotIn('shell', kwargs)
            self.assertTrue(kwargs['check'])
            self.assertLessEqual(kwargs['timeout'], 25)
        self.assertIn('BatchMode=yes', calls[1][0])
        self.assertIn('ClearAllForwardings=yes', calls[1][0])
        self.assertIn('ForwardAgent=no', calls[1][0])
        self.assertIn('RequestTTY=no', calls[1][0])
        self.assertIn('PermitLocalCommand=no', calls[1][0])
        self.assertIn('RemoteCommand=none', calls[1][0])
        self.assertIn('mode=ro', REMOTE_INSPECTION)
        self.assertIn("enable=1 AND protocol='vless' AND port=443", REMOTE_INSPECTION)
        self.assertNotIn(',settings,', REMOTE_INSPECTION)
        self.assertNotIn('privateKey', REMOTE_INSPECTION)

    def test_remote_constant_selects_one_public_reality_profile_without_credentials(self):
        class FakeDB:
            def execute(self, query):
                self.query = query
                return self
            def fetchall(self):
                return [(443, 'existing-reality', json.dumps({'security': 'reality', 'network': 'tcp',
                    'realitySettings': {'serverNames': ['public.example'], 'shortIds': ['12ab'],
                                       'settings': {'publicKey': REALITY['public_key']}, 'privateKey': 'never-export'}}))]
        output = io.StringIO()
        database = FakeDB()
        with patch('sqlite3.connect', return_value=database) as connect, contextlib.redirect_stdout(output):
            exec(REMOTE_INSPECTION, {})
        self.assertEqual(json.loads(output.getvalue()), REALITY)
        self.assertNotIn('never-export', output.getvalue())
        self.assertEqual(connect.call_args.kwargs, {'uri': True})
        self.assertIn('mode=ro', connect.call_args.args[0])

    def test_remote_sql_projects_public_fields_instead_of_loading_private_stream(self):
        database = sqlite3.connect(':memory:')
        self.addCleanup(database.close)
        database.execute('CREATE TABLE inbounds(port,tag,stream_settings,settings,enable,protocol)')
        stream = {'security': 'reality', 'network': 'tcp', 'realitySettings': {
            'serverNames': [REALITY['server_name']], 'shortIds': [REALITY['short_id']],
            'privateKey': 'never-read-private-key', 'settings': {'publicKey': REALITY['public_key']}}}
        database.execute('INSERT INTO inbounds VALUES(?,?,?,?,?,?)',
                         (443, REALITY['inbound_tag'], json.dumps(stream),
                          '{"clients":[{"id":"never-read-client"}]}', 1, 'vless'))
        output = io.StringIO()
        queries = []
        database.set_trace_callback(queries.append)
        with patch('sqlite3.connect', return_value=database), contextlib.redirect_stdout(output):
            exec(REMOTE_INSPECTION, {})
        self.assertEqual(json.loads(output.getvalue()), REALITY)
        self.assertEqual(len(queries), 1)
        self.assertIn('json_extract(stream_settings', queries[0])
        self.assertNotIn('SELECT port,tag,stream_settings', queries[0])
        self.assertNotIn('privateKey', queries[0])
        self.assertNotIn('clients', queries[0])

    def test_missing_malformed_or_private_response_never_creates_output(self):
        output = self.root / 'out.json'
        for response in [dict(REALITY, private_key='secret'), {**REALITY, 'public_key': None}, {'settings': {'clients': ['secret']}}]:
            with patch('server.inspect_host.Path.home', return_value=self.root), patch('server.inspect_host.inspect', side_effect=ValueError('secret')), contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(main(['vpn-host1', '--output', str(output)]), 1)
                self.assertEqual(stdout.getvalue(), '')
                self.assertFalse(output.exists())
            with patch('server.inspect_host.subprocess.run', side_effect=[subprocess.CompletedProcess([], 0, 'hostname fixture.example\n', ''), subprocess.CompletedProcess([], 0, json.dumps(response), '')]):
                with self.assertRaises(ValueError):
                    inspect('vpn-host1', self.config)

    def test_private_exclusive_nofollow_write_and_no_stdout_profile(self):
        output = self.root / 'public.json'
        profile = {'id': 'vpn-host1', 'gateway': REALITY}
        write_profile(output, profile)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(json.loads(output.read_text()), profile)
        with self.assertRaises(FileExistsError):
            write_profile(output, {})
        link = self.root / 'symlink.json'
        link.symlink_to(output)
        with self.assertRaises(FileExistsError):
            write_profile(link, {})
        fresh = self.root / 'fresh.json'
        with patch('server.inspect_host.inspect', return_value=profile), contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(['vpn-host1', '--output', str(fresh)]), 0)
            self.assertEqual(stdout.getvalue(), '')
            self.assertNotIn(REALITY['public_key'], stderr.getvalue())
        self.assertEqual(json.loads(fresh.read_text()), profile)

    def test_output_flag_is_required_before_ssh(self):
        with patch('server.inspect_host.subprocess.run') as run, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(['vpn-host1'])
        self.assertEqual(error.exception.code, 2)
        run.assert_not_called()

    def test_explicit_ws_metadata_and_mlkem_do_not_mutate_default_profile(self):
        profile = {'id': 'vpn-host1', 'gateway': dict(REALITY)}
        ws = {'address': 'backup.example', 'port': 8443, 'server_name': 'tls.example',
              'host': 'ws.example', 'path': '/socket?ed=2048', 'inbound_tag': 'managed-ws',
              'tls_termination': 'external'}
        source = self.root / 'ws.json'
        source.write_text(json.dumps(ws))
        merged = public_options(profile, source, 'enabled')
        self.assertEqual(merged['gateway']['ws_backup'], ws)
        self.assertIs(merged['gateway']['support_x25519mlkem768'], True)
        self.assertEqual(profile['gateway'], REALITY)
        self.assertIs(public_options(profile), profile)
        self.assertIs(public_options(profile, reality_mlkem='disabled')['gateway']['support_x25519mlkem768'], False)

    def test_ws_metadata_rejects_credentials_unknown_fields_and_unsafe_inputs(self):
        profile = {'id': 'vpn-host1', 'gateway': dict(REALITY)}
        ws = {'address': 'backup.example', 'port': 443, 'server_name': 'tls.example',
              'host': 'ws.example', 'path': '/socket', 'inbound_tag': 'managed-ws',
              'tls_termination': 'xray'}
        source = self.root / 'ws.json'
        variants = [None, [], {**ws, 'private_key': 'never-export'},
                    {**ws, 'clients': []}, {**ws, 'host': 'ws.example\r\nAuthorization: secret'},
                    {**ws, 'path': '//foreign.example'}, {**ws, 'port': True},
                    {**ws, 'inbound_tag': REALITY['inbound_tag']},
                    {**ws, 'tls_termination': 'guess'}]
        for variant in variants:
            with self.subTest(variant=variant):
                source.write_text(json.dumps(variant))
                with self.assertRaises(ValueError):
                    public_options(profile, source)
        source.write_bytes(b' ' * 16385)
        with self.assertRaises(ValueError):
            public_options(profile, source)

    def test_cli_explicit_metadata_is_saved_without_ws_inspection_or_secret_output(self):
        ws = {'address': 'backup.example', 'port': 443, 'server_name': 'tls.example',
              'host': 'ws.example', 'path': '/socket?mode=1', 'inbound_tag': 'managed-ws',
              'tls_termination': 'external'}
        source, target = self.root / 'ws.json', self.root / 'profile.json'
        source.write_text(json.dumps(ws))
        profile = {'id': 'vpn-host1', 'gateway': dict(REALITY)}
        with patch('server.inspect_host.inspect', return_value=profile) as inspector, contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(['vpn-host1', '--output', str(target), '--ws-backup-profile', str(source),
                                   '--reality-mlkem', 'enabled']), 0)
        self.assertEqual(inspector.call_count, 1)
        self.assertEqual(stdout.getvalue(), '')
        self.assertNotIn(ws['address'], stderr.getvalue())
        self.assertIn('remain unverified', stderr.getvalue())
        saved = json.loads(target.read_text())
        self.assertEqual(saved['gateway']['ws_backup'], ws)
        self.assertIs(saved['gateway']['support_x25519mlkem768'], True)

    def test_invalid_ws_file_never_writes_output_or_echoes_contents(self):
        source, target = self.root / 'ws.json', self.root / 'profile.json'
        source.write_text('{"password":"never-export"}')
        with patch('server.inspect_host.inspect', return_value={'gateway': dict(REALITY)}), contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(['vpn-host1', '--output', str(target), '--ws-backup-profile', str(source)]), 1)
        self.assertFalse(target.exists())
        self.assertEqual(stdout.getvalue(), '')
        self.assertNotIn('never-export', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
