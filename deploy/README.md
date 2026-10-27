# 管理 vpn-host1 上的后台

后台使用 systemd 服务 `claude-switcher`，开机自启，仅监听服务器的 `127.0.0.1:8787`。Cloudflare Tunnel 将管理页面、需要认证的 API 和本项目的订阅下载路径转发到后台。

## 打开管理页

直接打开 [云端管理页面](https://sub.610247.xyz/admin)。账号为 `admin`，密码使用部署时提供的值，不记录在本文件中。

本机已有 SSH 转发时，也可通过 `http://127.0.0.1:8787/admin` 管理同一个云端数据库。原本地调试服务已停止，其数据库作为迁移前副本保留。

如果电脑重启或转发断开，在终端运行以下命令，并保持终端打开。

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:8787:127.0.0.1:8787 vpn-host1
```

已有转发连接可通过以下命令检查。

```bash
ssh -S ~/.local/share/claude-switcher-server/vpn-host1-admin.sock \
  -O check vpn-host1
```

## 查看服务和数据

使用现有 SSH 别名登录后，运行 `systemctl status claude-switcher` 查看后台状态。代码入口为 `/opt/claude-switcher/current`，对应不可变的 `releases/<release_id>` 目录。云端数据库为 `/var/lib/claude-switcher/subscriptions.db`，管理员认证哈希文件为 `/etc/claude-switcher/admin-auth.json`。

服务使用独立的 `claude-switcher` 用户。私有目录权限为 `0700`，数据库与认证文件权限为 `0600`。不要将它们加入 Git，或直接从 Web 目录提供下载。

`install_vpn_host1.py` 仅用于首次安装，会拒绝覆盖已有后台或数据。它接收私有 stage，其中包含 `release/`、`state.db`、`admin-auth.json` 和 `manifest.json`。Manifest 记录完整文件摘要、数据库逻辑摘要和原 Tunnel 摘要。运行测试和安装器时使用 `python3 -B`，避免 bytecode 文件改变完整文件集合。

## 回滚首次安装

每个部署版本的 `/var/lib/claude-switcher-deploy/backups/<release_id>/` 包含原 Tunnel 配置、安装 receipt 和 `rollback.py`。需要回滚时，在服务器上运行以下命令，将占位符替换为对应版本。

```bash
/usr/bin/python3 -B \
  /var/lib/claude-switcher-deploy/backups/<release_id>/rollback.py
```

回滚会先核对文件漂移，停止新后台并恢复原 Tunnel 路由。数据库、认证文件及代码版本保留。Tunnel 重启失败会保留未完成状态，修复原因后可重复执行回滚。它不会修改 Xray、3X-UI、旧订阅服务或其他 Tunnel 规则。

## 核对实际代理接入

订阅后台保存用户、住宅代理和绑定关系，并生成客户端订阅；独立 root 同步器读取本项目数据库，通过本机 3X-UI API 更新受管身份、出口和路由，Xray 执行实际流量转发。HTTP 后台不直接调用面板管理 API。全局订阅模板只影响客户端配置，不修改服务端住宅绑定。完整分工见 [服务端说明](../server/README.md#与-3x-ui-的分工与同步)。

服务器列表显示 3X-UI 同步结果。`已应用到 3X-UI` 要求后台当前配置摘要、同步收据和运行时核对一致，且最近一次检查不超过 60 秒。它证明路由配置已应用；家庭 IP 的实际可用性仍需流量测试。

`claude-switcher-sync.timer` 每次同步结束后等待 15 秒再检查。配置变化时，同步器先写入受管用户的拦截规则，再更新客户身份、家庭出口和路由，最后重启并核对 Xray。重绑、解绑和停用会断开旧连接，也会短暂影响同一 Xray 上的其他连接。未变更的轮询不写面板、不重启。

同步器保留非受管客户端、既有入站、私钥及非受管路由。迁移到 WS 时，仅禁用本项目原受管 Reality 身份，保留历史身份记录和拦截规则，不影响其他 Reality 用户。停用身份保留为禁用记录，并保留兜底拦截规则。API 失败时不会将状态标为成功。不要在同步过程中手工编辑面板的受管对象；面板 API 不提供跨请求事务。

当前同步器只处理本机配置明确指定的一台服务器。后台可以登记多台服务器；其他服务器显示未配置，必须为各自的 3X-UI 配置对接后才会自动生效。

在云端使用以下命令检查服务状态。

```bash
systemctl status claude-switcher claude-switcher-sync.timer
cat /var/lib/claude-switcher-sync-status/status.json
```

根权限同步代码在 `/opt/claude-switcher-sync`。部署时复制完整的 `server/*.py` 包，因为包初始化会导入 HTTP 模块及其依赖。目标配置在 `/etc/claude-switcher-sync/config.json`，私有恢复记录在 `/var/lib/claude-switcher-sync/state.json`。这些文件由 root 管理，HTTP 用户不能修改。同步器从本机面板读取监听参数，通过固定 CLI 签发专用 API token，仅保留在进程内存中。

实际出口检查使用与服务器配置一致的一次性 WS/TLS 客户端（历史 Reality 仍可诊断），不打印身份或出口 IP。将示例用户 ID 替换为需要检查的记录。

```bash
cd /opt/claude-switcher/current
python3 -B deploy/probe_xray_binding.py --server-id vpn-host1 --user-id USER_ID
```

启用用户成功时返回 `passed=true` 和 `egress_matches_binding=true`。对已经停用的用户增加 `--expect-disabled --control-user-id ENABLED_USER_ID`。只有停用身份已从运行配置中移除、连接失败且同期有效用户对照成功时，才返回 `disabled_connection_rejected=true`。

## WS 唯一入口迁移与撤销

公开 gateway 使用 `kind: "xray-vless-ws"`，字段见服务端说明。root 私有 `/etc/claude-switcher-sync/config.json` 的主锁定应指向已经核验的 WS 入站，增加 `transport: "ws"` 与 `tls_termination: "external"`（或原生 TLS 的 `"xray"`）。其余必填项与原配置相同，`gateway_digest` 使用 `digest(gateway_server_identity(gateway))`。

从旧 Reality + WS 配置迁移时，暂停同步 timer/service，私下备份目标服务器 gateway、同步代码、配置、state 和旧订阅。把旧 `ws_backup` 的四个锁定字段提升到主配置；原主锁定的 `inbound_id`、`inbound_tag`、`inbound_digest` 放入 `retired_reality`，删除 `ws_backup`。保留原 state 中的身份、规则和 `identity_targets`，保持用户 UUID、令牌和住宅绑定不变。确认固定入站摘要、gateway 与同步器版本一致后再运行同步。

WS 源站必须是真实且已启用的 VLESS WS 入站。摘要采用 `digest(inbound_identity(inbound))`，包含传输、安全、监听等元数据但不含客户端列表；禁止根据名称信任、遇漂移直接覆盖或打印含密钥的原始入站。外部 TLS 要求明文源站只监听 loopback；原生 TLS 要求公开端口匹配。

同步器只启用本项目的 WS 身份。原受管 Reality 身份禁用并保留拦截规则，非受管客户端和入站保持原配置。历史 Reality pin 继续保留以完成撤销；不要删除 state、历史身份或目标 pin。停用、解绑、换绑和故障恢复都按已知身份执行，不能把空列表当作撤销成功。共享 Xray 重启可能短暂影响其他连接。

发布前在隔离环境验证身份保留、住宅换绑、停用、源站漂移和故障恢复；发布后重新下载新旧真实地址，用独立内核验证实际 TLS/HTTP/HTTPS、出口与业务 DNS。`已应用到 3X-UI` 收据表示面板和运行配置已核对，不能代替公网流量或手机真机验收。小火箭格式还需 iOS 实际导入，Android 则需刷新订阅并重新选择住宅节点。

回滚必须先停止同步，再按私有备份恢复旧 gateway、同步代码/配置/state 与旧订阅；核验原目标后重新同步并恢复 timer。仅回退后台 symlink 无法撤销此迁移，禁止恢复整个活跃数据库覆盖其他业务变更。

## 升级和回滚后台

`upgrade_vpn_host1.py` 用于已有安装。它检查 root 私有 stage 中的代码摘要、原版本路径和 Tunnel 摘要，保留数据库与管理员认证文件，然后切换代码版本并核验服务。stage 不包含数据库或密码文件。不要用首次安装器覆盖已有安装。

升级回滚命令如下。

```bash
python3 -B /var/lib/claude-switcher-deploy/backups/RELEASE_ID/rollback.py \
  --rollback /var/lib/claude-switcher-deploy/backups/RELEASE_ID
```

它先检查漂移，停止同步器，再恢复后台版本和 Tunnel。它保留数据库、登录配置和已应用的 3X-UI 对象。要撤销面板对接，先停用自动同步，再通过面板 API 按私有备份和所有权记录处理；不要覆盖正在运行的面板数据库或生成配置。
