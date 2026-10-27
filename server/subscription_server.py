#!/usr/bin/env python3
"""Small dependency-free control plane for one-user/one-egress-IP subscriptions.

The HTTP surface is intentionally narrow.  It is suitable for local development
and a protected private deployment behind TLS and a reverse proxy.  It does not
apply Xray snapshots remotely or process payments. It generates private Xray
configuration snapshots for manual application.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import stat
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from .xui_contract import desired_revision
from .materials import MAX_UPLOAD_BODY, ServerMaterials, migrate as migrate_materials
from .admin_auth import AdminAuth, LoginError, SESSION_TTL, load_admin_auth_file
from .gateway import DomainError, managed_nodes, managed_uri, upstream_config, validate_gateway, xray_config, xray_merge


ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
TOKEN_PREFIX = "cs_"
MAX_BODY = 64 * 1024
SUBSCRIPTION_PATH_RE = re.compile(r"/sub/(cs_[A-Za-z0-9_-]+)/([A-Za-z0-9][A-Za-z0-9_.-]{0,63})\.yaml")
LEGACY_PATH_RE = re.compile(r"/s/(cs_[A-Za-z0-9_-]+)")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
ADMIN_SESSION_COOKIE = "cs_admin_session"


from .subscription_template import (DEFAULT_TEMPLATE, PROTECTED_AI_DOMAINS, render_template,
                                    validate_template, template_revision)

ROUTING_RULES = {"strict": (), "domestic-direct": tuple(DEFAULT_TEMPLATE["domestic_direct_rules"])}


def managed_clash(nodes: list[dict[str, Any]], *, routing_profile: str = "strict", template=None) -> dict[str, Any]:
    return render_template(nodes, template=template, routing_profile=routing_profile)

PUBLIC_FIELDS = {
    "server",
    "port",
    "protocol",
    "name",
    "uuid",
    "password",
    "username",
    "cipher",
    "tls",
    "servername",
    "network",
    "flow",
    "path",
    "host",
    "serviceName",
    "sni",
    "udp",
    "skip-cert-verify",
}


def public_origin(value: str) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 33 or c.isspace() for c in value):
        raise ValueError("invalid subscription base URL")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if (parsed.scheme not in {"https", "http"} or not host or parsed.username is not None
                or parsed.password is not None or parsed.path not in {"", "/"}
                or "?" in value or "#" in value or "\\" in value or "%" in value
                or (port is not None and not 1 <= port <= 65535)
                or (parsed.scheme == "http" and host not in LOOPBACK_HOSTS)):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if len(host) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                                          for label in host.split(".")):
                raise ValueError
        if parsed.netloc.endswith(":"):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("invalid subscription base URL") from None
    authority = "[" + host + "]" if ":" in host else host.lower()
    if port is not None and port != (443 if parsed.scheme == "https" else 80):
        authority += ":" + str(port)
    return parsed.scheme + "://" + authority


INTEGRATION_CODES = {
    "ok", "source_invalid", "source_changed", "target_drift", "ownership_conflict",
    "panel_unavailable", "panel_auth_failed", "apply_failed", "runtime_unverified",
    "unsafe_path", "internal_error",
}
REVISION_RE = re.compile(r"^[a-f0-9]{64}$")


def integration_status(store: SubscriptionStore, server_id: str, status_path: Path | None) -> dict[str, Any]:
    validate_id(server_id, "server id")
    with store._lock:
        server = next((row for row in store.list_servers() if row["id"] == server_id), None)
        if server is None:
            raise DomainError("unknown server")
        revision = None
        if server["gateway"] and server["gateway"]["kind"] in {"xray-vless-reality", "xray-vless-ws"}:
            revision = desired_revision(server, store.export_xray_config(server_id, merge=True))
    result = {"server_id": server_id, "state": "unconfigured", "desired_revision": revision,
              "applied_revision": None, "checked_at": None, "applied_at": None,
              "managed_count": None, "code": "unconfigured"}
    if status_path is None or revision is None:
        return result
    try:
        fd = os.open(status_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                raise ValueError
            receipt = json.loads(stream.read(65537).decode("utf-8"))
        if not isinstance(receipt, dict):
            raise ValueError
        if receipt.get("server_id") != server_id:
            return result
        state = receipt.get("state")
        source_revision, applied_revision = receipt.get("desired_revision"), receipt.get("applied_revision")
        if (state not in {"applied", "applying", "blocked"}
                or not isinstance(source_revision, str) or not REVISION_RE.fullmatch(source_revision)
                or (applied_revision is not None and (not isinstance(applied_revision, str)
                    or not REVISION_RE.fullmatch(applied_revision)))):
            raise ValueError
        def timestamp(value: Any) -> datetime:
            if not isinstance(value, str) or len(value) > 40:
                raise ValueError
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
                raise ValueError
            return parsed
        checked = timestamp(receipt.get("checked_at"))
        now = datetime.now(timezone.utc)
        age = (now - checked).total_seconds()
        applied = timestamp(receipt["applied_at"]) if receipt.get("applied_at") is not None else None
        count = receipt.get("managed_count")
        if (age < -5 or (applied and applied > checked) or type(count) is not int or count < 0
                or (state == "applied" and (applied is None or applied_revision is None))):
            raise ValueError
        code = receipt.get("code")
        if state == "applied" and code != "ok":
            raise ValueError
        code = code if isinstance(code, str) and code in INTEGRATION_CODES else "status_unavailable"
        if age > 60:
            state, code = "stale", "stale"
        elif source_revision != revision or (state == "applied" and applied_revision != revision):
            state, code = "pending", "revision_changed"
        result.update(state=state, code=code, applied_revision=applied_revision,
                      checked_at=checked.isoformat(), applied_at=applied.isoformat() if applied else None,
                      managed_count=count)
        return result
    except FileNotFoundError:
        return result
    except (OSError, ValueError, TypeError, UnicodeError):
        result.update(state="blocked", code="status_unavailable")
        return result


def subscription_format(query: str, user_agent: str) -> str:
    try:
        if re.search(r"%(?![0-9A-Fa-f]{2})", query):
            raise ValueError
        parsed = parse_qs(query, keep_blank_values=True, strict_parsing=True, errors="strict") if query else {}
        if set(parsed) - {"format", "routing"}:
            raise ValueError
        values = parsed.get("format")
        if values is None:
            return "base64" if "shadowrocket" in user_agent.lower() else "clash"
        formats = {"clash": "clash", "uri": "uri", "base64": "base64", "shadowrocket": "base64"}
        if len(values) != 1 or values[0] not in formats:
            raise ValueError
        return formats[values[0]]
    except (ValueError, UnicodeError):
        raise DomainError("invalid subscription format") from None


def subscription_routing(query: str) -> str:
    """Return the explicitly requested routing profile.

    Strict routing is deliberately the default.  The domestic-direct profile is
    opt-in through one exact query parameter so links cannot silently change
    policy because of an unknown or repeated parameter.
    """
    try:
        if re.search(r"%(?![0-9A-Fa-f]{2})", query):
            raise ValueError
        parsed = parse_qs(query, keep_blank_values=True, strict_parsing=True, errors="strict") if query else {}
        if set(parsed) - {"format", "routing"}:
            raise ValueError
        values = parsed.get("routing")
        if values is None:
            return "strict"
        if len(values) != 1 or values[0] not in {"strict", "domestic-direct"}:
            raise ValueError
        return values[0]
    except (ValueError, UnicodeError):
        raise DomainError("invalid subscription routing") from None


def subscription_path_token(path: str) -> str | None:
    match = SUBSCRIPTION_PATH_RE.fullmatch(path) or LEGACY_PATH_RE.fullmatch(path)
    return match.group(1) if match else None


@dataclass(frozen=True)
class ImportedProxy:
    line: int
    address: str
    port: int
    username: str
    password: str

    @property
    def ip_id(self) -> str:
        return "proxy-" + self.address.replace(".", "-")


def parse_proxy_import(text: str) -> tuple[list[ImportedProxy], list[dict[str, Any]], int]:
    rows: list[ImportedProxy] = []
    errors: list[dict[str, Any]] = []
    lines = [(number, raw.removesuffix("\r")) for number, raw in enumerate(text.split("\n"), 1)
             if raw.strip()]
    if not lines:
        return rows, [{"line": 0, "code": "empty_batch"}], 0
    if len(lines) > 200:
        return rows, [{"line": lines[200][0], "code": "too_many_rows"}], len(lines)
    for number, raw in lines:
        code = ""
        if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in raw):
            code = "control_character"
        else:
            parts = raw.split(":", 3)
            if len(parts) != 4:
                code = "invalid_format"
            else:
                address, port_text, username, password = parts
                try:
                    parsed = ipaddress.IPv4Address(address)
                    if not parsed.is_global or parsed.is_multicast or parsed.is_unspecified:
                        code = "invalid_address"
                except ValueError:
                    code = "invalid_address"
                if not code and (not re.fullmatch(r"[0-9]{1,5}", port_text)
                                 or not 1 <= int(port_text) <= 65535):
                    code = "invalid_port"
                if not code and (not username.strip() or not password.strip()
                                 or len(username.encode("utf-8")) > 255 or len(password.encode("utf-8")) > 255):
                    code = "invalid_credentials"
                if not code:
                    rows.append(ImportedProxy(number, str(parsed), int(port_text), username, password))
        if code:
            errors.append({"line": number, "code": code})
    return rows, errors, len(lines)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def validate_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise DomainError(f"invalid {field}")
    return value


def validate_ip(value: Any) -> str:
    if not isinstance(value, str):
        raise DomainError("invalid address")
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError as exc:
        raise DomainError("invalid address") from exc
    if parsed.is_multicast or parsed.is_unspecified or parsed.is_loopback or not parsed.is_global:
        raise DomainError("address must be a public routable unicast IP")
    return str(parsed)


def validate_port(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise DomainError("invalid port")
    return value


def validate_protocol(value: Any) -> str:
    allowed = {"vless", "vmess", "trojan", "ss", "socks5", "http"}
    if not isinstance(value, str) or value not in allowed:
        raise DomainError("unsupported protocol")
    return value


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_hint(token: str) -> str:
    return token[:7] + "…" + token[-4:]


def yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def yaml_lines(value: Any, indent: int = 0) -> list[str]:
    """Render the small YAML subset used by Clash without a YAML dependency."""
    pad = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}{key}:")
                lines.extend(yaml_lines(item, indent + 2))
            else:
                lines.append(f"{pad}{key}: {yaml_scalar(item)}")
        return lines
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict):
                first = True
                for key, child in item.items():
                    if first:
                        if isinstance(child, (dict, list)):
                            lines.append(f"{pad}- {key}:")
                            lines.extend(yaml_lines(child, indent + 4))
                        else:
                            lines.append(f"{pad}- {key}: {yaml_scalar(child)}")
                        first = False
                    elif isinstance(child, (dict, list)):
                        lines.append(f"{pad}  {key}:")
                        lines.extend(yaml_lines(child, indent + 4))
                    else:
                        lines.append(f"{pad}  {key}: {yaml_scalar(child)}")
            else:
                lines.append(f"{pad}- {yaml_scalar(item)}")
        return lines
    return [f"{pad}{yaml_scalar(value)}"]


def render_clash(user: dict[str, Any], ip: dict[str, Any], *, routing_profile: str = "strict", template=None) -> str:
    if routing_profile not in ROUTING_RULES:
        raise DomainError("invalid subscription routing")
    config = json.loads(ip["config_json"])
    name = str(config.get("name") or ip["name"] or f"{user['id']}-出口")
    proxy: dict[str, Any] = {
        "name": name,
        "type": ip["protocol"],
        "server": ip["address"],
        "port": ip["port"],
        "udp": bool(config.get("udp", True)),
    }
    for key in PUBLIC_FIELDS:
        if key in {"server", "port", "protocol", "name", "udp"}:
            continue
        if key in config:
            proxy[key] = config[key]
    document = managed_clash([proxy], routing_profile=routing_profile, template=template)
    return "\n".join(yaml_lines(document)) + "\n"


def render_uri(user: dict[str, Any], ip: dict[str, Any]) -> str:
    config = json.loads(ip["config_json"])
    protocol = ip["protocol"]
    name = quote(str(config.get("name") or ip["name"] or user["id"]), safe="")
    host = ip["address"]
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = ip["port"]
    if protocol in {"vless", "vmess"}:
        identity = config.get("uuid")
        if not isinstance(identity, str) or not identity:
            raise DomainError("IP config requires uuid for this protocol")
        query: list[str] = ["encryption=none"]
        if config.get("tls"):
            query.append("security=tls")
        if config.get("servername") or config.get("sni"):
            query.append("sni=" + quote(str(config.get("servername") or config.get("sni")), safe=""))
        if config.get("type") or config.get("network"):
            query.append("type=" + quote(str(config.get("type") or config.get("network")), safe=""))
        return f"{protocol}://{quote(identity, safe='')}@{host}:{port}?{'&'.join(query)}#{name}"
    if protocol == "trojan":
        password = config.get("password")
        if not isinstance(password, str) or not password:
            raise DomainError("IP config requires password for this protocol")
        query = "security=tls" if config.get("tls") else ""
        return f"trojan://{quote(password, safe='')}@{host}:{port}?{query}#{name}"
    if protocol == "ss":
        password = config.get("password")
        cipher = config.get("cipher", "aes-128-gcm")
        if not isinstance(password, str) or not password:
            raise DomainError("IP config requires password for this protocol")
        payload = f"{cipher}:{password}@{host}:{port}".encode()
        return f"ss://{base64.urlsafe_b64encode(payload).decode().rstrip('=')}#{name}"
    scheme = "socks5" if protocol == "socks5" else "http"
    auth = ""
    if config.get("username") or config.get("password"):
        auth = quote(str(config.get("username", "")), safe="") + ":" + quote(str(config.get("password", "")), safe="") + "@"
    return f"{scheme}://{auth}{host}:{port}#{name}"


class SubscriptionStore:
    """SQLite-backed domain store with transactional one-to-one bindings."""

    def __init__(self, path: str | os.PathLike[str]):
        if str(path) == ":memory:":
            raise ValueError("file-backed database required")
        db_path = Path(path).expanduser()
        db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent_mode = stat.S_IMODE(db_path.parent.stat().st_mode)
        if parent_mode & 0o077:
            raise ValueError("database directory must not be group/world accessible")
        if db_path.exists() and stat.S_IMODE(db_path.stat().st_mode) & 0o077:
            raise ValueError("database file must not be group/world accessible")
        self.path = str(db_path)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA busy_timeout = 5000")
        self.db.execute("PRAGMA journal_mode = DELETE")
        try:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute("""CREATE TABLE IF NOT EXISTS servers (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, host TEXT NOT NULL,
                    region TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','disabled')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
                self.db.execute("""CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
                    token_hint TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','disabled')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
                self.db.execute("""CREATE TABLE IF NOT EXISTS ips (
                    id TEXT PRIMARY KEY, address TEXT NOT NULL, port INTEGER NOT NULL,
                    protocol TEXT NOT NULL, name TEXT NOT NULL, config_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('available','disabled')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    server_id TEXT REFERENCES servers(id))""")
                server_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(servers)")}
                if "gateway_json" not in server_columns:
                    self.db.execute("ALTER TABLE servers ADD COLUMN gateway_json TEXT")
                user_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(users)")}
                if "ingress_uuid" not in user_columns:
                    self.db.execute("ALTER TABLE users ADD COLUMN ingress_uuid TEXT")
                if "subscription_token" not in user_columns:
                    self.db.execute("ALTER TABLE users ADD COLUMN subscription_token TEXT")
                missing = self.db.execute("SELECT id FROM users WHERE ingress_uuid IS NULL").fetchall()
                for user in missing:
                    self.db.execute("UPDATE users SET ingress_uuid=? WHERE id=?", (str(uuid.uuid4()), user["id"]))
                self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_ingress_uuid_unique ON users(ingress_uuid)")
                columns = {row["name"] for row in self.db.execute("PRAGMA table_info(ips)")}
                if "server_id" not in columns:
                    self.db.execute("ALTER TABLE ips ADD COLUMN server_id TEXT REFERENCES servers(id)")
                self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ips_address_unique ON ips(address)")
                self.db.execute("CREATE INDEX IF NOT EXISTS ips_server_id ON ips(server_id)")
                self.db.execute("""CREATE TABLE IF NOT EXISTS subscription_settings (
                    id INTEGER PRIMARY KEY CHECK(id=1), template_json TEXT NOT NULL)""")
                migrate_materials(self.db)
                self.db.execute("""CREATE TABLE IF NOT EXISTS bindings (
                    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    ip_id TEXT NOT NULL UNIQUE REFERENCES ips(id) ON DELETE RESTRICT,
                    bound_at TEXT NOT NULL)""")
        except sqlite3.IntegrityError as exc:
            self.db.close()
            raise DomainError("database migration conflict") from exc
        except Exception:
            self.db.close()
            raise

    def subscription_template(self) -> dict[str, Any]:
        with self._lock:
            row = self.db.execute("SELECT template_json FROM subscription_settings WHERE id=1").fetchone()
            template = json.loads(row[0]) if row else json.loads(json.dumps(DEFAULT_TEMPLATE))
        preview_node = {"name": "example-ws-tls", "type": "vless", "server": "edge.example.invalid",
                        "port": 443, "uuid": "00000000-0000-4000-8000-000000000000", "network": "ws",
                        "tls": True, "skip-cert-verify": False, "udp": False,
                        "servername": "edge.example.invalid",
                        "ws-opts": {"path": "/example-ws", "headers": {"Host": "edge.example.invalid"}}}
        previews = {profile: "\n".join(yaml_lines(managed_clash([preview_node], template=template,
                    routing_profile=profile))) + "\n" for profile in ROUTING_RULES}
        return {"template": template, "revision": template_revision(template), "previews": previews}

    def save_subscription_template(self, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) != {"template", "revision"}:
            raise DomainError("invalid subscription template request")
        template = validate_template(payload["template"])
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if payload["revision"] != self.subscription_template()["revision"]:
                raise DomainError("subscription template changed; reload before saving")
            self.db.execute("INSERT INTO subscription_settings(id,template_json) VALUES(1,?) "
                            "ON CONFLICT(id) DO UPDATE SET template_json=excluded.template_json",
                            (json.dumps(template, ensure_ascii=False),))
            return self.subscription_template()

    @property
    def materials(self) -> ServerMaterials:
        return ServerMaterials(self.db, self._lock)

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def add_server(self, payload: dict[str, Any]) -> dict[str, Any]:
        server_id = validate_id(payload.get("id"), "server id")
        name = payload.get("name")
        region = payload.get("region", "")
        for value, field, required in [(name, "server name", True), (region, "server region", False)]:
            if (not isinstance(value, str) or len(value) > 120 or (required and not value.strip())
                    or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
                raise DomainError("invalid " + field)
        host = payload.get("host")
        if not isinstance(host, str) or not host or len(host) > 253:
            raise DomainError("invalid server host")
        try:
            ipaddress.ip_address(host)
            if "%" in host:
                raise ValueError("scoped host")
        except ValueError:
            if not all(re.fullmatch(r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?", label)
                       for label in host.split(".")):
                raise DomainError("invalid server host")
        with self._lock, self.db:
            old = self.db.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
            status = payload.get("status", old["status"] if old else "active")
            if not isinstance(status, str) or status not in {"active", "disabled"}:
                raise DomainError("invalid server status")
            gateway = validate_gateway(payload["gateway"]) if "gateway" in payload else (
                json.loads(old["gateway_json"]) if old and old["gateway_json"] else None)
            if old and old["gateway_json"] and gateway is None and self.db.execute(
                    "SELECT 1 FROM bindings b JOIN ips i ON i.id=b.ip_id WHERE i.server_id=?", (server_id,)).fetchone():
                raise DomainError("gateway_in_use")
            now = utc_now()
            self.db.execute("""INSERT INTO servers(id,name,host,region,status,created_at,updated_at,gateway_json)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                host=excluded.host,region=excluded.region,status=excluded.status,updated_at=excluded.updated_at,
                gateway_json=excluded.gateway_json""",
                (server_id, name.strip(), host, region.strip(), status, old["created_at"] if old else now, now,
                 json.dumps(gateway) if gateway else None))
            return next(row for row in self.list_servers() if row["id"] == server_id)

    def list_servers(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = [dict(row) for row in self.db.execute("""SELECT s.*,COUNT(i.id) AS ip_count,
                COUNT(b.user_id) AS bound_count,
                COALESCE(SUM(CASE WHEN s.status='active' AND s.gateway_json IS NOT NULL AND i.status='available'
                    AND b.user_id IS NULL THEN 1 ELSE 0 END),0) AS available_count
                FROM servers s LEFT JOIN ips i ON i.server_id=s.id
                LEFT JOIN bindings b ON b.ip_id=i.id GROUP BY s.id ORDER BY s.id""")]
            for row in rows:
                row["gateway"] = json.loads(row.pop("gateway_json")) if row["gateway_json"] else None
                row.pop("gateway_json", None)
                row["gateway_ready"] = row["status"] == "active" and row["gateway"] is not None
            return rows

    def _active_server_locked(self, server_id: Any) -> sqlite3.Row:
        validate_id(server_id, "server id")
        row = self.db.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
        if not row or row["status"] != "active":
            raise DomainError("server is unavailable")
        return row

    def _ready_gateway_locked(self, server_id: Any) -> sqlite3.Row:
        server = self._active_server_locked(server_id)
        if not server["gateway_json"]:
            raise DomainError("gateway is unavailable")
        validate_gateway(json.loads(server["gateway_json"]))
        return server

    def export_xray_config(self, server_id: str, *, merge: bool = False) -> dict[str, Any]:
        validate_id(server_id, "server id")
        with self._lock:
            server = self.db.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
            if not server:
                raise DomainError("unknown server")
            if not server["gateway_json"]:
                raise DomainError("gateway is unavailable")
            rows = [dict(row) for row in self.db.execute("""SELECT u.ingress_uuid,i.id AS ip_id,
                i.address,i.port,i.protocol,i.config_json FROM users u JOIN bindings b ON b.user_id=u.id
                JOIN ips i ON i.id=b.ip_id WHERE i.server_id=? AND u.status='active'
                AND i.status='available' ORDER BY u.id""", (server_id,))]
            renderer = xray_merge if merge else xray_config
            return renderer(dict(server), validate_gateway(json.loads(server["gateway_json"])), rows)

    def assign_ip_server(self, ip_id: str, server_id: Any) -> dict[str, Any]:
        validate_id(ip_id, "IP id")
        with self._lock, self.db:
            self._active_server_locked(server_id)
            old = self.db.execute("SELECT * FROM ips WHERE id=?", (ip_id,)).fetchone()
            if not old:
                raise DomainError("unknown IP")
            bound = self.db.execute("SELECT 1 FROM bindings WHERE ip_id=?", (ip_id,)).fetchone()
            upstream_config(old["protocol"], json.loads(old["config_json"]))
            if bound and old["server_id"] is None:
                raise DomainError("bound legacy IP must be unbound before assignment")
            if bound:
                self._ready_gateway_locked(server_id)
            if bound and old["server_id"] is not None and old["server_id"] != server_id:
                raise DomainError("bound IP server cannot change")
            if old["server_id"] != server_id:
                self.db.execute("UPDATE ips SET server_id=?,updated_at=? WHERE id=?", (server_id, utc_now(), ip_id))
            return next(row for row in self.list_ips() if row["id"] == ip_id)

    def add_ip(self, payload: dict[str, Any]) -> dict[str, Any]:
        ip_id = validate_id(payload.get("id"), "IP id")
        address = validate_ip(payload.get("address"))
        port = validate_port(payload.get("port", 443))
        protocol = validate_protocol(payload.get("protocol", "vless"))
        name = payload.get("name") or ip_id
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise DomainError("invalid IP name")
        config = payload.get("config", {})
        if not isinstance(config, dict):
            raise DomainError("config must be an object")
        status = payload.get("status", "available")
        if not isinstance(status, str) or status not in {"available", "disabled"}:
            raise DomainError("invalid IP status")
        with self._lock, self.db:
            old = self.db.execute("SELECT * FROM ips WHERE id = ?", (ip_id,)).fetchone()
            server_id = payload.get("server_id", old["server_id"] if old else None)
            if not old or "server_id" in payload:
                self._active_server_locked(server_id)
            if server_id is not None:
                upstream_config(protocol, config)
            bound = self.db.execute("SELECT 1 FROM bindings WHERE ip_id=?", (ip_id,)).fetchone()
            if bound and old:
                if old["server_id"] is None and server_id is not None:
                    raise DomainError("bound legacy IP must be unbound before assignment")
                if server_id is not None:
                    self._ready_gateway_locked(server_id)
                if old["address"] != address:
                    raise DomainError("bound IP address cannot change")
                if old["server_id"] is not None and old["server_id"] != server_id:
                    raise DomainError("bound IP server cannot change")
            address_owner = self.db.execute("SELECT id FROM ips WHERE address = ? AND id <> ?", (address, ip_id)).fetchone()
            if address_owner:
                raise DomainError("address is already registered")
            now = utc_now()
            created_at = old["created_at"] if old else now
            self.db.execute(
                """INSERT INTO ips(id,address,port,protocol,name,config_json,status,created_at,updated_at,server_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET address=excluded.address, port=excluded.port,
                     protocol=excluded.protocol, name=excluded.name, config_json=excluded.config_json,
                     status=excluded.status, updated_at=excluded.updated_at,server_id=excluded.server_id""",
                (ip_id, address, port, protocol, name.strip(), json.dumps(config, ensure_ascii=False),
                 status, created_at, now, server_id),
            )
            return next(row for row in self.list_ips() if row["id"] == ip_id)

    def import_ips(self, payload: dict[str, Any], base_url: str) -> dict[str, Any]:
        text = payload.get("text")
        protocol = payload.get("protocol")
        dry_run = payload.get("dry_run", True)
        create_subscriptions = payload.get("create_subscriptions", False)
        if not isinstance(text, str):
            raise DomainError("invalid_text")
        if not isinstance(protocol, str) or protocol not in {"socks5", "http"}:
            raise DomainError("invalid_protocol")
        if not isinstance(dry_run, bool):
            raise DomainError("invalid_dry_run")
        if not isinstance(create_subscriptions, bool):
            raise DomainError("invalid_create_subscriptions")
        subscriptions: list[dict[str, str]] = []
        try:
            with self._lock, self.db:
                if not dry_run:
                    self.db.execute("BEGIN IMMEDIATE")
                server = self._active_server_locked(payload.get("server_id"))
                if create_subscriptions:
                    self._ready_gateway_locked(server["id"])
                parsed, errors, total = parse_proxy_import(text)
                existing = {row["address"]: (row["server_id"], row["server_name"])
                            for row in self.db.execute("""SELECT i.address,i.server_id,s.name AS server_name
                            FROM ips i LEFT JOIN servers s ON s.id=i.server_id""")}
                ids = {row["id"] for row in self.db.execute("SELECT id FROM ips")}
                additions: list[ImportedProxy] = []
                rows: list[dict[str, Any]] = []
                for proxy in parsed:
                    action = "skip" if proxy.address in existing else "add"
                    if action == "add" and proxy.ip_id in ids:
                        errors.append({"line": proxy.line, "code": "id_conflict"})
                        continue
                    owner_id, owner_name = existing.get(proxy.address, (server["id"], server["name"]))
                    rows.append({"line": proxy.line, "address": proxy.address, "port": proxy.port,
                                 "protocol": protocol, "action": action,
                                 "server_id": owner_id, "server_name": owner_name})
                    existing[proxy.address] = (owner_id, owner_name)
                    if action == "add":
                        additions.append(proxy)
                imported = 0
                if not errors and not dry_run:
                    now = utc_now()
                    self.db.executemany(
                        """INSERT INTO ips(id,address,port,protocol,name,config_json,status,created_at,updated_at,server_id)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        [(proxy.ip_id, proxy.address, proxy.port, protocol, proxy.ip_id,
                          json.dumps({"username": proxy.username, "password": proxy.password, "udp": False}),
                          "available", now, now, server["id"]) for proxy in additions],
                    )
                    if create_subscriptions:
                        for proxy in additions:
                            user_id = "sub-" + secrets.token_hex(8)
                            token = new_token()
                            self.db.execute(
                                "INSERT INTO users(id,name,token_hash,token_hint,status,created_at,updated_at,ingress_uuid,subscription_token) VALUES(?,?,?,?,?,?,?,?,?)",
                                (user_id, user_id, token_hash(token), token_hint(token), "active", now, now, str(uuid.uuid4()), token),
                            )
                            self.db.execute("INSERT INTO bindings VALUES(?,?,?)", (user_id, proxy.ip_id, now))
                            subscriptions.append({
                                "user_id": user_id, "ip_id": proxy.ip_id, "address": proxy.address,
                                "server_id": server["id"], "server_name": server["name"],
                                "subscription_url": self.subscription_url(base_url, token, server["id"]),
                            })
                    imported = len(additions)
        except sqlite3.IntegrityError as exc:
            raise DomainError("import_conflict") from exc
        return {"server_id": server["id"], "server_name": server["name"],
                "dry_run": dry_run, "valid": not errors, "total": total,
                "imported": imported, "skipped": sum(row["action"] == "skip" for row in rows),
                "rows": rows, "errors": errors, "subscriptions": subscriptions}

    def create_user(self, payload: dict[str, Any], base_url: str) -> dict[str, Any]:
        user_id = validate_id(payload.get("id"), "user id")
        name = payload.get("name") or user_id
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise DomainError("invalid user name")
        token = new_token()
        now = utc_now()
        with self._lock, self.db:
            try:
                self.db.execute(
                    "INSERT INTO users(id,name,token_hash,token_hint,status,created_at,updated_at,ingress_uuid,subscription_token) VALUES(?,?,?,?,?,?,?,?,?)",
                    (user_id, name.strip(), token_hash(token), token_hint(token), "active", now, now, str(uuid.uuid4()), token),
                )
                if payload.get("ip_id") is not None:
                    self._bind_locked(user_id, payload["ip_id"])
            except sqlite3.IntegrityError as exc:
                raise DomainError("user or binding already exists") from exc
        user = self.get_user(user_id)
        return {"user": user, "token": token,
                "subscription_url": self.subscription_url(base_url, token, user["server_id"])}

    def _bind_locked(self, user_id: str, ip_id: str | None) -> None:
        user = self.db.execute("SELECT status FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            raise DomainError("unknown user")
        if ip_id is not None and user["status"] != "active":
            raise DomainError("user is unavailable")
        self.db.execute("DELETE FROM bindings WHERE user_id = ?", (user_id,))
        if ip_id is None:
            return
        validate_id(ip_id, "IP id")
        ip_row = self.db.execute("SELECT status,server_id,protocol,config_json FROM ips WHERE id = ?", (ip_id,)).fetchone()
        if not ip_row or ip_row["status"] != "available":
            raise DomainError("IP is unavailable")
        self._ready_gateway_locked(ip_row["server_id"])
        upstream_config(ip_row["protocol"], json.loads(ip_row["config_json"]))
        try:
            self.db.execute("INSERT INTO bindings VALUES(?,?,?)", (user_id, ip_id, utc_now()))
        except sqlite3.IntegrityError as exc:
            raise DomainError("IP is already bound") from exc
        self.db.execute("UPDATE users SET updated_at = ? WHERE id = ?", (utc_now(), user_id))

    def bind(self, user_id: str, ip_id: str | None) -> dict[str, Any]:
        user_id = validate_id(user_id, "user id")
        with self._lock, self.db:
            self._bind_locked(user_id, ip_id)
        return self.get_user(user_id)

    def rotate_token(self, user_id: str, base_url: str) -> dict[str, Any]:
        user_id = validate_id(user_id, "user id")
        token = new_token()
        with self._lock, self.db:
            if not self.db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
                raise DomainError("unknown user")
            self.db.execute("UPDATE users SET token_hash=?, token_hint=?, subscription_token=?, updated_at=? WHERE id=?",
                            (token_hash(token), token_hint(token), token, utc_now(), user_id))
        user = self.get_user(user_id)
        return {"user": user, "token": token,
                "subscription_url": self.subscription_url(base_url, token, user["server_id"])}

    def disable(self, user_id: str) -> dict[str, Any]:
        user_id = validate_id(user_id, "user id")
        with self._lock, self.db:
            updated = self.db.execute("UPDATE users SET status='disabled', updated_at=? WHERE id=?", (utc_now(), user_id)).rowcount
            if not updated:
                raise DomainError("unknown user")
            # Disabling a user releases the IP for an explicit, auditable reassignment.
            self.db.execute("DELETE FROM bindings WHERE user_id = ?", (user_id,))
        return self.get_user(user_id)

    def get_user(self, user_id: str) -> dict[str, Any]:
        validate_id(user_id, "user id")
        with self._lock:
            row = self.db.execute(
                """SELECT u.id,u.name,u.token_hint,u.status,u.created_at,u.updated_at,b.ip_id,
                          i.address,i.port,i.protocol,i.name AS ip_name,
                          i.server_id,s.name AS server_name,s.status AS server_status
                   FROM users u LEFT JOIN bindings b ON b.user_id=u.id
                   LEFT JOIN ips i ON i.id=b.ip_id LEFT JOIN servers s ON s.id=i.server_id
                   WHERE u.id=?""", (user_id,)
            ).fetchone()
            if not row:
                raise DomainError("unknown user")
            return dict(row)

    def list_users(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self.db.execute(
                """SELECT u.id,u.name,u.token_hint,u.status,u.created_at,u.updated_at,b.ip_id,
                          i.address,i.port,i.protocol,i.name AS ip_name,
                          i.server_id,s.name AS server_name,s.status AS server_status
                   FROM users u LEFT JOIN bindings b ON b.user_id=u.id
                   LEFT JOIN ips i ON i.id=b.ip_id LEFT JOIN servers s ON s.id=i.server_id ORDER BY u.id"""
            )]

    def _ip_public(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result.pop("config_json", None)
        return result

    def list_ips(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._ip_public(row) for row in self.db.execute(
                """SELECT i.*,b.user_id,s.name AS server_name,s.status AS server_status FROM ips i
                   LEFT JOIN bindings b ON b.ip_id=i.id LEFT JOIN servers s ON s.id=i.server_id ORDER BY i.id"""
            )]

    def subscription_url(self, base_url: str, token: str, server_id: str | None = None) -> str:
        filename = validate_id(server_id, "server id") if server_id is not None else "subscription"
        return public_origin(base_url) + "/sub/" + quote(token, safe="") + "/" + filename + ".yaml"

    def list_subscriptions(self, base_url: str) -> list[dict[str, Any]]:
        public_origin(base_url)
        with self._lock:
            self.db.execute("SAVEPOINT subscription_delivery_read")
            try:
                items = []
                for user in self.list_users():
                    row = self.db.execute("SELECT subscription_token,token_hash FROM users WHERE id=?",
                                          (user["id"],)).fetchone()
                    token = row["subscription_token"]
                    state = "legacy" if token is None else "invalid"
                    if (isinstance(token, str) and re.fullmatch(r"cs_[A-Za-z0-9_-]+", token)
                            and len(token) >= 16 and isinstance(row["token_hash"], str)
                            and hmac.compare_digest(token_hash(token).encode(), row["token_hash"].encode())):
                        state = "stored"
                    item = {**user, "user_id": user["id"], "token_state": state, "delivery_state": "unavailable"}
                    if state == "stored":
                        try:
                            for fmt in ("clash", "base64"):
                                self.subscription(token, fmt)
                        except (DomainError, ValueError, TypeError, KeyError):
                            pass
                        else:
                            url = self.subscription_url(base_url, token, user["server_id"])
                            item.update(delivery_state="ready", subscription_url=url,
                                        clash_url=url + "?format=clash", shadowrocket_url=url + "?format=shadowrocket")
                    items.append(item)
                return items
            finally:
                self.db.execute("RELEASE SAVEPOINT subscription_delivery_read")

    def convert_subscription(self, payload: dict[str, Any], base_url: str, local_port: int) -> dict[str, Any]:
        try:
            value = payload.get("url")
            if set(payload) != {"url"} or not isinstance(value, str) or len(value) > 2048:
                raise ValueError
            if any(ord(c) < 33 or c.isspace() for c in value) or any(c in value for c in "#%\\"):
                raise ValueError
            parsed = urlsplit(value)
            if ("?" in value and parsed.query not in
                    {"format=clash", "format=shadowrocket", "format=uri", "format=base64"}):
                raise ValueError
            origin = public_origin(parsed.scheme + "://" + parsed.netloc)
            token = subscription_path_token(parsed.path)
            trusted_local = (parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS
                             and parsed.port in {8787, local_port})
            if not token or (origin != public_origin(base_url) and not trusted_local):
                raise ValueError
            with self._lock, self.db:
                self.db.execute("BEGIN IMMEDIATE")
                self.subscription(token, "clash")
                digest = token_hash(token)
                row = self.db.execute("SELECT id,subscription_token FROM users WHERE token_hash=?", (digest,)).fetchone()
                if not row:
                    raise ValueError
                if row["subscription_token"] != token:
                    updated = self.db.execute(
                        "UPDATE users SET subscription_token=? WHERE id=? AND token_hash=?",
                        (token, row["id"], digest)).rowcount
                    if updated != 1:
                        raise ValueError
                user = self.get_user(row["id"])
                return {"user": user,
                        "subscription_url": self.subscription_url(base_url, token, user["server_id"])}
        except (ValueError, TypeError, DomainError):
            raise DomainError("invalid_subscription_reference") from None

    def subscription(self, token: str, fmt: str, *, routing_profile: str = "strict") -> tuple[bytes, str, str, dict[str, Any]]:
        if routing_profile not in {"strict", "domestic-direct"}:
            raise DomainError("invalid subscription routing")
        if fmt not in {"clash", "uri", "base64"}:
            raise DomainError("invalid subscription format")
        if routing_profile != "strict" and fmt != "clash":
            raise DomainError("routing requires clash format")
        if not isinstance(token, str) or len(token) < 16:
            raise DomainError("invalid subscription")
        with self._lock:
            row = self.db.execute(
                """SELECT u.*,i.id AS ip_id,i.address,i.port,i.protocol,i.name AS ip_name,i.config_json,
                          i.server_id,s.host AS server_host,s.gateway_json
                   FROM users u JOIN bindings b ON b.user_id=u.id JOIN ips i ON i.id=b.ip_id
                   LEFT JOIN servers s ON s.id=i.server_id
                   WHERE u.token_hash=? AND i.status='available'
                     AND (i.server_id IS NULL OR s.status='active')""", (token_hash(token),)
            ).fetchone()
            if not row or row["status"] != "active":
                raise DomainError("subscription unavailable")
            ip = {
                "address": row["address"],
                "port": row["port"],
                "protocol": row["protocol"],
                "name": row["ip_name"],
                "config_json": row["config_json"],
            }
            user = dict(row)
            if row["server_id"] is not None:
                if not row["gateway_json"]:
                    raise DomainError("subscription unavailable")
                upstream_config(row["protocol"], json.loads(row["config_json"]))
                gateway = validate_gateway(json.loads(row["gateway_json"]))
                nodes = managed_nodes(user, {"host": row["server_host"]}, gateway)
                uri = "\n".join(managed_uri(node) for node in nodes)
                if fmt == "uri":
                    body, content_type = (uri + "\n").encode(), "text/plain; charset=utf-8"
                elif fmt == "base64":
                    body, content_type = base64.b64encode(uri.encode()) + b"\n", "text/plain; charset=utf-8"
                else:
                    document = managed_clash(nodes, routing_profile=routing_profile,
                                             template=self.subscription_template()["template"])
                    body, content_type = ("\n".join(yaml_lines(document)) + "\n").encode(), "text/yaml; charset=utf-8"
            elif fmt == "uri":
                body = (render_uri(user, ip) + "\n").encode()
                content_type = "text/plain; charset=utf-8"
            elif fmt == "base64":
                body = (base64.b64encode(render_uri(user, ip).encode()).decode() + "\n").encode()
                content_type = "text/plain; charset=utf-8"
            else:
                body = render_clash(user, ip, routing_profile=routing_profile,
                                    template=self.subscription_template()["template"]).encode()
                content_type = "text/yaml; charset=utf-8"
            headers = {"X-Claude-Switcher-Egress-IP": row["address"], "X-Claude-Switcher-User": row["id"]}
            return body, content_type, row["address"], headers


class SubscriptionHandler(BaseHTTPRequestHandler):
    store: SubscriptionStore
    admin_token: str
    admin_auth: AdminAuth | None
    base_url: str
    integration_status_file: Path | None

    def log_message(self, format: str, *args: Any) -> None:
        # Disable the default logger because it includes the remote IP and request path.
        # Operators can add a structured, redacted access log at the TLS proxy.
        return

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        if subscription_path_token(urlsplit(self.path).path) is not None:
            self.send_header("Vary", "User-Agent")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def _admin_transport(self, path: str) -> bool:
        if path != "/admin" and path != "/api" and not path.startswith("/api/"):
            return True
        host_headers = self.headers.get_all("Host", [])
        try:
            if len(host_headers) != 1:
                raise ValueError
            authority = host_headers[0]
            parsed = urlsplit("//" + authority)
            host, port = parsed.hostname, parsed.port
            if (not host or parsed.username is not None or parsed.password is not None
                    or parsed.path or parsed.query or parsed.fragment or any(c.isspace() for c in authority)
                    or any(c in authority for c in "\\%") or authority.endswith(":")):
                raise ValueError
            peer_loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
            if peer_loopback and host.lower() in LOOPBACK_HOSTS:
                return True
            configured = urlsplit(self.base_url)
            if (configured.scheme != "https" or host.lower() != configured.hostname
                    or (port or 443) != (configured.port or 443)):
                self._error(HTTPStatus.FORBIDDEN, "admin_host_not_allowed")
                return False
        except ValueError:
            self._error(HTTPStatus.FORBIDDEN, "admin_host_not_allowed")
            return False
        proto = self.headers.get_all("X-Forwarded-Proto", [])
        if peer_loopback and proto == ["https"]:
            return True
        if self.command == "GET" and path == "/admin":
            self.send_response(HTTPStatus.PERMANENT_REDIRECT)
            self.send_header("Location", self.base_url + "/admin")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._error(HTTPStatus.FORBIDDEN, "admin_https_required")
        return False

    def _browser_origin(self) -> str:
        authority = self.headers.get("Host", "")
        if urlsplit("//" + authority).hostname not in LOOPBACK_HOSTS:
            return self.base_url
        scheme = "https" if self.headers.get_all("X-Forwarded-Proto", []) == ["https"] else "http"
        return public_origin(scheme + "://" + authority)

    def _same_origin(self) -> bool:
        return self.headers.get_all("Origin", []) == [self._browser_origin()]

    def _session_token(self) -> str:
        authorization = self.headers.get("Authorization")
        if authorization is not None:
            return authorization[7:] if authorization.startswith("Bearer ") else ""
        try:
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            return cookie[ADMIN_SESSION_COOKIE].value if ADMIN_SESSION_COOKIE in cookie else ""
        except CookieError:
            return ""

    def _json(self, status: int, value: dict[str, Any], *, session_token: str | None = None) -> None:
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if session_token is not None:
            cookie = SimpleCookie()
            cookie[ADMIN_SESSION_COOKIE] = session_token
            item = cookie[ADMIN_SESSION_COOKIE]
            item["path"] = "/api"
            item["httponly"] = True
            item["samesite"] = "Strict"
            item["max-age"] = SESSION_TTL if session_token else 0
            if self._browser_origin().startswith("https://"):
                item["secure"] = True
            self.send_header("Set-Cookie", item.OutputString())
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str) -> None:
        self._json(status, {"error": code})

    def _authorized(self) -> bool:
        supplied = self._session_token()
        return bool(supplied) and (
            (self.headers.get("Authorization") is not None
             and hmac.compare_digest(supplied.encode("utf-8"), self.admin_token.encode("utf-8")))
            or bool(self.admin_auth and self.admin_auth.authorized(supplied))
        )

    def _require_admin(self) -> bool:
        if not self._authorized():
            self._error(HTTPStatus.UNAUTHORIZED, "admin_auth_required")
            return False
        if self.command not in {"GET", "HEAD"} and self.headers.get("Authorization") is None and not self._same_origin():
            self._error(HTTPStatus.FORBIDDEN, "admin_origin_required")
            return False
        return True

    def _body(self, limit: int = MAX_BODY) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise DomainError("invalid content length") from exc
        if length < 0:
            raise DomainError("invalid content length")
        if length > limit:
            raise DomainError("material_too_large" if limit == MAX_UPLOAD_BODY else "request body too large")
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise DomainError("request body must be an object")
        return value

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if not self._admin_transport(path):
            return
        try:
            if path == "/admin":
                body = Path(__file__).with_name("admin.html").read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/healthz":
                self._json(HTTPStatus.OK, {"status": "ok"})
            elif path == "/api/auth/session":
                if self._require_admin():
                    self._json(HTTPStatus.OK, {"authenticated": True,
                               "username": self.admin_auth.username if self.admin_auth else "admin"})
            elif path == "/api/subscription-template":
                if self._require_admin():
                    self._json(HTTPStatus.OK, self.store.subscription_template())
            elif path == "/api/servers":
                if self._require_admin():
                    self._json(HTTPStatus.OK, {"items": self.store.list_servers()})
            elif re.fullmatch(r"/api/servers/[^/]+/integration", path):
                if self._require_admin():
                    self._json(HTTPStatus.OK, integration_status(self.store, path.split("/")[3], self.integration_status_file))
            elif re.fullmatch(r"/api/servers/[^/]+/materials(?:/[^/]+/download)?", path):
                if self._require_admin():
                    parts = path.split('/')
                    if len(parts) == 5:
                        self._json(HTTPStatus.OK, {'items': self.store.materials.list(parts[3])})
                    else:
                        item, body = self.store.materials.download(parts[3], parts[5])
                        self.send_response(HTTPStatus.OK)
                        self.send_header('Content-Type', 'application/octet-stream')
                        self.send_header('Content-Disposition', "attachment; filename=material; filename*=UTF-8''" + quote(item['filename'], safe=''))
                        self.send_header('Content-Security-Policy', 'sandbox')
                        self.send_header('Content-Length', str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
            elif re.fullmatch(r"/api/servers/[^/]+/(xray-config|xray-merge)", path):
                if self._require_admin():
                    server_id = path.split("/")[3]
                    body = (json.dumps(self.store.export_xray_config(server_id, merge=path.endswith("/xray-merge")), ensure_ascii=False, indent=2) + "\n").encode()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Disposition", f'attachment; filename="xray-{server_id}{"-merge" if path.endswith("/xray-merge") else ""}.json"')
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            elif path == "/api/ips":
                if self._require_admin():
                    self._json(HTTPStatus.OK, {"items": self.store.list_ips()})
            elif path == "/api/users":
                if self._require_admin():
                    self._json(HTTPStatus.OK, {"items": self.store.list_users()})
            elif path == "/api/subscriptions":
                if self._require_admin():
                    self._json(HTTPStatus.OK, {"items": self.store.list_subscriptions(self.base_url)})
            elif subscription_path_token(path) is not None:
                token = subscription_path_token(path)
                query = urlsplit(self.path).query
                fmt = subscription_format(query, self.headers.get("User-Agent", ""))
                routing_profile = subscription_routing(query)
                body, content_type, egress_ip, headers = self.store.subscription(
                    token, fmt, routing_profile=routing_profile)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._error(HTTPStatus.NOT_FOUND, "not_found")
        except DomainError as exc:
            if re.fullmatch(r"/api/servers/[^/]+/(integration|xray-config|xray-merge|materials(?:/[^/]+/download)?)", path):
                status = HTTPStatus.NOT_FOUND if str(exc) in {"unknown server", "unknown material"} else (
                    HTTPStatus.CONFLICT if str(exc) in {"gateway is unavailable", "manual_panel_merge_required", "reality_gateway_required"} else HTTPStatus.BAD_REQUEST)
            else:
                status = HTTPStatus.NOT_FOUND if str(exc) == "subscription unavailable" else HTTPStatus.BAD_REQUEST
            self._error(status, str(exc))
        except (OSError, UnicodeError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request")

    def _login(self) -> None:
        if ((self.headers.get("Origin") is not None and not self._same_origin())
                or self.headers.get("Sec-Fetch-Site") == "cross-site"):
            self._error(HTTPStatus.FORBIDDEN, "admin_origin_required")
            return
        if self.admin_auth is None:
            self._error(HTTPStatus.NOT_FOUND, "auth_not_configured")
            return
        try:
            payload = self._body()
            username, password = payload.get("username"), payload.get("password")
            if (set(payload) != {"username", "password"} or not isinstance(username, str)
                    or not isinstance(password, str) or not username or len(username) > 128
                    or not password or len(password.encode("utf-8")) > 4096):
                raise DomainError("invalid_credentials")
            token = self.admin_auth.login(username, password, self.client_address[0])
        except LoginError as exc:
            status = HTTPStatus.TOO_MANY_REQUESTS if exc.code == "too_many_attempts" else HTTPStatus.UNAUTHORIZED
            self._error(status, exc.code)
            return
        except (DomainError, OSError, UnicodeError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "invalid_credentials")
            return
        self._json(HTTPStatus.OK, {"token": token, "username": self.admin_auth.username,
                                 "expires_in": SESSION_TTL}, session_token=token)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if not self._admin_transport(path):
            return
        if path == "/api/auth/login":
            self._login()
            return
        if not self._require_admin():
            return
        if path == "/api/auth/logout":
            if self.admin_auth:
                self.admin_auth.logout(self._session_token())
            self._json(HTTPStatus.OK, {"ok": True}, session_token="")
            return
        try:
            material_match = re.fullmatch(r"/api/servers/([^/]+)/materials", path)
            payload = self._body(MAX_UPLOAD_BODY if material_match else MAX_BODY) if self.headers.get("Content-Length") else {}
            if material_match:
                result = self.store.materials.add(material_match.group(1), payload)
                self._json(HTTPStatus.OK if result['duplicate'] else HTTPStatus.CREATED, result)
            elif path == "/api/servers":
                self._json(HTTPStatus.OK, self.store.add_server(payload))
            elif path == "/api/ips":
                self._json(HTTPStatus.OK, self.store.add_ip(payload))
            elif path == "/api/ips/import":
                self._json(HTTPStatus.OK, self.store.import_ips(payload, self.base_url))
            elif path == "/api/users":
                self._json(HTTPStatus.CREATED, self.store.create_user(payload, self.base_url))
            elif path == "/api/subscriptions/convert":
                self._json(HTTPStatus.OK, self.store.convert_subscription(payload, self.base_url, self.server.server_port))
            else:
                match = re.fullmatch(r"/api/users/([^/]+)/(rotate-token|disable)", path)
                if not match:
                    self._error(HTTPStatus.NOT_FOUND, "not_found")
                    return
                if match.group(2) == "rotate-token":
                    self._json(HTTPStatus.OK, self.store.rotate_token(match.group(1), self.base_url))
                else:
                    self._json(HTTPStatus.OK, self.store.disable(match.group(1)))
        except DomainError as exc:
            if re.fullmatch(r"/api/servers/[^/]+/materials", path):
                status = {'unknown server': HTTPStatus.NOT_FOUND, 'material_too_large': HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                          'material_quota_exceeded': HTTPStatus.CONFLICT}.get(str(exc), HTTPStatus.BAD_REQUEST)
            else:
                status = HTTPStatus.CONFLICT if "already" in str(exc) or "unavailable" in str(exc) or "reload before saving" in str(exc) else HTTPStatus.BAD_REQUEST
            self._error(status, str(exc))
        except (OSError, UnicodeError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request")

    def do_PUT(self) -> None:
        path = urlsplit(self.path).path
        if not self._admin_transport(path):
            return
        if not self._require_admin():
            return
        try:
            payload = self._body()
            if path == "/api/subscription-template":
                self._json(HTTPStatus.OK, self.store.save_subscription_template(payload))
                return
            owner_match = re.fullmatch(r"/api/ips/([^/]+)/server", path)
            if owner_match:
                self._json(HTTPStatus.OK, self.store.assign_ip_server(owner_match.group(1), payload.get("server_id")))
                return
            match = re.fullmatch(r"/api/users/([^/]+)/binding", path)
            if not match:
                self._error(HTTPStatus.NOT_FOUND, "not_found")
                return
            ip_id = payload.get("ip_id")
            if ip_id is not None and not isinstance(ip_id, str):
                raise DomainError("invalid IP id")
            self._json(HTTPStatus.OK, self.store.bind(match.group(1), ip_id))
        except DomainError as exc:
            status = HTTPStatus.CONFLICT if "already" in str(exc) or "unavailable" in str(exc) or "reload before saving" in str(exc) else HTTPStatus.BAD_REQUEST
            self._error(status, str(exc))
        except (OSError, UnicodeError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request")


def create_server(db_path: str, admin_token: str | None, host: str = "127.0.0.1", port: int = 8787,
                  base_url: str | None = None, admin_username: str | None = None,
                  admin_password_hash: str | None = None,
                  integration_status_file: str | None = None) -> ThreadingHTTPServer:
    if (admin_username is None) != (admin_password_hash is None):
        raise ValueError("admin username and password hash must be configured together")
    admin_auth = None if admin_username is None else AdminAuth(admin_username, admin_password_hash)
    if not admin_token and admin_auth:
        admin_token = secrets.token_urlsafe(32)
    if not admin_token or len(admin_token) < 16:
        raise ValueError("admin token must be at least 16 characters")
    authority = "[::1]" if host == "::1" else host
    base = public_origin(base_url if base_url is not None else f"http://{authority}:{port or 8787}")
    status_path = Path(integration_status_file) if integration_status_file is not None else None
    if status_path is not None and not status_path.is_absolute():
        raise ValueError("integration status file must be an absolute path")
    store = SubscriptionStore(db_path)

    class Handler(SubscriptionHandler):
        pass

    Handler.store = store
    Handler.admin_token = admin_token
    Handler.admin_auth = admin_auth
    Handler.base_url = base
    Handler.integration_status_file = status_path
    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except Exception:
        store.close()
        raise
    if base_url is None:
        Handler.base_url = public_origin(f"http://{authority}:{server.server_port}")
    server.store = store  # type: ignore[attr-defined]
    server.admin_auth = admin_auth  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="subscription.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--base-url", default=os.environ.get("CLAUDE_SWITCHER_BASE_URL"))
    parser.add_argument("--admin-token", default=os.environ.get("CLAUDE_SWITCHER_ADMIN_TOKEN"))
    parser.add_argument("--admin-auth-file", default=os.environ.get("CLAUDE_SWITCHER_ADMIN_AUTH_FILE"))
    parser.add_argument("--integration-status-file", default=os.environ.get("CLAUDE_SWITCHER_INTEGRATION_STATUS_FILE"))
    args = parser.parse_args(argv)
    auth_config = {}
    if args.admin_auth_file:
        try:
            auth_config = load_admin_auth_file(args.admin_auth_file)
        except ValueError:
            parser.error("invalid admin authentication configuration")
    if not args.admin_token and not auth_config:
        parser.error("--admin-auth-file or --admin-token is required")
    server = create_server(args.db, args.admin_token, args.host, args.port, args.base_url,
                           auth_config.get("username"), auth_config.get("password_hash"), args.integration_status_file)
    print(f"claude-switcher server listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.shutdown()
        server.store.close()  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
