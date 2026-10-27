from __future__ import annotations

import base64
import ipaddress
import json
import re
import uuid
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote


class DomainError(ValueError):
    """A client-correctable domain or request error."""


def valid_host(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 253 or '%' in value:
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', part)
                   for part in value.split('.'))


def validate_gateway(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    schemas = {
        'xray-vless-tls': {'kind', 'port', 'server_name', 'certificate_file', 'key_file'},
        'xray-vless-reality': {'kind', 'port', 'server_name', 'public_key', 'short_id', 'inbound_tag', 'flow'},
    }
    optional = {'support_x25519mlkem768', 'ws_backup'}
    if (not isinstance(value, dict) or not isinstance(value.get('kind'), str)
            or value['kind'] not in schemas
            or not schemas[value['kind']] <= set(value)
            or set(value) - schemas[value['kind']] - (optional if value['kind'] == 'xray-vless-reality' else set())
            or isinstance(value['port'], bool) or not isinstance(value['port'], int)
            or not 1 <= value['port'] <= 65535 or not valid_host(value['server_name'])):
        raise DomainError('invalid gateway configuration')
    if value['kind'] == 'xray-vless-reality':
        key = value['public_key']
        if (not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', key)
                or base64.urlsafe_b64encode(base64.urlsafe_b64decode(key + '=')).decode().rstrip('=') != key
                or not isinstance(value['short_id'], str)
                or not re.fullmatch(r'(?:[0-9a-fA-F]{2}){1,8}', value['short_id'])
                or not isinstance(value['inbound_tag'], str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value['inbound_tag'])
                or value['flow'] != 'xtls-rprx-vision'):
            raise DomainError('invalid gateway configuration')
        if ('support_x25519mlkem768' in value
                and not isinstance(value['support_x25519mlkem768'], bool)):
            raise DomainError('invalid gateway configuration')
        if 'ws_backup' in value:
            ws = value['ws_backup']
            if (not isinstance(ws, dict)
                    or set(ws) != {'address', 'port', 'server_name', 'host', 'path', 'inbound_tag', 'tls_termination'}
                    or any(not valid_host(ws[key]) for key in ('address', 'server_name', 'host'))
                    or isinstance(ws['port'], bool) or not isinstance(ws['port'], int)
                    or not 1 <= ws['port'] <= 65535
                    or not isinstance(ws['path'], str) or not ws['path'].startswith('/')
                    or ws['path'].startswith('//') or len(ws['path']) > 1024
                    or any(c.isspace() or ord(c) <= 32 or 127 <= ord(c) <= 159 for c in ws['path'])
                    or any(c in ws['path'] for c in ('#', '\\'))
                    or not isinstance(ws['inbound_tag'], str)
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', ws['inbound_tag'])
                    or ws['inbound_tag'] == value['inbound_tag']
                    or ws['tls_termination'] not in ('xray', 'external')):
                raise DomainError('invalid gateway configuration')
    else:
        for key in ['certificate_file', 'key_file']:
            path = value[key]
            if (not isinstance(path, str) or len(path) > 1024 or not path.startswith('/')
                    or path == '/' or '\\' in path or '//' in path
                    or any(part in {'.', '..'} for part in path.split('/')[1:])
                    or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in path)
                    or str(PurePosixPath(path)) != path):
                raise DomainError('invalid gateway configuration')
    result = dict(value)
    if 'ws_backup' in result:
        result['ws_backup'] = dict(result['ws_backup'])
    return result


def gateway_server_identity(gateway: dict[str, Any] | None) -> dict[str, Any] | None:
    if gateway is None:
        return None
    return {key: value for key, value in gateway.items() if key != 'support_x25519mlkem768'}


def ws_identity(ingress_uuid: str) -> str:
    return str(uuid.uuid5(uuid.UUID(ingress_uuid), 'claude-switcher-ws-v1'))


def upstream_config(protocol: str, value: Any) -> dict[str, str]:
    if protocol not in {'socks5', 'http'}:
        raise DomainError('unsupported gateway upstream')
    if not isinstance(value, dict) or set(value) - {'username', 'password', 'name', 'udp'}:
        raise DomainError('invalid upstream configuration')
    if 'udp' in value and not isinstance(value['udp'], bool):
        raise DomainError('invalid upstream configuration')
    if 'name' in value and (not isinstance(value['name'], str) or len(value['name']) > 120
                            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value['name'])):
        raise DomainError('invalid upstream configuration')
    credentials = {key: value.get(key, '') for key in ['username', 'password']}
    for item in credentials.values():
        if (not isinstance(item, str) or len(item.encode('utf-8')) > 255
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in item)):
            raise DomainError('invalid upstream configuration')
    if bool(credentials['username']) != bool(credentials['password']):
        raise DomainError('invalid upstream configuration')
    return credentials


def managed_nodes(user: dict[str, Any], server: dict[str, Any], gateway: dict[str, Any]) -> list[dict[str, Any]]:
    node = {'name': user['id'] + '-gateway', 'type': 'vless', 'server': server['host'],
            'port': gateway['port'], 'uuid': user['ingress_uuid'], 'tls': True,
            'servername': gateway['server_name'], 'skip-cert-verify': False,
            'network': 'tcp', 'udp': False}
    if gateway['kind'] == 'xray-vless-reality':
        node.update({'flow': gateway['flow'], 'client-fingerprint': 'chrome',
                     'reality-opts': {'public-key': gateway['public_key'], 'short-id': gateway['short_id']}})
        if 'support_x25519mlkem768' in gateway:
            node['reality-opts']['support-x25519mlkem768'] = gateway['support_x25519mlkem768']
    nodes = [node]
    if 'ws_backup' in gateway:
        ws = gateway['ws_backup']
        nodes.append({'name': user['id'] + '-gateway-ws', 'type': 'vless', 'server': ws['address'],
                      'port': ws['port'], 'uuid': ws_identity(user['ingress_uuid']), 'tls': True,
                      'servername': ws['server_name'], 'skip-cert-verify': False,
                      'network': 'ws', 'udp': False,
                      'ws-opts': {'path': ws['path'], 'headers': {'Host': ws['host']}}})
    return nodes


def managed_uri(node: dict[str, Any]) -> str:
    host = node['server']
    if ':' in host:
        host = '[' + host + ']'
    security = 'reality' if 'reality-opts' in node else 'tls'
    extra = ''
    if security == 'reality':
        extra = ('&pbk=' + node['reality-opts']['public-key'] + '&sid=' + node['reality-opts']['short-id']
                 + '&flow=xtls-rprx-vision&fp=chrome')
    elif node['network'] == 'ws':
        extra = ('&host=' + quote(node['ws-opts']['headers']['Host'], safe='')
                 + '&path=' + quote(node['ws-opts']['path'], safe=''))
    return (f"vless://{node['uuid']}@{host}:{node['port']}?encryption=none&security={security}"
            f"&sni={quote(node['servername'], safe='')}&type={node['network']}&allowInsecure=0&udp=false"
            f"{extra}#{quote(node['name'], safe='')}")


def upstream_outbound(row: dict[str, Any], tag: str) -> dict[str, Any]:
    credentials = upstream_config(row['protocol'], json.loads(row['config_json']))
    settings = {'address': row['address'], 'port': row['port']}
    if credentials['username']:
        settings.update({'user': credentials['username'], 'pass': credentials['password']})
    return {'tag': tag, 'protocol': 'socks' if row['protocol'] == 'socks5' else 'http', 'settings': settings}


def xray_config(server: dict[str, Any], gateway: dict[str, Any], bindings: list[dict[str, Any]]) -> dict[str, Any]:
    if gateway['kind'] == 'xray-vless-reality':
        raise DomainError('manual_panel_merge_required')
    users, outbounds, routes = [], [{'tag': 'block', 'protocol': 'blackhole', 'settings': {}}], []
    if server['status'] == 'active':
        for row in bindings:
            email = row['ingress_uuid'] + '@users.claude-switcher.invalid'
            tag = 'residential-' + row['ip_id']
            users.append({'id': row['ingress_uuid'], 'email': email})
            outbounds.append(upstream_outbound(row, tag))
            routes.append({'type': 'field', 'inboundTag': ['shared-ingress'], 'user': [email],
                           'network': 'tcp', 'outboundTag': tag})
    return {'log': {'loglevel': 'none'}, 'inbounds': [
        {'tag': 'shared-ingress', 'listen': '0.0.0.0', 'port': gateway['port'], 'protocol': 'vless',
         'settings': {'users': users, 'decryption': 'none'},
         'streamSettings': {'network': 'tcp', 'security': 'tls', 'tlsSettings': {
             'serverName': gateway['server_name'], 'certificates': [
                 {'certificateFile': gateway['certificate_file'], 'keyFile': gateway['key_file']}]}}}],
        'outbounds': outbounds, 'routing': {'domainStrategy': 'AsIs', 'rules': [
            {'type': 'field', 'network': 'udp', 'outboundTag': 'block'}, *routes,
            {'type': 'field', 'network': 'tcp,udp', 'outboundTag': 'block'}]}}


def xray_merge(server: dict[str, Any], gateway: dict[str, Any], bindings: list[dict[str, Any]]) -> dict[str, Any]:
    if gateway['kind'] != 'xray-vless-reality':
        raise DomainError('reality_gateway_required')
    namespace = 'cs-' + server['id']
    block_tag = namespace + '-block'
    clients, outbounds, rules, managed, ws_clients = [], [], [], [], []
    if server['status'] == 'active':
        for row in bindings:
            email = namespace + '-' + row['ingress_uuid'] + '@users.claude-switcher.invalid'
            tag = namespace + '-residential-' + row['ip_id']
            clients.append({'id': row['ingress_uuid'], 'email': email, 'flow': gateway['flow']})
            outbounds.append(upstream_outbound(row, tag))
            rules.extend([{'type': 'field', 'inboundTag': [gateway['inbound_tag']], 'user': [email],
                           'network': network, 'outboundTag': outbound}
                          for network, outbound in [('tcp', tag), ('udp', block_tag)]])
            managed.append({'uuid': row['ingress_uuid'], 'email': email, 'outbound_tag': tag})
            if 'ws_backup' in gateway:
                ws_uuid = ws_identity(row['ingress_uuid'])
                ws_email = namespace + '-' + row['ingress_uuid'] + '-ws@users.claude-switcher.invalid'
                ws_clients.append({'id': ws_uuid, 'email': ws_email, 'flow': ''})
                rules.extend([{'type': 'field', 'inboundTag': [gateway['ws_backup']['inbound_tag']],
                               'user': [ws_email], 'network': network, 'outboundTag': outbound}
                              for network, outbound in [('tcp', tag), ('udp', block_tag)]])
                managed.append({'uuid': ws_uuid, 'email': ws_email, 'outbound_tag': tag})
    if clients:
        outbounds.append({'tag': block_tag, 'protocol': 'blackhole', 'settings': {}})
    result = {'format': 'claude-switcher-3xui-merge-v1', 'application_status': 'not_applied',
            'server_id': server['id'], 'inbound_tag': gateway['inbound_tag'], 'clients': clients,
            'outbounds': outbounds, 'routing_rules': rules,
            'managed_objects': {'clients': managed, 'block_outbound_tag': block_tag},
            'instructions': [
                'Manual reference package only. It is not a runnable Xray config and has not been applied.',
                'Compare all UUIDs, emails and outbound tags against the existing 3x-ui configuration. Stop on any same-name conflict; never overwrite it.',
                'Register each client id, email and flow in the specified existing 3x-ui inbound.',
                'Insert these per-user rules BEFORE existing broad inbound rules. Each user has TCP through its own upstream and UDP blocked.',
                'Preserve all unmanaged clients, WS inbounds, Reality private keys and panel-owned generated configuration.',
                'On disable or rebind manually remove the old managed UUID, email, routes and outbounds. An empty list is not a revocation snapshot.',
                'Do not use the fresh-install template apply tool, replace the full config, or add catch-all rules. Remote access remains unverified.']}
    if 'ws_backup' in gateway:
        result['additional_inbounds'] = [{'inbound_tag': gateway['ws_backup']['inbound_tag'], 'clients': ws_clients}]
    return result
