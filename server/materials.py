from __future__ import annotations

import base64
import binascii
import hashlib
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any

from .gateway import DomainError

MAX_UPLOAD_BODY = 7 * 1024 * 1024
MAX_MATERIAL_SIZE = 5 * 1024 * 1024
MAX_SERVER_FILES = 20
MAX_SERVER_BYTES = 25 * 1024 * 1024
METADATA = 'id,server_id,filename,sha256,size_bytes,created_at'


def migrate(db) -> None:
    db.execute('''CREATE TABLE IF NOT EXISTS server_materials (
        id TEXT PRIMARY KEY, server_id TEXT NOT NULL REFERENCES servers(id),
        filename TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
        created_at TEXT NOT NULL, content BLOB NOT NULL, UNIQUE(server_id,sha256))''')


def decode_upload(payload: Any) -> tuple[str, bytes]:
    if not isinstance(payload, dict) or set(payload) != {'filename', 'content_base64'}:
        raise DomainError('material_invalid_request')
    filename = payload['filename']
    if (not isinstance(filename, str) or not filename or filename != unicodedata.normalize('NFC', filename)
            or len(filename.encode('utf-8')) > 240 or filename.startswith('.')
            or '/' in filename or '\\' in filename
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 or unicodedata.category(c) == 'Cf' for c in filename)
            or '.' not in filename or filename.rsplit('.', 1)[1].lower() not in {'zip', 'md', 'html', 'tsv'}):
        raise DomainError('material_invalid_request')
    encoded = payload['content_base64']
    if not isinstance(encoded, str):
        raise DomainError('material_invalid_request')
    if len(encoded) > ((MAX_MATERIAL_SIZE + 2) // 3) * 4:
        raise DomainError('material_too_large')
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise DomainError('material_invalid_request') from None
    if not content:
        raise DomainError('material_invalid_request')
    if len(content) > MAX_MATERIAL_SIZE:
        raise DomainError('material_too_large')
    if base64.b64encode(content).decode('ascii') != encoded:
        raise DomainError('material_invalid_request')
    return filename, content


class ServerMaterials:
    def __init__(self, db, lock):
        self.db, self.lock = db, lock

    def _server(self, server_id: str) -> None:
        if (not isinstance(server_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', server_id)
                or not self.db.execute('SELECT 1 FROM servers WHERE id=?', (server_id,)).fetchone()):
            raise DomainError('unknown server')

    def list(self, server_id: str) -> list[dict[str, Any]]:
        with self.lock:
            self._server(server_id)
            return [dict(row) for row in self.db.execute(
                'SELECT ' + METADATA + ' FROM server_materials WHERE server_id=? ORDER BY created_at,id', (server_id,))]

    def add(self, server_id: str, payload: Any) -> dict[str, Any]:
        filename, content = decode_upload(payload)
        digest = hashlib.sha256(content).hexdigest()
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._server(server_id)
            old = self.db.execute('SELECT ' + METADATA + ' FROM server_materials WHERE server_id=? AND sha256=?',
                                  (server_id, digest)).fetchone()
            if old:
                return {'item': dict(old), 'duplicate': True}
            count, size = self.db.execute('SELECT COUNT(*),COALESCE(SUM(size_bytes),0) FROM server_materials WHERE server_id=?',
                                         (server_id,)).fetchone()
            if count >= MAX_SERVER_FILES or size + len(content) > MAX_SERVER_BYTES:
                raise DomainError('material_quota_exceeded')
            item = {'id': str(uuid.uuid4()), 'server_id': server_id, 'filename': filename, 'sha256': digest,
                    'size_bytes': len(content), 'created_at': datetime.now(timezone.utc).isoformat()}
            self.db.execute('INSERT INTO server_materials (' + METADATA + ',content) VALUES (?,?,?,?,?,?,?)',
                            (*item.values(), content))
            return {'item': item, 'duplicate': False}

    def download(self, server_id: str, material_id: str) -> tuple[dict[str, Any], bytes]:
        with self.lock:
            self._server(server_id)
            row = self.db.execute('SELECT ' + METADATA + ',content FROM server_materials WHERE server_id=? AND id=?',
                                  (server_id, material_id)).fetchone()
            if row is None:
                raise DomainError('unknown material')
            item = dict(row)
            return {key: item[key] for key in METADATA.split(',')}, bytes(item['content'])
