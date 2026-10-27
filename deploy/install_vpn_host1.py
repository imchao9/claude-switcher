#!/usr/bin/env python3
"""First installation only. All subprocess output is discarded, never logged."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.request

APP = "claude-switcher"
UNIT = Path("/etc/systemd/system/claude-switcher.service")
CURRENT = Path("/opt/claude-switcher/current")
STATE = Path("/var/lib/claude-switcher")
AUTH = Path("/etc/claude-switcher")
TUNNEL = Path("/etc/cloudflared-vpn-host1/config.yml")
BACKUPS = Path("/var/lib/claude-switcher-deploy/backups")
ROUTES = [
    {"hostname": "sub.610247.xyz", "path": r"^/sub/cs_[A-Za-z0-9_-]+/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\.yaml$", "service": "http://127.0.0.1:8787"},
    {"hostname": "sub.610247.xyz", "path": r"^/s/cs_[A-Za-z0-9_-]+$", "service": "http://127.0.0.1:8787"},
]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def database_hash(path: Path) -> str:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("database_integrity")
        return sha("\n".join(connection.iterdump()).encode("utf-8"))


def validate_payload(stage: Path) -> dict:
    if stage.is_symlink() or not stage.is_dir() or stat.S_IMODE(stage.stat().st_mode) != 0o700:
        raise ValueError("stage_permissions")
    manifest = json.loads((stage / "manifest.json").read_bytes())
    if set(manifest) != {"release_id", "files", "database_logical_sha256", "tunnel_sha256"}:
        raise ValueError("manifest_fields")
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8,16}", manifest["release_id"]):
        raise ValueError("release_id")
    actual = {}
    for path in stage.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("payload_file_type")
        if path.is_file() and path != stage / "manifest.json":
            actual[path.relative_to(stage).as_posix()] = sha(path.read_bytes())
    expected = manifest["files"]
    if not isinstance(expected, dict) or actual != expected:
        raise ValueError("payload_hashes")
    for name in expected:
        parts = PurePosixPath(name).parts
        if (not parts or any(p in ("", ".", "..") for p in parts)
                or not (name in ("state.db", "admin-auth.json")
                        or len(parts) > 2 and parts[:2] in (("release", "server"), ("release", "tests"), ("release", "deploy")))):
            raise ValueError("payload_path")
    required = {"state.db", "admin-auth.json", "release/server/__main__.py",
                "release/deploy/claude-switcher.service", "release/deploy/install_vpn_host1.py"}
    if not required <= expected.keys() or database_hash(stage / "state.db") != manifest["database_logical_sha256"]:
        raise ValueError("payload_database")
    sys.path.insert(0, str(stage / "release"))
    from server.admin_auth import load_admin_auth_file
    load_admin_auth_file(stage / "admin-auth.json")
    return manifest


def tunnel_candidate(raw: bytes) -> bytes:
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get("ingress"), list) or not value["ingress"]:
        raise ValueError("tunnel_structure")
    if any(rule in value["ingress"] for rule in ROUTES):
        raise ValueError("tunnel_already_configured")
    value["ingress"] = ROUTES + value["ingress"]
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def run(arguments: list[str]) -> None:
    result = subprocess.run(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
    if result.returncode:
        raise RuntimeError("command_failed")


def write(path: Path, data: bytes, mode: int) -> None:
    temporary = path.with_name(path.name + ".new")
    with temporary.open("xb") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def receipt_write(backup: Path, receipt: dict) -> None:
    write(backup / "receipt.json", json.dumps(receipt).encode(), 0o600)


def rollback(backup: Path) -> None:
    receipt = json.loads((backup / "receipt.json").read_bytes())
    if receipt.get("unit") and (UNIT.exists() or UNIT.is_symlink()):
        if UNIT.is_symlink() or not UNIT.is_file() or sha(UNIT.read_bytes()) != receipt["unit_sha256"]:
            raise RuntimeError("rollback_unit_drift")
    if receipt.get("current") and (CURRENT.exists() or CURRENT.is_symlink()):
        if not CURRENT.is_symlink() or str(CURRENT.readlink()) != receipt["release"]:
            raise RuntimeError("rollback_current_drift")
    if receipt.get("tunnel"):
        current_hash = sha(TUNNEL.read_bytes())
        if current_hash not in (receipt["tunnel_installed_sha256"], receipt["tunnel_original_sha256"]):
            raise RuntimeError("rollback_tunnel_drift")
    if receipt.get("unit"):
        if UNIT.exists():
            run(["systemctl", "disable", "--now", APP])
            UNIT.unlink()
            run(["systemctl", "daemon-reload"])
    if receipt.get("current") and CURRENT.is_symlink():
        CURRENT.unlink()
    if receipt.get("tunnel"):
        current_hash = sha(TUNNEL.read_bytes())
        if current_hash == receipt["tunnel_installed_sha256"]:
            write(TUNNEL, (backup / "tunnel.original").read_bytes(), receipt["tunnel_mode"])
            os.chown(TUNNEL, receipt["tunnel_uid"], receipt["tunnel_gid"])
        if current_hash == receipt["tunnel_installed_sha256"] or not receipt.get("tunnel_rollback_complete"):
            run(["systemctl", "restart", "cloudflared-vpn-host1"])
            run(["systemctl", "is-active", "--quiet", "cloudflared-vpn-host1"])
            receipt["tunnel_rollback_complete"] = True
            receipt_write(backup, receipt)


def install(stage: Path) -> dict:
    step, backup = "validate_payload", None
    try:
        manifest = validate_payload(stage)
        step = "preflight"
        release = Path("/opt/claude-switcher/releases") / manifest["release_id"]
        if any(path.exists() or path.is_symlink() for path in (UNIT, CURRENT, STATE, AUTH, release)):
            raise ValueError("existing_installation")
        loaded = subprocess.run(["systemctl", "show", "--property=LoadState", "--value", APP],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
        if loaded.returncode or loaded.stdout.strip() != b"not-found":
            raise ValueError("existing_service")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 8787))
        if TUNNEL.is_symlink() or sha(TUNNEL.read_bytes()) != manifest["tunnel_sha256"]:
            raise ValueError("tunnel_drift")
        original = TUNNEL.read_bytes()
        candidate = tunnel_candidate(original)
        cloudflared = shutil.which("cloudflared")
        if cloudflared is None:
            raise ValueError("cloudflared_missing")
        draft = stage / "tunnel.draft.json"
        write(draft, candidate, 0o600)
        step = "validate_tunnel"
        run([cloudflared, "tunnel", "--config", str(draft), "ingress", "validate"])
        step = "backup"
        BACKUPS.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = BACKUPS / manifest["release_id"]
        backup.mkdir(mode=0o700)
        write(backup / "tunnel.original", original, 0o600)
        write(backup / "rollback.py", Path(__file__).read_bytes(), 0o700)
        info = TUNNEL.stat()
        unit_bytes = (stage / "release/deploy/claude-switcher.service").read_bytes()
        receipt = {"release": str(release), "unit_sha256": sha(unit_bytes), "unit": False, "current": False,
                   "tunnel": False, "tunnel_original_sha256": sha(original), "tunnel_installed_sha256": sha(candidate),
                   "tunnel_mode": stat.S_IMODE(info.st_mode), "tunnel_uid": info.st_uid, "tunnel_gid": info.st_gid}
        receipt_write(backup, receipt)
        step = "install_files"
        try:
            account = pwd.getpwnam(APP)
            if account.pw_shell not in ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false"):
                raise ValueError("existing_account_not_system")
        except KeyError:
            run(["useradd", "--system", "--user-group", "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin", APP])
            account = pwd.getpwnam(APP)
        release.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        release.parent.parent.chmod(0o755)
        release.parent.chmod(0o755)
        shutil.copytree(stage / "release", release)
        for path in [release, *release.rglob("*")]:
            os.chown(path, 0, 0)
            path.chmod(0o755 if path.is_dir() else 0o644)
        for directory, source, filename in ((STATE, "state.db", "subscriptions.db"), (AUTH, "admin-auth.json", "admin-auth.json")):
            directory.mkdir(mode=0o700)
            os.chown(directory, account.pw_uid, account.pw_gid)
            write(directory / filename, (stage / source).read_bytes(), 0o600)
            os.chown(directory / filename, account.pw_uid, account.pw_gid)
        receipt["current"] = True
        receipt_write(backup, receipt)
        CURRENT.symlink_to(release)
        receipt["unit"] = True
        receipt_write(backup, receipt)
        write(UNIT, unit_bytes, 0o644)
        step = "start_backend"
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "enable", "--now", APP])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for attempt in range(30):
            try:
                with opener.open("http://127.0.0.1:8787/healthz", timeout=2) as response:
                    if response.status == 200 and json.loads(response.read(1024)) == {"status": "ok"}:
                        break
            except (OSError, ValueError):
                pass
            time.sleep(0.25)
        else:
            raise RuntimeError("backend_unhealthy")
        step = "activate_tunnel"
        if sha(TUNNEL.read_bytes()) != sha(original):
            raise ValueError("tunnel_drift")
        receipt["tunnel"] = True
        receipt_write(backup, receipt)
        write(TUNNEL, candidate, stat.S_IMODE(info.st_mode))
        os.chown(TUNNEL, info.st_uid, info.st_gid)
        run(["systemctl", "restart", "cloudflared-vpn-host1"])
        for service in ("cloudflared-vpn-host1", APP):
            run(["systemctl", "is-active", "--quiet", service])
        return {"status": "installed", "release_id": manifest["release_id"], "rollback_available": True}
    except Exception:
        rolled_back = False
        if backup is not None and (backup / "receipt.json").exists():
            try:
                rollback(backup)
                rolled_back = True
            except Exception:
                pass
        return {"status": "failed", "step": step, "rolled_back": rolled_back}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--rollback", type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        print(json.dumps({"status": "failed", "step": "root_required"}))
        return 1
    if args.rollback or (Path(__file__).name == "rollback.py" and not args.stage):
        try:
            rollback(args.rollback or Path(__file__).resolve().parent)
            result = {"status": "rolled_back", "data_preserved": True}
        except Exception:
            result = {"status": "failed", "step": "rollback"}
    elif args.stage:
        result = install(args.stage)
    else:
        result = {"status": "failed", "step": "stage_required"}
    print(json.dumps(result))
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
