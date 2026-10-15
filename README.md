# Elenvind

一个只用 **Python 标准库 + WSGI + SQLite + Jinja2 + Markdown** 构建的个人网站/博客服务。

```bash
pip install "elenvind[gunicorn]"
elenvind init --dir /srv/elenvind --domain example.com
elenvind serve                 # 自动检测并选择 WSGI 服务器
elenvind proxy configure --domain example.com --apply   # 443 + 80→443
```

命令行的完整用法见 [`manual/CLI.md`](manual/CLI.md)。

## 边界（先读这一节）

**本服务只输出 HTML、`robots.txt` 与 `sitemap.xml`。它不发出任何静态文件。**
样式表、图片、视频、图标一律由管理员自己托管（CDN、图床、对象存储、或你自己的
Web 服务器），把地址写进 `config.toml` 的 `[static]` 即可；留空就不输出对应的
HTML 节点。**链路的可用性与安全性由管理员自己负责**（本服务不下载、不校验、
不缓存这些资源）。

由此得到两条清晰的职责线：

| 关注点 | 由谁负责 |
|---|---|
| 认证 / 会话 / CSRF / XSS / 注入 / 路径穿越 / 限流 / 请求体上限 / 安全响应头 / 备份 | **本服务**（见下文"安全模型"） |
| TLS 终结、连接级慢连接（slowloris）与带宽、网络 ACL、证书续期 | **任意反向代理**（nginx / Caddy / Apache / HAProxy / …） |
| 样式表、图片、视频、图标的内容与可用性 | **管理员**（自己托管） |

**必须有反向代理，但不依赖任何特定反代。** 反向代理**只需要做反向代理**：转发 TCP、
保留 `Host`、设置连接超时（这三件事任何反代都有）。它不需要理解本服务的任何业务语义，
也不承担任何应用层防护。

应用**不**自己终结 TLS、**不**监听公网端口、也**不使用任何 WSGI 服务器特有的 TLS 能力**
（例如 Gunicorn 的 `certfile/ssl_context`）—— 因此**换掉反代**或**换掉 WSGI 服务器**
都不会改变站点的运行方式与安全语义（完整的边界划分见
[`manual/HOSTING.md`](manual/HOSTING.md#为什么应用不自己终结-tls也不依赖某个-wsgi-服务器)）。

## 设计约束（项目的"宪法"）

- **标准库为绝对核心**，核心运行时依赖只有 3 个：`Jinja2`、`Markdown`、`MarkupSafe`；
  **WSGI 服务器不在其中**（`pip install elenvind[gunicorn|waitress|uwsgi|hypercorn]`）——
  换服务器只该换一个包，不该改代码或改变安全语义；
- 单体、单机、单库（SQLite），多 worker 通过 `flock` + `BEGIN IMMEDIATE` 串行化写事务；
- 不引入中间件框架 / ORM / 前端构建链：Zero-JS，服务端渲染；
- 分层单向依赖：`app（装配）→ modules（业务）→ database（持久化）`、`core（运行时基座）`，
  依赖方向由 `tests/test_architecture.py` 强制；
- 不做企业级功能：没有搜索集群、消息队列、多租户、插件系统、指标管道。

```
elenvind/                    随包分发（pip install elenvind）
  app.py        装配点（唯一同时认识 core / database / modules 的地方）
  wsgi.py       WSGI 入口：elenvind.wsgi:application
  cli.py        命令行：init / serve / backup / doctor / proxy
  launcher.py   探测并选择 WSGI 服务器（Gunicorn / Waitress / uWSGI / Hypercorn / wsgiref）
  reverse_proxy.py  探测反代并生成它的站点配置（默认 dry-run）
  scaffold.py   `init` 的骨架生成
  core/         运行时基座：HTTP 边界、路由、会话、CSRF、安全头、Markdown、模板
  db/           持久化基座：唯一允许 import sqlite3 与执行 SQL 的地方
  modules/      业务模块：auth / users / admin / blog / pages / seo / system
  templates/    Jinja2 模板
  i18n/         界面文案（en / zh / ja）—— 属于代码，随包分发
  resources/    示例配置与 systemd 单元（init / install 用）

articles/       文章（TOML front matter + Markdown）—— 站点实例
custom/         自定义页面（纯 Markdown，文件名即路由）—— 站点实例
data/           运行时数据（sqlite.db 及 -wal/-shm）—— 不进版本库
logs/           日志 —— 不进版本库
backups/        `elenvind backup` 的默认输出目录 —— 不进版本库

manual/         运维手册：命令行、托管、配置、备份/恢复、排障、样例文件
tests/          标准库 unittest 测试（含架构、边界与打包守卫）
run.py          兼容入口：把旧的 `python run.py` 参数翻译成 `elenvind` 子命令
```

## 快速开始

需要 **Python ≥ 3.11**（标准库 `tomllib`）与 **Linux**（或同样提供 `fcntl.flock` 的
POSIX 系统）：跨进程写互斥、迁移串行化与限流记账的原子性都建立在 `flock` 上。平台不
提供 `fcntl` 时服务在启动阶段直接拒绝，不会退化成"进程内假锁"。

### 从 PyPI 安装（推荐）

```bash
python3 -m venv /srv/elenvind/.venv
/srv/elenvind/.venv/bin/pip install "elenvind[gunicorn]"

cd /srv/elenvind
/srv/elenvind/.venv/bin/elenvind init --domain example.com   # 生成 config.toml 与目录骨架
$EDITOR config.toml                                          # 至少改 admin_user_id 与 [static]
/srv/elenvind/.venv/bin/elenvind doctor                      # 体检：缺什么会直说
/srv/elenvind/.venv/bin/elenvind serve                       # 自动选服务器并启动
sudo /srv/elenvind/.venv/bin/elenvind proxy configure --domain example.com --apply
```

### 仓库与发布

- **主仓库在 Codeberg**：[`codeberg.org/ortzikantu/elenvind`](https://codeberg.org/ortzikantu/elenvind) ——
  提交、Issue、PR 都在这里。
- **GitHub 是镜像 + 构建仓**：[`github.com/ortzikantu/elenvind`](https://github.com/ortzikantu/elenvind) ——
  由 Codeberg 镜像同步（含 tag），并负责跑 CI 与把发行版发布到 PyPI，见
  [`manual/RELEASE.md`](manual/RELEASE.md)。

### 从源码运行（开发/审阅）

```bash
git clone https://codeberg.org/ortzikantu/elenvind && cd elenvind
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp elenvind/resources/config.example.toml config.toml
.venv/bin/elenvind serve                    # 默认 127.0.0.1:6789（只监听回环）
```


首次启动会自动建库（默认 `data/sqlite.db`）、建表、迁移，并在 `logs/app.log`
里记账。**先注册自己的账号**，再把 `admin_user_id` 设成它的 id；
`admin_user_id` 缺失或指向不存在的用户时，服务**没有管理员**（启动日志会点名），
此时没人能涂黑评论。

常用启动参数：

```bash
elenvind serve --workers 4        # 覆盖进程/线程数
elenvind serve --server waitress  # 换一个 WSGI 服务器（已装的会被自动探测）
elenvind serve --preload          # master 里导入应用（startup 只跑一次）
elenvind serve --reload           # 开发：代码改动自动重载
elenvind serve --access-log       # 打开访问日志
elenvind doctor                   # 环境体检
elenvind backup                   # 在线备份数据库
```

**worker 数默认自适应**：`[server].workers = 0` 表示按**物理核心**（超线程不加倍）
决定，上限 8；写正数则完全按你写的来；`--workers N` 优先级最高。写正数不会有
收益的下限是"超过物理核心"——写事务本来就被 `flock` 串行化。

## 安全模型（一页速览）

- **会话**：32 字节随机 token 存服务端表，Cookie `HttpOnly` + `SameSite=Lax` + （HTTPS 时）`Secure`，
  可选 `__Host-` 前缀；登录轮换（防会话固定），改密/删号清空全部会话；绝对 30 天 + 滑动 15 天双过期。
- **CSRF**：双提交 Cookie，调度器对所有非安全方法**默认**校验，模块不写一行 CSRF 代码。
- **XSS**：Jinja 自动转义 + Markdown 白名单净化（裸 HTML 转义、协议白名单、危险标签连内容丢弃）；
  模板里**没有**内联样式与脚本，因此 CSP 是 `script-src 'none'` + `style-src 'self'`。
- **注入**：所有 SQL 参数化；动态标识符只来自代码内白名单。
- **路径穿越**：不再有文件服务面；文章/页面只按文件名查表，URL 从不参与路径拼接。
- **开放重定向**：回跳地址只接受站内路径（拒绝 `//`、反斜杠、TAB/控制字符）。
- **代理信任**：`X-Forwarded-For` / `X-Forwarded-Proto` 只在直连对端属于
  `[server].trusted_proxies` 时采信，且取最右非受信跳；代理没发 `X-Forwarded-Proto`
  时，用 `site_url` 声明的协议兜底（非受信对端不享受这条兜底）。
- **Host 头**：绝对地址只来自 `site_url` 配置，绝不从请求推导。
- **限流**：登录（邮箱 / IP / 全站三闸门 + 渐进冷却）、注册（只读预检 + 原子判定）、
  评论（用户 / IP / 单篇配额）。**闸门只约束错误凭据**：正确密码永远不会被限流挡住；
  被闸门拦下的尝试既不写库、也不渲染整页。
- **请求边界**：表单方法必须声明 `Content-Length`（411），请求体按 `max_body_size` 先判后读（413），
  Content-Type 白名单（415），方法不允许带 `Allow`（405），截断的请求体 400。
- **安全响应头**：每个响应（含 400/403/404/405/413/500）都经过同一出口注入，
  模块无法漏掉，也无法伪造 `Set-Cookie`。
- **备份**：`elenvind backup` 走 SQLite 在线备份 API、边写边以 `0600` 落盘、
  拷完 `quick_check`、拒绝覆盖已有文件。
- **日志**：访问日志不记查询串、请求体、Cookie 与凭据，非打印字符一律转义。

**TLS 与 HSTS**：TLS 一律由反向代理终结（应用只监听回环，且不使用任何 WSGI 服务器
特有的 TLS 能力）。`site_url` 是 scheme 的唯一裁决者，因此"代理少发一个头"或
"客户端伪造 `X-Forwarded-Proto`"都不会让 `Secure`/HSTS 静默失效。

**不在本服务职责内的**：TLS 与证书、连接级慢连接与带宽、http→https 跳转、
网络层 ACL/地理封锁、外部资源的可用性 —— 由反代与管理员负责
（见 [`manual/HOSTING.md`](manual/HOSTING.md)）。

## 测试与静态检查

```bash
.venv/bin/python -m unittest discover -s tests -t . -v     # 全部
.venv/bin/python -m unittest tests.test_boundaries -v       # 单个模块

.venv/bin/pip install ruff        # 开发工具，不是运行时依赖
.venv/bin/ruff check .
```

测试只用标准库 `unittest`，**不会碰仓库里的 `config.toml` / `data/sqlite.db` / `logs/`**
（配置由夹具注入，数据库与日志落在临时目录）。守卫覆盖：依赖方向与分层、
Markdown 净化、CSRF、回跳地址、请求 framing、安全头、评论与限流、登录闸门、
并发（线程级）、备份/恢复、日志与代理信任、i18n 三语一致、模板类名漂移、
**"不发出静态文件"这条边界**、以及 worker 数解析与配置校验。

## 部署与运维

- 托管与反向代理（**多反代等价写法**、TLS、慢连接、CSP 与外部资源）：
  [`manual/HOSTING.md`](manual/HOSTING.md)
- 运行、systemd、日志轮转、备份/恢复、升级与回滚、排障：
  [`manual/OPERATIONS.md`](manual/OPERATIONS.md)
- 发布流程（版本号、tag、PyPI Trusted Publishing、失败对照）：
  [`manual/RELEASE.md`](manual/RELEASE.md)
- 命令行（init / serve / backup / doctor / proxy、退出码、非交互用法）：
  [`manual/CLI.md`](manual/CLI.md)
- 全部配置项与语义：[`manual/CONFIGURATION.md`](manual/CONFIGURATION.md)
- 手册索引：[`manual/README.md`](manual/README.md)
- 安全模型、信任边界、反代差异矩阵与已知限制：[`manual/SECURITY.md`](manual/SECURITY.md)

## 许可

见 [LICENSE](LICENSE)（GPL-3.0）。
