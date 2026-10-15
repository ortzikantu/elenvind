# 托管：必须有反向代理，但不依赖特定反代

**本服务只监听回环地址**（默认 `127.0.0.1:6789`），由**任意**反向代理终结 TLS 并转发。
应用**不**自己对外提供服务，也**不**使用任何 WSGI 服务器特有的 TLS 能力 —— 这样
"换掉反代"与"换掉 WSGI 服务器"都不会改变站点的运行方式与安全语义（见文末
[为什么](#为什么应用不自己终结-tls也不依赖某个-wsgi-服务器)）。

## 应用与代理的职责线

| 关注点 | 本服务 | 反向代理 |
|---|---|---|
| 认证、会话、CSRF、XSS、SQL 注入、路径穿越 | ✅ | — |
| 请求体上限（`max_body_size` → 413）、framing（411/400/415）、405 + Allow | ✅ | 可选兜底（**不要更小**，否则应用自己的 413 永远不会触发） |
| 登录 / 注册 / 评论限流 | ✅ | 可选边缘限流（不是必需） |
| 安全响应头（CSP、XCTO、XFO、Referrer-Policy、COOP/CORP、HSTS） | ✅ | — |
| scheme / `Secure` Cookie / HSTS 的判定 | ✅（由 `site_url` 与受信代理头裁决） | 只需如实转发 `X-Forwarded-Proto`（连这个都可以不发，见下） |
| 绝对地址（robots / sitemap 里的 URL） | ✅ 只来自 `site_url` | — |
| TLS 与证书续期 | — | ✅ |
| **连接级**慢连接 / 半开连接 / 带宽 | — | ✅ |
| http→https 跳转 | — | ✅ |
| 网络 ACL、地理封锁、WAF | — | ✅（如果需要） |
| 静态资源（CSS/图片/视频/图标） | — | 由**管理员**自行托管（CDN/图床/任意 Web 服务器） |

代理**不需要**知道任何业务语义：不需要限流规则、不需要 URL 白名单、不需要改响应头、
不需要隐藏或注入任何东西。它只需要"转发 TCP + 设置连接超时"。

## 应用侧需要你提供的东西

1. **代理地址写进 `[server].trusted_proxies`**（默认 `["127.0.0.1", "::1"]`，同机部署无需改动）。
   只有直连对端在这个列表里，应用才采信 `X-Forwarded-For`（真实客户端 IP）与
   `X-Forwarded-Proto`（https 判定）。
2. **`site_url` 写成真实对外地址**（如 `https://example.com`）。它是一切绝对 URL 的唯一来源，
   也是 scheme 的最终裁决者 —— 因此代理**不发** `X-Forwarded-Proto` 也不会让
   `Secure` Cookie / HSTS 静默降级；反过来，客户端伪造这个头也骗不到应用。
3. **代理要自己把 http 跳成 https**（应用不做跳转；Caddy 自动，nginx/Apache/HAProxy 要手写）。

### 按部署形态选 `trusted_proxies`

| 形态 | 该配什么 | 结果 |
|---|---|---|
| HTTP 反代（nginx / Caddy / Apache / HAProxy `mode http`） | 代理的真实 IP（同机就是默认值） | 真实客户端 IP 可用；代理会覆写/追加 `X-Forwarded-*`，客户端伪造的前缀被"最右非受信跳"忽略 |
| 最简 TCP 转发器（socat / stunnel / DNAT / HAProxy `mode tcp`） | **`[]`** | 转发头一概不采信，`X-Forwarded-For/-Proto` **都无法伪造**；scheme 仍由 `site_url` 正确决定。代价：客户端 IP = 转发器地址，per-IP 闸门只剩一只桶 |
| CDN + 源站反代 | 源站反代的 IP（不是 CDN 的） | 还要在源站把 `X-Forwarded-For` **重写**成 CDN 提供的客户端 IP，例如 Cloudflare：`proxy_set_header X-Forwarded-For $http_cf_connecting_ip;` |
| 代理与本站不在同一台机器 | 代理的实际地址 | 否则应用看到的是代理 IP；同机部署请保持回环默认值 |

### 三件必须由代理承担、且容易漏的事

1. **连接级慢连接**：Gunicorn 的 `sync` worker 一个进程同时只处理一个连接，
   "声明了 `Content-Length` 却不发正文"的少数连接会占住 worker 直到 `timeout`
   （默认 30s）被杀掉。因此 `client_*_timeout`（nginx）/`timeout client`（HAProxy）/
   连接数上限是**必需**的，不是可选优化。
2. **http→https 重定向**：应用不会自己跳转。纯 TCP 转发器做不到 —— 那种形态下
   **不要**暴露 80 端口。
3. **代理的请求体上限必须 ≥ 应用的 `max_body_size`**：nginx 的 `client_max_body_size`
   默认只有 `1m`。实测：把应用上限调大而忘记调代理上限时，超限请求会被 nginx 抢答 413，
   **应用侧日志里什么都看不到**，可观测性静默丢失。

## nginx（最小可用）

`/etc/nginx/conf.d/elenvind.conf`：

```nginx
# zone 只能出现在 http 上下文（conf.d/*.conf 被 include 进 http 块）
limit_conn_zone $binary_remote_addr zone=elenvind_conn:10m;

server {
    listen 80;
    listen [::]:80;
    server_name example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    # HTTP/2：nginx >= 1.25.1 用 `http2 on;`；更早的版本（如 Ubuntu 24.04 自带的
    # 1.24）不认识它，改用 `listen 443 ssl http2;` 并删掉这一行。
    # `elenvind proxy configure` 会按本机版本自动选对写法。
    http2 on;
    server_name example.com;

    ssl_certificate     /etc/letsencrypt/live/example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    server_tokens off;

    # ---- 连接级防护（必需）----
    client_header_timeout 10s;
    client_body_timeout   10s;
    send_timeout          10s;
    keepalive_timeout     30s;

    client_max_body_size 2m;               # 必须 > config.toml 的 max_body_size

    location / {
        limit_conn elenvind_conn 20;
        proxy_pass http://127.0.0.1:6789;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection        "";
        proxy_connect_timeout 5s;
        proxy_read_timeout    40s;         # 必须 > [server].timeout（默认 30s）
        proxy_send_timeout    40s;
    }

    # 存活探针：不碰数据库、不写访问日志。就绪探针请用 `GET /`。
    location = /healthz {
        access_log off;
        proxy_pass http://127.0.0.1:6789/robots.txt;
    }
}
```

可选的边缘限流（应用内已有三闸门，这一层只是削峰）：

```nginx
# http 上下文
limit_req_zone $binary_remote_addr zone=elenvind_auth:10m rate=20r/m;

location = /login    { limit_req zone=elenvind_auth burst=5 nodelay;
                       proxy_pass http://127.0.0.1:6789; }
location = /register { limit_req zone=elenvind_auth burst=3 nodelay;
                       proxy_pass http://127.0.0.1:6789; }
```

`manual/nginx.conf.example` 是这份配置的完整版。

## Caddy（证书自动续期，配置最短）

```
example.com {
	encode zstd gzip

	request_body {
		max_size 2MB
	}

	reverse_proxy 127.0.0.1:6789 {
		transport http {
			dial_timeout  5s
			read_timeout  40s
			write_timeout 40s
		}
	}
}
```

Caddy 自动申请/续期证书、自动 http→https、自动设置 `X-Forwarded-For` 与
`X-Forwarded-Proto`。`encode` 会压缩 HTML：本站 Zero-JS 且 CSP `script-src 'none'`，
但若你对 BREACH 类攻击特别敏感，可以去掉这一行（见 `manual/SECURITY.md` §7 R3）。

## Apache httpd

```apache
# 需要：mod_ssl mod_proxy mod_proxy_http mod_headers mod_reqtimeout
<VirtualHost *:443>
    ServerName example.com

    SSLEngine on
    SSLCertificateFile    /etc/letsencrypt/live/example.com/fullchain.pem
    SSLCertificateKeyFile /etc/letsencrypt/live/example.com/privkey.pem
    SSLProtocol -all +TLSv1.2 +TLSv1.3

    Timeout 30
    LimitRequestBody 2097152

    ProxyPreserveHost On
    RequestHeader set X-Forwarded-Proto "https"
    # X-Forwarded-For 由 mod_proxy 默认添加（ProxyAddHeaders On）
    ProxyPass        / http://127.0.0.1:6789/ connectiontimeout=5 timeout=40
    ProxyPassReverse / http://127.0.0.1:6789/
</VirtualHost>
```

Apache 默认启用 `mod_reqtimeout`（慢头/慢体防护），这是它比"裸转发"更省心的地方。

## HAProxy

```
frontend https
    bind :443 ssl crt /etc/haproxy/certs/example.com.pem
    http-request set-header X-Forwarded-Proto https
    option forwardfor                       # 别忘了它：否则所有客户端看起来都来自 LB
    timeout client 30s                      # 连接级慢连接防线（HAProxy 里这个是必填项）
    default_backend elenvind

backend elenvind
    option httpchk GET /robots.txt
    server app 127.0.0.1:6789 check
    timeout connect 5s
    timeout server  40s
```

## TLS 与安全头注意点

- **不要在代理里重复添加 HSTS**：HSTS 由应用按 `[security].hsts_enabled` 下发
  （且只在请求被判定为 HTTPS 时），代理再加一份会造成"两处开关、行为不一致"。
- `site_url` 必须与实际对外地址、协议一致。
- **外部资源与 CSP**：图片/视频默认允许任意外链，但**样式表只允许同源**。
  把 CSS 放到 CDN 时必须显式放行：

  ```toml
  [static]
  css = "https://cdn.example.com/site.css"

  [security.csp]
  style-src = ["'self'", "https://cdn.example.com"]
  ```

  （守卫只允许"收窄"：裸 `http:` / `https:` / `*` 会被启动校验拒绝。）

## 健康检查

| 用途 | 路径 | 说明 |
|---|---|---|
| 存活（liveness） | `GET /robots.txt` | 极轻、不碰数据库、不需 Cookie |
| 就绪（readiness） | `GET /` | 页面渲染要读数据库（页脚用户数） |

不要用 `/robots.txt` 判断"服务是否健康"：它不碰数据库，库损坏时它照样 200。

## 关于 `Server` 头

应用侧由 Gunicorn 发出 `Server: gunicorn`（**不含版本号**），是信息性头、不是安全边界。
若你希望对外看不到它，可在代理侧隐藏：nginx `proxy_hide_header Server;`、
Caddy `header_up -Server`、Apache `Header unset Server`。

## 迁移到另一个反代

因为应用只要求"TCP 转发 + 连接超时"，换反代的步骤是：

1. 在新代理上照本文写最小配置（同端口、同 `Host`、同超时）；
2. 切换流量；
3. 确认 `site_url` 与 `[server].trusted_proxies` 仍然正确。

应用侧**不需要改任何代码或配置**。

---

## 为什么应用不自己终结 TLS，也不依赖某个 WSGI 服务器

这是本项目的**设计原则**，不是疏漏：

- 应用是**纯 WSGI callable**（`elenvind.wsgi:application`），任何 WSGI 服务器
  （Gunicorn / uWSGI / waitress / hypercorn / mod_wsgi …）都能运行它；
- 应用**不**用 WSGI 服务器的 TLS 能力（例如 Gunicorn 的 `certfile/keyfile/ssl_context`）——
  那会把"站点是否 HTTPS、Cookie 是否 `Secure`、要不要发 HSTS"绑在某一个服务器上；
- 应用**不**自己监听公网端口：对外服务与 TLS 一律交给反向代理。

### 哪些配置/能力属于**应用**（换 WSGI 服务器照样生效）

| 项目 | 说明 |
|---|---|
| `site_url` | scheme（`Secure` / HSTS）与 robots/sitemap 绝对地址的**唯一**裁决者 |
| `[server].trusted_proxies` | 转发头的信任边界（客户端 IP、https 判定） |
| `cookie_prefix`、`session_*`、`max_body_size`、`max_*`、`[security]`、`[static]`、`[params]`、`[logging]`、`locale`、`*_dir`、`database` | 全部由应用读取并执行 |
| 请求 framing / 411 / 413 / 415 / 405、安全响应头、CSRF、会话、限流 | 全部在应用内实现 |

应用的 Secure/HSTS 判定**不依赖** WSGI 服务器报告 `https`：它由 `site_url` 与
受信代理的 `X-Forwarded-Proto` 决定；`wsgi.url_scheme` 只作为一个额外的正向信号
（若服务器自己就报了 `https`，当然更好）。这就是"换 WSGI 服务器不影响安全服务"的落点。

### 哪些配置/行为属于**启动器** `run.py`（Gunicorn 特有，换服务器就不生效）

| 项目 | 说明 |
|---|---|
| `[server].host` / `port` | 只用于拼出监听地址（`--bind` 可覆盖）；换服务器请改用它的监听参数 |
| `[server].workers` | 翻译成 Gunicorn 的 `workers`（`0` = 按物理核心自适应） |
| `[server].timeout` | 翻译成 Gunicorn 的 `timeout`（单条连接占用 worker 的上限） |
| `--bind/--workers/--reload/--preload/--access-log` | Gunicorn 启动参数 |
| `--backup` / `--backup-to` | 运维动作（在线备份），与 WSGI 服务器无关，可直接用 |
| 服务器日志接入 | 把 `gunicorn.*` 的 logger 接到同一批 handler（`elenvind/core/logging_config.py`）。换 WSGI 服务器时，那个服务器自己的日志按它自己的方式输出；`elenvind.*` 的应用日志不受影响 |

用别的 WSGI 服务器时，把 `host/port/workers/timeout` 换成它的等价参数即可，
**其余配置一字不改**。例如（示意，不新增依赖）：

```bash
# 应用是纯 WSGI callable，任何服务器都能指向它
waitress-serve --listen=127.0.0.1:6789 elenvind.wsgi:application
uwsgi --http 127.0.0.1:6789 --module elenvind.wsgi:application
```

唯一需要留意的是：这些键不生效时，**连接级慢连接的兜底能力取决于那个服务器**，
所以反代侧的超时设置依旧必需。
