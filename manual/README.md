# Elenvind 运维手册

本目录是**交付物**的一部分：部署、配置、备份、升级与排障都写在这里，
README 只保留项目概览与边界。

## 读哪一篇

| 我要做什么 | 看这篇 |
|---|---|
| 让站点对外可用（TLS、反向代理、换反代） | [HOSTING.md](HOSTING.md) |
| 改配置项、搞清每个键的语义与默认值 | [CONFIGURATION.md](CONFIGURATION.md) |
| 装 systemd、轮转日志、备份恢复、升级回滚、排障 | [OPERATIONS.md](OPERATIONS.md) |
| 用命令行：建站 / 启动 / 备份 / 体检 | [CLI.md](CLI.md) |
| 发一个新版本（改版本号、打 tag、PyPI 发布流水线） | [RELEASE.md](RELEASE.md) |
| 看安全模型、信任边界、各反代的差异与已知限制 | [SECURITY.md](SECURITY.md) |

## 本目录的文件

| 文件 | 用途 |
|---|---|
| — | 配置模板已移到包内 `elenvind/resources/config.example.toml`（用 `elenvind init` 生成实例） |
| `nginx.conf.example` | nginx 最小反代示例（Caddy / Apache / HAProxy 见 HOSTING.md） |
| `elenvind.service` | systemd 单元（只监听回环；TLS 由前面的反代终结） |
| `logrotate.conf` | 日志轮转示例（应用默认不做应用内轮转） |
| `style.example.css` | 样式表样板：**由你自己托管**，本服务不发出静态文件 |
| `../.github/workflows/` | CI（测试矩阵）与发布流水线（PyPI Trusted Publishing + GitHub Release） |

## 两条必须先理解的事实

1. **本服务不发出任何静态文件。** 输出只有 HTML、`robots.txt`、`sitemap.xml`。
   样式表、图片、视频、图标由管理员自己托管（CDN / 图床 / 自建 Web 服务器）；
   地址写进 `[static]`，留空就不渲染对应节点。外部资源的可用性与链接安全
   由管理员负责。

2. **反向代理只做反向代理，而且必须是它做。** 它负责 TLS、http→https 跳转与
   连接级慢连接；认证、会话、CSRF、XSS、注入、限流、请求体上限、安全响应头
   全部由本服务自己提供。因此换掉 nginx 用 Caddy / Apache / HAProxy 都行，
   **应用侧不需要改任何配置**。

3. **应用不绑定任何 WSGI 服务器。** 它不自己终结 TLS（不用 Gunicorn 的
   `certfile/ssl_context` 之类），Secure/HSTS 的判定只依赖 `site_url` 与受信代理头；
   `host/port/workers/timeout` 属于启动器，换服务器时换成等价参数即可。
   哪些配置属于应用、哪些属于启动器，见 [HOSTING.md](HOSTING.md) 末节。
