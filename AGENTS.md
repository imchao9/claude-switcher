# AGENTS.md —— Agent 交接提示词

> 这份文件写给 AI Agent。用户把本项目交给你时，请按本文档执行安装与配置。
> 用户也可以直接把本文件内容粘贴给任意本地模型（Kimi Code / Cursor / Claude Code 等）作为指令。

---

## 你的任务

帮助用户在 macOS 上安装并配置「Claude 切换器」：一个带环境安检的 Claude 账号启动/切换工具。目标状态是：**用户双击桌面图标，14 项检查无失败项，可以正常启动 Claude；进程时区未实测项保持提醒**。

## 开始之前：必须先确认的前置条件

逐项检查，缺哪项就先补哪项：

1. **macOS**（Apple Silicon 或 Intel 均可）
2. **代理客户端**已安装且代理可用，支持 Clash Verge / Shadowrocket / sing-box / ClashX / Surge 任一（TUN 可用）。Clash Verge 的配置目录应存在于 `~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/`；Shadowrocket 以 VPN 配置形式存在（`scutil --nc list` 可见）
3. **Claude 桌面版** 在 `/Applications/Claude.app`
4. **python3** 可用（`python3 --version` 能输出版本号；没有则引导用户运行 `xcode-select --install`）
5. **当前出口 IP 所在国家不能是 CN**——先跑 `curl -s https://www.cloudflare.com/cdn-cgi/trace | grep loc=`，返回的 `loc` 不是 `CN` 才能继续。是 CN 就停下，让用户先把代理调好（安装程序会按出口 IP 自动锁定地区目标：国家/时区/地区格式三件套，裸连时装机会把它们锁错）

## 安装步骤

首选引导用户运行安装器（双击 `installer/安装.command`，或终端执行它）。如果安装器环境受限，按下面的手动等效步骤执行：

1. 把 `core/claude-switch.sh` 复制为 `~/.local/bin/claude-switch.sh`（可执行），把 `core/stun-probe.py` 放到 `~/.claude-switcher/`
2. 探测代理客户端并写进脚本顶部 `PROXY_MODE="..."`：存在 Clash Verge 配置目录 → `clash-verge`；否则 `scutil --nc list` 有 Shadowrocket VPN 配置 → `shadowrocket`；都没有则让用户在 clash-verge/shadowrocket/sing-box/clashx/surge/auto 中选择
3. Clash Verge 模式下，从 `clash-verge.yaml` 的 `proxy-groups:` 找第一个 `type: select` 的组名写进 `PROXY_GROUP="..."`（其他客户端跳过）
4. 按出口 IP 自动锁定地区目标：`curl -s ipinfo.io/json` 的 country → `COUNTRY_TARGET`，timezone → 桌面启动进程的 `TZ_TARGET`，country 经映射表（US→en_US、JP→ja_JP、TW→zh_TW、HK→zh_HK、SG→en_SG、GB→en_GB、DE→de_DE、FR→fr_FR、KR→ko_KR、AU→en_AU、CA→en_CA，其余→en_US）→ `LOCALE_TARGET`，三项都写进脚本头部
5. 从 `daemons/` 物化保镖脚本，把 `__HOME__` / `__PROXY_MODE__` 替换为实际值，装入 root:wheel、755 的 `/Library/Application Support/claude-switcher/`。只注册 `local.claude-bodyguard`。不安装或注册 `local.tz-watchdog`；获授权升级安装时停用旧时区服务。自动更新仍为用户态 `local.claude-update` LaunchAgent，每 12 小时运行。系统时区由用户设置，Agent 不执行 `systemsetup -settimezone`。
6. 现场编译界面 App（需要 Xcode 命令行工具，显式传入 macOS SDK 以兼容独立 Swift 工具链，并带 `-target` 避免新 SDK 产物在老系统拒启动）：
   ```bash
   swiftc -sdk "$(xcrun --sdk macosx --show-sdk-path)" -O -parse-as-library -target "$(uname -m)-apple-macos13.0" app/main.swift -o Claude切换器.app/Contents/MacOS/ClaudeSwitcher -framework SwiftUI -framework AppKit
   ```
   图标用 `assets/icon-2048.png` 经 sips 生成 iconset 再 `iconutil -c icns` 打进 `Contents/Resources/AppIcon.icns`，`Info.plist` 声明 `CFBundleIconFile`，最后 `codesign --force --deep -s -`
7. Clash Verge 模式下通过核心脚本的 `write-merge-rules` 隐藏入口生成「全局扩展脚本」(profiles/Script.js)，直接前置 `config.rules`，保留已有 main，备份并迁移 Merge.yaml 中本项目旧标记块；重启 Clash Verge 后必须验证内核 `/rules`。其他客户端提示用户自行添加 DOMAIN-SUFFIX 置顶规则
8. 系统时区由用户自行设置，不修改。IPv6 按现有安装流程检查，必要时 `networksetup -setv6off Wi-Fi`（需管理员）。
9. 收尾自检：运行 `~/.local/bin/claude-switch.sh checkdetail` 确认各项状态

日常维护命令：`check`（静默总检）/ `checkdetail`（14 项逐项）/ `fix`（一键修复）/ `switch A|B|C|D`（切换账号）/ `set-region`（换国家节点后重锁地区目标）/ `set-client <mode>`（安装后更换代理客户端：clash-verge|shadowrocket|sing-box|clashx|surge|auto）/ `fingerprint`（设备指纹体检：字体地区特征/GPU/虚拟机；Canvas/WebGL 用 Web 检测页）/ `cli-info`（Claude Code CLI 只读体检：版本/安装方式/多份安装冲突，不执行卸载升级）/ `open-safe <url>`（一次性隔离浏览器打开 http/https 链接；代理未就绪会拒绝，窗口关闭后销毁临时 profile）/ `snapshot-export <A|B|C|D> <file>` / `snapshot-import <file> <A|B|C|D>`（macOS 加密快照导出/导入，AES-256 DMG，密码经 stdin，目标槽位非空会拒绝）/ `update [--check|--auto]` / `update-check`（自动更新：`update-check` 为只读检查入口，只下载并校验、比对配置行后报告是否为新版本，不备份不写入；旧版核心不识别该入口时会安全退出，不会误触发真实更新；`update --check` 在新版中等价；`--auto` 由用户态 LaunchAgent 每 12 小时调用、输出落 `~/.claude-switcher/update.log`；手动跑则直接打印。完整流程：拉取 main → 逐项 SHA256 校验工件（对照仓库 SHA256SUMS，不符即中止）→ 剔除 5 个配置行比对去重 → 备份 .bak → 注入配置 → 守护脚本仅比对并提示 → 自检，含任何 `=fail` 自动回滚）

跨身份保留（可选，默认关闭）：`~/.claude-switcher/keep-in-workspace.txt` 每行一个相对 Claude 数据目录的路径（如 `settings.json`、`skills/`），`#` 注释、空行忽略。切换时先收进 `~/.claude-switcher/keep-stage/`，还原目标账号后合并回去；绝对路径、含 `..` 和符号链接会跳过并记 WARN。文件不存在或为空时行为与未启用完全一致。只放通用配置，不要放登录态/缓存等账号专属文件。

## 独立代理诊断

`bash core/claude-switch.sh diagnose-proxy --config /path/to/private-proxy.json` 只验证明确指定代理到 Claude 网页/API 的无认证传输，详见 README。它可直接从仓库运行，不适用上述安装/启动前置条件，不启动或关闭应用，不创建账号状态、不修改系统设置。输出 JSON；401/403 分别标注认证或访问状态，传输到达不等于账号可用、启动许可或防泄漏通过。不要为运行诊断先安装后台或改 TUN/时区。

隔离回归：`python3 -m unittest discover -s tests -v`。测试使用本机假 curl 和隔离 HOME，不使用真实登录资料或代理。

## Claude Code（macOS）

检测窗口可在 CLI 缺失时显式安装官方 Claude Code。`code install [--json]` 复用现有助手，固定使用官方原生安装入口；不在检查或 Antify 安装时自动执行。不接受账号、路由、时区或 Claude 透传参数。已有 CLI 保留，不自动升级或登录。安装后必须通过可执行文件与版本验证才能报告完成；取消不等于安装文件已回滚。CLI 查找共用 PATH 与 `~/.local/bin` 回退。

开发验收使用假下载和隔离 HOME，不运行真实官方安装脚本，不用安装结果代替网络、登录或 Antify 保护验收。用户此前暂停 Claude 域名实测的要求仍适用。

`code run [A|B] --timezone America/Chicago -- [Claude 参数]` 只为该子进程传入 `TZ`，不改系统时区。省略参数时保留已有环境和客户端配置。桌面版使用 `TZ_TARGET` 经 LaunchServices 传入环境，必须先退出旧进程；不能将传入值视为已验证渲染或原生组件采用。

`bash core/claude-switch.sh code check [A|B]` 只读检查 CLI、账号目录、代理、TUN、出口国家和已配置的订阅绑定。`code run [A|B] -- [Claude 参数]` 检查通过后启动并监督本次进程组，不依赖桌面 App、桌面保镖或桌面版 14 项检查。运行期检查失败会终止该进程组；直接运行 `claude` 或 IDE 扩展不受此入口保护。

A/B 通过官方 `CLAUDE_CONFIG_DIR` 使用 `~/.claude-switcher/code/A`、`B`，首次分别人工登录。不复制现有凭据、Keychain 或默认配置，不修改桌面账号存档结构。不带 API key 的 Console 登录不支持此目录隔离。省略 A/B 时保留现有默认配置选择。真实登录验收须单独说明，不用假 CLI 测试冒充。

`core/claude-code.py` 是用户态会话监督器，安装器和更新器将其分发到 `~/.claude-switcher/claude-code.py`。变更时保持 `scripts/gen-sums.sh` 与 `SHA256SUMS` 包含此工件。维护者本机部署禁令同样适用该文件。旧更新器首次升级后需再执行一次新版 `update` 或重跑安装器才能补齐助手文件。

## Antify 专用安装与检测

用户选择 Antify 且不希望修改系统时区时，使用 `installer/安装Antify检测版.command` 或 `installer/安装.command --code-antify`。该入口在桌面安装前置条件之前分流，不检查 Claude.app、不执行系统时区、网络、后台服务或旧桌面切换器变更。`--dry-run` 不执行下载、编译、安装和启动。

安装器验证官方 Antify DMG 的 Bundle ID、Team ID、签名和 Gatekeeper，保留已验证的 0.0.82 或更新版本。独立检测 App 为 `~/Desktop/ClaudeCode环境检测.app`，助手与核心副本在 `~/.claude-switcher/code-tools/`。更新重新执行专用安装器；旧更新器与卸载器不管理它。

`code check [A|B] --route antify --json` 仅报告 CLI、目录、安装、扩展授权和待核验的路由与保护。不得发起 Claude 服务请求，不读取 Antify 私有数据库、凭据或配置。扩展已批准不能推断代理运行，路由与 Kill Switch 不自动通过。`code run --route antify` 拒绝启动。首次系统扩展与内容过滤器授权由用户决定，安装器不自动授权或改变 TUN。

## 验收标准（全部满足才算装完）

- 终端执行 `~/.local/bin/claude-switch.sh checkdetail` 输出 14 行且无 `=fail`；进程时区实际采用情况保持 `warn`，不伪造已通过
- 双击桌面 App，检查无失败项，能正常启动 Claude；提醒项仍按实际状态展示
- 保镖 `system/local.claude-bodyguard` 与用户态更新 `gui/$(id -u)/local.claude-update` 已注册，旧 `system/local.tz-watchdog` 已停用
- `/Library/Application Support/claude-switcher/` 下保镖脚本属 root:wheel、权限 755，当前用户不可写
- 切换一次账号 B 再切回 A，A 的登录态完好

## 排障手册（以下全是实战中踩过的坑，按症状直接查）

| 症状 | 根因 | 处理 |
|---|---|---|
| TUN 网卡消失、出口变回国内 | **Clash Verge 更新/异常重启会把 `enable_tun_mode` 重置为 false**（生产环境已发生 4 次） | 退出 App → 改 `verge.yaml` 为 true → 重启 App。切换器的一键修复已内置此逻辑 |
| 时区总是自己变回去 | macOS 自动设置或遗留的时区看门狗 | 系统时区由用户设置；只在获授权后停用旧 `local.tz-watchdog`，不替用户设置目标时区 |
| 检查结果显示 0/11、数据为空 | 中文日志被 `cut -c` 按字节截断产生非法 UTF-8，Swift 严格解码整体失败 | 脚本必须 `export LC_ALL=en_US.UTF-8`；界面端用容错解码 `String(decoding:as:)` |
| 修改 App 后界面没变化 | 菜单栏版本关闭窗口仅隐藏；`pkill` 匹配中文路径还可能因 Unicode NFD 编码失配而静默失败 | 先从盾牌菜单选「退出 Claude 切换器」，再替换 App；开发排障也可按进程名退出测试实例。不要把关窗改回退出，以免破坏菜单栏驻留 |
| headless Chrome 做 WebRTC 检测无结果 | headless 模式 ICE 子系统不产出候选 | 不要走浏览器路线，用本项目的纯 Python STUN 探针 |
| 代理节点报错但流量没泄漏 | 节点被重置时隧道内请求失败≠泄漏；Fail-Closed 下流量只断不绕 | 换线路即可 |
| 切换账号报「存在待恢复快照，拒绝覆盖」 | 上次归档被中断，`~/.claude-switcher/accounts/<账号>.new` 或 `.previous` 残留（可能是唯一副本） | 先确认 `<账号>/` 是否完整；若有 `.previous` 且正式目录缺失，用它恢复；确认无价值后再删除 `.new`/`.previous` 重试 |
| 规则置顶显示「未生效」/检查提示运行规则未验证 | Clash 处于 global 模式时规则本不生效(正常);rule 模式下多为 Script.js 未启用、代理组不存在或控制 socket 失效 | 全局模式无需处理;rule 模式先确认全局扩展脚本启用与代理组名,再跑 `fix`;控制接口失联时会自动回退服务 socket |
| WebRTC 检测失败但 TUN 正常 | STUN 被国内直连规则匹配、全局脚本未启用、代理组或节点 UDP 能力不符 | 运行 `fix`，通过全局 Script.js 加 UDP 3478/5349/19302 规则，重启后验证内核 rule 模式、规则顺序和实际代理组。默认 proxy；`WEBRTC_POLICY=reject claude-switch.sh fix` 可切换并持久化拒绝策略（影响部分通话）。不要再向全局 Merge.yaml 手写 prepend-rules，不要把无 STUN 响应视为零暴露 |
| 切换账号后对方看到"新设备" | 正常：快照隔离意味着每个账号有独立设备指纹，这正是设计目标 | 无需处理 |
| 切换后某些文件没过去／不该过去 | 跨身份保留默认关闭，只复制 `~/.claude-switcher/keep-in-workspace.txt` 中列出的相对路径；绝对路径、含 `..`、符号链接会被跳过 | 对照该文件与 `~/Library/Logs/claude-switch.log` 的 WARN；只保留通用配置，不要把登录态/缓存列进去 |

## 安全红线（Agent 必须遵守）

1. **绝不在出口国家与目标不一致时启动 Claude**——任何时候先验 cloudflare trace 的 `loc` 与脚本头部 `COUNTRY_TARGET` 一致；换了国家节点先跑 `claude-switch.sh set-region` 再启动
2. 切换账号前必须先完全退出 Claude 进程，再交换数据目录
3. 不要在日志、文档、Issue 里暴露用户的住宅/代理 IP 与任何凭据
4. 不要改动 `~/.claude-switcher/accounts/` 下的存档结构
5. **维护纪律**：`core/`、`daemons/`、`installer/` 下任何文件改动后，提交前必须运行 `scripts/gen-sums.sh` 重新生成根目录 `SHA256SUMS` 并同提交——自动更新靠它校验工件完整性，清单与代码不一致会导致所有用户更新失败
6. **守护进程脚本只在 root 目录**：`/Library/Application Support/claude-switcher/` 下的保镖/看门狗属 root:wheel，任何功能都不要改回写 `~/.local/bin/` 下的同名脚本（用户可写=提权面）；需要变更时引导用户重跑安装器

## Windows 版（windows/ 目录, Beta）

Windows 用户请使用 `windows/` 下的 PowerShell 版本。关键差异与注意事项：

- **文件编码**：所有 `.ps1` 文件必须保持 **UTF-8 with BOM**（Windows PowerShell 5.1 无 BOM 会把中文读成乱码）。编辑后若中文变乱码，重新加 BOM。
- **时区 ID 体系不同**：Windows 用 `Eastern Standard Time` 这类 ID，安装器负责把 IANA 时区（America/New_York）映射过去，见 `install.ps1` 的 `$tzMap`。
- **权限模型**：`install.ps1` 需要管理员运行（注册计划任务/锁时区/关 IPv6）；日常使用中「一键修复」通过 `Start-Process -Verb RunAs` 触发 UAC 弹窗。
- **保镖实现**：用计划任务（AtLogOn 触发）启动隐藏 PowerShell 常驻进程，10 秒巡检，与 Mac 版行为一致。
- **robocopy 退出码**：0-7 都是成功，8+ 才是失败——判断切换成败时不要按常规退出码判断。
- **账号数据目录**：`%APPDATA%\Claude`（不是 macOS 的 `~/Library/Application Support/Claude`）。
- **已知限制**：WPF 界面为第一版重写，未经大规模真机测试；Clash Verge 的 TUN 开关被更新重置后在 Windows 端需手动重开。
- **自动更新**：启动时静默检查 GitHub 最新 Release，有新版显示提示条；一键更新会下载 zip、校验 SHA256、备份并替换用户目录脚本、重启保镖任务和界面，失败自动回滚，不需要管理员。发布 zip 根必须带 `VERSION`（`release.yml` 已写入），安装后落到 `%USERPROFILE%\.claude-switcher\version.txt`。
- **Clash 规则**：一键修复先识别 Clash 模式（Mihomo API `/configs`，失败回退 `clash-verge.yaml`/`verge.yaml` 的 `mode:`）。全局/直连模式不写规则，仅清理旧 Merge；规则模式只写 `profiles\Script.js` 托管块，重启后用内核 `/rules` 验证前 16 条，失败回滚。安装器不再写 `Merge.yaml`，旧 `prepend-rules`/`---` 会被清理并备份。
- SmartScreen 首次运行拦截属正常现象（未签名脚本），引导用户「更多信息 → 仍要运行」。

## Linux 版（linux/ 目录, Beta）

Linux 用户使用 `linux/` 下的 bash 版本。**开始之前必须先理解最大的前提差异**：

- **Claude 官方没有 Linux 桌面版**——使用形态是「网页版(浏览器) + Claude Code CLI」。因此：
  - 账号切换切换的是**浏览器配置目录**（`~/.config/google-chrome` 或 `~/.config/chromium`），即切换 Web 登录态；切换前必须完全退出浏览器
  - 保镖只终止 `claude` CLI 进程，**不要让它去杀浏览器**（网页端保护交给 TUN + 置顶规则）
- **多客户端**：`PROXY_MODE` 支持 clash-verge / mihomo / sing-box / v2raya / nekobox / hiddify / wireguard / other / auto；非 Clash 客户端不做配置改写，只做 fake-ip 行为检测并提示用户自行加 DOMAIN-SUFFIX 规则；`wireguard` / `other` 为通用虚拟网卡启发式判定
- **地区自适应**：`COUNTRY_TARGET` / `TZ_TARGET` / `LOCALE_TARGET` 由安装器按出口自动写入；`CN` 出口拒绝安装；换国家节点后手动改这三个值（Linux 版暂无 `set-region` 命令）
- **常驻服务**：systemd 用户单元（`~/.config/systemd/user/`，免 sudo、登录自启）
- **系统设置**：时区用 `timedatectl set-timezone`（sudo）；IPv6 用 `sysctl -w net.ipv6.conf.all.disable_ipv6=1` + `/etc/sysctl.d/` 持久化
- **DNS 查询工具**：`dig` → `nslookup` → `getent` 逐级回退（部分发行版默认无 dig，脚本已内置回退，勿改成 dig 硬依赖）
- **界面**：无 GUI，终端菜单（`claude-switch menu`）+ CLI 参数
- **发行版差异**：安装器不区分发行版，但依赖 systemd；非 systemd 发行版（如 Alpine/void 部分配置）需要手工替代保镖常驻方式
- 配置目录路径：Clash Verge Linux 版配置在 `~/.config/io.github.clash-verge-rev.clash-verge-rev/`（脚本已兼容两种常见路径）

## 文件索引

- 核心逻辑：`core/claude-switch.sh`（安检/切换/修复全在这一个文件）
- 泄漏探针：`core/stun-probe.py`
- 界面：`app/main.swift`（单文件 SwiftUI）
- 守护进程模板：`daemons/`
- 安装器：`installer/`
- Windows 版：`windows/`（核心 `claude-switch.ps1` / 界面 `SwitcherUI.ps1` / 安装器 `install.ps1` / 保镖 `claude-bodyguard.ps1`）
- Linux 版：`linux/`（核心 `claude-switch.sh` / 保镖 `claude-bodyguard.sh` / 安装器 `install.sh`）
- Web 版：`docs/` 下的 index.html/app.js/style.css（纯静态环境检测页，GitHub Pages 从 docs/ 发布）

## 维护者本机纪律（Agent 必须遵守）

**除非维护者在当次对话中明确要求，否则不得更新/部署维护者本机的已安装版本。** 具体包括：

- 不得覆盖 `~/.local/bin/claude-switch.sh`、`~/.local/bin/local.claude-*.plist`
- 不得重新编译或替换桌面 App（`~/Desktop/Claude切换器.app`）
- 不得改动 `/Library/Application Support/claude-switcher/`、`/Library/LaunchDaemons/local.claude-*`、`~/Library/LaunchAgents/local.claude-*`
- 不得触发本机 `update`、不得执行安装器/卸载器

日常开发默认流程：**只修改仓库文件 → 验证（本地只跑不影响安装态的测试/静态检查）→ 提交推送**。需要真机验证时，先询问维护者是否允许在本机部署。
