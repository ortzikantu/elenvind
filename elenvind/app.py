"""应用装配点：把 Core、database 与各业务模块装成一个可运行的站点。

这是**唯一**允许同时认识 `core`、`database` 与业务模块的地方。依赖方向（由
`tests/test_architecture.py` 守卫）：

    app.py / wsgi.py / cli.py      装配与入口
        │
        ├──► core      （core ↛ database、core ↛ modules）
        ├──► database        （database ↛ core、database ↛ modules）
        └──► modules   （modules ↛ modules、modules ↛ app）

路由注册顺序**就是优先级**，改顺序会改变谁能命中：

1. auth / users / admin（固定路径）；
2. blog（`/`、`/article/<slug>` 与评论写操作）；
3. seo（`/robots.txt`、`/sitemap.xml`，文章条目由这里注入）；
4. pages（`/theme` 与 `/<slug>` 兜底）——**必须最后**，否则兜底会遮蔽固定路径。

本服务**不发出任何静态文件**：样式表、图片、视频一律由管理员自己托管
（CDN / 图床 / 自建 Web 服务器），地址写进 `config.toml` 的 `[static]`。
"""
from __future__ import annotations

import logging
import os
from functools import partial
from pathlib import Path

from . import database
from .core.app import App
from .core.context import set_user_count_provider
from .core.session import load_user
from .modules.admin import routes as admin_routes
from .modules.auth import routes as auth_routes
from .modules.blog import logic as blog_logic
from .modules.blog import routes as blog_routes
from .modules.pages import logic as pages_logic
from .modules.pages import routes as pages_routes
from .modules.seo import routes as seo_routes
from .modules.system import routes as system_routes
from .modules.users import routes as users_routes

logger = logging.getLogger(__name__)

#: 启动钩子：数据库与模板就绪后按序执行（模块的内容缓存预热）
STARTUP_HOOKS = (
    ("Articles loaded", blog_logic.load_articles),
    ("Custom pages loaded", pages_logic.load_pages),
)


def resolve_db_path() -> Path:
    """数据库文件路径（**装配层职责**）。

    优先级：`ELENVIND_DB` 环境变量 > `config.toml` 的 `database` > `<实例根>/data/sqlite.db`；
    相对路径一律相对实例根解析。

    兜底刻意落在 `data/` 下：systemd 单元只把 `data/`、`logs/`、`backups/` 授予服务账号
    写权限，落在实例根会直接写失败。配置语义留在这一层，database 只接受解析好的路径
    （`database.configure()`），因此持久化基座不必认识配置。
    """
    from .core.config import ROOT, config, resolve_path

    override = os.environ.get("ELENVIND_DB")
    if override:
        return Path(override)
    configured = config.get("database")
    if isinstance(configured, str) and configured.strip():
        return resolve_path(configured, ROOT / "data" / "sqlite.db")
    return ROOT / "data" / "sqlite.db"


def prepare_database() -> None:
    """数据库启动步骤：绑定路径与会话窗口 → 建表迁移 → 清理过期数据 → 记录库信息。

    `core.lifespan.startup(prepare_database=…)` 只决定"何时执行"；"怎么初始化数据库"
    属于装配层与 database 层。
    """
    from .core.config import config

    warn_on_exposed_bind()
    database.configure(resolve_db_path())
    # 会话窗口实时从 config 取：database 不认识 config，只接受这个 callable
    database.session.set_limits_provider(lambda: (
        config.get("session_absolute_days", database.DEFAULT_ABSOLUTE_DAYS),
        config.get("session_idle_days", database.DEFAULT_IDLE_DAYS),
    ))
    database.init_db()
    warn_about_missing_admin()

    removed_sessions = database.cleanup_expired_sessions()
    database.cleanup_old_login_attempts(days=30)
    database.cleanup_old_comment_attempts(days=7)
    database.cleanup_old_attempts(days=7)

    logger.info("Database: %s (schema v%s, journal_mode=%s, write lock %s)",
                database.db_path(), database.SCHEMA_VERSION, database.journal_mode(),
                database.lock_path_for(database.db_path()))
    logger.info("Expired sessions and rate-limit history cleaned "
                "(%s session(s) removed)", removed_sessions)


#: 回环地址：只有监听这些地址时，"应用端口不可从公网直连"才成立
_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def warn_on_exposed_bind() -> None:
    """启动告警：**实际**监听地址不是回环。

    本服务的形态是"只监听回环 + 由反向代理终结 TLS"：应用不自己提供对外服务，也不使用
    任何 WSGI 服务器特有的 TLS 能力。因此把端口暴露到回环之外就是在绕过整个安全边界 ——
    能连到它的人跳过了 TLS/HSTS/边缘限速，也跳过了连接级慢连接防护。这里只告警、不拒绝
    启动：这是部署配置问题，不是代码缺陷。

    host 取**实际 bind**（启动器通过 `config.set_effective_bind()` 注入），只有在直接
    `gunicorn elenvind.wsgi:application` 时才回落到配置值 —— 只读配置会在 `--bind`
    覆盖时漏报或误报。
    """
    from .core.config import config, effective_bind_host

    server = config.get("server") or {}
    host = effective_bind_host() or str(server.get("host", "127.0.0.1") or "").strip()
    trusted = server.get("trusted_proxies") or []
    if host in _LOOPBACK_HOSTS:
        return
    if trusted:
        logger.warning(
            "Listening on %s (not loopback) while trusting forwarded headers from %s: "
            "clients that can reach this port bypass the reverse proxy "
            "(TLS/HSTS/edge limits). Bind to 127.0.0.1 and let the proxy forward, "
            "or firewall the port.", host, trusted)
        return
    logger.warning(
        "Listening on %s (not loopback) and trusting no proxy: this port serves "
        "PLAINTEXT traffic with no reverse proxy in front. Bind to 127.0.0.1 and put "
        "a reverse proxy in front of it (see manual/HOSTING.md).", host)


def warn_about_missing_admin() -> None:
    """启动告警：`admin_user_id` 缺失、为 null 或指向不存在的用户。

    管理员身份的唯一来源是配置里那个 id，所以配置写错（或该账号后来注销）的结果不是
    报错，而是**静默地没有管理员** —— 评论区被刷屏时没人能涂黑。启动日志里点名，
    避免"以为自己是管理员"（新库还没注册时尤其容易踩）。
    """
    from .core.security import admin_id

    owner = admin_id()
    if owner is None:
        logger.warning("No administrator configured (admin_user_id missing, null, "
                       "or invalid): nobody can moderate comments")
        return
    if database.get_user_by_id(owner) is None:
        logger.warning("admin_user_id=%s does not match an active user: nobody can "
                       "moderate comments. Register that account, or point "
                       "admin_user_id at an existing user id.", owner)


def create_app() -> App:
    """构造应用：Core App + 错误页 + 会话加载 + 各模块路由。"""
    # Core 不认识 database：用户总数（页脚用）由这里注入一个数据源
    set_user_count_provider(database.get_user_number)

    app = App(
        not_found=system_routes.not_found,
        forbidden=system_routes.forbidden,
        server_error=system_routes.server_error,
        bad_request=system_routes.bad_request,
        # 会话 → 用户：Core 的 HTTP 层 + 注入的持久化 store
        user_loader=partial(load_user, store=database),
    )

    router = app.router
    auth_routes.register(router)
    users_routes.register(router)
    admin_routes.register(router)
    blog_routes.register(router, render_not_found=system_routes.not_found)
    # 模块之间不互相 import：seo 需要的文章条目与页面 slug 在这里显式注入
    seo_routes.register(router, articles=blog_logic.sitemap_articles,
                        pages=pages_logic.page_slugs)
    pages_routes.register(router, render_not_found=system_routes.not_found)
    return app


app = create_app()

__all__ = ["STARTUP_HOOKS", "app", "create_app", "prepare_database",
           "resolve_db_path", "warn_about_missing_admin", "warn_on_exposed_bind"]
