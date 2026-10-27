from __future__ import annotations

import copy
from contextlib import closing
import datetime as dt
import fcntl
import grp
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field

from .gateway import gateway_server_identity, valid_host, validate_gateway, xray_merge
from .xui_contract import desired_revision

CONFIG = Path('/etc/claude-switcher-sync/config.json')
STATE_DIR = Path('/var/lib/claude-switcher-sync')
STATUS_DIR = Path('/var/lib/claude-switcher-sync-status')
PANEL_DB = Path('/etc/x-ui/x-ui.db')
RUNTIME = Path('/usr/local/x-ui/bin/config.json')
PANEL_BINARY = Path('/usr/local/x-ui/x-ui')
MAX_BYTES = 16 * 1024 * 1024
CODES = {'ok', 'source_invalid', 'source_changed', 'target_drift', 'ownership_conflict',
         'panel_unavailable', 'panel_auth_failed', 'apply_failed', 'runtime_unverified',
         'unsafe_path', 'internal_error'}


class SyncError(Exception):
    def __init__(self, code):
        self.code = code if code in CODES else 'internal_error'
        super().__init__(self.code)


class RuntimeStarting(SyncError):
    def __init__(self):
        super().__init__('runtime_unverified')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True).encode()).hexdigest()


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


def readonly(path):
    connection = sqlite3.connect(Path(path).absolute().as_uri() + '?mode=ro', uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    connection.execute('BEGIN')
    return connection


@dataclass(frozen=True)
class Desired:
    revision: str
    merge: dict
    gateway: dict | None = None
    clients: dict = field(init=False)

    def __post_init__(self):
        clients = {}
        seen_ids = set()
        for inbound in [self.merge, *self.merge.get('additional_inbounds', [])]:
            for client in inbound['clients']:
                if client['email'] in clients or client['id'] in seen_ids:
                    raise SyncError('source_invalid')
                seen_ids.add(client['id'])
                clients[client['email']] = {**client, 'inbound_tag': inbound['inbound_tag']}
        object.__setattr__(self, 'clients', clients)


def source_snapshot(config):
    try:
        with closing(readonly(config['source_db'])) as db:
            server = db.execute('SELECT * FROM servers WHERE id=?', (config['server_id'],)).fetchone()
            if not server:
                raise SyncError('target_drift')
            server = dict(server)
            gateway = validate_gateway(json.loads(server['gateway_json']))
            if (gateway is None or server['host'] != config['expected_host']
                    or digest(gateway_server_identity(gateway)) != config['gateway_digest']
                    or gateway['kind'] != 'xray-vless-reality'
                    or gateway['inbound_tag'] != config['inbound_tag']
                    or server['status'] not in {'active', 'disabled'}):
                raise SyncError('target_drift')
            ws = gateway.get('ws_backup')
            if ws and (config.get('ws_backup', {}).get('inbound_tag') != ws['inbound_tag']
                       or config['ws_backup'].get('tls_termination') != ws['tls_termination']):
                raise SyncError('target_drift')
            rows = [dict(row) for row in db.execute('''SELECT u.ingress_uuid,i.id AS ip_id,
                i.address,i.port,i.protocol,i.config_json FROM users u JOIN bindings b ON b.user_id=u.id
                JOIN ips i ON i.id=b.ip_id WHERE i.server_id=? AND u.status='active'
                AND i.status='available' ORDER BY u.id''', (config['server_id'],))]
            if len(rows) > 1000:
                raise SyncError('source_invalid')
            for row in rows:
                if (str(uuid.UUID(row['ingress_uuid'])) != row['ingress_uuid']
                        or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', row['ip_id'])
                        or isinstance(row['port'], bool) or not isinstance(row['port'], int)
                        or not 1 <= row['port'] <= 65535
                        or not ipaddress.ip_address(row['address']).is_global):
                    raise SyncError('source_invalid')
            merge = xray_merge(server, gateway, rows)
            return Desired(desired_revision(server, merge), merge, gateway)
    except SyncError:
        raise
    except Exception:
        raise SyncError('source_invalid') from None


def inbound_identity(inbound):
    value = {key: inbound.get(key) for key in
             ('id', 'tag', 'protocol', 'listen', 'port', 'enable', 'nodeId', 'streamSettings', 'sniffing')}
    value['settings'] = {k: v for k, v in inbound['settings'].items() if k not in {'clients', 'users'}}
    return value


@dataclass
class Observed:
    template: dict
    test_url: str
    inbounds: list
    clients: dict
    runtime: dict
    process: str
    base_url: str = ''

    def fingerprint(self):
        return digest({'template': self.template, 'test_url': self.test_url,
                       'inbounds': [inbound_identity(i) for i in self.inbounds],
                       'clients': self.clients, 'runtime': self.runtime, 'process': self.process})


CLIENT_FIELDS = {
    'uuid': 'id', 'email': 'email', 'sub_id': 'subId', 'password': 'password', 'auth': 'auth',
    'flow': 'flow', 'security': 'security', 'limit_ip': 'limitIp', 'total_gb': 'totalGB',
    'expiry_time': 'expiryTime', 'enable': 'enable', 'tg_id': 'tgId', 'group_name': 'group',
    'comment': 'comment', 'reset': 'reset', 'reset_day': 'resetDay', 'reset_weekday': 'resetWeekday',
    'reset_max': 'resetMax', 'traffic_reset': 'trafficReset', 'traffic_reset_day': 'trafficResetDay',
}


def read_json(path):
    with open(path, 'rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise SyncError('unsafe_path')
    return json.loads(raw)


def process_marker():
    matches = []
    for entry in Path('/proc').glob('[0-9]*'):
        try:
            executable = os.readlink(entry / 'exe')
            if executable not in {'/usr/local/x-ui/bin/xray-linux-amd64',
                                  '/usr/local/x-ui/bin/xray-linux-arm64'}:
                continue
            arguments = (entry / 'cmdline').read_bytes().split(b'\0')
            if b'bin/config.json' not in arguments and str(RUNTIME).encode() not in arguments:
                continue
            matches.append(entry.name + ':' + (entry / 'stat').read_text().rsplit(')', 1)[1].split()[19])
        except (OSError, IndexError):
            continue
    return matches[0] if len(matches) == 1 else ''


def panel_snapshot():
    try:
        with closing(readonly(PANEL_DB)) as db:
            settings = dict(db.execute('SELECT key,value FROM settings'))
            port = int(settings['webPort'])
            base = settings['webBasePath']
            if (settings.get('webListen') != '127.0.0.1' or not 1 <= port <= 65535
                    or not re.fullmatch(r'/[A-Za-z0-9_/-]*/?', base)):
                raise SyncError('target_drift')
            inbounds = []
            for row in db.execute('SELECT * FROM inbounds ORDER BY id'):
                item = dict(row)
                item['nodeId'] = item.pop('node_id', None)
                item['streamSettings'] = decode(item.pop('stream_settings'))
                item['settings'] = decode(item['settings'])
                item['sniffing'] = decode(item.get('sniffing') or '{}')
                item['enable'] = bool(item['enable'])
                inbounds.append(item)
            links = {}
            for row in db.execute('SELECT client_id,inbound_id FROM client_inbounds ORDER BY inbound_id'):
                links.setdefault(row['client_id'], []).append(row['inbound_id'])
            clients = {}
            for row in db.execute('SELECT * FROM clients ORDER BY id'):
                item = {public: row[column] for column, public in CLIENT_FIELDS.items() if column in row.keys()}
                item['enable'] = bool(item['enable'])
                if row['reverse']:
                    item['reverse'] = decode(row['reverse'])
                clients[item['email']] = {'client': item, 'inboundIds': links.get(row['id'], [])}
            return Observed(decode(settings['xrayTemplateConfig']), settings['xrayOutboundTestUrl'],
                            inbounds, clients, read_json(RUNTIME), process_marker(),
                            'http://127.0.0.1:' + str(port) + '/' + base.strip('/') + '/panel/api')
    except SyncError:
        raise
    except Exception:
        raise SyncError('panel_unavailable') from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise SyncError('panel_unavailable')


class PanelAPI:
    def __init__(self, base_url):
        trusted_path(PANEL_BINARY)
        result = subprocess.run([str(PANEL_BINARY), 'setting', '-getApiToken',
                                 '-tokenName', 'claude-switcher-sync', '-tokenScope', 'admin'],
                                capture_output=True, timeout=20, cwd='/usr/local/x-ui',
                                env={'PATH': '/usr/bin:/bin', 'HOME': '/root'})
        match = re.search(rb'apiToken:\s*(\S+)', result.stdout)
        if result.returncode or not match:
            raise SyncError('panel_auth_failed')
        self.token = match.group(1).decode('ascii')
        self.base_url = base_url
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, endpoint, payload=None, form=False):
        headers = {'Authorization': 'Bearer ' + self.token}
        data = None
        if payload is not None:
            data = (urllib.parse.urlencode(payload).encode() if form else json.dumps(payload).encode())
            headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
        try:
            request = urllib.request.Request(self.base_url + endpoint, data=data, headers=headers,
                                             method='POST')
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise SyncError('panel_unavailable')
            body = json.loads(raw)
            if body.get('success') is not True:
                message = str(body.get('msg', '')).lower()
                if (endpoint == '/xray/routeTest' and 'rpc error' in message
                        and 'unavailable' in message and 'connection refused' in message):
                    raise RuntimeStarting()
                raise SyncError('apply_failed')
            return body.get('obj')
        except urllib.error.HTTPError as exc:
            raise SyncError('panel_auth_failed' if exc.code in {401, 403} else 'panel_unavailable') from None
        except SyncError:
            raise
        except Exception:
            raise SyncError('panel_unavailable') from None

    def template(self, template, test_url):
        self.request('/xray/update', {'xraySetting': json.dumps(template), 'outboundTestUrl': test_url}, True)

    def client(self, client, inbound_id, exists):
        if exists:
            self.request('/clients/update/' + urllib.parse.quote(client['email'], safe='')
                         + '?inboundIds=' + str(inbound_id), client)
        else:
            self.request('/clients/add', {'client': client, 'inboundIds': [inbound_id]})

    def restart(self):
        self.request('/server/restartXrayService')

    def stop(self):
        self.request('/server/stopXrayService')

    def route(self, inbound_tag, email, network, domain):
        deadline = time.monotonic() + 10
        while True:
            try:
                result = self.request('/xray/routeTest', {'inboundTag': inbound_tag, 'email': email,
                                      'network': network, 'domain': domain, 'port': '443'}, True)
                return result.get('outboundTag') if isinstance(result, dict) else None
            except RuntimeStarting:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.25)


def trusted_path(path, directory=False, private=False):
    path = Path(path)
    for part in [path, *path.parents]:
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise SyncError('unsafe_path')
    info = path.stat()
    if (directory and not stat.S_ISDIR(info.st_mode)) or (not directory and not stat.S_ISREG(info.st_mode)):
        raise SyncError('unsafe_path')
    if private and info.st_mode & 0o077:
        raise SyncError('unsafe_path')


def atomic_json(path, value, gid=None):
    path = Path(path)
    descriptor, temp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            os.fchmod(stream.fileno(), 0o640 if gid is not None else 0o600)
            if gid is not None:
                os.fchown(stream.fileno(), 0, gid)
            json.dump(value, stream, sort_keys=True, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class Files:
    def __init__(self, state_path, status_path, gid=None):
        self.state_path, self.status_path, self.gid = Path(state_path), Path(status_path), gid

    def load(self):
        return read_json(self.state_path) if self.state_path.exists() else {
            'identities': {}, 'outbound_hashes': {}, 'rules': [], 'applied_revision': None}

    def save(self, state):
        atomic_json(self.state_path, state)

    def status(self, config, state, revision, phase, code):
        atomic_json(self.status_path, {'server_id': config['server_id'], 'state': phase,
                    'desired_revision': revision, 'applied_revision': state.get('applied_revision'),
                    'checked_at': now(), 'applied_at': state.get('applied_at'),
                    'managed_count': len(state.get('identities', {})),
                    'code': code if code in CODES else 'internal_error'}, self.gid)


def guard_rules(identity_targets, block):
    return [{'type': 'field', 'inboundTag': [identity_targets[email]['inbound_tag']],
             'user': [email], 'network': 'tcp,udp',
             'outboundTag': block} for email in sorted(identity_targets)]


def configured_targets(config):
    primary = {key: config[key] for key in ('inbound_id', 'inbound_tag', 'inbound_digest')}
    pins = [dict(primary, transport='reality')]
    if 'ws_backup' in config:
        ws = config['ws_backup']
        if (not isinstance(ws, dict)
                or set(ws) != {'inbound_id', 'inbound_tag', 'inbound_digest', 'tls_termination'}
                or ws['tls_termination'] not in ('xray', 'external')):
            raise SyncError('unsafe_path')
        pins.append(dict(ws, transport='ws'))
    targets, ids = {}, set()
    for pin in pins:
        if (not isinstance(pin['inbound_id'], int) or isinstance(pin['inbound_id'], bool)
                or pin['inbound_id'] <= 0
                or not isinstance(pin['inbound_tag'], str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', pin['inbound_tag'])
                or not isinstance(pin['inbound_digest'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', pin['inbound_digest'])
                or pin['inbound_tag'] in targets or pin['inbound_id'] in ids):
            raise SyncError('unsafe_path')
        targets[pin['inbound_tag']] = pin
        ids.add(pin['inbound_id'])
    return targets


def validate_config(config):
    required = {'source_db', 'server_id', 'expected_host', 'inbound_id', 'inbound_tag',
                'gateway_digest', 'inbound_digest'}
    if (not isinstance(config, dict) or not required <= set(config)
            or set(config) - required - {'ws_backup'}
            or not isinstance(config['server_id'], str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', config['server_id'])
            or not isinstance(config['gateway_digest'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', config['gateway_digest'])
            or not valid_host(config['expected_host'])
            or not isinstance(config['source_db'], str) or not Path(config['source_db']).is_absolute()
            or any(ord(c) < 32 for c in config['source_db'])):
        raise SyncError('unsafe_path')
    configured_targets(config)


def validate_ws_origin(inbound, pin, ws=None):
    stream = inbound.get('streamSettings', {})
    if (inbound.get('protocol') != 'vless' or stream.get('network') != 'ws'
            or stream.get('security') != ('tls' if pin['tls_termination'] == 'xray' else 'none')):
        raise SyncError('target_drift')
    if pin['tls_termination'] == 'external':
        try:
            if not ipaddress.ip_address(inbound.get('listen')).is_loopback:
                raise ValueError()
        except (ValueError, TypeError):
            raise SyncError('target_drift') from None
    if ws:
        options = stream.get('wsSettings', {})
        headers = options.get('headers', {})
        host_headers = [value for key, value in headers.items() if key.lower() == 'host']
        if (ws['tls_termination'] != pin['tls_termination']
                or options.get('path') != ws['path']
                or host_headers and host_headers != [ws['host']]
                or pin['tls_termination'] == 'xray' and inbound.get('port') != ws['port']):
            raise SyncError('target_drift')


def validate_target(config, observed, desired=None):
    for tag, pin in configured_targets(config).items():
        matches = [i for i in observed.inbounds if i['id'] == pin['inbound_id']]
        if (len(matches) != 1 or matches[0]['tag'] != tag
                or digest(inbound_identity(matches[0])) != pin['inbound_digest']
                or matches[0]['protocol'] != 'vless' or matches[0].get('nodeId') is not None
                or not matches[0]['enable']):
            raise SyncError('target_drift')
        stream = matches[0]['streamSettings']
        if pin['transport'] == 'reality':
            if stream.get('security') != 'reality' or stream.get('network') not in ('tcp', 'raw'):
                raise SyncError('target_drift')
        else:
            ws = desired.gateway.get('ws_backup') if desired and desired.gateway else None
            validate_ws_origin(matches[0], pin, ws)
    if (not isinstance(observed.template, dict) or 'xraySetting' in observed.template
            or not isinstance(observed.template.get('outbounds'), list)
            or not isinstance(observed.template.get('routing', {}).get('rules'), list)):
        raise SyncError('target_drift')


def identity_targets(config, state, desired):
    targets = configured_targets(config)
    if 'identity_targets' not in state:
        primary = config['inbound_tag']
        for rule in state['rules']:
            if set(rule.get('user', [])) & set(state['identities']) and rule.get('inboundTag') != [primary]:
                raise SyncError('target_drift')
        state['identity_targets'] = {
            email: {'inbound_tag': primary, 'flow': 'xtls-rprx-vision'} for email in state['identities']}
    if set(state['identity_targets']) != set(state['identities']):
        raise SyncError('target_drift')
    result = copy.deepcopy(state['identity_targets'])
    for email, client in desired.clients.items():
        target = {'inbound_tag': client['inbound_tag'], 'flow': client['flow']}
        if email in result and result[email] != target:
            raise SyncError('ownership_conflict')
        result[email] = target
    for target in result.values():
        if not isinstance(target, dict) or set(target) != {'inbound_tag', 'flow'}:
            raise SyncError('target_drift')
        pin = targets.get(target['inbound_tag'])
        if pin is None or target['flow'] != ('xtls-rprx-vision' if pin['transport'] == 'reality' else ''):
            raise SyncError('target_drift')
    return result


def runtime_managed_clients(runtime, state, desired):
    identities = state['identities']
    known_emails = set(identities) | set(desired.clients)
    known_ids = set(identities.values()) | {client['id'] for client in desired.clients.values()}
    clients, seen_ids = {}, set()
    for inbound in runtime.get('inbounds', []):
        for client in inbound.get('settings', {}).get('clients', inbound.get('settings', {}).get('users', [])):
            email, client_id = client.get('email'), client.get('id')
            if email not in known_emails and client_id not in known_ids:
                continue
            if (email not in identities or identities[email] != client_id
                    or inbound.get('tag') != state['identity_targets'][email]['inbound_tag']
                    or email in clients or client_id in seen_ids):
                raise SyncError('ownership_conflict')
            clients[email] = client
            seen_ids.add(client_id)
    return clients


def check_ownership(config, observed, state, desired):
    identities = state['identities']
    proposed = {email: client['id'] for email, client in desired.clients.items()}
    targets = configured_targets(config)
    for email, client_id in proposed.items():
        if email in identities and identities[email] != client_id:
            raise SyncError('ownership_conflict')
        for existing_email, record in observed.clients.items():
            if existing_email == email or record['client'].get('id') == client_id:
                if identities.get(existing_email) != client_id or existing_email != email:
                    raise SyncError('ownership_conflict')
    for email, client_id in identities.items():
        if any(other != email and item['client'].get('id') == client_id
               for other, item in observed.clients.items()):
            raise SyncError('ownership_conflict')
        record = observed.clients.get(email)
        if record and (record['client'].get('id') != client_id
                       or record['inboundIds'] != [targets[state['identity_targets'][email]['inbound_tag']]['inbound_id']]
                       or record['client'].get('flow', '') != state['identity_targets'][email]['flow']
                       or record['client'].get('reverse')):
            raise SyncError('ownership_conflict')
    planned_tags = {o['tag'] for o in desired.merge['outbounds']} | {'cs-' + config['server_id'] + '-block'}
    seen = set()
    for outbound in observed.template['outbounds']:
        tag = outbound.get('tag')
        if tag in seen:
            raise SyncError('ownership_conflict')
        seen.add(tag)
        if tag in state['outbound_hashes']:
            if digest(outbound) not in state['outbound_hashes'][tag]:
                raise SyncError('ownership_conflict')
        elif tag in planned_tags:
            raise SyncError('ownership_conflict')
    for outbound in observed.runtime.get('outbounds', []):
        if outbound.get('tag') in planned_tags and outbound.get('tag') not in state['outbound_hashes']:
            raise SyncError('ownership_conflict')
    all_emails = set(identities) | set(proposed)
    for rule in observed.template['routing']['rules']:
        if rule in state['rules']:
            continue
        if set(rule.get('user', [])) & all_emails or rule.get('outboundTag') in state['outbound_hashes']:
            raise SyncError('ownership_conflict')
    runtime_managed_clients(observed.runtime, state, desired)


def unmanaged(observed, state):
    template = copy.deepcopy(observed.template)
    template['outbounds'] = [o for o in template['outbounds'] if o.get('tag') not in state['outbound_hashes']]
    template['routing']['rules'] = [r for r in template['routing']['rules'] if r not in state['rules']]
    return {'template': template, 'test_url': observed.test_url,
            'inbounds': [inbound_identity(i) for i in observed.inbounds],
            'clients': {email: value for email, value in observed.clients.items() if email not in state['identities']}}


def merged_template(observed, state, outbounds, rules):
    result = unmanaged(observed, state)['template']
    result['outbounds'].extend(copy.deepcopy(outbounds))
    result['routing']['rules'] = copy.deepcopy(rules) + result['routing']['rules']
    return result


def verify_template(observed, outbounds, rules, state, baseline):
    if unmanaged(observed, state) != baseline:
        raise SyncError('ownership_conflict')
    actual = {o['tag']: o for o in observed.template['outbounds'] if o.get('tag') in state['outbound_hashes']}
    if actual != {o['tag']: o for o in outbounds}:
        raise SyncError('runtime_unverified')
    selected = [r for r in observed.template['routing']['rules'] if r in state['rules']]
    if selected != rules:
        raise SyncError('runtime_unverified')
    all_rules = observed.template['routing']['rules']
    start = next((n for n, rule in enumerate(all_rules) if rule in rules), len(all_rules))
    if all_rules[start:start + len(rules)] != rules:
        raise SyncError('runtime_unverified')
    managed_inbounds = {tag for rule in rules for tag in rule.get('inboundTag', [])}
    for rule in all_rules[:start] if rules else []:
        if not rule.get('inboundTag') or managed_inbounds.intersection(rule['inboundTag']):
            raise SyncError('runtime_unverified')


def runtime_tls_settings(inbound):
    settings = inbound.get('streamSettings', {}).get('tlsSettings')
    if not isinstance(settings, dict):
        raise SyncError('runtime_unverified')
    # 3x-ui GetXrayConfig removes this client-share metadata before generating Xray JSON.
    return {key: value for key, value in settings.items() if key != 'settings'}


def verify_runtime(config, observed, clients, outbounds, rules, state, desired):
    if not observed.process:
        raise SyncError('runtime_unverified')
    runtime = observed.runtime
    outbound_map = {o['tag']: o for o in runtime.get('outbounds', [])}
    if any(outbound_map.get(o['tag']) != o for o in outbounds):
        raise SyncError('runtime_unverified')
    runtime_rules = runtime.get('routing', {}).get('rules', [])
    pins = configured_targets(config)
    # Panel-generated API routing may precede user rules, but cannot include our inbound or users.
    start = next((n for n, r in enumerate(runtime_rules) if r in rules), len(runtime_rules))
    for rule in runtime_rules[:start] if rules else []:
        if not rule.get('inboundTag') or set(pins).intersection(rule['inboundTag']):
            raise SyncError('runtime_unverified')
    if runtime_rules[start:start + len(rules)] != rules:
        raise SyncError('runtime_unverified')
    actual = runtime_managed_clients(runtime, state, desired)
    for tag, pin in pins.items():
        matches = [i for i in runtime.get('inbounds', []) if i.get('tag') == tag]
        if len(matches) != 1:
            raise SyncError('runtime_unverified')
        inbound = matches[0]
        panel = next(i for i in observed.inbounds if i['tag'] == tag)
        if any(inbound.get(key) != panel.get(key) for key in ('protocol', 'listen', 'port')):
            raise SyncError('runtime_unverified')
        if pin['transport'] == 'ws':
            ws = desired.gateway.get('ws_backup') if desired.gateway else None
            try:
                validate_ws_origin(inbound, pin, ws)
            except SyncError:
                raise SyncError('runtime_unverified') from None
            if inbound.get('streamSettings', {}).get('wsSettings') != panel['streamSettings'].get('wsSettings'):
                raise SyncError('runtime_unverified')
            if pin['tls_termination'] == 'xray' and runtime_tls_settings(inbound) != runtime_tls_settings(panel):
                raise SyncError('runtime_unverified')
        elif (inbound.get('streamSettings', {}).get('security') != 'reality'
              or inbound.get('streamSettings', {}).get('network') not in ('tcp', 'raw')):
            raise SyncError('runtime_unverified')
    for email, client in clients.items():
        found = actual.get(email)
        if client['enable']:
            if not found or found.get('id') != client['id'] or found.get('flow', '') != client['flow']:
                raise SyncError('runtime_unverified')
        elif found:
            raise SyncError('runtime_unverified')


def reconcile(config, files, source, observe, api_factory):
    state = files.load()
    revision = state.get('pending') or state.get('applied_revision')
    api = None
    guarded = None
    baseline = None
    try:
        desired = source()
        revision = desired.revision
        observed = observe()
        validate_target(config, observed, desired)
        planned_targets = identity_targets(config, state, desired)
        targets = configured_targets(config)
        check_ownership(config, observed, state, desired)
        if (not state.get('pending') and state.get('applied_revision') == revision
                and state.get('observed_digest') == observed.fingerprint() and observed.process):
            files.status(config, state, revision, 'applied', 'ok')
            return True
        baseline = unmanaged(observed, state)
        prior = bool(state['identities'])
        identities = dict(state['identities'])
        identities.update({email: client['id'] for email, client in desired.clients.items()})
        block = {'tag': 'cs-' + config['server_id'] + '-block', 'protocol': 'blackhole', 'settings': {}}
        guards = guard_rules(planned_targets, block['tag'])
        outbounds = [o for o in desired.merge['outbounds'] if o['tag'] != block['tag']] + [block]
        final_rules = desired.merge['routing_rules'] + guards
        for outbound in outbounds:
            state['outbound_hashes'].setdefault(outbound['tag'], [])
            if digest(outbound) not in state['outbound_hashes'][outbound['tag']]:
                state['outbound_hashes'][outbound['tag']].append(digest(outbound))
        for rule in guards + final_rules:
            if rule not in state['rules']:
                state['rules'].append(rule)
        state['identities'] = identities
        state['identity_targets'] = planned_targets
        state['pending'] = revision
        files.save(state)
        files.status(config, state, revision, 'applying', 'ok')
        api = api_factory(observed.base_url)

        def save_template(outbounds_to_save, rules_to_save):
            current = observe()
            validate_target(config, current, desired)
            check_ownership(config, current, state, desired)
            if unmanaged(current, state) != baseline:
                raise SyncError('ownership_conflict')
            template = merged_template(current, state, outbounds_to_save, rules_to_save)
            api.template(template, current.test_url)
            after = observe()
            verify_template(after, outbounds_to_save, rules_to_save, state, baseline)
            return after

        previous_outbounds = [o for o in observed.template['outbounds']
                              if o.get('tag') in state['outbound_hashes'] and o.get('tag') != block['tag']]
        guarded = (previous_outbounds + [block], guards)
        observed = save_template(*guarded)
        if prior:
            api.restart()
            observed = observe()
        if identities:
            for email in identities:
                for network in ('tcp', 'udp'):
                    if api.route(planned_targets[email]['inbound_tag'], email, network, 'example.com') != block['tag']:
                        raise SyncError('runtime_unverified')
        enabled = desired.clients
        clients = {}
        for email, client_id in identities.items():
            observed = observe()
            check_ownership(config, observed, state, desired)
            record = observed.clients.get(email)
            target = planned_targets[email]
            inbound_id = targets[target['inbound_tag']]['inbound_id']
            client = copy.deepcopy(record['client']) if record else {
                'id': client_id, 'email': email, 'flow': target['flow'], 'limitIp': 0,
                'totalGB': 0, 'expiryTime': 0, 'subId': uuid.uuid4().hex, 'comment': '', 'reset': 0}
            client.update({'id': client_id, 'email': email, 'enable': email in enabled,
                           'flow': target['flow']})
            if not record or client != record['client']:
                api.client(client, inbound_id, record is not None)
            after = observe().clients.get(email)
            if (not after or after['inboundIds'] != [inbound_id]
                    or any(after['client'].get(k) != client[k] for k in ('id', 'email', 'enable', 'flow', 'subId'))):
                raise SyncError('runtime_unverified')
            clients[email] = client
        guarded = (outbounds, guards + desired.merge['routing_rules'])
        save_template(*guarded)
        if source().revision != revision:
            raise SyncError('source_changed')
        observed = save_template(outbounds, final_rules)
        api.restart()
        observed = observe()
        validate_target(config, observed, desired)
        check_ownership(config, observed, state, desired)
        verify_template(observed, outbounds, final_rules, state, baseline)
        verify_runtime(config, observed, clients, outbounds, final_rules, state, desired)
        for email in identities:
            tcp_tag = next((r['outboundTag'] for r in desired.merge['routing_rules']
                            if r['user'] == [email] and r['network'] == 'tcp'), block['tag'])
            for domain in ('example.com', 'claude.ai'):
                for network, expected in (('tcp', tcp_tag), ('udp', block['tag'])):
                    if api.route(planned_targets[email]['inbound_tag'], email, network, domain) != expected:
                        raise SyncError('runtime_unverified')
        if source().revision != revision:
            raise SyncError('source_changed')
        state.update({'applied_revision': revision, 'applied_at': now(), 'pending': None,
                      'observed_digest': observed.fingerprint()})
        files.save(state)
        files.status(config, state, revision, 'applied', 'ok')
        return True
    except Exception as exc:
        code = exc.code if isinstance(exc, SyncError) else 'internal_error'
        if api is not None and guarded is not None:
            # A failure can follow a committed panel write. Read truth before restoring a deny.
            try:
                current = observe()
                validate_target(config, current, desired)
                check_ownership(config, current, state, desired)
                if unmanaged(current, state) != baseline:
                    raise SyncError('ownership_conflict')
                template = merged_template(current, state, *guarded)
                api.template(template, current.test_url)
                verify_template(observe(), *guarded, state, baseline)
                api.restart()
                for email, target in state['identity_targets'].items():
                    for network in ('tcp', 'udp'):
                        if api.route(target['inbound_tag'], email, network, 'example.com') != block['tag']:
                            raise SyncError('runtime_unverified')
            except Exception:
                try:
                    api.stop()
                except Exception:
                    pass
        files.status(config, state, revision, 'blocked', code)
        return False


def main():
    os.umask(0o077)
    if os.geteuid() != 0:
        return 1
    try:
        trusted_path(CONFIG, private=True)
        trusted_path(Path(__file__))
        trusted_path(Path(__file__).with_name('gateway.py'))
        trusted_path(Path(__file__).with_name('xui_contract.py'))
        trusted_path(STATE_DIR, directory=True, private=True)
        trusted_path(STATUS_DIR, directory=True)
        config = read_json(CONFIG)
        validate_config(config)
        lock = os.open(STATE_DIR / 'lock', os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            files = Files(STATE_DIR / 'state.json', STATUS_DIR / 'status.json', grp.getgrnam('claude-switcher').gr_gid)
            if files.state_path.exists():
                trusted_path(files.state_path, private=True)
            return 0 if reconcile(config, files, lambda: source_snapshot(config), panel_snapshot, PanelAPI) else 1
        finally:
            os.close(lock)
    except Exception:
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
