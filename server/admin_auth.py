from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from typing import Callable


SESSION_TTL = 8 * 60 * 60
PASSWORD_ROUNDS = 600_000
_HASH_RE = re.compile(r"^pbkdf2_sha256\$([0-9]{6,7})\$([A-Za-z0-9_-]{22})\$([0-9a-f]{64})$")


def _password_parts(encoded: str) -> tuple[int, bytes, bytes]:
    match = _HASH_RE.fullmatch(encoded) if isinstance(encoded, str) else None
    if match is None or not 100_000 <= int(match[1]) <= 2_000_000:
        raise ValueError("invalid admin password hash")
    salt = base64.urlsafe_b64decode(match[2] + "==")
    if base64.urlsafe_b64encode(salt).decode().rstrip("=") != match[2]:
        raise ValueError("invalid admin password hash")
    return int(match[1]), salt, bytes.fromhex(match[3])


def hash_admin_password(password: str) -> str:
    if not isinstance(password, str) or not password or len(password.encode("utf-8")) > 4096:
        raise ValueError("invalid admin password")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ROUNDS)
    return f"pbkdf2_sha256${PASSWORD_ROUNDS}${base64.urlsafe_b64encode(salt).decode().rstrip('=')}${digest.hex()}"


def load_admin_auth_file(path: str | Path) -> dict[str, str]:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 16_384:
                raise ValueError
            value = json.loads(stream.read(16_385).decode("utf-8"))
        if not isinstance(value, dict) or set(value) != {"username", "password_hash"}:
            raise ValueError
        AdminAuth(value["username"], value["password_hash"])
        return value
    except (OSError, ValueError, TypeError, UnicodeError):
        raise ValueError("invalid admin authentication configuration") from None


class LoginError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class AdminAuth:
    def __init__(self, username: str, password_hash: str, *, clock: Callable[[], float] = time.monotonic):
        if (not isinstance(username, str) or not username.strip() or len(username) > 128
                or any(ord(char) < 32 or ord(char) == 127 for char in username)):
            raise ValueError("invalid admin username")
        self.username = username
        self._password = _password_parts(password_hash)
        self._clock = clock
        self._sessions: dict[bytes, float] = {}
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        self._sessions = {key: expiry for key, expiry in self._sessions.items() if expiry > now}
        self._failures = {key: [stamp for stamp in stamps if stamp > now - 60]
                          for key, stamps in self._failures.items() if stamps[-1] > now - 60}

    def login(self, username: str, password: str, remote: str) -> str:
        with self._lock:
            now = self._clock()
            self._prune(now)
            failures = self._failures.get(remote, [])
            if len(failures) >= 5 or (remote not in self._failures and len(self._failures) >= 1024):
                raise LoginError("too_many_attempts")
            rounds, salt, expected = self._password
            digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
            correct_name = hmac.compare_digest(username.encode("utf-8"), self.username.encode("utf-8"))
            if not hmac.compare_digest(digest, expected) or not correct_name:
                self._failures[remote] = failures + [now]
                raise LoginError("invalid_credentials")
            self._failures.pop(remote, None)
            if len(self._sessions) >= 128:
                del self._sessions[next(iter(self._sessions))]
            token = "cs_admin_" + secrets.token_urlsafe(32)
            self._sessions[hashlib.sha256(token.encode("utf-8")).digest()] = now + SESSION_TTL
            return token

    def authorized(self, token: str) -> bool:
        with self._lock:
            now = self._clock()
            self._prune(now)
            return hashlib.sha256(token.encode("utf-8")).digest() in self._sessions

    def logout(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(hashlib.sha256(token.encode("utf-8")).digest(), None)
