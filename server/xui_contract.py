from __future__ import annotations

import hashlib
import json

from .gateway import gateway_server_identity


def desired_revision(server: dict, merge: dict) -> str:
    gateway = server.get('gateway')
    if gateway is None and server.get('gateway_json'):
        gateway = json.loads(server['gateway_json'])
    value = {'server': {key: server[key] for key in ('id', 'host', 'status')},
             'merge': {key: merge[key] for key in
                       ('inbound_tag', 'clients', 'outbounds', 'routing_rules')}}
    value['server']['gateway'] = gateway_server_identity(gateway)
    if 'additional_inbounds' in merge:
        value['merge']['additional_inbounds'] = merge['additional_inbounds']
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True).encode()).hexdigest()
