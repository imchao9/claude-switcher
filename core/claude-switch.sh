#!/bin/bash
# Claude 账号切换器 - 核心脚本 (通用版)
# 用法: claude-switch.sh check | status | switch A|B | checkdetail | fix | set-region | update [--auto] | subscription set|fetch|status|ports|check|install|clear | diagnose-proxy
set -u
export LC_ALL=en_US.UTF-8
umask 077

# 随现有单脚本更新分发;在任何账号状态读写、审计/修复/启动之前分派。
if [ "${1:-}" = "diagnose-proxy" ]; then
    shift
    exec "${CLAUDE_SWITCHER_PYTHON:-python3}" - "$@" <<'PY_PROXY_DIAGNOSE'
"""Read-only transport diagnostics for an explicitly selected HTTP/SOCKS proxy."""
import argparse
import concurrent.futures
import ipaddress
import json
import math
import os
import subprocess
from urllib.parse import urlsplit


TARGETS = (
    ("anthropic_api", "https://api.anthropic.com/v1/models"),
    ("claude_web", "https://claude.ai/"),
)


def select_proxy(args, environ):
    if args.proxy is not None:
        value, source = args.proxy, "argument"
    elif args.config is not None:
        with open(args.config, encoding="utf-8") as handle:
            config = json.load(handle)
        if not isinstance(config, dict):
            raise ValueError("invalid config")
        value, source = config.get("proxy_url"), "config"
    else:
        value, source = environ.get("CLAUDE_SWITCHER_PROXY_URL"), "environment"
    if not isinstance(value, str) or not value.strip():
        raise ValueError("proxy required")
    value = value.strip()
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("invalid proxy")
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https", "socks5", "socks5h")
            or not parsed.hostname or parsed.port is None
            or not 0 < parsed.port < 65536
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("invalid proxy")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.lower() == "localhost"
    # Do not include proxy addresses, credentials, or private config paths in reports.
    public = {"scheme": parsed.scheme, "port": parsed.port,
              "host_scope": "loopback" if loopback else "remote",
              "authentication_configured": parsed.username is not None,
              "source": source}
    return value, public


def probe(name, url, proxy, environ):
    # -q ignores curlrc. Pass proxy credentials over stdin, never in argv/reports.
    quoted = proxy.replace("\\", "\\\\").replace('"', '\\"')
    config = 'proxy = "' + quoted + '"\n'
    args = ["curl", "-q", "--config", "-", "-sS", "-o", os.devnull,
            "--proto", "=https", "--noproxy", "", "--connect-timeout", "6",
            "--max-time", "12", "-w",
            "%{http_code}\t%{http_connect}\t%{ssl_verify_result}\t%{time_appconnect}\t%{time_total}",
            url]
    keep = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
            "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE", "SYSTEMROOT"}
    clean_env = {k: v for k, v in environ.items() if k in keep}
    result = {"name": name, "url": url, "transport_reached": False,
              "access": "unknown"}
    try:
        process = subprocess.run(args, input=config, env=clean_env, capture_output=True,
                                 text=True, timeout=15)
        fields = process.stdout.strip().split()
        status, connect, verify = map(int, fields[:3])
        tls, elapsed = map(float, fields[3:5])
        if (not 0 <= status <= 599 or not 0 <= connect <= 599 or verify < 0
                or not math.isfinite(tls) or not math.isfinite(elapsed)
                or tls < 0 or elapsed < 0):
            raise ValueError("invalid probe metrics")
        reached = process.returncode == 0 and status > 0 and verify == 0 and tls > 0
        access = ("unauthenticated" if status == 401 else
                  "access_restricted" if status in (403, 407) else
                  "server_error" if status >= 500 else "http_response")
        result.update(curl_exit=process.returncode, http_status=status,
                      proxy_connect_status=connect, tls_verified=reached,
                      elapsed_seconds=elapsed, transport_reached=reached,
                      access=access if reached else "unknown")
    except subprocess.TimeoutExpired:
        result["error"] = "probe_timeout"
    except OSError:
        result["error"] = "curl_unavailable"
    except (ValueError, IndexError):
        result["error"] = "invalid_probe_output"
    return result


class DiagnosticParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error can echo private URLs or paths from argv.
        raise ValueError("invalid arguments")


def main(argv=None, environ=None):
    parser = DiagnosticParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--proxy", help="explicit proxy URL including a port")
    choice.add_argument("--config", help="private JSON file containing proxy_url")
    environ = os.environ if environ is None else environ
    report = {"schema_version": 1, "mode": "proxy_diagnostics",
              "scope": "unauthenticated_transport_only",
              "account_status": "not_evaluated", "leak_protection": "not_evaluated",
              "launch_allowed": None, "application_started": False,
              "system_modified": False}
    try:
        args = parser.parse_args(argv)
        proxy, public = select_proxy(args, environ)
    except (OSError, ValueError, TypeError):
        report.update(state="configuration_error", error="invalid_or_missing_proxy",
                      hint="Use --proxy, --config, or CLAUDE_SWITCHER_PROXY_URL; an explicit port is required.")
        print(json.dumps(report, ensure_ascii=False))
        return 2
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(probe, name, url, proxy, environ) for name, url in TARGETS]
        checks = [f.result() for f in futures]
    reached = all(check["transport_reached"] for check in checks)
    report.update(proxy=public, checks=checks,
                  state="transport_reached" if reached else "transport_failed")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if reached else 1


if __name__ == "__main__":
    raise SystemExit(main())
PY_PROXY_DIAGNOSE
fi

# ============ 安装时可配置项(安装程序会自动改写) ============
PROXY_MODE="clash-verge"       # 代理客户端: clash-verge|shadowrocket|sing-box|clashx|surge|auto
PROXY_GROUP="IpEqual"          # Clash 代理组名(安装时自动探测)
COUNTRY_TARGET="US"            # 目标出口国家(安装时按出口自动识别)
TZ_TARGET="America/New_York"   # Claude 进程请求时区，系统时区由用户管理
LOCALE_TARGET="en_US"          # 锁定地区格式
# ============================================================

DATA="$HOME/Library/Application Support/Claude"
BASE="$HOME/.claude-switcher"
LOG="$HOME/Library/Logs/claude-switch.log"
CLASH_DIR="$HOME/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"
SUBSCRIPTION_MANIFEST="$BASE/subscription.json"
SUBSCRIPTION_FILE="$BASE/subscription.yaml"
ROUTE_MISSED=""
case "${1:-}" in
    code|code-health) ;;
    *) mkdir -p "$BASE/accounts" ;;
esac

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >>"$LOG"; }

# TUN 在线判定:存在 fake-ip 网段地址即视为 TUN 已接管(兼容各种 TUN 设备名)
tun_up() { ifconfig 2>/dev/null | grep -q 'inet 198\.18\.'; }

# 代理客户端显示名(供检查细节输出)
proxy_name() {
    case "$PROXY_MODE" in
        clash-verge)  echo "Clash Verge" ;;
        shadowrocket) echo "Shadowrocket" ;;
        sing-box)     echo "sing-box" ;;
        clashx)       echo "ClashX" ;;
        surge)        echo "Surge" ;;
        auto)         echo "代理客户端" ;;
        *)            echo "$PROXY_MODE" ;;
    esac
}

# 可 open -a 启动的客户端 App 名(sing-box/auto 无标准 App,返回空)
proxy_app_name() {
    case "$PROXY_MODE" in
        clash-verge)  echo "Clash Verge" ;;
        shadowrocket) echo "Shadowrocket" ;;
        clashx)       echo "ClashX" ;;
        surge)        echo "Surge" ;;
        *)            echo "" ;;
    esac
}

# Shadowrocket 在 Mac 上表现为 VPN 配置;launchd/非图形环境下 scutil 可能查不到该行,此时以 TUN 判定为准(避免误杀)
shadowrocket_up() {
    nc=$(scutil --nc list 2>/dev/null)
    if echo "$nc" | grep -q 'com.liguangming.Shadowrocket'; then
        echo "$nc" | grep 'com.liguangming.Shadowrocket' | grep -q '(Connected)'
    else
        tun_up
    fi
}

# 代理内核在线判定:按安装时识别的客户端分派;auto 时任一命中即活
proxy_alive() {
    case "$PROXY_MODE" in
        clash-verge)  pgrep -f verge-mihomo >/dev/null 2>&1 ;;
        shadowrocket) shadowrocket_up ;;
        sing-box)     pgrep -f sing-box >/dev/null 2>&1 ;;
        clashx)       pgrep -x ClashX >/dev/null 2>&1 || pgrep -f clash >/dev/null 2>&1 ;;
        surge)        pgrep -f Surge >/dev/null 2>&1 ;;
        auto)         pgrep -f verge-mihomo >/dev/null 2>&1 || shadowrocket_up \
                      || pgrep -f sing-box >/dev/null 2>&1 \
                      || pgrep -x ClashX >/dev/null 2>&1 || pgrep -f clash >/dev/null 2>&1 \
                      || pgrep -f Surge >/dev/null 2>&1 ;;
        *)            pgrep -f verge-mihomo >/dev/null 2>&1 ;;
    esac
}

# 置顶规则配置检查(仅 Clash Verge):合并配置里是否含置顶规则
merge_ok() {
    grep -q "DOMAIN-SUFFIX,claude.ai,$PROXY_GROUP" "$CLASH_DIR/clash-verge.yaml" 2>/dev/null
}

stun_ok() {
    python3 - "$CLASH_DIR/clash-verge.yaml" "$PROXY_GROUP" <<'PY_STUN_RULES'
import json
from pathlib import Path
import re
import sys


def scalar(value):
    value = value.strip()
    if not value:
        raise ValueError()
    if value.startswith('"'):
        result, end = json.JSONDecoder().raw_decode(value)
        tail = value[end:]
        if not isinstance(result, str) or (tail and not re.fullmatch(r'\s+(?:#.*)?', tail)):
            raise ValueError()
        return result
    if value.startswith("'"):
        match = re.fullmatch(r"'((?:[^']|'')*)'(?:\s+(?:#.*)?)?", value)
        if not match:
            raise ValueError()
        return match[1].replace("''", "'")
    if value[0] in '[]{}&*!|>@`' or ': ' in value:
        raise ValueError()
    return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()


def pinned(text, group):
    if group in ('', 'DIRECT', 'REJECT', 'REJECT-DROP', 'PASS'):
        return False
    rules = []
    found = False
    inside = False
    item_indent = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if '\t' in line or line.startswith(('---', '...')):
            return False
        if not line.startswith(' ') and not line.startswith('- '):
            header = re.match(r"([A-Za-z0-9_-]+):(?:\s|$)", line)
            if not header:
                return False
            inside = header[1] == 'rules'
            if inside:
                tail = line[header.end():].strip()
                if found or (tail and not tail.startswith('#')):
                    return False
                found = True
            continue
        if not inside:
            continue
        item = re.fullmatch(r"( *)- (.+)", line)
        if not item:
            return False
        indent = len(item[1])
        if item_indent is not None and indent != item_indent:
            return False
        item_indent = indent
        rule = scalar(item[2])
        if any(ord(char) < 32 for char in rule):
            return False
        rules.append(rule)
    required = {'3478', '5349', '19302'}
    known = {'DOMAIN', 'DOMAIN-SUFFIX', 'DOMAIN-KEYWORD', 'DOMAIN-REGEX',
             'GEOSITE', 'IP-CIDR', 'IP-CIDR6', 'GEOIP', 'SRC-IP-CIDR',
             'SRC-PORT', 'DST-PORT', 'PROCESS-NAME', 'PROCESS-PATH', 'RULE-SET'}
    for rule in rules:
        parts = [part.strip() for part in rule.split(',')]
        if parts[0] == 'MATCH' and len(parts) == 2:
            policy = parts[1]
        elif (parts[0] in known and len(parts) in (3, 4)
              and (len(parts) == 3 or parts[3] == 'no-resolve')):
            policy = parts[2]
        else:
            return False
        if not all(parts) or policy != group:
            return False
        if parts[0] == 'DST-PORT' and len(parts) == 3:
            required.discard(parts[1])
        if not required:
            return True
    return False


try:
    valid = pinned(Path(sys.argv[1]).read_text(encoding='utf-8'), sys.argv[2])
except (OSError, UnicodeError, ValueError):
    valid = False
sys.exit(0 if valid else 1)
PY_STUN_RULES
}

# 置顶规则行为检测:三个生态域名全部解析到 fake-ip 段(198.18.x.x)即视为已置顶;未命中域名记入 ROUTE_MISSED
route_ok() {
    ROUTE_MISSED=""
    local d a
    for d in claude.ai api.anthropic.com claudeusercontent.com; do
        a=$(dig +short +time=3 +tries=1 "$d" 2>/dev/null | head -1)
        case "$a" in
            198.18.*) ;;
            *) ROUTE_MISSED="$ROUTE_MISSED $d" ;;
        esac
    done
    [ -z "$ROUTE_MISSED" ]
}

# 国家代码 → 地区格式映射(未收录的国家回退 en_US)
locale_for() {
    case "$1" in
        US) echo en_US ;; JP) echo ja_JP ;; TW) echo zh_TW ;; HK) echo zh_HK ;;
        SG) echo en_SG ;; GB) echo en_GB ;; DE) echo de_DE ;; FR) echo fr_FR ;;
        KR) echo ko_KR ;; AU) echo en_AU ;; CA) echo en_CA ;; *) echo en_US ;;
    esac
}

# IP 审查:记录出口IP并统计最近48h内不同IP个数(账号画像风险指标)
iphist_count() {
    [ -z "$1" ] && { echo 0; return; }
    local HIST="$BASE/iphistory.log"
    local now=$(date +%s)
    echo "$1 $now" >>"$HIST"
    # 裁掉48小时(172800秒)以前的记录
    awk -v cutoff=$((now-172800)) '$2>=cutoff' "$HIST" > "$HIST.tmp" && mv "$HIST.tmp" "$HIST"
    # 统计48h内不同IP个数
    awk '{print $1}' "$HIST" | sort -u | wc -l | tr -d ' '
}

# ---------- 用户订阅与绑定 IP ----------
# 订阅地址是凭据:不写日志、不写 checkdetail,默认只接受 HTTPS.
subscription_url_from_manifest() {
    [ -f "$SUBSCRIPTION_MANIFEST" ] || return 1
    python3 - "$SUBSCRIPTION_MANIFEST" <<'PY_SUBSCRIPTION_URL'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        value = json.load(handle).get("url")
    if isinstance(value, str) and value:
        print(value)
except (OSError, ValueError, TypeError):
    pass
PY_SUBSCRIPTION_URL
}

subscription_expected_ip() {
    [ -f "$SUBSCRIPTION_MANIFEST" ] || return 0
    python3 - "$SUBSCRIPTION_MANIFEST" <<'PY_SUBSCRIPTION_IP'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        value = json.load(handle).get("expected_ip")
    if isinstance(value, str) and value:
        print(value)
except (OSError, ValueError, TypeError):
    pass
PY_SUBSCRIPTION_IP
}

subscription_url_validate() {
    python3 - "$1" <<'PY_SUBSCRIPTION_VALIDATE'
import os
import sys
from urllib.parse import urlsplit
value = sys.argv[1]
parts = urlsplit(value)
allowed_http = os.environ.get("CLAUDE_SWITCHER_ALLOW_INSECURE_SUBSCRIPTION") == "1"
valid = (parts.scheme == "https" or (allowed_http and parts.scheme == "http"))
valid = valid and bool(parts.hostname) and not any(ord(c) < 32 or c.isspace() for c in value)
valid = valid and not parts.fragment and not parts.username and not parts.password
raise SystemExit(0 if valid else 1)
PY_SUBSCRIPTION_VALIDATE
}

subscription_set() {
    local url=""
    # 只从 stdin 读取，避免 token 出现在 shell/Swift 进程参数。
    IFS= read -r url || true
    subscription_url_validate "$url" || { echo 'FAIL:订阅地址无效或不是 HTTPS'; return 2; }
    local tmp="$SUBSCRIPTION_MANIFEST.tmp.$$"
    umask 077
    printf '%s\n' "$url" | python3 -c '
import json, os, sys
from datetime import datetime, timezone
path = sys.argv[1]
url = sys.stdin.readline().rstrip("\n")
doc = {"url": url, "expected_ip": "", "token_hint": "已配置", "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(doc, handle, ensure_ascii=False)
os.chmod(path, 0o600)
' "$tmp"
    mv -f "$tmp" "$SUBSCRIPTION_MANIFEST" || { rm -f "$tmp"; echo 'FAIL:无法保存订阅配置'; return 1; }
    chmod 600 "$SUBSCRIPTION_MANIFEST"
    echo 'OK:订阅地址已保存'
}

subscription_fetch() {
    local url
    url=$(subscription_url_from_manifest) || { echo 'FAIL:尚未配置订阅地址'; return 2; }
    if ! proxy_alive || ! tun_up; then
        echo 'FAIL:同步订阅前必须先启动代理并打开 TUN'
        return 1
    fi
    umask 077
    local headers body
    headers=$(mktemp "$BASE/subscription.headers.XXXXXX") || { echo 'FAIL:无法创建订阅临时文件'; return 1; }
    body=$(mktemp "$BASE/subscription.body.XXXXXX") || { rm -f "$headers"; echo 'FAIL:无法创建订阅临时文件'; return 1; }
    local proto=(--proto '=https')
    [ "${CLAUDE_SWITCHER_ALLOW_INSECURE_SUBSCRIPTION:-0}" = "1" ] && proto=()
    trap 'rm -f "$headers" "$body"' RETURN
    # URL/token 通过 curl 配置 stdin 传递，不出现在 curl 的 argv 或进程列表。
    local curl_url=${url//\\/\\\\}
    curl_url=${curl_url//\"/\\\"}
    if ! printf 'url = "%s"\n' "$curl_url" | curl -q -fsS "${proto[@]}" --max-time 20 -D "$headers" -o "$body" --config - >/dev/null 2>&1; then
        echo 'FAIL:订阅下载失败'
        return 1
    fi
    local expected
    expected=$(awk 'tolower($1)=="x-claude-switcher-egress-ip:" {print $2; exit}' "$headers" | tr -d '\r')
    [ -n "$expected" ] || { echo 'FAIL:订阅缺少绑定 IP 标记'; return 1; }
    grep -q '^proxies:' "$body" || { echo 'FAIL:订阅内容不是受支持的 Clash 配置'; return 1; }
    local out="$SUBSCRIPTION_FILE.tmp.$$"
    umask 077
    cp "$body" "$out" && chmod 600 "$out" && mv -f "$out" "$SUBSCRIPTION_FILE" || { rm -f "$out"; echo 'FAIL:无法保存订阅配置'; return 1; }
    python3 - "$SUBSCRIPTION_MANIFEST.tmp.$$" "$SUBSCRIPTION_MANIFEST" "$expected" <<'PY_SUBSCRIPTION_FETCH'
import json
import os
import sys
from datetime import datetime, timezone
tmp, source, expected = sys.argv[1:]
with open(source, encoding="utf-8") as handle:
    doc = json.load(handle)
doc["expected_ip"] = expected
doc["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
with open(tmp, "w", encoding="utf-8") as handle:
    json.dump(doc, handle, ensure_ascii=False)
os.chmod(tmp, 0o600)
PY_SUBSCRIPTION_FETCH
    mv -f "$SUBSCRIPTION_MANIFEST.tmp.$$" "$SUBSCRIPTION_MANIFEST" || { rm -f "$SUBSCRIPTION_MANIFEST.tmp.$$"; echo 'FAIL:无法更新订阅状态'; return 1; }
    echo 'OK:订阅已同步'
}

subscription_ports() {
    python3 - "$CLASH_DIR" "$SUBSCRIPTION_FILE" "${1:-}" <<'PY_SUBSCRIPTION_PORTS'
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys

root, subscription, output = sys.argv[1:]
result = {"status": "warn", "runtime_verified": False, "source": "unknown",
          "ports": {}, "listeners": {}, "reason": "runtime_unavailable"}

def scalar(path, key):
    try:
        values = re.findall(r"^" + re.escape(key) + r":[ \t]*(.*)$", Path(path).read_text(), re.M)
        if len(values) != 1:
            return None
        raw = values[0].strip()
        if raw.startswith('"'):
            return json.loads(raw)
        if raw.startswith("'") and raw.endswith("'"):
            return raw[1:-1].replace("''", "'")
        if re.fullmatch(r"[A-Za-z0-9_.:/-]*", raw):
            return raw
    except (OSError, ValueError):
        pass
    return None

def port(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= 65535:
        return value
    return None

def inspect():
    try:
        ps = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True,
                            text=True, timeout=3, check=True)
        sockets = []
        cores = []
        for line in ps.stdout.splitlines():
            executable = re.match(r"^\s*\d+\s+(.+?)(?=\s+-[A-Za-z]|$)", line)
            if not executable or Path(executable[1].strip('"').strip("'")).name not in {"verge-mihomo", "mihomo", "clash"}:
                continue
            cores.append(line)
            match = re.search(r"(?:^|\s)-ext-ctl-unix(?:=|\s+)(.+?)(?=\s+-[A-Za-z]|$)", line)
            if match:
                candidate = match[1].strip().strip('"').strip("'")
                if candidate.startswith("/") and not any(ord(c) < 32 for c in candidate):
                    sockets.append(candidate)
        if len(cores) != 1:
            result["reason"] = "ambiguous_runtime" if cores else "runtime_unavailable"
            return
        if len(sockets) != 1:
            result["reason"] = "runtime_unavailable"
            return
        secret = scalar(Path(root) / "clash-verge.yaml", "secret")
        if not isinstance(secret, str) or any(ord(c) < 32 for c in secret):
            result["reason"] = "auth_unavailable"
            return
        header = "Authorization: Bearer " + secret
        escaped = header.replace("\\", "\\\\").replace('"', '\\"')
        response = subprocess.run(["curl", "-q", "--silent", "--show-error", "--fail",
            "--noproxy", "*", "--max-time", "3", "--unix-socket", sockets[0],
            "--config", "-", "http://localhost/configs"],
            input='header = "' + escaped + '"\n', capture_output=True, text=True, timeout=4)
        if response.returncode:
            result["reason"] = "api_unavailable_or_unauthorized"
            return
        config = json.loads(response.stdout)
        if not isinstance(config, dict):
            raise ValueError()
        ports = {key: port(config.get(key, 0)) for key in ("mixed-port", "port", "socks-port")}
        if any(value is None for value in ports.values()) or not any(ports.values()):
            result["reason"] = "invalid_runtime_ports"
            return
        result.update(source="runtime_api", ports=ports)
        listeners = {}
        for key, value in ports.items():
            if not value:
                continue
            try:
                with socket.create_connection(("127.0.0.1", value), timeout=.3):
                    listeners[key] = True
            except OSError:
                listeners[key] = False
        result["listeners"] = listeners
        if not all(listeners.values()):
            result["reason"] = "listener_unreachable"
            return
        result.update(status="ok", runtime_verified=True, reason="runtime_and_listeners_verified")
    except (OSError, ValueError, subprocess.SubprocessError):
        result["reason"] = "runtime_inspection_failed"

inspect()
if not result["ports"]:
    candidates = {}
    for key in ("mixed-port", "port", "socks-port"):
        raw = scalar(Path(root) / "clash-verge.yaml", key)
        if isinstance(raw, str) and raw.isdigit() and port(int(raw)) is not None:
            candidates[key] = int(raw)
    if candidates:
        result.update(source="configuration_candidate", ports=candidates)
raw = scalar(subscription, "mixed-port")
if isinstance(raw, str) and raw.isdigit() and port(int(raw)) is not None:
    result["template_mixed_port"] = int(raw)
    if result["runtime_verified"]:
        result["template_matches_runtime"] = result["ports"]["mixed-port"] == int(raw)
        if not result["template_matches_runtime"]:
            result["status"] = "warn"
if output == "--json":
    print(json.dumps(result, ensure_ascii=False))
elif result["runtime_verified"]:
    values = ", ".join(key + "=" + str(value) for key, value in result["ports"].items() if value)
    mismatch = result.get("template_matches_runtime") is False
    print("subscription_ports=" + ("warn:" if mismatch else "ok:") + "实际监听 " + values +
          ("；与订阅模板端口不同，保留用户设置并核对 Antify 上游" if mismatch else ""))
else:
    print("subscription_ports=warn:运行端口未验证（" + result["reason"] + "），勿将订阅模板端口用于 Antify")
PY_SUBSCRIPTION_PORTS
}

subscription_status() {
    [ -f "$SUBSCRIPTION_MANIFEST" ] || { echo 'subscription=fail:未配置'; return 1; }
    local expected updated
    expected=$(subscription_expected_ip)
    updated=$(python3 - "$SUBSCRIPTION_MANIFEST" <<'PY_SUBSCRIPTION_STATUS'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        print(json.load(handle).get("updated_at", ""))
except (OSError, ValueError, TypeError):
    print("")
PY_SUBSCRIPTION_STATUS
)
    [ -n "$expected" ] && echo 'subscription=ok:已配置' || echo 'subscription=warn:待同步'
    [ -n "$updated" ] && echo "subscription_sync=$updated"
    [ -n "$expected" ] && echo 'subscription_binding=ok:已绑定独立出口 IP' || true
    subscription_ports
}

subscription_check() {
    subscription_ports
    local expected actual trace
    expected=$(subscription_expected_ip)
    [ -n "$expected" ] || { echo 'subscription_check=fail:尚未同步'; return 1; }
    if ! proxy_alive || ! tun_up; then
        echo 'subscription_check=fail:代理或 TUN 未就绪'
        return 1
    fi
    trace=$(curl -s --max-time 8 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null)
    actual=$(echo "$trace" | grep '^ip=' | cut -d= -f2)
    if subscription_ip_ok "$actual"; then
        echo 'subscription_check=ok:出口 IP 与订阅绑定一致'
        return 0
    fi
    echo 'subscription_check=fail:出口 IP 与订阅绑定不一致'
    return 1
}

subscription_clear() {
    rm -f "$SUBSCRIPTION_MANIFEST" "$SUBSCRIPTION_FILE"
    echo 'OK:本地订阅已清除'
}

subscription_install() {
    subscription_ports
    [ -f "$SUBSCRIPTION_FILE" ] || { echo 'FAIL:尚未同步订阅'; return 2; }
    case "$PROXY_MODE" in
        clash-verge)
            local dest="$CLASH_DIR/profiles/claude-switcher-subscription.yaml" tmp
            mkdir -p "$CLASH_DIR/profiles"
            tmp="$dest.tmp.$$"
            cp "$SUBSCRIPTION_FILE" "$tmp" && chmod 600 "$tmp" && mv -f "$tmp" "$dest" || {
                rm -f "$tmp"
                echo 'FAIL:无法写入 Clash Verge Profile'
                return 1
            }
            echo 'OK:订阅已写入 Clash Verge Profile，请在客户端选中该 Profile'
            ;;
        *)
            echo 'INFO:订阅文件已保存，请在当前代理客户端中导入本地配置'
            ;;
    esac
}

subscription_ip_ok() {
    local actual="$1" expected
    expected=$(subscription_expected_ip)
    [ -z "$expected" ] || [ "$actual" = "$expected" ]
}

code_health() {
    proxy_alive || { echo 'proxy=fail'; return 1; }
    echo 'proxy=ok'
    tun_up || { echo 'tun=fail'; return 1; }
    echo 'tun=ok'
    local trace
    trace=$(curl -q -fsS --connect-timeout 4 --max-time 8 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null) || return 1
    printf '%s\n' "$trace" | python3 -c '
import ipaddress, json, os, re, sys
try:
    pairs = [line.split("=", 1) for line in sys.stdin.read(65537).splitlines() if "=" in line]
    locations = [v for k, v in pairs if k == "loc"]
    addresses = [v for k, v in pairs if k == "ip"]
    if len(locations) != 1 or len(addresses) != 1:
        raise ValueError()
    country = locations[0]
    actual = ipaddress.ip_address(addresses[0])
    if not re.fullmatch("[A-Z]{2}", country) or country == "CN" or country != sys.argv[1]:
        raise ValueError()
    print("country=ok")
    manifest = sys.argv[2]
    if os.path.lexists(manifest):
        if os.path.islink(manifest):
            raise ValueError()
        with open(manifest, encoding="utf-8") as handle:
            data = json.load(handle)
        expected = data.get("expected_ip") if isinstance(data, dict) else None
        if not isinstance(expected, str) or ipaddress.ip_address(expected) != actual:
            raise ValueError()
    print("subscription=warn" if sys.argv[3] == "report" and not os.path.lexists(manifest) else "subscription=ok")
except (OSError, ValueError, TypeError):
    sys.exit(1)
' "$COUNTRY_TARGET" "$SUBSCRIPTION_MANIFEST" "${1:-}"
}

code_command() {
    local directory helper
    directory=$(cd "$(dirname "$0")" && pwd) || return 1
    if [ -f "$directory/claude-code.py" ]; then
        helper="$directory/claude-code.py"
    else
        helper="$BASE/claude-code.py"
    fi
    [ -f "$helper" ] || { echo 'Claude Code helper 未安装；旧版升级后请再执行一次 claude-switch.sh update，或重新运行安装器' >&2; return 1; }
    shift
    exec python3 "$helper" "$directory/$(basename "$0")" "$@"
}

timezone_valid() {
    python3 - "$1" <<'PY_TIMEZONE'
import re
import sys
from pathlib import Path
name = sys.argv[1]
if not re.fullmatch(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*", name):
    raise SystemExit(1)
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
raise SystemExit(0 if valid else 1)
PY_TIMEZONE
}

# ---------- 安全检查 ----------
check() {
    fails=""

    proxy_alive || fails="$fails;代理内核未运行"
    tun_up || fails="$fails;TUN网卡不在线"

    trace=$(curl -s --max-time 8 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null) || trace=""
    ip=$(echo "$trace" | grep '^ip=' | cut -d= -f2)
    loc=$(echo "$trace" | grep '^loc=' | cut -d= -f2)
    [ "$loc" = "$COUNTRY_TARGET" ] || fails="$fails;出口IP异常(${ip:-无}/${loc:-未知},期望$COUNTRY_TARGET)"
    subscription_ip_ok "$ip" || fails="$fails;订阅绑定IP不一致"
    ipwarn=""
    lastip=""; [ -f "$BASE/lastip" ] && lastip=$(cat "$BASE/lastip")
    if [ -n "$ip" ] && [ -n "$lastip" ] && [ "$ip" != "$lastip" ]; then
        ipwarn="注意:出口IP与上次不同($lastip -> $ip)"
    fi
    [ -n "$ip" ] && echo "$ip" > "$BASE/lastip"

    # IP 审查:48h 内出口 IP 变化 ≥3 次追加警告(只 warn 不 fail)
    ipn=$(iphist_count "$ip")
    if [ "$ipn" -ge 3 ] 2>/dev/null; then
        ipwarn="${ipwarn:+$ipwarn; }注意:48h内出口IP变化${ipn}次,账号画像有风险,建议固定节点"
    fi

    timezone_valid "$TZ_TARGET" || fails="$fails;Claude 进程时区目标无效"

    locale=$(defaults read -g AppleLocale 2>/dev/null)
    [ "$locale" = "$LOCALE_TARGET" ] || fails="$fails;地区异常($locale)"

    v6=$(networksetup -getinfo Wi-Fi 2>/dev/null | grep -i '^IPv6:')
    echo "$v6" | grep -qi 'off' || fails="$fails;IPv6未关闭"

    dnsip=$(dig +short +time=3 +tries=1 claude.ai 2>/dev/null | head -1)
    case "$dnsip" in
        198.18.*) ;;
        *) fails="$fails;DNS未走隧道(返回${dnsip:-空})" ;;
    esac

    wr=$(webrtc_test); wr_status=$?
    case "$(webrtc_result "$wr" "$wr_status" "$ip")" in
        matched) ;;
        leak) fails="$fails;WebRTC返回与代理出口不一致的IP" ;;
        no_response) fails="$fails;WebRTC未收到STUN响应,无法确认出口" ;;
        *) fails="$fails;WebRTC检测异常或缺少有效出口IP" ;;
    esac

    launchctl print system/local.claude-bodyguard >/dev/null 2>&1 || fails="$fails;保镖未运行"

    if [ "$PROXY_MODE" = "clash-verge" ]; then
        { merge_ok || route_ok; } && stun_ok || fails="$fails;置顶规则或STUN端口配置缺失"
    else
        route_ok || fails="$fails;置顶规则缺失(未命中:${ROUTE_MISSED# })"
    fi

    defaults read com.anthropic.claudefordesktop AppleLanguages 2>/dev/null | grep -q 'en-US' || {
        syslang=$(defaults read -g AppleLanguages 2>/dev/null | sed -n '2p' | tr -d ' ,"')
        [[ "$syslang" == en* ]] || fails="$fails;客户端语言为中文"
    }

    r1=$(curl -s --max-time 8 -o /dev/null -w '%{http_code}' https://claude.ai/ 2>/dev/null)
    r2=$(curl -s --max-time 8 -o /dev/null -w '%{http_code}' https://api.anthropic.com/v1/models 2>/dev/null)
    { [ "$r1" = "000" ] || [ "$r2" = "000" ]; } && fails="$fails;Anthropic生态不可达($r1/$r2)"

    if [ -n "$fails" ]; then
        echo "FAIL:${fails#;}"
        return 1
    fi
    echo "OK:$ip${ipwarn:+|$ipwarn}"
    return 0
}

# WebRTC/STUN 探测:返回 RESULT:ip,ip 或 RESULT:NONE 或空
webrtc_test() {
    python3 "$BASE/stun-probe.py" 2>/dev/null
}

webrtc_result() {
    python3 - "$1" "$2" "$3" <<'PY_WEBRTC_RESULT'
import ipaddress
import sys


raw, status, expected = sys.argv[1:]
result = 'unknown'
if status == '0' and raw == 'RESULT:NONE':
    result = 'no_response'
elif status == '0' and raw.startswith('RESULT:'):
    try:
        reference = ipaddress.ip_address(expected)
        addresses = [ipaddress.ip_address(value) for value in raw[7:].split(',')]
        result = 'matched' if all(value == reference for value in addresses) else 'leak'
    except ValueError:
        pass
print(result)
PY_WEBRTC_RESULT
}

# 逐项输出检查结果(供UI使用): 名称=ok|fail:细节
checkdetail() {
    if proxy_alive; then
        case "$PROXY_MODE" in
            shadowrocket) echo '代理内核=ok:Shadowrocket 已连接' ;;
            *) echo "代理内核=ok:$(proxy_name) 运行中" ;;
        esac
    else
        echo "代理内核=fail:$(proxy_name) 未运行"
    fi
    if tun_up; then echo 'TUN网卡=ok'; else echo 'TUN网卡=fail:不在线'; fi
    trace=$(curl -s --max-time 8 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null) || trace=""
    ip=$(echo "$trace" | grep '^ip=' | cut -d= -f2)
    loc=$(echo "$trace" | grep '^loc=' | cut -d= -f2)
    if [ "$loc" != "$COUNTRY_TARGET" ]; then
        echo "出口IP=fail:${ip:-无}/${loc:-未知}(期望$COUNTRY_TARGET)"
    elif ! subscription_ip_ok "$ip"; then
        echo '出口IP=fail:订阅绑定IP不一致'
    else
        echo "出口IP=ok:$ip/$loc"
    fi
    lastip=""; [ -f "$BASE/lastip" ] && lastip=$(cat "$BASE/lastip")
    ipst=""; ipdt=""
    if [ -z "$ip" ]; then echo 'IP一致=fail:未获取到出口IP'
    elif [ -z "$lastip" ]; then ipst="ok"; ipdt="首次记录"
    elif [ "$ip" = "$lastip" ]; then ipst="ok"
    else ipst="warn"; ipdt="$lastip → $ip"; fi
    [ -n "$ip" ] && echo "$ip" > "$BASE/lastip"
    # IP 审查:48h 内出口 IP 变化 ≥3 次提示账号画像风险(只 warn 不 fail)
    if [ -n "$ipst" ]; then
        ipn=$(iphist_count "$ip")
        if [ "$ipn" -ge 3 ] 2>/dev/null; then
            if [ "$ipst" = "ok" ]; then
                ipst="warn"; ipdt="48h内出口IP变化${ipn}次,账号画像有风险,建议固定节点"
            else
                ipdt="$ipdt;48h内出口IP变化${ipn}次,账号画像有风险,建议固定节点"
            fi
        fi
        if [ -z "$ipdt" ]; then echo "IP一致=$ipst"; else echo "IP一致=$ipst:$ipdt"; fi
    fi
    if timezone_valid "$TZ_TARGET"; then
        echo "时区=warn:启动时请求 TZ=$TZ_TARGET；客户端实际生效未验证，系统时区由用户设置"
    else
        echo "时区=fail:Claude 进程时区目标无效"
    fi
    locale=$(defaults read -g AppleLocale 2>/dev/null)
    if [ "$locale" = "$LOCALE_TARGET" ]; then echo '地区=ok'; else echo "地区=fail:$locale"; fi
    v6=$(networksetup -getinfo Wi-Fi 2>/dev/null | grep -i '^IPv6:')
    if echo "$v6" | grep -qi 'off'; then echo 'IPv6=ok:已关闭'; else echo 'IPv6=fail:未关闭'; fi
    dnsip=$(dig +short +time=3 +tries=1 claude.ai 2>/dev/null | head -1)
    case "$dnsip" in
        198.18.*) echo 'DNS=ok:走隧道' ;;
        *) echo "DNS=fail:未走隧道(${dnsip:-空})" ;;
    esac
    wr=$(webrtc_test); wr_status=$?
    case "$(webrtc_result "$wr" "$wr_status" "$ip")" in
        matched) echo 'WebRTC=ok:STUN返回与代理出口一致' ;;
        leak) echo 'WebRTC=fail:STUN返回与代理出口不一致' ;;
        no_response) echo 'WebRTC=fail:未收到STUN响应,无法确认出口' ;;
        *) echo 'WebRTC=fail:检测异常或缺少有效出口IP' ;;
    esac

    if launchctl print system/local.claude-bodyguard >/dev/null 2>&1; then
        lastkill=$(tail -1 "$HOME/Library/Logs/claude-bodyguard.log" 2>/dev/null | cut -c1-40)
        echo "保镖=ok:${lastkill:-待命}"
    else
        echo '保镖=fail:未安装'
    fi
    if [ "$PROXY_MODE" = "clash-verge" ]; then
        if ! stun_ok; then
            echo '规则置顶=fail:生成配置缺少STUN端口置顶规则'
        elif merge_ok; then
            echo '规则置顶=ok:生成配置已包含置顶规则'
        elif route_ok; then
            echo '规则置顶=ok:域名行为与STUN端口配置通过'
        else
            echo '规则置顶=fail:未生效'
        fi
    else
        if route_ok; then
            echo '规则置顶=ok:行为检测通过'
        else
            echo "规则置顶=fail:未命中(${ROUTE_MISSED# }),请在 $(proxy_name) 中自行加置顶规则"
        fi
    fi
    # 12. Claude 客户端语言(单独设为英文,或系统首选语言为英文均可;防止 JS 层读出中文)
    clang=$(defaults read com.anthropic.claudefordesktop AppleLanguages 2>/dev/null | head -2)
    syslang=$(defaults read -g AppleLanguages 2>/dev/null | sed -n '2p' | tr -d ' ,"')
    if echo "$clang" | grep -q 'en-US'; then
        echo '客户端语言=ok:en-US'
    elif [[ "$syslang" == en* ]]; then
        echo '客户端语言=ok:跟随系统(英文)'
    else
        echo '客户端语言=fail:跟随系统(中文)'
    fi
    # 13. ASN 网络属性(住宅宽带 vs 机房,按目标地区收录主流运营商)
    org=$(curl -s --max-time 6 https://ipinfo.io/json 2>/dev/null | grep '"org"' | cut -d'"' -f4)
    case "$org" in
        '') echo 'ASN属性=warn:未获取' ;;
        *Comcast*|*AT\&T*|*Verizon*|*Spectrum*|*Charter*|*Cox*|*T-Mobile*|*Cable*|*esidential*|\
        *NTT*|*KDDI*|*SoftBank*|*IIJ*|*OCN*|*So-net*|\
        *HiNet*|*Chunghwa*|*CHT*|*kbro*|*Taiwan*|*FarEasTone*|*Data\ Communication*|\
        *Singtel*|*StarHub*|*M1*|\
        *HKT*|*PCCW*|*HKBN*|*HGC*|\
        *BT*|*Sky*|*Virgin*|*TalkTalk*|*Vodafone*|\
        *Deutsche\ Telekom*|*1\&1*|*O2*) echo "ASN属性=ok:$org" ;;
        *) echo "ASN属性=warn:疑似机房($org)" ;;
    esac
    # 14. Anthropic 生态可达性
    r1=$(curl -s --max-time 8 -o /dev/null -w '%{http_code}' https://claude.ai/ 2>/dev/null)
    r2=$(curl -s --max-time 8 -o /dev/null -w '%{http_code}' https://api.anthropic.com/v1/models 2>/dev/null)
    if [ "$r1" != "000" ] && [ "$r2" != "000" ]; then
        echo "生态可达性=ok:claude.ai:$r1 api:$r2"
    else
        echo "生态可达性=fail:claude.ai:$r1 api:$r2"
    fi
}

# 置顶规则写入(智能合并):不整文件覆盖,用户已有的 Merge 配置原样保留
# 标记块自包含 prepend-rules 键+全部规则,重复执行幂等
write_merge_rules() {
    M="$CLASH_DIR/profiles/Merge.yaml"
    B0="# >>> claude-switcher pinned rules >>>"
    B1="# <<< claude-switcher pinned rules <<<"
    mkdir -p "$CLASH_DIR/profiles"
    # 置顶规则清单(缩进两格)
    rules=""
    for d in claude.ai claude.com anthropic.com claudeusercontent.com challenges.cloudflare.com datadoghq.com segment.io stripe.com stripe.network stripecdn.com intercom.io intercomcdn.com intercomassets.com; do
        rules="$rules  - DOMAIN-SUFFIX,$d,$PROXY_GROUP
"
    done
    # STUN/TURN 标准端口置顶:即使用户规则集把国内 STUN 域名判为直连,WebRTC 探测的 UDP 也必须进隧道
    for p in 3478 5349 19302; do
        rules="$rules  - DST-PORT,$p,$PROXY_GROUP
"
    done

    # 首次:文件不存在,整文件写入
    if [ ! -f "$M" ]; then
        cat > "$M" <<MEOF
# Profile Enhancement Merge Template for Clash Verge

profile:
  store-selected: true

# Claude 全家族及生态域名置顶强制走代理(优先级高于任何国内规则集)
$B0
prepend-rules:
$rules$B1
MEOF
        return 0
    fi

    # 已存在:备份后幂等替换(只在将要修改时备份)
    cp "$M" "$M.bak.$(date +%Y%m%d%H%M%S)"
    tmp="$M.cs.tmp"
    cp "$M" "$tmp"
    # 删除旧标记块(含标记行,兼容块在 prepend-rules 下缩进的情形)
    if grep -qF "$B0" "$tmp"; then
        awk -v b0="$B0" -v b1="$B1" '
            index($0, b0) {inblock=1; next}
            inblock && index($0, b1) {inblock=0; next}
            !inblock
        ' "$tmp" > "$tmp.out" && mv "$tmp.out" "$tmp"
        # 清理孤立的 prepend-rules: 键(其后没有规则行,只剩空行/注释/EOF)
        awk '
            { L[NR]=$0 }
            END {
                for (i=1; i<=NR; i++) {
                    if (L[i] ~ /^prepend-rules:/) {
                        has=0
                        for (j=i+1; j<=NR; j++) {
                            if (L[j] ~ /^[ \t]*- /) { has=1; break }
                            if (L[j] ~ /^[ \t]*$/ || L[j] ~ /^[ \t]*#/) continue
                            break
                        }
                        if (!has) continue
                    }
                    print L[i]
                }
            }
        ' "$tmp" > "$tmp.out" && mv "$tmp.out" "$tmp"
    fi
    # 注入:已有 prepend-rules: 则标记规则块(不带键)插到该行之后,否则文件顶部插入完整块
    # 注意:多行文本走环境变量传递(macOS awk 的 -v 不支持含换行的字符串);先写中间文件再替换,失败不碰原文件
    if grep -q '^prepend-rules:' "$tmp"; then
        if MARK0="$B0" MARK1="$B1" RULES="$rules" awk '
            /^prepend-rules:/ && !done {
                print
                print "  " ENVIRON["MARK0"]
                printf "%s", ENVIRON["RULES"]
                print "  " ENVIRON["MARK1"]
                done=1
                next
            }
            { print }
        ' "$tmp" > "$tmp.out"; then
            mv "$tmp.out" "$M" && rm -f "$tmp"
        else
            rm -f "$tmp" "$tmp.out"
            return 1
        fi
    else
        {
            echo "# Claude 全家族及生态域名置顶强制走代理(优先级高于任何国内规则集)"
            echo "$B0"
            echo "prepend-rules:"
            printf "%s" "$rules"
            echo "$B1"
            echo ""
            cat "$tmp"
        } > "$tmp.out" && mv "$tmp.out" "$M" && rm -f "$tmp"
    fi
    rm -f "$tmp"
}

# 一键修复:按项自动修复,每步输出结果
fix() {
    echo "FIX:开始一键修复"

    # A. 代理内核 + TUN
    if [ "$PROXY_MODE" = "clash-verge" ]; then
        if ! pgrep -f verge-mihomo >/dev/null 2>&1; then
            echo "FIX:正在启动 Clash Verge..."
            open -a "Clash Verge"
        fi
        for i in $(seq 1 40); do
            pgrep -f verge-mihomo >/dev/null 2>&1 && tun_up && break
            sleep 1
        done
        if pgrep -f verge-mihomo >/dev/null 2>&1 && tun_up; then
            echo "FIX:代理内核+TUN ✓"
        else
            V="$CLASH_DIR/verge.yaml"
            if [ -f "$V" ] && grep -q '^enable_tun_mode: false' "$V"; then
                echo "FIX:检测到 TUN 开关被关闭,正在恢复..."
                osascript -e 'tell application "Clash Verge" to quit' >/dev/null 2>&1
                sleep 3
                sed -i '' 's/^enable_tun_mode: false/enable_tun_mode: true/' "$V"
                open -a "Clash Verge"
                for i in $(seq 1 40); do
                    tun_up && break
                    sleep 1
                done
            fi
            if pgrep -f verge-mihomo >/dev/null 2>&1 && tun_up; then
                echo "FIX:代理内核+TUN ✓(已恢复TUN开关)"
            else
                echo "FIX:代理内核/TUN ✗(请打开 Clash Verge 检查节点/线路)"
            fi
        fi
    else
        app=$(proxy_app_name)
        if ! proxy_alive && [ -n "$app" ]; then
            echo "FIX:正在启动 $app..."
            open -a "$app" >/dev/null 2>&1
        fi
        for i in $(seq 1 40); do
            proxy_alive && tun_up && break
            sleep 1
        done
        if proxy_alive && tun_up; then
            echo "FIX:代理内核+TUN ✓"
        else
            echo "FIX:代理内核/TUN ✗(请打开 $(proxy_name) 手动检查节点/线路及TUN模式)"
        fi
    fi

    v6=$(networksetup -getinfo Wi-Fi 2>/dev/null | grep -i '^IPv6:')
    adminCmds=""
    echo "$v6" | grep -qi 'off' || adminCmds="${adminCmds}networksetup -setv6off Wi-Fi; "
    if ! launchctl print system/local.claude-bodyguard >/dev/null 2>&1; then
        adminCmds="${adminCmds}cp $HOME/.local/bin/local.claude-bodyguard.plist /Library/LaunchDaemons/ && chown root:wheel /Library/LaunchDaemons/local.claude-bodyguard.plist && launchctl bootstrap system /Library/LaunchDaemons/local.claude-bodyguard.plist; "
    fi
    if [ -n "$adminCmds" ]; then
        adminCmds="${adminCmds}dscacheutil -flushcache; killall -HUP mDNSResponder"
        echo "FIX:修复IPv6/DNS/保镖(需要密码)..."
        if osascript <<ASEOF >/dev/null 2>&1
do shell script "$adminCmds" with prompt "一键修复需要管理员权限" with administrator privileges
ASEOF
        then
            echo "FIX:IPv6/DNS/保镖 ✓"
        else
            echo "FIX:IPv6/保镖 ✗(取消或失败)"
        fi
    else
        echo "FIX:IPv6/保镖 无需修复 ✓"
    fi

    # B3. Claude 客户端单独设英文(用户级,无需密码;系统首选语言已是英文则跳过)
    syslang=$(defaults read -g AppleLanguages 2>/dev/null | sed -n '2p' | tr -d ' ,"')
    if ! defaults read com.anthropic.claudefordesktop AppleLanguages 2>/dev/null | grep -q 'en-US' && [[ "$syslang" != en* ]]; then
        defaults write com.anthropic.claudefordesktop AppleLanguages -array en-US
        echo "FIX:Claude 客户端语言 → en-US ✓(重启 Claude 后生效)"
    fi

    # B4. 地区格式(用户级,无需密码)
    locale=$(defaults read -g AppleLocale 2>/dev/null)
    if [ "$locale" != "$LOCALE_TARGET" ]; then
        defaults write -g AppleLocale "$LOCALE_TARGET"
        echo "FIX:地区格式 → $LOCALE_TARGET ✓"
    fi

    # B2. 置顶规则缺失(含 STUN/TURN 端口) → Clash Verge 智能合并写入 Merge 配置并重启;其他客户端提示自行配置
    if [ "$PROXY_MODE" = "clash-verge" ]; then
        if ! merge_ok || ! stun_ok || ! route_ok; then
            echo "FIX:写入置顶规则(智能合并,不动你的已有配置)..."
            write_merge_rules
            osascript -e 'tell application "Clash Verge" to quit' >/dev/null 2>&1
            sleep 3
            open -a "Clash Verge"
            for i in $(seq 1 40); do
                tun_up && break
                sleep 1
            done
            if merge_ok && stun_ok; then
                echo "FIX:置顶规则 ✓(生成配置已包含域名和STUN端口规则)"
            else
                echo "FIX:置顶规则 ✗(生成配置仍缺少域名或STUN端口规则)"
            fi
        fi
    else
        if ! route_ok; then
            echo "FIX:置顶规则 ✗(未命中:${ROUTE_MISSED# }) — 请在 $(proxy_name) 中自行添加 DOMAIN-SUFFIX 置顶规则"
        fi
    fi

    wrtrace=$(curl -s --max-time 8 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null) || wrtrace=""
    wrip=$(echo "$wrtrace" | grep '^ip=' | cut -d= -f2)
    wr=$(webrtc_test); wr_status=$?
    wr_result=$(webrtc_result "$wr" "$wr_status" "$wrip")
    if [ "$wr_result" = "leak" ] && [ "$PROXY_MODE" = "clash-verge" ] && tun_up; then
        echo "FIX:WebRTC 返回与代理出口不一致,重载 TUN 后复测..."
        osascript -e 'tell application "Clash Verge" to quit' >/dev/null 2>&1
        sleep 3
        open -a "Clash Verge"
        for i in $(seq 1 30); do tun_up && break; sleep 1; done
        sleep 2
        wrtrace=$(curl -s --max-time 8 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null) || wrtrace=""
        wrip=$(echo "$wrtrace" | grep '^ip=' | cut -d= -f2)
        wr=$(webrtc_test); wr_status=$?
        wr_result=$(webrtc_result "$wr" "$wr_status" "$wrip")
    fi
    case "$wr_result" in
        matched) echo "FIX:WebRTC ✓ (STUN返回与当前代理出口一致)" ;;
        leak) echo "FIX:WebRTC ✗ STUN返回与当前代理出口不一致,请检查TUN与UDP转发" ;;
        no_response) echo "FIX:WebRTC ✗ 未收到STUN响应,无法确认出口或UDP是否被阻断" ;;
        *) echo "FIX:WebRTC ✗ 检测异常或缺少有效出口IP,请检查网络与探针" ;;
    esac

    # D. 出口IP最终验证
    sleep 2
    trace=$(curl -s --max-time 8 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null)
    ip=$(echo "$trace" | grep '^ip=' | cut -d= -f2)
    loc=$(echo "$trace" | grep '^loc=' | cut -d= -f2)
    if [ "$loc" = "$COUNTRY_TARGET" ]; then
        echo "FIX:出口IP ✓ ($ip/$loc)"
    else
        echo "FIX:出口IP ✗ (${ip:-无}/${loc:-未知},期望$COUNTRY_TARGET) — 可能是节点故障,请到 $(proxy_name) 里换条线路"
    fi
    echo "FIX:修复流程结束"
}

# 地区自适应:按当前出口国家一键重设 国家/时区/地区格式(换了国家节点后运行)
set_region() {
    trace=$(curl -s --max-time 8 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null)
    loc=$(echo "$trace" | grep '^loc=' | cut -d= -f2)
    [ -z "$loc" ] && { echo "FAIL:无法获取出口国家(cloudflare trace 无结果,请先挂代理)"; return 1; }
    info=$(curl -s --max-time 8 https://ipinfo.io/json 2>/dev/null)
    ctry=$(echo "$info" | grep '"country"' | cut -d'"' -f4)
    ntz=$(echo "$info" | grep '"timezone"' | cut -d'"' -f4)
    if [ -z "$ctry" ] || [ -z "$ntz" ]; then
        echo "FAIL:ipinfo 未返回国家或时区"; return 1
    fi
    if [ "$loc" != "$ctry" ]; then
        echo "FAIL:出口国家不一致(cloudflare=$loc ipinfo=$ctry),已中止"; return 1
    fi
    timezone_valid "$ntz" || { echo "FAIL:出口时区不是有效 IANA 时区"; return 1; }
    nlocale=$(locale_for "$ctry")

    # 改写脚本自身头部三个配置项
    sed -i '' \
        -e "s|^COUNTRY_TARGET=\".*\"|COUNTRY_TARGET=\"$ctry\"|" \
        -e "s|^TZ_TARGET=\".*\"|TZ_TARGET=\"$ntz\"|" \
        -e "s|^LOCALE_TARGET=\".*\"|LOCALE_TARGET=\"$nlocale\"|" \
        "$0"
    COUNTRY_TARGET="$ctry"; TZ_TARGET="$ntz"; LOCALE_TARGET="$nlocale"

    # 地区格式(用户级,无需密码)
    defaults write -g AppleLocale "$nlocale"

    echo "OK:region=$loc process_tz=$ntz locale=$nlocale；系统时区由用户设置"
}

# 自动更新:拉取 main 分支最新代码,SHA256 校验+配置注入+自检+失败回滚(--auto 供 LaunchAgent 每12小时调用)
self_update() {
    if [ "${1:-}" = "--auto" ]; then auto=1; else auto=0; fi
    # 路径全部从脚本自身位置推导,兼容旧版 LaunchDaemon(root) 混跑时 $HOME 不可靠的场景
    BIN=$(cd "$(dirname "$0")" && pwd)
    UHOME="${BIN%/.local/bin}"
    [ "$UHOME" = "$BIN" ] && UHOME="$HOME"
    local code_base="$UHOME/.claude-switcher" code_owner=""
    if [ "$(id -u)" -eq 0 ]; then
        code_owner=$(stat -f '%Su' "$UHOME" 2>/dev/null)
        [ -n "$code_owner" ] || { echo 'FAIL:无法确定安装目录的用户'; return 1; }
    fi
    code_as_user() {
        if [ -n "$code_owner" ]; then sudo -H -u "$code_owner" "$@"; else "$@"; fi
    }
    if [ "$auto" -eq 1 ]; then
        mkdir -p "$BASE"
        exec >>"$BASE/update.log" 2>&1
        echo "# $(date '+%F %T') update --auto 开始"
    fi

    tmpd=$(mktemp -d)
    if ! curl -sL --max-time 30 "https://codeload.github.com/YOURSJEYO/claude-switcher/zip/refs/heads/main" -o "$tmpd/main.zip" \
        || ! unzip -q "$tmpd/main.zip" -d "$tmpd" 2>/dev/null; then
        echo "FAIL:下载或解压失败(网络问题,稍后再试)"; rm -rf "$tmpd"; return 1
    fi
    NEW="$tmpd/claude-switcher-main"
    if [ ! -f "$NEW/core/claude-switch.sh" ]; then
        echo "FAIL:远程包结构异常,已中止"; rm -rf "$tmpd"; return 1
    fi

    # 工件完整性校验:逐项比对仓库 SHA256SUMS,任一不符即中止(防传输篡改;信任根为 GitHub 仓库本身)
    if ! curl -sL --max-time 15 "https://raw.githubusercontent.com/YOURSJEYO/claude-switcher/main/SHA256SUMS" -o "$tmpd/SHA256SUMS" \
        || [ ! -s "$tmpd/SHA256SUMS" ]; then
        echo "FAIL:无法获取校验清单(SHA256SUMS),已中止"; rm -rf "$tmpd"; return 1
    fi
    bad=0
    while read -r sum rel; do
        [ -z "$sum" ] && continue
        if [ ! -f "$NEW/$rel" ] || [ "$(shasum -a 256 "$NEW/$rel" | awk '{print $1}')" != "$sum" ]; then
            bad=1; break
        fi
    done < "$tmpd/SHA256SUMS"
    for required in core/claude-switch.sh core/claude-code.py; do
        awk -v path="$required" '$2 == path { found=1 } END { exit !found }' "$tmpd/SHA256SUMS" || bad=1
    done
    if [ "$bad" -eq 1 ]; then
        echo "FAIL:工件校验失败,已中止(不排除网络篡改,可稍后重试或手动 update)"; rm -rf "$tmpd"; return 1
    fi

    if ! bash -n "$NEW/core/claude-switch.sh" || ! python3 - "$NEW/core/claude-code.py" <<'PY_CODE_COMPILE'
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    compile(handle.read(), "claude-code.py", "exec")
PY_CODE_COMPILE
    then
        echo "FAIL:新版脚本语法检查失败"; rm -rf "$tmpd"; return 1
    fi

    # 跳过判断:剔除头部5个配置行后与本地已安装版本比对,无差异即已是最新
    strip_cfg() { grep -vE '^(PROXY_MODE|PROXY_GROUP|COUNTRY_TARGET|TZ_TARGET|LOCALE_TARGET)="' "$1"; }
    if [ -f "$BIN/claude-switch.sh" ] && \
       diff <(strip_cfg "$BIN/claude-switch.sh") <(strip_cfg "$NEW/core/claude-switch.sh") >/dev/null 2>&1 && \
       cmp -s "$code_base/claude-code.py" "$NEW/core/claude-code.py"; then
        echo "OK:已是最新版本"; rm -rf "$tmpd"; return 0
    fi

    local helper_had=0
    code_as_user mkdir -p "$code_base" || { rm -rf "$tmpd"; return 1; }
    if [ -f "$code_base/claude-code.py" ]; then
        helper_had=1
        code_as_user cp "$code_base/claude-code.py" "$code_base/claude-code.py.bak" || { rm -rf "$tmpd"; return 1; }
    fi
    # 备份现有核心脚本
    if [ -f "$BIN/claude-switch.sh" ]; then
        cp "$BIN/claude-switch.sh" "$BIN/claude-switch.sh.bak" || { rm -rf "$tmpd"; return 1; }
    fi

    # 替换核心脚本 + 从 .bak 读出5个配置值注入新脚本
    if ! cp "$NEW/core/claude-switch.sh" "$BIN/claude-switch.sh" || ! (set -o pipefail; cat "$NEW/core/claude-code.py" | code_as_user tee "$code_base/claude-code.py" >/dev/null); then
        [ -f "$BIN/claude-switch.sh.bak" ] && cp "$BIN/claude-switch.sh.bak" "$BIN/claude-switch.sh"
        if [ "$helper_had" -eq 1 ]; then code_as_user cp "$code_base/claude-code.py.bak" "$code_base/claude-code.py"; else code_as_user rm -f "$code_base/claude-code.py"; fi
        echo "FAIL:复制更新失败,已回滚"; rm -rf "$tmpd"; return 1
    fi
    PM=""; PG=""; CT=""; TZV=""; LCT=""
    if [ -f "$BIN/claude-switch.sh.bak" ]; then
        get_cfg() { grep "^$1=\"" "$BIN/claude-switch.sh.bak" | head -1 | cut -d'"' -f2; }
        PM=$(get_cfg PROXY_MODE); PG=$(get_cfg PROXY_GROUP); CT=$(get_cfg COUNTRY_TARGET)
        TZV=$(get_cfg TZ_TARGET); LCT=$(get_cfg LOCALE_TARGET)
        inject_cfg() { grep -q "^$1=\"" "$BIN/claude-switch.sh.bak" && sed -i '' "s|^$1=\".*\"|$1=\"$2\"|" "$BIN/claude-switch.sh"; }
        inject_cfg PROXY_MODE "$PM"; inject_cfg PROXY_GROUP "$PG"; inject_cfg COUNTRY_TARGET "$CT"
        inject_cfg TZ_TARGET "$TZV"; inject_cfg LOCALE_TARGET "$LCT"
    fi
    chmod +x "$BIN/claude-switch.sh"

    # 守护进程脚本(root 所有,用户态不可写):与远程模板比对,有差异只提示不自动改
    APPDIR="/Library/Application Support/claude-switcher"
    if [ -f "$APPDIR/claude-bodyguard.sh" ] || [ -f "$APPDIR/tz-watchdog.sh" ]; then
        [ -z "$PM" ] && PM="clash-verge"
        [ -z "$TZV" ] && TZV="America/New_York"
        sed -e "s|__HOME__|$UHOME|g" -e "s|__PROXY_MODE__|$PM|g" "$NEW/daemons/claude-bodyguard.sh" > "$tmpd/bg.new"
        sed -e "s|__HOME__|$UHOME|g" -e "s|__TZ_TARGET__|$TZV|g" "$NEW/daemons/tz-watchdog.sh" > "$tmpd/tz.new"
        if ! diff -q "$tmpd/bg.new" "$APPDIR/claude-bodyguard.sh" >/dev/null 2>&1 || \
           ! diff -q "$tmpd/tz.new" "$APPDIR/tz-watchdog.sh" >/dev/null 2>&1; then
            echo "INFO:守护进程脚本有更新,请运行 installer/安装.command 刷新(需要密码)"
        fi
    fi
    if [ -f "$APPDIR/tz-watchdog.sh" ] && grep -q 'systemsetup' "$APPDIR/tz-watchdog.sh"; then
        echo "WARN:旧时区看门狗可能仍会修改系统时区，请运行新版 installer/安装.command 禁用并停止旧服务；本次更新不会修改系统服务"
    fi
    rm -rf "$tmpd"

    # 更新后自检:root(旧版 LaunchDaemon)下切回真实用户执行,避免 defaults 读到 root 的配置
    if [ "$(id -u)" -eq 0 ]; then
        owner=$(stat -f '%Su' "$UHOME" 2>/dev/null)
        if [ -n "$owner" ]; then out=$(sudo -H -u "$owner" "$BIN/claude-switch.sh" checkdetail 2>&1); else out=""; fi
    else
        out=$("$BIN/claude-switch.sh" checkdetail 2>&1)
    fi
    if [ -z "$out" ] || echo "$out" | grep -q '=fail'; then
        [ -f "$BIN/claude-switch.sh.bak" ] && cp "$BIN/claude-switch.sh.bak" "$BIN/claude-switch.sh"
        if [ "$helper_had" -eq 1 ]; then code_as_user cp "$code_base/claude-code.py.bak" "$code_base/claude-code.py"; else code_as_user rm -f "$code_base/claude-code.py"; fi
        echo "FAIL:新版自检未通过,已自动回滚"
        return 1
    fi
    echo "OK:更新完成 $(date '+%Y-%m-%d')"
    return 0
}

# 设备指纹体检:字体地区特征 + GPU/虚拟机特征(Canvas/WebGL 由 Web 检测页覆盖)
fingerprint() {
    echo "# 设备指纹体检 $(date '+%F %T')"
    hits=""
    for d in /Library/Fonts "$HOME/Library/Fonts"; do
        [ -d "$d" ] || continue
        while IFS= read -r f; do
            b=$(basename "$f" | tr '[:upper:]' '[:lower:]')
            case "$b" in
                *simsun*|*simhei*|*simkai*|*simfang*|*msyh*|*msjh*|*dengxian*|*wqy*|*fzshu*|*fzkai*|*fzhei*|*fangsong*) hits="$hits $b" ;;
            esac
        done < <(find "$d" -type f \( -iname '*.ttf' -o -iname '*.ttc' -o -iname '*.otf' \) 2>/dev/null)
    done
    if [ -n "$hits" ]; then
        echo "字体=warn:检测到地区特征字体:${hits# }"
        echo "建议=上述字体通常随中文办公软件安装,美区环境很少出现;不用的可在字体册停用"
    else
        echo "字体=ok:未发现中文办公字体(仅系统自带字体)"
    fi

    gpu=$(system_profiler SPDisplaysDataType 2>/dev/null)
    chip=$(echo "$gpu" | grep -m1 'Chipset Model' | sed 's/.*: //')
    res=$(echo "$gpu" | grep -m1 'Resolution' | sed 's/.*: //')
    [ -z "$chip" ] && chip="未知"
    echo "GPU=ok:${chip}"
    echo "显示=ok:${res:-未知}"
    virt=$(sysctl -n kern.hv_vmm_present 2>/dev/null)
    case "$chip" in
        *Virtual*|*VMware*|*Parallels*|*VirtualBox*|*SwiftShader*|*llvmpipe*)
            echo "虚拟机=warn:检测到虚拟 GPU($chip),这是强指纹" ;;
        *)
            [ "$virt" = "1" ] && echo "虚拟机=warn:运行在虚拟机中" || echo "虚拟机=ok:物理机" ;;
    esac
    echo "CanvasWebGL=info:原生检查读不到 Claude Electron 的渲染特征,请打开 Web 检测页 https://yoursjeyo.github.io/claude-switcher/ 查看 Canvas/WebGL 指纹"
}

status() {
    cur="none"; [ -f "$BASE/current" ] && cur=$(cat "$BASE/current")
    hasA=0; [ -d "$BASE/accounts/A" ] && hasA=1
    hasB=0; [ -d "$BASE/accounts/B" ] && hasB=1
    echo "current=$cur hasA=$hasA hasB=$hasB"
}

quit_claude() {
    pgrep -f "/Applications/Claude.app/Contents/MacOS/Claude" >/dev/null 2>&1 || return 0
    osascript -e 'tell application "Claude" to quit' >/dev/null 2>&1
    for i in $(seq 1 15); do
        pgrep -f "/Applications/Claude.app/Contents/MacOS/Claude" >/dev/null 2>&1 || return 0
        sleep 1
    done
    return 1
}

archive_current() {
    local cur="$1"
    local t0 staged current previous moved=0 had_old=0
    t0=$(date +%s)
    staged="$BASE/accounts/$cur.new"
    current="$BASE/accounts/$cur"
    previous="$BASE/accounts/$cur.previous"

    # An interrupted archive may be the only remaining copy. Never clear it
    # automatically, or delete the previous snapshot before the new one commits.
    if [ -e "$staged" ] || [ -L "$staged" ] || [ -e "$previous" ] || [ -L "$previous" ]; then
        log "FAIL:账号$cur 存在待恢复快照，拒绝覆盖"
        return 1
    fi
    if mv "$DATA" "$staged" 2>/dev/null; then
        moved=1
    else
        # A cross-volume move may leave a partial destination on failure.
        if [ -e "$staged" ] || [ -L "$staged" ]; then
            log "FAIL:账号$cur 归档移动不完整，保留现场"
            return 1
        fi
        if ! copy_tree "$DATA" "$staged"; then
            log "FAIL:账号$cur 归档复制失败，保留现场"
            return 1
        fi
    fi

    if [ -e "$current" ] || [ -L "$current" ]; then
        if ! mv "$current" "$previous"; then
            if [ "$moved" -eq 1 ]; then
                mv "$staged" "$DATA" || log "WARN:账号$cur 数据保留在暂存位置"
            fi
            log "FAIL:账号$cur 旧快照无法安全转存"
            return 1
        fi
        had_old=1
    fi
    if ! mv "$staged" "$current"; then
        if [ "$had_old" -eq 1 ]; then
            mv "$previous" "$current" || log "WARN:账号$cur 旧快照保留在恢复位置"
        fi
        if [ "$moved" -eq 1 ]; then
            mv "$staged" "$DATA" || log "WARN:账号$cur 当前数据保留在暂存位置"
        fi
        log "FAIL:账号$cur 归档提交失败"
        return 1
    fi
    if [ "$had_old" -eq 1 ]; then
        rm -rf "$previous" || log "WARN:账号$cur 旧快照副本需手动清理"
    fi
    log "已存档账号$cur ($(( $(date +%s) - t0 ))s)"
}

launch_gate() {
    local detail count
    detail=$(checkdetail 2>/dev/null)
    count=$(printf '%s\n' "$detail" | grep -c '=')
    if [ "$count" -ne 14 ] || printf '%s\n' "$detail" | grep -q '=fail'; then
        echo 'FAIL:启动前环境检查未通过'
        return 1
    fi
    return 0
}

# 快照复制:APFS 上用 clonefile(COW) 近乎瞬时,不改写已有快照;不支持时回退 ditto 全量复制
copy_tree() {
    src="$1"; dst="$2"
    if [ "${CLAUDE_SWITCHER_NO_CLONE:-0}" != "1" ] && cp -cRp "$src" "$dst" 2>/dev/null; then
        return 0
    fi
    rm -rf "$dst"
    ditto "$src" "$dst"
}

launch_claude() {
    timezone_valid "$TZ_TARGET" || { echo "FAIL:Claude 进程时区目标无效"; return 1; }
    quit_claude || { echo "FAIL:Claude 未能退出，请手动退出后重试"; return 1; }
    if ! open --env "TZ=$TZ_TARGET" -a "Claude"; then
        echo "FAIL:Claude 启动请求失败"
        return 1
    fi
}

switch_to() {
    target="$1"
    cur="none"; [ -f "$BASE/current" ] && cur=$(cat "$BASE/current")
    just_archived=0

    launch_gate || return 1

    # 首次运行:把当前登录中的状态收编为账号A
    if [ "$cur" = "none" ] && [ -d "$DATA" ] && [ -n "$(ls -A "$DATA" 2>/dev/null)" ]; then
        quit_claude || { echo "FAIL:Claude 未能退出，请手动退出后重试"; return 1; }
        archive_current A || { echo "FAIL:首次账号归档失败，已停止切换"; return 1; }
        just_archived=1
        cur="A"; echo "$cur" > "$BASE/current"
        log "首次运行:当前登录状态已收编为账号A"
        echo "NOTE:已将当前登录收编为账号A"
    fi

    if [ "$cur" = "$target" ]; then
        # 归档是移动语义,首次收编后需把当前账号还原回 DATA
        if [ "$just_archived" = "1" ] && [ ! -d "$DATA" ] && [ -d "$BASE/accounts/$target" ]; then
            copy_tree "$BASE/accounts/$target" "$DATA"
        fi
        launch_gate || return 1
        launch_claude || return 1
        echo "INFO:当前已是账号$target"
        return 0
    fi

    quit_claude || { echo "FAIL:Claude 未能退出，请手动退出后重试"; return 1; }

    if [ "$just_archived" = "0" ] && [ "$cur" != "none" ] && [ -d "$DATA" ]; then
        archive_current "$cur" || { echo "FAIL:账号$cur 归档失败，已停止切换"; return 1; }
    fi

    rm -rf "$DATA.new"
    t0=$(date +%s)
    if [ -d "$BASE/accounts/$target" ]; then
        copy_tree "$BASE/accounts/$target" "$DATA.new"
        log "已载入账号$target 存档 ($(( $(date +%s) - t0 ))s)"
    else
        mkdir -p "$DATA.new"
        log "账号$target 无存档，将以全新状态初始化"
    fi
    rm -rf "$DATA" && mv "$DATA.new" "$DATA"
    echo "$target" > "$BASE/current"

    launch_gate || return 1
    launch_claude || return 1
    log "已切换到账号$target 并启动 ($(( $(date +%s) - t0 ))s)"
    echo "OK:已切换到账号$target ($(( $(date +%s) - t0 ))s)"
    return 0
}

case "${1:-}" in
    code) code_command "$@" ;;
    code-health) code_health "${2:-}" ;;
    check)  check ;;
    checkdetail)
        OUT_FILE="$BASE/last-check.txt"
        {
            echo "# $(date '+%F %T') pid=$$ 开始"
            checkdetail
            echo "# 结束"
        } > "$OUT_FILE" 2>&1
        cat "$OUT_FILE"
        ;;
    fix) fix ;;
    status) status ;;
    switch) switch_to "${2:-}" ;;
    set-region) set_region ;;
    update) self_update "${2:-}"; exit $? ;;
    write-merge-rules) write_merge_rules ;;  # 隐藏入口:供安装器调用,保证两处合并逻辑一致
    fingerprint) fingerprint ;;
    subscription)
        case "${2:-}" in
            set) subscription_set "$@" ;;
            fetch) subscription_fetch ;;
            status) subscription_status ;;
            ports)
                if [ "$#" -gt 3 ] || { [ "$#" -eq 3 ] && [ "$3" != "--json" ]; }; then
                    echo "用法: claude-switch.sh subscription ports [--json]"; exit 2
                fi
                subscription_ports "${3:-}" ;;
            check) subscription_check ;;
            clear) subscription_clear ;;
            install) subscription_install ;;
            *) echo "用法: claude-switch.sh subscription set|fetch|status|ports|check|install|clear"; exit 2 ;;
        esac
        ;;
    *) echo "用法: claude-switch.sh check|checkdetail|fix|status|switch A|B|set-region|update [--auto]|fingerprint|subscription set|fetch|status|ports|check|install|clear|code install [--json]|code check [A|B]|code run [A|B] -- [Claude 参数]|diagnose-proxy [--proxy URL|--config FILE]"; exit 2 ;;
esac
