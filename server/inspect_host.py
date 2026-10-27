#!/usr/bin/env python3
"""Export only a configured host's public Reality connection record, without applying changes."""
from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

if __package__:
    from .gateway import DomainError, valid_host, validate_gateway
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from server.gateway import DomainError, valid_host, validate_gateway

SSH_OPTIONS = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'ClearAllForwardings=yes',
               '-o', 'ForwardAgent=no', '-o', 'ForwardX11=no', '-o', 'RequestTTY=no',
               '-o', 'PermitLocalCommand=no', '-o', 'RemoteCommand=none',
               '-o', 'ControlMaster=no', '-o', 'ControlPath=none']
REMOTE_INSPECTION = '''import json, sqlite3
connection = sqlite3.connect('file:/etc/x-ui/x-ui.db?mode=ro', uri=True)
rows = connection.execute("""SELECT port,tag,json_object(
    'security',json_extract(stream_settings,'$.security'),
    'network',json_extract(stream_settings,'$.network'),
    'realitySettings',json_object(
        'serverNames',json_extract(stream_settings,'$.realitySettings.serverNames'),
        'shortIds',json_extract(stream_settings,'$.realitySettings.shortIds'),
        'publicKey',COALESCE(json_extract(stream_settings,'$.realitySettings.settings.publicKey'),
                             json_extract(stream_settings,'$.realitySettings.publicKey'))))
    FROM inbounds WHERE enable=1 AND protocol='vless' AND port=443""").fetchall()
profiles = []
for port, tag, raw in rows:
    stream = json.loads(raw)
    if stream.get('security') != 'reality' or stream.get('network') not in ('tcp', 'raw'):
        continue
    reality = stream['realitySettings']
    public_key = reality.get('settings', {}).get('publicKey') or reality.get('publicKey')
    profiles.append({'kind': 'xray-vless-reality', 'port': port, 'server_name': reality['serverNames'][0],
                     'public_key': public_key, 'short_id': reality['shortIds'][0], 'inbound_tag': tag,
                     'flow': 'xtls-rprx-vision'})
if len(profiles) != 1:
    raise ValueError('expected one Reality TCP 443 inbound')
print(json.dumps(profiles[0], separators=(',', ':')))
'''


def configured_alias(alias: str, config: Path) -> bool:
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,63}', alias):
        return False
    seen = set()
    found = False

    def visit(path: Path):
        nonlocal found
        path = path.expanduser().resolve()
        if path in seen:
            return
        if len(seen) >= 64 or path.stat().st_size > 1024 * 1024:
            raise ValueError('unsupported SSH configuration')
        seen.add(path)
        for line in path.read_text(encoding='utf-8').splitlines():
            tokens = shlex.split(line, comments=True)
            if not tokens:
                continue
            if '=' in tokens[0]:
                key, value = tokens.pop(0).split('=', 1)
                tokens = [key, value, *tokens]
            directive = tokens[0].lower()
            if directive == 'match' and any(token.lower().split('=', 1)[0] == 'exec' for token in tokens[1:]):
                raise ValueError('unsupported SSH configuration')
            if directive == 'host' and alias in tokens[1:]:
                found = True
            if directive == 'include':
                for pattern in tokens[1:]:
                    if '%' in pattern or '$' in pattern:
                        raise ValueError('unsupported SSH configuration')
                    include = Path(pattern).expanduser()
                    if not include.is_absolute():
                        include = config.parent / include
                    matches = glob.glob(str(include))
                    if len(matches) > 64:
                        raise ValueError('unsupported SSH configuration')
                    for match in sorted(matches):
                        visit(Path(match))
    visit(config)
    return found


def inspect(alias: str, config: Path) -> dict:
    if not configured_alias(alias, config):
        raise ValueError('explicit configured SSH alias required')
    command = ['ssh', *SSH_OPTIONS, '-F', str(config), '-G', alias]
    resolved = subprocess.run(command, capture_output=True, text=True, timeout=12, check=True)
    hostnames = [line.split(None, 1)[1] for line in resolved.stdout.splitlines() if line.startswith('hostname ')]
    if len(hostnames) != 1 or not valid_host(hostnames[0]):
        raise ValueError('invalid configured SSH hostname')
    remote = subprocess.run(['ssh', *SSH_OPTIONS, '-F', str(config), alias,
                             'python3 -c ' + shlex.quote(REMOTE_INSPECTION)],
                            capture_output=True, text=True, timeout=25, check=True)
    if len(remote.stdout.encode('utf-8')) > 16384:
        raise ValueError('invalid inspection response')
    gateway = validate_gateway(json.loads(remote.stdout))
    expected = {'kind', 'port', 'server_name', 'public_key', 'short_id', 'inbound_tag', 'flow'}
    if (gateway is None or set(gateway) != expected
            or gateway['kind'] != 'xray-vless-reality' or gateway['port'] != 443):
        raise ValueError('invalid inspection response')
    return {'id': alias, 'name': alias, 'host': hostnames[0], 'region': '', 'status': 'active', 'gateway': gateway}


def write_profile(path: Path, profile: dict) -> None:
    content = (json.dumps(profile, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink()
        raise


def public_options(profile: dict, ws_backup_path: Path | None = None,
                   reality_mlkem: str | None = None) -> dict:
    if ws_backup_path is None and reality_mlkem is None:
        return profile
    gateway = dict(profile['gateway'])
    if ws_backup_path is not None:
        with ws_backup_path.open('rb') as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError('invalid WS public profile')
        gateway['ws_backup'] = json.loads(raw)
    if reality_mlkem is not None:
        if reality_mlkem not in {'enabled', 'disabled'}:
            raise ValueError('invalid Reality capability')
        gateway['support_x25519mlkem768'] = reality_mlkem == 'enabled'
    return {**profile, 'gateway': validate_gateway(gateway)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Read existing Reality TCP 443 public parameters from an explicitly configured SSH alias. No configuration is applied.')
    parser.add_argument('alias')
    parser.add_argument('--output', required=True, type=Path, help='New private JSON file. Existing files are never overwritten.')
    parser.add_argument('--ws-backup-profile', type=Path,
                        help='Explicit WS public JSON object with address, port, server_name, host, path, inbound_tag and tls_termination. Metadata only; does not inspect or adopt a WS inbound.')
    parser.add_argument('--reality-mlkem', choices=('enabled', 'disabled'),
                        help='Explicit verified capability for this server. Omit for unknown; not inferred from the server version.')
    args = parser.parse_args(argv)
    try:
        profile = inspect(args.alias, Path.home() / '.ssh/config')
        profile = public_options(profile, args.ws_backup_profile, args.reality_mlkem)
        write_profile(args.output, profile)
    except (OSError, ValueError, DomainError, subprocess.SubprocessError):
        print('Inspection failed. Check the SSH alias, public parameters, optional WS JSON, and a new writable output path.', file=sys.stderr)
        return 1
    print('Public parameters saved privately. Import the file to review before saving; remote access and optional WS TLS/sync remain unverified.', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
