# 配置参考（config.toml）

`config.toml` 是**唯一**的配置来源，只在启动时读取一次：

- 所有键都要通过 `core/config.py:validate_config()`；非法值**直接拒绝启动**
  （不会先跑起来、再在某个页面 500）；
- 路径类配置一律相对**项目根目录**解析，与启动时的 cwd 无关；
- 文件不存在 → 启动失败。模板随包分发（`elenvind/resources/config.example.toml`），
  用 `elenvind init` 生成一份带目录骨架的实例即可；
- TOML 的表（`[table]`）会"吃掉"它之后的所有顶层键，因此**所有表都写在顶层键之后**；
- 未知的键会被忽略（只有已知键参与校验）。

启动时的告警（不阻止启动，但要处理）：

| 日志 | 含义 |
|---|---|
| `Listening on 0.0.0.0 while trusting forwarded headers …` | 端口可被直连，绕过 TLS/HSTS/连接级限速。改回 `127.0.0.1`，或给端口加防火墙 |
| `admin_user_id=N does not match an active user` | 配置的管理员不存在或已注销 → 没人能涂黑评论 |
| `No administrator configured` | `admin_user_id` 缺失、`null` 或非法 → **无管理员** |

启动时**会拒绝**的配置组合（fail-closed）：

- `locale` 不是 en/zh/ja；`title` 为空；
- `site_url` 非 http(s) 或以 `/` 结尾；
- `[server].cookie_prefix = true`，但既没有 https 的 `site_url`、也没有配
  `trusted_proxies`（否则浏览器会丢弃 `__Host-` Cookie，表现为"登录后立刻掉线"）；
- `[security.csp]` 里出现裸 `*` / `http:` / `https:`（只允许收窄，不允许放宽）；
- 任何会进入 HTML 属性的地址含引号、圆括号、尖括号、反斜杠、空白或控制字符。

---

## 顶层：站点与内容

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `locale` | string | `en` | 界面语言，只能是 `en` / `zh` / `ja`（也接受 `zh-CN` 这类带区域后缀的写法）。**不做浏览器自动检测**：语言是站点级决定 |
| `title` | string | 必填 | 站名（非空） |
| `copyright` | string | 回落到 `title` | 页脚版权署名 |
| `keywords` | string / list | `""` | `<meta name="keywords">`，列表会以 `, ` 连接；留空则不输出该标签 |
| `description` | string | `""` | `<meta name="description">`，留空则不输出 |
| `site_url` | string | `""` | **绝对地址的唯一来源**（robots.txt / sitemap.xml）。必须是不带结尾斜杠的 `http(s)://…`；留空则不输出绝对地址。也用于"代理没发 `X-Forwarded-Proto` 时判断是否 HTTPS"。绝不从请求的 Host 头推导 |
| `admin_user_id` | int / `null` | **无默认** | 管理员账号 id：可涂黑任意评论。**缺失或非法 = 无管理员**（启动日志点名），绝不静默回退到 1 |
| `admin_badge` | string | `BIG BOSS` | 管理员昵称旁的徽章文字；空串 = 不显示 |
| `deleted_user_nickname` | string | `Journeyed On` | 已注销用户的评论显示名 |
| `max_length` | int | `1000` | 单条评论最大字符数（前端 `maxlength` 与后端双重限制），范围 1–100000 |
| `max_comment_depth` | int | `32` | 评论最大层级（顶层 = 1）。**硬约束**：写入侧在事务内校验，超层回复被拒，范围 1–10000 |
| `max_comments_per_article` | int | `1000` | 单篇文章评论数量上限（只统计**未涂黑**的评论；涂黑旧评论即可释放名额），范围 1–1000000 |
| `registration_enabled` | bool | `true` | 是否开放公开注册。`false` 时 `GET /register` 直接说明"已关闭"并且**不渲染表单**，登录页也不再显示注册入口 |
| `session_absolute_days` | int | `30` | 绝对过期：会话创建后活够这么多天必须重新登录（token 泄露的风险窗口硬上限）。`0` = 该维度不过期，最大 3650 |
| `session_idle_days` | int | `15` | 滑动过期：闲置这么多天即失效；有效访问会刷新 `last_seen`（按 5 分钟节流）。`0` = 不过期，最大 3650 |
| `max_body_size` | int | `1048576` | 表单请求体上限（字节），超出返回 413。范围 1 KiB–64 MiB。反向代理侧的对应上限应设得**更大**（示例 `client_max_body_size 2m`），这样应用先触发 413 并留下日志 |
| `database` | string | `data/sqlite.db` | SQLite 文件路径（相对**实例根**）。默认落在 `data/` 子目录：systemd 单元只把 `data/`、`logs/`、`backups/` 授予服务账号写权限，代码与配置保持只读。环境变量 `ELENVIND_DB` 优先级更高 |
| `articles_dir` | string | `articles` | 文章目录（`*.md`，TOML front matter） |
| `custom_pages_dir` | string | `custom` | 自定义页面目录（`*.md`，文件名即路由） |
| `templates_dir` | string | 包内 `elenvind/templates` | Jinja2 模板目录，**一般不用写**（默认就是包内那份，随包升级）。写了就按实例根解析，用于整套替换模板。无论哪种情况，启动时都会**逐个编译**所有模板，语法错误直接导致启动失败 |

> `[server]` 里既有**应用自己的**键（`trusted_proxies`、`cookie_prefix` —— 换 WSGI
> 服务器照样生效），也有**启动器 `run.py`/Gunicorn 的**键（`host`/`port`/`workers`/
> `timeout` —— 换服务器时换成等价参数即可）。完整区分见
> [`HOSTING.md`](HOSTING.md#为什么应用不自己终结-tls也不依赖某个-wsgi-服务器)。

## `[server]`

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `host` | string | `127.0.0.1` | 监听地址。**保持回环**，公网服务由反向代理回源；`elenvind serve --bind`（或兼容入口 `python run.py --bind`）可临时覆盖 |
| `port` | int | `6789` | 监听端口（1–65535） |
| `workers` | int | `0` | Gunicorn worker 进程数。**`0` = 按物理核心自适应（超线程不加倍，上限 8）**；正数则完全按你写的来（1–64）；`--workers N` 优先级最高。只有启动器（`elenvind serve` / `run.py`）会读它：直接 `gunicorn elenvind.wsgi:application` 时由 Gunicorn 自己的 `-w` 决定。Waitress 下它表示线程数 |
| `timeout` | int | `30` | **启动器级**（不影响应用的安全判定）：Gunicorn worker 超时（秒，5–120），单条连接占用一个 worker 超过这个时间就由 master 杀掉重启。连接级慢连接由反向代理承担；换用别的 WSGI 服务器时这个键不生效 |
| `trusted_proxies` | list | `["127.0.0.1", "::1"]` | 受信代理（**唯一来源**）：仅当直连对端在此列表中时，才采信 `X-Forwarded-For`（客户端 IP）与 `X-Forwarded-Proto`（https 判定）。多级代理请把每一跳都写进来。**用最简 TCP 转发器（socat / stunnel / DNAT / `mode tcp`）时请设成 `[]`** —— 它们不覆写请求头，受信等于让客户端自己申报 IP 与 scheme（详见 `manual/SECURITY.md`） |
| `cookie_prefix` | bool | `false` | 给会话/CSRF Cookie 加 `__Host-` 前缀（要求全程 HTTPS + `Path=/` + 无 Domain）。开启时**必须**让应用有办法知道自己是 HTTPS：https 的 `site_url`，或非空的 `trusted_proxies` |

## `[pagination]`

| 键 | 默认 | 说明 |
|---|---|---|
| `per_page` | `6` | 首页每页文章数（1–1000） |

## 评论与登录限流

`[comment_limits]`

| 键 | 默认 | 说明 |
|---|---|---|
| `max_per_user` | `5` | 窗口内单用户最多评论数 |
| `max_per_ip` | `10` | 窗口内单 IP 最多评论数 |
| `window_seconds` | `60` | 窗口长度（秒） |

`[login_limits]`：三闸门 + 渐进冷却（第 `limit` 次失败起冷却 `base=60s`，
此后每多一次翻倍，上限 24h）。

| 键 | 默认 | 说明 |
|---|---|---|
| `max_email_failures` | `5` | 单邮箱失败上限（主防线，防定向爆破） |
| `email_window_seconds` | `86400` | 单邮箱统计窗口 |
| `max_ip_failures` | `20` | 单 IP 失败上限（辅助防线） |
| `ip_window_seconds` | `900` | 单 IP 统计窗口 |
| `max_global_failures` | `200` | 全站失败上限（分布式爆破的最后闸门） |
| `global_window_seconds` | `900` | 全站统计窗口 |

> **闸门只约束错误凭据**：闸门命中时请求仍会走密码校验，密码正确即放行并清空该
> 邮箱的失败流水（等于当场自解封）；密码错误才返回冷却提示，且**被闸门拦下的
> 尝试不再记账**（下一次放行的尝试会正常记账，冷却照旧逐次加深）。
> 因此不存在"被限流锁死、正确密码也进不来"的状态。回归测试见
> `tests/test_auth_lockout.py`。

`[register_limits]`

| 键 | 默认 | 说明 |
|---|---|---|
| `max_per_ip` | `5` | 窗口内单 IP 最多注册尝试次数 |
| `window_seconds` | `3600` | 窗口长度（秒） |

## `[static]`：**你自己托管**的资源地址

本服务**不发出任何静态文件**（没有内置样式表、没有缺省图标）。这四个键是管理员
提供的 URL；留空就不输出对应的 HTML 节点。

| 键 | 默认 | 渲染成 | 说明 |
|---|---|---|---|
| `css` | `""` | `<link rel="stylesheet">` | 全站样式表。站内绝对路径或 CDN 绝对 URL。**跨源时必须同时把它加进 `[security.csp] style-src`**，否则会被自己的 CSP 拦掉 |
| `favicon` | `""` | `<link rel="icon">` | 站点图标 |
| `logo` | `""` | 页头站标 `<img>` | 留空则回退到 `favicon`，再留空则只显示站名 |
| `hero` | `""` | `<img class="hero">` | 首页 Hero 大图；留空则整块 hero 不渲染 |

外观基线：`manual/style.example.css` 是一份可直接使用的样式表（同时也是模板类名
漂移守卫的检查对象）。把它放到你自己的服务器/CDN，再把地址填进 `css`。

地址校验（**注入防护，不是信任问题**）：站内绝对路径或 `http(s)` 绝对 URL；
禁止引号、圆括号、尖括号、反斜杠、空白与控制字符；禁止协议相对地址（`//host/…`）。
本服务不下载、不校验、不缓存这些资源——**其可用性与安全性由管理员负责**。

## `[params]`

页头/页脚与首页的展示内容。`url` / `icon` 与 `[static]` 走同一套 URL 校验，
社交链接额外允许 `mailto:`。

```toml
[params]
intro = "首页一句话介绍"

[[params.nav]]           # 页脚导航
name = "About"
url = "/about"

[[params.social]]        # 首页社交链接（icon 留空则显示名称文字）
name = "Codeberg"
url = "https://codeberg.org/"
icon = "https://cdn.example.com/codeberg.svg"

[[params.projects]]      # 首页项目列表
name = "Elenvind"
url = "https://codeberg.org/ortzikantu/elenvind"
description = "一句话说明"
```

## `[logging]`

| 键 | 默认 | 说明 |
|---|---|---|
| `level` | `info` | `debug` / `info` / `warning` / `error` / `critical` |
| `file` | `logs/app.log` | 日志文件（相对项目根目录） |
| `rotate` | `false` | `false`（推荐）：只写当前文件，由外部 `logrotate` 轮转（多 worker 下唯一安全的做法，见 `manual/logrotate.conf`）。`true`：应用内轮转，仅在**单 worker**或本地开发时使用 |
| `max_bytes` | `10485760` | 仅 `rotate = true` 时生效 |
| `backup_count` | `5` | 仅 `rotate = true` 时生效 |

## `[security]`

安全响应头由 Core 在响应收尾时统一注入（含 400/403/404/405/413/500），模块层不感知。

| 键 | 默认 | 说明 |
|---|---|---|
| `csp_enabled` | `true` | `false` = 完全不下发 CSP（不推荐） |
| `permissions_policy` | 内置全关 | 留空字符串 = 不下发该头；删掉整行 = 用内置默认（关得更全） |
| `hsts_enabled` | 跟随 `cookie_prefix` | **默认不下发**；只有 `cookie_prefix = true` 时才默认启用（`__Host-` 本身就要求全程 HTTPS）。显式写 `true` 即启用。只有 HTTPS 请求才会真的带上 |
| `hsts_max_age` | `31536000` | 秒（0–63072000）。首次上线建议先用 300 确认无误 |
| `hsts_include_subdomains` | `false` | 子域全部支持 HTTPS 后再开 |

### `[security.csp]`

只写要改的指令，其余用内置默认：

| 默认指令 | 值 |
|---|---|
| `default-src` | `'self'` |
| `script-src` | `'none'`（本站 Zero-JS） |
| `style-src` | `'self'`（模板里没有任何内联样式，因此**不需要** `'unsafe-inline'`；跨源样式表要显式加进来） |
| `img-src` | `'self' data: http: https:` |
| `media-src` | `'self' http: https:` |
| `font-src` | `'self'` |
| `connect-src` | `'self'` |
| `object-src` | `'none'` |
| `base-uri` | `'none'` |
| `form-action` | `'self'` |
| `frame-ancestors` | `'none'` |

**只允许收窄**：显式写裸 `*`、`http:`、`https:` 会被启动校验拒绝（那等于把"任意
第三方"重新放开）。要放行外部资源就写明确的主机：

```toml
[security.csp]
style-src = ["'self'", "https://cdn.example.com"]
img-src = ["'self'", "data:", "https://cdn.example.com"]
# 字符串形式也行：media-src = "'self' https://videos.example.com"
# 整条关掉：object-src = false
```
