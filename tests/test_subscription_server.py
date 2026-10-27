"""Behavior tests for the dependency-free subscription control plane."""

from __future__ import annotations

import http.client
import json
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from urllib.parse import quote, urlsplit
from unittest.mock import patch

from server.subscription_server import SubscriptionStore, create_server


class SubscriptionServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "subscriptions.db")
        self.admin = "admin-test-token-123456789"
        self.server = create_server(self.db, self.admin, port=0, base_url="http://127.0.0.1:8787")
        self.server.store.add_server({"id": "server-a", "name": "Server A", "host": "edge-a.example", "gateway": {"kind": "xray-vless-tls", "port": 443, "server_name": "edge-a.example", "certificate_file": "/etc/xray/cert.pem", "key_file": "/etc/xray/key.pem"}})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.store.close()
        self.temp.cleanup()

    def request(self, method, path, body=None, auth=True):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + self.admin
        encoded = None if body is None else json.dumps(body).encode()
        conn.request(method, path, encoded, headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response, raw

    def add_ip(self, ip_id="ip-a", address="8.8.8.8"):
        response, raw = self.request("POST", "/api/ips", {
            "id": ip_id, "server_id": "server-a",
            "address": address,
            "port": 443,
            "protocol": "socks5",
            "name": ip_id,
            "config": {"username": "fixture-user", "password": "fixture-password"},
        })
        self.assertEqual(response.status, 200, raw)

    def create_user(self, user_id="user-a", ip_id="ip-a"):
        response, raw = self.request("POST", "/api/users", {"id": user_id, "name": user_id, "ip_id": ip_id})
        self.assertEqual(response.status, 201, raw)
        return json.loads(raw)

    def test_admin_auth_and_health(self):
        self.assertEqual(stat.S_IMODE(Path(self.db).stat().st_mode), 0o600)
        response, raw = self.request("GET", "/healthz", auth=False)
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(raw), {"status": "ok"})
        response, raw = self.request("GET", "/api/ips", auth=False)
        self.assertEqual(response.status, 401)
        self.assertNotIn(self.admin.encode(), raw)

        response, raw = self.request("GET", "/admin", auth=False)
        self.assertEqual(response.status, 200)
        self.assertIn("<title>Claude Switcher · 管理工作台</title>".encode(), raw)
        admin_html = raw.decode()
        self.assertIn("const esc =", admin_html)
        self.assertNotIn("${x.name}", admin_html)

    def test_world_readable_database_is_rejected(self):
        unsafe = Path(self.temp.name) / "unsafe.db"
        unsafe.touch(mode=0o644)
        with self.assertRaises(ValueError):
            SubscriptionStore(unsafe)

    def test_binding_is_one_to_one_and_lists_do_not_expose_tokens(self):
        self.add_ip()
        response, raw = self.request("POST", "/api/ips", {
            "id": "duplicate-address", "server_id": "server-a", "address": "8.8.8.8", "port": 443,
            "protocol": "socks5", "config": {},
        })
        self.assertEqual(response.status, 409, raw)
        self.add_ip("ip-b", "1.1.1.1")
        created = self.create_user()
        token = created["token"]
        response, raw = self.request("POST", "/api/users", {"id": "user-b", "ip_id": "ip-a"})
        self.assertEqual(response.status, 409)
        response, raw = self.request("GET", "/api/users")
        self.assertEqual(response.status, 200)
        self.assertNotIn(token.encode(), raw)
        self.assertEqual(json.loads(raw)["items"][0]["ip_id"], "ip-a")

    def test_subscription_returns_only_bound_ip_and_supports_formats(self):
        self.add_ip()
        self.add_ip("ip-b", "1.1.1.1")
        created = self.create_user()
        token = created["token"]
        response, raw = self.request("GET", "/s/" + token)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("X-Claude-Switcher-Egress-IP"), "8.8.8.8")
        self.assertIn(b"edge-a.example", raw)
        # Public resolver IPs may match fixture residential addresses; nodes must not.
        node_section = raw.split(b"\nproxy-groups:", 1)[0]
        self.assertNotIn(b"8.8.8.8", node_section)
        self.assertNotIn(b"1.1.1.1", node_section)
        self.assertNotIn(token.encode(), raw)
        response, raw = self.request("GET", "/s/" + token + "?format=uri")
        self.assertEqual(response.status, 200)
        self.assertTrue(raw.startswith(b"vless://"))
        response, raw = self.request("GET", "/s/" + token + "?format=base64")
        self.assertEqual(response.status, 200)
        self.assertNotIn(b"8.8.8.8", raw)
        response, raw = self.request("PUT", "/api/users/user-a/binding", {"ip_id": "ip-b"})
        self.assertEqual(response.status, 200, raw)
        response, raw = self.request("GET", "/s/" + token)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("X-Claude-Switcher-Egress-IP"), "1.1.1.1")

    def test_rotate_revokes_old_token_and_disable_revokes_new_token(self):
        self.add_ip()
        created = self.create_user()
        old_token = created["token"]
        response, raw = self.request("POST", "/api/users/user-a/rotate-token", {})
        self.assertEqual(response.status, 200)
        new_token = json.loads(raw)["token"]
        self.assertNotEqual(old_token, new_token)
        response, _ = self.request("GET", "/s/" + old_token)
        self.assertEqual(response.status, 404)
        response, _ = self.request("GET", "/s/" + new_token)
        self.assertEqual(response.status, 200)
        response, _ = self.request("POST", "/api/users/user-a/disable", {})
        self.assertEqual(response.status, 200)
        response, _ = self.request("GET", "/s/" + new_token)
        self.assertEqual(response.status, 404)
        response, raw = self.request("POST", "/api/users", {"id": "user-b", "ip_id": "ip-a"})
        self.assertEqual(response.status, 201, raw)

    def import_ips(self, text, protocol="socks5", **options):
        return self.request("POST", "/api/ips/import", {"text": text, "protocol": protocol, "server_id": "server-a", **options})

    def test_import_defaults_to_preview_and_confirm_is_idempotent(self):
        text = "\n8.8.8.8:1337:sample-user:sample-password\r\n\n1.1.1.1:1080:other-user:other-password\n"
        response, raw = self.import_ips(text)
        self.assertEqual(response.status, 200, raw)
        preview = json.loads(raw)
        self.assertTrue(preview["valid"])
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["total"], 2)
        self.assertEqual(preview["imported"], 0)
        self.assertEqual([row["line"] for row in preview["rows"]], [2, 4])
        self.assertEqual(self.server.store.list_ips(), [])
        response, raw = self.import_ips(text, dry_run=False)
        self.assertEqual(response.status, 200, raw)
        result = json.loads(raw)
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["imported"], 2)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual([row["id"] for row in self.server.store.list_ips()], ["proxy-1-1-1-1", "proxy-8-8-8-8"])
        self.assertTrue(all(row["status"] == "available" for row in self.server.store.list_ips()))
        response, raw = self.import_ips(text, dry_run=False)
        self.assertEqual(response.status, 200, raw)
        self.assertEqual(json.loads(raw)["imported"], 0)
        self.assertEqual(json.loads(raw)["skipped"], 2)

    def test_import_invalid_and_empty_batches_are_atomic_and_redacted(self):
        cases = [
            ("secret-user:secret-password", "invalid_format"),
            ("127.0.0.1:1080:secret-user:secret-password", "invalid_address"),
            ("10.0.0.1:1080:secret-user:secret-password", "invalid_address"),
            ("224.0.0.1:1080:secret-user:secret-password", "invalid_address"),
            ("2001:4860:4860::8888:1080:secret-user:secret-password", "invalid_address"),
            ("1.1.1.1:0:secret-user:secret-password", "invalid_port"),
            ("1.1.1.1:65536:secret-user:secret-password", "invalid_port"),
            ("1.1.1.1:true:secret-user:secret-password", "invalid_port"),
            ("1.1.1.1:1080::secret-password", "invalid_credentials"),
            ("1.1.1.1:1080:secret-user:", "invalid_credentials"),
            ("1.1.1.1:1080:secret-user:secret-password\t", "control_character"),
            ("1.1.1.1:1080:secret-user:secret-password\x7f", "control_character"),
        ]
        for invalid, code in cases:
            with self.subTest(code=code, invalid=invalid.split(":")[0]):
                response, raw = self.import_ips("8.8.8.8:1080:ok-user:ok-password\n" + invalid, dry_run=False)
                self.assertEqual(response.status, 200, raw)
                result = json.loads(raw)
                self.assertFalse(result["valid"])
                self.assertEqual(result["errors"], [{"line": 2, "code": code}])
                self.assertEqual(result["imported"], 0)
                for secret in [b"secret-user", b"secret-password", b"ok-password", b"ok-user"]:
                    self.assertNotIn(secret, raw)
                self.assertEqual(self.server.store.list_ips(), [])
        response, raw = self.import_ips("\n \n", dry_run=False)
        self.assertEqual(response.status, 200, raw)
        result = json.loads(raw)
        self.assertEqual(result["errors"], [{"line": 0, "code": "empty_batch"}])
        self.assertEqual(result["total"], 0)
        self.assertFalse(result["valid"])

    def test_import_skips_existing_and_repeated_addresses_without_overwriting(self):
        self.add_ip()
        response, raw = self.import_ips(
            "8.8.8.8:1080:new-user:new-password\n"
            "1.1.1.1:1080:first-user:first-password\n"
            "1.1.1.1:9999:replacement-user:replacement-password", dry_run=False,
        )
        self.assertEqual(response.status, 200, raw)
        result = json.loads(raw)
        self.assertEqual([row["action"] for row in result["rows"]], ["skip", "add", "skip"])
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["skipped"], 2)
        created = self.create_user()
        response, raw = self.request("GET", "/s/" + created["token"])
        self.assertEqual(response.status, 200, raw)
        self.assertIn(b'type: "vless"', raw)
        self.assertNotIn(b"new-password", raw)
        created = self.create_user("user-b", "proxy-1-1-1-1")
        response, raw = self.request("GET", "/s/" + created["token"])
        self.assertEqual(response.status, 200, raw)
        self.assertNotIn(b"first-user", raw)
        self.assertNotIn(b"first-password", raw)
        self.assertNotIn(b"replacement", raw)
        response, raw = self.request("GET", "/api/ips")
        self.assertEqual(response.status, 200, raw)
        for secret in [b"first-user", b"first-password", b"new-password", b"replacement"]:
            self.assertNotIn(secret, raw)

    def test_import_auth_shape_limits_and_maximum_body(self):
        response, raw = self.request("POST", "/api/ips/import", {"text": "secret"}, auth=False)
        self.assertEqual(response.status, 401, raw)
        self.assertNotIn(b"secret", raw)
        for body, code in [
            ({"text": None, "protocol": "http"}, "invalid_text"),
            ({"text": 1, "protocol": "http"}, "invalid_text"),
            ({"text": "", "protocol": "vless"}, "invalid_protocol"),
            ({"text": "", "protocol": []}, "invalid_protocol"),
            ({"text": "", "protocol": "http", "dry_run": "false"}, "invalid_dry_run"),
            ({"text": "", "protocol": "http", "dry_run": 0}, "invalid_dry_run"),
            ({"text": "", "protocol": "http", "dry_run": None}, "invalid_dry_run"),
            ({"text": "", "protocol": "http", "create_subscriptions": "false"}, "invalid_create_subscriptions"),
            ({"text": "", "protocol": "http", "create_subscriptions": 1}, "invalid_create_subscriptions"),
            ({"text": "", "protocol": "http", "create_subscriptions": None}, "invalid_create_subscriptions"),
        ]:
            response, raw = self.request("POST", "/api/ips/import", body)
            self.assertEqual(response.status, 400, raw)
            self.assertEqual(json.loads(raw), {"error": code})
        response, raw = self.import_ips("8.8.8.8:1080:user:pass\n" * 201, dry_run=False)
        self.assertEqual(response.status, 200, raw)
        self.assertEqual(json.loads(raw)["errors"], [{"line": 201, "code": "too_many_rows"}])
        response, raw = self.import_ips("8.8.8.8:1080:user:" + "p" * (64 * 1024), dry_run=False)
        self.assertEqual(response.status, 400, raw)
        self.assertEqual(json.loads(raw), {"error": "request body too large"})
        self.assertEqual(self.server.store.list_ips(), [])

    def test_import_generates_independent_bound_subscriptions_once(self):
        text = ("8.8.8.8:1080:first-user:first:password\n"
                "1.1.1.1:1337:second-user:second-password\n"
                "8.8.8.8:9999:replacement-user:replacement-password")
        response, raw = self.import_ips(text, dry_run=False, create_subscriptions=True)
        self.assertEqual(response.status, 200)
        result = json.loads(raw)
        self.assertEqual((result["imported"], result["skipped"]), (2, 1))
        subscriptions = result["subscriptions"]
        self.assertEqual(len(subscriptions), 2)
        self.assertEqual(len({item["user_id"] for item in subscriptions}), 2)
        self.assertEqual(len({item["subscription_url"] for item in subscriptions}), 2)
        for secret in [b"first-user", b"first:password", b"second-user", b"second-password", b"replacement"]:
            self.assertNotIn(secret, raw)
        users = self.server.store.list_users()
        self.assertEqual(len(users), 2)
        self.assertEqual({row["ip_id"] for row in users}, {"proxy-8-8-8-8", "proxy-1-1-1-1"})
        response, management = self.request("GET", "/api/users")
        self.assertEqual(response.status, 200)
        for subscription in subscriptions:
            token = urlsplit(subscription["subscription_url"]).path.split("/")[2]
            self.assertNotIn(token.encode(), management)
            self.assertNotIn(subscription["subscription_url"].encode(), management)
            stored_hash = self.server.store.db.execute(
                "SELECT token_hash FROM users WHERE id = ?", (subscription["user_id"],),
            ).fetchone()[0]
            self.assertTrue(stored_hash != token)
        for subscription, username, password, other_ip in [
            (subscriptions[0], "first-user", "first:password", "1.1.1.1"),
            (subscriptions[1], "second-user", "second-password", "8.8.8.8"),
        ]:
            path = urlsplit(subscription["subscription_url"]).path
            self.assertTrue(subscription["subscription_url"].startswith("http://127.0.0.1:8787/sub/"))
            self.assertTrue(subscription["user_id"].startswith("sub-"))
            self.assertEqual(self.server.store.db.execute("SELECT subscription_token FROM users WHERE id=?", (subscription["user_id"],)).fetchone()[0], path.split("/")[2])
            response, yaml = self.request("GET", path, auth=False)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("X-Claude-Switcher-Egress-IP"), subscription["address"])
            self.assertIn(b"edge-a.example", yaml)
            node_section = yaml.split(b"\nproxy-groups:", 1)[0]
            self.assertNotIn(subscription["address"].encode(), node_section)
            self.assertNotIn(other_ip.encode(), node_section)
            self.assertNotIn(username.encode(), yaml)
            self.assertNotIn(password.encode(), yaml)
            response, uri = self.request("GET", path + "?format=uri", auth=False)
            self.assertEqual(response.status, 200)
            self.assertTrue(uri.startswith(b"vless://"))
            self.assertIn(b"@edge-a.example:443?", uri)
            self.assertNotIn(password.encode(), uri)
        response, raw = self.import_ips(text, dry_run=False, create_subscriptions=True)
        self.assertEqual(response.status, 200)
        repeated = json.loads(raw)
        self.assertEqual((repeated["imported"], repeated["skipped"]), (0, 3))
        self.assertEqual(repeated["subscriptions"], [])
        self.assertEqual(len(self.server.store.list_users()), 2)
        for subscription in subscriptions:
            response, _ = self.request("GET", urlsplit(subscription["subscription_url"]).path, auth=False)
            self.assertEqual(response.status, 200)

    def test_import_subscription_preview_and_legacy_mode_create_no_users(self):
        text = "8.8.8.8:1080:sample-user:sample-password"
        with patch("server.subscription_server.new_token", side_effect=AssertionError("preview generated token")):
            response, raw = self.import_ips(text, create_subscriptions=True)
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(raw)["subscriptions"], [])
        self.assertEqual(self.server.store.list_ips(), [])
        self.assertEqual(self.server.store.list_users(), [])
        response, raw = self.import_ips(text, dry_run=False)
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(raw)["subscriptions"], [])
        self.assertEqual(len(self.server.store.list_ips()), 1)
        self.assertEqual(self.server.store.list_users(), [])
        response, raw = self.import_ips(text, dry_run=False, create_subscriptions=True)
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(raw)["subscriptions"], [])
        self.assertEqual(self.server.store.list_users(), [])
        self.assertEqual(self.server.store.db.execute("SELECT COUNT(*) FROM bindings").fetchone()[0], 0)

    def test_invalid_subscription_import_writes_none_of_the_three_tables(self):
        response, raw = self.import_ips(
            "8.8.8.8:1080:sample-user:sample-password\n127.0.0.1:1080:other-user:other-password",
            dry_run=False, create_subscriptions=True,
        )
        self.assertEqual(response.status, 200)
        result = json.loads(raw)
        self.assertFalse(result["valid"])
        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["subscriptions"], [])
        self.assertEqual(self.server.store.list_ips(), [])
        self.assertEqual(self.server.store.list_users(), [])
        self.assertEqual(self.server.store.db.execute("SELECT COUNT(*) FROM bindings").fetchone()[0], 0)

    def test_subscription_user_insert_failure_rolls_back_the_whole_import(self):
        with self.server.store._lock, self.server.store.db:
            self.server.store.db.execute("""
                CREATE TRIGGER refuse_second_imported_user BEFORE INSERT ON users
                WHEN (SELECT COUNT(*) FROM users) > 0
                BEGIN SELECT RAISE(ABORT, 'synthetic private trigger detail'); END
            """)
        response, raw = self.import_ips(
            "8.8.8.8:1080:sample-user:sample-password\n1.1.1.1:1080:other-user:other-password",
            dry_run=False, create_subscriptions=True,
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(raw), {"error": "import_conflict"})
        self.assertEqual(self.server.store.list_ips(), [])
        self.assertEqual(self.server.store.list_users(), [])
        self.assertEqual(self.server.store.db.execute("SELECT COUNT(*) FROM bindings").fetchone()[0], 0)

    def test_import_auto_subscription_http_preserves_auth(self):
        response, raw = self.import_ips(
            "8.8.8.8:1337:http-user:http:password", "http", dry_run=False, create_subscriptions=True,
        )
        self.assertEqual(response.status, 200)
        path = urlsplit(json.loads(raw)["subscriptions"][0]["subscription_url"]).path
        response, yaml = self.request("GET", path, auth=False)
        self.assertEqual(response.status, 200)
        self.assertIn(b'type: "vless"', yaml)
        self.assertNotIn(b"http-user", yaml)
        self.assertNotIn(b"http:password", yaml)
        outbound = self.server.store.export_xray_config("server-a")["outbounds"][1]
        self.assertEqual(outbound["protocol"], "http")
        self.assertEqual(outbound["settings"]["pass"], "http:password")
        response, uri = self.request("GET", path + "?format=uri", auth=False)
        self.assertEqual(response.status, 200)
        self.assertTrue(uri.startswith(b"vless://"))
        self.assertIn(b"@edge-a.example:443?", uri)

    def test_import_credentials_survive_clash_uri_and_base64_rendering(self):
        import base64

        username = "user/@?#%雪"
        password = "secret:colon/@?#%雪"
        for number, protocol in enumerate(["socks5", "http"]):
            address = ["8.8.8.8", "1.1.1.1"][number]
            response, raw = self.import_ips(f"{address}:1337:{username}:{password}", protocol, dry_run=False)
            self.assertEqual(response.status, 200, raw)
            self.assertEqual(json.loads(raw)["imported"], 1)
            self.assertNotIn(username.encode(), raw)
            self.assertNotIn(password.encode(), raw)
            created = self.create_user(f"user-{number}", "proxy-" + address.replace(".", "-"))
            response, raw = self.request("GET", "/s/" + created["token"])
            self.assertEqual(response.status, 200, raw)
            self.assertNotIn(username.encode(), raw)
            self.assertNotIn(password.encode(), raw)
            outbound = self.server.store.export_xray_config("server-a")["outbounds"][number + 1]
            self.assertEqual(outbound["settings"]["user"], username)
            self.assertEqual(outbound["settings"]["pass"], password)
            self.assertIn(b"udp: false", raw)
            identity = self.server.store.db.execute("SELECT ingress_uuid FROM users WHERE id=?", (f"user-{number}",)).fetchone()[0]
            expected = f"vless://{identity}@edge-a.example:443?encryption=none&security=tls&sni=edge-a.example&type=tcp&allowInsecure=0&udp=false#user-{number}-gateway"
            response, raw = self.request("GET", "/s/" + created["token"] + "?format=uri")
            self.assertEqual(response.status, 200, raw)
            self.assertEqual(raw.decode().strip(), expected)
            response, raw = self.request("GET", "/s/" + created["token"] + "?format=base64")
            self.assertEqual(response.status, 200, raw)
            self.assertEqual(base64.b64decode(raw).decode(), expected)


if __name__ == "__main__":
    unittest.main()
