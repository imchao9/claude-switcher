import importlib.util
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('probe_binding', Path(__file__).resolve().parents[1] / 'deploy/probe_xray_binding.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class DisabledProbeTests(unittest.TestCase):
    def test_failed_endpoint_is_not_proof_of_revocation(self):
        self.check(False, True, False)

    def test_still_present_identity_is_not_proof_of_revocation(self):
        self.check(True, False, False)

    def test_positive_control_and_removed_identity_required(self):
        self.check(True, True, True)

    def check(self, control_passed, absent, expected):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / 'test.db'
            with closing(sqlite3.connect(database)) as db, db:
                db.execute('CREATE TABLE users(id TEXT,ingress_uuid TEXT)')
                db.execute('INSERT INTO users VALUES (?,?)', ('disabled-test', 'synthetic-uuid'))
            runtime = {'inbounds': [{'settings': {'clients': [] if absent else [{'id': 'synthetic-uuid'}]}}]}
            responses = [{'client_started': True, 'connected': False, 'passed': False}, {'passed': control_passed}]
            with mock.patch.object(probe, 'connection_probe', side_effect=responses), mock.patch.object(Path, 'read_bytes', return_value=json.dumps(runtime).encode()):
                result = probe.probe(database, 'test-server', 'disabled-test', expect_disabled=True, control_user_id='active-test')
            self.assertEqual(result['passed'], expected)
            self.assertEqual(result['disabled_connection_rejected'], expected)

    def test_missing_or_same_control_refused_before_network(self):
        with mock.patch.object(probe, 'connection_probe') as connect:
            for control in (None, 'disabled-test'):
                with self.assertRaisesRegex(ValueError, 'distinct_enabled_control_required'):
                    probe.probe(Path('/synthetic'), 'test-server', 'disabled-test', expect_disabled=True, control_user_id=control)
            connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
