"""Server ownership, migration, and stable subscription behavior."""

import base64
import http.client
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from server.subscription_server import (
    DomainError, SubscriptionStore, create_server, render_clash, render_uri, token_hash,
)


LEGACY_SCHEMA = """
CREATE TABLE users (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
    token_hint TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','disabled')),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE ips (
    id TEXT PRIMARY KEY, address TEXT NOT NULL, port INTEGER NOT NULL,
    protocol TEXT NOT NULL, name TEXT NOT NULL, config_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('available','disabled')),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE bindings (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    ip_id TEXT NOT NULL UNIQUE REFERENCES ips(id) ON DELETE RESTRICT,
    bound_at TEXT NOT NULL
);
"""


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "legacy.db"
        self.token = "cs_synthetic-legacy-stable-token"
        self.user = ("legacy-user", "Historical", token_hash(self.token), "cs_test…0000", "active", "old", "old")
        self.config = '{ "username": "fixture-name", "password": "fixture:secret", "udp": false }'
        self.ip = ("legacy-ip", "8.8.8.8", 1080, "socks5", "Historical IP", self.config, "available", "old", "old")
        db = sqlite3.connect(self.path)
        db.executescript(LEGACY_SCHEMA)
        db.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)", self.user)
        db.execute("INSERT INTO ips VALUES(?,?,?,?,?,?,?,?,?)", self.ip)
        db.execute("INSERT INTO bindings VALUES(?,?,?)", ("legacy-user", "legacy-ip", "bound-old"))
        db.commit()
        db.close()
        os.chmod(self.path, 0o600)

    def tearDown(self):
        self.temp.cleanup()

    def test_real_legacy_migration_preserves_records_and_exact_render_bytes_on_reopen(self):
        before = sqlite3.connect(self.path)
        before.row_factory = sqlite3.Row
        ip = dict(before.execute("SELECT * FROM ips").fetchone())
        user = dict(before.execute("SELECT * FROM users").fetchone())
        binding = tuple(before.execute("SELECT * FROM bindings").fetchone())
        before.close()
        expected = {
            "clash": render_clash(user, ip).encode(),
            "uri": (render_uri(user, ip) + "\n").encode(),
            "base64": base64.b64encode(render_uri(user, ip).encode()) + b"\n",
        }
        ingress_uuid = None
        for _ in range(2):
            store = SubscriptionStore(self.path)
            try:
                self.assertEqual(store.list_servers(), [])
                self.assertIsNone(store.list_ips()[0]["server_id"])
                self.assertIsNone(store.get_user("legacy-user")["server_name"])
                self.assertEqual(tuple(store.db.execute("SELECT * FROM users").fetchone())[:7], self.user)
                actual_uuid = store.db.execute("SELECT ingress_uuid FROM users").fetchone()[0]
                if ingress_uuid is None:
                    ingress_uuid = actual_uuid
                self.assertEqual(actual_uuid, ingress_uuid)
                self.assertEqual(tuple(store.db.execute("SELECT * FROM bindings").fetchone()), binding)
                self.assertEqual(tuple(store.db.execute("SELECT * FROM ips").fetchone())[:9], self.ip)
                self.assertEqual(store.db.execute("SELECT config_json FROM ips").fetchone()[0], self.config)
                self.assertEqual(store.db.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertTrue(any(row["table"] == "servers" and row["from"] == "server_id"
                                    for row in store.db.execute("PRAGMA foreign_key_list(ips)")))
                for fmt, raw in expected.items():
                    self.assertEqual(store.subscription(self.token, fmt)[0], raw)
                with self.assertRaises(DomainError):
                    store.create_user({"id": "new", "ip_id": "legacy-ip"}, "https://fixture.invalid")
                self.assertEqual(len(store.list_users()), 1)
            finally:
                store.close()

    def test_legacy_clash_render_is_fail_closed_for_business_and_dns(self):
        rendered = render_clash(self.user, {
            "address": self.ip[1], "port": self.ip[2], "protocol": self.ip[3],
            "name": self.ip[4], "config_json": self.config,
        })
        self.assertIn("MATCH,CLAUDE-SWITCHER", rendered)
        self.assertNotIn("MATCH,DIRECT", rendered)
        self.assertIn("NETWORK,udp,REJECT", rendered)
        self.assertIn("nameserver:", rendered)
        self.assertIn("https://1.1.1.1/dns-query#CLAUDE-SWITCHER", rendered)

    def test_duplicate_old_addresses_abort_all_schema_changes(self):
        db = sqlite3.connect(self.path)
        db.execute("INSERT INTO ips VALUES(?,?,?,?,?,?,?,?,?)", ("duplicate", *self.ip[1:]))
        db.commit()
        before_dump = list(db.iterdump())
        db.close()
        before_bytes = self.path.read_bytes()
        with self.assertRaisesRegex(DomainError, "migration conflict"):
            SubscriptionStore(self.path)
        self.assertEqual(self.path.read_bytes(), before_bytes)
        db = sqlite3.connect(self.path)
        self.assertEqual(list(db.iterdump()), before_dump)
        self.assertNotIn("server_id", {row[1] for row in db.execute("PRAGMA table_info(ips)")})
        self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='servers'").fetchone())
        db.close()

    def test_initial_attribution_requires_unbinding_and_ready_gateway_before_new_binding(self):
        store = SubscriptionStore(self.path)
        try:
            before = store.subscription(self.token, "clash")[0]
            store.add_server({"id": "a", "name": "A", "host": "private-alias"})
            with self.assertRaisesRegex(DomainError, "bound legacy"):
                store.assign_ip_server("legacy-ip", "a")
            with self.assertRaisesRegex(DomainError, "bound legacy"):
                store.add_ip({"id": "legacy-ip", "address": "8.8.8.8", "port": 1080,
                              "protocol": "socks5", "config": json.loads(self.config), "server_id": "a"})
            self.assertEqual(store.subscription(self.token, "clash")[0], before)
            store.bind("legacy-user", None)
            self.assertEqual(store.assign_ip_server("legacy-ip", "a")["server_id"], "a")
            with self.assertRaisesRegex(DomainError, "gateway is unavailable"):
                store.bind("legacy-user", "legacy-ip")
            self.assertEqual(store.db.execute("SELECT config_json FROM ips").fetchone()[0], self.config)
        finally:
            store.close()

    def test_unsupported_legacy_protocol_cannot_be_claimed_even_unbound(self):
        store = SubscriptionStore(self.path)
        try:
            store.bind("legacy-user", None)
            with store.db:
                store.db.execute("UPDATE ips SET protocol='vless',config_json=?", ('{"uuid":"fixture"}',))
            store.add_server({"id": "a", "name": "A", "host": "alias"})
            with self.assertRaisesRegex(DomainError, "unsupported gateway upstream"):
                store.assign_ip_server("legacy-ip", "a")
            self.assertIsNone(store.list_ips()[0]["server_id"])
        finally:
            store.close()

    def test_existing_legacy_metadata_update_can_omit_owner_but_not_explicit_null(self):
        store = SubscriptionStore(self.path)
        payload = {"id": "legacy-ip", "address": "8.8.8.8", "port": 1080,
                   "protocol": "socks5", "config": json.loads(self.config)}
        try:
            self.assertIsNone(store.add_ip(payload)["server_id"])
            with self.assertRaises(DomainError):
                store.add_ip({**payload, "server_id": None})
            self.assertEqual(store.subscription(self.token, "uri")[2], "8.8.8.8")
        finally:
            store.close()


class MultiServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = create_server(str(Path(self.temp.name) / "db"), "synthetic-admin-multiserver", port=0,
                                    base_url="https://fixture.invalid")
        self.store = self.server.store
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.add_server("a")
        self.add_server("b")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.temp.cleanup()

    def request(self, method, path, body=None, auth=True):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer synthetic-admin-multiserver"
        conn.request(method, path, None if body is None else json.dumps(body).encode(), headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, raw

    def add_server(self, server_id, **values):
        return self.store.add_server({"id": server_id, "name": "Server " + server_id,
                                      "host": server_id + ".example", "gateway": {"kind": "xray-vless-tls", "port": 443, "server_name": server_id + ".example", "certificate_file": "/etc/xray/cert.pem", "key_file": "/etc/xray/key.pem"}, **values})

    def add_ip(self, ip_id="ip-a", address="8.8.8.8", server_id="a", **values):
        return self.store.add_ip({"id": ip_id, "address": address, "port": 1080, "protocol": "socks5",
                                  "config": {"username": "fixture-user", "password": "fixture-secret"},
                                  "server_id": server_id, **values})

    def import_ips(self, server_id="a", **values):
        return self.store.import_ips({"server_id": server_id, "text": "8.8.8.8:1080:fixture-user:fixture-secret",
                                      "protocol": "socks5", **values}, "https://fixture.invalid")

    def test_server_crud_counts_and_status_omission_preserve_state(self):
        self.add_ip()
        self.add_ip("ip-b", "1.1.1.1", "a", status="disabled")
        self.store.create_user({"id": "person", "ip_id": "ip-a"}, "https://fixture.invalid")
        self.add_ip("ip-c", "9.9.9.9", "a")
        record = self.store.list_servers()[0]
        self.assertEqual((record["ip_count"], record["bound_count"], record["available_count"]), (3, 1, 1))
        status, raw = self.request("POST", "/api/servers", {"id": "a", "name": "Updated", "host": "10.0.0.1", "status": "disabled"})
        self.assertEqual(status, 200, raw)
        status, raw = self.request("POST", "/api/servers", {"id": "a", "name": "Renamed", "host": "ssh-alias"})
        self.assertEqual(status, 200, raw)
        result = json.loads(raw)
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["available_count"], 0)
        self.assertEqual(result["created_at"], record["created_at"])
        self.assertEqual(result["region"], "")
        status, raw = self.request("GET", "/api/servers")
        self.assertEqual(status, 200, raw)
        self.assertEqual(len(json.loads(raw)["items"]), 2)

    def test_each_server_url_has_only_its_bound_node_in_every_format(self):
        self.add_ip()
        self.add_ip("ip-b", "1.1.1.1", "b")
        records = [self.store.create_user({"id": person, "ip_id": ip_id}, "https://fixture.invalid")
                   for person, ip_id in [("alice", "ip-a"), ("bob", "ip-b")]]
        self.assertNotEqual(records[0]["subscription_url"], records[1]["subscription_url"])
        for record, address, other, owner in [(records[0], "8.8.8.8", "1.1.1.1", "a"),
                                               (records[1], "1.1.1.1", "8.8.8.8", "b")]:
            self.assertEqual(record["user"]["server_id"], owner)
            self.assertEqual(record["user"]["server_status"], "active")
            for fmt in ["clash", "uri", "base64"]:
                raw = self.store.subscription(record["token"], fmt)[0]
                if fmt == "base64":
                    raw = base64.b64decode(raw)
                # DoH's public resolver can equal a fixture IP; only nodes represent bindings.
                node_section = raw.split(b"\nproxy-groups:", 1)[0] if fmt == "clash" else raw
                self.assertNotIn(address.encode(), node_section)
                self.assertNotIn(other.encode(), node_section)
                self.assertIn((owner + ".example").encode(), raw)

    def test_bound_owner_and_address_are_protected_and_explicit_rebind_keeps_token(self):
        self.add_ip()
        self.add_ip("ip-b", "1.1.1.1", "b")
        record = self.store.create_user({"id": "alice", "ip_id": "ip-a"}, "https://fixture.invalid")
        original = self.store.db.execute("SELECT config_json FROM ips WHERE id='ip-a'").fetchone()[0]
        for method, path, body in [
            ("PUT", "/api/ips/ip-a/server", {"server_id": "b"}),
            ("POST", "/api/ips", {"id": "ip-a", "address": "9.9.9.9", "server_id": "a"}),
            ("POST", "/api/ips", {"id": "ip-a", "address": "8.8.8.8", "server_id": "b"}),
        ]:
            status, raw = self.request(method, path, body)
            self.assertEqual(status, 400, raw)
        self.assertEqual(self.store.get_user("alice")["server_id"], "a")
        self.assertEqual(self.store.db.execute("SELECT config_json FROM ips WHERE id='ip-a'").fetchone()[0], original)
        status, raw = self.request("PUT", "/api/ips/ip-a/server", {"server_id": "a"})
        self.assertEqual(status, 200, raw)
        user = self.store.bind("alice", "ip-b")
        self.assertEqual(user["server_id"], "b")
        self.assertEqual(self.store.subscription(record["token"], "uri")[2], "1.1.1.1")
        self.assertEqual(self.store.assign_ip_server("ip-a", "b")["server_id"], "b")
        self.assertEqual(self.store.db.execute("SELECT token_hash FROM users WHERE id='alice'").fetchone()[0], token_hash(record["token"]))

    def test_cross_server_import_duplicates_keep_actual_owner_and_credentials(self):
        self.add_ip()
        before = self.store.db.execute("SELECT * FROM ips WHERE id='ip-a'").fetchone()
        result = self.import_ips("b", dry_run=False, create_subscriptions=True,
                                 text="8.8.8.8:9999:replacement:replacement\n1.1.1.1:1080:fixture-two:fixture-two")
        self.assertEqual((result["server_id"], result["server_name"]), ("b", "Server b"))
        self.assertEqual([(row["action"], row["server_id"]) for row in result["rows"]], [("skip", "a"), ("add", "b")])
        self.assertEqual(result["rows"][0]["server_name"], "Server a")
        self.assertEqual(result["subscriptions"][0]["server_id"], "b")
        self.assertEqual(tuple(self.store.db.execute("SELECT * FROM ips WHERE id='ip-a'").fetchone()), tuple(before))
        self.assertNotIn("replacement", json.dumps(result))
        self.assertNotIn("fixture-two", json.dumps(result))
        self.assertEqual(len(self.store.list_users()), 1)
        with self.assertRaisesRegex(DomainError, "already registered"):
            self.add_ip("duplicate", "8.8.8.8", "b")
        with self.assertRaises(sqlite3.IntegrityError), self.store.db:
            self.store.db.execute("UPDATE ips SET address='8.8.8.8' WHERE id='proxy-1-1-1-1'")

    def test_disabling_after_preview_blocks_confirm_and_all_fetch_formats_then_restores_url(self):
        self.assertTrue(self.import_ips(create_subscriptions=True)["valid"])
        self.add_server("a", status="disabled")
        before = list(self.store.db.iterdump())
        with self.assertRaisesRegex(DomainError, "unavailable"):
            self.import_ips(dry_run=False, create_subscriptions=True)
        self.assertEqual(list(self.store.db.iterdump()), before)
        self.add_server("a", status="active")
        result = self.import_ips(dry_run=False, create_subscriptions=True)
        path = urlsplit(result["subscriptions"][0]["subscription_url"]).path
        user_before = tuple(self.store.db.execute("SELECT * FROM users").fetchone())
        binding_before = tuple(self.store.db.execute("SELECT * FROM bindings").fetchone())
        expected = {fmt: self.request("GET", path + "?format=" + fmt, auth=False)[1]
                    for fmt in ["clash", "uri", "base64"]}
        self.add_server("a", status="disabled")
        for fmt in expected:
            status, raw = self.request("GET", path + "?format=" + fmt, auth=False)
            self.assertEqual(status, 404, raw)
        self.assertEqual(tuple(self.store.db.execute("SELECT * FROM users").fetchone()), user_before)
        self.assertEqual(tuple(self.store.db.execute("SELECT * FROM bindings").fetchone()), binding_before)
        with self.assertRaises(DomainError):
            self.store.create_user({"id": "new", "ip_id": "proxy-8-8-8-8"}, "https://fixture.invalid")
        self.add_server("a", status="active")
        for fmt, expected_bytes in expected.items():
            status, raw = self.request("GET", path + "?format=" + fmt, auth=False)
            self.assertEqual(status, 200, raw)
            self.assertEqual(raw, expected_bytes)

    def test_disabled_or_unknown_owner_rejects_new_ips_imports_and_binding(self):
        self.add_ip()
        self.store.create_user({"id": "person"}, "https://fixture.invalid")
        self.add_server("a", status="disabled")
        for owner in [None, "missing", "a", [], "bad/id"]:
            with self.subTest(owner=owner):
                with self.assertRaises(DomainError):
                    self.add_ip("new", "1.1.1.1", owner)
                with self.assertRaises(DomainError):
                    self.import_ips(owner, dry_run=False)
                with self.assertRaises(DomainError):
                    self.store.assign_ip_server("ip-a", owner)
        with self.assertRaises(DomainError):
            self.store.bind("person", "ip-a")
        self.assertIsNone(self.store.get_user("person")["ip_id"])
        self.assertEqual(len(self.store.list_ips()), 1)
        status, raw = self.request("POST", "/api/ips", {"id": "missing-owner", "address": "1.1.1.1"})
        self.assertEqual(status, 400, raw)

    def test_nonnull_orphan_fails_closed_even_in_corrupted_database(self):
        self.add_ip()
        record = self.store.create_user({"id": "person", "ip_id": "ip-a"}, "https://fixture.invalid")
        self.store.db.execute("PRAGMA foreign_keys=OFF")
        with self.store.db:
            self.store.db.execute("DELETE FROM servers WHERE id='a'")
        self.store.db.execute("PRAGMA foreign_keys=ON")
        with self.assertRaisesRegex(DomainError, "unavailable"):
            self.store.subscription(record["token"], "uri")
        self.assertEqual(self.store.list_ips()[0]["server_id"], "a")
        self.assertIsNone(self.store.list_ips()[0]["server_status"])
        self.store.bind("person", None)
        with self.assertRaises(DomainError):
            self.store.bind("person", "ip-a")

    def test_server_validation_auth_and_public_response_redaction(self):
        for method, path, payload in [("GET", "/api/servers", None),
                                      ("POST", "/api/servers", {"id": "private-secret"}),
                                      ("PUT", "/api/ips/private-secret/server", {"server_id": "private-secret"})]:
            status, raw = self.request(method, path, payload, auth=False)
            self.assertEqual(status, 401, raw)
            self.assertNotIn(b"private-secret", raw)
        for host in ["https://fixture.invalid", "user:private-secret@host", "host/path", "host?token=private-secret",
                     "host\nprivate-secret", "host:22", "bad host", "", None, []]:
            status, raw = self.request("POST", "/api/servers", {"id": "x", "name": "X", "host": host})
            self.assertEqual(status, 400, raw)
            self.assertNotIn(b"private-secret", raw)
        for field, value in [("id", "bad/id"), ("id", []), ("name", "x" * 121), ("region", 1), ("status", [])]:
            status, raw = self.request("POST", "/api/servers", {"id": "x", "name": "X", "host": "alias", field: value})
            self.assertEqual(status, 400, raw)
        for host in ["10.0.0.1", "private-alias", "edge.example", "2001:db8::1"]:
            status, raw = self.request("POST", "/api/servers", {"id": "valid", "name": "Valid", "host": host})
            self.assertEqual(status, 200, raw)
        self.add_ip()
        record = self.store.create_user({"id": "person", "ip_id": "ip-a"}, "https://fixture.invalid")
        for path in ["/api/ips", "/api/users", "/api/servers"]:
            status, raw = self.request("GET", path)
            self.assertEqual(status, 200, raw)
            for secret in [b"fixture-user", b"fixture-secret", b"config_json", record["token"].encode(), token_hash(record["token"]).encode()]:
                self.assertNotIn(secret, raw)
        self.assertEqual(self.store.rotate_token("person", "https://fixture.invalid")["user"]["server_id"], "a")

    def test_confirm_failure_rolls_back_ips_users_and_bindings_across_servers(self):
        self.import_ips(dry_run=False, create_subscriptions=True)
        before = list(self.store.db.iterdump())
        with patch("server.subscription_server.new_token", return_value="cs_same-fixture-token"):
            with self.assertRaisesRegex(DomainError, "import_conflict"):
                self.import_ips("b", dry_run=False, create_subscriptions=True,
                                text="1.1.1.1:1080:first:first\n9.9.9.9:1080:second:second")
        self.assertEqual(list(self.store.db.iterdump()), before)

    def test_shared_connection_readers_wait_for_rollback_and_return_materialized_data(self):
        self.add_ip()
        record = self.store.create_user({"id": "person", "ip_id": "ip-a"}, "https://fixture.invalid")
        for read in [self.store.list_servers, self.store.list_ips, self.store.list_users,
                     lambda: self.store.get_user("person"), lambda: self.store.subscription(record["token"], "uri")]:
            started, finished = threading.Event(), threading.Event()
            result = []
            def reader():
                started.set()
                result.append(read())
                finished.set()
            with self.store._lock:
                self.store.db.execute("BEGIN")
                self.store.db.execute("UPDATE servers SET status='disabled' WHERE id='a'")
                thread = threading.Thread(target=reader)
                thread.start()
                self.assertTrue(started.wait(2))
                self.assertFalse(finished.wait(.03))
                self.store.db.rollback()
            thread.join(2)
            self.assertTrue(finished.is_set())
            self.assertIsInstance(result[0], (list, dict, tuple))
            self.assertNotIn("sqlite3.Cursor", str(type(result[0])))
