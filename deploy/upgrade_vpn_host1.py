#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import time
import urllib.request

CURRENT = Path('/opt/claude-switcher/current')
UNIT = Path('/etc/systemd/system/claude-switcher.service')
TUNNEL = Path('/etc/cloudflared-vpn-host1/config.yml')
BACKUPS = Path('/var/lib/claude-switcher-deploy/backups')
ADMIN_ROUTES = [
    {'hostname': 'sub.610247.xyz', 'path': r'^/admin$', 'service': 'http://127.0.0.1:8787'},
    {'hostname': 'sub.610247.xyz', 'path': r'^/api(?:/.*)?$', 'service': 'http://127.0.0.1:8787'},
]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write(path: Path, data: bytes, mode: int) -> None:
    temporary = path.with_name(path.name + '.new')
    with temporary.open('xb') as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def run(args: list[str]) -> None:
    subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=45, check=True)


def tunnel_candidate(raw: bytes) -> bytes:
    value = json.loads(raw)
    rules = value.get('ingress')
    if not isinstance(rules, list) or not rules or rules[-1] != {'service': 'http_status:404'}:
        raise ValueError('tunnel_structure')
    for wanted in ADMIN_ROUTES:
        if any(rule.get('hostname') == wanted['hostname'] and rule.get('path') == wanted['path']
               and rule != wanted for rule in rules):
            raise ValueError('tunnel_route_conflict')
    value['ingress'] = [rule for rule in ADMIN_ROUTES if rule not in rules] + rules
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


def validate(stage: Path) -> dict:
    if (stage.is_symlink() or not stage.is_dir() or stage.stat().st_uid != 0
            or stat.S_IMODE(stage.stat().st_mode) != 0o700):
        raise ValueError('stage_ownership')
    manifest = json.loads((stage / 'upgrade.json').read_bytes())
    if set(manifest) != {'release_id', 'files', 'previous_release', 'tunnel_sha256'}:
        raise ValueError('manifest_fields')
    import re
    if not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8,16}', manifest['release_id']):
        raise ValueError('release_id')
    payload = stage / 'release'
    if payload.is_symlink() or not payload.is_dir() or payload.stat().st_uid != 0 or payload.stat().st_mode & 0o022:
        raise ValueError('payload_ownership')
    actual = {}
    for path in payload.rglob('*'):
        if path.is_symlink() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
            raise ValueError('payload_ownership')
        if path.is_file():
            name = path.relative_to(stage / 'release').as_posix()
            parts = PurePosixPath(name).parts
            if len(parts) < 2 or parts[0] not in {'server', 'tests', 'deploy'}:
                raise ValueError('payload_path')
            actual[name] = sha(path.read_bytes())
        elif not path.is_dir():
            raise ValueError('payload_file_type')
    if actual != manifest['files']:
        raise ValueError('payload_hashes')
    required = {'server/__main__.py', 'server/xui_sync.py', 'server/xui_contract.py',
                'deploy/claude-switcher.service'}
    if not required <= actual.keys():
        raise ValueError('payload_incomplete')
    if not CURRENT.is_symlink() or str(CURRENT.readlink()) != manifest['previous_release']:
        raise ValueError('current_drift')
    if TUNNEL.is_symlink() or sha(TUNNEL.read_bytes()) != manifest['tunnel_sha256']:
        raise ValueError('tunnel_drift')
    return manifest


def rollback(backup: Path) -> None:
    receipt = json.loads((backup / 'receipt.json').read_bytes())
    if not CURRENT.is_symlink() or str(CURRENT.readlink()) not in (receipt['old_release'], receipt['release']):
        raise ValueError('rollback_current_drift')
    for path, key in ((UNIT, 'unit'), (TUNNEL, 'tunnel')):
        if path.is_symlink() or sha(path.read_bytes()) not in (receipt[key + '_old_sha'], receipt[key + '_sha']):
            raise ValueError('rollback_' + key + '_drift')
    for service in ('claude-switcher-sync.timer', 'claude-switcher-sync.service'):
        subprocess.run(['systemctl', 'disable' if service.endswith('.timer') else 'stop',
                        *(['--now'] if service.endswith('.timer') else []), service],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
        probe = subprocess.run(['systemctl', 'is-active', '--quiet', service],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        if probe.returncode == 0:
            raise RuntimeError('rollback_sync_still_active')
    temporary = CURRENT.with_name('current.rollback')
    temporary.symlink_to(receipt['old_release'])
    os.replace(temporary, CURRENT)
    write(UNIT, (backup / 'unit.original').read_bytes(), 0o644)
    write(TUNNEL, (backup / 'tunnel.original').read_bytes(), receipt['tunnel_mode'])
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'restart', 'claude-switcher'])
    run(['systemctl', 'restart', 'cloudflared-vpn-host1'])
    for service in ('claude-switcher', 'cloudflared-vpn-host1'):
        run(['systemctl', 'is-active', '--quiet', service])


def upgrade(stage: Path) -> dict:
    step, backup = 'validate', None
    try:
        manifest = validate(stage)
        release = CURRENT.parent / 'releases' / manifest['release_id']
        if release.exists():
            raise ValueError('release_exists')
        original = TUNNEL.read_bytes()
        candidate = tunnel_candidate(original)
        draft = stage / 'tunnel.draft.json'
        write(draft, candidate, 0o600)
        run(['/usr/local/bin/cloudflared', 'tunnel', '--config', str(draft), 'ingress', 'validate'])
        backup = BACKUPS / manifest['release_id']
        backup.mkdir(mode=0o700)
        write(backup / 'unit.original', UNIT.read_bytes(), 0o600)
        write(backup / 'tunnel.original', original, 0o600)
        write(backup / 'rollback.py', Path(__file__).read_bytes(), 0o700)
        new_unit = (stage / 'release/deploy/claude-switcher.service').read_bytes()
        receipt = {'old_release': manifest['previous_release'], 'release': str(release),
                   'unit_old_sha': sha(UNIT.read_bytes()), 'unit_sha': sha(new_unit),
                   'tunnel_old_sha': sha(original), 'tunnel_sha': sha(candidate),
                   'tunnel_mode': stat.S_IMODE(TUNNEL.stat().st_mode)}
        write(backup / 'receipt.json', json.dumps(receipt).encode(), 0o600)
        step = 'install'
        shutil.copytree(stage / 'release', release)
        for path in [release, *release.rglob('*')]:
            os.chown(path, 0, 0)
            path.chmod(0o755 if path.is_dir() else 0o644)
        validate(stage)
        temporary = CURRENT.with_name('current.new')
        temporary.symlink_to(release)
        os.replace(temporary, CURRENT)
        write(UNIT, new_unit, 0o644)
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'restart', 'claude-switcher'])
        step = 'health'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for attempt in range(40):
            try:
                with opener.open('http://127.0.0.1:8787/healthz', timeout=2) as response:
                    if response.status == 200 and json.load(response) == {'status': 'ok'}:
                        break
            except (OSError, ValueError):
                pass
            time.sleep(.25)
        else:
            raise RuntimeError('backend_unhealthy')
        step = 'public_routes'
        if sha(TUNNEL.read_bytes()) != manifest['tunnel_sha256']:
            raise ValueError('tunnel_drift')
        write(TUNNEL, candidate, receipt['tunnel_mode'])
        run(['systemctl', 'restart', 'cloudflared-vpn-host1'])
        for service in ('claude-switcher', 'cloudflared-vpn-host1', 'x-ui', 'clash-subscription'):
            run(['systemctl', 'is-active', '--quiet', service])
        return {'status': 'upgraded', 'release_id': manifest['release_id'], 'rollback_available': True}
    except Exception:
        restored = False
        if backup and (backup / 'receipt.json').exists():
            try:
                rollback(backup)
                restored = True
            except Exception:
                pass
        return {'status': 'failed', 'step': step, 'rolled_back': restored}


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--stage', type=Path)
    group.add_argument('--rollback', type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        result = {'status': 'failed', 'step': 'root_required'}
    elif args.stage:
        result = upgrade(args.stage)
    else:
        try:
            rollback(args.rollback)
            result = {'status': 'rolled_back', 'database_and_login_preserved': True}
        except Exception:
            result = {'status': 'failed', 'step': 'rollback'}
    print(json.dumps(result))
    return int(result['status'] == 'failed')


if __name__ == '__main__':
    raise SystemExit(main())
