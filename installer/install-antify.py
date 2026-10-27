#!/usr/bin/env python3
import argparse
from contextlib import contextmanager
from pathlib import Path
import platform
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile


DMG_URL = "https://r2.antifyapp.com/releases/latest/Antify.dmg"
BUNDLE_ID = "com.lester.Antify"
TEAM_ID = "H6PU3TBL97"
MIN_VERSION = (0, 0, 82)
ANTIFY = Path("/Applications/Antify.app")
APP_NAME = "ClaudeCode环境检测.app"


class InstallError(Exception):
    pass


def run(args, message, timeout=30):
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        raise InstallError(message) from None
    if result.returncode != 0:
        raise InstallError(message)
    return result


def verify_antify(app):
    try:
        with (app / "Contents/Info.plist").open("rb") as handle:
            info = plistlib.load(handle)
        raw_version = info.get("CFBundleShortVersionString", "")
        if (info.get("CFBundleIdentifier") != BUNDLE_ID or not isinstance(raw_version, str)
                or not re.fullmatch(r"\d+\.\d+\.\d+", raw_version)):
            raise ValueError()
        version = tuple(map(int, raw_version.split(".")))
    except (OSError, ValueError, TypeError, plistlib.InvalidFileException):
        raise InstallError("Antify 应用标识或版本无效，已停止安装") from None
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], "Antify 签名验证失败，已停止安装")
    identity = run(["/usr/bin/codesign", "-dv", "--verbose=4", str(app)], "无法读取 Antify 开发者签名")
    if ("TeamIdentifier=" + TEAM_ID).encode() not in identity.stderr.splitlines():
        raise InstallError("Antify 开发者签名不匹配，已停止安装")
    run(["/usr/sbin/spctl", "--assess", "--type", "execute", str(app)], "Antify 未通过 Gatekeeper 验证，已停止安装", timeout=60)
    return version


def needs_antify(app):
    if app.is_symlink():
        raise InstallError("Antify 安装路径是符号链接，请先人工检查")
    if not app.exists():
        return True
    if verify_antify(app) >= MIN_VERSION:
        return False
    try:
        result = subprocess.run(["/usr/bin/pgrep", "-f", r"^/Applications/Antify\.app/Contents/MacOS/"],
                                capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        raise InstallError("无法确认 Antify 是否退出，请使用应用内更新") from None
    if result.returncode == 0:
        raise InstallError("旧版 Antify 正在运行，请先退出或使用应用内更新，然后重试")
    if result.returncode != 1:
        raise InstallError("无法确认 Antify 是否退出，请使用应用内更新")
    return True


@contextmanager
def official_candidate(stage):
    dmg = stage / "Antify.dmg"
    run(["/usr/bin/curl", "--fail", "--location", "--proto", "=https", "--proto-redir", "=https",
         "--connect-timeout", "20", "--max-time", "180", "--output", str(dmg), DMG_URL],
        "Antify 官方下载失败，未更改已安装文件", timeout=190)
    result = run(["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen", "-plist", str(dmg)],
                 "无法只读挂载 Antify 安装镜像")
    mounts = []
    try:
        try:
            entities = plistlib.loads(result.stdout)["system-entities"]
            mounts = [Path(item["mount-point"]) for item in entities if "mount-point" in item]
            candidates = [mount / "Antify.app" for mount in mounts if (mount / "Antify.app").is_dir()]
            if len(candidates) != 1 or candidates[0].is_symlink():
                raise ValueError()
        except (KeyError, TypeError, ValueError, plistlib.InvalidFileException):
            raise InstallError("Antify 镜像内容不符合预期，已停止安装") from None
        if verify_antify(candidates[0]) < MIN_VERSION:
            raise InstallError("官方镜像版本低于 0.0.82，已停止安装")
        yield candidates[0]
    finally:
        for mount in mounts:
            try:
                run(["/usr/bin/hdiutil", "detach", str(mount)], "Antify 安装镜像未能卸载，请在 Finder 中推出")
            except InstallError:
                print("Antify 安装镜像未能自动卸载，请在 Finder 中推出。", file=sys.stderr)


def build_code_app(repo, stage):
    app = stage / APP_NAME
    executable = app / "Contents/MacOS/ClaudeCodeCheck"
    executable.parent.mkdir(parents=True)
    sdk = run(["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"], "缺少 Xcode 命令行工具，请先安装").stdout.decode().strip()
    arch = platform.machine()
    if arch not in ("arm64", "x86_64"):
        raise InstallError("不支持当前 Mac 架构")
    compiler = shutil.which("swiftc")
    if not compiler:
        raise InstallError("缺少 Swift 编译器，请先安装 Xcode 命令行工具")
    run([compiler, "-sdk", sdk, "-module-cache-path", str(stage / "module-cache"), "-O", "-parse-as-library", "-target", arch + "-apple-macos14.0",
         str(repo / "app/main.swift"), "-o", str(executable), "-framework", "SwiftUI", "-framework", "AppKit"],
        "Code 检测 App 编译失败，未更改已安装文件", timeout=180)
    info = {"CFBundleExecutable": "ClaudeCodeCheck", "CFBundleIdentifier": "local.claude-switcher.code-check",
            "CFBundleName": "Claude Code 环境检测", "CFBundlePackageType": "APPL", "CFBundleVersion": "1",
            "CFBundleShortVersionString": "1.0", "LSMinimumSystemVersion": "14.0", "ClaudeCodeOnly": True,
            "NSHighResolutionCapable": True}
    with (app / "Contents/Info.plist").open("wb") as handle:
        plistlib.dump(info, handle)
    run(["/usr/bin/codesign", "--force", "--deep", "-s", "-", str(app)], "Code 检测 App 签名失败")
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], "Code 检测 App 校验失败")
    tools = stage / "code-tools"
    tools.mkdir(mode=0o700)
    for name in ("claude-switch.sh", "claude-code.py"):
        shutil.copy2(repo / "core" / name, tools / name)
        (tools / name).chmod(0o700)
    run(["/bin/bash", "-n", str(tools / "claude-switch.sh")], "Code 检测脚本语法校验失败")
    compile((tools / "claude-code.py").read_bytes(), "claude-code.py", "exec")
    return app, tools


CANCELLATION_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


@contextmanager
def block_cancellation():
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, CANCELLATION_SIGNALS)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def ignore_cancellation():
    with block_cancellation():
        for sig in CANCELLATION_SIGNALS:
            signal.signal(sig, signal.SIG_IGN)


def publish(entries):
    previous = {sig: signal.getsignal(sig) for sig in CANCELLATION_SIGNALS}
    cancelled = False

    def cancel(sig, frame):
        nonlocal cancelled
        if cancelled:
            return
        cancelled = True
        raise InstallError("安装已中断，已尝试恢复原文件")

    try:
        with block_cancellation():
            for sig in CANCELLATION_SIGNALS:
                signal.signal(sig, cancel)
        publish_transaction(entries)
    finally:
        with block_cancellation():
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def publish_transaction(entries):
    staged = []
    try:
        for source, target, verify in entries:
            if target.exists() and not target.is_dir():
                raise InstallError("安装目标不是应用或工具目录，已停止安装")
            if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
                raise InstallError("安装目标含符号链接，已停止安装")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory = Path(tempfile.mkdtemp(prefix=".claude-code-install-", dir=target.parent))
            staged.append((directory, target, verify, False, False))
            candidate = directory / "candidate"
            run(["/usr/bin/ditto", str(source), str(candidate)], "无法准备安装文件，请检查目标目录写入权限")
            verify(candidate)
        for index, (directory, target, verify, _, _) in enumerate(staged):
            with block_cancellation():
                existed = target.exists()
                if existed:
                    target.rename(directory / "backup")
                staged[index] = (directory, target, verify, existed, False)
                (directory / "candidate").rename(target)
                staged[index] = (directory, target, verify, existed, True)
            verify(target)
        ignore_cancellation()
    except BaseException:
        ignore_cancellation()
        rollback_failed = False
        for directory, target, _, backed_up, installed in reversed(staged):
            try:
                if installed and target.exists():
                    shutil.rmtree(target)
                if backed_up:
                    (directory / "backup").rename(target)
            except OSError:
                rollback_failed = True
        if rollback_failed:
            raise InstallError("安装失败且自动回滚未完成。原文件保留在目标目录旁的 .claude-code-install-* /backup 中，请人工恢复") from None
        raise
    else:
        for directory, _, _, backed_up, _ in staged:
            if backed_up:
                shutil.rmtree(directory / "backup", ignore_errors=True)
    finally:
        for directory, _, _, _, _ in staged:
            if not (directory / "backup").exists():
                shutil.rmtree(directory, ignore_errors=True)


def verify_code_app(path):
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(path)], "Code 检测 App 安装验证失败")


def verify_tools(path):
    if not all((path / name).is_file() for name in ("claude-switch.sh", "claude-code.py")):
        raise InstallError("Code 检测工具安装不完整")


def install(repo, home, dry_run=False):
    if platform.system() != "Darwin" or int(platform.mac_ver()[0].split(".")[0] or "0") < 14:
        raise InstallError("Antify 检测版需要 macOS 14 或更高版本")
    for name in ("app/main.swift", "core/claude-switch.sh", "core/claude-code.py"):
        if not (repo / name).is_file():
            raise InstallError("源码不完整，请使用完整仓库安装")
    if dry_run:
        print("预演完成。正式安装将验证官方 Antify、编译独立检测 App 并安装到桌面。")
        print("预演未下载、启动或安装应用，未修改系统时区、网络和后台服务。")
        return
    required = needs_antify(ANTIFY)
    with tempfile.TemporaryDirectory(prefix="claude-antify-install-") as directory:
        stage = Path(directory)
        app, tools = build_code_app(repo, stage)
        entries = [(tools, home / ".claude-switcher/code-tools", verify_tools),
                   (app, home / "Desktop" / APP_NAME, verify_code_app)]
        if required:
            with official_candidate(stage) as candidate:
                publish([(candidate, ANTIFY, verify_antify)] + entries)
        else:
            publish(entries)
    print("Antify 与独立 Claude Code 环境检测 App 已安装。系统时区、网络和原桌面切换器保持原状。")
    print("待完成：在 Antify 批准系统扩展和内容过滤器，配置命令行规则，启用并实测 Kill Switch，核验 DNS 与实际出口。")
    print("本检测不启动 Claude Code，也不把扩展已批准视为代理运行或防泄漏通过。更新时重新运行此安装器。")
    try:
        run(["/usr/bin/open", str(ANTIFY)], "Antify 未能自动打开")
        run(["/usr/bin/open", str(home / "Desktop" / APP_NAME)], "检测 App 未能自动打开")
    except InstallError:
        print("应用已安装，请从应用程序和桌面手动打开。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        install(Path(__file__).resolve().parents[1], Path.home(), args.dry_run)
    except (InstallError, OSError, SyntaxError):
        error = sys.exc_info()[1]
        print(str(error) if isinstance(error, InstallError) else "安装失败，请检查磁盘空间、文件和目标目录权限。已有应用已尝试回滚。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
