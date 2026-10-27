"""HTTP status truthfulness and the public administrator transport boundary."""

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest

from server.subscription_server import create_server


REALITY = {"kind": "xray-vless-reality", "port": 443, "server_name": "edge.example",
           "public_key": base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("="),
           "short_id": "12ab", "inbound_tag": "reality", "flow": "xtls-rprx-vision"}


class IntegrationStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.receipt = Path(self.temp.name) / "status.json"
        self.admin = "test-admin-token-integration"
        self.http = create_server(str(Path(self.temp.name) / "db"), self.admin, port=0,
                                  base_url="https://console.example", integration_status_file=str(self.receipt))
        self.store = self.http.store
        self.store.add_server({"id": "one", "name": "One", "host": "edge.example", "gateway": REALITY})
        self.store.add_ip({"id": "exit", "server_id": "one", "address": "8.8.8.8", "port": 1080,
                           "protocol": "socks5", "config": {"username": "private-login", "password": "private-password"}})
        self.store.create_user({"id": "user", "ip_id": "exit"}, "https://console.example")
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.store.close()
        self.temp.cleanup()

    def request(self, method="GET", path="/api/servers/one/integration", *, auth=True, headers=None, body=None):
        connection = http.client.HTTPConnection(*self.http.server_address, timeout=3)
        request_headers = {"Authorization": "Bearer " + self.admin} if auth else {}
        request_headers.update(headers or {})
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def status(self):
        status, _, raw = self.request()
        self.assertEqual(status, 200, raw)
        return json.loads(raw)

    def revision(self):
        server = self.store.list_servers()[0]
        merge = self.store.export_xray_config("one", merge=True)
        document = {"server": {key: server[key] for key in ["id", "host", "status", "gateway"]},
                    "merge": {key: merge[key] for key in ["inbound_tag", "clients", "outbounds", "routing_rules"]}}
        return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()

    def write_receipt(self, **changes):
        now = datetime.now(timezone.utc)
        value = {"server_id": "one", "state": "applied", "desired_revision": self.revision(),
                 "applied_revision": self.revision(), "checked_at": now.isoformat(),
                 "applied_at": (now - timedelta(seconds=1)).isoformat(), "managed_count": 1, "code": "ok"}
        value.update(changes)
        self.receipt.write_text(json.dumps(value))

    def test_authenticated_missing_receipt_and_unknown_server(self):
        self.assertEqual(self.request(auth=False)[0], 401)
        self.assertEqual(self.request(path="/api/servers/absent/integration")[0], 404)
        data = self.status()
        self.assertEqual(data["state"], "unconfigured")
        self.assertEqual(data["desired_revision"], self.revision())
        self.assertIsNone(data["applied_revision"])
        self.write_receipt(server_id="different")
        self.assertEqual(self.status()["state"], "unconfigured")

    def test_applied_is_fresh_exact_revision_and_sanitized(self):
        self.write_receipt(password="private-receipt", raw_error="private-URL", clients=["private-client"])
        data = self.status()
        self.assertEqual(data["state"], "applied")
        self.assertEqual(data["managed_count"], 1)
        self.assertEqual(data["applied_revision"], data["desired_revision"])
        self.assertEqual(set(data), {"server_id", "state", "desired_revision", "applied_revision", "checked_at",
                                     "applied_at", "managed_count", "code"})
        raw = json.dumps(data)
        for secret in ["private-receipt", "private-URL", "private-client", "private-password", "private-login", "8.8.8.8"]:
            self.assertNotIn(secret, raw)

    def test_receipt_revision_mismatch_never_applied(self):
        for changes in [{"desired_revision": "0" * 64}, {"applied_revision": "0" * 64}]:
            with self.subTest(changes=changes):
                self.write_receipt(**changes)
                self.assertEqual(self.status()["state"], "pending")
        self.write_receipt()
        self.store.bind("user", None)
        data = self.status()
        self.assertEqual(data["state"], "pending")
        self.assertNotEqual(data["applied_revision"], data["desired_revision"])
        self.write_receipt()
        self.store.add_server({"id": "one", "name": "One", "host": "edge.example", "status": "disabled"})
        self.assertEqual(self.status()["state"], "pending")

    def test_stale_and_blocked_and_applying_are_distinct(self):
        now = datetime.now(timezone.utc)
        self.write_receipt(checked_at=(now - timedelta(seconds=61)).isoformat(),
                           applied_at=(now - timedelta(seconds=62)).isoformat())
        self.assertEqual(self.status()["state"], "stale")
        for state in ["blocked", "applying"]:
            self.write_receipt(state=state, applied_revision=None, applied_at=None, code="panel_unavailable")
            self.assertEqual(self.status()["state"], state)
        self.write_receipt(state="blocked", code="raw-private-error")
        self.assertEqual(self.status()["code"], "status_unavailable")

    def test_invalid_receipts_fail_closed_without_raw_content(self):
        for changes in [{"state": "success"}, {"desired_revision": "not-a-hash"}, {"applied_revision": []},
                        {"checked_at": "yesterday"}, {"checked_at": "2026-10-25T00:00:00"},
                        {"checked_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()},
                        {"applied_at": None}, {"code": "private-unknown-code"}, {"code": "apply_failed"}, {"managed_count": True}, {"managed_count": -1}]:
            with self.subTest(changes=changes):
                self.write_receipt(**changes)
                self.assertEqual(self.status()["state"], "blocked")
        self.receipt.write_text("private malformed content")
        self.assertEqual(self.status()["code"], "status_unavailable")
        self.receipt.write_text('"private string"')
        self.assertEqual(self.status()["state"], "blocked")
        self.receipt.unlink()
        self.receipt.symlink_to(Path(self.temp.name) / "db")
        self.assertEqual(self.status()["state"], "blocked")

    def test_public_https_and_fixed_redirect_local_ssh_remains_available(self):
        headers = {"Host": "console.example"}
        status, response_headers, _ = self.request(path="/admin?next=https://evil.example", auth=False, headers=headers)
        self.assertEqual(status, 308)
        self.assertEqual(response_headers["Location"], "https://console.example/admin")
        self.assertEqual(self.request(headers=headers)[0], 403)
        status, _, raw = self.request(headers={**headers, "X-Forwarded-Proto": "https"})
        self.assertEqual(status, 200, raw)
        self.assertEqual(self.request(path="/admin", auth=False)[0], 200)
        for host in ["evil.example", "console.example.evil", "console.example:444", "user@console.example", "console.example:"]:
            with self.subTest(host=host):
                self.assertEqual(self.request(path="/admin", headers={"Host": host, "X-Forwarded-Proto": "https"})[0], 403)
        for proto in ["http", "https,http", "HTTPS"]:
            self.assertEqual(self.request(headers={**headers, "X-Forwarded-Proto": proto})[0], 403)

    def test_rejects_unsafe_public_request_before_waiting_for_body(self):
        for method, path in [("POST", "/api/auth/login"), ("POST", "/api/servers"),
                             ("PUT", "/api/users/user/binding"), ("POST", "/api")]:
            with self.subTest(method=method, path=path):
                connection = socket.create_connection(self.http.server_address, timeout=2)
                connection.settimeout(2)
                connection.sendall((method + " " + path + " HTTP/1.1\r\nHost: console.example\r\nContent-Length: 1000000\r\n\r\n").encode())
                response = http.client.HTTPResponse(connection)
                response.begin()
                self.assertEqual(response.status, 403)
                self.assertEqual(json.loads(response.read())["error"], "admin_https_required")
                connection.close()

    def test_proxy_headers_and_loopback_host_not_trusted_from_remote_peer(self):
        original_handler = self.http.RequestHandlerClass
        class RemotePeerHandler(original_handler):
            def setup(self):
                super().setup()
                self.client_address = ("198.51.100.2", 20000)
        self.http.RequestHandlerClass = RemotePeerHandler
        for host in ["console.example", "localhost", "127.0.0.1"]:
            self.assertEqual(self.request(headers={"Host": host, "X-Forwarded-Proto": "https"})[0], 403)

    def test_security_headers_for_html_api_and_errors(self):
        for path, auth in [("/admin", False), ("/api/servers/one/integration", True), ("/api/users", False)]:
            _, headers, _ = self.request(path=path, auth=auth)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_status_path_must_be_fixed_absolute_configuration(self):
        with self.assertRaisesRegex(ValueError, "absolute path"):
            create_server(str(Path(self.temp.name) / "another-db"), self.admin, port=0, integration_status_file="relative.json")


if __name__ == "__main__":
    unittest.main()
