"""Opt-in real Xray gateway proof, using synthetic identities and loopback only.

Run with XRAY_BIN=/absolute/path/to/xray and optional TARGET_ROOT=/source/root.
Only the exported inbound's listen address and residential socket endpoints are
rewritten for isolation. TLS, identities, credentials and routing stay exported.
The CA is private to this test and explicitly trusted, never allowInsecure.
"""

from __future__ import annotations

import base64
import copy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import os
from pathlib import Path
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from urllib.parse import parse_qs, urlsplit
import uuid


LOOPBACK = "127.0.0.1"


def free_port():
    with socket.socket() as sock:
        sock.bind((LOOPBACK, 0))
        return sock.getsockname()[1]


def receive_exact(sock, length):
    result = b""
    while len(result) < length:
        chunk = sock.recv(length - len(result))
        if not chunk:
            raise EOFError("closed fixture connection")
        result += chunk
    return result


def read_headers(sock):
    result = b""
    while b"\r\n\r\n" not in result:
        result += receive_exact(sock, 1)
        if len(result) > 16384:
            raise ValueError("oversized fixture request")
    return result.decode("ascii")


class Origin(BaseHTTPRequestHandler):
    def do_GET(self):
        marker = self.headers.get("X-Fixture-Exit", "DIRECT")
        with self.server.lock:
            self.server.markers.append(marker)
        data = marker.encode("ascii")
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class ResidentialProxy(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        sock.settimeout(3)
        try:
            if self.server.protocol == "socks5":
                host, port = self.socks(sock)
            else:
                host, port = self.http(sock)
            if host != LOOPBACK or port != self.server.origin_port:
                return
            request = read_headers(sock)
            method, path, _ = request.split("\r\n", 1)[0].split(" ", 2)
            if method != "GET":
                return
            conn = http.client.HTTPConnection(LOOPBACK, port, timeout=3)
            try:
                conn.request("GET", path, headers={"X-Fixture-Exit": self.server.marker})
                response = conn.getresponse()
                body = response.read()
            finally:
                conn.close()
            sock.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
        except (OSError, EOFError, ValueError):
            return

    def record_auth(self, accepted):
        with self.server.lock:
            self.server.auth_results.append(accepted)
        return accepted

    def socks(self, sock):
        version, count = receive_exact(sock, 2)
        methods = receive_exact(sock, count)
        if version != 5 or 2 not in methods:
            raise ValueError("fixture requires SOCKS authentication")
        sock.sendall(b"\x05\x02")
        version, length = receive_exact(sock, 2)
        username = receive_exact(sock, length).decode("ascii")
        password = receive_exact(sock, receive_exact(sock, 1)[0]).decode("ascii")
        accepted = self.record_auth(version == 1 and (username, password) == self.server.credentials)
        sock.sendall(b"\x01" + (b"\x00" if accepted else b"\x01"))
        if not accepted:
            raise ValueError("fixture credentials rejected")
        version, command, reserved, address_type = receive_exact(sock, 4)
        if (version, command, reserved) != (5, 1, 0):
            raise ValueError("fixture accepts TCP CONNECT only")
        if address_type == 1:
            host = socket.inet_ntoa(receive_exact(sock, 4))
        elif address_type == 3:
            host = receive_exact(sock, receive_exact(sock, 1)[0]).decode("ascii")
        else:
            raise ValueError("fixture accepts IPv4 only")
        port = int.from_bytes(receive_exact(sock, 2), "big")
        sock.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
        return host, port

    def http(self, sock):
        headers = read_headers(sock).split("\r\n")
        method, authority, _ = headers[0].split(" ", 2)
        fields = dict(line.split(":", 1) for line in headers[1:] if ":" in line)
        expected = "Basic " + base64.b64encode(":".join(self.server.credentials).encode()).decode()
        accepted = self.record_auth(fields.get("Proxy-Authorization", "").strip() == expected)
        if method != "CONNECT" or not accepted:
            sock.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
            raise ValueError("fixture HTTP credentials rejected")
        host, port = authority.rsplit(":", 1)
        sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        return host, int(port)


class ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class XrayGatewayRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binary = os.environ.get("XRAY_BIN")
        if not binary:
            raise unittest.SkipTest("set XRAY_BIN to opt into real Xray runtime verification")
        cls.binary = str(Path(binary).resolve())
        if not Path(cls.binary).is_file() or not os.access(cls.binary, os.X_OK):
            raise RuntimeError("XRAY_BIN must name an executable Xray binary")
        if not shutil.which("openssl") or not shutil.which("curl"):
            raise RuntimeError("runtime verification requires openssl and curl")
        root = Path(os.environ.get("TARGET_ROOT", Path(__file__).resolve().parents[1])).resolve()
        package = types.ModuleType("_gateway_runtime_target")
        package.__path__ = [str(root / "server")]
        sys.modules[package.__name__] = package
        cls.module = importlib.import_module(package.__name__ + ".subscription_server")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gateway-runtime-")
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.addCleanup(self.temp.cleanup)
        self.processes = []
        self.servers = []
        self.addCleanup(self.cleanup_resources)
        self.make_certificates()

    def cleanup_resources(self):
        for process in reversed(self.processes):
            self.stop(process)
        for server in reversed(self.servers):
            server.shutdown()
            server.server_close()

    def stop(self, process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def command(self, args):
        result = subprocess.run(args, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace")[-1500:])
        return result

    def make_certificates(self):
        self.ca = self.root / "ca.pem"
        self.cert = self.root / "server.pem"
        self.key = self.root / "server.key"
        ca_key = self.root / "ca.key"
        csr = self.root / "server.csr"
        extension = self.root / "extensions.cnf"
        extension.write_text("subjectAltName=DNS:localhost\nbasicConstraints=CA:FALSE\n"
                             "extendedKeyUsage=serverAuth\nkeyUsage=digitalSignature,keyEncipherment\n")
        self.command(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                      "-keyout", str(ca_key), "-out", str(self.ca), "-days", "1",
                      "-subj", "/CN=Private Loopback Test CA", "-addext", "basicConstraints=critical,CA:TRUE"])
        self.command(["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes",
                      "-keyout", str(self.key), "-out", str(csr), "-subj", "/CN=localhost"])
        self.command(["openssl", "x509", "-req", "-in", str(csr), "-CA", str(self.ca),
                      "-CAkey", str(ca_key), "-CAcreateserial", "-out", str(self.cert),
                      "-days", "1", "-extfile", str(extension)])
        for path in self.root.iterdir():
            path.chmod(0o600)

    def serve(self, server):
        self.servers.append(server)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return server

    def proxy(self, protocol, marker, origin_port, port=0):
        server = ProxyServer((LOOPBACK, port), ResidentialProxy)
        server.protocol = protocol
        server.marker = marker
        server.credentials = ("synthetic-" + marker, "synthetic-password-" + marker)
        server.origin_port = origin_port
        server.lock = threading.Lock()
        server.auth_results = []
        return self.serve(server)

    def start_xray(self, config, label):
        path = self.root / (label + ".json")
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        self.command([self.binary, "run", "-test", "-config", str(path)])
        log = self.root / (label + ".log")
        with log.open("wb") as output:
            process = subprocess.Popen([self.binary, "run", "-config", str(path)],
                                       stdout=output, stderr=subprocess.STDOUT)
        log.chmod(0o600)
        self.processes.append(process)
        port = config["inbounds"][0]["port"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail("synthetic Xray process exited: " + log.read_text(errors="replace")[-1500:])
            try:
                with socket.create_connection((LOOPBACK, port), timeout=0.1):
                    return process
            except OSError:
                time.sleep(0.03)
        self.fail("synthetic Xray listener startup timed out")

    def local_export(self, store, server_id, endpoint_map):
        exported = store.export_xray_config(server_id)
        self.assertIsInstance(exported, dict)
        raw_path = self.root / (server_id + "-export.json")
        raw_path.write_text(json.dumps(exported))
        raw_path.chmod(0o600)
        self.command([self.binary, "run", "-test", "-config", str(raw_path)])
        config = copy.deepcopy(exported)
        self.assertEqual(len(config["inbounds"]), 1)
        config["inbounds"][0]["listen"] = LOOPBACK
        self.assertLessEqual({item["protocol"] for item in config["outbounds"]},
                             {"blackhole", "socks", "http"})
        rewritten = set()
        for outbound in config["outbounds"]:
            if outbound["protocol"] in {"socks", "http"}:
                settings = outbound["settings"]
                address = settings["address"]
                self.assertIn(address, endpoint_map)
                self.assertEqual(settings["port"], endpoint_map[address])
                settings["address"] = LOOPBACK
                rewritten.add(address)
        self.assertEqual(rewritten, set(endpoint_map))
        return config

    def client(self, identity, gateway_port, label, server_name="localhost"):
        port = free_port()
        config = {
            "log": {"loglevel": "warning"},
            "inbounds": [{"listen": LOOPBACK, "port": port, "protocol": "socks",
                          "settings": {"auth": "noauth", "udp": False}}],
            "outbounds": [{"protocol": "vless", "settings": {
                "address": LOOPBACK, "port": gateway_port, "id": identity, "encryption": "none"},
                "streamSettings": {"network": "tcp", "security": "tls", "tlsSettings": {
                    "serverName": server_name, "allowInsecure": False,
                    "certificates": [{"certificateFile": str(self.ca), "usage": "verify"}]}}}],
        }
        self.start_xray(config, label)
        return port

    def fetch(self, client_port, origin_port):
        return subprocess.run(["curl", "--silent", "--show-error", "--fail", "--max-time", "3",
                               "--noproxy", "", "--proxy", "socks5h://127.0.0.1:" + str(client_port),
                               "http://127.0.0.1:" + str(origin_port) + "/canary"],
                              capture_output=True, timeout=4)

    def assert_response(self, client_port, origin_port, marker):
        result = self.fetch(client_port, origin_port)
        self.assertEqual(result.returncode, 0, "synthetic tunneled request failed")
        self.assertEqual(result.stdout, marker.encode())

    def assert_denied(self, client_port, origin):
        before = list(origin.markers)
        result = self.fetch(client_port, origin.server_address[1])
        self.assertNotEqual(result.returncode, 0, "unauthorized or unavailable route delivered traffic")
        self.assertEqual(list(origin.markers), before, "denied request reached origin")

    def test_shared_gateway_routes_and_revokes_without_direct_fallback(self):
        origin = ThreadingHTTPServer((LOOPBACK, 0), Origin)
        origin.markers = []
        origin.lock = threading.Lock()
        self.serve(origin)
        origin_port = origin.server_address[1]
        proxy_a = self.proxy("socks5", "A", origin_port)
        proxy_b = self.proxy("http", "B", origin_port)
        store = self.module.SubscriptionStore(self.root / "subscriptions.db")
        self.addCleanup(store.close)
        gateway_ports = {"cloud-1": free_port(), "cloud-2": free_port()}
        for server_id, port in gateway_ports.items():
            store.add_server({"id": server_id, "name": server_id, "host": LOOPBACK,
                              "gateway": {"kind": "xray-vless-tls", "port": port,
                                          "server_name": "localhost", "certificate_file": str(self.cert),
                                          "key_file": str(self.key)}})
        users = {}
        addresses = {"A": "8.8.8.8", "B": "1.1.1.1", "C": "9.9.9.9"}
        for marker, proxy, cloud in [("A", proxy_a, "cloud-1"), ("B", proxy_b, "cloud-1"),
                                      ("C", proxy_b, "cloud-2")]:
            store.add_ip({"id": "ip-" + marker, "name": "exit-" + marker,
                          "address": addresses[marker], "port": proxy.server_address[1],
                          "protocol": proxy.protocol, "server_id": cloud,
                          "config": {"username": proxy.credentials[0], "password": proxy.credentials[1]}})
            created = store.create_user({"id": "user-" + marker, "name": "user-" + marker,
                                         "ip_id": "ip-" + marker}, "http://127.0.0.1")
            raw, _, expected_ip, headers = store.subscription(created["token"], "uri")
            uri = urlsplit(raw.decode().strip())
            self.assertEqual(uri.scheme, "vless")
            self.assertEqual(uri.hostname, LOOPBACK)
            self.assertEqual(uri.port, gateway_ports[cloud])
            self.assertEqual(expected_ip, addresses[marker])
            self.assertEqual(headers["X-Claude-Switcher-Egress-IP"], addresses[marker])
            self.assertEqual(parse_qs(uri.query).get("security"), ["tls"])
            users[marker] = uri.username
        self.assertEqual(len(set(users.values())), 3)
        map_1 = {addresses["A"]: proxy_a.server_address[1], addresses["B"]: proxy_b.server_address[1]}
        gateway_1 = self.start_xray(self.local_export(store, "cloud-1", map_1), "gateway-1")
        self.start_xray(self.local_export(store, "cloud-2", {addresses["C"]: proxy_b.server_address[1]}),
                        "gateway-2")
        client_a = self.client(users["A"], gateway_ports["cloud-1"], "client-a")
        client_b = self.client(users["B"], gateway_ports["cloud-1"], "client-b")
        self.assert_response(client_a, origin_port, "A")
        self.assert_response(client_b, origin_port, "B")
        self.assertTrue(proxy_a.auth_results and all(proxy_a.auth_results), "SOCKS credentials not accepted")
        self.assertTrue(proxy_b.auth_results and all(proxy_b.auth_results), "HTTP credentials not accepted")
        self.assertEqual(origin.markers, ["A", "B"])
        wrong = self.client(str(uuid.uuid4()), gateway_ports["cloud-1"], "client-invalid")
        self.assert_denied(wrong, origin)
        cross = self.client(users["A"], gateway_ports["cloud-2"], "client-cross-server")
        self.assert_denied(cross, origin)
        valid_2 = self.client(users["C"], gateway_ports["cloud-2"], "client-server-2")
        self.assert_response(valid_2, origin_port, "B")
        wrong_tls = self.client(users["B"], gateway_ports["cloud-1"], "client-wrong-tls", "wrong.invalid")
        self.assert_denied(wrong_tls, origin)
        proxy_a.shutdown()
        proxy_a.server_close()
        self.servers.remove(proxy_a)
        self.assert_denied(client_a, origin)
        self.assert_response(client_b, origin_port, "B")
        self.proxy("socks5", "A", origin_port, map_1[addresses["A"]])
        self.assert_response(client_a, origin_port, "A")
        store.disable("user-A")
        revoked = store.export_xray_config("cloud-1")
        self.assertNotIn(users["A"], json.dumps(revoked))
        self.stop(gateway_1)
        self.start_xray(self.local_export(store, "cloud-1", {addresses["B"]: proxy_b.server_address[1]}),
                        "gateway-1-revoked")
        self.assert_denied(client_a, origin)
        self.assert_response(client_b, origin_port, "B")
        self.assertNotIn("DIRECT", origin.markers)


if __name__ == "__main__":
    unittest.main()
