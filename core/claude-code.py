#!/usr/bin/env python3
"""Run one Claude Code process group with bounded network health checks."""
import argparse
import json
import os
import plistlib
import re
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import termios
import time


HEALTH_TIMEOUT = 15
HEALTH_INTERVAL = 5
STOP_TIMEOUT = 2
INSTALL_TIMEOUT = 240
INSTALL_SCRIPT_LIMIT = 1024 * 1024
INSTALL_URL = "https://claude.ai/install.sh"
INSTALL_DETAILS = {
    "installed": "Claude Code 已安装；首次使用请自行登录",
    "already_present": "已找到 Claude Code；保留现有安装，未执行或验证安装来源",
    "failed": "Claude Code 安装未完成；请检查网络后重试，现有或部分安装未删除",
    "cancelled": "Claude Code 安装已中断；现有或部分安装未删除",
}
VERSION_PATTERN = r"[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}(?:-[A-Za-z0-9.-]{1,40})?(?: \(Claude Code\))?"
AUTH_KEYS = {
    "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_API_KEY_HELPER_TTL_MS", "GOOGLE_APPLICATION_CREDENTIALS",
    "CLOUD_ML_REGION", "ANTHROPIC_VERTEX_PROJECT_ID",
}
AUTH_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_", "AWS_", "GOOGLE_CLOUD_", "GCLOUD_")


class UsageError(ValueError):
    pass


class Terminated(BaseException):
    def __init__(self, sig):
        self.sig = sig


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError("参数无效。用法: code install [--json]、code check [A|B] 或 code run [A|B] -- [Claude 参数]")


def parse(argv):
    parser = Parser(description=__doc__)
    parser.add_argument("shell")
    parser.add_argument("action", choices=("check", "run", "install"))
    parser.add_argument("profile", nargs="?", default=None, choices=("default", "A", "B"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--timezone")
    parser.add_argument("--route", choices=("tun", "antify"))
    split = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:split])
    if args.action == "install" and (args.profile is not None or args.route is not None
                                     or args.timezone is not None or split < len(argv)):
        raise UsageError("code install 仅接受可选的 --json")
    args.profile = args.profile or "default"
    args.route = args.route or "tun"
    if args.timezone is not None:
        validate_timezone(args.timezone)
    args.command = argv[split + 1:] if split < len(argv) else []
    if args.action == "check" and split < len(argv):
        raise UsageError("code check 不接收 Claude 参数")
    if args.json and args.action not in ("check", "install"):
        raise UsageError("--json 仅用于 code check 或 code install")
    return args


def validate_timezone(name):
    if not re.fullmatch(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*", name):
        raise UsageError("--timezone 需要有效的 IANA 时区")
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        try:
            valid = (Path("/usr/share/zoneinfo") / name).read_bytes().startswith(b"TZif")
        except OSError:
            valid = False
    else:
        try:
            ZoneInfo(name)
            valid = True
        except (ValueError, KeyError):
            valid = False
    if not valid:
        raise UsageError("--timezone 需要有效的 IANA 时区")


def find_cli():
    for candidate in (shutil.which("claude"), str(Path.home() / ".local/bin/claude")):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return os.path.abspath(candidate)
    return None


class InstallCancelled(Exception):
    pass


def install_child(command, deadline, parent, capture=False, script=None):
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    output = bytearray()
    try:
        if capture:
            os.set_blocking(process.stdout.fileno(), False)
        while True:
            if os.getppid() != parent:
                raise InstallCancelled()
            if time.monotonic() >= deadline:
                raise TimeoutError()
            if script is not None and script.exists() and script.stat().st_size > INSTALL_SCRIPT_LIMIT:
                raise ValueError()
            if capture:
                try:
                    chunk = os.read(process.stdout.fileno(), 4097 - len(output))
                except BlockingIOError:
                    chunk = b""
                output.extend(chunk)
                if len(output) > 4096:
                    raise ValueError()
            if process.poll() is not None:
                if capture:
                    while True:
                        try:
                            chunk = os.read(process.stdout.fileno(), 4097 - len(output))
                        except BlockingIOError:
                            break
                        if not chunk:
                            break
                        output.extend(chunk)
                        if len(output) > 4096:
                            raise ValueError()
                if process.returncode != 0:
                    raise ValueError()
                return bytes(output)
            time.sleep(0.05)
    finally:
        signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)
        previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in signals}
        try:
            stop_group(process.pid)
            process.wait()
            if process.stdout is not None:
                process.stdout.close()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def install_cli():
    version = None
    try:
        if find_cli() is not None:
            status = "already_present"
        else:
            parent = os.getppid()
            if parent == 1:
                raise InstallCancelled()
            deadline = time.monotonic() + INSTALL_TIMEOUT
            with tempfile.TemporaryDirectory(prefix="claude-official-install-") as temporary:
                script = Path(temporary) / "install.sh"
                install_child(["curl", "-q", "--fail", "--silent", "--show-error", "--location",
                               "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "15",
                               "--max-time", "45", "--max-filesize", str(INSTALL_SCRIPT_LIMIT),
                               "--output", str(script), INSTALL_URL], min(deadline, time.monotonic() + 50),
                              parent, script=script)
                if not script.is_file() or not 0 < script.stat().st_size <= INSTALL_SCRIPT_LIMIT:
                    raise ValueError()
                install_child(["/bin/bash", str(script)], deadline, parent)
                executable = find_cli()
                if executable is None:
                    raise ValueError()
                raw = install_child([executable, "--version"], min(deadline, time.monotonic() + 10),
                                    parent, capture=True).decode("ascii").strip()
                if not re.fullmatch(VERSION_PATTERN, raw):
                    raise ValueError()
                version = raw
            status = "installed"
    except (InstallCancelled, Terminated, KeyboardInterrupt):
        status = "cancelled"
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
        status = "failed"
    return {"schema_version": 1, "kind": "claude_code_install", "status": status,
            "detail": INSTALL_DETAILS[status], "version": version}


def profile_path(profile, create=False):
    if profile == "default":
        return None
    if any(value and (key in AUTH_KEYS or key.startswith(AUTH_PREFIXES))
           for key, value in os.environ.items()):
        raise UsageError("独立账号不接受继承的认证、提供商或 CLAUDE_CONFIG_DIR 环境变量，请在干净的终端环境中重试")
    home = Path.home()
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate((".claude-switcher", "code", profile)):
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    return home / ".claude-switcher" / "code" / profile
                os.mkdir(part, 0o700, dir_fd=fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            info = os.fstat(child)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                os.close(child)
                raise UsageError("账号配置目录所有者或权限不安全")
            if index > 0:
                if create:
                    os.fchmod(child, 0o700)
                elif stat.S_IMODE(info.st_mode) != 0o700:
                    os.close(child)
                    raise UsageError("独立账号目录必须为 700 权限")
            os.close(fd)
            fd = child
    except OSError:
        raise UsageError("无法安全访问独立账号目录，请检查目录和符号链接") from None
    finally:
        os.close(fd)
    return home / ".claude-switcher" / "code" / profile


def group_signal(pid, sig):
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        # Darwin returns EPERM for groups containing only unreaped zombies.
        pass


def stop_group(pid):
    group_signal(pid, signal.SIGTERM)
    group_signal(pid, signal.SIGCONT)
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.05)
    group_signal(pid, signal.SIGKILL)


class Health:
    def __init__(self, shell, report=False):
        self.output = b""
        self.process = subprocess.Popen(
            ["/bin/bash", shell, "code-health", *( ["report"] if report else [])], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        self.deadline = time.monotonic() + HEALTH_TIMEOUT

    def result(self):
        if self.process.poll() is None:
            if time.monotonic() < self.deadline:
                return None
            self.close()
            return False
        self.output = self.process.communicate()[0][:8192]
        return self.process.returncode == 0 and self.output == b"proxy=ok\ntun=ok\ncountry=ok\nsubscription=ok\n"

    def close(self):
        stop_group(self.process.pid)
        self.process.wait()
        self.process.stdout.close()


def check(shell):
    probe = Health(shell)
    try:
        while True:
            result = probe.result()
            if result is not None:
                return result
            time.sleep(0.05)
    finally:
        probe.close()


def profile_checks(profile, timezone=None):
    checks = []

    def add(key, status, detail):
        checks.append({"id": key, "status": status, "detail": detail})

    found = find_cli() is not None
    add("cli", "ok" if found else "fail", "已找到可执行命令；未验证安装来源" if found else "未找到 claude，请先安装官方 CLI")
    try:
        path = profile_path(profile)
        if path is not None and not path.exists():
            add("profile", "warn", "独立目录尚未创建；首次受检启动时初始化，之后需自行登录")
        else:
            add("profile", "ok", "沿用默认配置选择；未验证登录" if path is None else "独立目录权限检查通过；未验证登录")
    except UsageError:
        add("profile", "fail", "目录权限、符号链接或继承的认证环境不符合独立配置要求")
    if timezone is not None:
        checks[-1]["detail"] += "; 启动时请求 TZ=" + timezone + "，实际生效未验证"
    return checks


def check_report(shell, profile, timezone=None):
    checks = profile_checks(profile, timezone)

    def add(key, status, detail):
        checks.append({"id": key, "status": status, "detail": detail})

    probe = None
    output = []
    succeeded = False
    try:
        probe = Health(shell, report=True)
        while probe.result() is None:
            time.sleep(0.05)
        output = probe.output.decode("ascii", errors="replace").splitlines()
        succeeded = probe.process.returncode == 0
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        if probe is not None:
            probe.close()
    details = {
        "proxy": ("检测到代理客户端运行；未验证 CLI 代理配置", "未检测到代理客户端运行，或检查无法完成"),
        "tun": ("检测到当前入口要求的 TUN；未验证断线阻断", "未检测到当前入口要求的 TUN"),
        "country": ("探针出口国家符合目标；不代表 Claude 实际请求路径", "探针连接、出口国家或地址校验失败"),
        "subscription": ("探针出口符合已配置的订阅绑定", "订阅绑定缺失有效地址、格式异常或出口不匹配"),
    }
    blocked = False
    for index, key in enumerate(details):
        if blocked:
            add(key, "skipped", "前置检查未通过，本项未执行")
            continue
        token = output[index] if index < len(output) else ""
        if token == key + "=ok":
            add(key, "ok", details[key][0])
        elif key == "subscription" and token == "subscription=warn" and succeeded:
            add(key, "warn", "未配置订阅，未校验出口 IP 绑定")
        else:
            add(key, "fail", details[key][1])
            blocked = True
    if not succeeded and not any(item["status"] == "fail" for item in checks[2:]):
        checks[-1].update(status="fail", detail="网络检查未完整结束，请重试")
    print(json.dumps({"schema_version": 1, "profile": profile, "checks": checks}, ensure_ascii=False))
    return 1 if any(item["status"] == "fail" for item in checks) else 0


ANTIFY_APP = Path("/Applications/Antify.app")
ANTIFY_BUNDLE = "com.lester.Antify"
ANTIFY_TEAM = "H6PU3TBL97"
ANTIFY_EXTENSION = "com.lester.Antify.AntifyCore"


def antify_report(profile, timezone=None):
    checks = profile_checks(profile, timezone)

    def add(key, status, detail):
        checks.append({"id": key, "status": status, "detail": detail})

    version = None
    trusted = False
    if not ANTIFY_APP.exists():
        add("antify_install", "fail", "未找到 Antify，请运行 Antify 检测版安装器")
    else:
        try:
            with (ANTIFY_APP / "Contents/Info.plist").open("rb") as handle:
                info = plistlib.load(handle)
            raw_version = info.get("CFBundleShortVersionString", "")
            if not isinstance(raw_version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", raw_version):
                raise ValueError()
            version = tuple(map(int, raw_version.split(".")))
            verify = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(ANTIFY_APP)],
                                    capture_output=True, timeout=10)
            identity = subprocess.run(["/usr/bin/codesign", "-dv", "--verbose=4", str(ANTIFY_APP)],
                                      capture_output=True, text=True, timeout=10)
            trusted = (info.get("CFBundleIdentifier") == ANTIFY_BUNDLE and verify.returncode == 0
                       and identity.returncode == 0 and "TeamIdentifier=" + ANTIFY_TEAM in identity.stderr.splitlines())
            if not trusted:
                add("antify_install", "fail", "Antify 签名或应用标识校验失败，请从官网下载")
            elif version < (0, 0, 78):
                add("antify_install", "fail", "Antify " + raw_version + " 不支持 Kill Switch，请更新")
            else:
                add("antify_install", "ok", "Antify " + raw_version + " 已安装，签名与开发者校验通过")
        except (OSError, ValueError, TypeError, plistlib.InvalidFileException, subprocess.SubprocessError):
            add("antify_install", "fail", "无法验证 Antify 安装，请重新安装官方版本")
    try:
        result = subprocess.run(["/usr/bin/systemextensionsctl", "list"], capture_output=True, text=True, timeout=10)
        entries = [line for line in result.stdout.splitlines() if ANTIFY_EXTENSION in line.split() and ANTIFY_TEAM in line.split()]
        if result.returncode != 0:
            add("antify_extension", "warn", "系统扩展状态查询失败，请在 Antify 中检查")
        elif any("[activated enabled]" in line for line in entries):
            add("antify_extension", "ok", "扩展已获系统批准；此状态不能证明代理服务正在运行")
        else:
            add("antify_extension", "warn", "未确认扩展已批准启用，请打开 Antify 完成系统授权")
    except (OSError, subprocess.SubprocessError):
        add("antify_extension", "warn", "无法读取系统扩展状态，请在 Antify 中检查")
    add("antify_route", "warn", "需在 Antify 配置命令行规则，并以 Claude 实际新连接核验出口；本检测不发起网络探针")
    add("antify_protection", "fail" if trusted and version < (0, 0, 78) else "warn",
        "当前版本不支持 Kill Switch" if trusted and version < (0, 0, 78) else
        "需在 Antify 启用并实测 Kill Switch；DNS 与扩展崩溃窗口仍需单独核验")
    return {"schema_version": 1, "kind": "antify_diagnostics", "profile": profile,
            "launch_supported": False, "checks": checks}


class Supervisor:
    def __init__(self, shell):
        self.shell = shell
        self.pid = None
        self.health = None
        self.tty = sys.stdin.fileno() if sys.stdin.isatty() else None
        self.pgrp = os.getpgrp()
        self.terminal = termios.tcgetattr(self.tty) if self.tty is not None else None
        self.pause_requested = False
        self.terminate_signal = None
        self.handlers = {}

    def receive(self, sig, frame):
        if sig == signal.SIGTSTP:
            self.pause_requested = True
        elif sig == signal.SIGWINCH:
            if self.pid:
                group_signal(self.pid, sig)
        else:
            if self.pid:
                group_signal(self.pid, sig)
            if sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGQUIT):
                self.terminate_signal = sig

    def terminal_to(self, group, attributes=None):
        if self.tty is not None:
            os.tcsetpgrp(self.tty, group)
            if attributes is not None:
                termios.tcsetattr(self.tty, termios.TCSANOW, attributes)

    def pause(self):
        group_signal(self.pid, signal.SIGSTOP)
        child_terminal = termios.tcgetattr(self.tty) if self.tty is not None else None
        self.terminal_to(self.pgrp, self.terminal)
        self.pause_requested = False
        os.kill(os.getpid(), signal.SIGSTOP)
        # A resumed background job must not steal the foreground terminal.
        if self.tty is not None:
            while os.tcgetpgrp(self.tty) != self.pgrp:
                os.kill(os.getpid(), signal.SIGSTOP)
        self.terminal_to(self.pid, child_terminal)
        group_signal(self.pid, signal.SIGCONT)

    def run(self, executable, argv, environ):
        if self.tty is not None and os.tcgetpgrp(self.tty) != self.pgrp:
            raise UsageError("请在前台启动 Claude Code")
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT,
                    signal.SIGTSTP, signal.SIGWINCH, signal.SIGTTOU):
            self.handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, signal.SIG_IGN if sig == signal.SIGTTOU else self.receive)
        read_fd, write_fd = os.pipe()
        try:
            self.pid = os.fork()
            if self.pid == 0:
                try:
                    os.close(write_fd)
                    os.setpgid(0, 0)
                    for sig in self.handlers:
                        signal.signal(sig, signal.SIG_DFL)
                    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
                    if os.read(read_fd, 1) != b"1":
                        os._exit(125)
                    os.close(read_fd)
                    os.execve(executable, [executable, *argv], environ)
                except BaseException:
                    os.write(2, "Claude Code 启动失败\n".encode())
                    os._exit(126)
            os.close(read_fd)
            read_fd = None
            os.setpgid(self.pid, self.pid)
            self.terminal_to(self.pid)
            os.write(write_fd, b"1")
            os.close(write_fd)
            write_fd = None
            next_health = time.monotonic() + HEALTH_INTERVAL
            while True:
                waited, status = os.waitpid(self.pid, os.WNOHANG | os.WUNTRACED)
                if waited:
                    if os.WIFSTOPPED(status):
                        self.pause()
                        next_health = 0
                    elif os.WIFEXITED(status):
                        return os.WEXITSTATUS(status)
                    elif os.WIFSIGNALED(status):
                        return -os.WTERMSIG(status)
                if self.terminate_signal:
                    return -self.terminate_signal
                if self.pause_requested:
                    self.pause()
                    next_health = 0
                if self.health is None and time.monotonic() >= next_health:
                    self.health = Health(self.shell)
                if self.health is not None:
                    healthy = self.health.result()
                    if healthy is not None:
                        self.health.close()
                        self.health = None
                        if not healthy:
                            print("Claude Code 网络检查失败，已终止本次启动的进程组", file=sys.stderr)
                            return 125
                        next_health = time.monotonic() + HEALTH_INTERVAL
                time.sleep(0.05)
        finally:
            for fd in (read_fd, write_fd):
                if fd is not None:
                    os.close(fd)
            if self.health:
                self.health.close()
            if self.pid:
                stop_group(self.pid)
                try:
                    os.waitpid(self.pid, 0)
                except ChildProcessError:
                    pass
            try:
                self.terminal_to(self.pgrp, self.terminal)
            finally:
                for sig, handler in self.handlers.items():
                    signal.signal(sig, handler)


def main(argv=None):
    def interrupted(sig, frame):
        signal.signal(sig, signal.SIG_IGN)
        raise Terminated(sig)

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)}
    try:
        args = parse(sys.argv[1:] if argv is None else argv)
        if args.action == "install":
            report = install_cli()
            print(json.dumps(report, ensure_ascii=False) if args.json else report["detail"])
            return {"installed": 0, "already_present": 0, "failed": 1, "cancelled": 130}[report["status"]]
        if args.route == "antify":
            if args.action == "run":
                raise UsageError("Antify 当前仅支持诊断，未验证实际路由与断线保护，不能通过此入口启动 Claude Code")
            report = antify_report(args.profile, args.timezone)
            if args.json:
                print(json.dumps(report, ensure_ascii=False))
            else:
                for item in report["checks"]:
                    print(item["id"] + "=" + item["status"] + ":" + item["detail"])
            return 1 if any(item["status"] == "fail" for item in report["checks"]) else 0
        if args.json:
            return check_report(args.shell, args.profile, args.timezone)
        path = profile_path(args.profile)
        executable = find_cli()
        if not executable:
            raise UsageError("未找到 Claude Code CLI，请先安装 claude")
        if not check(args.shell):
            print("network=fail:代理、TUN、出口国家或订阅绑定检查未通过", file=sys.stderr)
            return 1
        if args.action == "check":
            print("cli=ok\nprofile=ok\nproxy=ok\ntun=ok\ncountry=ok\nsubscription=ok")
            return 0
        path = profile_path(args.profile, create=True)
        environ = os.environ.copy()
        if args.timezone is not None:
            environ["TZ"] = args.timezone
        if path is not None:
            environ["CLAUDE_CONFIG_DIR"] = str(path)
        return Supervisor(args.shell).run(executable, args.command, environ)
    except UsageError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError, termios.error):
        # Paths, argv and exception details may contain credentials.
        print("Claude Code 无法安全启动；请检查参数、CLI、账号目录及认证环境变量", file=sys.stderr)
        return 2
    except Terminated as error:
        return -error.sig
    except KeyboardInterrupt:
        return -signal.SIGINT
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    result = main()
    if result < 0:
        if -result not in (signal.SIGKILL, signal.SIGSTOP):
            signal.signal(-result, signal.SIG_DFL)
        os.kill(os.getpid(), -result)
    raise SystemExit(result)
