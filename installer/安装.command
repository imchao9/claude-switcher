#!/bin/bash
# Claude 切换器 - 通用安装程序 (macOS)
set -Ee -o pipefail
if [ "${1:-}" = "--code-antify" ]; then
    shift
    exec /bin/bash "$(dirname "$0")/安装Antify检测版.command" "$@"
fi
STAGE=""
APP_PENDING=""
cleanup() {
    if [ -n "$APP_PENDING" ] && [ -e "$APP_PENDING" ]; then rm -rf "$APP_PENDING"; fi
    if [ -n "$STAGE" ] && [ -d "$STAGE" ]; then rm -rf "$STAGE"; fi
}
fail() { echo "❌ $*" >&2; exit 1; }
trap cleanup EXIT
trap 'printf "\n❌ 安装失败（第 %s 行），未完成安装。请修复错误后重试。\n" "$LINENO" >&2' ERR
cd "$(dirname "$0")"
REPO="$(cd .. && pwd)"
H="$HOME"
clear 2>/dev/null || true
echo "=================================="
echo "  Claude 账号切换器 · 安装程序"
echo "=================================="
echo ""

# 国家代码 → 地区格式映射(未收录的国家回退 en_US)
locale_for() {
    case "$1" in
        US) echo en_US ;; JP) echo ja_JP ;; TW) echo zh_TW ;; HK) echo zh_HK ;;
        SG) echo en_SG ;; GB) echo en_GB ;; DE) echo de_DE ;; FR) echo fr_FR ;;
        KR) echo ko_KR ;; AU) echo en_AU ;; CA) echo en_CA ;; *) echo en_US ;;
    esac
}

# ---- 0. 前置检查 ----
# 先检查完整源码，避免缺文件时留下半套安装。
for source in core/claude-switch.sh core/stun-probe.py core/claude-code.py app/main.swift \
    daemons/claude-bodyguard.sh daemons/tz-watchdog.sh \
    daemons/local.claude-bodyguard.plist daemons/local.tz-watchdog.plist \
    daemons/local.claude-update.plist; do
    [ -f "$REPO/$source" ] || fail "缺少安装文件: ${source}。请使用完整的源码目录。"
done
CLASH_DIR="$H/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"
if [ -d "$CLASH_DIR" ]; then
    MODE="clash-verge"
elif scutil --nc list 2>/dev/null | grep -q 'com.liguangming.Shadowrocket'; then
    MODE="shadowrocket"
else
    echo "未自动识别到代理客户端,请手动选择:"
    echo "  1) clash-verge    2) shadowrocket    3) sing-box"
    echo "  4) clashx         5) surge           6) auto(任一命中即活)"
    read -r -p "请输入序号或名称: " pick
    case "$pick" in
        1|clash-verge)  MODE="clash-verge" ;;
        2|shadowrocket) MODE="shadowrocket" ;;
        3|sing-box)     MODE="sing-box" ;;
        4|clashx)       MODE="clashx" ;;
        5|surge)        MODE="surge" ;;
        6|auto)         MODE="auto" ;;
        *) echo "未识别的选择,退出"; exit 1 ;;
    esac
fi
echo "✓ 代理客户端: $MODE"
if [ ! -d /Applications/Claude.app ]; then
    echo "⚠️  未检测到 Claude 桌面版(/Applications/Claude.app),请先安装。"
    read -n 1 -s -r -p "按任意键退出..."; exit 1
fi
if ! python3 --version >/dev/null 2>&1; then
    echo "⚠️  未检测到 python3,请先安装 Xcode 命令行工具: xcode-select --install"
    read -n 1 -s -r -p "按任意键退出..."; exit 1
fi
if ! command -v swiftc >/dev/null 2>&1; then
    echo "⚠️  未检测到 swiftc(界面 App 需要现场编译),请先运行: xcode-select --install"
    echo "   注意:macOS 26/27 上仅装 Command Line Tools 可能因缺少 SwiftUIMacros 插件编译失败,"
    echo "   如遇失败请安装完整 Xcode 并执行: sudo xcode-select -s /Applications/Xcode.app/Contents/Developer"
    read -n 1 -s -r -p "按任意键退出..."; exit 1
fi
# 独立安装的 Swift 工具链不会自动找到 Apple SDK，不能只判断 swiftc 是否存在。
if ! MACOS_SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || [ ! -d "$MACOS_SDK" ]; then
    fail "未找到 macOS SDK，请检查 xcode-select -p 或安装 Xcode 命令行工具。"
fi
echo "✓ 前置检查通过($MODE / Claude / python3 / swiftc / macOS SDK)"

# ---- 1. 自动探测代理组名(仅 Clash Verge;多个 select 组时让用户选,避免静默选错专线) ----
GRP=""
if [ "$MODE" = "clash-verge" ]; then
    GRPS=()
    while IFS= read -r g; do GRPS+=("$g"); done < <(awk '
        /^proxy-groups:/ {f=1; next}
        f && /^[a-zA-Z]/ {f=0}
        f && /- name:/ { n=$0; sub(/.*- name: */, "", n); gsub(/"/, "", n) }
        f && /type: *select/ { print n }
    ' "$CLASH_DIR/clash-verge.yaml" 2>/dev/null || true)
    if [ "${#GRPS[@]}" -eq 0 ]; then
        echo ""
        echo "未能自动识别你的代理组名,请打开 Clash Verge 查看「代理」页面顶部的组名。"
        read -r -p "请输入代理组名: " GRP
        [ -z "$GRP" ] && { echo "未输入,退出"; exit 1; }
    elif [ "${#GRPS[@]}" -eq 1 ]; then
        GRP="${GRPS[0]}"
    else
        echo ""
        echo "检测到多个代理组,请选择用于 Claude 专线的那个:"
        i=1
        for g in "${GRPS[@]}"; do echo "  $i) $g"; i=$((i+1)); done
        read -r -p "请输入序号: " pick
        case "$pick" in ''|*[!0-9]*) echo "输入无效,退出"; exit 1 ;; esac
        if [ "$pick" -lt 1 ] || [ "$pick" -gt "${#GRPS[@]}" ]; then
            echo "序号超范围,退出"; exit 1
        fi
        GRP="${GRPS[$((pick-1))]}"
    fi
    echo "✓ 代理组: $GRP"
fi

IPINFO=$(curl -fsS --max-time 20 --retry 2 --retry-all-errors https://ipinfo.io/json 2>/dev/null || true)
CTRY=$(echo "$IPINFO" | grep '"country"' | cut -d'"' -f4 || true)
if [ "$CTRY" = "CN" ] && [ -z "$FORCE" ]; then
    echo ""
    echo "❌ 检测到当前出口在国内(国家: CN)。"
    echo "   请先在代理客户端里开启代理(TUN模式)并确认节点可用,再运行本安装程序。"
    echo "   (否则进程时区与地区目标可能不匹配)"
    read -n 1 -s -r -p "按任意键退出..."; exit 1
fi
if [ -z "$CTRY" ]; then
    echo "⚠️  未能识别出口国家,请确认代理已开启后重试(或用 FORCE=1 跳过)。"
    [ -z "$FORCE" ] && { read -n 1 -s -r -p "按任意键退出..."; exit 1; }
    CTRY="US"
fi
TZV=$(echo "$IPINFO" | grep '"timezone"' | cut -d'"' -f4 || true)
[ -z "$TZV" ] && TZV="America/New_York"
LOCALE=$(locale_for "$CTRY")
echo "✓ 地区自适应: 国家=$CTRY 进程时区=$TZV 地区格式=$LOCALE (按当前出口IP自动识别)"

# ---- 3. 先在临时目录构建完整 App，失败时不改动现有安装 ----
STAGE=$(mktemp -d)
APP="$STAGE/Claude切换器.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
# 必须带 -target,否则新 SDK 编出的二进制在老系统被拒启动
if swiftc -sdk "$MACOS_SDK" -module-cache-path "$STAGE/module-cache" \
    -O -parse-as-library -target "$(uname -m)-apple-macos13.0" \
    "$REPO/app/main.swift" -o "$APP/Contents/MacOS/ClaudeSwitcher" \
    -framework SwiftUI -framework AppKit; then
    echo "✓ App 编译完成"
else
    echo "✗ App 编译失败。若日志提示 SwiftUIMacros/插件缺失(macOS 26/27 常见),"
    echo "  请安装完整 Xcode 后执行: sudo xcode-select -s /Applications/Xcode.app/Contents/Developer"
    exit 1
fi
# 图标: assets/ 里有 PNG 源文件时现场生成 icns
PNG="$REPO/assets/icon-2048.png"
if [ -f "$PNG" ]; then
    ICONSET="$STAGE/AppIcon.iconset"
    mkdir -p "$ICONSET"
    for sz in 16 32 128 256 512; do
        sips -z "$sz" "$sz" "$PNG" --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null 2>&1
        sips -z $((sz*2)) $((sz*2)) "$PNG" --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null 2>&1
    done
    iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns" 2>/dev/null \
        && echo "✓ 图标已生成" || echo "⚠️  图标生成失败(不影响使用)"
fi
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleExecutable</key><string>ClaudeSwitcher</string>
	<key>CFBundleIdentifier</key><string>local.claude-switcher</string>
	<key>CFBundleName</key><string>Claude切换器</string>
	<key>CFBundleDisplayName</key><string>Claude切换器</string>
	<key>CFBundlePackageType</key><string>APPL</string>
	<key>CFBundleShortVersionString</key><string>1.0</string>
	<key>CFBundleVersion</key><string>1</string>
	<key>LSMinimumSystemVersion</key><string>13.0</string>
	<key>CFBundleIconFile</key><string>AppIcon</string>
	<key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF
codesign --force --deep -s - "$APP"
codesign --verify --deep --strict "$APP"

if [ -n "$DRYRUN" ]; then
    echo "[演练模式] App 构建通过；未写入安装文件、代理配置或系统设置，不代表安装验收通过。"
    exit 0
fi


# ---- 4. 安装已准备好的文件 ----
mkdir -p "$H/.local/bin" "$H/.claude-switcher" "$H/Library/Logs"
cp "$REPO/core/claude-switch.sh" "$H/.local/bin/"
cp "$REPO/core/stun-probe.py" "$H/.claude-switcher/"
cp "$REPO/core/claude-code.py" "$H/.claude-switcher/"
chmod +x "$H/.local/bin/claude-switch.sh"
# 写入探测到的客户端/代理组名/地区目标
sed -i '' \
    -e "s|^PROXY_MODE=\".*\"|PROXY_MODE=\"$MODE\"|" \
    -e "s|^PROXY_GROUP=\".*\"|PROXY_GROUP=\"$GRP\"|" \
    -e "s|^COUNTRY_TARGET=\".*\"|COUNTRY_TARGET=\"$CTRY\"|" \
    -e "s|^TZ_TARGET=\".*\"|TZ_TARGET=\"$TZV\"|" \
    -e "s|^LOCALE_TARGET=\".*\"|LOCALE_TARGET=\"$LOCALE\"|" \
    "$H/.local/bin/claude-switch.sh"

sed -e "s|__HOME__|$H|g" -e "s|__PROXY_MODE__|$MODE|g" \
    "$REPO/daemons/claude-bodyguard.sh" > "$STAGE/claude-bodyguard.sh"
chmod +x "$STAGE/claude-bodyguard.sh"
# 旧版守护脚本留到系统级迁移成功后再清理，避免中途失败时旧服务失效。

cp "$REPO/daemons/local.claude-bodyguard.plist" "$H/.local/bin/"
sed "s|__HOME__|$H|g" "$REPO/daemons/local.claude-update.plist" > "$H/.local/bin/local.claude-update.plist"
echo "✓ 核心文件已安装"

mkdir -p "$H/Desktop"
PENDING_PATH="$H/Desktop/.Claude切换器-installing.app"
[ ! -e "$PENDING_PATH" ] || fail "桌面存在未完成的安装: ${PENDING_PATH}，请先检查并移走。"
APP_PENDING="$PENDING_PATH"
cp -R "$APP" "$APP_PENDING"
pkill -x ClaudeSwitcher 2>/dev/null || true
APP_DEST="$H/Desktop/Claude切换器.app"
if [ -e "$APP_DEST" ]; then mv "$APP_DEST" "$STAGE/previous.app"; fi
if ! mv "$APP_PENDING" "$APP_DEST"; then
    if [ -e "$STAGE/previous.app" ]; then
        if ! mv "$STAGE/previous.app" "$APP_DEST"; then
            echo "❌ 旧 App 无法自动恢复，已保留在 $STAGE/previous.app" >&2
            STAGE=""  # 保留备份，不能在 EXIT 时删除。
        fi
    fi
    fail "桌面 App 安装失败。"
fi
echo "✓ App 已放到桌面"

# ---- 5. 写入置顶规则(仅 Clash Verge;智能合并不覆盖已有配置;其他客户端自行配置) ----
if [ "$MODE" = "clash-verge" ]; then
    # 复用核心脚本里的 write_merge_rules(隐藏入口),保证与 fix() 行为一致
    if "$H/.local/bin/claude-switch.sh" write-merge-rules; then
        echo "✓ Claude 域名置顶规则已写入(智能合并,不动已有配置;重启 Clash Verge 后生效)"
    else
        fail "置顶规则写入失败，安装未完成。请检查 Clash 配置后重试。"
    fi
else
    echo "ℹ️  当前客户端为 $MODE,请自行在客户端中添加 DOMAIN-SUFFIX 置顶规则:"
    echo "    claude.ai / claude.com / anthropic.com / claudeusercontent.com"
    echo "    challenges.cloudflare.com / datadoghq.com / segment.io"
    echo "    stripe.com / stripe.network / stripecdn.com"
    echo "    intercom.io / intercomcdn.com / intercomassets.com"
fi

# ---- 6. 系统级配置(需要一次密码;独立 shell 显式启用失败即停) ----
echo ""
echo "接下来需要输入一次开机密码,用于: 安装守护进程(系统目录) + 停用旧时区看门狗 + 关闭IPv6"
# 参数单独传递，不把用户目录或时区拼接成 root shell 代码。
if sudo /bin/bash -e -s -- "$STAGE" "$H/.local/bin" <<'ROOT_SETUP'
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
STAGE="$1"
BIN="$2"
SYS_DIR="/Library/Application Support/claude-switcher"
mkdir -p "$SYS_DIR"
chown root:wheel "$SYS_DIR"
chmod 755 "$SYS_DIR"
cp "$STAGE/claude-bodyguard.sh" "$SYS_DIR/"
chown root:wheel "$SYS_DIR/claude-bodyguard.sh"
chmod 755 "$SYS_DIR/claude-bodyguard.sh"
cp "$BIN/local.claude-bodyguard.plist" /Library/LaunchDaemons/
chown root:wheel /Library/LaunchDaemons/local.claude-bodyguard.plist
chmod 644 /Library/LaunchDaemons/local.claude-bodyguard.plist
launchctl bootout system/local.claude-bodyguard 2>/dev/null || true
launchctl bootstrap system /Library/LaunchDaemons/local.claude-bodyguard.plist
launchctl print system/local.claude-bodyguard >/dev/null
launchctl disable system/local.tz-watchdog
launchctl print-disabled system | grep -Eq '^[[:space:]]*"local[.]tz-watchdog"[[:space:]]*=>[[:space:]]*(true|disabled)[[:space:];,]*$' >/dev/null || { echo "旧时区看门狗未确认禁用" >&2; exit 1; }
if launchctl print system/local.tz-watchdog >/dev/null 2>&1; then
    launchctl bootout system/local.tz-watchdog
else
    status=$?
    [ "$status" -eq 113 ] || { echo "无法查询旧时区看门狗" >&2; exit 1; }
fi
if launchctl print system/local.tz-watchdog >/dev/null 2>&1; then
    echo "旧时区看门狗仍在运行" >&2; exit 1
else
    status=$?
    [ "$status" -eq 113 ] || { echo "无法确认旧时区看门狗已停止" >&2; exit 1; }
fi

v6=$(networksetup -getinfo Wi-Fi)
if ! echo "$v6" | grep -qi '^IPv6:.*off'; then networksetup -setv6off Wi-Fi; fi
networksetup -getinfo Wi-Fi | grep -qi '^IPv6:.*off' || { echo "IPv6 关闭未生效" >&2; exit 1; }
dscacheutil -flushcache
killall -HUP mDNSResponder
# 新守护服务确认可用后再清理旧版 root 自动更新。
launchctl bootout system/local.claude-update 2>/dev/null || true
rm -f /Library/LaunchDaemons/local.claude-update.plist
ROOT_SETUP
then
    echo "✓ 系统级配置完成(保镖/旧看门狗停用/IPv6 已读回确认)"
else
    fail "系统级配置未完成（授权取消或命令失败），请处理错误后重新安装。"
fi
rm -f "$H/.local/bin/claude-bodyguard.sh" "$H/.local/bin/tz-watchdog.sh" \
      "$H/.local/bin/claude-bodyguard.sh.bak" "$H/.local/bin/tz-watchdog.sh.bak"

# 自动更新:用户态 LaunchAgent(每12小时)，注册失败不能宣称已启用。
mkdir -p "$H/Library/LaunchAgents"
cp "$H/.local/bin/local.claude-update.plist" "$H/Library/LaunchAgents/local.claude-update.plist"
launchctl bootout "gui/$(id -u)/local.claude-update" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$H/Library/LaunchAgents/local.claude-update.plist"
launchctl print "gui/$(id -u)/local.claude-update" >/dev/null
echo "✓ 自动更新服务已注册(每12小时检查一次)"

# ---- 7. 重启 Clash 使规则生效(仅 Clash Verge) ----
if [ "$MODE" = "clash-verge" ]; then
    osascript -e 'tell application "Clash Verge" to quit' >/dev/null 2>&1
    sleep 2
    open -a "Clash Verge" >/dev/null 2>&1
fi

# ---- 8. 自检 ----
echo ""
echo "=================================="
echo "  自检结果 (checkdetail)"
echo "=================================="
if ! CHECK_OUT=$("$H/.local/bin/claude-switch.sh" checkdetail 2>&1); then
    fail "自检命令执行失败，安装尚未通过验收。请在 App 中检查详情。"
fi
# checkdetail 目前不以退出码表示红项，必须核对 14 个唯一检查项的实际状态。
# 仅显示状态，避免在安装输出中暴露出口 IP 等详情。
if ! printf '%s\n' "$CHECK_OUT" | awk -F= '
    BEGIN {
        split("代理内核 TUN网卡 出口IP IP一致 时区 地区 IPv6 DNS WebRTC 保镖 规则置顶 客户端语言 ASN属性 生态可达性", keys, " ")
        for (i in keys) expected[keys[i]]=1
    }
    $1 in expected {
        count++
        split($2, state, ":")
        status=state[1]
        if (status !~ /^(ok|warn|fail)$/) status="invalid"
        if (seen[$1]++ || (status != "ok" && !($1 == "时区" && status == "warn"))) bad=1
        print $1 "=" status
    }
    END { if (count != 14 || bad) exit 1 }
'; then
    fail "安装文件已写入，但 14 项安检尚未通过。请在 App 中处理红项及其他警告后复查。"
fi

echo ""
echo "=================================="
echo "  ✅ 安装完成!"
echo "  使用: 双击桌面「Claude切换器」"
echo "  首次使用: 打开后选账号A(自动收编当前登录)"
echo "  换了其他国家节点后: 运行 claude-switch.sh set-region 一键重新适配"
echo "  自动更新已启用(每12小时检查一次),也可手动: claude-switch.sh update"
echo "=================================="
if [ -t 0 ]; then read -n 1 -s -r -p "按任意键关闭窗口..." || true; fi
