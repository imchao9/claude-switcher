# 订阅与独立 IP 服务端

`server/` 管理多台共享云服务器和各自绑定的家庭代理 IP，不处理支付。每个用户独占一个家庭 IP，并获得一个稳定订阅链接。用户通过云服务器的 VLESS + TLS 入口连接，云服务器按用户身份转发到其专属家庭代理。家庭代理的认证信息留在服务端。

当前实现使用 Python 标准库和 SQLite，适合本地开发、联调和受保护的单机部署。生产环境需要在前面配置 HTTPS 反向代理、备份、限流和进程托管。

## 启动

先在仓库根目录运行以下配置脚本。它通过隐藏输入读取密码，只把带盐哈希写入私有文件。首次配置和后续修改密码都可以使用；修改后重启服务，旧登录会话会失效。

```bash
python3 - <<'PYCONFIG'
import getpass
import json
import os
from pathlib import Path
from server.admin_auth import hash_admin_password

path = Path.home() / ".config/claude-switcher-server/admin-auth.json"
path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
username = "admin"
password_hash = hash_admin_password(getpass.getpass("管理员密码: "))
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
os.fchmod(fd, 0o600)
with os.fdopen(fd, "w") as stream:
    json.dump({"username": username, "password_hash": password_hash}, stream)
print("管理员配置已保存。重启服务后生效。")
PYCONFIG
python3 -m server --db /var/lib/claude-switcher/subscriptions.db \
  --host 127.0.0.1 --port 8787 \
  --base-url https://sub.example.com \
  --admin-auth-file ~/.config/claude-switcher-server/admin-auth.json
```

配置文件也可通过环境变量 `CLAUDE_SWITCHER_ADMIN_AUTH_FILE` 指定。它必须是普通文件、仅当前用户可读写，不接受符号链接。不要将配置提交到仓库。

本机打开 `http://127.0.0.1:8787/admin`，使用配置的账号密码登录。`--base-url` 只决定交付地址，不改变监听地址或自动开放管理页。登录成功后自动加载数据，页面内存中的会话最长有效 8 小时；退出会撤销该会话并清除页面中的代理及订阅信息。页面刷新或服务重启后需要重新登录。页面不使用 localStorage 或 cookie 保存登录凭证。

登录失败按服务端观察到的来源限速，每个来源 60 秒内最多 5 次失败。只信任服务端直接看到的来源，不读取客户端可伪造的转发头。

旧的 `--admin-token` / `CLAUDE_SWITCHER_ADMIN_TOKEN` 仍支持管理 API 调用。仅配置 token 时是 API 模式，管理页需要另行配置上述账号密码后才能登录。密码登录与 API token 可以同时启用；API token 不会因网页退出而失效。

本机联调资料使用固定私有数据库 `~/.local/share/claude-switcher-server/subscriptions.db`，不再存放在临时目录。再次启动本机管理页可运行：

```bash
python3 -m server --db ~/.local/share/claude-switcher-server/subscriptions.db \
  --host 127.0.0.1 --port 8787 --base-url https://sub.610247.xyz \
  --admin-auth-file ~/.config/claude-switcher-server/admin-auth.json
```

新生成的地址形如 `https://sub.610247.xyz/sub/<个人令牌>/vpn-host1.yaml`。每个人的令牌不同，文件名来自当前绑定服务器的 ID。此启动命令预设后续的公网域名，公网下载仍需完成[服务器部署与 Tunnel 路由](PUBLIC_SUBSCRIPTIONS.md)。本机纯测试可将 `--base-url` 换回 `http://127.0.0.1:8787`。也可通过 `CLAUDE_SWITCHER_BASE_URL` 设置同一选项，显式命令行参数优先。地址只允许纯 HTTPS 来源，或用于本机测试的环回 HTTP 来源，不接受路径、查询参数、账号密码或片段。

## 接入已安装的 Reality 服务器

服务器管理页只登记入口和资源，不重新安装、不更新或重启远端服务。现有 3X-UI Reality 服务器选择“VLESS + Reality”，填写公网入口地址、端口、SNI、公钥、short ID 和实际入站 tag。此模式不填写普通 TLS 证书路径，也不保存 Reality 私钥。已有 VLESS + TLS 配置仍可继续使用。

可以在运行管理服务的电脑上只读提取已配置 SSH 别名的公共接入参数：

```bash
python3 -m server.inspect_host vpn-host1 --output /private/path/vpn-host1-profile.json
```

工具使用现有 SSH 认证，仅读取 3X-UI 入站元数据。输出文件权限为 `0600`，不覆盖已有文件，不输出用户 UUID、私钥、住宅代理凭据或私密订阅。管理页可导入该 JSON 到表单，核对后保存。HTTP 管理接口不执行 SSH。

“参数已登记”表示本地字段齐备，远端接入尚未因此验证。本地新生成的每用户 UUID、email 和住宅路由需要在对应 3X-UI 入站中配置后才能使用。Reality 行的“导出合并资料”提供明确标记 `not_applied` 的人工合并包，包含目标 tag、用户及住宅出站规则；它不是完整 Xray 配置，不得覆盖现有生成文件。合并前比较同名对象，有冲突时停止。每用户规则应位于旧的宽泛入站规则之前，并保留其他用户、Reality 私钥及 WS 备用入站。停用、换绑后的旧 UUID 和规则也需要人工清理；空清单本身不会撤销旧身份。

## 保存和复用安装资料

在服务器操作列点击“安装资料”，选择 ZIP、Markdown、HTML 或 TSV 上传。资料按服务器归档，保存于管理服务的私有 SQLite 数据库中；只有管理员可以列出和下载。文件不会自动上传到云服务器，也不会执行安装包、解压 ZIP 或在线运行 HTML。

每个文件最多 5 MiB，每台服务器最多 20 个文件、合计 25 MiB。同一服务器重复上传相同内容会复用原件，不占新额度。列表显示文件名、大小和 SHA-256；下载保留原始字节。原始文档不会被修改。备份和迁移管理服务时，需要一起保管私有数据库，不将其提交到 Git。

后续新服务器可直接下载已归档的资料包，按其安装指南准备独立凭据并逐步验证，再登记新服务器入口。软件清单是版本快照，不是批量安装脚本。安装记录应区分现有服务器检查、模板语法检查和全新服务器完整重装。

## 管理流程

管理页使用左侧菜单切换概览、服务器管理、IP 资源、批量导入、用户绑定与订阅交付。手机端从顶部菜单按钮打开导航。手动添加 IP 或创建用户使用弹窗；新订阅统一在交付页查看和复制，用户更换绑定后会同步显示当前 IP，停用后移除待交付项。

1. 在“服务器管理”登记云服务器 ID、名称、入口地址和地区，选择与现有服务一致的 Reality 或 TLS 参数。
2. 添加家庭代理 IP 或批量导入前选择其所属云服务器。新家庭代理支持 SOCKS5 或 HTTP。
3. 创建用户并绑定一个可用 IP。一个 IP 不能被两个用户绑定。
4. 创建响应只返回一次订阅地址。用户应立即保存。
5. Reality 使用人工合并资料；独立 TLS 使用完整配置。通过对应服务配置并验证用户路径后，再交付订阅。用户停用或轮换 token 后，旧订阅下载失效；已下载节点的撤销需要重新应用云端配置。

管理 API 使用 `Authorization: Bearer <登录会话 token 或原 API token>`。密码登录端点为 `POST /api/auth/login`，JSON 字段为 `username` 和 `password`；退出端点为 `POST /api/auth/logout`，登录状态查询为 `GET /api/auth/session`。会话 token 仅在登录成功时返回，密码和登录请求不会写入访问日志。公开订阅端点是 `/sub/<token>/<服务器ID>.yaml`，旧 `/s/<token>` 仍直接返回相同内容。两种路径默认输出 Clash/Mihomo YAML，也支持 `?format=uri` 和 `?format=base64`。响应头 `X-Claude-Switcher-Egress-IP` 是客户端本地校验绑定的依据。

在订阅交付页点击“恢复已有地址”，粘贴本项目以前生成的地址，可转换为当前公网域名下的新格式。`POST /api/subscriptions/convert` 要求管理员认证，仅接收 `{"url": "<旧地址>"}`，返回用户公开字段和 `subscription_url`。恢复仅验证本地记录，不访问输入网址；在事务中保存提供的当前令牌，不轮换令牌，也不修改 UUID、绑定或原时间戳。可接受空查询或单个显式格式参数，重复恢复不改写数据库。输入来源仅允许已配置的公网来源或本机管理端口的环回 HTTP 地址。其他服务生成的旧静态订阅不能通过此接口转换。历史哈希记录保持原样，无法还原丢失的令牌，需提供之前保存的地址，或明确轮换令牌使旧链接失效。

## 多服务器与固定订阅

服务器与 IP 是一对多关系，用户通过绑定的 IP 确定唯一服务器。每个订阅只返回该用户当前绑定的入口；显式配置 WS 备用时返回主、备用两个节点，两者仍共用这一住宅出口。新增其他服务器不会把节点追加到旧订阅。链接保持稳定，除非管理员主动轮换 token。管理员主动更换用户绑定时，该用户的原链接会指向新选择的 IP 和服务器。文件名用于识别交付时的服务器，不决定访问权限；换绑后，原文件名的链接仍按该令牌返回当前绑定。

服务器的 `host` 是客户端连接的云入口地址，不填写 SSH 命令、面板凭据或完整 URL。多位用户可以共用该入口和端口，每人使用独立 UUID。订阅中的节点地址是云入口，响应头中的预期出口仍是家庭 IP。

服务器可先只登记信息，显示“待配置”。为其配置完整网关后，才能创建绑定和生成可交付订阅。TLS 模式的 `gateway` 包含 `kind: "xray-vless-tls"`、`port`、`server_name`、`certificate_file`、`key_file`。证书字段只接受云服务器上的绝对路径，不读取维护者本机文件，也不接收私钥内容。客户端强制 TLS 验证。

新 IP、批量导入和新绑定必须选择已启用服务器。IP 地址在所有服务器之间保持唯一；重复导入会跳过，不会把其他服务器的 IP 移过来。已有用户绑定时，不能通过资源更新更换该 IP 地址或已确定的服务器；需要迁移时先解除绑定，再操作资源，最后由管理员明确重新绑定。

服务器停用后，停止新资源分配并拒绝其订阅下载，但保留原用户、IP、绑定和 token。重新启用后，原链接恢复。此操作不会关闭远端服务器，也不能撤销用户已经下载的代理配置。

升级旧数据库会增加服务器归属、网关配置及用户入口身份，保留原始代理凭据、用户 token 和绑定。历史未归属 IP 的旧直连订阅继续可用，这是迁移兼容例外。迁入云网关前先解除旧绑定；仅 SOCKS5 和 HTTP 资源可以设置云归属。旧直连配置中已经交付的家庭代理凭据需要另行轮换，迁移不能收回这些凭据。重复启动不会重建用户 UUID；旧库若含重复 IP，迁移将回滚并停止，需要先核对重复资源。

切换器对 `https://sub.610247.xyz/sub/<43位令牌>/vpn-host1.yaml` 提供受限的旧静态订阅兼容模式。该模式只接受固定域名和完整路径，从旧配置的“住宅出口”节点读取预期 IP，并要求 Claude/Anthropic 规则先于所有 `DIRECT` 规则、最终 `MATCH` 仍指向代理组。它不会接受任意缺少绑定标记的订阅，也不会把旧配置转换成新的服务端用户绑定。兼容模式只用于迁移，交付后应改用新后台生成的 `/sub/cs_<token>/vpn-host1.yaml` 地址。

管理接口为 `GET /api/servers`、`POST /api/servers` 和 `PUT /api/ips/{id}/server`。服务器写入字段为 `id`、`name`、`host`、可选 `region`、可选 `status` 及可选 `gateway`。已有服务器省略 `status` 或 `gateway` 时保留原值。`gateway: null` 清除网关，有绑定时不允许清除。IP 归属接口只接收 `server_id`，不改动其他节点配置。IP 与用户列表包含派生的服务器信息，不回显代理凭据或入口 UUID。

## 应用独立 TLS 云服务器配置

在服务器列表点击“导出 Xray 配置”。`GET /api/servers/{id}/xray-config` 需要管理员认证，返回禁止缓存的 JSON 附件。文件含家庭代理凭据和用户 UUID，只能交给对应服务器管理员，并以私有文件权限保管。

每台服务器的完整配置包含一个共享 VLESS + TLS 入站、该服务器当前有效绑定的用户，以及每人的家庭代理出站。路由同时匹配入站和用户身份，仅允许 TCP。默认出站和未匹配流量均进入 blackhole，不生成直连出口。托管订阅的 Clash 规则将默认流量交给受管节点组，并关闭 UDP 与 IPv6；可选 WS 备用也必须使用相同的家庭出口。

将导出文件放在对应云服务器的私有配置目录，准备与 `server_name` 匹配的有效 TLS 证书，再用 `xray run -test -config /path/to/config.json` 检查。当前导出按 Xray `v26.10.21` 验证。确认监听端口无冲突后，由管理员应用配置并重启 Xray。已有 3X-UI 管理的实例需要先核对其配置所有权，不能直接覆盖面板管理的运行配置。

导出是数据库当前状态的快照。新增、停用、解绑和重新分配都需要重新导出并应用，才会改变云端运行状态。TLS 模式的服务器停用时仍可导出无有效用户的拒绝配置。Reality 合并包需要人工移除旧身份，不能用空包替代撤销操作。订阅 token 只控制配置下载，轮换 token 不轮换入口 UUID，也不会撤销已下载节点。当前项目不自动执行 SSH、上传配置或调用 3X-UI API。

## 批量导入代理

管理页的“批量导入代理 IP”接受每行一条 `IP:端口:用户名:密码`。选择所属服务器及 SOCKS5 或 HTTP，点击“预览校验”，核对行号、地址、端口及新增/跳过结果，再点“入库并生成订阅”。每个新增 IP 会自动创建一个用户、绑定这个 IP，并生成独立订阅地址。格式本身不能判断代理协议，所选协议应用于整个批次。

每批最多 200 条、请求体最多 64 KiB，仅接受公网 IPv4。空行会忽略，密码可以包含冒号，用户名不能包含冒号。预览不写数据库；任何一行有误时整批不写入。已有 IP 及批内重复 IP 会跳过，不覆盖原来的端口、协议或认证信息。修改内容、协议或所属服务器后必须重新预览。服务端会在确认写入时重新检查该服务器是否启用。

管理 API 为 `POST /api/ips/import`，JSON 字段为必填的 `server_id`、`text`、`protocol`（`socks5` 或 `http`）、`dry_run`（布尔值，默认 `true`）。确认时传 `dry_run: false`，服务端重新校验并在同一事务中写入。管理页会传 `create_subscriptions: true`。此时确认响应的 `subscriptions` 数组包含每个新用户的 `user_id`、`ip_id`、`address`、`server_id`、`server_name`、`subscription_url`。省略此字段或传 `false` 时仅登记 IP，传其他类型会被拒绝。IP、用户及绑定在同一事务中写入；任何一步失败都会整批回滚。预览不生成订阅，重复 IP 不新建用户、不返回或轮换旧订阅。

新建、导入和轮换会把可恢复令牌与哈希一起存入私有 SQLite，目录权限 0700、文件权限 0600；数据库备份也必须按凭据保护。管理员通过 `GET /api/subscriptions` 查阅全部用户的交付状态，可用且令牌完整的记录包含显式 Clash 与 Shadowrocket 链接。普通用户/IP 列表与网关导出不包含订阅令牌。链接默认隐藏，可分别复制或复制全部，刷新、重新登录和服务重启后仍可查阅。历史记录显示待恢复，使用“恢复已有地址”保存有效旧链接；没有旧链接时才主动轮换 token，旧地址随即失效。响应和管理列表不回显家庭代理用户名与密码。托管订阅只包含用户自己的云入口身份。请勿提交数据库或真实代理列表到仓库。

生成订阅和导出配置不探测真实代理可用性，也不证明公网出口固定或防泄露。本地 `127.0.0.1` 订阅地址仅供本机测试，交付其他设备前需要部署服务并设置其可访问的 HTTPS `base-url`。

## 订阅兼容与可选 WS 备用

Reality 的 `gateway` 可增加 `support_x25519mlkem768: true` 或 `false`。省略表示尚未声明，保持旧客户端参数，不自动猜测服务器能力。管理页提供三态选项，公开参数检查工具可显式使用 `--reality-mlkem enabled|disabled`。本次排障已在目标组合上观察到开启此选项能修复 Reality 握手；其他服务器仍需分别验证。该选项只影响客户端输出，不改变同步器对服务器路由的摘要锁定。

备用入口不是把旧 WS 节点直接复制进订阅。仅在登记并同步受管 WS 身份后使用 `gateway.ws_backup`，例如以下公开参数：

```json
{
  "address": "edge.example.com",
  "port": 443,
  "server_name": "edge.example.com",
  "host": "origin.example.com",
  "path": "/managed-ws",
  "inbound_tag": "managed-ws",
  "tls_termination": "external"
}
```

`address` 是客户端连接地址，可与 Reality 入口不同；`host` 是 WS Host，`server_name` 用于 TLS 验证。`tls_termination: "xray"` 要求已存在的原生 WS+TLS 入站。`"external"` 用于外部 TLS 终止，但对应明文 WS 源站必须明确监听 loopback，不能纳管公开监听的明文源站。同步器固定入站身份、传输、路径及已配置的 Host；外部 TLS 证书、边缘转发和公网可达性仍需分别实测。参数登记不会安装证书、创建入站或配置反向代理。

管理页支持这些字段的导入和编辑。也可将上例保存为私有 JSON，使用 `python3 -m server.inspect_host ALIAS --output /private/new-profile.json --ws-backup-profile /private/ws-public.json` 生成待审核公开参数文件。这个入口不修改远端配置，也不证明 WS 已可用。

Reality 身份保留原 UUID 和 Vision flow；WS 使用稳定派生的独立 UUID 和空 flow，以符合 3X-UI 的全局客户端模型。两者分别挂到已锁定的入站，共用该用户的同一住宅出站，UDP 均拒绝。订阅的 Clash、URI、base64 三种格式均包含这两个节点，选择组不提供裸云入口、DIRECT 或其他用户出口。WS 是手动备用选择，不自动回退。新增 WS 后必须更新同步器的明确目标锁定，见 [部署说明](../deploy/README.md#可选-ws-目标与撤销)。

托管 Clash 配置自带 DNS：loopback `1053`，fake-IP `198.18.0.1/16`，AAAA 空回应，普通解析使用 IP 地址 DoH 并指定所选受管组。禁用 HTTP/3、系统 hosts、业务直连 fallback 和业务 `DIRECT` 规则。代理入口域名的启动解析使用单独的 IP 地址 DoH `#DIRECT`，用于避免先解析代理、又要先连代理的循环。这一例外只服务于解析受管入口地址，不能用于业务访问，也意味着不能宣称所有 DNS 都经住宅。Claude、Anthropic 相关域名先匹配受管节点规则，代理节点失败时请求失败，不降级到国内直连。旧订阅的国内直连方案只有在显式选择 `domestic-direct` 策略时才可生成。配置也不会替用户改变系统 DNS、TUN 或 Antify 接管；客户端全局覆盖项可能改变最终行为。参数含义参考 [Mihomo DNS 文档](https://wiki.metacubex.one/config/dns/)。

严格模式是公开订阅的默认值。需要日常国内分流时，在订阅地址上显式增加 `?routing=domestic-direct`；如果地址已有格式参数，使用 `?format=clash&routing=domestic-direct`。该模式只把明确的国内规则放在 AI 规则之后，Claude 和 Anthropic 域名仍先进入受管组。未知或重复的查询参数会被拒绝。示例文件见 [`server/examples/managed-subscription.yaml`](examples/managed-subscription.yaml)。

`subscription ports --json` 从运行内核的本地控制器读取端口，再检查 loopback 监听。缺少授权、多个内核或无法读回时返回 `runtime_verified: false`；配置文件只能标记为候选。订阅默认 `mixed-port: 7890` 与本机实际端口可能不同，诊断只提醒，不改全局配置或 Antify 上游。

开发验收工具需要显式网络授权标志和已有 PyYAML；缺失时返回 `missing_dependency_pyyaml`，不自动安装依赖。私有 YAML 与绑定清单必须权限 `0600`，清单至少含 `expected_ip`。使用实际下载文件逐节点运行：

```bash
python3 deploy/probe_mihomo_subscription.py \
  --subscription /private/downloaded.yaml \
  --manifest /private/binding.json \
  --core /path/to/mihomo --allow-network --node-index 0
# 配置了备用时，另跑 --node-index 1。
```

工具使用独立临时内核、随机 loopback 端口，关闭该临时内核的 TUN、外部控制器和 LAN 监听；不改已安装配置。配置经 stdin，不保存真实节点配置或打印出口地址。检查中立目标 Cloudflare、Gstatic HTTP/HTTPS、出口是否匹配绑定、本地 fake-IP/AAAA 行为，并用 `example.com SOA` 查询触发实际业务解析器响应。仅测试指定节点，其他节点计为 `untested_node_count`；不请求 Claude 域名。SOA 响应不证明解析出口或断线不泄漏。宿主仍开启的 TUN 可能影响外层连接，收据中的 `dns_leak_free: "unverified"` 不能改称防泄漏已通过。真实发布后的节点、DNS 解析出口与断线保护验收应另行记录。

## 本地客户端

当前项目的 macOS 核心脚本支持以下命令。订阅 URL 通过 stdin 传入，避免 Swift 进程参数和调试日志记录 token。

```bash
printf '%s\n' 'https://sub.example.com/sub/your-token/server-a.yaml' | \
  ~/.local/bin/claude-switch.sh subscription set
~/.local/bin/claude-switch.sh subscription fetch
~/.local/bin/claude-switch.sh subscription status
~/.local/bin/claude-switch.sh subscription ports --json
~/.local/bin/claude-switch.sh subscription check
~/.local/bin/claude-switch.sh subscription install
~/.local/bin/claude-switch.sh subscription clear
```

客户端把订阅状态和配置写入 `~/.claude-switcher/`，文件权限为 `0600`。同步前要求代理内核和 TUN 在线。启动检查发现当前出口 IP 与订阅绑定 IP 不一致时，检查失败并阻止后续启动流程。

在 Clash Verge 模式下，`subscription install` 会把已校验的 YAML 写入其 `profiles/claude-switcher-subscription.yaml`，然后由用户在 Clash Verge 中选中该 Profile。其他客户端保留本地配置文件，按客户端自己的导入流程使用。

## 接入 3X-UI 或其他节点

新家庭 IP 的 `config` 字段保存 SOCKS5 或 HTTP 的上游认证参数，云端导出只读取所需字段。历史未归属节点保留原有协议渲染，以便兼容旧数据。当前不调用 3X-UI API。

## 安全边界

订阅 token 等同密码。服务端只存 token 的 SHA-256，管理列表不回显 token，内置访问日志不记录请求路径。默认只接受 HTTPS 订阅地址，本机环回测试除外。数据库还保存家庭代理认证和入口 UUID，应按私密配置保护。不要把数据库、导出的 Xray 配置、管理员 token 或用户订阅地址提交到仓库。
