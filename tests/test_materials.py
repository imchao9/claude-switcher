import base64
import concurrent.futures
import hashlib
import http.client
import json
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from server.gateway import DomainError
from server.materials import MAX_MATERIAL_SIZE, MAX_UPLOAD_BODY, decode_upload
from server.subscription_server import SubscriptionStore, create_server


class MaterialsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'db'
        self.http = create_server(str(self.path), 'fixture-materials-admin', port=0)
        self.store = self.http.store
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        for server in ['one', 'two']:
            self.store.add_server({'id': server, 'name': server, 'host': server + '.example'})

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.store.close()
        self.temp.cleanup()

    @staticmethod
    def upload(content=b'<script>inert</script>', filename='安装指南.html'):
        return {'filename': filename, 'content_base64': base64.b64encode(content).decode()}

    def request(self, method, path, payload=None, auth=True, length=None):
        connection = http.client.HTTPConnection(*self.http.server_address, timeout=5)
        headers = {'Authorization': 'Bearer fixture-materials-admin'} if auth else {}
        if length is not None:
            headers['Content-Length'] = str(length)
        body = json.dumps(payload).encode() if payload is not None else None
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        raw = response.read()
        result = response.status, dict(response.getheaders()), raw
        connection.close()
        return result

    def test_upload_list_download_owner_scope_and_private_reopen(self):
        content = b'<script>never execute</script>\x00\xff'
        status, _, raw = self.request('POST', '/api/servers/one/materials', self.upload(content))
        self.assertEqual(status, 201)
        response = json.loads(raw)
        item = response['item']
        self.assertFalse(response['duplicate'])
        self.assertEqual(set(item), {'id', 'server_id', 'filename', 'sha256', 'size_bytes', 'created_at'})
        self.assertEqual(item['sha256'], hashlib.sha256(content).hexdigest())
        self.assertEqual(item['size_bytes'], len(content))
        status, headers, raw = self.request('GET', '/api/servers/one/materials')
        self.assertEqual(json.loads(raw), {'items': [item]})
        self.assertNotIn(b'content', raw)
        route = '/api/servers/one/materials/' + item['id'] + '/download'
        status, headers, raw = self.request('GET', route)
        self.assertEqual((status, raw), (200, content))
        self.assertEqual(headers['Content-Type'], 'application/octet-stream')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(headers['Content-Security-Policy'], 'sandbox')
        self.assertIn("filename*=UTF-8''%E5%AE%89", headers['Content-Disposition'])
        for path in [route.replace('/one/', '/two/'), route.replace(item['id'], 'missing')]:
            self.assertEqual(self.request('GET', path)[0], 404)
            self.assertEqual(self.request('GET', path, auth=False)[0], 401)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        reopened = SubscriptionStore(self.path)
        try:
            self.assertEqual(reopened.materials.download('one', item['id'])[1], content)
        finally:
            reopened.close()

    def test_auth_precedes_body_read_and_unknown_server_disclosure(self):
        for path in ['/api/servers/one/materials', '/api/servers/missing/materials']:
            self.assertEqual(self.request('POST', path, auth=False, length=MAX_UPLOAD_BODY + 1)[0], 401)
            self.assertEqual(self.request('GET', path, auth=False)[0], 401)
        self.assertEqual(self.request('POST', '/api/servers/one/materials', length=MAX_UPLOAD_BODY + 1)[0], 413)
        self.assertEqual(self.request('GET', '/api/servers/missing/materials')[0], 404)
        self.assertEqual(self.request('POST', '/api/servers/missing/materials', self.upload())[0], 404)

    def test_strict_filename_and_base64_validation_never_echoes_input(self):
        names = ['../secret.md', '/secret.md', 'a\\secret.md', '.hidden.md', '..', 'secret.exe',
                 'secret.md\r\nX-Test:bad', 's\u0085ecret.md', 's\u202eecret.md', 's\u200becret.md',
                 's\u2066ecret.md', 'x' * 238 + '.md', 'secre\u0301t.md']
        malformed = [self.upload(filename=name) for name in names]
        malformed.extend([dict(self.upload(), extra='secret')])
        malformed.extend({'filename': 'secret.md', 'content_base64': value}
                         for value in ['', 'A===', 'eA==\n', 'eB==', '秘密', False, [], None])
        for payload in malformed:
            with self.subTest(payload=repr(payload)[:80]):
                status, _, raw = self.request('POST', '/api/servers/one/materials', payload)
                self.assertEqual(status, 400)
                self.assertNotIn(b'secret', raw)
        self.assertEqual(self.store.materials.list('one'), [])

    def test_decoded_size_default_body_limit_and_upload_override(self):
        self.assertEqual(self.request('POST', '/api/servers', {'invalid': 'x' * 65536})[0], 400)
        content = b'x' * 65536
        self.assertEqual(self.request('POST', '/api/servers/one/materials', self.upload(content))[0], 201)
        payload = self.upload(b'x' * (MAX_MATERIAL_SIZE + 1))
        self.assertEqual(self.request('POST', '/api/servers/one/materials', payload)[0], 413)
        self.assertEqual(len(self.store.materials.list('one')), 1)
        filename, content = decode_upload(self.upload(b'x' * MAX_MATERIAL_SIZE, 'a.zip'))
        self.assertEqual(len(content), MAX_MATERIAL_SIZE)

    def test_duplicate_at_file_quota_returns_original_metadata(self):
        first = self.store.materials.add('one', self.upload(b'first', 'original.md'))
        for number in range(19):
            self.store.materials.add('one', self.upload(str(number).encode(), f'{number}.tsv'))
        duplicate = self.store.materials.add('one', self.upload(b'first', 'renamed.zip'))
        self.assertEqual(duplicate, {'item': first['item'], 'duplicate': True})
        with self.assertRaisesRegex(DomainError, '^material_quota_exceeded$'):
            self.store.materials.add('one', self.upload(b'extra'))
        self.assertEqual(len(self.store.materials.list('one')), 20)
        self.assertFalse(self.store.materials.add('two', self.upload(b'first'))['duplicate'])

    def test_byte_quota_and_dedup_full(self):
        with patch('server.materials.MAX_SERVER_BYTES', 10):
            first = self.store.materials.add('one', self.upload(b'12345'))
            self.store.materials.add('one', self.upload(b'67890'))
            self.assertTrue(self.store.materials.add('one', self.upload(b'12345'))['duplicate'])
            with self.assertRaisesRegex(DomainError, '^material_quota_exceeded$'):
                self.store.materials.add('one', self.upload(b'X'))
            self.assertEqual(self.store.materials.list('one')[0]['id'], first['item']['id'])

    def test_concurrent_distinct_connections_cannot_exceed_quota_or_duplicate_hash(self):
        other = SubscriptionStore(self.path)
        try:
            for number in range(19):
                self.store.materials.add('one', self.upload(str(number).encode()))
            barrier = threading.Barrier(2)
            def add(store, content):
                barrier.wait()
                try:
                    return store.materials.add('one', self.upload(content))
                except DomainError as error:
                    return str(error)
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda pair: add(*pair), [(self.store, b'A'), (other, b'B')]))
            self.assertEqual(sum(isinstance(item, dict) for item in results), 1)
            self.assertIn('material_quota_exceeded', results)
            self.assertEqual(len(self.store.materials.list('one')), 20)
            barrier = threading.Barrier(2)
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda pair: add(*pair), [(self.store, b'same'), (other, b'same')]))
            self.assertEqual(results, ['material_quota_exceeded'] * 2)
        finally:
            other.close()

    def test_concurrent_same_hash_has_one_item(self):
        other = SubscriptionStore(self.path)
        try:
            barrier = threading.Barrier(2)
            def add(store):
                barrier.wait()
                return store.materials.add('one', self.upload(b'same'))
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                results = list(pool.map(add, [self.store, other]))
            self.assertEqual(sorted(item['duplicate'] for item in results), [False, True])
            self.assertEqual(results[0]['item'], results[1]['item'])
        finally:
            other.close()


if __name__ == '__main__':
    unittest.main()
