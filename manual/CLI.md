# 命令行（CLI）

装完包就有一个 `elenvind` 命令。它管四件事：**建站**、**选服务器并启动**、
**备份**、**体检**，以及**生成反向代理配置**。

## 安装

```bash
pip install elenvind[gunicorn]        # 核心 + Gunicorn（最常用的组合）
pip install elenvind[waitress]        # 或者换一个服务器
pip install elenvind                 # 只装核心，服务器由你自己指定
```

**WSGI 服务器不在核心依赖里**：换服务器只该换一个包，不该改代码或配置语义。
可用的 extra 见 [选一个 WSGI 服务器](#选一个-wsgi-服务器)。

## 五分钟走完

```bash
elenvind init --dir /srv/elenvind --domain example.com   # 1. 建站骨架
$EDITOR /srv/elenvind/config.toml                        # 2. 至少改 admin_user_id 与 [static]
cd /srv/elenvind && elenvind doctor                      # 3. 体检：缺什么它会直说
elenvind serve                                           # 4. 启动（自动选服务器）
```

第 5 步（对外服务）不在命令行里：反向代理与 TLS 证书按 [`HOSTING.md`](HOSTING.md) 手写，
写完自己 `nginx -t` 校验并 reload。**本工具不修改任何系统配置。**

## 站点实例根目录

配置、文章、数据库、日志都相对**实例根目录**解析。它的确定顺序是：

1. 环境变量 `ELENVIND_ROOT`（显式指定，优先级最高）；
2. 从当前目录**向上**找到的第一个含 `config.toml` 的目录（像 git 找 `.git`）；
3. 当前目录。

所以在站点目录的任意子目录里都能直接敲 `elenvind serve`。用 systemd 时把
`WorkingDirectory` 设成站点目录即可。

> 为什么不用"包所在目录"：本包可以从 PyPI 装进 `site-packages/`，而站点数据属于你，
> 绝不能落到那里。

## 选一个 WSGI 服务器

`elenvind serve` 会探测当前环境装了哪些服务器，**只有一个就直接用，有多个才问你**，
`--server NAME` 可以跳过询问，`--yes` 则一律采用推荐项（脚本与非交互环境用）。

| 服务器 | 安装 | 适合什么 |
|---|---|---|
| **Gunicorn**（默认优先级最高） | `pip install elenvind[gunicorn]` | 多进程 + 连接超时 + 转发头白名单；Linux 上的常规选择 |
| Waitress | `pip install elenvind[waitress]` | 纯 Python、无需编译；线程模型，不想装 C 扩展时最省心 |
| uWSGI | `pip install elenvind[uwsgi]` | 功能最全、性能最好；需要编译 |
| Hypercorn | `pip install elenvind[hypercorn]` | 需要 HTTP/2 时 |
| 标准库 wsgiref | 无需安装 | **仅供本机验证**：单进程单线程、无连接超时 |

选择可以写进配置，避免每次靠探测：

```toml
[server]
wsgi_server = "gunicorn"   # 不写则按可用项的优先级自动选择
host = "127.0.0.1"         # 应用只监听回环，对外由反向代理提供
port = 6789
workers = 0                # 0 = 按物理核心自适应（上限 8）
timeout = 30               # 启动器级的 worker 超时（秒）
```

`workers` 的含义随服务器而定：Gunicorn/uWSGI 是**进程数**，Waitress 是**线程数**
（这些差异属于启动器，不影响应用的安全语义）。

想完全不用 CLI（例如交给别的进程管理器）：

```bash
gunicorn --workers 4 --bind 127.0.0.1:6789 elenvind.wsgi:application
waitress-serve --listen=127.0.0.1:6789 elenvind.wsgi:application
```

`elenvind.wsgi:application` 是标准 WSGI callable，任何 WSGI 服务器都能跑。

## 子命令

### `elenvind init`

生成站点骨架：`config.toml`（含注释的完整示例）、`articles/`、`custom/`、`data/`、
`logs/`、`backups/`、`.gitignore`。**已存在的文件一律不动**，除非加 `--force`。

| 选项 | 说明 |
|---|---|
| `--dir PATH` | 实例目录（默认当前目录） |
| `--domain DOMAIN` | 写进 `site_url`（`https://<域名>`） |
| `--title TEXT` | 站名 |
| `--force` | 覆盖已存在的 `config.toml` / `.gitignore` |

### `elenvind serve`

| 选项 | 说明 |
|---|---|
| `--server NAME` | 指定服务器，跳过询问 |
| `--bind HOST:PORT` | 监听地址（默认取 `config.toml`） |
| `--workers N` | 进程/线程数（`0` = 自适应） |
| `--reload` | 代码改动自动重载（开发用，会明显变慢） |
| `--preload` | 在 master 进程导入应用（仅 Gunicorn） |
| `--access-log` | 打开访问日志（默认关闭） |
| `-y, --yes` | 不提问 |

启动前会**整体校验配置**：不合法就直接退出，不会起一个"看起来在跑、每个请求都报错"的进程。

### `elenvind backup`

在线备份数据库（SQLite backup API，不是 `cp`）。默认写到 `backups/elenvind-<时间戳>.db`，
文件以 `0600` 创建。

| 选项 | 说明 |
|---|---|
| `--to PATH` | 指定备份文件路径 |

恢复：停服务 → 移走当前 `sqlite.db`（连同 `-wal` / `-shm`）→ 放入备份文件。

### `elenvind doctor`

体检：Python 与版本、实例根、配置是否可用、数据库与三个可写目录、可用的 WSGI 服务器、
检测到的反向代理，并检查两个最容易配错的东西 —— `[server].host` 是不是回环地址、
`site_url` 是不是 `https://…`。发现问题返回退出码 1。

## 反向代理与证书：不在本工具的职责内

本命令行**不修改任何系统配置**。反向代理的站点配置、TLS 证书的申请与续期都由你
（或你的配置管理工具）完成，写法与注意事项见 [`HOSTING.md`](HOSTING.md)：

- nginx / Caddy / Apache / HAProxy 四种最小可用配置（转发 + 80→443 + 连接超时）；
- 证书获取（certbot / acme.sh / 自签仅供测试）、私钥权限、续期之后如何重载代理；
- 三件**必须由代理承担**的事：连接级慢连接、http→https 跳转、请求体上限不小于应用的
  `max_body_size`。

`elenvind doctor` 只做只读体检：它会告诉你这台机器上装了哪个反代、配置通常放在哪个
目录，并检查 `[server].host` 是否回环、`site_url` 是否 `https://…`。它不会写文件、
不 reload 服务。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 环境或配置问题（缺配置文件、没有可用的 WSGI 服务器、目录不可写、体检未通过） |
| 2 | 用法错误（未知子命令或选项、缺参数、参数取值不合法） |
| 130 | 被 Ctrl-C 中断 |

## 与 `python run.py` 的关系

`python run.py <旧旗标>` 仍然可用，它只做一层参数翻译后交给 CLI：

| 旧写法 | 等价的新写法 |
|---|---|
| `python run.py` | `elenvind serve` |
| `python run.py --workers 4 --bind 127.0.0.1:6789` | `elenvind serve --workers 4 --bind 127.0.0.1:6789` |
| `python run.py --reload --access-log` | `elenvind serve --reload --access-log` |
| `python run.py --backup` | `elenvind backup` |
| `python run.py --backup-to /tmp/x.db` | `elenvind backup --to /tmp/x.db` |

新代码请直接用 `elenvind`。

## 常见问题

| 现象 | 原因与处理 |
|---|---|
| `没有检测到任何 WSGI 服务器` | `pip install elenvind[gunicorn]`（或 `[waitress]` 等） |
| `没有找到配置文件` | 在站点目录里运行，或设 `ELENVIND_ROOT=/srv/elenvind`，或先 `elenvind init` |
| 反代不生效 / 证书报错 | 这不是本工具管的：按 [`HOSTING.md`](HOSTING.md) 检查配置，`nginx -t` 校验后 `systemctl reload` |
| 端口被占用 | `elenvind doctor` 会指出监听地址；换端口或停掉占用者 |
