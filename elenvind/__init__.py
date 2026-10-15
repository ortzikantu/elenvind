"""Elenvind —— 一个安全默认的小型个人网站服务器。

定位：**标准库为绝对核心** + WSGI + 单机 SQLite + Jinja2/Markdown，服务端渲染、
Zero-JS。它不是通用 Web 框架：没有 ORM、依赖注入、事件总线、插件系统，也没有
asyncio 或后台 writer 进程；SQLite 是唯一数据存储与不变量来源（WAL，写事务统一
走 `database.write_tx()`）。

分层与依赖方向（**只有这一个方向**，由 `tests/test_architecture.py` 守卫）：

    app.py / wsgi.py / cli.py     装配与入口：唯一同时认识 Core 与业务模块的地方
                ↓
        core/                    运行时基座：配置 / HTTP 边界 / 路由 / 安全 / 会话 /
                ↓                模板 / Markdown / 内容格式 / 日志
        db/                      持久化基座（唯一 SQLite 边界）：连接 / 锁 / 写事务 /
                ↓                schema 迁移 / 各表读写
        modules/                 业务模块：blog / pages / auth / users / admin /
                                 seo / system

两条铁律：

- Core 不认识 database，也不认识业务模块；database 不认识 core 与 modules。模块可以依赖 core
  与 database，但**模块之间互不 import** —— 需要协作时由装配层显式注入（见 `elenvind/app.py`）。
- 业务模块只管业务，Web 安全与基础设施归 Core：模块不得自行实现会话、CSRF、
  Cookie、密码、安全响应头或请求限制。

包内随发行版分发的还有 `templates/`（模板）、`i18n/`（界面文案）与 `resources/`
（示例配置与 systemd 单元）；站点实例（`config.toml`、文章、数据库、日志）不在这里。
"""

__all__ = ["__version__"]

from .core.version import raw_version

__version__ = raw_version()
