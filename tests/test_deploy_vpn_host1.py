import hashlib
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

MODULE = Path(__file__).resolve().parents[1] / "deploy/install_vpn_host1.py"
spec = importlib.util.spec_from_file_location("deploy_vpn_host1", MODULE)
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeploymentValidationTests(unittest.TestCase):
    def test_tunnel_preserves_every_existing_rule_and_top_level_field(self):
        original = {"tunnel": "synthetic", "credentials-file": "/private/credentials.json", "ingress": [
            {"hostname": "sub.610247.xyz", "path": "^/sub/legacy/.*$", "service": "http://127.0.0.1:18081", "originRequest": {"connectTimeout": "10s"}},
            {"hostname": "ws.example.invalid", "service": "http://127.0.0.1:10000"},
            {"service": "http_status:404"},
        ]}
        candidate = json.loads(deploy.tunnel_candidate(json.dumps(original).encode()))
        self.assertEqual(candidate["ingress"][2:], original["ingress"])
        self.assertEqual({k: v for k, v in candidate.items() if k != "ingress"},
                         {k: v for k, v in original.items() if k != "ingress"})
        self.assertEqual(candidate["ingress"][:2], deploy.ROUTES)

    def test_routes_expose_only_token_downloads(self):
        import re
        for path in ("/admin", "/api/users", "/healthz", "/sub/legacy/server.yaml", "/sub/cs_abc/../server.yaml"):
            self.assertFalse(any(re.fullmatch(route["path"], path) for route in deploy.ROUTES))
        for path in ("/sub/cs_abc/vpn-host1.yaml", "/s/cs_abc"):
            self.assertEqual(sum(bool(re.fullmatch(route["path"], path)) for route in deploy.ROUTES), 1)

    def test_tunnel_rejects_missing_rules_and_already_installed_routes(self):
        for value in ({}, {"ingress": []}, {"ingress": deploy.ROUTES + [{"service": "http_status:404"}]}):
            with self.assertRaises(ValueError):
                deploy.tunnel_candidate(json.dumps(value).encode())

    def test_database_hash_is_logical_and_readonly(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test.db"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("CREATE TABLE sample(value TEXT)")
                connection.execute("INSERT INTO sample VALUES ('synthetic')")
            original = path.read_bytes()
            with closing(sqlite3.connect(path)) as connection:
                expected = hashlib.sha256("\n".join(connection.iterdump()).encode()).hexdigest()
            self.assertEqual(deploy.database_hash(path), expected)
            self.assertEqual(path.read_bytes(), original)

    def test_payload_rejects_links_before_importing_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            stage.chmod(0o700)
            (stage / "manifest.json").write_text(json.dumps({"release_id": "20261025T120000Z-aabbccdd", "files": {},
                                                          "database_logical_sha256": "", "tunnel_sha256": ""}))
            (stage / "alias").symlink_to("manifest.json")
            with self.assertRaisesRegex(ValueError, "payload_file_type"):
                deploy.validate_payload(stage)

    def test_payload_detects_unlisted_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            stage.chmod(0o700)
            (stage / "manifest.json").write_text(json.dumps({"release_id": "20261025T120000Z-aabbccdd", "files": {},
                                                          "database_logical_sha256": "", "tunnel_sha256": ""}))
            (stage / "unexpected").write_text("synthetic")
            with self.assertRaisesRegex(ValueError, "payload_hashes"):
                deploy.validate_payload(stage)

    def test_rollback_is_repeatable_and_preserves_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backup, state = root / "backup", root / "state"
            backup.mkdir()
            state.mkdir()
            (state / "subscriptions.db").write_bytes(b"private synthetic database")
            unit, current, tunnel = root / "service", root / "current", root / "tunnel"
            release = root / "release"
            release.mkdir()
            unit.write_bytes(b"synthetic unit")
            current.symlink_to(release)
            tunnel.write_bytes(b"new synthetic tunnel")
            (backup / "tunnel.original").write_bytes(b"original synthetic tunnel")
            info = tunnel.stat()
            (backup / "receipt.json").write_text(json.dumps({
                "release": str(release), "unit": True, "current": True, "tunnel": True,
                "unit_sha256": deploy.sha(unit.read_bytes()),
                "tunnel_original_sha256": deploy.sha(b"original synthetic tunnel"),
                "tunnel_installed_sha256": deploy.sha(tunnel.read_bytes()),
                "tunnel_mode": 0o600, "tunnel_uid": info.st_uid, "tunnel_gid": info.st_gid,
            }))
            with mock.patch.multiple(deploy, UNIT=unit, CURRENT=current, TUNNEL=tunnel), mock.patch.object(deploy, "run") as run:
                deploy.rollback(backup)
                deploy.rollback(backup)
            self.assertFalse(unit.exists())
            self.assertFalse(current.is_symlink())
            self.assertEqual(tunnel.read_bytes(), b"original synthetic tunnel")
            self.assertEqual((state / "subscriptions.db").read_bytes(), b"private synthetic database")
            self.assertTrue(release.exists())
            self.assertEqual(run.call_args_list.count(mock.call(["systemctl", "restart", "cloudflared-vpn-host1"])), 1)

    def test_rollback_refuses_to_replace_changed_tunnel(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tunnel = root / "tunnel"
            tunnel.write_bytes(b"subsequent administrator change")
            (root / "receipt.json").write_text(json.dumps({
                "unit": False, "current": False, "tunnel": True,
                "tunnel_original_sha256": deploy.sha(b"original"),
                "tunnel_installed_sha256": deploy.sha(b"installed"),
            }))
            with mock.patch.object(deploy, "TUNNEL", tunnel), mock.patch.object(deploy, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "rollback_tunnel_drift"):
                    deploy.rollback(root)
            self.assertEqual(tunnel.read_bytes(), b"subsequent administrator change")
            run.assert_not_called()

    def test_full_rollback_detects_all_drift_before_stopping_service(self):
        for changed in ("unit", "current", "tunnel"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                unit, current, tunnel = root / "unit", root / "current", root / "tunnel"
                release = root / "release"
                release.mkdir()
                unit.write_bytes(b"installed unit")
                current.symlink_to(release)
                tunnel.write_bytes(b"installed tunnel")
                (root / "receipt.json").write_text(json.dumps({
                    "unit": True, "current": True, "tunnel": True, "release": str(release),
                    "unit_sha256": deploy.sha(unit.read_bytes()),
                    "tunnel_installed_sha256": deploy.sha(tunnel.read_bytes()),
                    "tunnel_original_sha256": deploy.sha(b"original tunnel"),
                }))
                if changed == "current":
                    current.unlink()
                    current.write_bytes(b"administrator replacement")
                else:
                    (root / changed).write_bytes(b"administrator change")
                def snapshot():
                    return [(path.is_symlink(), str(path.readlink()) if path.is_symlink() else path.read_bytes())
                            for path in (unit, current, tunnel)]
                before = snapshot()
                with mock.patch.multiple(deploy, UNIT=unit, CURRENT=current, TUNNEL=tunnel), mock.patch.object(deploy, "run") as run:
                    with self.assertRaisesRegex(RuntimeError, "rollback_" + changed + "_drift"):
                        deploy.rollback(root)
                self.assertEqual(snapshot(), before)
                run.assert_not_called()

    def test_rollback_retries_tunnel_restart_until_active_is_confirmed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tunnel = root / "tunnel"
            tunnel.write_bytes(b"installed tunnel")
            info = tunnel.stat()
            (root / "tunnel.original").write_bytes(b"original tunnel")
            (root / "receipt.json").write_text(json.dumps({
                "unit": False, "current": False, "tunnel": True,
                "tunnel_installed_sha256": deploy.sha(tunnel.read_bytes()),
                "tunnel_original_sha256": deploy.sha(b"original tunnel"),
                "tunnel_mode": 0o600, "tunnel_uid": info.st_uid, "tunnel_gid": info.st_gid,
            }))
            restart = mock.call(["systemctl", "restart", "cloudflared-vpn-host1"])
            active = mock.call(["systemctl", "is-active", "--quiet", "cloudflared-vpn-host1"])
            with mock.patch.object(deploy, "TUNNEL", tunnel), mock.patch.object(deploy, "run") as run:
                run.side_effect = RuntimeError("restart_failed")
                with self.assertRaises(RuntimeError):
                    deploy.rollback(root)
                self.assertEqual(tunnel.read_bytes(), b"original tunnel")
                self.assertFalse(json.loads((root / "receipt.json").read_text()).get("tunnel_rollback_complete"))
                run.side_effect = None
                deploy.rollback(root)
                deploy.rollback(root)
            self.assertEqual(run.call_args_list.count(restart), 2)
            self.assertEqual(run.call_args_list.count(active), 1)
            self.assertTrue(json.loads((root / "receipt.json").read_text())["tunnel_rollback_complete"])


if __name__ == "__main__":
    unittest.main()
