#!/usr/bin/env python3
"""Explicit developer probe for a private managed subscription, without installing it.

PyYAML is optional and never installed by this tool. Inputs must already exist and
have private permissions. Each run selects one node and uses only neutral public
endpoints. Fake-IP and AAAA results verify the probe's local DNS behavior. An
example.com SOA response exercises the upstream DNS path. These checks do not
prove the upstream resolver's observed egress or DNS leak protection. The existing host TUN
and routing remain in effect. No Claude service request or login is performed.
"""
from __future__ import annotations

import argparse
import copy
import ipaddress
import json
import os
from pathlib import Path
import signal
import select
import socket
import struct
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit


class ProbeError(ValueError):
    pass


def private_document(path: Path, *, yaml_format=False):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise ProbeError('invalid_private_file')
    if path.stat().st_mode & 0o077:
        raise ProbeError('private_file_permissions_required')
    if yaml_format:
        try:
            import yaml
        except ImportError:
            raise ProbeError('missing_dependency_pyyaml') from None
        try:
            return yaml.safe_load(path.read_text())
        except (ValueError, yaml.YAMLError):
            raise ProbeError('invalid_subscription') from None
    return json.loads(path.read_text())


def validate_subscription(document, *, routing_profile=None):
    allowed = {'mixed-port', 'allow-lan', 'ipv6', 'mode', 'log-level', 'proxies',
               'proxy-groups', 'rules', 'dns', 'profile', 'bind-address', 'tun',
               'unified-delay', 'tcp-concurrent', 'find-process-mode', 'sniffer',
               'geodata-mode', 'geo-auto-update', 'geox-url'}
    if not isinstance(document, dict) or set(document) - allowed:
        raise ProbeError('unsupported_subscription_fields')
    if 'tun' in document and document['tun'] != {'enable': False}:
        raise ProbeError('unsupported_subscription_fields')
    if document.get('bind-address', '127.0.0.1') != '127.0.0.1':
        raise ProbeError('loopback_bind_required')
    if document.get('mode') != 'rule' or document.get('ipv6') is not False:
        raise ProbeError('managed_rule_and_ipv6_policy_required')
    nodes = document.get('proxies')
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 8:
        raise ProbeError('invalid_managed_nodes')
    node_keys = {'name', 'type', 'server', 'port', 'uuid', 'tls', 'servername',
                 'skip-cert-verify', 'network', 'udp', 'flow', 'client-fingerprint',
                 'reality-opts', 'ws-opts'}
    names = []
    for node in nodes:
        if (not isinstance(node, dict) or set(node) - node_keys or node.get('type') != 'vless'
                or node.get('tls') is not True or node.get('skip-cert-verify') is not False
                or node.get('udp') is not False or node.get('network') not in {'tcp', 'ws'}
                or not isinstance(node.get('name'), str) or not node['name']
                or node['name'] in {'DIRECT', 'REJECT', 'REJECT-DROP', 'PASS', 'GLOBAL'}
                or not isinstance(node.get('server'), str) or not node['server']
                or type(node.get('port')) is not int or not 1 <= node['port'] <= 65535
                or not isinstance(node.get('uuid'), str) or not node['uuid']):
            raise ProbeError('unsafe_managed_node')
        if 'reality-opts' in node and (not isinstance(node['reality-opts'], dict)
                or set(node['reality-opts']) - {'public-key', 'short-id', 'support-x25519mlkem768'}):
            raise ProbeError('unsafe_reality_options')
        if 'ws-opts' in node and (not isinstance(node['ws-opts'], dict)
                or set(node['ws-opts']) - {'path', 'headers'}):
            raise ProbeError('unsafe_ws_options')
        names.append(node['name'])
    if len(names) != len(set(names)):
        raise ProbeError('duplicate_nodes')
    groups = document.get('proxy-groups')
    if (not isinstance(groups, list) or len(groups) != 1 or not isinstance(groups[0], dict)
            or set(groups[0]) != {'name', 'type', 'proxies'} or groups[0]['type'] != 'select'
            or not isinstance(groups[0]['name'], str) or not groups[0]['name'] or groups[0]['name'] in names
            or groups[0]['name'] in {'DIRECT', 'REJECT', 'REJECT-DROP', 'PASS', 'GLOBAL'}
            or not isinstance(groups[0]['proxies'], list) or groups[0]['proxies'] != names):
        raise ProbeError('unsafe_proxy_group')
    group = groups[0]['name']
    strict_rules = ['NETWORK,udp,REJECT',
                      'DOMAIN-SUFFIX,claude.ai,' + group,
                      'DOMAIN-SUFFIX,anthropic.com,' + group,
                      'DOMAIN-SUFFIX,claude.com,' + group,
                      'DOMAIN-SUFFIX,clau.de,' + group,
                      'DOMAIN-SUFFIX,claudeusercontent.com,' + group,
                      'MATCH,' + group]
    rules = document.get('rules')
    if (not isinstance(rules, list) or rules[:6] != strict_rules[:6]
            or rules[-1:] != strict_rules[-1:] or any(not isinstance(rule, str) for rule in rules)):
        raise ProbeError('unsafe_routing_rules')
    # Use the same rule grammar as the template editor, with the reserved prefix/suffix above.
    package_root = str(Path(__file__).resolve().parents[1])
    if package_root not in sys.path:
        sys.path.insert(0, package_root)
    from server.subscription_template import DEFAULT_TEMPLATE, GROUP, validate_template
    candidate = copy.deepcopy(DEFAULT_TEMPLATE)
    candidate['rules'], candidate['domestic_direct_rules'] = [], []
    direct_seen = False
    for rule in rules[6:-1]:
        fields = rule.split(',')
        direct = len(fields) >= 3 and fields[2] == 'DIRECT'
        if direct_seen and not direct:
            raise ProbeError('unsafe_routing_rules')
        direct_seen = direct_seen or direct
        if len(fields) >= 3 and fields[2] == group:
            fields[2] = GROUP
        candidate['domestic_direct_rules' if direct else 'rules'].append(','.join(fields))
    if routing_profile is None:
        routing_profile = 'domestic-direct' if direct_seen else 'strict'
    if routing_profile not in {'strict', 'domestic-direct'} or (routing_profile == 'strict' and direct_seen):
        raise ProbeError('unsafe_routing_rules')
    for key in ('tun', 'unified-delay', 'tcp-concurrent', 'find-process-mode', 'sniffer',
                'geodata-mode', 'geo-auto-update', 'geox-url'):
        if key in document:
            candidate['config'][key] = document[key]
    try:
        validate_template(candidate)
    except ValueError:
        raise ProbeError('unsafe_template_settings') from None
    dns = document.get('dns')
    dns_keys = {'enable', 'listen', 'ipv6', 'enhanced-mode', 'fake-ip-range',
                'fake-ip-filter', 'default-nameserver', 'nameserver', 'respect-rules',
                'proxy-server-nameserver', 'fake-ip-filter-mode', 'prefer-h3', 'use-hosts', 'use-system-hosts'}
    if (not isinstance(dns, dict) or set(dns) - dns_keys or dns.get('enable') is not True
            or dns.get('ipv6') is not False or dns.get('enhanced-mode') != 'fake-ip'
            or dns.get('respect-rules') is not True):
        raise ProbeError('managed_dns_policy_required')
    def resolver_list(values, route):
        if not isinstance(values, list) or not values:
            return False
        for value in values:
            if not isinstance(value, str) or any(ord(char) <= 32 for char in value):
                return False
            try:
                url = urlsplit(value)
                address = ipaddress.ip_address(url.hostname)
                if (url.scheme != 'https' or not address.is_global or url.path != '/dns-query'
                        or url.fragment != route or url.query or url.username or url.password
                        or url.port not in {None, 443}):
                    return False
            except (ValueError, TypeError):
                return False
        return True
    if (not resolver_list(dns.get('nameserver'), group)
            or not resolver_list(dns.get('default-nameserver'), 'DIRECT')
            or not resolver_list(dns.get('proxy-server-nameserver'), 'DIRECT')
            or dns.get('prefer-h3') is not False or dns.get('use-hosts') is not False
            or dns.get('use-system-hosts') is not False):
        raise ProbeError('fixed_route_doh_required')
    if document.get('profile', {}) not in ({}, {'store-selected': True}, {'store-selected': False},
                                           {'store-selected': True, 'store-fake-ip': True},
                                           {'store-selected': False, 'store-fake-ip': False}):
        raise ProbeError('unsupported_profile')
    return copy.deepcopy(document)


def unused_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def dns_probe(port, qtype):
    transaction = os.urandom(2)
    question = b'\x07example\x03com\x00' + struct.pack('!HH', qtype, 1)
    query = transaction + struct.pack('!HHHHH', 0x100, 1, 0, 0, 0) + question
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(3)
            client.sendto(query, ('127.0.0.1', port))
            response, _ = client.recvfrom(4096)
        if (len(response) < 12 or response[:2] != transaction
                or not response[2] & 0x80 or response[3] & 0xf):
            return False
        answers = struct.unpack('!H', response[6:8])[0]
        if qtype == 28:
            return answers == 0
        cursor = 12
        for _ in range(struct.unpack('!H', response[4:6])[0]):
            while response[cursor]:
                cursor += response[cursor] + 1
            cursor += 5
        for _ in range(answers):
            if response[cursor] & 0xc0 == 0xc0:
                cursor += 2
            else:
                while response[cursor]:
                    cursor += response[cursor] + 1
                cursor += 1
            record_type, record_class, _, size = struct.unpack('!HHIH', response[cursor:cursor + 10])
            cursor += 10
            if cursor + size > len(response):
                return False
            if qtype == 6 and record_type == 6 and record_class == 1 and size >= 22:
                return True
            if qtype == 1 and record_type == 1 and record_class == 1 and size == 4:
                return ipaddress.ip_address(response[cursor:cursor + 4]) in ipaddress.ip_network('198.18.0.0/15')
            cursor += size
    except (OSError, IndexError, struct.error, ValueError):
        pass
    return False


def clean_environment():
    return {key: value for key, value in os.environ.items()
            if key.lower() not in {'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'}}


def curl_request(proxy_port, url, *, trace=False):
    config = ('proxy = "socks5h://127.0.0.1:' + str(proxy_port) + '"\n'
              'noproxy = ""\nurl = "' + url + '"\n')
    command = ['curl', '-q', '--config', '-', '--silent', '--show-error', '--fail',
               '--connect-timeout', '5', '--max-time', '12']
    if trace:
        command += ['--write-out', '\n%{http_code}']
    else:
        command += ['--output', os.devnull, '--write-out', '%{http_code}']
    result = subprocess.run(command, input=config, capture_output=True, text=True,
                            env=clean_environment(), timeout=14)
    return result.returncode, result.stdout


def probe(subscription: Path, manifest: Path, core: Path, *, allow_network=False, node_index=0):
    if not allow_network:
        raise ProbeError('explicit_allow_network_required')
    document = validate_subscription(private_document(subscription, yaml_format=True))
    if type(node_index) is not int or not 0 <= node_index < len(document['proxies']):
        raise ProbeError('invalid_node_index')
    group_nodes = document['proxy-groups'][0]['proxies']
    group_nodes.insert(0, group_nodes.pop(node_index))
    binding = private_document(manifest)
    try:
        expected = ipaddress.ip_address(binding['expected_ip'])
        if not expected.is_global:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ProbeError('valid_binding_required') from None
    if not core.is_file() or not os.access(core, os.X_OK):
        raise ProbeError('core_executable_required')
    mixed, dns_port = unused_port(), unused_port(socket.SOCK_DGRAM)
    document.update({'mixed-port': mixed, 'allow-lan': False, 'bind-address': '127.0.0.1',
        'port': 0, 'socks-port': 0, 'redir-port': 0, 'tproxy-port': 0,
        'external-controller': '', 'external-controller-unix': '',
        'external-controller-pipe': '', 'secret': '', 'tun': {'enable': False},
        'log-level': 'silent', 'profile': {'store-selected': False, 'store-fake-ip': False}})
    document['dns']['listen'] = '127.0.0.1:' + str(dns_port)
    config = json.dumps(document).encode()
    if len(config) > 16384:
        raise ProbeError('subscription_too_large')
    result = {'passed': False, 'scope': 'isolated_mihomo_neutral_endpoints',
              'system_configuration_changed': False, 'tun_disabled_in_probe': True,
              'real_config_persisted': False, 'dns_leak_free': 'unverified',
              'claude_services_requested': False, 'node_count': len(document['proxies']),
              'tested_node_index': node_index, 'untested_node_count': len(document['proxies']) - 1}
    with tempfile.TemporaryDirectory(prefix='claude-mihomo-probe-') as directory:
        command = [str(core.resolve()), '-d', directory, '-f', '/dev/stdin']
        syntax = subprocess.run(command + ['-t'], input=config, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=clean_environment(), timeout=15)
        result['config_valid'] = syntax.returncode == 0
        if not result['config_valid']:
            return result
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, env=clean_environment(), start_new_session=True)
        try:
            deadline = time.monotonic() + 8
            descriptor = process.stdin.fileno()
            os.set_blocking(descriptor, False)
            offset = 0
            while offset < len(config) and time.monotonic() < deadline and process.poll() is None:
                if select.select([], [descriptor], [], .1)[1]:
                    try:
                        offset += os.write(descriptor, config[offset:])
                    except BlockingIOError:
                        continue
            process.stdin.close()
            if offset != len(config):
                result['core_ready'] = False
                return result
            ready = False
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    with socket.create_connection(('127.0.0.1', mixed), timeout=.2):
                        ready = True
                        break
                except OSError:
                    time.sleep(.1)
            result['core_ready'] = ready
            if not ready:
                return result
            result['dns_example_fake_ip'] = dns_probe(dns_port, 1)
            result['dns_ipv6_suppressed'] = dns_probe(dns_port, 28)
            result['dns_business_resolver_soa'] = dns_probe(dns_port, 6)
            code, trace = curl_request(mixed, 'https://www.cloudflare.com/cdn-cgi/trace', trace=True)
            body, _, status = trace.rpartition('\n')
            values = dict(line.split('=', 1) for line in body.splitlines() if '=' in line)
            result['cloudflare_reachable'] = code == 0 and status.strip() == '200' and bool(values.get('ip'))
            try:
                result['egress_matches_binding'] = result['cloudflare_reachable'] and ipaddress.ip_address(values.get('ip', '')) == expected
            except ValueError:
                result['egress_matches_binding'] = False
            for label, url in [('gstatic_http', 'http://www.gstatic.com/generate_204'),
                               ('gstatic_https', 'https://www.gstatic.com/generate_204')]:
                code, status = curl_request(mixed, url)
                result[label + '_204'] = code == 0 and status.strip() == '204'
            result['passed'] = all(result.get(key) is True for key in [
                'config_valid', 'core_ready', 'dns_example_fake_ip', 'dns_ipv6_suppressed',
                'dns_business_resolver_soa',
                'cloudflare_reachable', 'egress_matches_binding', 'gstatic_http_204', 'gstatic_https_204'])
            return result
        finally:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=3)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--subscription', required=True, type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--core', required=True, type=Path)
    parser.add_argument('--allow-network', action='store_true')
    parser.add_argument('--node-index', type=int, default=0, help='Zero-based node to select for this run')
    args = parser.parse_args(argv)
    try:
        result = probe(args.subscription, args.manifest, args.core, allow_network=args.allow_network, node_index=args.node_index)
    except ProbeError as error:
        result = {'passed': False, 'error': str(error)}
    except subprocess.TimeoutExpired:
        result = {'passed': False, 'error': 'probe_timeout'}
    except (OSError, ValueError, TypeError):
        result = {'passed': False, 'error': 'probe_failed'}
    print(json.dumps(result))
    return int(not result['passed'])


if __name__ == '__main__':
    raise SystemExit(main())
