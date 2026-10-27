import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('upgrade_vpn_host1', Path(__file__).resolve().parents[1] / 'deploy/upgrade_vpn_host1.py')
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


class UpgradeTests(unittest.TestCase):
    def test_public_admin_routes_preserve_existing_subscriptions_ws_and_fallback(self):
        original = {'tunnel': 'synthetic', 'ingress': [
            {'hostname': 'sub.610247.xyz', 'path': '^/sub/.*$', 'service': 'http://127.0.0.1:8787'},
            {'hostname': 'ws.example.invalid', 'service': 'http://127.0.0.1:10000'},
            {'service': 'http_status:404'}]}
        candidate = json.loads(upgrade.tunnel_candidate(json.dumps(original).encode()))
        self.assertEqual(candidate['ingress'][2:], original['ingress'])
        self.assertEqual(candidate['tunnel'], original['tunnel'])
        self.assertEqual(json.loads(upgrade.tunnel_candidate(json.dumps(candidate).encode())), candidate)
        for path in ('/admin', '/api/auth/login', '/api/servers/vpn-host1/integration'):
            self.assertTrue(any(re.fullmatch(rule['path'], path) for rule in upgrade.ADMIN_ROUTES))
        for path in ('/', '/healthz', '/panel', '/admin/extra', '/apianother'):
            self.assertFalse(any(re.fullmatch(rule['path'], path) for rule in upgrade.ADMIN_ROUTES))

    def test_conflicting_tunnel_route_refused(self):
        route = dict(upgrade.ADMIN_ROUTES[0], service='http://foreign.invalid')
        with self.assertRaisesRegex(ValueError, 'tunnel_route_conflict'):
            upgrade.tunnel_candidate(json.dumps({'ingress': [route, {'service': 'http_status:404'}]}).encode())

    def test_rollback_drift_checked_before_any_service_or_file_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current, unit, tunnel = root / 'current', root / 'unit', root / 'tunnel'
            current.symlink_to('/synthetic/new')
            unit.write_bytes(b'newunit')
            tunnel.write_bytes(b'administrator edit')
            (root / 'receipt.json').write_text(json.dumps({
                'old_release': '/synthetic/old', 'release': '/synthetic/new',
                'unit_old_sha': upgrade.sha(b'oldunit'), 'unit_sha': upgrade.sha(b'newunit'),
                'tunnel_old_sha': upgrade.sha(b'old'), 'tunnel_sha': upgrade.sha(b'new')}))
            with mock.patch.multiple(upgrade, CURRENT=current, UNIT=unit, TUNNEL=tunnel), mock.patch.object(upgrade, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'rollback_tunnel_drift'):
                    upgrade.rollback(root)
            run.assert_not_called()
            self.assertEqual(current.readlink(), Path('/synthetic/new'))
            self.assertEqual(unit.read_bytes(), b'newunit')

    def test_atomic_write_permissions_and_replace(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'receipt.json'
            upgrade.write(path, b'synthetic', 0o600)
            self.assertEqual(path.read_bytes(), b'synthetic')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            upgrade.write(path, b'updated', 0o600)
            self.assertEqual(path.read_bytes(), b'updated')


if __name__ == '__main__':
    unittest.main()
