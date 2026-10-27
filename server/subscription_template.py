"""Public, credential-free Mihomo settings shared by every Clash subscription."""
from __future__ import annotations

from copy import deepcopy
import ipaddress
import json
from urllib.parse import urlsplit

from .gateway import DomainError

GROUP = 'CLAUDE-SWITCHER'
PROTECTED_AI_DOMAINS = ('claude.ai', 'anthropic.com', 'claude.com', 'clau.de', 'claudeusercontent.com')
AI_DOMAINS = ('statsigapi.net', 'openai.com', 'chatgpt.com', 'oaistatic.com', 'oaiusercontent.com',
              'arkoselabs.com', 'statsig.com', 'featuregates.org', 'gemini.google.com',
              'x.ai', 'grok.com', 'perplexity.ai')
BOOTSTRAP = ['https://223.5.5.5/dns-query#DIRECT', 'https://120.53.53.53/dns-query#DIRECT']
GEO_ROOT = 'https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/'
DEFAULT_TEMPLATE = {
    'config': {
        'mixed-port': 7897, 'bind-address': '127.0.0.1', 'allow-lan': False,
        'mode': 'rule', 'log-level': 'silent', 'ipv6': False,
        'unified-delay': True, 'tcp-concurrent': True, 'find-process-mode': 'strict',
        'profile': {'store-selected': True},
        'sniffer': {'enable': True, 'parse-pure-ip': True, 'override-destination': False,
                    'force-dns-mapping': True, 'sniff': {'TLS': {'ports': [443]},
                                                      'HTTP': {'ports': [80, 8080, 8443]}}},
        'geodata-mode': True, 'geo-auto-update': False,
        'geox-url': {key: GEO_ROOT + value for key, value in
                     [('geoip', 'geoip.dat'), ('geosite', 'geosite.dat'), ('mmdb', 'country.mmdb')]},
        'dns': {'enable': True, 'listen': '127.0.0.1:1053', 'ipv6': False,
                'enhanced-mode': 'fake-ip', 'fake-ip-range': '198.18.0.1/16',
                'fake-ip-filter': ['localhost', '+.local', '+.lan', '+.pool.ntp.org',
                                   'time.*.com', 'ntp.*.com', 'msftconnecttest.com', 'msftncsi.com'],
                'respect-rules': True, 'prefer-h3': False, 'use-hosts': False, 'use-system-hosts': False,
                'default-nameserver': BOOTSTRAP, 'proxy-server-nameserver': BOOTSTRAP,
                'nameserver': ['https://1.1.1.1/dns-query#' + GROUP,
                               'https://8.8.8.8/dns-query#' + GROUP]},
        'tun': {'enable': False},
    },
    'rules': ['GEOSITE,category-ads-all,REJECT', 'DOMAIN-KEYWORD,stun,REJECT',
              *[f'DOMAIN-SUFFIX,{domain},{GROUP}' for domain in
                ('browserleaks.com', 'ipleak.net', 'dnsleaktest.com', *AI_DOMAINS)],
              f'DOMAIN-KEYWORD,arkose,{GROUP}', f'DOMAIN,challenges.cloudflare.com,{GROUP}'],
    'domestic_direct_rules': [
        *[f'IP-CIDR,{cidr},DIRECT,no-resolve' for cidr in
          ('127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
           '100.64.0.0/10', '169.254.0.0/16')],
        *[f'IP-CIDR6,{cidr},DIRECT,no-resolve' for cidr in ('::1/128', 'fc00::/7', 'fe80::/10')],
        *[f'DST-PORT,{port},DIRECT' for port in (25, 465, 587, 110, 143, 993, 995)],
        'DOMAIN-SUFFIX,codemaster.com.cn,DIRECT', 'DOMAIN-SUFFIX,codemao.cn,DIRECT',
        'GEOSITE,cn,DIRECT', 'GEOIP,CN,DIRECT,no-resolve'],
}


def _shape(value, reference):
    if type(value) is not type(reference):
        raise DomainError('invalid template field type')
    if isinstance(reference, dict):
        if set(value) != set(reference):
            raise DomainError('invalid template fields')
        for key in reference:
            _shape(value[key], reference[key])
    elif isinstance(reference, list):
        if not value or len(value) > 256:
            raise DomainError('invalid template list')
        for item in value:
            _shape(item, reference[0])
    elif isinstance(value, str) and (not value or len(value) > 2048 or any(ord(c) < 32 for c in value)):
        raise DomainError('invalid template text')


def _url(value, error):
    try:
        parts = urlsplit(value)
        parts.port  # Validate malformed or out-of-range ports before rendering.
        return parts
    except ValueError:
        raise DomainError(error) from None


def validate_template(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_TEMPLATE):
        raise DomainError('invalid subscription template')
    config = value['config']
    _shape(config, DEFAULT_TEMPLATE['config'])
    if (config['bind-address'] != '127.0.0.1' or config['allow-lan'] or config['ipv6']
            or config['mode'] != 'rule' or config['tun']['enable']):
        raise DomainError('template requires loopback rule mode with IPv6 and TUN disabled')
    if not 1 <= config['mixed-port'] <= 65535:
        raise DomainError('invalid template port')
    if config['log-level'] not in {'silent', 'error', 'warning', 'info', 'debug'}:
        raise DomainError('invalid template log level')
    if config['find-process-mode'] not in {'always', 'strict', 'off'}:
        raise DomainError('invalid template process mode')
    for spec in config['sniffer']['sniff'].values():
        if any(not 1 <= port <= 65535 for port in spec['ports']):
            raise DomainError('invalid template sniffer port')
    for url in config['geox-url'].values():
        parts = _url(url, 'invalid template geodata URL')
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise DomainError('invalid template geodata URL')
    dns = config['dns']
    if (not dns['enable'] or not dns['respect-rules'] or dns['ipv6'] or dns['prefer-h3']
            or dns['use-hosts'] or dns['use-system-hosts'] or dns['enhanced-mode'] != 'fake-ip'):
        raise DomainError('invalid template DNS policy')
    try:
        host, port = dns['listen'].rsplit(':', 1)
        network = ipaddress.ip_network(dns['fake-ip-range'], strict=False)
        if host != '127.0.0.1' or not 1 <= int(port) <= 65535 or network.version != 4:
            raise ValueError
    except ValueError:
        raise DomainError('invalid template DNS address') from None
    for key in ('nameserver', 'default-nameserver', 'proxy-server-nameserver'):
        for url in dns[key]:
            parts = _url(url, 'invalid template DNS route')
            try:
                address = ipaddress.ip_address(parts.hostname or '')
            except ValueError:
                raise DomainError('template DNS requires IP-address DoH') from None
            expected = GROUP if key == 'nameserver' else 'DIRECT'
            if (parts.scheme != 'https' or parts.username or parts.password or not address.is_global
                    or parts.port not in {None, 443} or parts.path != '/dns-query' or parts.query or parts.fragment != expected):
                raise DomainError('invalid template DNS route')
    for key in ('rules', 'domestic_direct_rules'):
        rules = value[key]
        if not isinstance(rules, list) or len(rules) > 256:
            raise DomainError('invalid template rules')
        for rule in rules:
            if not isinstance(rule, str) or len(rule) > 2048 or any(c.isspace() for c in rule):
                raise DomainError('invalid template rule')
            fields = rule.split(',')
            if (len(fields) not in {3, 4} or fields[0] not in
                    {'DOMAIN', 'DOMAIN-SUFFIX', 'DOMAIN-KEYWORD', 'GEOSITE', 'GEOIP',
                     'IP-CIDR', 'IP-CIDR6', 'DST-PORT', 'NETWORK'} or not fields[1]
                    or fields[2] not in ({GROUP, 'REJECT'} if key == 'rules' else {'DIRECT'})
                    or (len(fields) == 4 and (fields[3] != 'no-resolve' or fields[0] not in {'GEOIP', 'IP-CIDR', 'IP-CIDR6'}))):
                raise DomainError('invalid template rule')
            if fields[0] in {'IP-CIDR', 'IP-CIDR6'}:
                try:
                    net = ipaddress.ip_network(fields[1], strict=False)
                    if net.version != (6 if fields[0] == 'IP-CIDR6' else 4):
                        raise ValueError
                except ValueError:
                    raise DomainError('invalid template rule network') from None
            if fields[0] == 'DST-PORT' and (not fields[1].isdigit() or not 1 <= int(fields[1]) <= 65535):
                raise DomainError('invalid template rule port')
            if fields[0] == 'NETWORK' and fields[1] not in {'tcp', 'udp'}:
                raise DomainError('invalid template rule network')
    return deepcopy(value)


def render_template(nodes, *, template=None, routing_profile='strict'):
    if routing_profile not in {'strict', 'domestic-direct'}:
        raise DomainError('invalid subscription routing')
    selected = deepcopy(DEFAULT_TEMPLATE if template is None else template)
    document = selected['config']
    dns = document.pop('dns')
    tun = document.pop('tun')
    document['proxies'] = deepcopy(nodes)
    document['proxy-groups'] = [{'name': GROUP, 'type': 'select', 'proxies': [n['name'] for n in nodes]}]
    document['dns'] = dns
    document['tun'] = tun
    # Reserved prefix and suffix keep AI traffic ahead of every custom direct rule.
    document['rules'] = ['NETWORK,udp,REJECT',
                         *[f'DOMAIN-SUFFIX,{domain},{GROUP}' for domain in PROTECTED_AI_DOMAINS],
                         *selected['rules']]
    if routing_profile == 'domestic-direct':
        document['rules'].extend(selected['domestic_direct_rules'])
    document['rules'].append('MATCH,' + GROUP)
    return document


def template_revision(value):
    import hashlib
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
