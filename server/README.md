# 订阅与独立 IP 服务端

`server/` 管理多台共享云服务器和各自绑定的家庭代理 IP，不处理支付。每个用户独占一个家庭 IP，并获得一个稳定订阅链接。用户通过云服务器的 VLESS + WS + TLS 入口连接，云服务器按用户身份转发到其专属家庭代理。家庭代理的认证信息留在服务端。

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

本机打开 `http://127.0.0.1:8787/admin`，使用配置的账号密码登录。`--base-url` 只决定交付地址，不改变监听地址或自动开放管理页。登录成功后自动加载数据，浏览器通过 HttpOnly、SameSite=Strict Cookie 保存最长 8 小时的会话，公网 HTTPS 下设置 Secure，刷新页面时自动验证并恢复登录。退出会撤销服务端会话、清除 Cookie 和页面中的代理及订阅信息；会话过期或服务重启后需重新登录。页面不使用 localStorage 或 sessionStorage 保存登录凭证；使用 Cookie 的管理写入还要求同源 Origin。

登录失败按服务端观察到的来源限速，每个来源 60 秒内最多 5 次失败。只信任服务端直接看到的来源，不读取客户端可伪造的转发头。

旧的 `--admin-token` / `CLAUDE_SWITCHER_ADMIN_TOKEN` 仍支持管理 API 调用。仅配置 token 时是 API 模式，管理页需要另行配置上述账号密码后才能登录。密码登录与 API token 可以同时启用；API token 不会因网页退出而失效。

本机联调资料使用固定私有数据库 `~/.local/share/claude-switcher-server/subscriptions.db`，不再存放在临时目录。再次启动本机管理页可运行：

```bash
python3 -m server --db ~/.local/share/claude-switcher-server/subscriptions.db \
  --host 127.0.0.1 --port 8787 --base-url https://sub.610247.xyz \
  --admin-auth-file ~/.config/claude-switcher-server/admin-auth.json
```

新生成的地址形如 `https://sub.610247.xyz/sub/<个人令牌>/vpn-host1.yaml`。每个人的令牌不同，文件名来自当前绑定服务器的 ID。此启动命令预设后续的公网域名，公网下载仍需完成[服务器部署与 Tunnel 路由](PUBLIC_SUBSCRIPTIONS.md)。本机纯测试可将 `--base-url` 换回 `http://127.0.0.1:8787`。也可通过 `CLAUDE_SWITCHER_BASE_URL` 设置同一选项，显式命令行参数优先。地址只允许纯 HTTPS 来源，或用于本机测试的环回 HTTP 来源，不接受路径、查询参数、账号密码或片段。

## 接入已安装的 WS 服务器

服务器管理页默认使用“VLESS + WS + TLS”，登记客户端连接域名、TLS 端口、SNI、WS Host、path、真实入站 tag 与 TLS 终止方式。公开配置采用 `kind: "xray-vless-ws"`。每个用户只生成一个 WS 节点，Clash 与 Shadowrocket 两种地址使用同一令牌、身份和住宅出口；住宅账号密码只保存在服务端。

参数登记不会创建入站、配置 Tunnel、安装证书或证明公网可用。对应 3X-UI 需要已存在的 VLESS WS 入站，并由 root 同步器明确锁定。服务器的“导出合并资料”是标记 `not_applied` 的参考包，不能替代同步收据或覆盖整个 Xray 配置。

旧 Reality/TLS 配置仍可读回，供迁移和撤销使用。`server.inspect_host` 保留历史 Reality 只读检查功能，WS 配置使用后台公开字段或公开参数 JSON 导入。禁止从旧共享节点直接复制身份来代替每用户的受管身份。

## 全局订阅模板

管理后台侧栏的“全局订阅模板”编辑所有用户共用的 Clash / Mihomo 配置。默认本地代理监听端口为 `mixed-port: 7897`，这是客户端代理端口；管理服务仍使用 `8787`。Clash Verge 的全局端口与 TUN 覆盖项会影响实际运行配置，刷新订阅不会自动修改这些本机设置。

编辑器使用 JSON，分为 `config`（端口、DNS、嗅探、Geo 数据与常规设置）、`rules`（公共代理和拦截规则）、`domestic_direct_rules`（显式国内直连策略的规则）。保存后写入现有私有 SQLite 数据库，所有既有和新建用户下次下载 Clash 订阅时采用；订阅令牌、节点身份和绑定不变。刷新页面、服务重启后仍保留。编辑时携带版本摘要，另一管理员先保存时返回冲突，保留草稿并重新加载后合并。预览只含不可连接的 WS + TLS 示例节点。

默认模板参考通用分流配置，启用并发连接、延迟统一、严格进程识别、选择记忆与 TLS/HTTP 嗅探；禁用目标覆写。修正了原示例的 DNS 列表和通配符格式，Geo 数据使用项目官方 GitHub 下载地址。首次使用广告 GEOSITE 或国内 GEO 规则时，Mihomo 可能需要下载对应数据；关闭自动更新不代表本地已有数据文件。`skip-proxy` 属客户端配置，本模板不向内核下发此字段。

节点与代理组由服务端自动注入，模板不接受 `proxies`、身份凭据或自定义代理组。WS + TLS 节点不添加 Reality 参数或 Vision flow。保留已有兼容入口类型，模板本身不改造服务器入站。UDP 继续整体拒绝；Claude / Anthropic 域名保护规则固定在模板规则之前，末尾固定 `MATCH,CLAUDE-SWITCHER`。严格策略不启用国内直连规则；在地址显式增加 `routing=domestic-direct` 时，国内、局域网和邮件直连规则排在 AI 规则之后。Shadowrocket 订阅仍只交付节点，不携带这份 Mihomo 全局模板。

DNS 保持本机 `127.0.0.1:1053` 监听，fake-IP、AAAA 空回应，业务 DoH 通过受管组，默认同时使用 Cloudflare 与 Google 的 IP 地址 DoH。入口域名启动解析走单独的国内 IP 地址 DoH `#DIRECT`。不添加业务直连 DNS fallback 或国内 DNS policy。模板保存会校验字段、类型、端口、规则目标及 DNS 约束；错误不覆盖已保存版本。

管理 API（需要管理员认证）：`GET /api/subscription-template` 返回 `template`、`revision`、两种策略的 `previews`；`PUT /api/subscription-template` 接受 `{ "template": {...}, "revision": "当前版本摘要" }`。有效保存返回新版本，过期版本返回 `409`。实现使用 Python 标准库，不要求服务端安装 YAML 库。默认实现与字段约束见 `server/subscription_template.py`，完整生成示例见 `server/examples/managed-subscription.yaml`。

## 保存和复用安装资料

在服务器操作列点击“安装资料”，选择 ZIP、Markdown、HTML 或 TSV 上传。资料按服务器归档，保存于管理服务的私有 SQLite 数据库中；只有管理员可以列出和下载。文件不会自动上传到云服务器，也不会执行安装包、解压 ZIP 或在线运行 HTML。

每个文件最多 5 MiB，每台服务器最多 20 个文件、合计 25 MiB。同一服务器重复上传相同内容会复用原件，不占新额度。列表显示文件名、大小和 SHA-256；下载保留原始字节。原始文档不会被修改。备份和迁移管理服务时，需要一起保管私有数据库，不将其提交到 Git。

后续新服务器可直接下载已归档的资料包，按其安装指南准备独立凭据并逐步验证，再登记新服务器入口。软件清单是版本快照，不是批量安装脚本。安装记录应区分现有服务器检查、模板语法检查和全新服务器完整重装。

## 管理流程

管理页使用左侧菜单切换概览、服务器管理、IP 资源、批量导入、用户绑定与订阅交付。手机端从顶部菜单按钮打开导航。手动添加 IP 或创建用户使用弹窗；新订阅统一在交付页查看和复制，用户更换绑定后会同步显示当前 IP，停用后移除待交付项。

1. 在“服务器管理”登记云服务器 ID、名称、入口地址和地区，登记现有的 VLESS + WS + TLS 公开参数。
2. 添加家庭代理 IP 或批量导入前选择其所属云服务器。新家庭代理支持 SOCKS5 或 HTTP。
3. 创建用户并绑定一个可用 IP。一个 IP 不能被两个用户绑定。
4. 在“订阅交付”页查看和复制 Clash 与 Shadowrocket 两种地址。后台安全保存令牌，可再次查阅。
5. 配置独立同步器并等待“已应用到 3X-UI”，再验证用户的实际出口后交付订阅。未配置同步器时需人工应用合并资料。停用用户会使订阅下载失效，并由同步器撤销受管身份；未配置同步器时需管理员应用撤销。轮换 token 只使旧订阅链接失效，不改变入口身份，也不撤销已下载节点。

管理 API 使用 `Authorization: Bearer <登录会话 token 或原 API token>`。密码登录端点为 `POST /api/auth/login`，JSON 字段为 `username` 和 `password`；退出端点为 `POST /api/auth/logout`，登录状态查询为 `GET /api/auth/session`。会话 token 仅在登录成功时返回，密码和登录请求不会写入访问日志。公开订阅端点是 `/sub/<token>/<服务器ID>.yaml`，旧 `/s/<token>` 仍直接返回相同内容。两种路径默认输出 Clash/Mihomo YAML，也支持 `?format=uri` 和 `?format=base64`。响应头 `X-Claude-Switcher-Egress-IP` 是客户端本地校验绑定的依据。

在订阅交付页点击“恢复已有地址”，粘贴本项目以前生成的地址，可转换为当前公网域名下的新格式。`POST /api/subscriptions/convert` 要求管理员认证，仅接收 `{"url": "<旧地址>"}`，返回用户公开字段和 `subscription_url`。恢复仅验证本地记录，不访问输入网址；在事务中保存提供的当前令牌，不轮换令牌，也不修改 UUID、绑定或原时间戳。可接受空查询或单个显式格式参数，重复恢复不改写数据库。输入来源仅允许已配置的公网来源或本机管理端口的环回 HTTP 地址。其他服务生成的旧静态订阅不能通过此接口转换。历史哈希记录保持原样，无法还原丢失的令牌，需提供之前保存的地址，或明确轮换令牌使旧链接失效。

## 多服务器与固定订阅

服务器与 IP 是一对多关系，用户通过绑定的 IP 确定唯一服务器。WS 订阅只返回该用户当前绑定的一个入口。历史双入口配置在迁移前仍返回原主、备用节点，两者共用同一住宅出口。新增其他服务器不会把节点追加到旧订阅。链接保持稳定，除非管理员主动轮换 token。管理员主动更换用户绑定时，该用户的原链接会指向新选择的 IP 和服务器。文件名用于识别交付时的服务器，不决定访问权限；换绑后，原文件名的链接仍按该令牌返回当前绑定。

服务器的 `host` 是登记的云入口地址；WS 客户端连接 `gateway.address`。两者不填写 SSH 命令、面板凭据或完整 URL。多位用户可以共用该入口和端口，每人使用独立 UUID。订阅中的节点地址是云入口，响应头中的预期出口仍是家庭 IP。

服务器可先只登记信息，显示“待配置”。为其配置完整网关后，才能创建绑定和生成可交付订阅。WS 模式字段见下方“WS 唯一入口与订阅验收”。历史 TLS 模式的 `gateway` 包含 `kind: "xray-vless-tls"`、`port`、`server_name`、`certificate_file`、`key_file`。证书字段只接受云服务器上的绝对路径，不读取维护者本机文件，也不接收私钥内容。客户端强制 TLS 验证。

新 IP、批量导入和新绑定必须选择已启用服务器。IP 地址在所有服务器之间保持唯一；重复导入会跳过，不会把其他服务器的 IP 移过来。已有用户绑定时，不能通过资源更新更换该 IP 地址或已确定的服务器；需要迁移时先解除绑定，再操作资源，最后由管理员明确重新绑定。

服务器停用后，停止新资源分配并拒绝其订阅下载，但保留原用户、IP、绑定和 token。重新启用后，原链接恢复。此操作不会关闭远端服务器，也不能撤销用户已经下载的代理配置。

升级旧数据库会增加服务器归属、网关配置及用户入口身份，保留原始代理凭据、用户 token 和绑定。历史未归属 IP 的旧直连订阅继续可用，这是迁移兼容例外。迁入云网关前先解除旧绑定；仅 SOCKS5 和 HTTP 资源可以设置云归属。旧直连配置中已经交付的家庭代理凭据需要另行轮换，迁移不能收回这些凭据。重复启动不会重建用户 UUID；旧库若含重复 IP，迁移将回滚并停止，需要先核对重复资源。

切换器为已确认的那一个旧静态订阅地址提供专用兼容处理，使用 URL 摘要匹配，不接受同域名下任意旧令牌，也不要求用户更换地址。旧响应没有绑定 IP 标记时，从“住宅出口”节点取得预期 IP，并仅解析已支持的两节点 WS 或历史三节点 block YAML 结构；重复字段、引用、标签、未知节点选项或不安全 TLS 设置会被拒绝。导入时重新生成严格配置：两节点配置保留 WS → SOCKS5 住宅链；历史三节点配置保留 Reality 与 WS 两条住宅链，手动选择组仅包含完整住宅链路。两者都禁用 UDP，全部业务规则和业务 DoH 固定走该组。旧国内直连规则及 DNS 直连兜底不会复制。只有入口域名的启动解析使用 IP DoH `#DIRECT`。生成文件与订阅状态保持 0600，失败时保留之前保存的配置。兼容转换不探测或修改客户端当前监听端口；生成文件中的端口是模板候选值，Antify 必须使用实际监听诊断值。

该兼容处理发生在新版客户端，既不修改线上旧文件，也不把旧节点变成服务端受管身份；旧配置中的住宅凭据仍会交付到客户端。直接将原地址导入其他代理客户端时，得到的仍是线上旧配置。合成旧输入见 `server/examples/legacy-subscription.yaml`，新版托管示例见 `server/examples/managed-subscription.yaml`；示例均不含真实凭据。

管理接口为 `GET /api/servers`、`POST /api/servers` 和 `PUT /api/ips/{id}/server`。服务器写入字段为 `id`、`name`、`host`、可选 `region`、可选 `status` 及可选 `gateway`。已有服务器省略 `status` 或 `gateway` 时保留原值。`gateway: null` 清除网关，有绑定时不允许清除。IP 归属接口只接收 `server_id`，不改动其他节点配置。IP 与用户列表包含派生的服务器信息，不回显代理凭据或入口 UUID。

## 应用独立 TLS 云服务器配置

在服务器列表点击“导出 Xray 配置”。`GET /api/servers/{id}/xray-config` 需要管理员认证，返回禁止缓存的 JSON 附件。文件含家庭代理凭据和用户 UUID，只能交给对应服务器管理员，并以私有文件权限保管。

每台服务器的完整配置包含一个共享 VLESS + TLS 入站、该服务器当前有效绑定的用户，以及每人的家庭代理出站。路由同时匹配入站和用户身份，仅允许 TCP。默认出站和未匹配流量均进入 blackhole，不生成直连出口。托管订阅的 Clash 规则将默认流量交给受管节点组，并关闭 UDP 与 IPv6；可选 WS 备用也必须使用相同的家庭出口。

将导出文件放在对应云服务器的私有配置目录，准备与 `server_name` 匹配的有效 TLS 证书，再用 `xray run -test -config /path/to/config.json` 检查。当前导出按 Xray `v26.10.21` 验证。确认监听端口无冲突后，由管理员应用配置并重启 Xray。已有 3X-UI 管理的实例需要先核对其配置所有权，不能直接覆盖面板管理的运行配置。

导出是数据库当前状态的快照。采用人工部署方式时，新增、停用、解绑和重新分配后都需要重新导出并应用，才会改变云端运行状态；已配置 3X-UI 自动同步的服务器由独立同步器应用受管变更，无须逐次手工导出。TLS 模式的服务器停用时仍可导出无有效用户的拒绝配置。人工应用合并包时需要移除旧身份，不能用空包替代撤销操作。订阅 token 只控制配置下载，轮换 token 不轮换入口 UUID，也不会撤销已下载节点。管理后台 HTTP 进程只保存业务数据、生成订阅和展示同步结果，不执行 SSH 或直接调用 3X-UI 管理 API。

## 批量导入代理

管理页的“批量导入代理 IP”接受每行一条 `IP:端口:用户名:密码`。选择所属服务器及 SOCKS5 或 HTTP，点击“预览校验”，核对行号、地址、端口及新增/跳过结果，再点“入库并生成订阅”。每个新增 IP 会自动创建一个用户、绑定这个 IP，并生成独立订阅地址。格式本身不能判断代理协议，所选协议应用于整个批次。

每批最多 200 条、请求体最多 64 KiB，仅接受公网 IPv4。空行会忽略，密码可以包含冒号，用户名不能包含冒号。预览不写数据库；任何一行有误时整批不写入。已有 IP 及批内重复 IP 会跳过，不覆盖原来的端口、协议或认证信息。修改内容、协议或所属服务器后必须重新预览。服务端会在确认写入时重新检查该服务器是否启用。

管理 API 为 `POST /api/ips/import`，JSON 字段为必填的 `server_id`、`text`、`protocol`（`socks5` 或 `http`）、`dry_run`（布尔值，默认 `true`）。确认时传 `dry_run: false`，服务端重新校验并在同一事务中写入。管理页会传 `create_subscriptions: true`。此时确认响应的 `subscriptions` 数组包含每个新用户的 `user_id`、`ip_id`、`address`、`server_id`、`server_name`、`subscription_url`。省略此字段或传 `false` 时仅登记 IP，传其他类型会被拒绝。IP、用户及绑定在同一事务中写入；任何一步失败都会整批回滚。预览不生成订阅，重复 IP 不新建用户、不返回或轮换旧订阅。

新建、导入和轮换会把可恢复令牌与哈希一起存入私有 SQLite，目录权限 0700、文件权限 0600；数据库备份也必须按凭据保护。管理员通过 `GET /api/subscriptions` 查阅全部用户的交付状态，可用且令牌完整的记录包含显式 Clash 与 Shadowrocket 链接。普通用户/IP 列表与网关导出不包含订阅令牌。链接默认隐藏，可分别复制或复制全部，刷新、重新登录和服务重启后仍可查阅。历史记录显示待恢复，使用“恢复已有地址”保存有效旧链接；没有旧链接时才主动轮换 token，旧地址随即失效。响应和管理列表不回显家庭代理用户名与密码。托管订阅只包含用户自己的云入口身份。请勿提交数据库或真实代理列表到仓库。

生成订阅和导出配置不探测真实代理可用性，也不证明公网出口固定或防泄露。本地 `127.0.0.1` 订阅地址仅供本机测试，交付其他设备前需要部署服务并设置其可访问的 HTTPS `base-url`。

## WS 唯一入口与订阅验收

公开入口参数示例（均为合成值）：

```json
{
  "kind": "xray-vless-ws",
  "address": "edge.example.com",
  "port": 443,
  "server_name": "edge.example.com",
  "host": "edge.example.com",
  "path": "/example-ws",
  "inbound_tag": "managed-ws",
  "tls_termination": "external"
}
```

`address` 是客户端连接地址，`server_name` 用于 TLS 验证，`host` 是 WS Host。`tls_termination: "xray"` 要求原生 WS+TLS；`"external"` 要求明文 WS 源站只监听 loopback。参数登记不证明边缘证书、转发或公网可达性。

WS 使用稳定派生 UUID 和空 flow，升级时保持原有 WS 身份、订阅令牌和出口绑定。用户 TCP 只进入自己的住宅出站，UDP 拒绝。迁移后订阅不含 Reality；旧受管 Reality 身份禁用并保留拦截规则，非受管共享用户不变。同步配置迁移与回滚见 [部署说明](../deploy/README.md#ws-唯一入口迁移与撤销)。

旧静态订阅地址可保留两节点布局：住宅 SOCKS 节点的 `dialer-proxy` 必须指向 WS 节点，规则选择住宅链。仓库核心脚本兼容旧三节点和新的两节点输入，统一生成住宅选择组。已安装旧版转换器需要更新后才能识别两节点布局；不要把后台升级当作本机客户端已经更新。

托管 Clash 配置自带 DNS：loopback `1053`，fake-IP `198.18.0.1/16`，AAAA 空回应，普通解析使用 Cloudflare / Google 的 IP 地址 DoH 并指定所选受管组。禁用 HTTP/3、系统 hosts、业务直连 fallback 和业务 `DIRECT` 规则。代理入口域名的启动解析使用 `223.5.5.5` 和 `120.53.53.53` 的 IP 地址 DoH `#DIRECT`，用于避免先解析代理、又要先连代理的循环。这一例外只服务于解析受管入口地址，不能用于业务访问，也意味着不能宣称所有 DNS 都经住宅。Claude、Anthropic 相关域名先匹配受管节点规则，代理节点失败时请求失败，不降级到国内直连。旧订阅的国内直连方案只有在显式选择 `domestic-direct` 策略时才可生成。配置也不会替用户改变系统 DNS、TUN 或 Antify 接管；客户端全局覆盖项可能改变最终行为。参数含义参考 [Mihomo DNS 文档](https://wiki.metacubex.one/config/dns/)。

严格模式是公开订阅的默认值。需要日常国内分流时，在订阅地址上显式增加 `?routing=domestic-direct`；如果地址已有格式参数，使用 `?format=clash&routing=domestic-direct`。该模式只把明确的国内规则放在 AI 规则之后，Claude 和 Anthropic 域名仍先进入受管组。未知或重复的查询参数会被拒绝。示例文件见 [`server/examples/managed-subscription.yaml`](examples/managed-subscription.yaml)。

`subscription ports --json` 从运行内核的本地控制器读取端口，再检查 loopback 监听。缺少授权、多个内核或无法读回时返回 `runtime_verified: false`；配置文件只能标记为候选。订阅默认 `mixed-port: 7897` 与本机实际端口可能不同，诊断只提醒，不改全局配置或 Antify 上游。

开发验收工具需要显式网络授权标志和已有 PyYAML；缺失时返回 `missing_dependency_pyyaml`，不自动安装依赖。私有 YAML 与绑定清单必须权限 `0600`，清单至少含 `expected_ip`。使用实际下载文件逐节点运行：

```bash
python3 deploy/probe_mihomo_subscription.py \
  --subscription /private/downloaded.yaml \
  --manifest /private/binding.json \
  --core /path/to/mihomo --allow-network --node-index 0
# 历史双入口配置应对每个节点分别验收；WS 唯一入口只有 index 0。
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

## 与 3X-UI 的分工与同步

| 组件 | 职责 |
| --- | --- |
| 订阅后台 | 管理用户、住宅代理 IP、服务器及绑定关系；生成订阅；维护 Clash / Mihomo 全局模板；展示同步核验结果 |
| 独立同步器 | 读取本项目数据库，通过本机 3X-UI API 更新受管客户端身份、住宅出口和路由，并核对 Xray 运行配置 |
| 3X-UI | 管理已有入站和 Xray 配置，提供同步器调用的管理 API |
| Xray | 接收用户 VLESS WS 连接，按用户身份将 TCP 流量转发到绑定的 SOCKS5 / HTTP 住宅代理 |

配置链路：`管理后台 → 项目数据库 → 独立同步器 → 3X-UI API → Xray`，同步器将核验结果写回状态文件，后台读取后展示。

当前已部署的 WS/TLS 流量链路：`客户端 → Cloudflare TLS / Tunnel → 本机 Xray WS 入站 → 用户绑定的住宅代理 → 目标网站`。TLS 由外部入口终结，明文 WS 源站只监听 loopback；这与后台订阅下载的 HTTP 监听端口是不同的入口。其他部署也可使用 Xray 原生 TLS，须按实际入口配置。

绑定或换绑后，同步器更新相应身份、住宅出口和路由；解绑或停用后禁用受管身份，并保留兜底拦截规则。每轮同步结束后等待 15 秒再检查，配置未变化时不写面板、不重启；应用变化时可能重启共享 Xray，短暂影响同实例其他连接。“已应用到 3X-UI”表示当前配置摘要、同步收据及运行时核对一致，且核验未超过 60 秒；住宅代理的实际可用性还需流量测试。

日常用户、住宅 IP 和绑定在本后台维护。3X-UI 中由同步器管理的身份、出口和路由应通过后台变更；手工冲突或固定入站漂移会阻止同步。同步器保留非受管客户端和路由。后台支持登记多台服务器，但每个同步器实例只处理配置中明确指定的一台；新登记的服务器仍需分别部署和锁定同步目标。安装、目标锁定和排障见 [部署说明](../deploy/README.md#核对实际代理接入)。

Clash / Mihomo 全局模板管理客户端端口、DNS、嗅探和分流，保存后在用户下次更新订阅时生效；它不修改 3X-UI 的服务端路由或住宅绑定。模板默认 `mixed-port: 7897`，订阅后台 HTTP 默认端口为 8787，两者用途不同。

住宅 IP 的 `config` 字段保存 SOCKS5 或 HTTP 上游认证参数，住宅代理凭据留在服务端，托管订阅只包含云入口身份。订阅 token 控制配置下载，轮换 token 不撤销已下载节点；需要撤销实际接入时停用用户并核对同步结果。历史未归属节点保留原有协议渲染，以兼容旧数据。

## 安全边界

订阅 token 等同密码。服务端只存 token 的 SHA-256，管理列表不回显 token，内置访问日志不记录请求路径。默认只接受 HTTPS 订阅地址，本机环回测试除外。数据库还保存家庭代理认证和入口 UUID，应按私密配置保护。不要把数据库、导出的 Xray 配置、管理员 token 或用户订阅地址提交到仓库。
