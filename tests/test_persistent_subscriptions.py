import base64
import http.client
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from server.subscription_server import create_server, token_hash, token_hint


class PersistentSubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'private.db')
        self.admin = 'synthetic-persistence-admin'
        self.origin = 'https://delivery.example'
        self.start()
        self.server.store.add_server({'id': 'edge', 'name': 'Synthetic Edge', 'host': 'edge.example',
            'gateway': {'kind': 'xray-vless-tls', 'port': 443, 'server_name': 'edge.example',
                        'certificate_file': '/synthetic/cert.pem', 'key_file': '/synthetic/key.pem'}})
        self.server.store.add_ip({'id': 'ip', 'address': '8.8.8.8', 'port': 1080,
            'server_id': 'edge', 'protocol': 'socks5', 'config': {'username': 'fixture', 'password': 'synthetic'}})
        self.created = self.request('POST', '/api/users', {'id': 'user', 'name': 'Synthetic User', 'ip_id': 'ip'})[2]

    def start(self):
        self.server = create_server(self.path, self.admin, port=0, base_url=self.origin)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.store.close()
        self.thread.join()

    def tearDown(self):
        self.stop()
        self.temp.cleanup()

    def request(self, method, path, payload=None, auth=True, headers=None, raw=False):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        request_headers = {'Content-Type': 'application/json'}
        if auth:
            request_headers['Authorization'] = 'Bearer ' + self.admin
        request_headers.update(headers or {})
        connection.request(method, path, None if payload is None else json.dumps(payload), request_headers)
        response = connection.getresponse()
        body = response.read()
        result = response.status, dict(response.getheaders()), body if raw else json.loads(body)
        connection.close()
        return result

    def deliveries(self):
        status, headers, body = self.request('GET', '/api/subscriptions')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        return body['items']

    def dump(self):
        return tuple(self.server.store.db.iterdump())

    def legacy(self):
        with self.server.store.db:
            self.server.store.db.execute("UPDATE users SET subscription_token=NULL WHERE id='user'")

    def test_create_restart_formats_and_secret_free_public_projections(self):
        before = self.deliveries()
        self.assertEqual((before[0]['token_state'], before[0]['delivery_state']), ('stored', 'ready'))
        self.stop()
        self.start()
        self.assertEqual(self.deliveries(), before)
        item = before[0]
        clash = self.request('GET', urlsplit(item['clash_url']).path + '?format=clash', auth=False,
                             headers={'User-Agent': 'Shadowrocket'}, raw=True)
        rocket = self.request('GET', urlsplit(item['shadowrocket_url']).path + '?format=shadowrocket', auth=False, raw=True)
        self.assertEqual((clash[0], rocket[0]), (200, 200))
        self.assertIn(b'proxies:', clash[2])
        self.assertTrue(base64.b64decode(rocket[2]).startswith(b'vless://'))
        for path in ['/api/users', '/api/ips', '/api/servers', '/api/servers/edge/xray-config']:
            raw = json.dumps(self.request('GET', path)[2])
            self.assertNotIn(self.created['token'], raw)
            self.assertNotIn('subscription_token', raw)
            self.assertNotIn('clash_url', raw)
        self.assertEqual(Path(self.path).stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(self.path).parent.stat().st_mode & 0o777, 0o700)

    def test_migration_keeps_every_existing_field_and_legacy_url(self):
        self.stop()
        db = sqlite3.connect(self.path)
        db.execute('ALTER TABLE users DROP COLUMN subscription_token')
        before = db.execute('SELECT * FROM users').fetchall()
        bindings = db.execute('SELECT * FROM bindings').fetchall()
        db.commit()
        db.close()
        self.start()
        columns = [row['name'] for row in self.server.store.db.execute('PRAGMA table_info(users)')]
        old_columns = ','.join(name for name in columns if name != 'subscription_token')
        after = [tuple(row) for row in self.server.store.db.execute('SELECT ' + old_columns + ' FROM users')]
        self.assertEqual(after, before)
        self.assertEqual([tuple(row) for row in self.server.store.db.execute('SELECT * FROM bindings')], bindings)
        item = self.deliveries()[0]
        self.assertEqual((item['token_state'], item['delivery_state']), ('legacy', 'unavailable'))
        self.assertFalse({'subscription_url', 'clash_url', 'shadowrocket_url'} & item.keys())
        self.assertEqual(self.request('GET', urlsplit(self.created['subscription_url']).path, auth=False, raw=True)[0], 200)

    def test_restore_legacy_preserves_token_identity_times_binding_and_is_idempotent(self):
        self.legacy()
        before = dict(self.server.store.db.execute("SELECT * FROM users WHERE id='user'").fetchone())
        binding = tuple(self.server.store.db.execute('SELECT * FROM bindings').fetchone())
        for suffix in ['', '?format=clash', '?format=shadowrocket', '?format=uri', '?format=base64']:
            status, _, _ = self.request('POST', '/api/subscriptions/convert', {'url': self.created['subscription_url'] + suffix})
            self.assertEqual(status, 200)
            after = dict(self.server.store.db.execute("SELECT * FROM users WHERE id='user'").fetchone())
            self.assertEqual({k: v for k, v in after.items() if k != 'subscription_token'},
                             {k: v for k, v in before.items() if k != 'subscription_token'})
            self.assertEqual(after['subscription_token'], self.created['token'])
            self.assertEqual(tuple(self.server.store.db.execute('SELECT * FROM bindings').fetchone()), binding)
            saved = self.dump()
            self.request('POST', '/api/subscriptions/convert', {'url': self.created['subscription_url'] + suffix})
            self.assertEqual(self.dump(), saved)
        self.stop()
        self.start()
        self.assertEqual(self.deliveries()[0]['delivery_state'], 'ready')

    def test_import_persists_and_duplicate_or_invalid_batch_does_not_change_database(self):
        payload = {'server_id': 'edge', 'protocol': 'socks5', 'dry_run': False,
                   'create_subscriptions': True, 'text': '1.1.1.1:1080:fixture:synthetic'}
        status, _, data = self.request('POST', '/api/ips/import', payload)
        self.assertEqual((status, data['imported']), (200, 1))
        before = self.dump()
        self.assertEqual(self.request('POST', '/api/ips/import', payload)[2]['subscriptions'], [])
        self.assertEqual(self.dump(), before)
        payload['text'] += '\nbad line'
        self.assertFalse(self.request('POST', '/api/ips/import', payload)[2]['valid'])
        self.assertEqual(self.dump(), before)
        self.stop()
        self.start()
        self.assertEqual(len([item for item in self.deliveries() if item['delivery_state'] == 'ready']), 2)

    def test_rotation_persists_new_token_and_revokes_both_old_formats(self):
        old = self.deliveries()[0]
        rotated = self.request('POST', '/api/users/user/rotate-token', {})[2]
        new = self.deliveries()[0]
        self.assertNotEqual(old['clash_url'], new['clash_url'])
        self.assertIn(rotated['token'], new['shadowrocket_url'])
        for field in ['clash_url', 'shadowrocket_url']:
            parsed = urlsplit(old[field])
            self.assertEqual(self.request('GET', parsed.path + '?' + parsed.query, auth=False, raw=True)[0], 404)
        before = self.dump()
        self.assertEqual(self.request('POST', '/api/subscriptions/convert', {'url': old['clash_url']})[0], 400)
        self.assertEqual(self.dump(), before)
        self.stop()
        self.start()
        self.assertEqual(self.deliveries()[0], new)

    def test_auth_transport_and_unavailable_never_reveal_links(self):
        self.assertEqual(self.request('GET', '/api/subscriptions', auth=False)[0], 401)
        self.assertEqual(self.request('GET', '/api/subscriptions', headers={'Host': 'untrusted.example'})[0], 403)
        status, headers, _ = self.request('GET', '/api/subscriptions', headers={'Host': 'delivery.example'})
        self.assertEqual(status, 403)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        with self.server.store.db:
            self.server.store.db.execute("UPDATE ips SET status='disabled' WHERE id='ip'")
        item = self.deliveries()[0]
        self.assertEqual((item['token_state'], item['delivery_state']), ('stored', 'unavailable'))
        self.assertFalse({'subscription_url', 'clash_url', 'shadowrocket_url'} & item.keys())
        before = self.dump()
        self.assertEqual(self.request('POST', '/api/subscriptions/convert', {'url': self.created['subscription_url']})[0], 400)
        self.assertEqual(self.dump(), before)

    def test_delivery_listing_uses_current_renderer_eligibility(self):
        cases = [
            ("UPDATE users SET status='disabled' WHERE id='user'", "UPDATE users SET status='active' WHERE id='user'"),
            ("UPDATE servers SET status='disabled' WHERE id='edge'", "UPDATE servers SET status='active' WHERE id='edge'"),
            ("UPDATE servers SET gateway_json=NULL WHERE id='edge'", None),
            ("UPDATE servers SET gateway_json='{}' WHERE id='edge'", None),
            ("UPDATE ips SET protocol='vless' WHERE id='ip'", "UPDATE ips SET protocol='socks5' WHERE id='ip'"),
        ]
        gateway = self.server.store.db.execute("SELECT gateway_json FROM servers WHERE id='edge'").fetchone()[0]
        for change, restore in cases:
            with self.server.store.db:
                self.server.store.db.execute(change)
            item = self.deliveries()[0]
            self.assertEqual(item['delivery_state'], 'unavailable')
            self.assertFalse({'subscription_url', 'clash_url', 'shadowrocket_url'} & item.keys())
            with self.server.store.db:
                if restore:
                    self.server.store.db.execute(restore)
                else:
                    self.server.store.db.execute("UPDATE servers SET gateway_json=? WHERE id='edge'", (gateway,))
        self.assertEqual(self.deliveries()[0]['delivery_state'], 'ready')
        self.request('PUT', '/api/users/user/binding', {'ip_id': None})
        self.assertEqual(self.deliveries()[0]['delivery_state'], 'unavailable')

    def test_invalid_stored_token_and_bad_query_recovery_fail_closed(self):
        with self.server.store.db:
            self.server.store.db.execute("UPDATE users SET subscription_token='cs_invalid-fixture' WHERE id='user'")
        item = self.deliveries()[0]
        self.assertEqual((item['token_state'], item['delivery_state']), ('invalid', 'unavailable'))
        for suffix in ['?', '?format=clash&x=1', '?x=1', '?format=clash&format=clash',
                       '?format=', '?format=%63lash', '?format=Clash', '?format=uri#fragment']:
            before = self.dump()
            self.assertEqual(self.request('POST', '/api/subscriptions/convert', {'url': self.created['subscription_url'] + suffix})[0], 400)
            self.assertEqual(self.dump(), before)

    def test_listing_is_one_cross_connection_snapshot_and_does_not_commit_callers(self):
        store = self.server.store
        original = store.list_users
        wrote = threading.Event()
        errors = []
        replacement = 'cs_synthetic-snapshot-replacement'
        def rotate_elsewhere():
            try:
                db = sqlite3.connect(self.path, timeout=5)
                db.execute('UPDATE users SET name=?,token_hash=?,token_hint=?,subscription_token=? WHERE id=?',
                           ('New Name', token_hash(replacement), token_hint(replacement), replacement, 'user'))
                wrote.set()
                db.commit()
                db.close()
            except Exception as error:
                errors.append(error)
                wrote.set()
        writer = threading.Thread(target=rotate_elsewhere)
        def snapshot_users():
            result = original()
            writer.start()
            self.assertTrue(wrote.wait(3))
            return result
        with patch.object(store, 'list_users', side_effect=snapshot_users):
            item = self.deliveries()[0]
        writer.join(5)
        self.assertFalse(writer.is_alive())
        self.assertFalse(errors)
        self.assertEqual(item['name'], 'Synthetic User')
        self.assertIn(self.created['token'], item['clash_url'])
        current = self.deliveries()[0]
        self.assertEqual(current['name'], 'New Name')
        self.assertIn(replacement, current['clash_url'])
        with store._lock:
            store.db.execute('BEGIN')
            store.db.execute("UPDATE users SET name='Uncommitted Name' WHERE id='user'")
            self.assertEqual(store.list_subscriptions(self.origin)[0]['name'], 'Uncommitted Name')
            self.assertTrue(store.db.in_transaction)
            store.db.rollback()
        self.assertEqual(self.deliveries()[0]['name'], 'New Name')

    def test_recovery_waiting_for_other_connection_rotation_cannot_restore_stale_token(self):
        self.legacy()
        writer = sqlite3.connect(self.path)
        writer.execute('BEGIN IMMEDIATE')
        replacement = 'cs_synthetic-replacement-after-rotation'
        writer.execute('UPDATE users SET token_hash=?,token_hint=?,subscription_token=? WHERE id=?',
                       (token_hash(replacement), token_hint(replacement), replacement, 'user'))
        entered = threading.Event()
        original = self.server.store.convert_subscription
        def observe(*args):
            entered.set()
            return original(*args)
        result = []
        with patch.object(self.server.store, 'convert_subscription', side_effect=observe):
            thread = threading.Thread(target=lambda: result.append(self.request('POST', '/api/subscriptions/convert', {'url': self.created['subscription_url']})))
            thread.start()
            self.assertTrue(entered.wait(3))
            writer.commit()
            writer.close()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0][0], 400)
        self.assertEqual(self.server.store.db.execute("SELECT subscription_token FROM users WHERE id='user'").fetchone()[0], replacement)


class PersistentDeliveryJavaScriptTests(unittest.TestCase):
    def test_real_script_load_races_logout_mutations_and_dual_copy(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js required for executable delivery guards')
        html = Path(__file__).resolve().parents[1].joinpath('server/admin.html').read_text()
        self.assertNotIn('localStorage', html)
        self.assertNotIn('sessionStorage', html)
        script = re.findall(r'<script>(.*?)</script>', html, re.S)[0]
        harness = r'''
const assert=require('node:assert/strict'),vm=require('node:vm');
class Element {
 constructor(tag='div'){this.tagName=tag;this.children=[];this.listeners={};this.value='';this.textContent='';this.style={};this.dataset={};this.classList={toggle(){},contains(){return false}};this.open=false;this.values={};}
 append(...children){this.children.push(...children)}
 replaceChildren(...children){this.children=children;this.textContent=''}
 set innerHTML(value){this._html=value;this.children=[]}get innerHTML(){return this._html||''}
 setAttribute(name,value){this[name]=value}removeAttribute(name){delete this[name]}
 addEventListener(name,callback){this.listeners[name]=callback}
 querySelector(){return new Element()}querySelectorAll(){return []}
 insertRow(){const row=new Element('tr');this.append(row);return row}
 insertCell(){const cell=new Element('td');this.append(cell);return cell}
 reset(){this.value=''}focus(){}add(){}showModal(){this.open=true}close(){this.open=false}remove(){}
}
const elements=new Map(),get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id)},requests=[],writes=[];
const context={console,process,assert,requests,writes,URL,Map,Set,Date,JSON,Object,Array,String,Boolean,Number,RegExp,Promise,Uint8Array,AbortController,
 document:{getElementById:get,createElement:tag=>new Element(tag),querySelectorAll:()=>[],addEventListener(){},body:new Element(),hidden:false},
 window:{addEventListener(){},scrollTo(){}},matchMedia:()=>({matches:false,addEventListener(){}}),
 setTimeout:()=>1,clearTimeout(){},navigator:{clipboard:{writeText:async text=>writes.push(text)}},
 FormData:class{constructor(form){this.form=form}entries(){return Object.entries(this.form.values)}},
 fetch:(path,options)=>new Promise(resolve=>requests.push({path,options,resolve})),Option:Element};
vm.createContext(context);
'''
        assertions = r'''
(async()=>{
 const tick=async()=>{for(let i=0;i<12;i++)await Promise.resolve()};
 const respond=(index,data,status=200)=>requests[index].resolve({ok:status>=200&&status<300,status,json:async()=>data});
 const ready=label=>({user_id:'u',name:'Synthetic User',address:'8.8.8.8',server_id:'edge',token_state:'stored',delivery_state:'ready',clash_url:'https://synthetic.example/'+label+'?format=clash',shadowrocket_url:'https://synthetic.example/'+label+'?format=shadowrocket'});
 const signIn=()=>{authGeneration++;authState={phase:'signed-in',token:'same-session',username:'synthetic'}};
 const user={id:'u',status:'active',ip_id:'ip'},ip={id:'ip',status:'available',protocol:'socks5',server_id:'edge',address:'8.8.8.8'},server={id:'edge',name:'Edge',host:'synthetic.example',status:'active',gateway_ready:true};
 const finishLoad=(offset,item)=>{respond(offset,{items:[ip]});respond(offset+1,{items:[user]});respond(offset+2,{items:[server]});respond(offset+3,{items:[item]})};
 signIn();
 const older=load(),newer=load();finishLoad(4,ready('newer'));await tick();assert.equal(await newer,true);
 finishLoad(0,ready('older'));await tick();assert.equal(await older,false);assert.equal(generatedSubscriptions[0].clash_url,ready('newer').clash_url);
 let item=generatedSubscriptions[0];await copySubscription(item);await copySubscription(item,'shadowrocket');assert.equal(JSON.stringify(writes.slice(0,2)),JSON.stringify([item.clash_url,item.shadowrocket_url]));
 await byId('copy-subscriptions').listeners.click();assert.ok(writes[2].includes('Synthetic User'));assert.ok(writes[2].includes(item.clash_url));assert.ok(writes[2].includes(item.shadowrocket_url));
 const descendants=node=>[...node.children,...node.children.flatMap(descendants)];
 const inputs=descendants(byId('subscription-links')).filter(node=>node.tagName==='input');assert.equal(inputs.length,2);assert.ok(inputs.every(input=>input.type==='password'));assert.ok(inputs[0]['aria-label'].includes('Clash'));assert.ok(inputs[1]['aria-label'].includes('Shadowrocket'));
 const reveal=descendants(byId('subscription-links')).find(node=>node['aria-pressed']==='false');reveal.listeners.click();assert.equal(inputs[0].type,'text');reveal.listeners.click();assert.equal(inputs[0].type,'password');
 byId('subscription-search').value='nonexistent';renderSubscriptions();assert.equal(descendants(byId('subscription-links')).filter(node=>node.tagName==='input').length,0);byId('subscription-search').value='edge';renderSubscriptions();assert.equal(descendants(byId('subscription-links')).filter(node=>node.tagName==='input').length,2);byId('subscription-search').value='';
 generatedSubscriptions=[{user_id:'legacy',token_state:'legacy',delivery_state:'unavailable'},{user_id:'off',token_state:'stored',delivery_state:'unavailable'}];renderSubscriptions();assert.equal(descendants(byId('subscription-links')).filter(node=>node.tagName==='input').length,0);assert.equal(byId('copy-subscriptions').disabled,true);await copySubscription(item);assert.equal(writes.length,3);
 generatedSubscriptions=[ready('before')];renderSubscriptions();let offset=requests.length,pending=load();signedOut();signIn();finishLoad(offset,ready('late'));await tick();await pending;assert.equal(generatedSubscriptions.length,0);assert.equal(byId('subscription-links').children.length,0);
 offset=requests.length;pending=load();respond(offset,{error:'admin_auth_required'},401);await tick();assert.equal(token(),'');assert.equal(generatedSubscriptions.length,0);for(let i=1;i<4;i++)respond(offset+i,{items:[]});await pending;
 signIn();ips=[ip];users=[user];servers=[server];generatedSubscriptions=[ready('current')];renderSubscriptions();
 offset=requests.length;pending=rotateToken('u');signedOut();signIn();respond(offset,{user,subscription_url:'https://synthetic.example/stale'});await tick();await pending;assert.equal(requests.length,offset+1);assert.equal(generatedSubscriptions.length,0);
 ips=[ip];users=[user];servers=[server];byId('batch-server').value='edge';batchProtocol.value='socks5';batchText.value='1.1.1.1:1080:fixture:synthetic';offset=requests.length;pending=submitBatch(true);assert.equal(batchText.readOnly,true);assert.equal(batchProtocol.disabled,true);signedOut();assert.equal(batchText.readOnly,false);assert.equal(batchProtocol.disabled,false);signIn();const reloginOffset=requests.length,reloaded=load();finishLoad(reloginOffset,ready('after-batch-logout'));await tick();await reloaded;assert.equal(byId('batch-server').disabled,false);byId('batch-server').value='edge';byId('batch-server').listeners.change();assert.equal(batchPreview.disabled,false);respond(offset,{valid:true,dry_run:true,rows:[],subscriptions:[]});await tick();await pending;assert.equal(batchText.readOnly,false);assert.equal(batchProtocol.disabled,false);assert.equal(batchPreview.disabled,false);assert.equal(generatedSubscriptions[0].clash_url,ready('after-batch-logout').clash_url);
 ips=[ip];users=[user];servers=[server];const form=byId('user-form');form.values={id:'u',name:'Synthetic User',ip_id:'ip'};offset=requests.length;pending=form.listeners.submit({preventDefault(){},target:form});respond(offset,{user,subscription_url:'https://synthetic.example/stale'});await tick();finishLoad(offset+1,ready('authoritative'));await tick();await pending;assert.equal(generatedSubscriptions[0].clash_url,ready('authoritative').clash_url);
 offset=requests.length;pending=rotateToken('u');respond(offset,{user,subscription_url:'https://synthetic.example/stale-rotation'});await tick();finishLoad(offset+1,ready('rotated'));await tick();await pending;assert.equal(generatedSubscriptions[0].clash_url,ready('rotated').clash_url);
 offset=requests.length;const obsoleteLoad=load();assert.equal(byId('load').disabled,true);pending=rotateToken('u');respond(offset+4,{error:'synthetic_rotation_failed'},500);await tick();await pending;finishLoad(offset,ready('obsolete'));await tick();await obsoleteLoad;assert.equal(byId('load').disabled,false);assert.equal(generatedSubscriptions.length,0);
 byId('batch-server').value='edge';batchProtocol.value='socks5';batchText.value='1.1.1.1:1080:fixture:synthetic';batchState='ready';batchFingerprint=currentBatchFingerprint();offset=requests.length;pending=submitBatch(false);respond(offset,{valid:true,dry_run:false,rows:[],total:1,imported:1,skipped:0,subscriptions:[{user_id:'u',subscription_url:'https://synthetic.example/stale-import'}]});await tick();finishLoad(offset+1,ready('imported'));await tick();await pending;assert.equal(generatedSubscriptions[0].clash_url,ready('imported').clash_url);
 byId('convert-subscription-dialog').open=true;byId('convert-subscription-url').value='https://synthetic.example/old';offset=requests.length;pending=byId('convert-subscription-form').listeners.submit({preventDefault(){}});respond(offset,{user,subscription_url:'https://synthetic.example/stale-conversion'});await tick();finishLoad(offset+1,ready('restored'));await tick();await pending;assert.equal(generatedSubscriptions[0].clash_url,ready('restored').clash_url);
 byId('convert-subscription-dialog').open=true;byId('convert-subscription-url').value='https://synthetic.example/old';offset=requests.length;pending=byId('convert-subscription-form').listeners.submit({preventDefault(){}});signedOut();signIn();respond(offset,{user,subscription_url:'https://synthetic.example/stale-conversion'});await tick();await pending;assert.equal(requests.length,offset+1);assert.equal(generatedSubscriptions.length,0);
 process.stdout.write('delivery-guards-complete');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        program = harness + '\nvm.runInContext(' + json.dumps(script + assertions) + ',context);\n'
        result = subprocess.run([node], input=program, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'delivery-guards-complete')
