# 安全模型与边界

本服务的安全边界**由应用自己完整提供**：认证、会话、CSRF、XSS/注入、开放重定向、
请求体上限与 framing、限流、安全响应头、备份一致性；反向代理只负责 TLS 终结与
**连接级**防护（慢连接、带宽）。

## 1. 信任边界

| 谁 | 被信任到什么程度 |
|---|---|
| `[server].trusted_proxies` 里的对端 | 它写的 `X-Forwarded-For` / `X-Forwarded-Proto` 是可信的（取**最右**一跳） |
| 其它任何对端 | 转发头一概不采信；scheme 由 `site_url` 声明裁决 |
| 站点根目录下的内容（`articles/`、`custom/`） | 可信来源；其 Markdown 仍经白名单净化后才作为 HTML 输出 |
| 请求里的任何东西（路径、查询串、正文、头部、Cookie） | 一律不可信，非安全方法必须过 CSRF 校验 |
| `config.toml` | 可信；但其中的 URL 与路径仍会被校验（注入防护、目录存在性） |

应用**只监听回环地址**。把端口暴露到回环之外就等于绕过整个边界：能连上的人跳过
TLS/HSTS、边缘限速与连接级防护（启动日志对此有明确告警）。

## 2. 安全性质

| 面 | 当前行为 |
|---|---|
| 认证 | 登录只对**错误**凭据限流；闸门命中时仍照常校验密码，正确凭据永不因闸门被挡；账号不存在时执行等量哈希校验抹平计时差异 |
| 会话 | 32 字节随机 token 只存服务端；登录轮换（防会话固定）；改密/删号与"作废全部会话"处于同一写事务；绝对 + 滑动双过期；Cookie 为 `HttpOnly` + `SameSite=Lax` + （HTTPS 时）`Secure`，可选 `__Host-` 前缀 |
| 授权 | 每个路由显式声明 `auth=` / `permission=`，未知取值在**注册期**报错；`permission` 只能比 `auth` 更严；评论改/删走 `require_owner`；授权只由应用负责（代理侧不放路径规则） |
| CSRF | 双提交 Cookie，调度器对**所有**非安全方法默认校验；令牌格式用 `hmac.compare_digest` 常量时间比对 |
| XSS | Jinja 自动转义；Markdown 白名单净化（裸 HTML 转义、协议白名单、危险标签连内容丢弃）；模板里 **0** 处 `\|safe`、0 内联脚本、0 内联样式 |
| 注入 | SQL 全参数化；动态标识符只来自代码内白名单；配置里的 URL/路径有注入防护校验 |
| 路径穿越 | 没有文件服务面；文章与页面只按扫描出来的文件名查表，URL 从不参与路径拼接；slug 白名单 `[A-Za-z0-9._-]` 且禁止点开头 |
| 开放重定向 | 回跳地址统一走 `core.http.safe_next_path`（唯一实现）：只接受站内路径，拒绝 `//`、反斜杠、CR/LF、TAB 与控制字符 |
| 代理信任 | `X-Forwarded-For` / `X-Forwarded-Proto` 只在对端属于 `trusted_proxies` 时采信，且取最右一跳；scheme 另有 `site_url` 兜底裁决 |
| 安全响应头 | CSP（`script-src 'none'`、`style-src 'self'`）、`X-Content-Type-Options`、`X-Frame-Options`、`Referrer-Policy`、COOP/CORP、`Permissions-Policy`、HSTS（HTTPS 时）—— 每个响应都从同一出口注入，**包括** 400/403/404/405/413/500 与请求解析期的早期错误 |
| 缓存 | 登录态 HTML `no-store`；匿名 HTML `no-cache`；错误页与登录/注册页 `no-store` —— 共享缓存不会把个性化页面发给别人 |
| 请求边界 | 缺 `Content-Length` 的表单方法 411、非表单类型 415、超上限 413（不读 body）、截断 400、方法不允许 405 + `Allow` |
| 备份/恢复 | 走 SQLite 在线备份 API（不是 `cp`）；目标自创建起 `0600`、`O_EXCL` 拒绝覆盖、`quick_check` 校验、失败删半成品 |
| 迁移 | 破坏性迁移前自动写数据库快照，快照失败就中止；`user_version` 只升不降，降级运行会被拒绝 |
| 日志 | 不记查询串、请求体、Cookie 与凭据；控制字符转义；`X-Forwarded-*` 只在受信对端生效 |

### 状态码契约

| 情况 | 状态码 |
|---|---|
| 表单令牌缺失或无效 | `400`（"表单令牌无效"的常规语义；认证 / 权限失败才是 `403`） |
| 表单方法但未声明 `Content-Length` | `411` |
| 非表单 `Content-Type` | `415` |
| 超过 `max_body_size` | `413` |
| 方法不允许 | `405` + `Allow` |
| 含空段的路径（`//`、多余结尾斜杠） | `404`（不做归一化，避免 `//a` 命中 `/a`） |
| 不存在的页面 / 未登录访问受保护页面 | `404` / 跳转登录页并带回跳地址 |

## 3. 平台约束

**只支持 Linux**（以及同样提供 `fcntl.flock` 的 POSIX 系统）。跨进程写互斥、迁移串行化
与限流记账的原子性都建立在 `flock` 之上，没有它就无法保证"多个 worker 不会同时写"。
运行平台不提供 `fcntl` 时，服务在启动阶段就拒绝继续 —— 不会退化成"进程内假锁"（那会
静默丢掉跨进程保证）。

## 4. 反向代理差异

### 4.1 实测（nginx / 最简 TCP 转发器 / 直连应用）

```
probe                      direct    nginx     relay(最简 TCP)
/                          200       200       200
//about                    404       404       404      <- 应用拒绝重复斜杠；代理转发原始 URI
/css/style.css             404       404       404      <- 没有静态服务面

HSTS + Secure Cookie       有        有         有       <- site_url 声明 https，三条路径一致

chunked POST（无长度）      411       400        411      <- nginx 缓冲请求体并补 Content-Length
CL + TE 同时出现            400       400        -        <- WSGI 服务器直接拒绝

伪造 XFF（真实客户端 127.0.0.2，经 nginx）  -> 应用记录 127.0.0.2（伪造前缀被忽略）
伪造 XFF（客户端就是本机 127.0.0.1）        -> 应用记录 9.9.9.9（见 §7 R1：同址对端无法区分）

请求体 1.5 MB（应用上限 1 MiB）
  nginx 2m   -> 413，应用日志 +1（应用自己处理，可观测）
  nginx 1m   -> 413，应用日志 +0（nginx 抢答，应用完全不知道）

伪造 XFP（site_url=http）
  经 relay + trusted_proxies=[loopback] -> 有 HSTS（客户端把应用骗了）
  经 relay + trusted_proxies=[]         -> 无 HSTS（不可伪造）
  经 nginx                              -> 无 HSTS（nginx 覆写）
伪造 XFF（site_url=https, trusted=[]）    -> 记录转发器地址，不可伪造
```

### 4.2 各代理的默认行为

| 关注点 | nginx | Caddy v2 | Apache 2.4 | HAProxy | 最简 TCP 转发器 | CDN |
|---|---|---|---|---|---|---|
| TLS 终结 | 需配证书 | 自动申请/续期 | 需配证书 | 需配 `crt` | stunnel 可行 | 边缘终结 |
| http→https | 手写 `return 301` | 自动 | 手写 Rewrite | 手写 `redirect` | **无** | 可开 Always Use HTTPS |
| 慢连接防护 | 手写 `client_*_timeout` + `limit_conn` | 需设 `servers.timeouts` | `mod_reqtimeout` 默认开 | `timeout client` 必填 | **完全没有** | 边缘有，源站仍需 |
| `X-Forwarded-For` | 默认追加 | 默认设置 | 默认追加 | **`option forwardfor` 才加** | 原样透传 | 追加自己的跳 |
| `X-Forwarded-Proto` | 覆写 | 默认设置 | **不自动设** | **不自动设** | 原样透传 | 通常设置 |
| chunked → CL | 缓冲并补 | 缓冲 | 缓冲 | 归一 | 不归一 | 归一 |
| 请求体上限 | `client_max_body_size` 默认 **1m** | 需显式设 | `LimitRequestBody` 默认很大 | 无默认 | 无 | 视套餐 |
| 上游 HTTP 版本 | 必须 `proxy_http_version 1.1` | 默认 1.1 | 默认 1.1 | 别在 server 上开 h2 | 透明 | 需 1.1 |
| 缓存 HTML | 默认不缓存 | 默认不缓存 | 默认不缓存 | 不缓存 | 不缓存 | **默认缓存**（靠 `no-store` 挡住） |
| `Server` 头 | `proxy_hide_header` 可选 | `header_up -Server` | `Header unset` | `del-header` | 透传 | 被覆盖 |

### 4.3 会把差异变成缺陷的四个组合

1. **HAProxy 忘了 `option forwardfor`** → 应用侧所有客户端都是 LB 的 IP，per-IP 闸门
   退化成一只桶：一个攻击者灌满就能挡住别人。
2. **代理的请求体上限没跟着 `max_body_size` 调大**（nginx 默认 `1m`）→ 超限请求被代理
   抢答 413，**应用侧没有任何日志**，"应用自己返回 413 且可观测"这条契约静默失效。
3. **最简 TCP 转发器 + 默认 `trusted_proxies`** → 客户端可自报 `X-Forwarded-For` /
   `X-Forwarded-Proto`。处方见 §5（scheme 侧另有 `site_url` 兜底，不会因此降级）。
4. **在代理里做路径级访问控制**（例如只让 `/admin` 走内网）→ 代理与应用对 `/a/../b`、
   `//a`、`%2f` 的归一化规则不同，可能被绕过。授权只由应用负责。

## 5. 按部署形态选配置

| 部署形态 | `[server].trusted_proxies` | 结果 |
|---|---|---|
| HTTP 反代（nginx / Caddy / Apache / HAProxy `mode http`） | 代理的真实 IP（同机即默认值） | 真实客户端 IP 可用；客户端伪造的前缀被"最右非受信跳"忽略 |
| 最简 TCP 转发器（socat / stunnel / DNAT / `mode tcp`） | **`[]`** | 转发头一概不采信，XFF/XFP 无法伪造；scheme 由 `site_url` 决定。代价：客户端 IP = 转发器地址，per-IP 闸门变成一只桶 |
| CDN + 源站反代 | 源站反代的 IP | 还要在源站把 `X-Forwarded-For` 重写成 CDN 给的客户端 IP（Cloudflare：`$http_cf_connecting_ip`），否则应用只看到边缘 IP |
| 直连应用端口（无代理） | `[]` | 同理不可伪造；同时提醒：`--bind 0.0.0.0` 时端口一旦可达就是明文且无边缘防护 |

与代理无关、但必须同时满足的四条：

1. 配好 http→https 重定向（应用不做；Caddy 自动，其余要手写，纯 TCP 转发器做不到）；
2. 代理请求体上限 ≥ 应用的 `max_body_size`；
3. 不要把 h2/h3 协商到上游（应用只说 HTTP/1.1）；
4. 不要把应用挂在子路径下（应用按站点根生成绝对 URL 与回跳地址）。

## 6. 应用不终结 TLS、也不绑定某个 WSGI 服务器

TLS 不属于应用，也不属于某一个 WSGI 服务器：把 TLS 绑到服务器的专有配置上（例如
Gunicorn 的 `certfile/ssl_context`）会让"换掉 WSGI 服务器"变成"换掉安全语义"。因此：

- 应用不监听公网端口、不终结 TLS：对外服务、TLS 与 http→https 一律交给反向代理
  （见 [`HOSTING.md`](HOSTING.md)）；
- 应用是纯 WSGI callable（`elenvind.wsgi:application`），任何 WSGI 服务器都能运行；
- `Secure` / HSTS 的判定只依赖 `site_url` 与受信代理的 `X-Forwarded-Proto`，不依赖
  服务器报告 `wsgi.url_scheme`（它只作为额外的正向信号）；
- "属于应用的配置/能力"与"属于启动器的配置"在 [`HOSTING.md`](HOSTING.md) 末节分成两张表：
  前者换服务器照样生效，后者换成等价参数即可。

## 7. 刻意保留的限制

| # | 限制 | 严重度 | 为什么保留 / 怎么缓解 |
|---|---|---|---|
| R1 | **同机进程可以伪造 `X-Forwarded-For`**：默认 `trusted_proxies` 含回环，客户端与代理同址时"最右非受信跳"退化为客户端自己写的值 | 低 | "信任同机代理"的必然代价。缓解：反代在别的机器上就把列表收窄到它的真实 IP；不跑反代就设 `[]`。邮箱维度与全站维度的闸门不按 IP 计数，不受影响 |
| R2 | 声明 `site_url = https://…` 后用**明文**访问应用端口会因 `Secure` 丢 Cookie（表现为登录没反应） | 刻意 | fail-closed：宁可在明文上不可用，也不在明文上发放会话。本地测试把 `site_url` 写成 `http://…` 即可 |
| R3 | 反代开启 HTML 压缩 + 页面回显用户输入（注册表单回填昵称/邮箱）＝ BREACH 面 | 很低 | Zero-JS + `script-src 'none'` + 跨源读不到响应长度，实际不可利用；要彻底消除就在代理侧关掉 HTML 压缩 |
| R4 | 内容目录里的**软链**会被当成页面发布（`custom/secret.md -> /etc/passwd`） | 低 | 需要本机写权限才能制造，不新增远程面。别在 `articles/`、`custom/` 放软链 |
| R5 | 第三方可构造 `/login?changed=password` 让用户看到一条"密码已修改"的假提示 | 很低 | 取值白名单（无 XSS、无状态变更），只是社交工程面；要消除就去掉这个参数，代价是改密后毫无反馈 |
| R6 | CDN / `mode tcp` 下 per-IP 限流退化到"边缘 IP"粒度 | 低 | 用 §5 的"源站重写 XFF"处方把真实客户端带进来；全站闸门仍在 |
| R7 | 代理侧的连接超时是**唯一**的慢连接防线（应用保持同步 worker，不引入线程/协程模型） | 中 | 部署时必须配 `client_*_timeout` / `timeout client`；`[server].timeout` 只是启动器级的兜底（单条连接占用 worker 的上限） |

## 8. 复跑这些检查

```bash
# 应用侧门禁（不启动网络服务、不碰仓库里的数据）
.venv/bin/python -m unittest discover -s tests -t .     # 194 tests OK
.venv/bin/ruff check .

# 头与协议契约
elenvind serve --bind 127.0.0.1:6871 --workers 1 --yes &
curl -sD- -o/dev/null http://127.0.0.1:6871/ | grep -iE 'security-policy|strict-transport|x-frame|x-content'
curl -s -o/dev/null -X POST -d 'a=b' http://127.0.0.1:6871/login            # 400（无 CSRF）
curl -s -o/dev/null -X DELETE http://127.0.0.1:6871/                        # 405 + Allow

# 域名检索与配置只增不覆盖
elenvind proxy detect --domain <域名>
elenvind proxy certs  --domain <域名>
elenvind proxy configure --domain <域名>          # dry-run：只打印计划
```

反代差异矩阵的复现：按 §4.1 的三列（直连 / nginx / 30 行标准库 TCP 转发器）各起一份，
对比状态码、`HSTS`、应用日志里的 `ip=`。最小 nginx 配置见
[`nginx.conf.example`](nginx.conf.example) 与 [`HOSTING.md`](HOSTING.md)。
