#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import time


def connection_probe(database: Path, server_id: str, user_id: str, *, expect_disabled: bool = False) -> dict:
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('BEGIN')
        server = connection.execute('SELECT * FROM servers WHERE id=?', (server_id,)).fetchone()
        user = connection.execute('''SELECT u.ingress_uuid,u.status,i.address FROM users u
            LEFT JOIN bindings b ON b.user_id=u.id LEFT JOIN ips i ON i.id=b.ip_id WHERE u.id=?''', (user_id,)).fetchone()
        if not server or not user:
            raise ValueError('unknown_target')
        gateway = json.loads(server['gateway_json'])
        if gateway['kind'] != 'xray-vless-reality':
            raise ValueError('reality_required')
        if expect_disabled != (user['status'] == 'disabled'):
            raise ValueError('unexpected_identity_status')
        expected_address = user['address']
        identity = user['ingress_uuid']
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    config = {'log': {'loglevel': 'none'}, 'inbounds': [{
        'tag': 'probe-socks', 'listen': '127.0.0.1', 'port': port, 'protocol': 'socks',
        'settings': {'auth': 'noauth', 'udp': False}}], 'outbounds': [{
        'tag': 'probe-reality', 'protocol': 'vless', 'settings': {'vnext': [{
            'address': server['host'], 'port': gateway['port'],
            'users': [{'id': identity, 'encryption': 'none', 'flow': gateway['flow']}]}]},
        'streamSettings': {'network': 'tcp', 'security': 'reality', 'realitySettings': {
            'fingerprint': 'chrome', 'serverName': gateway['server_name'],
            'publicKey': gateway['public_key'], 'shortId': gateway['short_id']}}}]}
    command = ['/usr/local/x-ui/bin/xray-linux-amd64', 'run', '-format', 'json', '-config', '/dev/stdin']
    test = subprocess.run(command + ['-test'], input=json.dumps(config).encode(),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    if test.returncode:
        return {'client_config_valid': False, 'passed': False}
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        process.stdin.write(json.dumps(config).encode())
        process.stdin.close()
        for attempt in range(60):
            if process.poll() is not None:
                return {'client_config_valid': True, 'client_started': False, 'passed': False}
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.2):
                    break
            except OSError:
                time.sleep(.1)
        else:
            return {'client_config_valid': True, 'client_started': False, 'passed': False}
        curl = subprocess.run(['curl', '-q', '--config', '-'], input=(
            'proxy = "socks5h://127.0.0.1:' + str(port) + '"\n'
            'noproxy = ""\nurl = "https://api.ipify.org"\n'
            'silent\nshow-error\nconnect-timeout = 8\nmax-time = 20\n').encode(),
            capture_output=True, timeout=25)
        connected = curl.returncode == 0 and bool(curl.stdout.strip())
        matched = connected and curl.stdout.strip().decode('ascii', errors='replace') == expected_address
        return {'client_config_valid': True, 'client_started': True,
                'connected': connected, 'egress_matches_binding': matched,
                'passed': matched if not expect_disabled else False}
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def probe(database: Path, server_id: str, user_id: str, *, expect_disabled: bool = False,
          control_user_id: str | None = None) -> dict:
    if expect_disabled and (not control_user_id or control_user_id == user_id):
        raise ValueError('distinct_enabled_control_required')
    result = connection_probe(database, server_id, user_id, expect_disabled=expect_disabled)
    if expect_disabled:
        control = connection_probe(database, server_id, control_user_id)
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            identity = db.execute('SELECT ingress_uuid FROM users WHERE id=?', (user_id,)).fetchone()[0]
        runtime = json.loads(Path('/usr/local/x-ui/bin/config.json').read_bytes())
        absent = all(client.get('id') != identity for inbound in runtime.get('inbounds', [])
                     for client in inbound.get('settings', {}).get('clients',
                         inbound.get('settings', {}).get('users', [])))
        rejected = (result.get('client_started') is True and result.get('connected') is False
                    and control.get('passed') is True and absent)
        result.update(enabled_control_passed=control.get('passed') is True,
                      disabled_identity_absent=absent, disabled_connection_rejected=rejected, passed=rejected)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', type=Path, default=Path('/var/lib/claude-switcher/subscriptions.db'))
    parser.add_argument('--server-id', required=True)
    parser.add_argument('--user-id', required=True)
    parser.add_argument('--expect-disabled', action='store_true')
    parser.add_argument('--control-user-id')
    args = parser.parse_args()
    try:
        result = probe(args.db, args.server_id, args.user_id, expect_disabled=args.expect_disabled,
                       control_user_id=args.control_user_id)
    except Exception as error:
        result = {'passed': False, 'error_type': type(error).__name__}
    print(json.dumps(result))
    return int(not result['passed'])


if __name__ == '__main__':
    raise SystemExit(main())
