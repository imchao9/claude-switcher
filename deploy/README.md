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

服务器列表显示 3X-UI 同步结果。`已生效` 要求后台当前配置摘要、同步收据和运行时核对一致，且最近一次检查不超过 60 秒。它证明路由配置已应用；家庭 IP 的实际可用性仍需流量测试。

`claude-switcher-sync.timer` 每次同步结束后等待 15 秒再检查。配置变化时，同步器先写入受管用户的拦截规则，再更新客户身份、家庭出口和路由，最后重启并核对 Xray。重绑、解绑和停用会断开旧连接，也会短暂影响同一 Xray 上的其他连接。未变更的轮询不写面板、不重启。

同步器保留原有 Reality 用户、WS 入站、私钥及非受管路由。停用身份保留为禁用记录，并保留兜底拦截规则。API 失败时不会将状态标为成功。不要在同步过程中手工编辑面板的受管对象；面板 API 不提供跨请求事务。

当前同步器只处理本机配置明确指定的一台服务器。后台可以登记多台服务器；其他服务器显示未配置，必须为各自的 3X-UI 配置对接后才会自动生效。

在云端使用以下命令检查服务状态。

```bash
systemctl status claude-switcher claude-switcher-sync.timer
cat /var/lib/claude-switcher-sync-status/status.json
```

根权限同步代码在 `/opt/claude-switcher-sync`。部署时复制完整的 `server/*.py` 包，因为包初始化会导入 HTTP 模块及其依赖。目标配置在 `/etc/claude-switcher-sync/config.json`，私有恢复记录在 `/var/lib/claude-switcher-sync/state.json`。这些文件由 root 管理，HTTP 用户不能修改。同步器从本机面板读取监听参数，通过固定 CLI 签发专用 API token，仅保留在进程内存中。

实际出口检查使用一次性 Reality 客户端，不打印身份或出口 IP。将示例用户 ID 替换为需要检查的记录。

```bash
cd /opt/claude-switcher/current
python3 -B deploy/probe_xray_binding.py --server-id vpn-host1 --user-id USER_ID
```

启用用户成功时返回 `passed=true` 和 `egress_matches_binding=true`。对已经停用的用户增加 `--expect-disabled --control-user-id ENABLED_USER_ID`。只有停用身份已从运行配置中移除、连接失败且同期有效用户对照成功时，才返回 `disabled_connection_rejected=true`。

## 可选 WS 目标与撤销

服务器公开 `gateway.ws_backup` 不会创建入站、安装证书、修改 Tunnel 或自动纳管原有 WS。启用前需要人工核对目标入站，并在 root 私有的 `/etc/claude-switcher-sync/config.json` 保留原 Reality 锁定，增加以下可选字段：

```json
{
  "ws_backup": {
    "inbound_id": 2,
    "inbound_tag": "managed-ws",
    "inbound_digest": "<64位已审核入站摘要>",
    "tls_termination": "external"
  }
}
```

这里只展示新增字段，不能代替原配置。`inbound_id`、tag 和摘要必须对应这台 3X-UI 的真实已启用 VLESS WS 入站，不能照抄示例或只根据名字信任。摘要采用 `server.xui_sync.digest(inbound_identity(inbound))`，其中 `inbound` 是 `panel_snapshot().inbounds` 中由 ID 精确选择的对象；不含客户端列表，但包含传输、安全、监听及其他固定元数据。不要打印完整入站对象，它含私钥等私密配置。

`gateway_digest` 使用 `digest(gateway_server_identity(gateway))`。该函数只剥离客户端 ML-KEM 选项，所以旧 Reality 锁定在 ML-KEM 未声明、启用、禁用间保持兼容。新增或修改 WS 会改变该摘要，必须显式审核并更新锁定，不能遇到 `target_drift` 就盲目覆盖。

`tls_termination: "xray"` 要求原生 WS+TLS，入站端口与公开 TLS 端口一致。`"external"` 要求 WS 明文源站只监听 IP loopback，公网连接地址与端口单独登记。同步会核对路径及源站已设置的 Host，不能因此推断外部 TLS 证书或反向代理已验收。外部代理没有在源站配置 Host 校验时，也不代表已经验证公网 Host 转发。

每用户的 WS 身份独立于 Reality 身份，两者 TCP 都指向同一个家庭出站，UDP 都拒绝。同步器先保护全部目标，再更新身份和绑定；停用、解绑、换绑及故障恢复都覆盖两个入口。未纳管客户端和入站保持原有配置。共享 Xray 重启仍可能短暂影响其他连接。

移除公开 `ws_backup` 后，仍须保留 root 配置里的 WS pin；同步器利用私有状态中的 `identity_targets` 找到历史身份，将它禁用并保留拦截规则。不要删除状态、手工清空历史身份或先删 pin。缺少历史目标锁定、身份冲突或入站漂移会拒绝同步，而不会把空快照当作撤销成功。当前版本不自动垃圾回收退休目标 pin。

`已生效` 收据只表示已锁定的面板、运行配置和路由核对一致。交付 WS 前，还须用实际下载订阅分别验证主、备用节点：TLS/HTTPS、中立 HTTP 目标、固定出口、真实业务 DNS 响应、停用两条身份后的拒绝行为。独立测试工具见 [订阅说明](../server/README.md#订阅兼容与可选-ws-备用)。保持既有 TUN 开启的测试不能证明 Antify 可独立替代它。

## 升级和回滚后台

`upgrade_vpn_host1.py` 用于已有安装。它检查 root 私有 stage 中的代码摘要、原版本路径和 Tunnel 摘要，保留数据库与管理员认证文件，然后切换代码版本并核验服务。stage 不包含数据库或密码文件。不要用首次安装器覆盖已有安装。

升级回滚命令如下。

```bash
python3 -B /var/lib/claude-switcher-deploy/backups/RELEASE_ID/rollback.py \
  --rollback /var/lib/claude-switcher-deploy/backups/RELEASE_ID
```

它先检查漂移，停止同步器，再恢复后台版本和 Tunnel。它保留数据库、登录配置和已应用的 3X-UI 对象。要撤销面板对接，先停用自动同步，再通过面板 API 按私有备份和所有权记录处理；不要覆盖正在运行的面板数据库或生成配置。
