"""Administrator login behavior with synthetic credentials and isolated databases."""

from __future__ import annotations

import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from server.admin_auth import AdminAuth, LoginError, SESSION_TTL, hash_admin_password, load_admin_auth_file
from server.subscription_server import create_server


class AdminAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "test-only-login-secret"
        cls.encoded = hash_admin_password(cls.password)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.servers = []
        self.server = self.start_server("first")

    def start_server(self, name, configured=True):
        kwargs = {"admin_username": "operator-test", "admin_password_hash": self.encoded} if configured else {}
        server = create_server(str(Path(self.temp.name) / (name + ".db")), "synthetic-legacy-admin-token", port=0, **kwargs)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        return server

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
            server.store.close()
        self.temp.cleanup()

    def request(self, method, path, body=None, token=None, server=None, raw=None):
        conn = http.client.HTTPConnection(*(server or self.server).server_address, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        encoded = raw if raw is not None else None if body is None else json.dumps(body).encode()
        conn.request(method, path, encoded, headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, json.loads(data)

    def login(self):
        status, data = self.request("POST", "/api/auth/login", {"username": "operator-test", "password": self.password})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["username"], "operator-test")
        self.assertEqual(data["expires_in"], SESSION_TTL)
        self.assertNotIn(self.password, json.dumps(data))
        self.assertNotIn(self.encoded, json.dumps(data))
        return data["token"]

    def test_login_management_session_and_logout(self):
        self.assertEqual(self.request("GET", "/api/ips")[0], 401)
        token = self.login()
        self.assertEqual(self.request("GET", "/api/ips", token=token), (200, {"items": []}))
        self.assertEqual(self.request("GET", "/api/auth/session", token=token),
                         (200, {"authenticated": True, "username": "operator-test"}))
        self.assertEqual(self.request("POST", "/api/auth/logout", {}, token=token), (200, {"ok": True}))
        self.assertEqual(self.request("GET", "/api/ips", token=token)[0], 401)
        self.assertEqual(self.request("POST", "/api/auth/logout", {}, token=token)[0], 401)

    def test_legacy_token_and_unconfigured_login_remain_supported(self):
        server = self.start_server("legacy", configured=False)
        self.assertEqual(self.request("GET", "/api/ips", token="synthetic-legacy-admin-token", server=server)[0], 200)
        self.assertEqual(self.request("POST", "/api/auth/login", {}, server=server),
                         (404, {"error": "auth_not_configured"}))
        self.assertEqual(self.request("GET", "/api/auth/session", token="synthetic-legacy-admin-token")[0], 200)
        self.assertEqual(self.request("GET", "/healthz")[0], 200)
        self.assertEqual(self.request("GET", "/s/nonexistent-synthetic-subscription-token")[0], 404)

    def test_session_expiry_and_independent_server(self):
        now = [100.0]
        self.server.admin_auth._clock = lambda: now[0]
        token = self.login()
        other = self.start_server("second")
        self.assertEqual(self.request("GET", "/api/ips", token=token, server=other)[0], 401)
        now[0] += SESSION_TTL
        self.assertEqual(self.request("GET", "/api/auth/session", token=token)[0], 401)

    def test_invalid_login_shapes_are_sanitized(self):
        invalid = [{}, [], {"username": 7, "password": self.password},
                   {"username": "operator-test", "password": None},
                   {"username": "operator-test", "password": ""},
                   {"username": "x" * 129, "password": self.password},
                   {"username": "operator-test", "password": "x" * 4097},
                   {"username": "operator-test", "password": self.password, "extra": True}]
        for body in invalid:
            with self.subTest(body_type=type(body).__name__):
                self.assertEqual(self.request("POST", "/api/auth/login", body),
                                 (400, {"error": "invalid_credentials"}))
        for raw in [b"{invalid", b'"not-an-object"', b"\xff", b"x" * 65537]:
            self.assertEqual(self.request("POST", "/api/auth/login", raw=raw),
                             (400, {"error": "invalid_credentials"}))
        self.assertEqual(self.request("GET", "/api/ips", token="non-ascii-\u00e9")[0], 401)

    def test_wrong_password_username_and_rate_limit(self):
        now = [100.0]
        self.server.admin_auth._clock = lambda: now[0]
        for index in range(5):
            body = {"username": "operator-test" if index % 2 else "another-test-user", "password": "wrong-test-secret"}
            self.assertEqual(self.request("POST", "/api/auth/login", body), (401, {"error": "invalid_credentials"}))
        self.assertEqual(self.request("POST", "/api/auth/login", {"username": "operator-test", "password": self.password}),
                         (429, {"error": "too_many_attempts"}))
        now[0] += 60
        self.login()

    def test_hash_is_salted_and_only_session_hash_is_retained(self):
        encoded = hash_admin_password(self.password)
        self.assertNotEqual(encoded, self.encoded)
        self.assertNotIn(self.password, encoded)
        auth = AdminAuth("operator-test", encoded)
        token = auth.login("operator-test", self.password, "test-client")
        self.assertTrue(auth.authorized(token))
        self.assertNotIn(token, auth._sessions)
        self.assertIn(hashlib.sha256(token.encode()).digest(), auth._sessions)
        with self.assertRaises(LoginError):
            auth.login("operator-test", "wrong-test-secret", "test-client")
        with self.assertRaises(LoginError):
            auth.login("operator-other", self.password, "test-client")

    def test_configuration_validation_and_permissions(self):
        path = Path(self.temp.name) / "auth.json"
        path.touch(mode=0o600)
        config = {"username": "operator-test", "password_hash": self.encoded}
        path.write_text(json.dumps(config))
        self.assertEqual(load_admin_auth_file(path), config)
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "^invalid admin authentication configuration$"):
            load_admin_auth_file(path)
        path.chmod(0o600)
        for value in [[], {"username": "operator-test", "password_hash": self.password},
                      {**config, "password": "test-secret"}, {**config, "username": ""},
                      {**config, "password_hash": self.encoded.replace("600000", "9999999")}]:
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "^invalid admin authentication configuration$"):
                load_admin_auth_file(path)
        link = Path(self.temp.name) / "auth-link.json"
        link.symlink_to(path)
        with self.assertRaises(ValueError):
            load_admin_auth_file(link)
        for kwargs in [{"admin_username": "operator-test"}, {"admin_password_hash": self.encoded}]:
            with self.assertRaises(ValueError):
                create_server(str(Path(self.temp.name) / "invalid.db"), "synthetic-legacy-admin-token", port=0, **kwargs)

    def test_session_and_failure_memory_is_bounded(self):
        auth = AdminAuth("operator-test", self.encoded, clock=lambda: 100.0)
        auth._sessions = {str(index).encode(): 1000.0 for index in range(128)}
        token = auth.login("operator-test", self.password, "test-client")
        self.assertEqual(len(auth._sessions), 128)
        self.assertTrue(auth.authorized(token))
        auth._failures = {str(index): [100.0] for index in range(1024)}
        with self.assertRaisesRegex(LoginError, "too_many_attempts"):
            auth.login("operator-test", self.password, "new-test-client")
        self.assertEqual(len(auth._failures), 1024)


if __name__ == "__main__":
    unittest.main()
