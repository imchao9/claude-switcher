# iOS Shadowrocket 订阅

在订阅交付页复制标注为 Shadowrocket（小火箭）的 HTTPS 地址 `/sub/<token>/<server>.yaml?format=shadowrocket`。Clash 地址显式带 `?format=clash`。后台保存两种格式使用的同一令牌，刷新、重新登录和服务重启后可再次查阅。历史未保存的令牌可通过“恢复已有地址”恢复，不会自动轮换。

未指定格式且 User-Agent 包含 `Shadowrocket` 时，服务返回标准 Base64 编码的节点链接列表；其他客户端继续默认获取 Clash YAML。扩展名不决定响应格式。

交付页分别显示、隐藏和复制两个格式的地址，不依赖客户端 User-Agent 猜测。显式 `format=clash`、`uri`、`base64`、`shadowrocket` 优先于 User-Agent，最后两种输出相同。空值、重复值和无效格式返回 HTTP 400。成功、格式错误和已撤销订阅均返回 `Cache-Control: no-store` 与 `Vary: User-Agent`。

托管入口的订阅仅含当前用户的一个 VLESS + WS + TLS 节点，使用其稳定派生的独立 WS UUID；Clash 与小火箭格式使用同一身份、令牌和住宅出口绑定，不包含住宅上游账号密码。URI 明确包含 `type=ws`、`security=tls`、SNI、WS Host 与 path，`allowInsecure=0`，不包含 Reality 或 Vision flow。

云入口到每位用户专属住宅出口的链路由服务端处理，用户无需手工配置链式代理。导入后在小火箭中将 Global Routing（全局路由）设为 Proxy（代理），选择分配给自己的节点，并开启 VPN。节点订阅不会配置小火箭的全局路由、UDP 或 DNS；导入后仍需在 iOS 设备上核验实际出口、UDP 和 DNS 行为。服务端节点交付测试不能证明整台设备防泄漏。此变更未经过 iOS 小火箭真机导入验收。

格式依据为 [3X-UI v3.10.0 Subscription 文档](https://github.com/MHSanaei/3x-ui/blob/v3.10.0/docs/content/docs/en/config/subscription.mdx) 的标准 Base64 节点列表，以及 [Share Links 文档](https://github.com/MHSanaei/3x-ui/blob/v3.10.0/docs/content/docs/en/config/share-links.mdx) 的 VLESS URI 结构。小火箭为闭源客户端，具体版本的导入与设备网络行为需要真机验证。

隔离回归命令为 `python3 -m unittest tests.test_shadowrocket_subscription -v`。测试启动临时 HTTP 服务，使用合成节点与凭据，并执行交付页复制函数的登录态、过期条目和退出后回调检查。
