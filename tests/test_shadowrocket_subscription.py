"""HTTP and delivery behavior for Shadowrocket-compatible node subscriptions."""

import base64
import http.client
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit
import uuid

from server.subscription_server import create_server


class ShadowrocketSubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.http = create_server(str(Path(self.temp.name) / 'db'), 'fixture-admin-token', port=0)
        self.store = self.http.store
        self.store.add_server({'id': 'edge', 'name': 'Edge', 'host': 'edge.example', 'gateway': {
            'kind': 'xray-vless-reality', 'port': 443, 'server_name': 'sni.example',
            'public_key': base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip('='),
            'short_id': '12ab', 'inbound_tag': 'reality', 'flow': 'xtls-rprx-vision',
        }})
        for identifier, address in [('one', '8.8.8.8'), ('two', '1.1.1.1')]:
            self.store.add_ip({'id': identifier, 'server_id': 'edge', 'address': address,
                               'port': 1080, 'protocol': 'socks5',
                               'config': {'username': identifier + '-private-user',
                                          'password': identifier + '-private-password'}})
        self.created = self.store.create_user({'id': 'alice', 'ip_id': 'one'}, 'https://sub.example')
        self.other = self.store.create_user({'id': 'bob', 'ip_id': 'two'}, 'https://sub.example')
        self.path = urlsplit(self.created['subscription_url']).path
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.store.close()
        self.temp.cleanup()

    def request(self, path, user_agent=None):
        connection = http.client.HTTPConnection(*self.http.server_address, timeout=5)
        connection.request('GET', path, headers={} if user_agent is None else {'User-Agent': user_agent})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def assert_subscription_headers(self, headers):
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['Vary'], 'User-Agent')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')

    def decoded_uri(self, body):
        uri = base64.b64decode(body.strip(), validate=True).decode()
        self.assertEqual(len(uri.splitlines()), 1)
        parsed = urlsplit(uri)
        self.assertEqual(parsed.scheme, 'vless')
        self.assertEqual(parsed.hostname, 'edge.example')
        self.assertEqual(parsed.port, 443)
        self.assertEqual(str(uuid.UUID(parsed.username)), parsed.username)
        self.assertEqual(parse_qs(parsed.query), {
            'encryption': ['none'], 'security': ['reality'], 'sni': ['sni.example'],
            'type': ['tcp'], 'allowInsecure': ['0'], 'udp': ['false'],
            'pbk': [base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip('=')],
            'sid': ['12ab'], 'flow': ['xtls-rprx-vision'], 'fp': ['chrome'],
        })
        self.assertEqual(parsed.fragment, 'alice-gateway')
        for secret in ['8.8.8.8', '1.1.1.1', 'one-private-user', 'one-private-password',
                       'two-private-user', 'two-private-password', self.created['token'], self.other['token']]:
            self.assertNotIn(secret, uri)
        return uri

    def test_same_canonical_url_auto_detects_shadowrocket_case_insensitively(self):
        for user_agent in ['Shadowrocket/2.2.70', 'shadowrocket', 'SHADOWROCKET iOS/18']:
            with self.subTest(user_agent=user_agent):
                status, headers, body = self.request(self.path, user_agent)
                self.assertEqual(status, 200)
                self.assertEqual(headers['Content-Type'], 'text/plain; charset=utf-8')
                self.assert_subscription_headers(headers)
                self.decoded_uri(body)
        for user_agent in [None, 'Mozilla/5.0', 'Clash Verge', 'Mihomo/1.19']:
            with self.subTest(user_agent=user_agent):
                status, headers, body = self.request(self.path, user_agent)
                self.assertEqual(status, 200)
                self.assertEqual(headers['Content-Type'], 'text/yaml; charset=utf-8')
                self.assertIn(b'proxies:', body)
                self.assert_subscription_headers(headers)

    def test_explicit_formats_override_user_agent_and_alias_reuses_identity(self):
        _, _, automatic = self.request(self.path, 'Shadowrocket/2.2.70')
        uri = self.decoded_uri(automatic)
        for user_agent in ['Shadowrocket/2.2.70', 'Clash Verge']:
            for fmt in ['clash', 'uri', 'base64', 'shadowrocket']:
                with self.subTest(user_agent=user_agent, fmt=fmt):
                    status, headers, body = self.request(self.path + '?format=' + fmt, user_agent)
                    self.assertEqual(status, 200)
                    self.assert_subscription_headers(headers)
                    if fmt == 'clash':
                        self.assertEqual(headers['Content-Type'], 'text/yaml; charset=utf-8')
                        self.assertIn(('uuid: "' + urlsplit(uri).username + '"').encode(), body)
                    elif fmt == 'uri':
                        self.assertEqual(body.decode().strip(), uri)
                    else:
                        self.assertEqual(body, automatic)
        _, _, other = self.request(urlsplit(self.other['subscription_url']).path, 'Shadowrocket')
        self.assertNotEqual(urlsplit(base64.b64decode(other).decode()).username, urlsplit(uri).username)
        self.assertEqual(self.store.get_user('alice')['ip_id'], 'one')
        self.assertEqual(self.store.get_user('bob')['ip_id'], 'two')

    def test_invalid_format_cannot_fall_back_to_default(self):
        queries = ['format=', 'format', 'format=uri&format=base64', 'format=clash&format=clash',
                   'format=unknown', 'format=Shadowrocket', 'format=%20', 'format=%FF',
                   'format=%', 'format=%GG', 'format=uri&%66ormat=clash']
        for user_agent in ['Shadowrocket', 'Clash Verge']:
            for query in queries:
                with self.subTest(user_agent=user_agent, query=query):
                    status, headers, body = self.request(self.path + '?' + query, user_agent)
                    self.assertEqual(status, 400)
                    self.assertEqual(json.loads(body), {'error': 'invalid subscription format'})
                    self.assert_subscription_headers(headers)
                    self.assertNotIn(self.created['token'].encode(), body)

    def test_legacy_route_and_revocation_apply_to_every_format(self):
        legacy = '/s/' + self.created['token']
        for suffix in ['', '?format=clash', '?format=uri', '?format=base64', '?format=shadowrocket']:
            with self.subTest(suffix=suffix):
                canonical = self.request(self.path + suffix, 'Shadowrocket')
                previous = self.request(legacy + suffix, 'Shadowrocket')
                self.assertEqual(canonical[0], 200)
                self.assertEqual((canonical[0], canonical[2]), (previous[0], previous[2]))
                self.assert_subscription_headers(previous[1])
        replacement = self.store.rotate_token('alice', 'https://sub.example')
        self.assertNotEqual(replacement['token'], self.created['token'])
        for path in [self.path, legacy]:
            for suffix in ['', '?format=clash', '?format=uri', '?format=base64', '?format=shadowrocket']:
                with self.subTest(path=path, suffix=suffix):
                    status, headers, body = self.request(path + suffix, 'Shadowrocket')
                    self.assertEqual(status, 404)
                    self.assertEqual(json.loads(body), {'error': 'subscription unavailable'})
                    self.assert_subscription_headers(headers)
        current = urlsplit(replacement['subscription_url']).path
        self.assertEqual(self.request(current, 'Shadowrocket')[0], 200)
        self.store.disable('alice')
        status, headers, _ = self.request(current, 'Shadowrocket')
        self.assertEqual(status, 404)
        self.assert_subscription_headers(headers)


class ShadowrocketDeliveryTests(unittest.TestCase):
    def test_admin_markup_syntax_and_copy_session_guards(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js required to execute delivery JavaScript')
        html = Path(__file__).resolve().parents[1].joinpath('server/admin.html').read_text()
        scripts = re.findall(r'<script>(.*?)</script>', html, re.S)
        self.assertEqual(len(scripts), 1)
        checked = subprocess.run([node, '--check'], input=scripts[0], text=True, capture_output=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("['shadowrocket','Shadowrocket（小火箭）','shadowrocket_url']", html)
        self.assertIn("()=>copySubscription(item,format)", html)
        self.assertIn('Global Routing（全局路由）设为 Proxy（代理）', html)
        self.assertIn('节点订阅不会配置小火箭的全局路由、UDP 或 DNS', html)
        functions = scripts[0].split('    const readySubscriptions = ', 1)[1].split('    function emptyState(', 1)[0]
        program = '''
const assert=require('node:assert/strict');
let session='session-one',generatedSubscriptions=[],writes=[],loadGeneration=0;
const token=()=>session,subscriptionMessage={textContent:''};
const sessionSnapshot=()=>({token:session}),sessionCurrent=snapshot=>Boolean(snapshot.token&&snapshot.token===session);
const navigator={clipboard:{writeText:async value=>writes.push(value)}};
const readySubscriptions = ''' + functions + '''
(async()=>{
  const item={user_id:'fixture',token_state:'stored',delivery_state:'ready',clash_url:'https://sub.example/sub/cs_fixture/edge.yaml?format=clash',shadowrocket_url:'https://sub.example/sub/cs_fixture/edge.yaml?format=shadowrocket'};
  generatedSubscriptions=[item];
  await copySubscription(item);
  assert.equal(writes[0],item.clash_url);
  await copySubscription(item,'shadowrocket');
  const fallback=new URL(writes[1]);
  assert.equal(fallback.searchParams.get('format'),'shadowrocket');
  assert.equal(fallback.searchParams.size,1);
  assert.equal(fallback.pathname,new URL(item.clash_url).pathname);
  const stale={...item,clash_url:item.clash_url+'-old'};
  await copySubscription(stale,'shadowrocket');assert.equal(writes.length,2);
  generatedSubscriptions=[];
  await copySubscription(item,'shadowrocket');assert.equal(writes.length,2);
  session='';generatedSubscriptions=[item];
  await copySubscription(item,'shadowrocket');await copySubscriptions('stale');assert.equal(writes.length,2);
  session='session-two';generatedSubscriptions=[item];
  let finish;subscriptionMessage.textContent='';
  navigator.clipboard.writeText=()=>new Promise(resolve=>finish=resolve);
  const pending=copySubscription(item,'shadowrocket');
  session='';generatedSubscriptions=[];finish();await pending;
  assert.equal(subscriptionMessage.textContent,'');
})().catch(error=>{process.stderr.write(error.stack);process.exitCode=1;});
'''
        result = subprocess.run([node], input=program, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
