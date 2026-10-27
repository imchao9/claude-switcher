from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from server.gateway import validate_gateway
from tests.test_reality_gateway import REALITY


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'server/admin.html').read_text()
NODE = shutil.which('node')
WS = {'address': 'backup.example', 'port': 8443, 'server_name': 'tls.example',
      'host': 'websocket.example', 'path': '/socket?ed=2048&mode=%2F',
      'inbound_tag': 'managed-ws', 'tls_termination': 'external'}
PROFILE = {'id': 'fixture-server', 'name': 'Fixture server', 'host': 'primary.example',
           'region': '', 'status': 'active', 'gateway': REALITY}


class Controls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.controls = {}
        self.server_form = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'form' and values.get('id') == 'server-form':
            self.server_form = True
        if 'id' in values:
            self.controls[values['id']] = {'name': values.get('name', '') if self.server_form else '',
                                         'initial': values.get('value', '')}

    def handle_endtag(self, tag):
        if tag == 'form':
            self.server_form = False


@unittest.skipUnless(NODE, 'Node.js is required to execute the admin form functions')
class AdminGatewayFormTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parser = Controls()
        parser.feed(HTML)
        cls.controls = parser.controls
        start = HTML.index('    const gatewayKinds=')
        end = HTML.index("    byId('server-profile-file').addEventListener", start)
        cls.form_code = HTML[start:end]

    def execute(self, assertions, profile=None):
        setup = '''
const assert=require('node:assert/strict');
const nodes=new Map(Object.entries(CONTROLS).map(([id,raw])=>{
  let value=String(raw.initial);
  return [id,{name:raw.name,checked:false,required:false,disabled:false,hidden:false,
    get value(){return value;},set value(next){value=String(next);},addEventListener(){}}];
}));
const byId=id=>{assert(nodes.has(id),'Missing real HTML control '+id);return nodes.get(id);};
const values=()=>Object.fromEntries([...nodes.values()].filter(node=>node.name).map(node=>[node.name,node.value]));
'''.replace('CONTROLS', json.dumps(self.controls))
        code = setup + self.form_code + '\nconst profile=' + json.dumps(profile or PROFILE) + ';\n' + assertions
        result = subprocess.run([NODE, '-e', code], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def test_import_edit_save_and_reopen_preserve_optional_fields(self):
        for capability in [None, True, False]:
            gateway = {**REALITY, 'ws_backup': WS}
            if capability is not None:
                gateway['support_x25519mlkem768'] = capability
            profile = {**PROFILE, 'gateway': gateway}
            with self.subTest(capability=capability):
                saved = self.execute('''
assert(validPublicProfile(profile));
fillServerForm(profile);
assert.equal(byId('gateway-ws-fields').hidden,false);
assert.equal(byId('gateway-ws-fields').disabled,false);
byId('server-name').value='Renamed server';
const saved=serverPayload(values(),byId('gateway-enabled').checked,byId('gateway-ws-enabled').checked);
assert.deepEqual(saved.gateway,profile.gateway);
fillServerForm(JSON.parse(JSON.stringify(saved)));
assert.deepEqual(serverPayload(values(),true,true),saved);
console.log(JSON.stringify(saved));
''', profile)
                self.assertEqual(validate_gateway(saved['gateway']), gateway)
                self.assertEqual(saved['name'], 'Renamed server')

    def test_ws_removal_and_tls_switch_do_not_serialize_hidden_reality_options(self):
        self.execute('''
fillServerForm(profile);
byId('gateway-ws-enabled').checked=false;updateGatewayFields();
assert.equal(byId('gateway-ws-fields').hidden,true);
assert.equal(byId('gateway-ws-address').required,false);
assert(!Object.hasOwn(serverPayload(values(),true,false).gateway,'ws_backup'));
byId('gateway-kind').value='xray-vless-tls';
byId('gateway-certificate-file').value='/etc/xray/fullchain.pem';
byId('gateway-key-file').value='/etc/xray/private.pem';
updateGatewayFields();
assert.equal(byId('gateway-reality-fields').disabled,true);
assert.equal(byId('gateway-ws-fields').disabled,true);
assert.equal(byId('gateway-public-key').required,false);
assert.equal(byId('gateway-certificate-file').required,true);
const tls=serverPayload(values(),true,true).gateway;
assert.equal(tls.kind,'xray-vless-tls');
assert(!Object.hasOwn(tls,'ws_backup'));
assert(!Object.hasOwn(tls,'support_x25519mlkem768'));
assert(!Object.hasOwn(tls,'flow'));
assert.equal(serverPayload(values(),false,true).gateway,null);
''', {**PROFILE, 'gateway': {**REALITY, 'support_x25519mlkem768': False, 'ws_backup': WS}})

    def test_public_profile_validation_rejects_unknown_or_private_metadata(self):
        self.execute('''
assert(validPublicProfile(profile));
const change=patch=>({...profile,gateway:{...profile.gateway,...patch}});
for(const bad of ['true',0,null,{},[]])assert.equal(validPublicProfile(change({support_x25519mlkem768:bad})),false);
for(const patch of [{private_key:'secret'},{clients:[]},{ws_backup:null},
  {ws_backup:{...profile.gateway.ws_backup,password:'secret'}},
  {ws_backup:{...profile.gateway.ws_backup,port:true}},
  {ws_backup:{...profile.gateway.ws_backup,host:'a.example\\r\\nAuthorization: secret'}},
  {ws_backup:{...profile.gateway.ws_backup,address:'https://other.example'}},
  {ws_backup:{...profile.gateway.ws_backup,path:'//other.example'}},
  {ws_backup:{...profile.gateway.ws_backup,path:'/path#fragment'}},
  {ws_backup:{...profile.gateway.ws_backup,inbound_tag:profile.gateway.inbound_tag}},
  {ws_backup:{...profile.gateway.ws_backup,tls_termination:'auto'}}])assert.equal(validPublicProfile(change(patch)),false,JSON.stringify(patch));
const missing=structuredClone(profile);delete missing.gateway.ws_backup.host;
assert.equal(validPublicProfile(missing),false);
const ipv6=structuredClone(profile);ipv6.host='2001:db8::1';ipv6.gateway.ws_backup.address='2001:db8::2';
assert(validPublicProfile(ipv6));
''', {**PROFILE, 'gateway': {**REALITY, 'ws_backup': WS}})

    def test_old_profile_and_each_new_mlkem_selection_round_trip(self):
        self.execute('''
fillServerForm(profile);
assert.equal(byId('gateway-mlkem').value,'');
assert.equal(byId('gateway-ws-enabled').checked,false);
assert.deepEqual(serverPayload(values(),true,false),profile);
for(const choice of ['true','false','']){
  byId('gateway-mlkem').value=choice;
  const gateway=serverPayload(values(),true,false).gateway;
  if(choice==='')assert(!Object.hasOwn(gateway,'support_x25519mlkem768'));
  else assert.equal(gateway.support_x25519mlkem768,choice==='true');
}
''')


if __name__ == '__main__':
    unittest.main()
